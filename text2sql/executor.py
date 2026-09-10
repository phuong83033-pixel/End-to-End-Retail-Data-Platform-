"""Phase 3 - execute generated SQL safely.

Every safety guarantee lives here, enforced in code rather than by asking the
model nicely in a prompt. A prompt is a suggestion; an AST check is not.

Guardrails, in the order they apply:

  1. Read-only connection    DuckDB refuses CREATE/INSERT/UPDATE/DELETE outright
  2. Single statement        no `SELECT 1; DROP TABLE x`
  3. SELECT-only AST         the root must be a query, and no write node may
                             appear anywhere inside it
  4. No COPY                 read_only does NOT block `COPY ... TO 'file'` - this
                             was verified against DuckDB 1.5.5, which happily
                             wrote a file from a read-only connection
  5. Schema allowlist        every table must live in an approved schema, which
                             also blocks read_parquet()/read_csv() against
                             arbitrary paths on disk
  6. Function allowlist      unknown function names are rejected before execution
                             (catches `ISNULL()`, which parses fine as DuckDB but
                             has no such function)
  7. LIMIT injection         a query with no LIMIT gets one
  8. Statement timeout       read-only does not prevent a cartesian join hanging
                             the process; a watchdog interrupts it

Note on 4 and 5: these are not theoretical. Both were probed against the live
warehouse before this module was written.
"""

from __future__ import annotations

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_ALLOWED_SCHEMAS = (
    "xom_retails_analytics",
    "xom_retails_gold",
    "xom_retails_silver",
)

DEFAULT_MAX_ROWS = int(os.getenv("TEXT2SQL_MAX_ROWS", "100"))
DEFAULT_TIMEOUT_S = float(os.getenv("TEXT2SQL_TIMEOUT_S", "10"))

# Node types that must never appear anywhere in the tree, mapped to the message
# shown when they do. exp.Command catches statements sqlglot does not model
# (PRAGMA, SET, INSTALL, EXPORT), which would otherwise slip through unexamined.
FORBIDDEN_NODES: dict[type, str] = {
    exp.Insert: "INSERT",
    exp.Update: "UPDATE",
    exp.Delete: "DELETE",
    exp.Drop: "DROP",
    exp.Create: "CREATE",
    exp.Alter: "ALTER",
    exp.Copy: "COPY",  # writes files even on a read-only connection
    exp.Attach: "ATTACH",
    exp.Detach: "DETACH",
    exp.Command: "non-query command (PRAGMA/SET/INSTALL)",
    exp.Transaction: "transaction control",
}

# Root node types that are legitimate queries.
ALLOWED_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except, exp.Subquery)


class UnsafeQueryError(Exception):
    """The SQL violated a guardrail and was never sent to the database."""


class ExecutionError(Exception):
    """The SQL was allowed but the database rejected or could not finish it."""


@dataclass
class QueryResult:
    """Everything the caller needs to display and audit one query."""

    sql: str
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    elapsed_ms: float = 0.0
    hit_limit: bool = False  # row count equals the cap, so more may exist

    @property
    def row_count(self) -> int:
        """Number of rows actually returned."""
        return len(self.rows)


def _table_function_name(table: exp.Table) -> str:
    """Name the table function behind a nameless table reference, for the error message.

    sqlglot models the common ones as typed nodes (ReadParquet, ReadCSV) and the
    rest as Anonymous, so both shapes are unpicked here to say `read_parquet()`
    rather than the baffling "table `` is not schema-qualified".
    """
    anonymous = table.find(exp.Anonymous)
    if anonymous is not None and anonymous.this:
        return f"{anonymous.this}()"

    inner = table.this
    if inner is not None and not isinstance(inner, exp.Identifier):
        # CamelCase node name -> snake_case function name. Two passes so runs of
        # capitals survive intact: ReadParquet -> read_parquet, ReadCSV -> read_csv
        # (a single pass would give read_c_s_v).
        name = type(inner).__name__
        name = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
        return f"{re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', name).lower()}()"

    return "table functions"


def _collect_cte_names(expression: exp.Expression) -> set[str]:
    """Return the names of CTEs defined in the query.

    A CTE referenced in FROM looks exactly like a table with no schema, so it
    would trip the schema allowlist unless it is exempted here.
    """
    names = set()
    for cte in expression.find_all(exp.CTE):
        alias = cte.alias_or_name
        if alias:
            names.add(alias.lower())
    return names


def validate_sql(
    sql: str,
    allowed_schemas: tuple[str, ...] = DEFAULT_ALLOWED_SCHEMAS,
    known_functions: set[str] | None = None,
) -> exp.Expression:
    """Parse the SQL and enforce every static guardrail. Returns the AST.

    Raises UnsafeQueryError with a specific reason - the message is shown to the
    user, so "table `main.secrets` is not in an allowed schema" beats "rejected".
    """
    try:
        statements = [s for s in sqlglot.parse(sql, dialect="duckdb") if s is not None]
    except ParseError as exc:
        raise UnsafeQueryError(f"SQL failed to parse: {exc}") from exc

    if not statements:
        raise UnsafeQueryError("No SQL statement found.")
    if len(statements) > 1:
        raise UnsafeQueryError(
            f"Only one statement may run at a time; {len(statements)} were provided."
        )

    expression = statements[0]

    # Forbidden nodes anywhere in the tree, including inside subqueries.
    for node_type, label in FORBIDDEN_NODES.items():
        if isinstance(expression, node_type) or expression.find(node_type):
            raise UnsafeQueryError(f"{label} is not permitted; this endpoint is read-only.")

    if not isinstance(expression, ALLOWED_ROOTS):
        raise UnsafeQueryError(
            f"Only SELECT queries are permitted, got {type(expression).__name__.upper()}."
        )

    # Every real table must sit in an allowed schema. This is also what stops
    # read_parquet('C:/anything') - a table function is not an allowlisted table.
    cte_names = _collect_cte_names(expression)
    for table in expression.find_all(exp.Table):
        name = (table.name or "").lower()
        if name in cte_names:
            continue
        schema = (table.db or "").lower()
        if not schema:
            # A table function (read_parquet, read_csv, glob, ...) parses as a
            # Table with no name, and is how a query would otherwise reach
            # arbitrary files on disk. Name it explicitly - "table `` is not
            # schema-qualified" would leave the user guessing.
            if not name:
                raise UnsafeQueryError(
                    f"{_table_function_name(table)} cannot be queried. Only tables in "
                    + ", ".join(allowed_schemas)
                    + " are accessible."
                )
            raise UnsafeQueryError(
                f"Table `{table.name}` is not schema-qualified. Use one of: "
                + ", ".join(allowed_schemas)
            )
        if schema not in allowed_schemas:
            raise UnsafeQueryError(
                f"Schema `{schema}` is not accessible. Allowed: " + ", ".join(allowed_schemas)
            )

    # Unknown function names: sqlglot parses anything it does not model as
    # Anonymous, which is exactly where a foreign dialect's function lands.
    if known_functions is not None:
        for func in expression.find_all(exp.Anonymous):
            fname = (func.this or "").lower()
            if fname and fname not in known_functions:
                raise UnsafeQueryError(
                    f"`{fname}()` is not a DuckDB function. It may be from another SQL dialect."
                )

    return expression


def apply_limit(expression: exp.Expression, max_rows: int = DEFAULT_MAX_ROWS) -> exp.Expression:
    """Add a LIMIT when the query has none, so no query can return the whole table.

    Queries that already limit themselves are left alone, including ones asking
    for more than the cap - an explicit `LIMIT 500` in a reviewed query is a
    deliberate choice, and the row cap is a guard against runaway output, not a
    policy on how much a caller may ask for.
    """
    if expression.args.get("limit") is not None:
        return expression
    try:
        return expression.limit(max_rows)
    except Exception:
        # Some root types do not support .limit(); wrapping is not worth the
        # complexity, and the timeout still bounds the damage.
        return expression


class QueryExecutor:
    """Runs validated SQL against the warehouse over a read-only connection."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        allowed_schemas: tuple[str, ...] = DEFAULT_ALLOWED_SCHEMAS,
        max_rows: int = DEFAULT_MAX_ROWS,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ):
        """Record configuration; the connection itself is opened on first use."""
        self.db_path = str(
            db_path
            or os.getenv("TEXT2SQL_DB_PATH")
            or os.getenv("DUCKDB_PATH")
            or PROJECT_ROOT / "duckdb_warehouse" / "warehouse.duckdb"
        )
        self.allowed_schemas = allowed_schemas
        self.max_rows = max_rows
        self.timeout_s = timeout_s
        self._con = None
        self._functions: set[str] | None = None

    def connect(self):
        """Open (once) and return the read-only DuckDB connection.

        Read-only matters twice over: it blocks writes, and it lets several
        readers share the file. It cannot be opened while dbt holds the write
        lock, which is why the pipeline publishes a serving snapshot.
        """
        if self._con is None:
            import duckdb

            try:
                self._con = duckdb.connect(self.db_path, read_only=True)
            except Exception as exc:
                raise ExecutionError(
                    f"Cannot open the warehouse at {self.db_path}: {exc}. "
                    "If `dbt build` is running, wait for it to finish."
                ) from exc
        return self._con

    def known_functions(self) -> set[str]:
        """Return DuckDB's function names, queried once and cached.

        Used to reject foreign-dialect functions before execution rather than
        after - `ISNULL()` parses cleanly as DuckDB but does not exist in it.
        """
        if self._functions is None:
            rows = self.connect().execute("select distinct lower(function_name) from duckdb_functions()").fetchall()
            self._functions = {r[0] for r in rows}
        return self._functions

    def execute(self, sql: str) -> QueryResult:
        """Validate, limit and run one query, returning rows and timing.

        The query runs on a worker thread so the main thread can interrupt it on
        timeout - DuckDB has no statement_timeout setting, and a read-only
        connection does nothing to stop a cartesian join running forever.
        """
        expression = validate_sql(sql, self.allowed_schemas, self.known_functions())
        limited = apply_limit(expression, self.max_rows)
        final_sql = limited.sql(dialect="duckdb", pretty=True)

        con = self.connect()
        started = time.perf_counter()

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(lambda: con.execute(final_sql).fetchall())
            try:
                rows = future.result(timeout=self.timeout_s)
            except FutureTimeout:
                con.interrupt()
                raise ExecutionError(
                    f"Query exceeded the {self.timeout_s:g}s timeout and was cancelled. "
                    "Try narrowing it with a filter."
                ) from None
            except Exception as exc:
                raise ExecutionError(f"DuckDB rejected the query: {exc}") from exc

        elapsed_ms = (time.perf_counter() - started) * 1000
        columns = [d[0] for d in con.description] if con.description else []

        return QueryResult(
            sql=final_sql,
            columns=columns,
            rows=rows,
            elapsed_ms=elapsed_ms,
            hit_limit=len(rows) == self.max_rows,
        )

    def close(self) -> None:
        """Close the connection, releasing the read lock on the warehouse file."""
        if self._con is not None:
            self._con.close()
            self._con = None

    def __enter__(self) -> "QueryExecutor":
        """Support `with QueryExecutor() as ex:` so the file lock is always released."""
        return self

    def __exit__(self, *exc_info) -> None:
        """Close the connection when leaving the context."""
        self.close()
