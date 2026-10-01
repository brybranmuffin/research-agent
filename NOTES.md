# Notes

Details behind every point are in [DETAILED_NOTES.md](DETAILED_NOTES.md).

## What we went deep on, and why

The brief names four ways long runs collapse: context corrupts, plans drift, tools fail quietly, and sub-agents work at cross purposes. We went deep on the two areas that address all four. The single-agent loop is deliberately plain.

1. **Orchestration, handoff, and recovery.**
   - Five OS processes coordinate only through a SQLite task queue: atomic claims, leases with heartbeats, fenced completion (a late result from a worker presumed dead is discarded), and retries classified as transient or deterministic.
   - The LLM planner runs only at phase barriers and returns typed actions that code validates. The plan changes in a few recorded steps instead of drifting with every worker result.
   - Budgets and timeouts degrade gracefully: the run always produces a brief and says what is missing and why.
2. **Tool calls and verification.**
   - Every LLM output is a forced, schema-validated tool call with one repair attempt.
   - Every claim's quote is checked against the source text by deterministic matching before it can be stored.
   - Verdicts come from counting verified, independent sources. The LLM can only downgrade them.

Context management came almost for free: no agent keeps a transcript, and every prompt is rebuilt from the store.

**First live run** (24 min, 111 steps):
- 3 provider timeouts were retried and recovered.
- 2% of quotes were rejected, including a real sentence stitched to an invented one.
- Prompt size per step type stayed flat (within about 5%) from start to finish.

## Decisions we're most confident about

1. **SQLite as the coordination layer.** Real multi-process handoff, atomic claims, crash-safe resume, and a full audit trail, with no infrastructure.
2. **The planner speaks in typed actions; code applies them.** The LLM never writes state, and every action is checked against limits.
3. **Deterministic verification; the LLM can only downgrade.** Citations are checked against the source text, not by another model.
4. **Fixed pipelines instead of ReAct workers.** Call counts are predictable, so budgets and the free-tier limits hold, and weaker models can't loop.
5. **No hidden retries.** The task queue owns every retry, so the run report's numbers are the real ones.

## What we cut, and what we'd build next

Three cuts matter most for the general problem of keeping agents coherent over a long horizon:

1. **Long-term memory across runs.**
   - *Today:* memory is durable *within* a run. It survives crashes and resumes, but every new question starts from nothing.
   - *Next:* persist verified claims and extractions keyed by (document, page window) and reuse them across runs and questions, so research accumulates instead of repeating. A LangGraph Store would fit.
2. **Context compaction.**
   - *Today:* not needed at this horizon. Every prompt is rebuilt from the store with hard caps, so prompt size stays flat.
   - *Next:* longer runs or larger corpora would hit those caps and silently drop evidence. Compaction would replace them: rolling summaries of the evidence per sub-question, and compressed plan history.
3. **A downloaded corpus instead of a web-search agent.**
   - *Today:* a pinned local corpus makes runs reproducible and keeps the debate's real conflicts inside a known set of sources. The cost is discovery: the system can't find anything outside its 34 documents.
   - *Next:* a fetch worker that searches the web, downloads and hashes new sources, and ingests them into the same index, so verification and verdicts apply unchanged.

**Functional improvements** (found in the first live run; details in DETAILED_NOTES §9):
- **Stance attribution:** critique papers' restatements of the views they rebut were tagged with the rebutted view, which inflated "contested". Next: an own/reported field per claim.
- **Two-axis verdicts:** separate "how much evidence" from "do the sources agree".
- **Throughput:** more extract and synthesize workers, and a longer LLM timeout. Latency, not the rate limit, was the bottleneck.
- **Quotes across page breaks:** match against adjacent pages joined, and strip running headers.
- **Also:** re-run replay against recorded LLM responses, respawning dead workers, automatic scoring against the answer key, a layout-aware PDF parser, and prompt caching.

## How we used coding tools

- **Claude Code as a design partner before any code:** the architecture, LangChain reference designs, and every number (leases, retries, budgets, verification thresholds).
- **A separate Claude Code session built the corpus** to a written contract: manifest schema, pinned versions, hashes, licences, and an answer key agents never read.
- **Probing before building.** The planned NIM model had been retired, so we benchmarked tool calling across the available models and picked Gemma 4 31B. We also read the `ChatNVIDIA` source to confirm it has no hidden retries.
- **Tests for the guarantees, then live runs.** Queue and verification tests existed before any agent code; there are 58 tests now, with no network. Live runs were debugged from `state.db` and the per-process logs, which surfaced issues such as unvalidated grouped citations.
