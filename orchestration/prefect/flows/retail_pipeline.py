"""Prefect flow orchestrating the whole retail pipeline.

    SQL Server  --ingest-->  MinIO (Bronze)  --dbt build-->  DuckDB (Silver + Gold)
                                                          \\--dbt docs--> lineage site

Every stage is a separate Prefect task with its own switch, so a failed or
changed stage can be re-run on its own instead of repeating the whole pipeline.
Run history, timings and failures show up in the Prefect UI at
http://localhost:4200.

Usage:
    python orchestration/prefect/flows/retail_pipeline.py              # run everything once
    python orchestration/prefect/flows/retail_pipeline.py --no-ingest  # transform only
    python orchestration/prefect/flows/retail_pipeline.py --serve      # expose it in the UI
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

# Point the client at the Prefect server from docker-compose unless the caller
# already chose one. Without this, runs go to an ephemeral local server and
# never appear in the UI.
os.environ.setdefault("PREFECT_API_URL", "http://localhost:4200/api")

from prefect import flow, get_run_logger, task  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DBT_DIR = PROJECT_ROOT / "dbt"

# The ingestion module lives in Ingestion/ and imports `config` from the root.
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "Ingestion"))


def resolve_dbt_executable() -> str:
    """Return the dbt executable, preferring the project's virtualenv copy.

    Falls back to whatever `dbt` is on PATH so the flow still works if it is
    ever run from a different environment (a container, CI).
    """
    candidates = [
        PROJECT_ROOT / "venv" / "Scripts" / "dbt.exe",  # Windows
        PROJECT_ROOT / "venv" / "bin" / "dbt",  # Linux/macOS
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return "dbt"


def run_dbt_command(args: list[str]) -> str:
    """Run a dbt command in the project directory and return its stdout.

    dbt is invoked as a subprocess rather than imported so its dependency tree
    stays isolated from Prefect's. Raises RuntimeError with the captured output
    when dbt exits non-zero, which marks the Prefect task as failed.
    """
    command = [resolve_dbt_executable(), *args, "--profiles-dir", "."]
    result = subprocess.run(
        command, cwd=DBT_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        raise RuntimeError(f"`dbt {' '.join(args)}` failed (exit {result.returncode}):\n{output}")
    return output


@task(name="ingest-bronze", retries=2, retry_delay_seconds=30)
def ingest_bronze(tables: list[str] | None = None) -> dict:
    """Extract the source tables from SQL Server into MinIO as Parquet.

    Retries twice because this is the only stage that depends on an external
    network hop. The load is a full snapshot (`replace`), so a retry after a
    partial failure is safe rather than duplicating rows.
    """
    logger = get_run_logger()
    import ingestion  # imported here so a Prefect worker picks up the project sys.path

    selected = tuple(tables) if tables else ingestion.SOURCE_TABLES
    logger.info("Ingesting %s into Bronze", ", ".join(selected))
    ingestion.run_dlt_pipeline(tables=selected)
    logger.info("Bronze ingestion complete")
    return {"tables": list(selected)}


@task(name="dbt-build")
def dbt_build(select: str | None = None) -> str:
    """Build the Silver and Gold layers and run every data test.

    `select` maps to dbt's node selection (e.g. "marts" or "stg_sales+"), so a
    single model and its children can be rebuilt without touching the rest.
    """
    logger = get_run_logger()
    args = ["build"]
    if select:
        args += ["--select", select]
    logger.info("Running dbt %s", " ".join(args))
    output = run_dbt_command(args)

    # Surface dbt's own summary line (PASS=.. ERROR=..) in the Prefect run log.
    for line in output.splitlines():
        if "Done." in line or "Completed" in line:
            logger.info(line.strip())
    return output


@task(name="dbt-docs-generate")
def dbt_docs_generate() -> str:
    """Regenerate the dbt documentation site served at http://localhost:8081.

    nginx serves dbt/target/ directly, so refreshing these artefacts is enough -
    the container does not need restarting.
    """
    logger = get_run_logger()
    output = run_dbt_command(["docs", "generate"])
    logger.info("dbt docs regenerated - refresh http://localhost:8081")
    return output


@flow(name="retail-sales-pipeline")
def retail_pipeline(
    run_ingestion: bool = True,
    run_transform: bool = True,
    run_docs: bool = True,
    tables: list[str] | None = None,
    dbt_select: str | None = None,
) -> None:
    """Run the retail pipeline end to end, or any subset of its stages.

    Each stage has its own flag so you never have to repeat work that already
    succeeded: flip `run_ingestion` off to rebuild the warehouse from the Bronze
    data already in MinIO, or pass `dbt_select` to rebuild a single model.
    """
    logger = get_run_logger()
    logger.info(
        "stages -> ingestion=%s transform=%s docs=%s", run_ingestion, run_transform, run_docs
    )

    if run_ingestion:
        ingest_bronze(tables=tables)
    else:
        logger.info("Skipping ingestion - using the Bronze data already in MinIO")

    if run_transform:
        dbt_build(select=dbt_select)
    else:
        logger.info("Skipping dbt build")

    if run_docs:
        dbt_docs_generate()

    logger.info("Pipeline finished")


def parse_args() -> argparse.Namespace:
    """Parse the command line options for running the flow directly."""
    parser = argparse.ArgumentParser(description="Run the retail sales pipeline via Prefect.")
    parser.add_argument("--no-ingest", action="store_true", help="Skip the Bronze ingestion stage.")
    parser.add_argument("--no-transform", action="store_true", help="Skip the dbt build stage.")
    parser.add_argument("--no-docs", action="store_true", help="Skip regenerating the dbt docs.")
    parser.add_argument("--tables", nargs="+", help="Subset of source tables to ingest.")
    parser.add_argument("--select", dest="dbt_select", help="dbt node selection for the build.")
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Register the flow as a deployment and wait for runs triggered from the Prefect UI.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if args.serve:
        # Creates a deployment and blocks, polling for runs started from the UI
        # or by the schedule. Stop it with Ctrl+C.
        retail_pipeline.serve(name="retail-sales-pipeline")
    else:
        retail_pipeline(
            run_ingestion=not args.no_ingest,
            run_transform=not args.no_transform,
            run_docs=not args.no_docs,
            tables=args.tables,
            dbt_select=args.dbt_select,
        )
