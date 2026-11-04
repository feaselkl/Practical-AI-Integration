"""
Feature 2: semantic search over tickets.

An embedding turns text into a fixed-length list of numbers, arranged so that
texts with similar meaning land close together. "Clicking from the back wheel"
and "ticking noise from the rear hub" share no keywords, but their vectors are
neighbors.

The vectors live in DuckDB next to the tickets, in a FLOAT[n] column. Search
is plain SQL: order by cosine distance, take the top k.
"""

import duckdb

from helpdesk.ai.llm import embedding_client, embedding_model
from helpdesk.ai.telemetry import track
from helpdesk.db import rows


def embed(texts: list[str], feature: str = "embeddings") -> list[list[float]]:
    """One API call for the whole batch. Same client shape for Azure and Ollama."""
    with track(feature, kind="embedding") as call:
        response = embedding_client().embeddings.create(model=embedding_model(), input=texts)
        call.update(model=response.model, input_tokens=response.usage.prompt_tokens if response.usage else 0)
    return [item.embedding for item in response.data]


def ticket_text(subject: str, body: str) -> str:
    return f"{subject}\n\n{body}"


def vector_dimensions(con: duckdb.DuckDBPyConnection, table: str = "ticket_embeddings") -> int | None:
    """Length of the vectors in a table's embedding column, or None if the table doesn't exist yet."""
    found = con.execute(
        """
        SELECT data_type FROM information_schema.columns
        WHERE table_name = ? AND column_name = 'embedding'
        """,
        [table],
    ).fetchone()
    # data_type looks like FLOAT[1536]
    return int(found[0].split("[")[1].rstrip("]")) if found else None


def index_status(con: duckdb.DuckDBPyConnection) -> dict:
    """How many tickets are embedded, and with which model."""
    if vector_dimensions(con) is None:
        return {"embedded": 0, "models": [], "dimensions": None}
    models = [r["model"] for r in rows(con, "SELECT DISTINCT model FROM ticket_embeddings")]
    embedded = con.execute("SELECT count(*) FROM ticket_embeddings").fetchone()[0]
    return {"embedded": embedded, "models": models, "dimensions": vector_dimensions(con)}


def build_index(con: duckdb.DuckDBPyConnection, rebuild: bool = False) -> int:
    """Embed every ticket that doesn't have a vector yet. Returns how many were embedded."""
    status = index_status(con)

    # Vectors from different models are not comparable. Never mix them.
    if status["models"] and status["models"] != [embedding_model()]:
        if not rebuild:
            raise RuntimeError(
                f"Index was built with {status['models']} but EMBEDDING model is {embedding_model()}. "
                "Re-embed everything with --rebuild."
            )
    if rebuild:
        con.execute("DROP TABLE IF EXISTS ticket_embeddings")

    pending = rows(
        con,
        """
        SELECT ticket_id, subject, body FROM tickets
        WHERE ticket_id NOT IN (SELECT ticket_id FROM ticket_embeddings)
        ORDER BY ticket_id
        """
        if vector_dimensions(con)
        else "SELECT ticket_id, subject, body FROM tickets ORDER BY ticket_id",
    )
    if not pending:
        return 0

    vectors = embed([ticket_text(t["subject"], t["body"]) for t in pending], feature="ticket_index")

    # The vector length is a property of the model: 768 for embeddinggemma,
    # 1536 for text-embedding-3-small. Create the table to match.
    dimensions = len(vectors[0])
    con.execute(
        f"""
        CREATE TABLE IF NOT EXISTS ticket_embeddings (
            ticket_id VARCHAR PRIMARY KEY,
            model     VARCHAR NOT NULL,
            embedding FLOAT[{dimensions}] NOT NULL
        )
        """
    )
    con.executemany(
        "INSERT INTO ticket_embeddings VALUES (?, ?, ?)",
        [[t["ticket_id"], embedding_model(), v] for t, v in zip(pending, vectors)],
    )
    return len(pending)


def embed_ticket(con: duckdb.DuckDBPyConnection, ticket_id: str, subject: str, body: str) -> None:
    """Embed one new ticket as it arrives, so it is searchable immediately."""
    dimensions = vector_dimensions(con)
    if dimensions is None or embedding_model() not in index_status(con)["models"]:
        return
    [vector] = embed([ticket_text(subject, body)], feature="ticket_index")
    con.execute(
        "INSERT OR REPLACE INTO ticket_embeddings VALUES (?, ?, ?)",
        [ticket_id, embedding_model(), vector],
    )


def _nearest(con: duckdb.DuckDBPyConnection, vector: list[float], k: int, exclude: str | None) -> list[dict]:
    dimensions = vector_dimensions(con)
    if dimensions is None:
        raise RuntimeError("No ticket embeddings yet. Run demos/02_semantic_search/build_index.py first.")
    # Switching embedding models without re-embedding doesn't crash; it just
    # quietly finds nothing. Say so instead.
    indexed_with = index_status(con)["models"]
    if embedding_model() not in indexed_with:
        raise RuntimeError(
            f"Index was built with {indexed_with} but the embedding model is now {embedding_model()}. "
            "Run demos/02_semantic_search/build_index.py --rebuild."
        )
    # Exact nearest-neighbor search: compare against every vector. At help-desk
    # scale (thousands of tickets) this takes milliseconds. An HNSW index trades
    # a little recall for speed once you have millions; see build_index.py --hnsw.
    return rows(
        con,
        f"""
        SELECT t.ticket_id, t.subject, t.status, t.category, t.resolution,
               array_cosine_distance(e.embedding, ?::FLOAT[{dimensions}]) AS distance
        FROM ticket_embeddings e
        JOIN tickets t USING (ticket_id)
        WHERE t.ticket_id IS DISTINCT FROM ?
          AND e.model = ?
        ORDER BY distance
        LIMIT ?
        """,
        [vector, exclude, embedding_model(), k],
    )


def similar_tickets(con: duckdb.DuckDBPyConnection, ticket_id: str, k: int = 3) -> list[dict]:
    """Past tickets most like this one. Resolved ones come with their resolution, which is the useful part."""
    found = con.execute(
        "SELECT embedding FROM ticket_embeddings WHERE ticket_id = ?", [ticket_id]
    ).fetchone()
    if found is None:
        return []
    return _nearest(con, list(found[0]), k, exclude=ticket_id)


def search_tickets(con: duckdb.DuckDBPyConnection, query: str, k: int = 5) -> list[dict]:
    [vector] = embed([query], feature="ticket_search")
    return _nearest(con, vector, k, exclude=None)


def keyword_search(con: duckdb.DuckDBPyConnection, query: str, k: int = 5) -> list[dict]:
    """What the help desk had before: every word must appear somewhere in the ticket."""
    words = [w for w in query.lower().split() if len(w) > 2]
    if not words:
        return []
    conditions = " AND ".join(["lower(subject || ' ' || body) LIKE ?"] * len(words))
    return rows(
        con,
        f"""
        SELECT ticket_id, subject, status, category, resolution
        FROM tickets WHERE {conditions}
        ORDER BY created_at DESC LIMIT ?
        """,
        [f"%{w}%" for w in words] + [k],
    )
