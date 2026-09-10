"""Phase 6 - HTTP access to the text-to-SQL engine.

    uvicorn api.main:app --reload
    curl -X POST localhost:8000/ask -H 'Content-Type: application/json' \\
         -d '{"question": "top 5 products by profit"}'

The API is a thin shell over the same generator and executor the CLI uses, so
every guardrail applies here identically - the safety lives in the executor, not
in each caller.

By default this reads the SERVING SNAPSHOT published by the Prefect flow rather
than the live warehouse, so serving traffic can never contend with `dbt build`
for DuckDB's single write lock.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from text2sql.executor import ExecutionError, QueryExecutor, UnsafeQueryError
from text2sql.generator import GenerationError, generate_sql, get_generator
from text2sql.schema import get_schema_context

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Prefer the snapshot; fall back to the live warehouse when it has not been
# published yet, so a fresh clone still works before the first flow run.
_SNAPSHOT = PROJECT_ROOT / "duckdb_warehouse" / "warehouse_serving.duckdb"
_LIVE = Path(os.getenv("DUCKDB_PATH", str(PROJECT_ROOT / "duckdb_warehouse" / "warehouse.duckdb")))

# Populated at startup so the schema context and DuckDB connection are built once
# rather than on every request.
state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open the warehouse connection and load the schema context once, at startup."""
    db_path = os.getenv("TEXT2SQL_DB_PATH") or (_SNAPSHOT if _SNAPSHOT.exists() else _LIVE)
    state["executor"] = QueryExecutor(db_path=db_path)
    state["schema"] = get_schema_context().text
    state["db_path"] = str(db_path)
    try:
        yield
    finally:
        state["executor"].close()


app = FastAPI(
    title="Retail Text-to-SQL API",
    description="Ask the retail warehouse questions in plain English.",
    version="1.0.0",
    lifespan=lifespan,
)


class AskRequest(BaseModel):
    """A natural-language question, with an optional backend override."""

    question: str = Field(..., min_length=1, max_length=1000, examples=["Top 5 products by profit"])
    provider: str | None = Field(None, description="gemini or rules; defaults to the env setting")
    sql_only: bool = Field(False, description="generate the SQL without executing it")


class AskResponse(BaseModel):
    """The generated SQL alongside its result.

    The SQL is always returned, never optional: text-to-SQL fails by producing a
    plausible wrong answer, and the query is how a caller judges whether the
    question was understood.
    """

    question: str
    sql: str
    columns: list[str] = []
    rows: list[list] = []
    row_count: int = 0
    elapsed_ms: float = 0.0
    hit_limit: bool = False
    provider: str = ""


@app.get("/health")
def health() -> dict:
    """Liveness check that also reports which database and backend are in use."""
    return {
        "status": "ok",
        "database": state.get("db_path"),
        "serving_snapshot": state.get("db_path", "").endswith("warehouse_serving.duckdb"),
        "provider": get_generator().name,
    }


@app.get("/schema")
def schema() -> dict:
    """Return the schema context handed to the model - useful for debugging answers."""
    context = get_schema_context()
    return {
        "tables": [t.fqn for t in context.tables],
        "approx_tokens": context.approx_tokens,
    }


@app.post("/ask", response_model=AskResponse)
def ask(request: AskRequest) -> AskResponse:
    """Turn a question into SQL, run it, and return both.

    The three failure modes map to distinct status codes so a client can tell
    them apart: 422 the model could not produce SQL, 400 a guardrail refused it,
    500 the database could not run it.
    """
    try:
        generated = generate_sql(
            request.question, schema_context=state["schema"], provider=request.provider
        )
    except GenerationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if request.sql_only:
        return AskResponse(question=request.question, sql=generated.sql, provider=generated.provider)

    try:
        result = state["executor"].execute(generated.sql)
    except UnsafeQueryError as exc:
        raise HTTPException(status_code=400, detail=f"Blocked by a safety guardrail: {exc}") from exc
    except ExecutionError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return AskResponse(
        question=request.question,
        sql=result.sql,
        columns=result.columns,
        # Values are stringified so Decimals, dates and NULLs all survive JSON.
        rows=[[None if v is None else str(v) for v in row] for row in result.rows],
        row_count=result.row_count,
        elapsed_ms=round(result.elapsed_ms, 2),
        hit_limit=result.hit_limit,
        provider=generated.provider,
    )
