"""
The Wobbly Wheel help desk: an ordinary web app that gains AI features.

Run:  uv run uvicorn helpdesk.web:app --reload
Then: http://127.0.0.1:8000

Every AI feature sits behind a feature flag in .env and inside an ai_guard
block. Turn the flags off, or unplug the network, and the help desk still
lists, opens, and creates tickets. That is the bar for "AI as a feature".
"""

import json
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from helpdesk import db
from helpdesk.ai import answer, assistant, llm, search, triage
from helpdesk.config import settings

log = logging.getLogger("helpdesk")

HERE = Path(__file__).parent
ORDER_NUMBER = re.compile(r"^WW-\d{5}$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    with db.connect() as con:
        db.seed(con)
    yield


app = FastAPI(title="Wobbly Wheel Help Desk", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals["settings"] = settings
templates.env.globals["ai_backend"] = llm.describe()


def redirect(url: str, message: str | None = None, error: str | None = None) -> RedirectResponse:
    if message:
        url += f"?msg={quote(message)}"
    elif error:
        url += f"?err={quote(error)}"
    return RedirectResponse(url, status_code=303)


def ai_failure(feature: str, exc: Exception) -> str:
    """
    The UI boundary for every AI call. Log the details for us, show a short
    message to the agent, and let the page render without the feature.
    """
    log.exception("AI feature %s failed", feature)
    # Our own RuntimeErrors are setup problems with a fix in the message
    # ("run build_index.py"), so show it. Anything else stays generic.
    detail = str(exc) if type(exc) is RuntimeError else type(exc).__name__
    return f"{feature} is unavailable right now ({detail}). The help desk still works without it."


@app.get("/")
def ticket_list(request: Request, status: str = "open"):
    where = "" if status == "all" else "WHERE t.status = 'open'"
    with db.connect() as con:
        tickets = db.rows(
            con,
            f"""
            SELECT t.*, c.name AS customer_name
            FROM tickets t JOIN customers c USING (customer_id)
            {where}
            ORDER BY t.created_at DESC
            """,
        )
    return templates.TemplateResponse(request, "tickets.html", {"tickets": tickets, "status": status})


@app.get("/tickets/new")
def new_ticket_form(request: Request):
    with db.connect() as con:
        customers = db.rows(con, "SELECT customer_id, name FROM customers ORDER BY name")
    return templates.TemplateResponse(request, "new_ticket.html", {"customers": customers})


@app.post("/tickets/new")
def create_ticket(customer_id: str = Form(...), subject: str = Form(...), body: str = Form(...)):
    with db.connect() as con:
        ticket_id = db.next_ticket_id(con)
        con.execute(
            "INSERT INTO tickets (ticket_id, customer_id, created_at, subject, body, status) "
            "VALUES (?, ?, now(), ?, ?, 'open')",
            [ticket_id, customer_id, subject, body],
        )

        # The ticket is saved before any AI runs. If the model is slow or down,
        # the customer's ticket is never lost.
        notes = []
        if settings.feature_triage:
            try:
                triage.save_triage(con, ticket_id, triage.triage_ticket(subject, body))
            except Exception as exc:
                notes.append(ai_failure("Triage", exc))
        if settings.feature_similar_tickets:
            try:
                search.embed_ticket(con, ticket_id, subject, body)
            except Exception as exc:
                notes.append(ai_failure("Similar tickets", exc))

    if notes:
        return redirect(f"/tickets/{ticket_id}", error=" ".join(notes))
    return redirect(f"/tickets/{ticket_id}", message=f"Created {ticket_id}.")


@app.get("/tickets/{ticket_id}")
def ticket_detail(request: Request, ticket_id: str, msg: str | None = None, err: str | None = None):
    with db.connect() as con:
        found = db.rows(
            con,
            """
            SELECT t.*, c.name AS customer_name, c.email AS customer_email, c.since AS customer_since
            FROM tickets t JOIN customers c USING (customer_id)
            WHERE t.ticket_id = ?
            """,
            [ticket_id],
        )
        if not found:
            raise HTTPException(404, f"No ticket {ticket_id}")
        ticket = found[0]

        orders = db.rows(
            con, "SELECT * FROM orders WHERE customer_id = ? ORDER BY placed_at DESC", [ticket["customer_id"]]
        )

        # Trust, then verify: the model extracted an order number, but does
        # that order exist, and does it belong to this customer?
        extracted = ticket["ai_order_number"]
        order_check = None
        if extracted:
            if not ORDER_NUMBER.match(extracted):
                order_check = f"Extracted order number {extracted!r} is not in the WW-12345 format."
            elif extracted not in {o["order_id"] for o in orders}:
                order_check = f"Order {extracted} does not belong to this customer."

        similar, similar_error = [], None
        if settings.feature_similar_tickets:
            try:
                similar = search.similar_tickets(con, ticket_id, k=3)
            except Exception as exc:
                similar_error = ai_failure("Similar tickets", exc)

        # Retrieval only, no generation: shown on page load so the agent can see
        # what the draft will be grounded in before asking for one.
        kb_sources, kb_error = [], None
        if settings.feature_draft_reply:
            try:
                kb_sources = answer.retrieve_for_ticket(con, ticket_id)
            except Exception as exc:
                kb_error = ai_failure("Knowledge base search", exc)

        conversation = assistant.transcript(con, ticket_id) if settings.feature_assistant else []
        refunds = db.rows(con, "SELECT * FROM refunds WHERE ticket_id = ? ORDER BY proposed_at", [ticket_id])

    return templates.TemplateResponse(
        request,
        "ticket.html",
        {
            "ticket": ticket,
            "orders": orders,
            "order_check": order_check,
            "similar": similar,
            "similar_error": similar_error,
            "kb_sources": kb_sources,
            "kb_error": kb_error,
            "conversation": conversation,
            "refunds": refunds,
            "msg": msg,
            "err": err,
        },
    )


@app.post("/tickets/{ticket_id}/triage")
def triage_one(ticket_id: str):
    with db.connect() as con:
        found = db.rows(con, "SELECT subject, body FROM tickets WHERE ticket_id = ?", [ticket_id])
        if not found:
            raise HTTPException(404, f"No ticket {ticket_id}")
        try:
            result = triage.triage_ticket(found[0]["subject"], found[0]["body"])
        except Exception as exc:
            return redirect(f"/tickets/{ticket_id}", error=ai_failure("Triage", exc))
        triage.save_triage(con, ticket_id, result)

    return redirect(
        f"/tickets/{ticket_id}",
        message=(
            f"Triaged by {result.model} with prompt {result.prompt.id} in {result.seconds:.1f}s "
            f"({result.input_tokens} tokens in, {result.output_tokens} out)."
        ),
    )


def sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.get("/tickets/{ticket_id}/draft")
def draft_reply(ticket_id: str):
    """
    Stream a drafted reply as server-sent events: `delta` for each piece of
    text, then `done` with the citation check, or `failed`. Nothing is saved
    and nothing is sent; the draft lands in an editable box.
    """
    if not settings.feature_draft_reply:
        raise HTTPException(404, "Draft a reply is turned off")
    with db.connect() as con:
        context = answer.ticket_context(con, ticket_id)
        if context is None:
            raise HTTPException(404, f"No ticket {ticket_id}")
        ticket, customer_name, orders = context
        try:
            sources = answer.retrieve_for_ticket(con, ticket_id)
        except Exception as exc:
            sources, retrieval_error = [], exc
        else:
            retrieval_error = None

    def events():
        if retrieval_error is not None:
            yield sse("failed", ai_failure("Draft a reply", retrieval_error))
            return
        try:
            for part in answer.stream_draft(ticket, customer_name, orders, sources):
                if isinstance(part, answer.Draft):
                    yield sse("done", {
                        "cited": part.cited,
                        "invalid": part.invalid_citations,
                        "model": part.model,
                        "prompt": part.prompt.id,
                        "seconds": round(part.seconds, 1),
                        "input_tokens": part.input_tokens,
                        "output_tokens": part.output_tokens,
                    })
                else:
                    yield sse("delta", part)
        except Exception as exc:
            yield sse("failed", ai_failure("Draft a reply", exc))

    return StreamingResponse(events(), media_type="text/event-stream")


@app.post("/tickets/{ticket_id}/assistant")
def ask_assistant(ticket_id: str, message: str = Form(...)):
    if not settings.feature_assistant:
        raise HTTPException(404, "The assistant is turned off")
    with db.connect() as con:
        ticket = assistant.load_ticket(con, ticket_id)
        if ticket is None:
            raise HTTPException(404, f"No ticket {ticket_id}")
        try:
            turn = assistant.run_turn(
                con, ticket, message, previous_response_id=assistant.last_response_id(con, ticket_id)
            )
        except Exception as exc:
            return redirect(f"/tickets/{ticket_id}", error=ai_failure("The assistant", exc))
        assistant.save_turn(con, ticket_id, message, turn)
    return redirect(
        f"/tickets/{ticket_id}",
        message=f"Assistant answered in {turn.seconds:.1f}s after {len(turn.tool_calls)} tool call(s).",
    )


@app.post("/tickets/{ticket_id}/assistant/reset")
def reset_assistant(ticket_id: str):
    with db.connect() as con:
        con.execute("DELETE FROM assistant_turns WHERE ticket_id = ?", [ticket_id])
    return redirect(f"/tickets/{ticket_id}", message="Started a new conversation.")


@app.post("/refunds/{refund_id}/{decision}")
def decide_refund(refund_id: str, decision: str):
    """The only path from "proposed" to "approved" is a person clicking a button."""
    if decision not in {"approve", "reject"}:
        raise HTTPException(404)
    status = "approved" if decision == "approve" else "rejected"
    with db.connect() as con:
        found = db.rows(con, "SELECT ticket_id FROM refunds WHERE refund_id = ? AND status = 'proposed'", [refund_id])
        if not found:
            raise HTTPException(404, f"No pending refund {refund_id}")
        con.execute("UPDATE refunds SET status = ?, decided_at = now() WHERE refund_id = ?", [status, refund_id])
    note = "Refund approved (simulated: no money moves in this demo)." if status == "approved" else "Refund rejected."
    return redirect(f"/tickets/{found[0]['ticket_id']}", message=f"{refund_id}: {note}")


@app.post("/tickets/{ticket_id}/accept")
def accept_triage(ticket_id: str):
    """A person accepts the suggestion. Only now do the real fields change."""
    with db.connect() as con:
        con.execute(
            "UPDATE tickets SET category = ai_category, priority = ai_priority "
            "WHERE ticket_id = ? AND ai_category IS NOT NULL",
            [ticket_id],
        )
    return redirect(f"/tickets/{ticket_id}", message="Suggestion accepted.")


@app.get("/search")
def search_page(request: Request, q: str = ""):
    keyword, semantic, error = [], [], None
    if q.strip():
        with db.connect() as con:
            keyword = search.keyword_search(con, q)
            if settings.feature_similar_tickets:
                try:
                    semantic = search.search_tickets(con, q)
                except Exception as exc:
                    error = ai_failure("Semantic search", exc)
    return templates.TemplateResponse(
        request, "search.html", {"q": q, "keyword": keyword, "semantic": semantic, "err": error}
    )
