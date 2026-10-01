# Research Agent: Multi-Agent Research Briefs over a Long Horizon

A small multi-agent system that takes a research question, gathers and cross-checks sources, and writes a short, cited brief. Separate worker processes split the work into **search**, **extraction**, and **synthesis**. An **orchestrator** plans the work, hands it out, supervises it, and revises the plan.

> **Status:** design phase. This README describes the intended architecture. Run instructions will be finalized once the system is built.

---

## Goal

**Chosen problem:** #2, take a research question, gather and cross-check sources, and write a short brief.

**Demo question:**
> *Was* Spinosaurus aegyptiacus *an aquatic pursuit predator, and how strong is the evidence?*

The topic was picked because the scientific literature genuinely disagrees. One camp argues for swimming and diving, based on the paddle-like tail and dense bones. The other argues for wading like a heron, based on buoyancy, stability, and anatomy. Rebuttals and replies run in both directions, so cross-checking has real conflicts to find without us planting any.

**Corpus:** about 30 open-access papers (PDF) and about 10 web pages (HTML), all local. A manifest pins every source by URL, version, license, and sha256 hash, so every run works from identical bytes. A separate answer key, which agents never see, labels each document's stance so the final brief can be scored.

**Outputs:**
- **Brief:** about 600 to 900 words, one section per sub-question. Every factual sentence cites a source location, and each citation's quote has been checked against the source text. Contested points show both sides and how strong the evidence is for each.
- **Run report:** steps taken, tasks, retries, recovered failures, plan-revision history with reasons, and prompt tokens per call over the run.

---

## Design priorities

The brief asks for depth on a few problems rather than breadth. Effort is split deliberately:

| Tier | Component | Treatment |
|---|---|---|
| **1** | **Orchestration + handoff** | Deep. Task lifecycle, atomic claiming, leases, retry budgets, continuous supervision, and plan revision at phase barriers |
| **1** | **Tool calls + verification** | Deep. Schema-validated outputs with repair, a deterministic quote-grounding gate, and cross-source agreement verdicts |
| **1** | **Subagents** | Solid. One worker runtime with three role configs (prompt, tools, output schema), each running as its own OS process |
| **1** | **Memory** | Solid. Durable state in a shared store, kept across crashes so a run can resume in a later session |
| 2 | Workspace context | Bare bones. A compact snapshot of the run's settled state, rebuilt before each orchestrator decision |
| 2 | Context management | Bare bones. No transcripts, per-role filtered views, hard caps, and paged document reads |
| 3 | Prompt caching | Not implemented. Prompts are laid out so a cache could reuse their stable parts, but the free models don't support provider-side caching |

---

## Architecture

```
                       ┌──────────────────────────── ORCHESTRATOR (process) ───────────────────────────┐
  research question ─► │  Supervisor (every tick, no LLM): leases, retries, failure detection          │
                       │  Planner   (LLM, at barriers only): plan → review/replan → assemble           │
                       └──────────────┬───────────────────────────────────────────▲────────────────────┘
                                      │ creates tasks                              │ reads results
                                      ▼                                            │
          ┌──────────────────────────────────── SHARED STATE STORE (SQLite, WAL) ────────────────────────┐
          │  goal & plan versions │ task queue + leases │ sources & chunks │ claims │ event/trace log      │
          └──────┬──────────────────────────────┬──────────────────────────────────┬────────────────────┘
                 │ claim / complete             │ claim / complete                 │ claim / complete
          ┌──────▼───────┐              ┌───────▼────────┐                 ┌───────▼────────┐
          │ SEARCH  ×1   │              │ EXTRACT  ×2    │                 │ SYNTHESIZE ×1  │
          │ (process)    │              │ (processes)    │                 │ (process)      │
          │ full-text    │              │ paged PDF/HTML │                 │ cross-check,   │
          │ index search │              │ readers        │                 │ write sections │
          └──────────────┘              └────────────────┘                 └────────────────┘
                 ▲
          local corpus (PDF + HTML) ── parsed and indexed once at startup, deterministically
```

**The runtime boundary is real.** The orchestrator and every worker run as separate OS processes. They share no memory and never call each other directly. All coordination goes through one SQLite database file in WAL mode. Handoff means writing a task row, claiming it atomically, and writing back a result row.

### Run lifecycle

The orchestrator calls the LLM only at **phase barriers**. In between, a supervisor loop that never calls the LLM keeps the run healthy.

1. **Plan.** Break the question into sub-questions, each tagged with the hypotheses it tests. The plan is stored as version 1.
2. **Gather.** For each sub-question, search tasks produce candidate sources, and extract tasks read those sources in page windows and produce claims.
3. **Cross-check.** For each sub-question, the synthesizer issues a verdict: *supported*, *contested*, or *thin*.
4. **Review (barrier).** The planner sees the verdicts and may revise the plan. For example, it can add a targeted sub-question for a contested point or more searches for thin coverage. Each revision is a new plan version with a recorded reason. Steps 2 to 4 repeat within a fixed budget.
5. **Write and assemble (barrier).** Write one section per sub-question, then assemble the brief and the run report.

### Agents

| Role | Input | Tools | Output |
|---|---|---|---|
| **Orchestrator** | Goal, settled-state snapshot | Task creation, plan versioning | Plan versions, tasks, final assembly |
| **Search** | Sub-question + hypotheses | Full-text search over the corpus index, document listing | Ranked candidate documents with reasons |
| **Extract** | Sub-question + one document | Paged PDF reader, sectioned HTML reader | Claims, each with a verbatim quote, location, and stance |
| **Synthesize** | Sub-question + its verified claims | None (reasons over stored claims) | Cross-check verdicts and cited sections |

Each worker gets a fresh context for every task. No agent keeps a running transcript.

---

## Orchestration, handoff, and recovery (Tier 1)

- **Task envelopes** carry the immutable goal, the sub-question id, the plan version, and the task inputs. Every piece of work can be traced back to the question it serves.
- **Atomic claiming:** a worker claims the next pending task for its role in a single conditional update. Two workers can never take the same task.
- **Leases:** a claimed task carries an expiry time. If a worker crashes or stalls, the lease runs out, the supervisor puts the task back in the queue, and another worker picks it up.
- **Retry budgets:** each task gets a limited number of attempts. A task that keeps failing is marked failed. The planner sees this at the next barrier and works around it.
- **Single writer of the plan:** only the orchestrator creates tasks or changes the plan. Workers only claim tasks and report results. This prevents sub-agents from working at cross purposes.
- **Deduplication:** repeated searches are collapsed by hashing the query.

## Tool calls and verification (Tier 1)

- **Schema validation:** every LLM output and tool result is checked against a typed schema. Malformed output gets one repair attempt with the validation error fed back, then counts as a task failure.
- **Quote-grounding gate:** a claim is stored as *verified* only if its quote actually appears in the cited chunk of the source. The check uses deterministic string and fuzzy matching, with no LLM involved. It runs in the storage layer, so no worker can skip it. Fabricated citations are rejected.
- **Cross-source agreement:** for each sub-question, verified claims are grouped by hypothesis and source. The verdict (*supported*, *contested*, or *thin*) combines independent-source counts with the synthesizer's judgment, and it drives replanning.

## Memory and context (Tiers 1 and 2)

- **Memory** is the shared store: goal, plan versions, tasks, sources, claims, and an append-only event and trace log. It survives crashes. Rerunning against an existing run resumes from the last state saved in the store.
- **Context is computed, not accumulated.** Before each LLM call, a role-specific view is rendered from the store, covering only settled facts in a fixed order. As a result, prompt size stays flat across the run. Prompt tokens are logged on every call so this can be checked.
- **Prompt layout**, from most stable to least: role system prompt → sub-agent and tool definitions → memory view → task/query.

---

## Reproducibility

- **Pinned corpus:** a manifest plus a fetch script with hash checks.
- **Replay mode:** every live LLM call is recorded in the trace, keyed by a hash of the request content. In replay mode the LLM client returns those recorded responses with no network or API keys. Responses are matched by content rather than step order, so this works even though the multi-process schedule changes between runs.
- **Chaos mode:** seeded fault injection (tool errors, malformed LLM output, a worker killed mid-task) shows recovery. The fixed seed means a replay fails the same way every time.

## Stack

- Python, LangChain
- LLM providers, selected by an environment variable: **NVIDIA NIM** (default, free tier), **OpenRouter**, or **replay**
- SQLite (standard library) for the shared store and its full-text search index
- PDF and HTML text extraction for the paged readers

## How to run

*Coming soon. The planned shape is:*

1. Fetch and verify the corpus.
2. Run the demo question live with a NIM key, or replay it from the committed trace with no keys.
3. Optionally enable chaos mode, or kill a run and restart it to watch it resume.

See **NOTES** (to be written) for what we went deep on and why, the decisions we're most confident about, what was cut, and how coding tools were used.
