"""
Demo 2 -- Embeddings and Semantic Search

Embed every ticket and store the vectors in DuckDB, next to the tickets.

    uv run python demos/02_semantic_search/build_index.py            # embed what's missing
    uv run python demos/02_semantic_search/build_index.py --rebuild  # after changing models
    uv run python demos/02_semantic_search/build_index.py --hnsw     # show the VSS index

Vectors from different embedding models are not comparable, not even when
they happen to have the same length. Change EMBEDDING_PROVIDER or the model,
and every vector has to be regenerated. That's why each row records the model
that produced it.
"""

import argparse
import time

import duckdb

from helpdesk.ai.llm import embedding_model
from helpdesk.ai.search import build_index, index_status
from helpdesk.config import settings
from helpdesk.db import connect


def show_hnsw(query_ticket: str = "T-1037") -> None:
    """
    The help desk uses exact search: compare the query against every vector.
    At thousands of tickets that takes milliseconds. At millions, you want an
    approximate index instead. DuckDB's VSS extension provides HNSW.

    HNSW persistence is still experimental in DuckDB, so this builds the index
    on an in-memory copy rather than in the help desk's database file.
    """
    mem = duckdb.connect()
    mem.execute("INSTALL vss")
    mem.execute("LOAD vss")
    mem.execute(f"ATTACH '{settings.db_path}' AS helpdesk (READ_ONLY)")

    mem.execute("USE helpdesk")
    dims = index_status(mem)["dimensions"]
    if dims is None:
        raise SystemExit("No embeddings yet. Run this script without --hnsw first.")

    mem.execute("USE memory")
    mem.execute("CREATE TABLE ticket_embeddings AS SELECT * FROM helpdesk.ticket_embeddings")
    mem.execute(
        "CREATE INDEX ticket_embeddings_hnsw ON ticket_embeddings "
        "USING HNSW (embedding) WITH (metric = 'cosine')"
    )

    query = f"""
        SELECT ticket_id, array_cosine_distance(embedding, ?::FLOAT[{dims}]) AS distance
        FROM ticket_embeddings
        ORDER BY distance
        LIMIT 3
    """
    [vector] = mem.execute(
        "SELECT embedding FROM ticket_embeddings WHERE ticket_id = ?", [query_ticket]
    ).fetchone()

    plan = mem.execute("EXPLAIN " + query, [vector]).fetchall()[0][1]
    uses_index = "HNSW_INDEX_SCAN" in plan
    print(f"\nTop 3 neighbors of {query_ticket}, through the HNSW index:")
    for ticket_id, distance in mem.execute(query, [vector]).fetchall():
        print(f"    {distance:.3f}  {ticket_id}")
    print(f"\nQuery plan uses the HNSW index: {uses_index}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Embed tickets for semantic search.")
    parser.add_argument("--rebuild", action="store_true", help="drop all vectors and re-embed with the current model")
    parser.add_argument("--hnsw", action="store_true", help="build an HNSW index on an in-memory copy and show it in use")
    args = parser.parse_args()

    if args.hnsw:
        show_hnsw()
        return

    print(f"Embedding model: {settings.embedding_provider}/{embedding_model()}")
    started = time.perf_counter()
    with connect() as con:
        embedded = build_index(con, rebuild=args.rebuild)
        status = index_status(con)

    print(f"Embedded {embedded} tickets in {time.perf_counter() - started:.1f}s.")
    print(f"Index: {status['embedded']} vectors of {status['dimensions']} dimensions, model {status['models']}")


if __name__ == "__main__":
    main()
