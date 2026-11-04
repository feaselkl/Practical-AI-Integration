"""
Feature 3: draft a reply, grounded in the knowledge base (RAG).

The model doesn't know Wobbly Wheel's return policy, and asked anyway it will
invent a plausible one. Retrieval-augmented generation fixes that in four steps:

    1. Chunk the KB articles, and embed every chunk (once, ahead of time)
    2. Retrieve the chunks closest to the ticket
    3. Put them in the prompt, numbered, and ask for citations
    4. Check the citations before anyone trusts the draft

The draft streams into the page, and a person edits it before anything is sent.
"""

import json
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date

import duckdb

from helpdesk.ai.llm import chat_client, chat_model, embedding_model, reasoning_options
from helpdesk.ai.prompts import Prompt, load_prompt
from helpdesk.ai.search import embed, vector_dimensions
from helpdesk.ai.telemetry import track
from helpdesk.db import rows

PROMPT_VERSION = "v1"
TOP_K = 4
CITATION = re.compile(r"\[(\d+)\]")


# ---------------------------------------------------------------------------
# 1. Chunk and embed the knowledge base
# ---------------------------------------------------------------------------


def chunk_article(slug: str, text: str) -> list[dict]:
    """
    Split on second-level headings. KB articles are already organized by
    topic, so a heading is a natural boundary: each chunk answers one question.
    Every chunk carries its article title, so "E-bikes" still means "Returns
    Policy > E-bikes" when it is retrieved on its own.
    """
    lines = text.strip().splitlines()
    title = lines[0].removeprefix("# ").strip()

    chunks, heading, body = [], None, []

    def flush() -> None:
        content = "\n".join(body).strip()
        if content:
            label = f"{title} > {heading}" if heading else title
            anchor = re.sub(r"[^a-z0-9]+", "-", (heading or "intro").lower()).strip("-")
            chunks.append(
                {"chunk_id": f"{slug}#{anchor}", "slug": slug, "title": title,
                 "heading": label, "text": f"{label}\n\n{content}"}
            )

    for line in lines[1:]:
        if line.startswith("## "):
            flush()
            heading, body = line.removeprefix("## ").strip(), []
        else:
            body.append(line)
    flush()
    return chunks


def kb_status(con: duckdb.DuckDBPyConnection) -> dict:
    if vector_dimensions(con, "kb_chunks") is None:
        return {"chunks": 0, "models": [], "dimensions": None}
    models = [r["model"] for r in rows(con, "SELECT DISTINCT model FROM kb_chunks")]
    count = con.execute("SELECT count(*) FROM kb_chunks").fetchone()[0]
    return {"chunks": count, "models": models, "dimensions": vector_dimensions(con, "kb_chunks")}


def build_kb_index(con: duckdb.DuckDBPyConnection) -> int:
    """Chunk and embed every KB article. Rebuilds from scratch: there are only a few dozen chunks."""
    chunks = [c for a in rows(con, "SELECT slug, body FROM kb_articles ORDER BY slug")
              for c in chunk_article(a["slug"], a["body"])]
    vectors = embed([c["text"] for c in chunks], feature="kb_index")

    con.execute("DROP TABLE IF EXISTS kb_chunks")
    con.execute(
        f"""
        CREATE TABLE kb_chunks (
            chunk_id  VARCHAR PRIMARY KEY,
            slug      VARCHAR NOT NULL,
            title     VARCHAR NOT NULL,
            heading   VARCHAR NOT NULL,
            text      VARCHAR NOT NULL,
            model     VARCHAR NOT NULL,
            embedding FLOAT[{len(vectors[0])}] NOT NULL
        )
        """
    )
    con.executemany(
        "INSERT INTO kb_chunks VALUES (?, ?, ?, ?, ?, ?, ?)",
        [[c["chunk_id"], c["slug"], c["title"], c["heading"], c["text"], embedding_model(), v]
         for c, v in zip(chunks, vectors)],
    )
    return len(chunks)


# ---------------------------------------------------------------------------
# 2. Retrieve
# ---------------------------------------------------------------------------


def _nearest_chunks(con: duckdb.DuckDBPyConnection, vector: list[float], k: int) -> list[dict]:
    status = kb_status(con)
    if status["dimensions"] is None:
        raise RuntimeError("No KB index yet. Run demos/03_rag/build_kb_index.py first.")
    if embedding_model() not in status["models"]:
        raise RuntimeError(
            f"KB index was built with {status['models']} but the embedding model is now "
            f"{embedding_model()}. Run demos/03_rag/build_kb_index.py."
        )
    return rows(
        con,
        f"""
        SELECT chunk_id, slug, heading, text,
               array_cosine_distance(embedding, ?::FLOAT[{status['dimensions']}]) AS distance
        FROM kb_chunks
        ORDER BY distance
        LIMIT ?
        """,
        [vector, k],
    )


def retrieve_for_ticket(con: duckdb.DuckDBPyConnection, ticket_id: str, k: int = TOP_K) -> list[dict]:
    """
    The ticket is already embedded for similar-ticket search, and its vector
    lives in the same space as the KB chunks. So retrieval for a stored ticket
    costs no API call at all.
    """
    found = con.execute(
        "SELECT embedding FROM ticket_embeddings WHERE ticket_id = ? AND model = ?",
        [ticket_id, embedding_model()],
    ).fetchone()
    if found is None:
        return []
    return _nearest_chunks(con, list(found[0]), k)


def retrieve(con: duckdb.DuckDBPyConnection, query: str, k: int = TOP_K) -> list[dict]:
    [vector] = embed([query], feature="kb_search")
    return _nearest_chunks(con, vector, k)


# ---------------------------------------------------------------------------
# 3. Generate, streaming
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Draft:
    text: str
    sources: list[dict]
    cited: list[int]
    invalid_citations: list[int]
    prompt: Prompt
    model: str
    input_tokens: int
    output_tokens: int
    seconds: float


def build_input(ticket: dict, customer_name: str, orders: list[dict], sources: list[dict]) -> str:
    """Everything the model may use, each part delimited so data can't pose as instructions."""
    parts = [f"Today's date: {date.today().isoformat()}"]
    if sources:
        numbered = "\n\n".join(f"[{i}] {s['text']}" for i, s in enumerate(sources, start=1))
        parts.append(f"<sources>\n{numbered}\n</sources>")
    if orders:
        # default=str turns dates and decimals into plain strings.
        parts.append(f"<orders>\n{json.dumps(orders, indent=1, default=str)}\n</orders>")
    parts.append(f"<customer>{customer_name}</customer>")
    parts.append(f"<ticket>\nSubject: {ticket['subject']}\n\n{ticket['body']}\n</ticket>")
    return "\n\n".join(parts)


def check_citations(text: str, source_count: int) -> tuple[list[int], list[int]]:
    """Which sources the draft cites, and which citations point at nothing we provided."""
    cited = sorted({int(n) for n in CITATION.findall(text)})
    return [n for n in cited if 1 <= n <= source_count], [n for n in cited if not 1 <= n <= source_count]


def stream_draft(
    ticket: dict,
    customer_name: str,
    orders: list[dict],
    sources: list[dict],
    prompt_version: str = PROMPT_VERSION,
) -> Iterator[str | Draft]:
    """
    Yield the draft as it is written, then a final Draft with citations checked.
    Streaming doesn't make the answer faster; it makes the wait feel shorter,
    because the agent starts reading after a second instead of after ten.
    """
    prompt = load_prompt("draft_reply", prompt_version)
    started = time.perf_counter()
    chunks: list[str] = []

    with track("draft_reply", prompt=prompt.id) as call, chat_client().responses.stream(
        model=chat_model(),
        instructions=prompt.text,
        input=build_input(ticket, customer_name, orders, sources),
        max_output_tokens=4000,
        **reasoning_options(),
    ) as stream:
        for event in stream:
            if event.type == "response.output_text.delta":
                chunks.append(event.delta)
                yield event.delta
        response = stream.get_final_response()
        usage = response.usage
        call.update(
            model=response.model,
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
        )

    text = "".join(chunks)
    cited, invalid = check_citations(text, len(sources))
    yield Draft(
        text=text,
        sources=sources,
        cited=cited,
        invalid_citations=invalid,
        prompt=prompt,
        model=response.model,
        input_tokens=usage.input_tokens if usage else 0,
        output_tokens=usage.output_tokens if usage else 0,
        seconds=time.perf_counter() - started,
    )


def ticket_context(con: duckdb.DuckDBPyConnection, ticket_id: str) -> tuple[dict, str, list[dict]] | None:
    """The ticket, the customer's name, and the customer's orders."""
    found = rows(
        con,
        """
        SELECT t.ticket_id, t.subject, t.body, t.customer_id, c.name AS customer_name
        FROM tickets t JOIN customers c USING (customer_id)
        WHERE t.ticket_id = ?
        """,
        [ticket_id],
    )
    if not found:
        return None
    ticket = found[0]
    orders = rows(
        con,
        """
        SELECT order_id, status, placed_at, promised_by, delivered_at, shipping_method, items, total
        FROM orders WHERE customer_id = ? ORDER BY placed_at DESC
        """,
        [ticket["customer_id"]],
    )
    return ticket, ticket["customer_name"], orders
