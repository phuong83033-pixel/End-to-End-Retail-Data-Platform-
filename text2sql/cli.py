"""Phase 4 - the terminal interface.

    python -m text2sql "Top 5 products by profit"     # one question
    python -m text2sql                                # interactive shell
    python -m text2sql --sql-only "total revenue"     # print SQL, do not run it

The generated SQL is always shown next to the answer. That is not decoration:
text-to-SQL fails by returning a plausible wrong number, and the query is the only
way a reader can tell whether the question was actually understood.
"""

from __future__ import annotations

import argparse
import sys

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from text2sql.executor import ExecutionError, QueryExecutor, UnsafeQueryError
from text2sql.generator import GenerationError, generate_sql, get_generator
from text2sql.schema import get_schema_context

console = Console()

MAX_DISPLAY_WIDTH = 40


def render_sql(sql: str) -> None:
    """Print the generated SQL with syntax highlighting."""
    console.print(Syntax(sql, "sql", theme="ansi_dark", word_wrap=True))


def render_result(result) -> None:
    """Print a query result as a table, plus row count and timing.

    Cells are truncated for display only - the underlying values are untouched.
    """
    if not result.rows:
        console.print("[yellow]No rows returned.[/yellow]")
        return

    table = Table(show_header=True, header_style="bold cyan", box=None, pad_edge=False)
    for column in result.columns:
        # One line per cell, ellipsised when too long. Folding instead would wrap
        # headers and values mid-word and make a wide result unreadable.
        table.add_column(column, overflow="ellipsis", no_wrap=True)

    for row in result.rows:
        cells = []
        for value in row:
            text = "" if value is None else str(value)
            if len(text) > MAX_DISPLAY_WIDTH:
                text = text[: MAX_DISPLAY_WIDTH - 1] + "…"
            cells.append(text)
        table.add_row(*cells)

    console.print(table)

    summary = f"[dim]{result.row_count} row(s) in {result.elapsed_ms:.1f} ms[/dim]"
    if result.hit_limit:
        summary += " [yellow](row cap reached - more rows may exist)[/yellow]"
    console.print(summary)


def answer(question: str, executor: QueryExecutor, schema_context: str, provider: str | None,
           sql_only: bool = False) -> bool:
    """Answer one question end to end. Returns True when it succeeded.

    Generation and execution failures are reported distinctly, because they mean
    different things: one is the model misunderstanding, the other is a guardrail
    or the database refusing.
    """
    try:
        generated = generate_sql(question, schema_context=schema_context, provider=provider)
    except GenerationError as exc:
        console.print(f"[red]Could not generate SQL:[/red] {exc}")
        return False

    render_sql(generated.sql)

    if sql_only:
        return True

    try:
        result = executor.execute(generated.sql)
    except UnsafeQueryError as exc:
        console.print(f"[red]Blocked by a safety guardrail:[/red] {exc}")
        return False
    except ExecutionError as exc:
        console.print(f"[red]Execution failed:[/red] {exc}")
        return False

    render_result(result)
    return True


def interactive(executor: QueryExecutor, schema_context: str, provider: str | None) -> None:
    """Run the question/answer shell until the user exits."""
    backend = get_generator(provider)
    console.print(
        Panel(
            "Ask a question about the retail warehouse in plain English.\n"
            f"[dim]backend: {backend.name}   ·   type 'exit' or press Ctrl+D to quit[/dim]",
            title="Retail Text-to-SQL",
            border_style="cyan",
        )
    )

    while True:
        try:
            question = console.input("\n[bold cyan]?[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]bye[/dim]")
            return

        if not question:
            continue
        if question.lower() in {"exit", "quit", r"\q"}:
            console.print("[dim]bye[/dim]")
            return

        console.print()
        answer(question, executor, schema_context, provider)


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to single-question or interactive mode."""
    parser = argparse.ArgumentParser(
        prog="text2sql", description="Ask the retail warehouse questions in plain English."
    )
    parser.add_argument("question", nargs="*", help="the question; omit for an interactive shell")
    parser.add_argument("--provider", help="gemini or rules (default: auto from env)")
    parser.add_argument("--sql-only", action="store_true", help="show the SQL without running it")
    parser.add_argument("--schema", action="store_true", help="print the schema context and exit")
    args = parser.parse_args(argv)

    if args.schema:
        context = get_schema_context()
        console.print(context.text)
        console.print(f"\n[dim]{len(context.tables)} tables, ~{context.approx_tokens:,} tokens[/dim]")
        return 0

    # The schema context is loaded once and reused for every question in the session.
    try:
        schema_context = get_schema_context().text
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    with QueryExecutor() as executor:
        if args.question:
            ok = answer(" ".join(args.question), executor, schema_context, args.provider, args.sql_only)
            return 0 if ok else 1
        interactive(executor, schema_context, args.provider)
    return 0


if __name__ == "__main__":
    sys.exit(main())
