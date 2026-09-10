"""Phase 2 - the system instruction handed to the LLM alongside the schema.

The schema tells the model what exists; this file tells it what the data *means*.
Every rule below comes from profiling the source, and each one exists because
without it the engine produces an answer that is wrong in a way the reader cannot
detect - which is far worse than an error.

The few-shot examples are executed against the live warehouse by
`tests/test_prompts.py`, so a broken example fails the test suite rather than
quietly teaching the model bad SQL.
"""

from __future__ import annotations

# Rules the model must follow. Ordered by how much damage they prevent.
BUSINESS_RULES = [
    "GRAIN: fact_sales has one row per ORDER LINE, not per order. It holds 62,884 "
    "rows across only 26,326 orders. Count orders with COUNT(DISTINCT order_id) - "
    "COUNT(*) over-counts by roughly 2.4x.",

    "PROFIT: use the gross_margin column (or the `profit` column in the analytics "
    "marts). It is computed from each product's CURRENT price and cost because the "
    "source records no transaction-time price, so profit trends over time reflect "
    "today's prices, not historical ones.",

    "DELIVERY DATES: delivery_date_key is NULL for in-store orders by design - only "
    "the online store (store_key = 0) records one. Never inner-join or filter on it "
    "unless the question is specifically about online deliveries; doing so silently "
    "drops 79% of rows.",

    "CUSTOMERS: dim_customer holds 15,266 customers but only 11,887 ever purchased. "
    "For 'average revenue per customer', use rpt_customer_performance and decide "
    "explicitly whether to filter has_purchased = true.",

    "STORES: store_key = 0 is the online channel, not a physical shop. It has NULL "
    "square_meters, so exclude it (is_online = false) for revenue-per-square-meter "
    "or any per-location comparison.",

    "DATES: fact_sales.date_key is an integer yyyymmdd, not a date. Join to dim_date "
    "and use dim_date.full_date for any filtering or grouping by time.",

    "COVERAGE: data runs 2016-01-01 to 2021-02-20. February 2021 is a partial month "
    "(rpt_monthly_revenue.is_partial_period flags it) and looks like a collapse in "
    "revenue if treated as complete. There is no data after 2021-02-20, so never "
    "filter on the current date.",

    "CURRENCY: every amount is USD. There is no currency column and no exchange rates.",

    "PREFER the xom_retails_analytics marts - most questions are a single SELECT "
    "there with no joins. Fall back to the gold star schema only when no mart fits.",
]

# Verified against the warehouse - see tests/test_prompts.py.
FEW_SHOT_EXAMPLES = [
    (
        "What is the total revenue and profit?",
        "SELECT SUM(gross_sales) AS total_revenue, SUM(gross_margin) AS total_profit\n"
        "FROM xom_retails_gold.fact_sales;",
    ),
    (
        "Top 5 products by profit",
        "SELECT product_name, category, revenue, profit, margin_pct\n"
        "FROM xom_retails_analytics.rpt_product_performance\n"
        "ORDER BY profit DESC\n"
        "LIMIT 5;",
    ),
    (
        # Demonstrates the grain rule: orders are distinct, rows are not.
        "How many orders were placed in 2019?",
        "SELECT COUNT(DISTINCT f.order_id) AS order_count\n"
        "FROM xom_retails_gold.fact_sales AS f\n"
        "JOIN xom_retails_gold.dim_date AS d ON f.date_key = d.date_key\n"
        "WHERE d.year = 2019;",
    ),
    (
        "Show monthly revenue in 2020",
        "SELECT month_start, revenue, profit, order_count\n"
        "FROM xom_retails_analytics.rpt_monthly_revenue\n"
        "WHERE year = 2020\n"
        "ORDER BY month_start;",
    ),
    (
        "Which product subcategories are most often bought together?",
        "SELECT antecedent_subcategory, consequent_subcategory, pair_order_count, lift\n"
        "FROM xom_retails_analytics.rpt_subcategory_affinity\n"
        "ORDER BY pair_order_count DESC\n"
        "LIMIT 10;",
    ),
    (
        # Demonstrates excluding the online channel from a per-location metric.
        "Which physical stores earn the most per square meter?",
        "SELECT store_key, country, state, square_meters, revenue, revenue_per_sqm\n"
        "FROM xom_retails_analytics.rpt_store_performance\n"
        "WHERE NOT is_online\n"
        "ORDER BY revenue_per_sqm DESC\n"
        "LIMIT 10;",
    ),
]

SYSTEM_TEMPLATE = """You are a DuckDB SQL expert for a retail sales data warehouse.
Translate the user's question into a single DuckDB SELECT statement.

OUTPUT FORMAT
- Return ONLY the SQL. No prose, no explanation, no markdown code fences.
- Exactly one statement, ending in a semicolon.
- Always schema-qualify tables (e.g. xom_retails_analytics.rpt_product_performance).
- Include a LIMIT for questions that could return many rows.
- Never write INSERT, UPDATE, DELETE, DROP, CREATE, ALTER or ATTACH. The connection
  is read-only and such a query will be rejected.

WAREHOUSE RULES
{rules}

SCHEMA
{schema}

EXAMPLES
{examples}"""


def build_system_prompt(schema_context: str) -> str:
    """Assemble the full system instruction from the rules, schema and examples.

    Kept as one function so the prompt has exactly one definition - the CLI, the
    eval harness and any future API all send the model the same instructions.
    """
    rules = "\n".join(f"{i}. {rule}" for i, rule in enumerate(BUSINESS_RULES, start=1))
    examples = "\n\n".join(f"Q: {q}\nA: {sql}" for q, sql in FEW_SHOT_EXAMPLES)
    return SYSTEM_TEMPLATE.format(rules=rules, schema=schema_context, examples=examples)
