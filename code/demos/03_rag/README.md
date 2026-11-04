# Demo 3 &mdash; RAG: Grounding AI in Your Data

**Talk section:** 5. RAG: Grounding AI in Your Data (10 min)

## The Point

The model doesn't know Wobbly Wheel's return policy, and if you ask anyway, it answers confidently from general knowledge. Retrieval-augmented generation puts the right policy in front of the model, makes it cite what it used, and gives it permission to say "I don't know." The result is a draft the agent can check in seconds and edit before sending.

## The Narrative

1. **The problem, live.** Run `draft_reply.py T-1040 --naive`. Aisha's helmet is too small and she tried it on indoors. The naive draft says *"Yes, you can return the Aero Lid for a refund"* and never mentions the rule that matters: helmets are returnable only if the retention straps haven't been adjusted. It sounds right, and nobody can check where it came from.
2. **Chunk.** Run `build_kb_index.py --show`. Twelve articles become 37 chunks, one per heading, each carrying its article title ("Returns Policy > E-bikes"). Splitting by heading works because the KB is already organized by topic. Say plainly that chunking is the step people get wrong most often and fix least often.
3. **Retrieve.** Open **T-1040** in the app. The **Draft a reply** panel already lists the four closest KB sections, with distances, *before* any generation. Point out that this cost no API call: the ticket's vector from demo 2 lives in the same space as the KB chunks.
4. **Generate, grounded.** Click **Draft a reply** and let it stream. The draft states the strap rule and the 45-day window, cites `[1]`, and works out *"start the return by November 17"* from the delivery date in the order details. When it finishes, the cited source is highlighted. Show [`prompts/draft_reply/v1.md`](../../prompts/draft_reply/v1.md): use only the sources, cite them, and if they don't answer, say so.
5. **The "I don't know" path.** Open **T-1042**, the prompt-injection ticket, whose real question is whether the helmet is MIPS certified. The KB doesn't say. Retrieval still returns four sections, because retrieval always returns something, but the draft says *"I don't have information on whether this helmet is MIPS certified, so a specialist will follow up"* and cites nothing. It also promises no refund.
6. **The customer's language.** Open **T-1044**. The ticket is in Spanish, the KB is in English, and the draft comes back in Spanish with the damaged-shipment policy and its citations.
7. **Production beat: check retrieval first, and check the citations.** When a draft is wrong, look at what was retrieved before blaming the model. Every `[n]` is checked against the sources actually provided; a citation to a source that doesn't exist gets flagged. Retrieved text is untrusted input, like the ticket.

## To Build

- [x] `answer.py` &mdash; chunk by heading, embed, retrieve (by stored ticket vector or by query), stream the draft, check citations
- [x] `prompts/draft_reply/naive.md` and `v1.md`
- [x] `build_kb_index.py` &mdash; chunk and embed; `--show` lists the chunks
- [x] `draft_reply.py` &mdash; stream a draft in the terminal; `--naive` for the no-retrieval contrast
- [x] Draft a reply panel: sources on page load, streamed draft into an editable box, cited sources highlighted
- [ ] Click through the panel in a real browser (the event stream is tested; the page script is not)
- [ ] Slides for this section (the four steps, chunking, the prompt, the "I don't know" path)
- [ ] Record the fallback video

## Run It

From `code/`:

```bash
uv run python demos/03_rag/build_kb_index.py           # once, and after any KB or embedding model change
uv run python demos/03_rag/build_kb_index.py --show

uv run python demos/03_rag/draft_reply.py T-1040 --naive
uv run python demos/03_rag/draft_reply.py T-1040
uv run python demos/03_rag/draft_reply.py T-1042
uv run python demos/03_rag/draft_reply.py T-1044
```

In the app: the **Draft a reply** panel on any ticket page.

## Rehearsal Baseline

`text-embedding-3-small` and prompt `draft_reply/v1`, 2026-10-16.

Top retrieved section per open ticket:

| Ticket | Top section | Distance |
|---|---|---|
| T-1037 clicking wheel | Warranty > Common warranty issues | 0.38 |
| T-1038 battery hot | E-Bike Battery Care and Safety > Safety first | 0.42 |
| T-1039 late Trailhawk | Shipping Times and Costs > Late orders | 0.57 |
| T-1040 helmet too small | Returns Policy > What you can return | 0.46 |
| T-1041 double charge | Refund Timeline > Duplicate charges | 0.47 |
| T-1042 MIPS question | Returns Policy > What you can return (does not answer it) | 0.60 |
| T-1043 several issues | Price Match (misses Locked accounts) | 0.64 |
| T-1044 Spanish, bent wheel | Damaged or Incorrect Shipments > What happens next | 0.47 |

Drafts: T-1040 cites [1] and gives the strap rule, the 45-day window, and a computed deadline, but leaves out the $12 label fee. T-1042 declines and cites nothing. T-1044 answers in Spanish and cites [1] and [2]. T-1039 cites [1] with the trace-then-replace rule. T-1041 cites [1] with the pending-authorization rule. Most drafts took about 3 seconds, with first text after about 2.

## Stage Risks

- **Latency has outliers.** Most drafts finished in 3 to 8 seconds, but one T-1040 run took 53 seconds. Streaming hides the wait only once text starts arriving. If the panel sits on "Drafting…" for more than ten seconds, talk through the retrieved sources, which are already on screen, and switch to the recording if it goes past thirty.
- **Drafts vary, and some are imperfect in useful ways.** The T-1040 draft dropped the $12 label fee; the T-1044 draft tells Lucía to "open a ticket," which she already did; the T-1039 draft asks Tom to "reply to confirm" before opening a trace. Each one is correct to its sources and still not quite what an agent would send. That is the case for a human in the loop, and the setup for the eval in section 7.
- **Multi-issue tickets retrieve badly.** T-1043 asks about a locked account, a price match, and an order change; retrieval finds the price match and misses the locked account. One query vector can't point three ways. Demo it only if you want that point; triage already flags it for review.
- **The KB index uses the embedding model too.** After changing models, rebuild both: `demos/02_semantic_search/build_index.py --rebuild` and `demos/03_rag/build_kb_index.py`. Until then the panel says which model the index was built with.
- **`helpdesk.db --reset` drops the KB index along with everything else.** Use `--reset-demo` between rehearsals.
