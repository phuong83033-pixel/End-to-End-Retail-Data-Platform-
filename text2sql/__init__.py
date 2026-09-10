"""Text-to-SQL engine over the retail DuckDB warehouse.

The LLM writes the query; DuckDB computes the answer. See model/text2sql_plan.md.
"""

__all__ = ["schema"]
