# Research Agent: Multi-Agent Research Briefs over a Long Horizon

A small multi-agent system that takes a research question, gathers and cross-checks sources, and writes a short, cited brief. Separate worker processes split the work into **search**, **extraction**, and **synthesis**. An **orchestrator** plans the work, hands it out, supervises it, and revises the plan.

> **Status:** working end to end. See [How to run](#how-to-run).

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

### Orchestration parameters

These numbers come from four constraints:
- **Rate limit.** All processes share one NIM key, which allows roughly 40 requests a minute. This limits throughput, not the worker count.
- **Corpus size.** About 40 documents.
- **Horizon target.** About 150 to 250 logged steps per run.
- **Latency.** Free models take 10 to 40 seconds per call, with occasional queueing over 60 seconds.

**Plan shape**

| Parameter | Value | Reasoning |
|---|---|---|
| Initial sub-questions | 4–6 (hard cap 6) | The debate has about 5 lines of evidence: tail, bone density, buoyancy and proportions, skull and neck feeding, paleoenvironment |
| Max sub-questions after replanning | 8 (at most +2 per review round) | The plan can grow without exploding |
| Review rounds | max 2 | Round 1 addresses contested or thin sub-questions, and round 2 confirms. More rounds cost calls for little gain |

**Work per sub-question**

| Parameter | Value | Reasoning |
|---|---|---|
| Search | 1 task per sub-question, ≤3 queries, top 4 documents | Covers both sides of the debate without reading the whole corpus |
| Extract | 1 task per (sub-question, document), ≤2 page windows of about 3 pages each, centered on the pages where search found hits | Papers run 15 to 30 pages, so we read where the evidence is |

**Timing and failure handling**

| Parameter | Value | Reasoning |
|---|---|---|
| Heartbeat / lease | heartbeat every 10s, lease 45s | Tells a *slow* worker apart from a *dead* one. A dead worker is detected within about 45s |
| Hard task timeout | 300s | Catches a worker that is alive but stuck in a loop, which heartbeats alone would miss |
| Retries, transient errors (429, timeout, network, lost worker) | 3 retries (4 attempts), backoff of 2, 8 and 30 seconds | Transient errors deserve patience |
| Retries, deterministic errors (schema still invalid after repair, unreadable document) | 1 retry (2 attempts) | Retrying the same failure again and again wastes rate limit |
| Schema repair | 1 per LLM call | One repair fixes most JSON slips |
| Shared rate limiter | 30 requests/min, a token bucket in the shared store | Leaves headroom under the provider's limit and keeps workers from triggering 429 errors and using up their retries |
| Supervisor tick / worker poll | 1s / 0.5–2s with idle backoff | Coordination never becomes the bottleneck |

**Barriers and budgets**

| Parameter | Value | Reasoning |
|---|---|---|
| Barrier rule | A phase is complete when all its tasks have finished, successfully or not, or after a 10-minute phase timeout | One stuck sub-question can't hold up the whole run |
| Global budgets | 120 tasks, 250 LLM calls, 30 minutes of wall-clock time | A safety stop. Hitting a limit triggers graceful degradation (see below) |

**Expected size of a run:** about 100 LLM calls plus about 100 tool calls, for about 200 steps in roughly 5 to 10 minutes at 30 requests a minute.

### Graceful degradation

The run always produces a brief. It never fails silently or without output. Graceful degradation kicks in when any of these happens:
- a budget limit is reached,
- a phase times out,
- a sub-question ends up with no verified evidence.

When that happens, the orchestrator skips straight to writing with everything verified so far. Each sub-question that lacks evidence gets an explicit *insufficient evidence* note giving the cause, for example "3 of 4 extract tasks failed". The run report records which trigger fired and when.

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
- **Replay (playback):** every run records its full trace (task events, tool calls, LLM prompts and outputs) in its state store. Replaying a run prints that trace in order, as if the run were happening, with no models, network, or API keys. Playback shows *what happened*. Re-executing the system against recorded LLM responses is planned next (see NOTES).
- **Chaos mode:** seeded fault injection (tool errors, malformed LLM output, a worker killed mid-task) shows recovery. The fixed seed makes the faults repeatable.

## Stack

- Python, LangChain
- **NVIDIA NIM** (free tier) via LangChain; each agent declares its own model
- SQLite (standard library) for the shared store and its full-text search index
- PDF and HTML text extraction for the paged readers

## How to run

Requires Python 3.11+ and a free NVIDIA NIM key from [build.nvidia.com](https://build.nvidia.com).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                     # then set NVIDIA_API_KEY

python corpus/fetch_corpus.py            # download the 34 open-access sources, sha256-verified (~125 MB)
python run.py ingest                     # parse PDFs/HTML -> corpus.db (chunks + FTS5 index), ~20 s

python run.py                            # run the demo question (about 10-15 min on the free tier)
python run.py "your question" --chaos    # seeded faults: tool errors, malformed LLM output, a killed worker
python run.py --resume <run_id>          # Ctrl-C a run, then continue it from state.db
python run.py replay <run_id>            # play a recorded run back from its trace (no keys, no network)

pytest -q                                # queue + verification guarantees (no network)
```

Each run writes to `runs/<run_id>/`: `brief.md` (the cited brief), `run_report.md` (steps, retries, recoveries, plan history, verification stats, context size over the run), `state.db` (everything), and `logs/` (one log per process).

See **NOTES.md** for what we went deep on and why, the decisions we're most confident about, what was cut, and how coding tools were used.
