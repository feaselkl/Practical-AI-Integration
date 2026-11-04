"""
Feature 4: a support assistant that can look things up and propose actions.

Tool calling, in one loop:

    1. Send the conversation and a list of tools
    2. The model either answers, or asks you to run one or more tools
    3. YOUR code runs them and sends back the results
    4. Repeat until it answers, or until you stop it

The model never touches the database. It asks; this code decides. Every tool
is scoped to the current ticket's customer, the hard rules live in code, and
the only "write" tool creates a proposal that a person has to approve.
"""

import json
import time
from dataclasses import dataclass, field
from decimal import Decimal

import duckdb

from helpdesk.ai import answer
from helpdesk.ai.llm import chat_client, chat_model, reasoning_options
from helpdesk.ai.prompts import load_prompt
from helpdesk.ai.telemetry import track
from helpdesk.db import rows

PROMPT_VERSION = "v1"

# A model that keeps calling tools is a bug or an attack. Either way, stop.
MAX_TOOL_ROUNDS = 5


def _function(name: str, description: str, properties: dict) -> dict:
    """A strict function tool: the arguments are guaranteed to match the schema."""
    return {
        "type": "function",
        "name": name,
        "description": description,
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


TOOLS = [
    _function(
        "get_order",
        "Look up one of this customer's orders: status, items, dates, tracking, and refunds so far.",
        {"order_id": {"type": "string", "description": "Order number, like WW-10457"}},
    ),
    _function("list_orders", "List this customer's orders, newest first.", {}),
    _function(
        "search_knowledge_base",
        "Search Wobbly Wheel's policy articles. Use this for any question about policy, time frames, or fees.",
        {"query": {"type": "string", "description": "What you need to know, in plain words"}},
    ),
    _function(
        "propose_refund",
        "Propose a refund for the agent to approve. This does NOT issue the refund.",
        {
            "order_id": {"type": "string", "description": "Order number, like WW-10462"},
            "amount": {"type": "number", "description": "Refund amount in US dollars"},
            "reason": {"type": "string", "description": "One sentence, citing the policy that supports it"},
        },
    ),
]


class TicketTools:
    """
    The tool implementations, bound to one ticket and its customer. Least
    privilege: there is no way to ask for another customer's orders, because
    the customer ID never comes from the model.
    """

    def __init__(self, con: duckdb.DuckDBPyConnection, ticket_id: str, customer_id: str):
        self.con, self.ticket_id, self.customer_id = con, ticket_id, customer_id

    def run(self, name: str, arguments: dict) -> dict:
        handler = {
            "get_order": self.get_order,
            "list_orders": self.list_orders,
            "search_knowledge_base": self.search_knowledge_base,
            "propose_refund": self.propose_refund,
        }.get(name)
        if handler is None:
            return {"error": f"Unknown tool {name}"}
        try:
            return handler(**arguments)
        except Exception as exc:
            # A failing tool is information for the model, not a crash for the app.
            return {"error": f"{type(exc).__name__}: {exc}"}

    def _order(self, order_id: str) -> dict | None:
        found = rows(
            self.con, "SELECT * FROM orders WHERE order_id = ? AND customer_id = ?", [order_id, self.customer_id]
        )
        return found[0] if found else None

    def _refunded(self, order_id: str) -> Decimal:
        total = self.con.execute(
            "SELECT coalesce(sum(amount), 0) FROM refunds WHERE order_id = ? AND status IN ('proposed', 'approved')",
            [order_id],
        ).fetchone()[0]
        return Decimal(total)

    def get_order(self, order_id: str) -> dict:
        order = self._order(order_id)
        if order is None:
            return {"error": f"No order {order_id} for this customer"}
        order.pop("customer_id")
        order["refunds"] = rows(
            self.con, "SELECT refund_id, amount, status, reason FROM refunds WHERE order_id = ?", [order_id]
        )
        return order

    def list_orders(self) -> dict:
        return {
            "orders": rows(
                self.con,
                "SELECT order_id, status, placed_at, delivered_at, total FROM orders "
                "WHERE customer_id = ? ORDER BY placed_at DESC",
                [self.customer_id],
            )
        }

    def search_knowledge_base(self, query: str) -> dict:
        return {
            "results": [
                {"section": s["heading"], "text": s["text"]}
                for s in answer.retrieve(self.con, query, k=3)
            ]
        }

    def propose_refund(self, order_id: str, amount: float, reason: str) -> dict:
        # The rules that must hold no matter what the model says live HERE, in
        # code. The prompt asks the model to follow policy; this enforces it.
        order = self._order(order_id)
        if order is None:
            return {"error": f"No order {order_id} for this customer"}
        amount = Decimal(str(amount)).quantize(Decimal("0.01"))
        if amount <= 0:
            return {"error": "Refund amount must be positive"}
        refundable = Decimal(order["total"]) - self._refunded(order_id)
        if amount > refundable:
            return {"error": f"Only ${refundable} of order {order_id} is still refundable"}

        count = self.con.execute("SELECT count(*) FROM refunds").fetchone()[0]
        refund_id = f"R-{1001 + count}"
        self.con.execute(
            "INSERT INTO refunds VALUES (?, ?, ?, ?, ?, 'proposed', now(), NULL)",
            [refund_id, self.ticket_id, order_id, amount, reason],
        )
        return {"refund_id": refund_id, "status": "proposed", "amount": str(amount),
                "note": "Waiting for the agent to approve it in the help desk. Nothing has been refunded."}


@dataclass
class ToolCall:
    name: str
    arguments: dict
    result: dict


@dataclass
class AssistantTurn:
    reply: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    response_id: str | None = None
    rounds: int = 0
    stopped_early: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0


def run_turn(
    con: duckdb.DuckDBPyConnection,
    ticket: dict,
    message: str,
    previous_response_id: str | None = None,
) -> AssistantTurn:
    """
    One agent message in, one assistant reply out, with however many tool
    calls it takes in between (up to MAX_TOOL_ROUNDS).

    previous_response_id lets the API keep the conversation: each request sends
    only what is new. The first turn carries the ticket itself.
    """
    prompt = load_prompt("assistant", PROMPT_VERSION)
    tools = TicketTools(con, ticket["ticket_id"], ticket["customer_id"])
    turn = AssistantTurn(reply="")
    started = time.perf_counter()

    if previous_response_id is None:
        content = (
            f"<ticket>\nTicket {ticket['ticket_id']} from {ticket['customer_name']}\n"
            f"Subject: {ticket['subject']}\n\n{ticket['body']}\n</ticket>\n\n{message}"
        )
    else:
        content = message
    pending: list = [{"role": "user", "content": content}]

    while True:
        with track("assistant", prompt=prompt.id) as call:
            response = chat_client().responses.create(
                model=chat_model(),
                # Instructions and tools are not carried over by previous_response_id.
                instructions=prompt.text,
                tools=TOOLS,
                input=pending,
                previous_response_id=previous_response_id,
                max_output_tokens=4000,
                **reasoning_options(),
            )
            usage = response.usage
            call.update(
                model=response.model,
                input_tokens=usage.input_tokens if usage else 0,
                output_tokens=usage.output_tokens if usage else 0,
            )
        previous_response_id = response.id
        turn.input_tokens += call["input_tokens"]
        turn.output_tokens += call["output_tokens"]

        calls = [item for item in response.output if item.type == "function_call"]
        if not calls:
            turn.reply = response.output_text
            break

        turn.rounds += 1
        if turn.rounds > MAX_TOOL_ROUNDS:
            turn.stopped_early = True
            turn.reply = f"Stopped after {MAX_TOOL_ROUNDS} rounds of tool calls without an answer."
            break

        pending = []
        for call in calls:
            arguments = json.loads(call.arguments)
            result = tools.run(call.name, arguments)
            turn.tool_calls.append(ToolCall(call.name, arguments, result))
            pending.append(
                {"type": "function_call_output", "call_id": call.call_id, "output": json.dumps(result, default=str)}
            )

    turn.response_id = previous_response_id
    turn.seconds = time.perf_counter() - started
    return turn


# ---------------------------------------------------------------------------
# Conversation storage: one row per agent message, so the page can show the
# transcript and the next turn can continue from the last response ID.
# ---------------------------------------------------------------------------


def transcript(con: duckdb.DuckDBPyConnection, ticket_id: str) -> list[dict]:
    turns = rows(con, "SELECT * FROM assistant_turns WHERE ticket_id = ? ORDER BY seq", [ticket_id])
    for t in turns:
        t["tool_calls"] = json.loads(t["tool_calls"]) if t["tool_calls"] else []
    return turns


def save_turn(con: duckdb.DuckDBPyConnection, ticket_id: str, message: str, turn: AssistantTurn) -> None:
    seq = con.execute(
        "SELECT coalesce(max(seq), 0) + 1 FROM assistant_turns WHERE ticket_id = ?", [ticket_id]
    ).fetchone()[0]
    calls = [{"name": c.name, "arguments": c.arguments, "result": c.result} for c in turn.tool_calls]
    con.execute(
        "INSERT INTO assistant_turns VALUES (?, ?, ?, ?, ?, ?, now())",
        [ticket_id, seq, message, turn.reply, json.dumps(calls, default=str), turn.response_id],
    )


def last_response_id(con: duckdb.DuckDBPyConnection, ticket_id: str) -> str | None:
    found = con.execute(
        "SELECT response_id FROM assistant_turns WHERE ticket_id = ? ORDER BY seq DESC LIMIT 1", [ticket_id]
    ).fetchone()
    return found[0] if found else None


def load_ticket(con: duckdb.DuckDBPyConnection, ticket_id: str) -> dict | None:
    found = rows(
        con,
        """
        SELECT t.ticket_id, t.customer_id, t.subject, t.body, c.name AS customer_name
        FROM tickets t JOIN customers c USING (customer_id) WHERE t.ticket_id = ?
        """,
        [ticket_id],
    )
    return found[0] if found else None
