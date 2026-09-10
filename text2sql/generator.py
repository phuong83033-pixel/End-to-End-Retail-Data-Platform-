"""Phase 2 - turn a natural-language question into validated DuckDB SQL.

Nothing here executes SQL. Generation and execution are separate on purpose: the
executor (Phase 3) owns every safety guarantee, so this module can be exercised
freely without a database in reach.

Two backends behind one interface:

    GeminiGenerator     gemini-2.5-flash - free tier (15 RPM / 1,500 RPD), no card
    RuleBasedGenerator  offline pattern matching, no key and no network required

Gemini is the only hosted model on purpose: the free tier means anyone who clones
this repo can run the engine without paying for API access. The SQLGenerator
protocol keeps another provider a small addition if that ever changes.

The SDK is imported lazily inside the class, so this module works without
`google-genai` installed and only the offline path is needed to develop against.

Whatever the backend, output goes through SQLGlot: parsed to an AST (which rejects
malformed SQL before it ever reaches DuckDB), transpiled to the DuckDB dialect,
and pretty-printed.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Protocol

import sqlglot
from sqlglot.errors import ParseError

from text2sql.prompts import build_system_prompt

# Override with TEXT2SQL_MODEL (e.g. gemini-2.5-pro for harder questions).
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"


class GenerationError(RuntimeError):
    """Raised when a question cannot be turned into valid SQL."""


@dataclass
class GeneratedSQL:
    """A generated statement plus the provenance needed to judge it."""

    sql: str
    provider: str
    model: str | None = None
    raw: str | None = None  # pre-formatting text, for debugging a bad generation


def strip_markdown_fence(text: str) -> str:
    """Remove ```sql fences that models add despite being told not to.

    Instructing the model is not enough on its own - this is cheap insurance, and
    a stray fence would otherwise fail SQLGlot parsing for a purely cosmetic reason.
    """
    fenced = re.search(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        return fenced.group(1).strip()
    return text.strip()


# Dialects tried, in order, when a generation does not parse as DuckDB. Models
# occasionally slip into a dialect they saw more often in training. Reading in
# the source dialect and writing DuckDB repairs those instead of failing outright.
#
# This only catches SYNTAX-level differences - `SELECT TOP n`, backtick-quoted
# identifiers, and similar. It cannot catch foreign FUNCTION names: sqlglot parses
# an unknown function as a generic call, so `ISNULL(x, 0)` parses cleanly as
# DuckDB, never reaches the fallbacks, and surfaces at execution as a DuckDB
# catalog error instead. That error is clear enough to act on ("Scalar Function
# with name isnull does not exist"), so it is left to the executor rather than
# guessed at here.
FALLBACK_DIALECTS = ("tsql", "postgres", "mysql", "snowflake")


def format_sql(raw_sql: str) -> str:
    """Parse, validate and pretty-print SQL in the DuckDB dialect.

    The parse is the point: it turns malformed generated SQL into a clear
    GenerationError here, rather than an opaque database error later. DuckDB is
    tried first; other dialects are attempted only as repair, and anything that
    parses in none of them is rejected.
    """
    cleaned = strip_markdown_fence(raw_sql)
    if not cleaned:
        raise GenerationError("The model returned an empty response.")

    first_error: Exception | None = None
    for dialect in ("duckdb", *FALLBACK_DIALECTS):
        try:
            statements = sqlglot.transpile(cleaned, read=dialect, write="duckdb", pretty=True)
        except ParseError as exc:
            first_error = first_error or exc
            continue

        if statements:
            return statements[0].strip().rstrip(";") + ";"

    if first_error is not None:
        raise GenerationError(f"Generated SQL failed to parse: {first_error}")
    raise GenerationError("Generated text contained no SQL statement.")


class SQLGenerator(Protocol):
    """The contract every backend implements."""

    name: str

    def generate(self, question: str, schema_context: str) -> GeneratedSQL:
        """Return DuckDB SQL answering `question` against the given schema."""
        ...


class GeminiGenerator:
    """Generate SQL with Gemini. Needs GOOGLE_API_KEY and the `google-genai` SDK."""

    name = "gemini"

    def __init__(self, model: str | None = None, api_key: str | None = None):
        """Store configuration; the SDK is imported on first use, not at import time."""
        self.model = model or os.getenv("TEXT2SQL_MODEL", DEFAULT_GEMINI_MODEL)
        self.api_key = api_key or os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")

    def generate(self, question: str, schema_context: str) -> GeneratedSQL:
        """Ask Gemini for SQL, then validate and format it."""
        if not self.api_key:
            raise GenerationError("GOOGLE_API_KEY is not set.")
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise GenerationError("The `google-genai` package is not installed.") from exc
    
        client = genai.Client(api_key=self.api_key)
        response = client.models.generate_content(
            model=self.model,
            contents=question,
            config=types.GenerateContentConfig(
                system_instruction=build_system_prompt(schema_context),
                temperature=0,
            ),
        )
        return GeneratedSQL(
            sql=format_sql(response.text), provider=self.name, model=self.model, raw=response.text
        )


class RuleBasedGenerator:
    """Offline fallback: map common question shapes onto the analytics marts.

    Not a general engine and not pretending to be one - it covers the handful of
    questions the marts were built for, so the CLI still demonstrates the full
    path with no API key and no network. Anything it does not recognise raises,
    rather than guessing at SQL that might silently answer the wrong question.
    """

    name = "rules"

    # (pattern, SQL). First match wins, so order matters: more specific first.
    RULES: list[tuple[str, str]] = [
        (
            r"\b(bought|sold|purchased)\s+together\b|\baffinit|\bbasket\b|\bgo(es)? with\b",
            "SELECT antecedent_subcategory, consequent_subcategory, pair_order_count, confidence, lift\n"
            "FROM xom_retails_analytics.rpt_subcategory_affinity\n"
            "ORDER BY pair_order_count DESC\nLIMIT 10;",
        ),
        (
            r"\bper\s+(square|sqm|m2)\b|\bsquare\s+met",
            "SELECT store_key, country, state, square_meters, revenue, revenue_per_sqm\n"
            "FROM xom_retails_analytics.rpt_store_performance\n"
            "WHERE NOT is_online\nORDER BY revenue_per_sqm DESC\nLIMIT 10;",
        ),
        (
            r"\b(month|monthly|over time|trend|by year|yearly)\b",
            "SELECT month_start, revenue, profit, order_count, is_partial_period\n"
            "FROM xom_retails_analytics.rpt_monthly_revenue\nORDER BY month_start;",
        ),
        (
            r"\bstores?\b",
            "SELECT store_key, country, state, is_online, revenue, profit, margin_pct\n"
            "FROM xom_retails_analytics.rpt_store_performance\nORDER BY revenue DESC\nLIMIT 10;",
        ),
        (
            r"\b(categor|subcategor)",
            "SELECT category, subcategory, revenue, profit, margin_pct, revenue_share_pct\n"
            "FROM xom_retails_analytics.rpt_category_performance\nORDER BY revenue DESC\nLIMIT 10;",
        ),
        (
            r"\bcustomers?\b",
            "SELECT customer_key, customer_name, country, order_count, revenue, avg_order_value\n"
            "FROM xom_retails_analytics.rpt_customer_performance\n"
            "WHERE has_purchased\nORDER BY revenue DESC\nLIMIT 10;",
        ),
        (
            r"\bhow many orders\b|\border count\b|\bnumber of orders\b",
            "SELECT COUNT(DISTINCT order_id) AS order_count\nFROM xom_retails_gold.fact_sales;",
        ),
        (
            r"\btotal\b.*\b(revenue|profit|sales|margin)\b",
            "SELECT SUM(gross_sales) AS total_revenue, SUM(gross_margin) AS total_profit\n"
            "FROM xom_retails_gold.fact_sales;",
        ),
        (
            r"\bproducts?\b|\bbest.?sell|\btop\b",
            "SELECT product_name, category, order_count, units_sold, revenue, profit, margin_pct\n"
            "FROM xom_retails_analytics.rpt_product_performance\nORDER BY revenue DESC\nLIMIT 10;",
        ),
    ]

    def generate(self, question: str, schema_context: str) -> GeneratedSQL:
        """Match the question against the known patterns, or raise if none apply."""
        text = question.lower()
        for pattern, sql in self.RULES:
            if re.search(pattern, text):
                return GeneratedSQL(sql=format_sql(sql), provider=self.name, model=None, raw=sql)
        raise GenerationError(
            "The offline rule engine does not recognise this question. "
            "Set GOOGLE_API_KEY to use Gemini for arbitrary questions - the free "
            "tier at aistudio.google.com needs no payment details."
        )


def get_generator(provider: str | None = None) -> SQLGenerator:
    """Return the configured backend, falling back sensibly when no key is set.

    Explicit LLM_PROVIDER wins; otherwise Gemini is used when a key is present and
    the offline engine is the last resort - so the tool always starts, and only
    reports a missing key when a question actually needs one.
    """
    provider = (provider or os.getenv("LLM_PROVIDER") or "").strip().lower()

    if provider in ("gemini", "google"):
        return GeminiGenerator()
    if provider in ("rules", "offline"):
        return RuleBasedGenerator()
    if provider:
        raise GenerationError(
            f"Unknown LLM_PROVIDER {provider!r}. Supported: 'gemini', 'rules'."
        )

    if os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"):
        return GeminiGenerator()
    return RuleBasedGenerator()


def generate_sql(question: str, schema_context: str | None = None, provider: str | None = None):
    """Convenience wrapper: pick a backend, load the schema, generate SQL."""
    if schema_context is None:
        from text2sql.schema import get_schema_context

        schema_context = get_schema_context().text
    return get_generator(provider).generate(question, schema_context)

if __name__ == "__main__":
    test_query = "What is the revenue and profit"
    print(f"Prompt:'{test_query}' \nGenerated Sql:{generate_sql(test_query)} ")