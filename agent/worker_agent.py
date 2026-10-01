"""Worker agent: one generic runtime hosting the three subagents (search, extract, synthesize).

Component: Subagents (Tier 1). Entered via `run.py worker --role <role>`.

Loop: register in the workers table -> poll for a claimable task of this role (idle backoff
0.5-2s) -> claim -> heartbeat every 10s -> run the role's fixed pipeline with a fresh
context -> complete or fail with a classified error (transient | deterministic) -> repeat
until the run is done/degraded. No transcript is carried between tasks.

Subagents are config objects: ROLES = {name: (system prompt, tools, output schema, pipeline)}.
Fixed pipelines: code calls the tools; the LLM is called once or twice per task.
- search:     LLM writes <=3 queries -> fts_search -> LLM ranks -> top 4 candidates with hit pages
- extract:    read <=2 page windows centered on the hits -> LLM extracts claims (verbatim quote,
              location, stance) -> verification gate on write
- synthesize: cross_check: deterministic verdict + LLM rationale (may only downgrade)
              write_section: cited markdown; every cited claim must be verified and in scope

Models: one per subagent, declared here: SEARCH_MODEL, EXTRACT_MODEL, SYNTHESIZE_MODEL =
  ChatNVIDIA(model from .env NIM_MODEL_<ROLE>, temperature=0, max_retries=0).
  Built-in retries are off: the task queue owns retries, so every one is counted.

LangChain: each pipeline LLM step is
  ChatPromptTemplate | <ROLE>_MODEL.with_structured_output(PydanticModel, include_raw=True)
  include_raw exposes parsing errors for the single repair attempt.
  The Pydantic output models (SearchResult, ExtractResult, CrossCheckResult, Section) live here.
  RunnableConfig metadata {run_id, task_id, role} is passed on every invoke, so the
  store.py callback handler can attribute each LLM/tool call to its task.
"""
