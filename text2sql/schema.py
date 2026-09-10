"""Phase 1 - build the schema context handed to the LLM.

This is the *only* thing about the warehouse the model ever sees: table names,
column names, types, descriptions and join keys. Never data rows.

Everything is derived from dbt's own artefacts, so the context cannot drift from
the warehouse:

    dbt/target/manifest.json  -> model + column descriptions, relationship tests
    dbt/target/catalog.json   -> the real column list and types
    warehouse.duckdb          -> row counts (optional)

`dbt docs generate` refreshes the first two, and the Prefect flow already runs it
as its last stage - so the context updates itself whenever the pipeline runs.

Run `python -m text2sql.schema` to print the context and its size.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DBT_TARGET = PROJECT_ROOT / "dbt" / "target"

# Schemas exposed to the engine, in the order they are presented to the model.
# Analytics comes first because a single-table lookup beats join reasoning, and
# Bronze is excluded entirely - raw Parquet is not a query target.
SCHEMA_ORDER = ("xom_retails_analytics", "xom_retails_gold", "xom_retails_silver")

SCHEMA_NOTES = {
    "xom_retails_analytics": (
        "Pre-aggregated reporting marts. PREFER THESE: most business questions "
        "are a single SELECT here with no joins."
    ),
    "xom_retails_gold": (
        "Star schema. Use when the analytics marts do not cover the question - "
        "join fact_sales to the dimensions on their *_key columns."
    ),
    "xom_retails_silver": (
        "Cleaned source tables. Use only for detail not carried into Gold."
    ),
}


@dataclass
class TableInfo:
    """One table's shape and documentation, assembled from the dbt artefacts."""

    schema: str
    name: str
    description: str
    columns: list[tuple[str, str, str]] = field(default_factory=list)  # (name, type, description)
    row_count: int | None = None

    @property
    def fqn(self) -> str:
        """Fully qualified name as it must appear in generated SQL."""
        return f"{self.schema}.{self.name}"


@dataclass
class SchemaContext:
    """The rendered schema text plus the structured tables behind it."""

    text: str
    tables: list[TableInfo]
    fingerprint: float  # manifest mtime, used to invalidate the cache

    @property
    def approx_tokens(self) -> int:
        """Rough token estimate (~4 characters per token) for budgeting prompts."""
        return len(self.text) // 4


def _load_json(path: Path) -> dict:
    """Read a dbt artefact, with an actionable error if it has not been built."""
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Run `dbt docs generate --profiles-dir .` in the dbt/ folder first."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _model_nodes(manifest: dict) -> dict:
    """Return only the model nodes from a dbt manifest, keyed by unique id."""
    return {k: v for k, v in manifest["nodes"].items() if v["resource_type"] == "model"}


def extract_foreign_keys(manifest: dict) -> list[tuple[str, str, str, str]]:
    """Recover join keys from dbt's `relationships` tests.

    Every relationship test states that one model's column points at another
    model's column - which is exactly the FK information an LLM needs to write
    correct joins, and it is already asserted on every build. Returns tuples of
    (from_table, from_column, to_table, to_column).
    """
    models = _model_nodes(manifest)
    edges: list[tuple[str, str, str, str]] = []

    for node in manifest["nodes"].values():
        meta = node.get("test_metadata") or {}
        if meta.get("name") != "relationships":
            continue

        source_id = node.get("attached_node")
        if source_id not in models:
            continue

        # The referenced model is the dependency that is not the attached model.
        targets = [n for n in node["depends_on"]["nodes"] if n != source_id and n in models]
        if not targets:
            continue

        kwargs = meta.get("kwargs", {})
        edges.append(
            (
                models[source_id]["alias"],
                node.get("column_name") or kwargs.get("column_name", "?"),
                models[targets[0]]["alias"],
                kwargs.get("field", "?"),
            )
        )

    return sorted(set(edges))


def _fetch_row_counts(db_path: Path, tables: list[TableInfo]) -> None:
    """Annotate tables with live row counts, read-only and best-effort.

    Row counts tell the model how big each table is, which is useful context for
    grain questions. This is deliberately non-fatal: DuckDB allows a single
    writer, so the file may be locked by a running `dbt build`, and a missing
    row count is far better than a crashed schema build.
    """
    try:
        import duckdb

        con = duckdb.connect(str(db_path), read_only=True)
    except Exception:
        return

    try:
        for table in tables:
            try:
                table.row_count = con.execute(f"select count(*) from {table.fqn}").fetchone()[0]
            except Exception:
                continue
    finally:
        con.close()


def _render_table(table: TableInfo) -> str:
    """Render one table as annotated CREATE TABLE DDL.

    DDL is used because it is the form the model has seen most often in training;
    the descriptions ride along as trailing comments on each column.
    """
    header = []
    if table.description:
        for line in table.description.strip().splitlines():
            header.append(f"-- {line.strip()}")
    if table.row_count is not None:
        header.append(f"-- Rows: {table.row_count:,}")

    width = max((len(c[0]) for c in table.columns), default=0)
    last = len(table.columns) - 1
    lines = []
    for index, (col_name, col_type, col_desc) in enumerate(table.columns):
        # The separator has to be decided per line, because the comma must sit
        # before the trailing comment - not after it.
        separator = "" if index == last else ","
        entry = f"  {col_name.ljust(width)}  {col_type}{separator}"
        if col_desc:
            entry = entry.ljust(width + 26) + f"  -- {' '.join(col_desc.split())}"
        lines.append(entry)

    body = "\n".join(lines)
    return "\n".join(header) + f"\nCREATE TABLE {table.fqn} (\n{body}\n);"


def build_schema_context(
    manifest_path: Path | None = None,
    catalog_path: Path | None = None,
    db_path: Path | None = None,
    schemas: tuple[str, ...] = SCHEMA_ORDER,
    include_row_counts: bool = True,
) -> SchemaContext:
    """Assemble the full schema context string from the dbt artefacts.

    Columns come from the catalog (the real table shape) and descriptions are
    attached from the manifest where they exist - that direction matters, since
    the manifest only lists documented columns and would silently hide the rest.
    """
    manifest_path = manifest_path or DBT_TARGET / "manifest.json"
    catalog_path = catalog_path or DBT_TARGET / "catalog.json"
    db_path = db_path or Path(
        os.getenv("DUCKDB_PATH", str(PROJECT_ROOT / "duckdb_warehouse" / "warehouse.duckdb"))
    )

    manifest = _load_json(manifest_path)
    catalog = _load_json(catalog_path)
    models = _model_nodes(manifest)

    tables: list[TableInfo] = []
    for unique_id, node in models.items():
        if node["schema"] not in schemas:
            continue

        cat_node = catalog["nodes"].get(unique_id)
        if cat_node is None:
            continue  # model documented but not yet built

        documented = node.get("columns", {})
        table = TableInfo(
            schema=node["schema"],
            name=node["alias"],
            description=" ".join((node.get("description") or "").split()),
        )
        for col_name, col in sorted(cat_node["columns"].items(), key=lambda kv: kv[1]["index"]):
            table.columns.append(
                (
                    col_name,
                    col["type"],
                    (documented.get(col_name, {}) or {}).get("description", "") or "",
                )
            )
        tables.append(table)

    # Present schemas in the configured order, tables alphabetically within each.
    tables.sort(key=lambda t: (schemas.index(t.schema), t.name))

    if include_row_counts:
        _fetch_row_counts(db_path, tables)

    sections: list[str] = []
    for schema in schemas:
        in_schema = [t for t in tables if t.schema == schema]
        if not in_schema:
            continue
        sections.append(
            f"-- {'=' * 70}\n-- SCHEMA {schema}\n-- {SCHEMA_NOTES.get(schema, '')}\n-- {'=' * 70}"
        )
        sections.extend(_render_table(t) for t in in_schema)

    edges = extract_foreign_keys(manifest)
    if edges:
        fk_lines = "\n".join(
            f"--   {src}.{src_col} -> {dst}.{dst_col}" for src, src_col, dst, dst_col in edges
        )
        sections.append(
            "-- " + "=" * 70 + "\n-- JOIN KEYS (verified by dbt relationship tests on every build)\n"
            + "-- " + "=" * 70 + "\n" + fk_lines
        )

    return SchemaContext(
        text="\n\n".join(sections),
        tables=tables,
        fingerprint=manifest_path.stat().st_mtime,
    )


_CACHE: SchemaContext | None = None


def get_schema_context(refresh: bool = False, **kwargs) -> SchemaContext:
    """Return the schema context, rebuilding only when the manifest has changed.

    Building costs a few JSON loads and one query per table, so it is cached for
    the life of the process and invalidated by the manifest's modification time -
    meaning a `dbt docs generate` is picked up automatically.
    """
    global _CACHE
    manifest_path = kwargs.get("manifest_path") or DBT_TARGET / "manifest.json"

    if not refresh and _CACHE is not None and manifest_path.exists():
        if _CACHE.fingerprint == manifest_path.stat().st_mtime:
            return _CACHE

    _CACHE = build_schema_context(**kwargs)
    return _CACHE


def main() -> None:
    """Print the schema context and its size - the CLI entry point for eyeballing it."""
    context = get_schema_context()
    print(context.text)
    print()
    print(f"-- tables: {len(context.tables)}")
    print(f"-- columns: {sum(len(t.columns) for t in context.tables)}")
    print(f"-- characters: {len(context.text):,}  (~{context.approx_tokens:,} tokens)")


if __name__ == "__main__":
    main()
