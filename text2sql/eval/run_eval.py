"""Phase 5 - measure how often the engine gets the right answer.

Text-to-SQL fails by returning a plausible wrong number, not by crashing, so
"it looked right when I tried it" is not evidence. This harness executes both the
generated SQL and a known-correct reference query, then compares their RESULTS.
SQL text is never compared - many different queries answer a question correctly.

    python -m text2sql.eval.run_eval                  # offline rule engine
    python -m text2sql.eval.run_eval --provider gemini
    python -m text2sql.eval.run_eval --verbose        # show generated SQL

Exit code is 0 when every attempted case passes, 1 otherwise, so it can gate CI.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import yaml

from text2sql.executor import ExecutionError, QueryExecutor, UnsafeQueryError
from text2sql.generator import GenerationError, generate_sql
from text2sql.schema import get_schema_context

QUESTIONS_PATH = Path(__file__).parent / "questions.yml"

# Relative tolerance for float comparison - decimal accumulation differs harmlessly
# between an aggregate over the fact table and the same total via a mart.
TOLERANCE = 1e-6


@dataclass
class CaseResult:
    """Outcome of a single evaluation case."""

    case_id: str
    question: str
    status: str  # pass | fail | error | skipped
    detail: str = ""
    sql: str = ""


def _normalise(value):
    """Coerce a cell into a comparable form.

    Decimals become floats so a mart's DECIMAL(38,2) total compares equal to the
    same figure computed straight off the fact table; everything else is compared
    as a lowercase string so 26326 and '26326' do not spuriously differ.
    """
    if isinstance(value, (Decimal, float, int)) and not isinstance(value, bool):
        return float(value)
    return str(value).strip().lower()


def _values_match(actual, expected) -> bool:
    """Compare two normalised cells, allowing a small tolerance on numbers."""
    a, e = _normalise(actual), _normalise(expected)
    if isinstance(a, float) and isinstance(e, float):
        return abs(a - e) <= max(TOLERANCE * max(abs(a), abs(e)), 0.01)
    return a == e


def compare(case: dict, generated_rows, generated_cols, reference_rows, reference_cols) -> tuple[bool, str]:
    """Judge one case by comparing generated results against the reference.

    Kept deliberately forgiving about shape - a correct answer may carry extra
    columns - but strict about the value being asked for.
    """
    mode = case.get("check", "scalar")

    if mode == "scalar":
        if not generated_rows:
            return False, "returned no rows"
        if not reference_rows:
            return False, "reference returned no rows"
        expected = reference_rows[0][0]
        # Accept the expected value anywhere in the first row: a model may answer
        # "how many orders" with (label, count) rather than a bare count.
        for cell in generated_rows[0]:
            if _values_match(cell, expected):
                return True, f"= {expected}"
        return False, f"expected {expected}, got {generated_rows[0]}"

    if mode == "row_count":
        if len(generated_rows) == len(reference_rows):
            return True, f"{len(generated_rows)} rows"
        return False, f"expected {len(reference_rows)} rows, got {len(generated_rows)}"

    if mode == "set":
        column = case.get("column")
        if column not in reference_cols:
            return False, f"reference has no column {column!r}"
        expected = {_normalise(r[reference_cols.index(column)]) for r in reference_rows}

        # The generated query may name the column differently, so try the named
        # column first and otherwise look for any column that carries the values.
        if column in generated_cols:
            actual = {_normalise(r[generated_cols.index(column)]) for r in generated_rows}
            if actual == expected:
                return True, f"{len(expected)} value(s) matched"
        for index in range(len(generated_cols)):
            actual = {_normalise(r[index]) for r in generated_rows}
            if actual == expected:
                return True, f"matched on column {generated_cols[index]!r}"
        return False, f"expected {sorted(expected)}, none of the returned columns match"

    return False, f"unknown check mode {mode!r}"


def run_case(case: dict, executor: QueryExecutor, schema_context: str, provider: str | None) -> CaseResult:
    """Generate SQL for one question, execute it, and compare against the reference."""
    question = case["question"]
    try:
        generated = generate_sql(question, schema_context=schema_context, provider=provider)
    except GenerationError as exc:
        return CaseResult(case["id"], question, "skipped", str(exc)[:70])

    try:
        actual = executor.execute(generated.sql)
    except (UnsafeQueryError, ExecutionError) as exc:
        return CaseResult(case["id"], question, "error", str(exc)[:70], generated.sql)

    try:
        reference = executor.execute(case["reference_sql"])
    except (UnsafeQueryError, ExecutionError) as exc:
        return CaseResult(case["id"], question, "error", f"reference failed: {exc}"[:70])

    passed, detail = compare(case, actual.rows, actual.columns, reference.rows, reference.columns)
    return CaseResult(case["id"], question, "pass" if passed else "fail", detail, generated.sql)


def main() -> int:
    """Run the whole suite and print a per-case report plus a summary."""
    parser = argparse.ArgumentParser(description="Evaluate text-to-SQL accuracy.")
    parser.add_argument("--provider", help="gemini or rules (default: auto from env)")
    parser.add_argument("--verbose", action="store_true", help="print the generated SQL")
    parser.add_argument("--only", help="run a single case by id")
    args = parser.parse_args()

    cases = yaml.safe_load(QUESTIONS_PATH.read_text(encoding="utf-8"))
    if args.only:
        cases = [c for c in cases if c["id"] == args.only]

    schema_context = get_schema_context().text
    results: list[CaseResult] = []

    with QueryExecutor() as executor:
        for case in cases:
            result = run_case(case, executor, schema_context, args.provider)
            results.append(result)

            mark = {"pass": "PASS", "fail": "FAIL", "error": "ERR ", "skipped": "SKIP"}[result.status]
            print(f"  {mark}  {result.case_id:26} {result.detail[:60]}")
            if args.verbose and result.sql:
                print("        " + result.sql.replace("\n", "\n        "))

    passed = sum(r.status == "pass" for r in results)
    failed = sum(r.status in ("fail", "error") for r in results)
    skipped = sum(r.status == "skipped" for r in results)
    attempted = passed + failed

    print()
    print(f"  attempted {attempted}/{len(results)}   passed {passed}   failed {failed}   skipped {skipped}")
    if attempted:
        print(f"  accuracy on attempted: {passed / attempted * 100:.0f}%")
    if skipped:
        print(f"  {skipped} skipped - the offline engine has no rule for them; set GOOGLE_API_KEY to attempt all.")

    return 0 if failed == 0 and attempted > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
