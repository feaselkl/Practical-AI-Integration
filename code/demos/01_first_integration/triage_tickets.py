"""
Demo 1 -- Ticket triage from the command line

The same feature the help desk's Triage button calls, run over the open
tickets so you can see every result side by side.

Run:  uv run python demos/01_first_integration/triage_tickets.py
      uv run python demos/01_first_integration/triage_tickets.py --ticket T-1042
      uv run python demos/01_first_integration/triage_tickets.py --save

Without --save, nothing is written: the tickets stay untriaged for the live demo.
"""

import argparse

from helpdesk.ai.llm import describe
from helpdesk.ai.triage import PROMPT_VERSION, TriageError, save_triage, triage_ticket
from helpdesk.db import connect, rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Triage open tickets.")
    parser.add_argument("--ticket", help="triage one ticket, open or not")
    parser.add_argument("--prompt", default=PROMPT_VERSION, help=f"prompt version (default {PROMPT_VERSION})")
    parser.add_argument("--save", action="store_true", help="store the suggestions, as the Triage button does")
    args = parser.parse_args()

    with connect(read_only=True) as con:
        if args.ticket:
            tickets = rows(con, "SELECT ticket_id, subject, body FROM tickets WHERE ticket_id = ?", [args.ticket])
        else:
            tickets = rows(
                con, "SELECT ticket_id, subject, body FROM tickets WHERE status = 'open' ORDER BY ticket_id"
            )
    if not tickets:
        raise SystemExit("No matching tickets.")

    print(f"{describe()}  |  prompt triage/{args.prompt}\n")
    print(f"{'Ticket':<8} {'Category':<17} {'Priority':<9} {'Order':<10} {'Review':<7} Summary")

    results, total_in, total_out, total_seconds = [], 0, 0, 0.0
    for t in tickets:
        try:
            result = triage_ticket(t["subject"], t["body"], prompt_version=args.prompt)
        except TriageError as exc:
            # One bad answer shouldn't stop the batch. In the app, it shouldn't stop the page.
            print(f"{t['ticket_id']:<8} FAILED: {exc}")
            continue

        x = result.triage
        review = "YES" if x.needs_human_review else ""
        print(
            f"{t['ticket_id']:<8} {x.category:<17} {x.priority:<9} {x.order_number or '-':<10} "
            f"{review:<7} {x.summary}"
        )
        results.append((t["ticket_id"], result))
        total_in += result.input_tokens
        total_out += result.output_tokens
        total_seconds += result.seconds

    if results:
        print(
            f"\n{len(results)} triaged in {total_seconds:.1f}s "
            f"({total_seconds / len(results):.1f}s each)  |  {total_in} tokens in, {total_out} out"
        )

    if args.save and results:
        with connect() as con:
            for ticket_id, result in results:
                save_triage(con, ticket_id, result)
        print(f"Saved {len(results)} suggestions. Reset with: uv run python -m helpdesk.db --reset-demo")


if __name__ == "__main__":
    main()
