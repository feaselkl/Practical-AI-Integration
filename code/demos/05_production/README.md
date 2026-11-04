# Demo 5 &mdash; Making It Production-Ready

**Talk section:** 7. Making It Production-Ready (10 min)

## The Point

You can't tell whether a prompt change made things better by reading a few outputs. You need an eval: labeled examples, a score per field, and gates that fail the build. The help desk already has the labels, because every resolved ticket has the category and priority a person gave it. Around the eval sit the unglamorous parts: every call logged, costs computed from the log, results cached where inputs repeat, and timeouts and retries set on purpose.

## The Narrative

1. **Your history is your eval set.** Show the 36 resolved tickets: each has a human-assigned category and priority. Nobody built an eval set; the help desk has been building one all along.
2. **The baseline.** Run `eval_triage.py v1`. Category 97%, priority 92%, urgent recall 100%. Walk the four misses and ask the room who is wrong. T-1010 is a 2FA problem whose real ask is changing a shipping address; *shipping* is defensible. Not every miss is the model's fault, and labels deserve a second look too.
3. **A reasonable change.** On-call complains about being paged for every urgent ticket. Someone edits one line of the prompt: *"urgent: someone has been injured."* Show the diff between [`v1.md`](../../prompts/triage/v1.md) and [`v2.md`](../../prompts/triage/v2.md). It looks like a fix.
4. **Catch it before it ships.** Run `eval_triage.py v1 v2`. Category accuracy goes *up*, to 100%. Then urgent recall: **0%**. Battery swelling, a cracked fork, failed brakes, and an overheating charger are all downgraded, two of them to `normal`. The gates fail and the command exits 1, which is what stops it in CI. The headline number improved while the one that matters collapsed; that is why you score more than one number.
5. **Caching, for free.** In that same run, v1 made **0 model calls**: every answer came from the cache, keyed by prompt text, model, reasoning settings, and input. Editing the prompt changes the key, so v2 made 36 fresh calls. Say where caching pays: evals, repeated queries, backfills. Live tickets are almost never identical.
6. **What it costs, and how fast it is.** Run `cost_report.py`. Calls, cache hits, errors, tokens, p50 and p95 latency, and cost per feature and prompt version, straight from the JSONL log with DuckDB. Point out that p95 is several times p50; plan the UI for p95.
7. **The rest, on slides:**
   - **Timeouts and retries:** `LLM_TIMEOUT_SECONDS=60`, `LLM_MAX_RETRIES=3` on the client, in [`llm.py`](../../helpdesk/ai/llm.py). The SDK defaults are 10 minutes and 2 retries.
   - **Graceful degradation:** every feature is behind a flag and an error boundary; demo 1 already showed the app surviving a dead model.
   - **Prompts as versioned files:** every call, every eval run, and every stored triage records the prompt version.
   - **Model lifecycle:** deployments retire. Pin the version and rerun the eval before every upgrade.
   - **Data you send:** tickets, orders, and conversations go to the provider, and `previous_response_id` means the provider stores the assistant's conversations. Know your retention settings.

## To Build

- [x] `telemetry.py` &mdash; every chat and embedding call logged to `logs/llm_calls.jsonl`, including failures
- [x] Triage cache in DuckDB, keyed by prompt text, model, reasoning settings, and input
- [x] `prompts/triage/v2.md` &mdash; the one-line "on-call fix"
- [x] `eval_triage.py` &mdash; parallel, cached, per-field accuracy, urgent recall, per-ticket diff between versions, gates, non-zero exit
- [x] `cost_report.py` &mdash; DuckDB over the call log; cost from prices in `.env`
- [x] Client timeouts and retries from `.env`
- [x] Prices in `.env`: `gpt-6-luna` and `text-embedding-3-small`, Global Standard, from the Azure Retail Prices API on 2026-10-16
- [ ] Slides for this section
- [ ] Record the fallback video

## Run It

From `code/`:

```bash
uv run python demos/05_production/eval_triage.py v1
uv run python demos/05_production/eval_triage.py v1 v2
uv run python demos/05_production/eval_triage.py v2 --no-cache
uv run python demos/05_production/cost_report.py
uv run python demos/05_production/cost_report.py --since 2026-10-16
```

Eval results are saved under `logs/evals/`. The cache lives in the `llm_cache` table; `helpdesk.db --reset-demo` keeps it, and `--reset` drops it.

## Rehearsal Baseline

2026-10-16, 36 labeled tickets:

| | triage/v1 | triage/v2 |
|---|---|---|
| Category accuracy | 97% | 100% |
| Priority accuracy | 92% | 78% |
| Urgent recall | 100% | **0%** |
| Gates | pass | **fail** (exit 1) |
| Model calls (second run) | 0, all cached | 36 |
| Wall time | 30s cold, 0.0s cached | 34s |

v1 &rarr; v2 changes: BROKE T-1006, T-1013, T-1027, T-1035 (all urgent safety tickets) and T-1021; FIXED T-1010.

Triage latency across both runs: p50 2.3 to 3.9 seconds, p95 8.6 to 11.4 seconds. At Global Standard prices, a full 36-ticket eval run costs about $0.004.

## Stage Risks

- **Run v1 once before the talk, so it is cached.** The cold run takes about 30 seconds; the cached one is instant, and that contrast is part of the demo. If you edit `v1.md`, even whitespace, the cache misses.
- **The exact numbers move between runs.** Run `--no-cache` and the accuracies shift by a ticket or two. The v2 urgent collapse has been consistent, but rehearse it with `v2 --no-cache` the day before. If v2 ever keeps an urgent ticket, the gate still fails on the others; say so rather than hiding it.
- **Thirty-six parallel calls can hit a rate limit** on a low-quota deployment. The SDK retries 429s with backoff, so it slows down rather than failing. If it is slow on the day, lower `--workers`.
- **Know where the prices came from.** The `.env` prices are Global Standard rates from the public [Azure Retail Prices API](https://prices.azure.com/api/retail/prices), pulled 2026-10-16: `gpt-6-luna` short context at $0.10 input and $0.50 output per million tokens, and `text-embedding-3-small` at $0.02 per million. Data Zone deployments cost about 10 to 20% more, and long-context requests about double. Re-pull before each delivery; the Foundry talk's `pricing/refresh-prices.py` already queries these meters. The report also charges every input token at the full rate, though Azure bills prompt-cache hits at a tenth of that.
- **The log keeps growing.** Every rehearsal adds lines. Use `--since` to show only today, or delete `logs/llm_calls.jsonl` before the talk.
