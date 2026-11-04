"""
The help desk's database: DuckDB, one file, no server.

Run this module to (re)create the database from the JSON in data/:

    uv run python -m helpdesk.db --reset        # everything, including embeddings
    uv run python -m helpdesk.db --reset-demo   # between rehearsals; keeps embeddings
"""

import argparse
import json
from collections.abc import Iterator
from contextlib import contextmanager

import duckdb

from helpdesk.config import DATA_DIR, settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    customer_id VARCHAR PRIMARY KEY,
    name        VARCHAR NOT NULL,
    email       VARCHAR NOT NULL,
    since       DATE
);

CREATE TABLE IF NOT EXISTS orders (
    order_id        VARCHAR PRIMARY KEY,
    customer_id     VARCHAR NOT NULL,
    placed_at       DATE,
    status          VARCHAR NOT NULL,
    shipping_method VARCHAR,
    carrier         VARCHAR,
    tracking_number VARCHAR,
    promised_by     DATE,
    delivered_at    DATE,
    items           STRUCT(sku VARCHAR, name VARCHAR, qty INTEGER, price DECIMAL(10, 2))[],
    total           DECIMAL(10, 2)
);

-- category and priority are what a human assigned. The ai_* columns are what
-- the model suggested. Keeping them apart is deliberate: the AI suggests, a
-- person accepts, and the difference between the two is your eval data.
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id         VARCHAR PRIMARY KEY,
    customer_id       VARCHAR NOT NULL,
    created_at        TIMESTAMP NOT NULL,
    subject           VARCHAR NOT NULL,
    body              VARCHAR NOT NULL,
    status            VARCHAR NOT NULL,
    category          VARCHAR,
    priority          VARCHAR,
    resolution        VARCHAR,
    ai_category       VARCHAR,
    ai_priority       VARCHAR,
    ai_order_number   VARCHAR,
    ai_product        VARCHAR,
    ai_sentiment      VARCHAR,
    ai_summary        VARCHAR,
    ai_needs_review   BOOLEAN,
    ai_prompt_version VARCHAR,
    ai_model          VARCHAR,
    ai_triaged_at     TIMESTAMP
);

-- Refunds the assistant has PROPOSED. Only a person can move one to approved.
CREATE TABLE IF NOT EXISTS refunds (
    refund_id   VARCHAR PRIMARY KEY,
    ticket_id   VARCHAR,
    order_id    VARCHAR NOT NULL,
    amount      DECIMAL(10, 2) NOT NULL,
    reason      VARCHAR,
    status      VARCHAR NOT NULL,   -- proposed, approved, rejected
    proposed_at TIMESTAMP NOT NULL,
    decided_at  TIMESTAMP
);

CREATE TABLE IF NOT EXISTS assistant_turns (
    ticket_id   VARCHAR NOT NULL,
    seq         INTEGER NOT NULL,
    message     VARCHAR NOT NULL,
    reply       VARCHAR,
    tool_calls  JSON,
    response_id VARCHAR,
    created_at  TIMESTAMP NOT NULL,
    PRIMARY KEY (ticket_id, seq)
);

-- Model answers keyed by everything that can change them. See triage._cache_key.
CREATE TABLE IF NOT EXISTS llm_cache (
    key        VARCHAR PRIMARY KEY,
    feature    VARCHAR NOT NULL,
    value      JSON NOT NULL,
    created_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS kb_articles (
    slug  VARCHAR PRIMARY KEY,
    title VARCHAR NOT NULL,
    body  VARCHAR NOT NULL
);
"""

TABLES = ["llm_cache", "assistant_turns", "refunds", "ticket_embeddings", "kb_chunks", "kb_articles", "tickets", "orders", "customers"]


@contextmanager
def connect(read_only: bool = False) -> Iterator[duckdb.DuckDBPyConnection]:
    """
    Open, use, close. DuckDB allows one writing process per file, so the web
    app and the demo scripts each hold the file only as long as they need it.

    Within one process, every connection to the file must use the same
    configuration, so the web app never asks for read_only. Scripts can.
    """
    con = duckdb.connect(str(settings.db_path), read_only=read_only)
    try:
        yield con
    finally:
        con.close()


def seed(con: duckdb.DuckDBPyConnection, reset: bool = False) -> None:
    if reset:
        for table in TABLES:
            con.execute(f"DROP TABLE IF EXISTS {table}")
    con.execute(SCHEMA)

    for table, file in [("customers", "customers.json"), ("orders", "orders.json"), ("tickets", "tickets.json")]:
        if con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0:
            con.execute(
                f"INSERT INTO {table} BY NAME SELECT * FROM read_json(?, format = 'array')",
                [str(DATA_DIR / file)],
            )

    if con.execute("SELECT count(*) FROM kb_articles").fetchone()[0] == 0:
        for path in sorted((DATA_DIR / "kb").glob("*.md")):
            text = path.read_text(encoding="utf-8")
            title = text.splitlines()[0].removeprefix("# ").strip()
            con.execute("INSERT INTO kb_articles VALUES (?, ?, ?)", [path.stem, title, text])


AI_COLUMNS = [
    "ai_category", "ai_priority", "ai_order_number", "ai_product", "ai_sentiment",
    "ai_summary", "ai_needs_review", "ai_prompt_version", "ai_model", "ai_triaged_at",
]


def reset_demo(con: duckdb.DuckDBPyConnection) -> int:
    """
    Put the demo back where it starts, without re-embedding anything: clear AI
    suggestions and accepted labels on the seeded open tickets, and delete any
    ticket created during the demo, every assistant conversation, and every
    refund. Returns how many tickets were deleted.
    """
    seeded = json.loads((DATA_DIR / "tickets.json").read_text(encoding="utf-8"))
    seeded_ids = [t["ticket_id"] for t in seeded]
    open_ids = [t["ticket_id"] for t in seeded if t["status"] == "open"]

    created = [
        r[0]
        for r in con.execute(
            "SELECT ticket_id FROM tickets WHERE NOT list_contains(?, ticket_id)", [seeded_ids]
        ).fetchall()
    ]
    if created:
        has_embeddings = con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = 'ticket_embeddings'"
        ).fetchone()[0]
        if has_embeddings:
            con.execute("DELETE FROM ticket_embeddings WHERE list_contains(?, ticket_id)", [created])
        con.execute("DELETE FROM tickets WHERE list_contains(?, ticket_id)", [created])

    con.execute("DELETE FROM assistant_turns")
    con.execute("DELETE FROM refunds")

    cleared = ", ".join(f"{c} = NULL" for c in AI_COLUMNS + ["category", "priority"])
    con.execute(f"UPDATE tickets SET {cleared} WHERE list_contains(?, ticket_id)", [open_ids])
    return len(created)


def rows(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> list[dict]:
    """Run a query and return a list of dicts, which is what templates want."""
    cursor = con.execute(sql, params or [])
    columns = [c[0] for c in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def next_ticket_id(con: duckdb.DuckDBPyConnection) -> str:
    highest = con.execute(
        "SELECT max(CAST(substr(ticket_id, 3) AS INTEGER)) FROM tickets"
    ).fetchone()[0]
    return f"T-{(highest or 1000) + 1}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Create and seed the help desk database.")
    parser.add_argument("--reset", action="store_true", help="drop everything and reload from data/")
    parser.add_argument("--reset-demo", action="store_true", help="clear AI results and demo-created tickets; keep embeddings")
    args = parser.parse_args()

    with connect() as con:
        seed(con, reset=args.reset)
        if args.reset_demo:
            deleted = reset_demo(con)
            print(f"Cleared triage on the open tickets; deleted {deleted} ticket(s) created during the demo.\n")
        for table in ["customers", "orders", "tickets", "kb_articles"]:
            count = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            print(f"{table:>12}: {count}")
    print(f"\nDatabase: {settings.db_path}")


if __name__ == "__main__":
    main()
