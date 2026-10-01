"""Orchestrator agent: supervisor (no LLM) + planner (LLM at phase barriers).

Component: Orchestration + handoff (Tier 1).
The single writer of the plan: only the orchestrator creates tasks or changes the plan.
Entered via `run.py orchestrator`; resumes from the stored phase if state.db has one.

Main loop (tick every 1s): supervisor tick -> if the phase barrier is reached, the planner
acts and advances the phase -> check budgets (graceful degradation if exceeded).
Phases: plan -> gather -> crosscheck -> review -> (gather again | write) -> done | degraded

Supervisor (every tick, never calls the LLM):
- reap expired leases -> re-queue; enforce the 300s hard task timeout
- mark workers dead when they stop heartbeating; log reassigned tasks
- turn search results into extract tasks; finished gathering into cross_check tasks
- detect barriers (all phase tasks finished, or 10 min phase timeout); track budgets

Planner (LLM at barriers only; outputs typed actions that this code carries out):
- plan:     question -> research brief (goal + acceptance criteria) -> 4-6 sub-questions
            tagged with hypotheses (plan v1)
- review:   verdicts -> actions: add_subquestion | more_search | retire | complete
            (<=2 rounds, <=2 new sub-questions/round, 8 total); each revision is a new
            plan version with a reason
- assemble: sections -> final brief; sub-questions without evidence get an explicit
            "insufficient evidence" note. Writes brief.md and run_report.md.

Model: PLANNER_MODEL = ChatNVIDIA(model from .env NIM_MODEL_PLANNER, temperature=0,
  max_retries=0). Built-in retries are off: the task queue owns retries, so every one is counted.

LangChain: each planner step is an LCEL chain
  ChatPromptTemplate (system_prompt.py + memory.py view) | PLANNER_MODEL.with_structured_output(PydanticModel)
  The Pydantic output models (Brief, Plan, ReviewDecision) live in this file.
"""
