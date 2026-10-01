"""The two databases: corpus.db (read-only sources) and state.db (per-run shared state).

Component: Memory + the handoff medium (Tier 1). Processes share nothing in memory;
all coordination goes through these files.

corpus.db: built once by ingest, shared by all runs
  documents   doc_id, type, title, authors, year, venue, license, sha256, n_units
  chunks      chunk_id, doc_id, unit (page / heading path), ord, text, norm_text
  chunks_fts  FTS5 index over chunks.text
  Ingest: manifest + corpus/raw -> sha256 check -> parse -> chunk -> index. Deterministic;
  skips "unavailable" entries; never reads eval/corpus_labels.json.

state.db: one per run; WAL mode, busy_timeout, corpus.db ATTACHed read-only
  run            question, goal, phase, review_round, frozen config, degrade_reason
  plan_versions  append-only plan history (reason, hypotheses)
  subquestions   current plan with status / verdict; retired ones kept
  tasks          the handoff queue (UNIQUE dedupe_key)
  candidates     search output -> source for extract tasks
  claims         evidence; rejected claims kept (made-up-citation metric)
  sections       written output with cited claim ids
  llm_calls      trace (prompts, outputs) + token metrics; source for playback replay
  events         append-only step log
  workers        process registry (pid, role, last_seen, status)
  rate_limiter   single-row token bucket shared by all processes (30 req/min)

Task queue: pending -> claimed -> done | failed; expired lease or retry -> pending;
retired sub-question -> cancelled. Claiming is one atomic conditional UPDATE ... RETURNING.

Write ownership: orchestrator -> run, plan, sub-questions, task inserts;
workers -> only tasks they own, candidates, claims, sections; everyone -> events, llm_calls.
Claims are written through tools.verify_quote (the verification gate).

Shared types: TaskEnvelope (goal, subq_id, plan_version, kind, inputs), as a Pydantic model.

LangChain: a BaseCallbackHandler (on_llm_end, on_tool_start/end, on_*_error) that writes
llm_calls rows (tokens, latency, outcome) and tool_call events, attributed via the
RunnableConfig metadata {run_id, task_id, role}. Every chain is invoked with it attached.
Not used: InMemoryRateLimiter (per-process only; our 4-5 processes share one key, so the
token bucket lives here in SQLite).
"""
