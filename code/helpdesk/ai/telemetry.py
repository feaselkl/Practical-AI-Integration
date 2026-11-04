"""
Every model call, logged: what it was for, which prompt, which model, how many
tokens, how long, whether it came from the cache, and whether it failed.

One JSON line per call in logs/llm_calls.jsonl. No logging service required:
DuckDB reads the file directly (see demos/05_production/cost_report.py).

Cost is not stored. It is computed at report time from the prices in .env, so
correcting a price corrects the whole history.
"""

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from helpdesk.config import CODE_DIR

LOG_PATH = CODE_DIR / "logs" / "llm_calls.jsonl"
_lock = threading.Lock()


def _write(record: dict) -> None:
    LOG_PATH.parent.mkdir(exist_ok=True)
    line = json.dumps(record, default=str)
    with _lock, LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


@contextmanager
def track(feature: str, kind: str = "chat", prompt: str | None = None) -> Iterator[dict]:
    """
    Wrap one model call. The caller fills in model and token counts; timing,
    errors, and the write happen here, even when the call raises.

        with track("triage", prompt=prompt.id) as call:
            response = client.responses.parse(...)
            call.update(model=response.model, input_tokens=..., output_tokens=...)
    """
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "feature": feature,
        "kind": kind,
        "prompt": prompt,
        "model": None,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached": False,
        "error": None,
    }
    started = time.perf_counter()
    try:
        yield record
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"[:300]
        raise
    finally:
        record["seconds"] = round(time.perf_counter() - started, 3)
        _write(record)
