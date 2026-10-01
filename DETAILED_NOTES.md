# Detailed notes

The design decisions, mechanisms, and parameters behind [NOTES.md](NOTES.md), plus what the first live run showed. Code references are file and function names.

1. [Design priorities](#1-design-priorities)
2. [How each failure mode is handled](#2-how-each-failure-mode-is-handled)
3. [Orchestration, handoff, and recovery](#3-orchestration-handoff-and-recovery)
4. [Tool calls and verification](#4-tool-calls-and-verification)
5. [Memory and context](#5-memory-and-context)
6. [Other decisions and the alternatives we rejected](#6-other-decisions-and-the-alternatives-we-rejected)
7. [Where LangChain is used, and where it isn't](#7-where-langchain-is-used-and-where-it-isnt)
8. [Model selection](#8-model-selection)
9. [First live run](#9-first-live-run)

## 1. Design priorities

The brief rewards a few hard problems solved with conviction over broad coverage. We split effort across seven components (six from Raschka's mini coding agent, plus orchestration and handoff) into three tiers:

| Tier | Component | Treatment |
|---|---|---|
| **1** | **Orchestration + handoff** | Deep. Task lifecycle, atomic claiming, leases, retry budgets, continuous supervision, plan revision at phase barriers |
| **1** | **Tool calls + verification** | Deep. Schema-validated outputs with repair, a deterministic quote-grounding gate, cross-source verdicts |
| **1** | **Subagents** | Solid. One worker runtime with three role configs (prompt, tools, output schema), each its own OS process |
| **1** | **Memory** | Solid. Durable state in a shared store; a run resumes after a crash or in a later session |
| 2 | Workspace context | Bare bones. A compact snapshot of the run's settled state, rebuilt before each planner decision |
| 2 | Context management | Bare bones. No transcripts, per-role views, hard caps, paged document reads |
| 3 | Prompt caching | Not implemented. Prompts are ordered stable-first so a cache could reuse them, but the free models offer no provider-side caching |

Subagents and memory are tier 1 but cheap: they fall out of the same SQLite store and worker loop that orchestration needs. "Memory" means durable within a run and resumable across sessions; reuse across different questions was cut.

## 2. How each failure mode is handled

| Failure mode (from the brief) | Mechanism |
|---|---|
| **Context corrupts** | No agent accumulates a transcript. Every prompt is rebuilt from the store (`agent/memory.py`), so step 200 has the same prompt shape as step 5. Prompt tokens are logged per call to check this. |
| **Plans drift** | The goal is fixed at plan time and every task carries its sub-question and plan version. The planner runs only at barriers and returns typed actions that code validates. Every revision is a new plan version with its reasons. |
| **Tools fail quietly** | Every LLM output and tool call is typed and validated. Every quote is checked against the source text. Every LLM and tool call is recorded by a LangChain callback. Rejected claims are kept and counted. |
| **Sub-agents work at cross purposes** | Only the orchestrator creates tasks or changes the plan. Workers claim tasks atomically and can only complete tasks they still own. A `UNIQUE dedupe_key` blocks duplicate work. |
| **Silent stalls and crashes** | Leases with heartbeats, a hard task timeout, a dead-worker check, and classified retries. |
| **Weak or conflicting evidence** | Verdicts (*supported* / *contested* / *thin*) drive the review: more searches, a targeted sub-question, or done. |

## 3. Orchestration, handoff, and recovery

### The runtime boundary

`run.py` starts five OS processes by running itself again with a subcommand: `orchestrator`, `worker --role search`, two `worker --role extract`, and `worker --role synthesize`. They share nothing in memory. All coordination goes through `runs/<run_id>/state.db` (SQLite, WAL mode, busy timeout). The read-only `corpus.db` is attached to it so claims can be joined to source text in one query.

### Task lifecycle (`agent/store.py`)

```
pending ──claim──► claimed ──complete──► done
   ▲                  │ fail (retries left) / lease expired / hard timeout
   └──── backoff ─────┘ fail (no retries left) ──► failed
pending / claimed ──► cancelled   (sub-question retired, or the run degraded)
```

- **Claim:** one atomic `UPDATE … WHERE task_id = (SELECT … LIMIT 1) RETURNING *`, which takes the oldest eligible pending task for the worker's role.
- **Heartbeat:** a background thread in the worker renews the lease every 10 s on its own connection. A slow LLM call keeps its task; a dead worker's lease lapses after 45 s.
- **Fenced completion (`complete_task`):** the result and all its side effects (candidates, claims, sections) commit in one transaction, and only if the task is still `claimed` by this worker. A worker that was presumed dead and comes back has its late result discarded (`stale_result_discarded`) instead of duplicating claims.
- **Reap (supervisor):** expired leases, and tasks past the 300 s hard timeout, go back to `pending` under the transient retry policy. Workers silent for longer than a lease are marked dead, and the tasks they held are logged.
- **Resume:** `prepare_resume` marks the previous session's workers dead and expires their leases, so their tasks are re-queued on the first tick.

### Retries

| Error class | Examples | Policy |
|---|---|---|
| Transient | HTTP 429/5xx, timeouts, connection errors, lost worker, injected tool fault | 3 retries (4 attempts), backoff 2 s, 8 s, 30 s |
| Deterministic | Output still invalid after repair, unknown document, unexpected exception | 1 retry (2 attempts) |

`ChatNVIDIA` has no built-in retries (we read the client source), and nothing else retries silently. A shared token bucket in `state.db` (30 requests/min, burst 4) keeps the processes under the provider limit together. LangChain's `InMemoryRateLimiter` only works within one process.

### Barriers and the planner (`agent/orchestrator_agent.py`)

| Phase | Barrier (what moves it on) | LLM |
|---|---|---|
| plan | planner call succeeds or falls back | `planner.plan` |
| gather | no open search or extract tasks, and every candidate has its extract task | none |
| crosscheck | no open cross-check tasks; verdicts applied (a failed cross-check falls back to the deterministic verdict) | none |
| review | planner actions applied; new work goes back to gather, otherwise on to write | `planner.review` |
| write | no open write tasks | none |
| assemble | brief and report rendered | `planner.assemble` |

Review actions and the limits code enforces on them:
- `more_search(subq_id, text)` — only for an active sub-question.
- `add_subquestion(text)` — at most 2 per round and 8 in total; duplicates are rejected.
- `retire(subq_id)` — never the last active sub-question.
- `complete`.

There are at most 2 review rounds. Rejected actions are logged. A plan version is written only when something changed.

### Graceful degradation

**Triggers:**
- a budget limit: 120 tasks, 250 LLM calls, or 30 min of wall-clock time per session;
- a 10-minute timeout in the gather or cross-check phase.

**What happens:** open research tasks are cancelled and the run goes straight to writing. A write-phase timeout goes straight to assembly instead.

**Fallbacks:**

| Failure | Fallback |
|---|---|
| Planning fails | A one-question plan |
| Review fails | Move on to writing |
| Assembly fails | A deterministic summary of the verdicts |
| A section writer fails | The verified claims are listed instead |
| A sub-question has no verified evidence | It gets the cause, e.g. "Insufficient evidence: no quote-verified claims (4 documents selected by search; 3 of 4 extract tasks failed or were cancelled; 2 claims rejected by quote verification)." |

### Parameters and why

| Parameter | Value | Reasoning |
|---|---|---|
| Initial / max sub-questions | 4–6 / 8 (≤ +2 per review) | The debate has about 5 lines of evidence; the plan can grow without exploding |
| Review rounds | ≤ 2 | Round 1 fixes thin or contested sub-questions; round 2 confirms |
| Search | ≤ 3 queries, top 4 documents | Covers both sides without reading the whole corpus |
| Extract | ≤ 2 windows of 3 pages around the search hits, ≤ 5,000 characters per page, ≤ 6 claims | Papers run 15–30 pages; read where the evidence is |
| Heartbeat / lease / hard timeout | 10 s / 45 s / 300 s | Separates slow from dead; catches a live but stuck worker |
| LLM call timeout | 90 s | Turns a hung call into a transient error (see §9: write calls needed more) |
| Supervisor tick / worker poll | 1 s / 0.5–2 s | Coordination is never the bottleneck |

The planning estimate was about 100 LLM calls and 100 tool calls per run. The first run used 53 and 58, and was limited by latency rather than the rate limit.

## 4. Tool calls and verification

### Structured calls (`tools.call_structured`)

Each LLM call:
1. takes a rate-limit token;
2. calls `model.bind_tools([Schema], tool_choice=Schema)`, which forces a tool call;
3. validates the tool arguments with Pydantic, plus a semantic check per step:
   - the stance must be one of the plan's hypotheses;
   - picked documents must be among the offered candidates;
   - every `[C#]` citation must be a verified claim of this sub-question; grouped markers like `[C4, C5]` are split first.

An invalid output gets one repair message with the validation error. If it is still invalid, the call raises `DeterministicError`. Provider exceptions are classified as transient or deterministic from their text.

### Tools (`agent/tools.py`)

| Tool | Agent | What it does |
|---|---|---|
| `fts_search(query, k)` | search | BM25 (FTS5, porter stemming) over passages, excluding reference lists |
| `list_docs(source_kind)` | search | Document metadata. Defined and shown to the search agent, but the fixed search pipeline does not call it |
| `read_pdf(doc_id, start, end)` / `read_html(...)` | extract | At most 3 pages or sections per call, reference lists omitted, characters capped |

Tools are LangChain `@tool`s with an injected `ctx` argument that the model never sees. The fixed pipelines call `tool.invoke()`, which still validates arguments and fires the trace callback.

**How the pipelines use them:**
- **search:** the LLM writes queries → `fts_search` (code) → hits are aggregated by document → the LLM picks up to 4.
- **extract:** windows are chosen around the most-hit pages → `read_*` → the LLM extracts claims.

**Ingest** (`store.build_corpus`):
- PDFs are split per page into chunks of at most 1,200 characters.
- Reference sections are detected and flagged: by heading (after 30% of the document) or by the density of years in the text. Flagged chunks are kept for verification but excluded from search.
- HTML is split into sections at h1–h3 headings; "References", "See also" and similar sections are dropped.
- Each document's `source_kind` comes from its manifest entry if set, otherwise from its type (PDF → primary, HTML → secondary).

### Quote-grounding gate (`verify_quote`, `record_claim`)

`record_claim` is the only code that writes the `claims` table. For each claim:
1. **Normalize:** NFKC (ligatures), quotes and dashes, de-hyphenation, lowercase, whitespace.
2. **Reject outright** quotes under 8 words, quotes containing an ellipsis (not contiguous), and locations that don't exist.
3. **Exact match** on the cited page.
4. **Otherwise, fuzzy match** (`rapidfuzz.partial_ratio` ≥ 90) on the cited page. This tolerates PDF extraction noise; a paraphrase scores well below 90.
5. **Otherwise, the neighbouring pages** (±1), correcting the cited location. Pages shorter than the quote are skipped.
6. **Otherwise rejected.** The rejected claim is stored with its best score and the reason.

### Verdicts (`compute_verdict`, `final_verdict`)

- **contested:** verified claims for two or more different hypotheses. This outranks *supported*.
- **supported:** 2 or more independent primary sources and no verified opposing claim. Independence means distinct first-author surnames. Web pages never count, because they usually report on a paper already in the corpus.
- **thin:** otherwise.

The cross-check LLM writes the rationale and may only move the verdict toward caution (supported → contested → thin). Attempted upgrades are ignored and logged.

## 5. Memory and context

**Two databases:**
- `corpus.db`: `documents`, `chunks`, and `chunks_fts`. Built once and read-only afterwards.
- `state.db`, one per run:

| Table | Holds |
|---|---|
| `run` | goal, phase, review round, the frozen config, any degrade reason |
| `plan_versions` | the plan history, append-only |
| `subquestions` | status, verdict and rationale per sub-question |
| `tasks` | the queue |
| `candidates` | search output |
| `claims` | verified and rejected claims |
| `sections` | written sections |
| `llm_calls` | prompts, responses, tokens, latency |
| `events` | the append-only step log |
| `workers` | the process registry |
| `rate_limiter` | the shared token bucket |

**Who writes what:**
- the orchestrator: the run, the plan, sub-questions, and new tasks;
- workers: only tasks they own, plus candidates, claims and sections;
- every process: appends to `events` and `llm_calls`.

**Prompt layout** (`agent/system_prompt.py`): the system message holds the role prompt and the tool definitions; the human message holds the memory view and the task. The stable parts come first.

**Memory views** (`agent/memory.py`) contain only settled facts, in a fixed order, with caps and "… N more" markers:

| Step | View |
|---|---|
| plan | the corpus listing (titles, capped at 150) |
| review | per sub-question: verdict, evidence by stance, rejected claims, failed tasks, documents read, budget used |
| search | hypotheses and documents already chosen for this sub-question |
| extract | hypotheses and document metadata |
| cross-check / write | hypotheses, the verdict floor, and up to 15 verified claims |

## 6. Other decisions and the alternatives we rejected

| Decision | Alternative | Why we chose it |
|---|---|---|
| OS processes + a SQLite queue | HTTP services; one LangGraph process | A real runtime boundary with no infrastructure. Services cost setup time; one process doesn't meet the brief |
| Fixed pipelines | ReAct tool-calling workers | Predictable call counts and budgets on slow free models, which can loop |
| Planner only at barriers | Replanning as results arrive | No drift, and every revision is recorded with its reason. Cost: a weak sub-question waits for the next barrier |
| LangChain only | LangGraph checkpointer or Store | Checkpoints resume a *graph*; they don't reassign work from a dead *worker*, and they'd be a second source of truth for the phase. The Store fits cross-run memory, which we cut |
| Playback replay | Re-executing against recorded responses | Playback is simple and needs no model. Re-execution, the stronger reproducibility proof, is next |
| One model per agent, set in its own file | A shared model factory | Each agent's model is visible where it's used |
| Two database files | One database with a `run_id` column on every table | Different lifecycles: the corpus is read-only and shared, state is per run and written by many processes |
| Local pinned corpus | Live web search | Reproducible, with real conflicts in the literature to find. Cost: no discovery beyond the corpus (see NOTES) |
| Domain-neutral code; the question and corpus are the only inputs | Topic-specific prompts or rules | The same system runs on any question with any PDF/HTML corpus. The prompts' examples are deliberately neutral |

## 7. Where LangChain is used, and where it isn't

**Used:**

| Feature | Purpose |
|---|---|
| `ChatNVIDIA` (`temperature=0`, per-agent `max_completion_tokens`, `timeout=90`) | One per agent |
| `bind_tools(..., tool_choice=...)` | Forced, structured output |
| `@tool` with `InjectedToolArg` | Tool schemas without the process context |
| `Document` | Return type of the tools |
| `ChatPromptTemplate` | The prompt layout |
| `BaseCallbackHandler` (`TraceHandler`) | Writes every LLM call to `llm_calls` and every tool call to `events`, attributed through `RunnableConfig` metadata, with no logging code in the agents |

**Not used:**
- **LangGraph** — see §6.
- **`InMemoryRateLimiter`** — it only limits within one process.
- **Agent executors / `create_agent`** — we chose fixed pipelines.
- **LangChain memory classes** — they assume a single conversation's history, which is the opposite of our no-transcript design.

## 8. Model selection

We probed NIM with a forced tool call that extracts a claim with a verbatim quote:

| Model | Result |
|---|---|
| meta/llama-3.3-70b-instruct (the original plan) | Retired on 2026-08-26 (HTTP 410) |
| **google/gemma-4-31b-it** | **4/4 valid, quotes verbatim, 7–28 s, about 220 tokens**: chosen |
| nvidia/nemotron-3-super-120b-a12b | 3/4 valid. One empty response: as a reasoning model it used up its token budget thinking |
| deepseek-v4.1-flash, glm-5.3-flash, gpt-oss-20b | Valid but 41–55 s per call |
| nemotron-3.5-lightning | Timed out at 120 s |
| kimi-k2.6, mistral-large-2, nemotron-70b, nemotron-nano | Listed, but returned 404 for this account |

## 9. First live run

**Run `run_20261001_151557`**, demo question, default settings.

| | |
|---|---|
| Duration | 23.9 min |
| Steps | 111 (53 LLM calls + 58 tool calls) |
| Tasks | 40, all completed: 6 search, 23 extract, 6 cross-check, 5 write |
| Plan | v1: 3 hypotheses, 5 sub-questions. Review 1 → v2 (`more_search` on SQ4). Review 2 → `complete` |
| Verdicts | Tail and hindlimbs, bone density, jaw/teeth/brain, buoyancy: contested. Isotopes: supported |
| Claims | 127: 113 exact, 11 fuzzy, 3 rejected (2%) |
| Recovery | 3 NIM read timeouts during writing, each retried and recovered |

**Prompt size per step type, first half of the run vs second half (tokens):**

| Step | First half | Second half |
|---|---|---|
| search queries | 439 | 453 |
| search ranking | 1,525 | 1,626 |
| extract | 4,741 | 4,973 |
| cross-check | 1,899 | 1,978 |
| write | 1,996 | 1,924 |

**What the 3 rejections were:**
- A real sentence stitched to an invented one (Hone & Holtz, score 89).
- A quote that crossed a page break and picked up the running header (Gimsa, score 81).
- A quote shortened with an ellipsis (Sereno).

The first was a correct catch; the second exposes a limitation.

**Fixed after the run:**
- Grouped citation markers (`[C4, C5]`) got past validation and appeared raw in the brief.
- Claim ids appeared in verdict rationales.
- A `more_search` with empty focus text didn't fall back to the planner's reason.
- The writer added a duplicate verdict line.
- The context chart mixed step types.
- Launcher output:
  - the status line printed every 5 s whether or not anything changed;
  - retries weren't shown on screen;
  - output was buffered when piped;
  - there was no live log view (now `--verbose`).

**Open issues:**
- **Stance attribution.**
  - Critique papers restate the views they rebut, and those restatements got tagged with the rebutted view. For example, Myhrvold 2024's *"they concluded that spinosaurids were 'aquatic specialists'"* was tagged `aquatic_pursuit`.
  - As a result, 4 of 5 verdict floors came out contested, and review round 1 was spent fixing one of them.
- **The verdict scale mixes two questions.**
  - For the jaw/teeth/brain sub-question, the LLM correctly saw the "contest" as a tagging artefact. The honest correction would have been *supported*, but upgrades aren't allowed, so it downgraded to *thin*.
  - Strength and agreement should be separate fields.
- **Throughput.**
  - Extract tasks queued for up to 4 minutes while we used about 3.5 of our 30 calls a minute.
  - The single synthesize worker made writing sequential.
  - 3 of 8 write calls hit the 90 s timeout.
- **The planner made up an acronym expansion:** "pFDA (posterior femoral density)" instead of phylogenetic flexible discriminant analysis. It was harmless for search, but it stayed in the sub-question text.
- **Parsing:** one PDF (`vullo2016_app`) yielded only 2 pages of text.
- **Replay cosmetics:**
  - LLM lines are stamped with when the call started;
  - timed-out calls print `None->None tokens`.
