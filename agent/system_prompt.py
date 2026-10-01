"""System prompts for every agent role, kept in one place.

Component: Prompt layout (Tier 3: cache-ready ordering, no cache implemented).

Roles: planner (brief, plan, review, assemble), search, extract, cross_check, write_section.

Every prompt is assembled from the most stable part to the most volatile:
  1. role system prompt          (this file; constant per role)
  2. tool definitions            (from tools.py schemas; constant per role)
  3. memory view                 (memory.py; changes per task)
  4. task / query                (the task envelope)
Rendering is deterministic: identical state produces an identical prompt (repeatable runs,
and a prerequisite for re-execution replay later).

LangChain: ChatPromptTemplate.from_messages([("system", ...), ("human", "{memory}\n\n{task}")])
per role; agents compose it into their LCEL chains.
"""
