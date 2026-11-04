"""
Demo 3 -- Chunk and embed the knowledge base

Each KB article is split at its headings, and every chunk is embedded with
the same model as the tickets. Same model means same vector space, which is
what lets a ticket's stored vector find the KB sections that answer it.

Run:  uv run python demos/03_rag/build_kb_index.py
      uv run python demos/03_rag/build_kb_index.py --show
"""

import argparse
import time

from helpdesk.ai.answer import build_kb_index, kb_status
from helpdesk.ai.llm import embedding_model
from helpdesk.config import settings
from helpdesk.db import connect, rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Chunk and embed the knowledge base.")
    parser.add_argument("--show", action="store_true", help="list the chunks instead of rebuilding")
    args = parser.parse_args()

    with connect(read_only=args.show) as con:
        if args.show:
            for c in rows(con, "SELECT chunk_id, heading, length(text) AS chars FROM kb_chunks ORDER BY chunk_id"):
                print(f"{c['chars']:>5}  {c['heading']}")
            print(f"\n{kb_status(con)}")
            return

        print(f"Embedding model: {settings.embedding_provider}/{embedding_model()}")
        started = time.perf_counter()
        count = build_kb_index(con)
        status = kb_status(con)

    print(f"Embedded {count} chunks in {time.perf_counter() - started:.1f}s.")
    print(f"Index: {status['chunks']} chunks, {status['dimensions']} dimensions, model {status['models']}")


if __name__ == "__main__":
    main()
