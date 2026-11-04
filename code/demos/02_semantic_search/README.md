# Demo 2 &mdash; Embeddings and Semantic Search

**Talk section:** 4. Embeddings and Semantic Search (9 min)

## The Point

Customers and past tickets describe the same problem in different words, and keyword search can't bridge that. Embeddings turn text into vectors where similar meaning lands close together; DuckDB stores them next to the tickets and searches them with plain SQL. The payoff in the app is a "Similar tickets" panel that shows the agent how the last three of these were resolved.

## The Narrative

1. **The search they already have.** In the app, go to **Search** and type *clicking from the back wheel*. Keyword search finds only T-1037, the ticket that uses those exact words. The two past tickets about the same freehub problem say "ticking," "rear hub," "buzzing," and "rattling."
2. **What an embedding is.** One slide, no math: text in, 1,536 numbers out, and similar meanings give nearby numbers. Then show [`search.embed()`](../../helpdesk/ai/search.py): one call, the same client as demo 1, a different deployment.
3. **Store and search.** Show the `ticket_embeddings` table: a `FLOAT[1536]` column and a `model` column. Then show `_nearest()`: `ORDER BY array_cosine_distance(...) LIMIT k`. Say out loud that it is ordinary SQL against the database they already have.
4. **Semantic search.** Run the same query again: T-1020 and T-1001 come back, *with their resolutions*. That is what the agent actually wanted.
5. **Queries with no shared words.** *my battery is overheating*, *they took my money twice*, and *bike showed up broken* each find nothing by keyword and the right tickets by meaning.
6. **Across languages.** Open **T-1044**, the Spanish ticket about a bent wheel. The Similar tickets panel shows T-1024, *Bike arrived with bent rotor*, and its resolution. The English query *bike showed up broken* also finds T-1044. Nobody wrote a translation step.
7. **Production beat: the model name on every row.** Explain that this index was first built with a local 768-dimension model and moved to Azure's 1,536-dimension one. The old vectors weren't wrong, they were *incomparable*, so every ticket was re-embedded. The `model` column is how the app knows, and it refuses to compare mismatched vectors instead of quietly returning nothing.
8. **Optional: scale.** Run `build_index.py --hnsw`. At 44 tickets exact search is instant; at millions you build an HNSW index from the VSS extension and the same query uses it.

## To Build

- [x] `search.py` &mdash; embed, build the index, exact nearest-neighbor search, keyword search for comparison
- [x] `build_index.py` &mdash; embed what's missing; `--rebuild` after a model change; `--hnsw` on an in-memory copy
- [x] `search_tickets.py` &mdash; keyword vs. semantic for a query, or similar tickets for one ticket
- [x] Similar tickets panel on the ticket page, and side-by-side search page
- [x] New tickets are embedded as they arrive
- [x] Model mismatch raises a clear error instead of returning nothing
- [ ] Slides for this section (what an embedding is, the table, the query, the model-change story)
- [ ] Record the fallback video

## Run It

From `code/`:

```bash
uv run python demos/02_semantic_search/build_index.py               # embed anything missing
uv run python demos/02_semantic_search/build_index.py --rebuild     # after changing models
uv run python demos/02_semantic_search/build_index.py --hnsw        # show the VSS index

uv run python demos/02_semantic_search/search_tickets.py "clicking from the back wheel"
uv run python demos/02_semantic_search/search_tickets.py --ticket T-1044
```

In the app: **Search** for side-by-side keyword and semantic results, and the **Similar tickets** panel on any ticket page.

## Rehearsal Baseline

`text-embedding-3-small`, 2026-10-16. Top results (cosine distance):

| Query | Keyword search | Semantic search |
|---|---|---|
| clicking from the back wheel | T-1037 only | T-1037 0.447, T-1020 0.492, T-1001 0.542 |
| my battery is overheating | nothing | T-1038 0.502, T-1006 0.572, T-1035 0.576 |
| they took my money twice | nothing | T-1004 0.478, T-1041 0.514, T-1017 0.676 |
| bike showed up broken | nothing | T-1024 0.488, T-1044 0.514, T-1027 0.537 |
| *similar to T-1044 (Spanish)* | &mdash; | T-1024 0.414, T-1008 0.544, T-1012 0.593 |

## Stage Risks

- **Semantic search always returns something.** Ask for three results and you get three, relevant or not: *bike showed up broken* ranks *Brakes failed on descent* third. If someone in the room notices, agree with them. It is why the app shows distances, and why a production version needs a cutoff.
- **Distances only mean something within one model.** The local model put close matches around 0.25 to 0.30; `text-embedding-3-small` puts them around 0.45 to 0.55. A threshold tuned on one model is wrong on the next. Tune it on your own data, per model, and expect to retune it when the model changes.
- **Every query costs an embedding call.** Searching by ticket doesn't (the vector is already stored), but a typed query does. It is fast and cheap, but if the endpoint is down, semantic search shows as unavailable while keyword search keeps working.
- **Don't run `helpdesk.db --reset` before the talk.** It drops the embeddings too, and rebuilding needs the endpoint. Use `--reset-demo` between rehearsals.
- **Changing `EMBEDDING_PROVIDER` or the deployment means `--rebuild`.** Until you do, the Similar tickets panel says the index was built with a different model. That's intentional.
