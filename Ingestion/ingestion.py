"""Milestone 1 - Bronze ingestion.

Extracts the four Xom Data retail tables from SQL Server and lands them in
MinIO (s3://bronze/xom_retails/<table>/) as Parquet, using dlt.

The load is a full snapshot (`write_disposition="replace"`), so the pipeline is
repeatable: running it twice produces the same Bronze layer. Every row is
stamped with ingestion metadata (`ingested_at_utc`, `source_system`,
`source_table`) for lineage. The names deliberately avoid a leading underscore,
which dlt reserves for its own columns (`_dlt_load_id`, `_dlt_id`).

Usage:
    python Ingestion/ingestion.py                      # all tables
    python Ingestion/ingestion.py --tables sales       # a single table
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

import dlt
import pymssql
from dlt.destinations import filesystem

# `config.py` lives in the project root, one level above this file. Put the root
# on sys.path so the script also works when launched from inside Ingestion/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402  (import must follow the sys.path bootstrap above)

# Tables to ingest. This tuple doubles as the allowlist for SQL generation and
# as the resource/folder names in the Bronze layer.
SOURCE_TABLES = ("customers", "products", "sales", "stores")

PIPELINE_NAME = "xom_retails"
DATASET_NAME = "xom_retails"
BATCH_SIZE = 1000

logger = logging.getLogger("ingestion")


def get_sql_server_connection() -> pymssql.Connection:
    """Open a read-only connection to the Xom Data SQL Server source."""
    return pymssql.connect(**config.get_sql_server_connection_kwargs())


def query_table(table_name: str, ingested_at_utc: datetime) -> Iterator[dict]:
    """Stream every row of a source table, enriched with ingestion metadata.

    Rows are fetched in batches of BATCH_SIZE so a large table never has to fit
    in memory. `table_name` is checked against SOURCE_TABLES before it reaches
    the SQL string, so no untrusted value can be interpolated into the query.
    """
    if table_name not in SOURCE_TABLES:
        raise ValueError(
            f"Unknown source table {table_name!r}; expected one of {SOURCE_TABLES}."
        )

    row_count = 0
    with get_sql_server_connection() as conn:
        cursor = conn.cursor(as_dict=True)
        cursor.execute(f"SELECT * FROM {config.OLTP_SCHEMA}.{table_name}")
        while True:
            rows = cursor.fetchmany(BATCH_SIZE)
            if not rows:
                break
            for row in rows:
                row["ingested_at_utc"] = ingested_at_utc
                row["source_system"] = config.SOURCE_SYSTEM
                row["source_table"] = f"{config.OLTP_SCHEMA}.{table_name}"
                row_count += 1
                yield row

    logger.info("Extracted %s rows from %s.%s", f"{row_count:,}", config.OLTP_SCHEMA, table_name)


def build_table_reader(table_name: str, ingested_at_utc: datetime):
    """Return a zero-argument generator function that reads one source table.

    dlt resources are built from callables, so each table needs its own reader
    with `table_name` bound - this factory avoids the late-binding closure bug
    that a plain loop would introduce.
    """

    def read_rows() -> Iterator[dict]:
        yield from query_table(table_name, ingested_at_utc)

    read_rows.__name__ = table_name
    return read_rows


@dlt.source(name=PIPELINE_NAME)
def sqlserver_source(
    tables: Sequence[str] = SOURCE_TABLES, ingested_at_utc: datetime | None = None
):
    """Declare one dlt resource per source table.

    The resource name becomes the Bronze folder name, so the tables land at
    s3://bronze/xom_retails/{customers,products,sales,stores}/ - matching the
    source names the dbt project reads from.
    """
    stamp = ingested_at_utc or datetime.now(timezone.utc)
    return [
        dlt.resource(
            build_table_reader(table_name, stamp),
            name=table_name,
            write_disposition="replace",
        )
        for table_name in tables
    ]


def ensure_bucket_exists() -> None:
    """Create the Bronze bucket in MinIO if it does not exist yet."""
    client = config.get_minio_client()
    if not client.bucket_exists(config.MINIO_BRONZE_BUCKET):
        client.make_bucket(config.MINIO_BRONZE_BUCKET)
        logger.info("Created MinIO bucket %r", config.MINIO_BRONZE_BUCKET)
    else:
        logger.info("MinIO bucket %r already exists", config.MINIO_BRONZE_BUCKET)


def run_dlt_pipeline(tables: Sequence[str] = SOURCE_TABLES):
    """Run the full Bronze ingestion: SQL Server -> Parquet in MinIO.

    Returns the dlt pipeline object so callers can inspect the load state.
    """
    config.validate_config()
    started_at = time.monotonic()
    ingested_at_utc = datetime.now(timezone.utc)

    ensure_bucket_exists()

    pipeline = dlt.pipeline(
        pipeline_name=PIPELINE_NAME,
        destination=filesystem(
            bucket_url=config.get_bronze_bucket_url(),
            credentials=config.get_dlt_filesystem_credentials(),
        ),
        dataset_name=DATASET_NAME,
    )

    logger.info(
        "Loading %s into %s/%s ...",
        ", ".join(tables),
        config.get_bronze_bucket_url(),
        DATASET_NAME,
    )
    info = pipeline.run(
        sqlserver_source(tables=tables, ingested_at_utc=ingested_at_utc),
        loader_file_format="parquet",
    )

    logger.info("Load packages: %s", [package.load_id for package in info.load_packages])
    logger.info("Ingestion finished in %.1fs", time.monotonic() - started_at)
    return pipeline


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments for the ingestion script."""
    parser = argparse.ArgumentParser(description="Ingest Xom Data retail tables into MinIO Bronze.")
    parser.add_argument(
        "--tables",
        nargs="+",
        choices=SOURCE_TABLES,
        default=list(SOURCE_TABLES),
        help="Subset of tables to ingest (default: all).",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    args = parse_args()
    run_dlt_pipeline(tables=args.tables)
