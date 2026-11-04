"""
Demo 2 -- Keyword search vs. semantic search

The help desk already had search: every word must appear in the ticket. That
finds nothing when the customer and the past ticket describe the same problem
in different words. Semantic search compares meaning instead.

Run:  uv run python demos/02_semantic_search/search_tickets.py "clicking from the back wheel"
      uv run python demos/02_semantic_search/search_tickets.py --ticket T-1037

Run build_index.py first.
"""

import argparse
import textwrap

from helpdesk.ai.llm import embedding_model
from helpdesk.ai.search import keyword_search, search_tickets, similar_tickets
from helpdesk.config import settings
from helpdesk.db import connect, rows


def show(title: str, results: list[dict]) -> None:
    print(f"\n{title}")
    if not results:
        print("    (nothing)")
    for r in results:
        distance = f"{r['distance']:.3f}  " if "distance" in r else ""
        print(f"    {distance}{r['ticket_id']}  {r['subject']}")
        if r.get("resolution"):
            for line in textwrap.wrap(r["resolution"], 80):
                print(f"{'':>{len(distance) + 14}}{line}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare keyword and semantic search over tickets.")
    parser.add_argument("query", nargs="?", help="describe the problem in your own words")
    parser.add_argument("--ticket", help="find tickets similar to this one instead")
    parser.add_argument("-k", type=int, default=3, help="how many results (default 3)")
    args = parser.parse_args()
    if not args.query and not args.ticket:
        parser.error("give a query or --ticket")

    print(f"Embedding model: {settings.embedding_provider}/{embedding_model()}")

    with connect(read_only=True) as con:
        if args.ticket:
            found = rows(con, "SELECT subject, body FROM tickets WHERE ticket_id = ?", [args.ticket])
            if not found:
                raise SystemExit(f"No ticket {args.ticket}")
            print(f"\n{args.ticket}: {found[0]['subject']}\n    {found[0]['body']}")
            # No API call here: the ticket's vector is already in the database.
            show("Similar tickets (semantic)", similar_tickets(con, args.ticket, k=args.k))
        else:
            print(f'\nQuery: "{args.query}"')
            show("Keyword search (every word must appear)", keyword_search(con, args.query, k=args.k))
            show("Semantic search (cosine distance, lower is closer)", search_tickets(con, args.query, k=args.k))


if __name__ == "__main__":
    main()
