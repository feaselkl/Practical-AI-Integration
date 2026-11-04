"""
Demo 3 -- Draft a reply, with and without the knowledge base

Run:  uv run python demos/03_rag/draft_reply.py T-1040 --naive   # no retrieval: watch it guess
      uv run python demos/03_rag/draft_reply.py T-1040           # grounded, with citations
      uv run python demos/03_rag/draft_reply.py T-1042           # the question the KB can't answer

Run build_kb_index.py first.
"""

import argparse
import sys
import time

from helpdesk.ai.answer import PROMPT_VERSION, Draft, retrieve_for_ticket, stream_draft, ticket_context
from helpdesk.ai.llm import describe
from helpdesk.db import connect


def main() -> None:
    parser = argparse.ArgumentParser(description="Draft a reply to a ticket.")
    parser.add_argument("ticket")
    parser.add_argument("--naive", action="store_true", help="no retrieval, no grounding rules")
    parser.add_argument("--prompt", default=PROMPT_VERSION, help=f"prompt version (default {PROMPT_VERSION})")
    args = parser.parse_args()

    with connect(read_only=True) as con:
        context = ticket_context(con, args.ticket)
        if context is None:
            raise SystemExit(f"No ticket {args.ticket}")
        ticket, customer_name, orders = context
        sources = [] if args.naive else retrieve_for_ticket(con, args.ticket)

    prompt_version = "naive" if args.naive else args.prompt
    print(f"{describe()}  |  prompt draft_reply/{prompt_version}\n")
    print(f"{ticket['ticket_id']}: {ticket['subject']}\n    {ticket['body']}\n")

    if sources:
        print("Retrieved (cosine distance):")
        for i, s in enumerate(sources, start=1):
            print(f"    [{i}] {s['distance']:.3f}  {s['heading']}")
        print()

    print("Draft:\n")
    started, first_text = time.perf_counter(), None
    for part in stream_draft(ticket, customer_name, orders if not args.naive else [], sources, prompt_version):
        if isinstance(part, Draft):
            draft = part
        else:
            if first_text is None:
                first_text = time.perf_counter() - started
            sys.stdout.write(part)
            sys.stdout.flush()

    # Streaming doesn't shorten the total; it shortens the wait before reading starts.
    print(f"\n\n{draft.model}  |  first text after {first_text or 0:.1f}s, done after {draft.seconds:.1f}s  |  {draft.input_tokens} tokens in, {draft.output_tokens} out")
    if args.naive:
        print("No sources, so nothing to check. Every policy in that draft is the model's guess.")
    elif draft.invalid_citations:
        print(f"WARNING: cites {draft.invalid_citations}, which were never provided.")
    elif draft.cited:
        print(f"Cites sources {draft.cited}; all of them were provided.")
    else:
        print("Cites nothing: the sources didn't answer the question, so the draft doesn't guess.")


if __name__ == "__main__":
    main()
