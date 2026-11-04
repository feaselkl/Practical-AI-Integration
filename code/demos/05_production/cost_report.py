"""
Demo 5 -- What the AI features cost, and how fast they are

Every model call is a line in logs/llm_calls.jsonl. DuckDB queries the file
directly: no log pipeline, no dashboard to stand up.

Run:  uv run python demos/05_production/cost_report.py
      uv run python demos/05_production/cost_report.py --since 2026-10-16

Set CHAT_PRICE_INPUT_PER_MTOK, CHAT_PRICE_OUTPUT_PER_MTOK, and
EMBEDDING_PRICE_PER_MTOK in .env to see dollars. Cost is computed here, at
report time, so fixing a price fixes the whole history.
"""

import argparse

import duckdb

from helpdesk.ai.telemetry import LOG_PATH
from helpdesk.config import settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize model calls from the call log.")
    parser.add_argument("--since", help="only calls on or after this date (YYYY-MM-DD)")
    args = parser.parse_args()

    if not LOG_PATH.exists():
        raise SystemExit(f"No call log yet at {LOG_PATH}. Use the app or run a demo first.")

    prices = {
        "chat_in": settings.chat_price_input_per_mtok,
        "chat_out": settings.chat_price_output_per_mtok,
        "embed": settings.embedding_price_per_mtok,
    }
    priced = all(v is not None for v in prices.values())

    con = duckdb.connect()
    # Views can't take parameters; the path is ours, not user input.
    log_file = str(LOG_PATH).replace("'", "''")
    con.execute(f"CREATE VIEW all_calls AS SELECT * FROM read_json('{log_file}', format = 'newline_delimited')")
    # ts is stored in UTC and loads as a plain TIMESTAMP. Convert --since (local
    # time, unless it carries an offset) to UTC before comparing.
    con.execute(
        "CREATE TABLE calls AS SELECT * FROM all_calls "
        "WHERE $since IS NULL OR ts >= timezone('UTC', $since::TIMESTAMPTZ)",
        {"since": args.since},
    )
    cost = (
        """
        CASE WHEN kind = 'embedding'
             THEN input_tokens * $embed / 1e6
             ELSE input_tokens * $chat_in / 1e6 + output_tokens * $chat_out / 1e6 END
        """
        if priced
        else "NULL"
    )

    rows = con.execute(
        f"""
        SELECT feature,
               coalesce(prompt, '') AS prompt,
               count(*) AS calls,
               count(*) FILTER (WHERE cached) AS cached,
               count(*) FILTER (WHERE error IS NOT NULL) AS errors,
               sum(input_tokens) AS tokens_in,
               sum(output_tokens) AS tokens_out,
               quantile_cont(seconds, 0.5) FILTER (WHERE NOT cached AND error IS NULL) AS p50,
               quantile_cont(seconds, 0.95) FILTER (WHERE NOT cached AND error IS NULL) AS p95,
               sum({cost}) AS dollars
        FROM calls
        GROUP BY ALL
        ORDER BY feature, prompt
        """,
        prices if priced else {},
    ).fetchall()

    if not rows:
        raise SystemExit("No calls in that range.")

    header = f"{'Feature':<15}{'Prompt':<20}{'Calls':>7}{'Cached':>8}{'Errors':>8}{'Tokens in':>11}{'Tokens out':>12}{'p50 s':>7}{'p95 s':>7}{'Cost':>10}"
    print(header)
    print("-" * len(header))
    for feature, prompt, calls, cached, errors, tin, tout, p50, p95, dollars in rows:
        print(
            f"{feature:<15}{prompt:<20}{calls:>7}{cached:>8}{errors:>8}{tin:>11,}{tout:>12,}"
            f"{p50 or 0:>7.1f}{p95 or 0:>7.1f}{(f'${dollars:,.4f}' if dollars is not None else 'n/a'):>10}"
        )

    errors = con.execute(
        "SELECT ts, feature, error FROM calls WHERE error IS NOT NULL ORDER BY ts DESC LIMIT 5"
    ).fetchall()
    if errors:
        print("\nMost recent errors:")
        for ts, feature, error in errors:
            print(f"    {ts}  {feature:<14} {error[:100]}")

    if not priced:
        print("\nNo prices set, so no dollars. Add CHAT_PRICE_INPUT_PER_MTOK, CHAT_PRICE_OUTPUT_PER_MTOK,")
        print("and EMBEDDING_PRICE_PER_MTOK to .env from your provider's price sheet.")


if __name__ == "__main__":
    main()
