"""
Demo 5 -- Evaluate triage against labeled history

Every resolved ticket already has the category and priority a person gave it.
That's an eval set you didn't have to build. Run the triage prompt over it,
score every field, and compare prompt versions before one ships.

Run:  uv run python demos/05_production/eval_triage.py              # prompt v1
      uv run python demos/05_production/eval_triage.py v1 v2        # compare
      uv run python demos/05_production/eval_triage.py v2 --no-cache

Results are cached by prompt text, model, and input, so rerunning an
unchanged prompt costs nothing. Exits non-zero if a gate fails, so the same
command works as a CI check.
"""

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from helpdesk.ai.llm import describe
from helpdesk.ai.triage import TriageError, triage_ticket
from helpdesk.config import CODE_DIR
from helpdesk.db import connect, rows, seed

# Gates: the rules a prompt or model change must pass before it ships.
# Missing a safety ticket is the expensive mistake, so it gets its own gate.
MIN_URGENT_RECALL = 1.0
MAX_ACCURACY_DROP = 0.03  # versus the first version listed

RESULTS_DIR = CODE_DIR / "logs" / "evals"


def run_version(con, tickets: list[dict], version: str, use_cache: bool, workers: int) -> dict:
    def one(ticket: dict) -> dict:
        cursor = con.cursor()  # one DuckDB cursor per thread
        try:
            r = triage_ticket(ticket["subject"], ticket["body"], prompt_version=version,
                              cache=cursor if use_cache else None)
            return {"ticket_id": ticket["ticket_id"], "category": r.triage.category.value,
                    "priority": r.triage.priority.value, "cached": r.cached,
                    "input_tokens": r.input_tokens, "output_tokens": r.output_tokens}
        except TriageError as exc:
            return {"ticket_id": ticket["ticket_id"], "error": str(exc)}
        finally:
            cursor.close()

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        predictions = list(pool.map(one, tickets))
    seconds = time.perf_counter() - started

    gold = {t["ticket_id"]: t for t in tickets}
    scored = [p for p in predictions if "error" not in p]
    category_ok = [p for p in scored if p["category"] == gold[p["ticket_id"]]["category"]]
    priority_ok = [p for p in scored if p["priority"] == gold[p["ticket_id"]]["priority"]]
    urgent = [p for p in scored if gold[p["ticket_id"]]["priority"] == "urgent"]
    urgent_caught = [p for p in urgent if p["priority"] == "urgent"]

    return {
        "version": version,
        "n": len(tickets),
        "errors": len(predictions) - len(scored),
        "category_accuracy": len(category_ok) / len(tickets),
        "priority_accuracy": len(priority_ok) / len(tickets),
        "urgent_recall": len(urgent_caught) / len(urgent) if urgent else 1.0,
        "urgent_total": len(urgent),
        "calls": sum(1 for p in scored if not p["cached"]),
        "cached": sum(1 for p in scored if p["cached"]),
        "input_tokens": sum(p.get("input_tokens", 0) for p in scored),
        "output_tokens": sum(p.get("output_tokens", 0) for p in scored),
        "seconds": round(seconds, 1),
        "predictions": predictions,
    }


def misses(result: dict, gold: dict) -> list[str]:
    lines = []
    for p in result["predictions"]:
        g = gold[p["ticket_id"]]
        if "error" in p:
            lines.append(f"{p['ticket_id']}  ERROR {p['error']}")
            continue
        for field in ("category", "priority"):
            if p[field] != g[field]:
                lines.append(f"{p['ticket_id']}  {field:<8} expected {g[field]:<16} got {p[field]:<16} {g['subject']}")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate triage prompts against labeled tickets.")
    parser.add_argument("versions", nargs="*", default=["v1"], help="prompt versions; the first is the baseline")
    parser.add_argument("--no-cache", action="store_true", help="call the model even for inputs seen before")
    parser.add_argument("--workers", type=int, default=8, help="parallel calls (default 8)")
    args = parser.parse_args()

    with connect() as con:
        seed(con)  # creates the cache table if this database predates it
        tickets = rows(
            con,
            "SELECT ticket_id, subject, body, category, priority FROM tickets "
            "WHERE status = 'resolved' AND category IS NOT NULL ORDER BY ticket_id",
        )
        gold = {t["ticket_id"]: t for t in tickets}
        print(f"{describe()}\nEval set: {len(tickets)} resolved tickets with human labels\n")

        results = []
        for version in args.versions:
            print(f"Running triage/{version}...", flush=True)
            results.append(run_version(con, tickets, version, not args.no_cache, args.workers))

    # Side-by-side summary
    print()
    print(f"{'':<20}" + "".join(f"{'triage/' + r['version']:>14}" for r in results))
    for label, key, fmt in [
        ("Category accuracy", "category_accuracy", "{:.0%}"),
        ("Priority accuracy", "priority_accuracy", "{:.0%}"),
        ("Urgent recall", "urgent_recall", "{:.0%}"),
        ("Errors", "errors", "{}"),
        ("Model calls", "calls", "{}"),
        ("From cache", "cached", "{}"),
        ("Tokens in", "input_tokens", "{:,}"),
        ("Tokens out", "output_tokens", "{:,}"),
        ("Seconds", "seconds", "{}"),
    ]:
        print(f"{label:<20}" + "".join(f"{fmt.format(r[key]):>14}" for r in results))

    for r in results:
        found = misses(r, gold)
        print(f"\ntriage/{r['version']} misses ({len(found)}):")
        for line in found or ["(none)"]:
            print(f"    {line}")

    # What changed, ticket by ticket, relative to the baseline
    if len(results) > 1:
        base = {p["ticket_id"]: p for p in results[0]["predictions"]}
        for r in results[1:]:
            print(f"\ntriage/{results[0]['version']} -> triage/{r['version']}, tickets that changed:")
            changed = False
            for p in r["predictions"]:
                b = base[p["ticket_id"]]
                for field in ("category", "priority"):
                    if p.get(field) != b.get(field):
                        changed = True
                        right = gold[p["ticket_id"]][field]
                        verdict = "FIXED" if p.get(field) == right else "BROKE" if b.get(field) == right else "changed"
                        print(f"    {verdict:<8}{p['ticket_id']}  {field}: {b.get(field)} -> {p.get(field)}  "
                              f"(label: {right})  {gold[p['ticket_id']]['subject']}")
            if not changed:
                print("    (none)")

    # Gates
    failures = []
    baseline = results[0]
    for r in results:
        if r["urgent_recall"] < MIN_URGENT_RECALL:
            failures.append(f"triage/{r['version']}: urgent recall {r['urgent_recall']:.0%} "
                            f"(missed {r['urgent_total'] - round(r['urgent_recall'] * r['urgent_total'])} "
                            f"of {r['urgent_total']} safety tickets)")
        if r is not baseline:
            for key in ("category_accuracy", "priority_accuracy"):
                if baseline[key] - r[key] > MAX_ACCURACY_DROP:
                    failures.append(f"triage/{r['version']}: {key.replace('_', ' ')} fell "
                                    f"{baseline[key]:.0%} -> {r[key]:.0%}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{'-'.join(args.versions)}.json"
    out.write_text(json.dumps({"model": describe(), "results": results, "gate_failures": failures}, indent=1))

    print()
    if failures:
        print("GATES FAILED:")
        for f in failures:
            print(f"    {f}")
    else:
        print("All gates passed.")
    print(f"Results saved to {out.relative_to(CODE_DIR)}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
