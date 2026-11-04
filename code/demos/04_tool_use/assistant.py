"""
Demo 4 -- The support assistant, from the command line

Each message is one agent turn. Later messages continue the same conversation.
Every tool call is printed with its arguments and result, so you can see
exactly what the model asked for and what the code allowed.

Run:  uv run python demos/04_tool_use/assistant.py T-1039 "Where is this order, and what can we do?"
      uv run python demos/04_tool_use/assistant.py T-1040 "Can she return it?" "OK, set up the refund."
      uv run python demos/04_tool_use/assistant.py T-1042 "Handle this ticket."

Refund proposals land in the refunds table as "proposed"; approve or reject
them in the app. Clear everything with: uv run python -m helpdesk.db --reset-demo
"""

import argparse
import json
import textwrap

from helpdesk.ai.assistant import load_ticket, run_turn
from helpdesk.ai.llm import describe
from helpdesk.db import connect, rows


def short(result: dict, width: int = 110) -> str:
    text = json.dumps(result, default=str)
    return text if len(text) <= width else text[: width - 3] + "..."


def main() -> None:
    parser = argparse.ArgumentParser(description="Talk to the support assistant about one ticket.")
    parser.add_argument("ticket")
    parser.add_argument("messages", nargs="+", help="one or more agent messages, in order")
    args = parser.parse_args()

    print(describe())
    with connect() as con:
        ticket = load_ticket(con, args.ticket)
        if ticket is None:
            raise SystemExit(f"No ticket {args.ticket}")
        print(f"\n{ticket['ticket_id']} from {ticket['customer_name']}: {ticket['subject']}")
        print(textwrap.indent(textwrap.fill(ticket["body"], 100), "    "))

        previous = None
        for message in args.messages:
            print(f"\nAGENT: {message}")
            turn = run_turn(con, ticket, message, previous_response_id=previous)
            previous = turn.response_id
            for call in turn.tool_calls:
                print(f"    -> {call.name}({json.dumps(call.arguments)})")
                print(f"       {short(call.result)}")
            print(f"\nASSISTANT: {turn.reply}")
            print(
                f"    [{turn.rounds} tool round(s), {turn.seconds:.1f}s, "
                f"{turn.input_tokens} tokens in, {turn.output_tokens} out"
                f"{', STOPPED EARLY' if turn.stopped_early else ''}]"
            )

        proposals = rows(
            con, "SELECT refund_id, order_id, amount, status, reason FROM refunds WHERE ticket_id = ?", [args.ticket]
        )
    if proposals:
        print("\nRefund proposals waiting for a person:")
        for p in proposals:
            print(f"    {p['refund_id']}  {p['order_id']}  ${p['amount']}  {p['status']}  {p['reason']}")


if __name__ == "__main__":
    main()
