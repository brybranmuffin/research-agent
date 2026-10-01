# Research Agent: Multi-Agent Research Briefs over a Long Horizon

A small multi-agent system that takes a research question, gathers and cross-checks sources, and writes a short, cited brief. Separate worker processes split the work into **search**, **extraction**, and **synthesis**. An **orchestrator** plans the work, hands it out, supervises it, and revises the plan.

**The system is general; the demo is not.** No code knows anything about dinosaurs:
- the planner derives hypotheses and sub-questions from whatever question it is given;
- the workers search, read, and verify whatever corpus has been ingested.

The only things specific to the Spinosaurus demo are **the question** (the default in `run.py`) and **the chosen corpus** (`corpus/manifest.json` and its fetch script). Swap those two and the same system researches another topic. See [Another question or corpus](#another-question-or-corpus).

- [NOTES.md](NOTES.md): what we went deep on and why, decisions, cuts, and how we used coding tools.
- [DETAILED_NOTES.md](DETAILED_NOTES.md): design priorities, mechanisms, parameters, and results from the first live run.

## Goal

**Chosen problem:** #2, take a research question, gather and cross-check sources, and write a short brief.

**Demo question** (one instance of the general system):
> *Was* Spinosaurus aegyptiacus *an aquatic pursuit predator, and how strong is the evidence?*

The topic was picked because the scientific literature genuinely disagrees. One camp argues for swimming and diving, based on the paddle-like tail and dense bones. The other argues for wading like a heron, based on buoyancy, stability, and anatomy. Rebuttals and replies run in both directions, so cross-checking has real conflicts to find without us planting any.

**Corpus:** 34 open-access sources, all local: 22 PDFs (mostly papers and preprints) and 12 web pages (HTML). A manifest pins every source by URL, version, licence, and sha256 hash, so every run works from identical bytes. It also lists 6 closed-access key papers that are not downloaded; the corpus covers them through replies, summaries, and press. A separate answer key (`eval/corpus_labels.json`), which agents never see, labels each document's stance.

**Outputs of a run:**
- **Brief:** a bottom line, a verdict table, one cited section per sub-question, an appendix of every cited quote, and the sources. Each citation points to a quote that was machine-checked against the source text.
- **Run report:** steps taken, the timeline, plan revisions and their reasons, task outcomes, retries and recoveries, verification statistics, and prompt size per step type.

## Architecture

```
                       ┌──────────────────────────── ORCHESTRATOR (process) ───────────────────────────┐
  research question ─► │  Supervisor (every tick, no LLM): leases, retries, failure detection          │
                       │  Planner   (LLM, at barriers only): plan → review/replan → assemble           │
                       └──────────────┬───────────────────────────────────────────▲────────────────────┘
                                      │ creates tasks                              │ reads results
                                      ▼                                            │
          ┌──────────────────────────────────── SHARED STATE STORE (SQLite, WAL) ────────────────────────┐
          │  goal & plan versions │ task queue + leases │ candidates │ claims │ sections │ event/trace log │
          └──────┬──────────────────────────────┬──────────────────────────────────┬────────────────────┘
                 │                              │                                  │ 
          ┌──────▼───────┐              ┌───────▼────────┐                 ┌───────▼────────┐
          │ SEARCH  ×1   │              │ EXTRACT  ×2    │                 │ SYNTHESIZE ×1  │
          │ (process)    │              │ (processes)    │                 │ (process)      │
          │ full-text    │              │ paged PDF/HTML │                 │ cross-check,   │
          │ index search │              │ readers        │                 │ write sections │
          └──────────────┘              └────────────────┘                 └────────────────┘
                 ▲
          local corpus (PDF + HTML) ── parsed and indexed once by `run.py ingest`, deterministically
```

**The runtime boundary is real.** The orchestrator and every worker run as separate OS processes. They share no memory and never call each other directly. All coordination goes through one SQLite database file in WAL mode. Handoff means writing a task row, claiming it atomically, and writing back a result row.

### Run lifecycle

The orchestrator calls the LLM only at **phase barriers**. In between, a supervisor loop that never calls the LLM keeps the run healthy.

1. **Plan.** Break the question into competing hypotheses and 4-6 sub-questions. The plan is stored as version 1.
2. **Gather.** For each sub-question, a search task picks candidate documents, and extract tasks read them in page windows and produce claims. Every claim's quote is checked against the source before it is stored.
3. **Cross-check.** For each sub-question, a verdict: *supported*, *contested*, or *thin*.
4. **Review (barrier).** The planner sees the verdicts and may revise the plan: more searches for a thin sub-question, a new targeted sub-question, retiring one, or done. Each revision is a new plan version with a recorded reason. Steps 2 to 4 repeat at most twice.
5. **Write and assemble (barrier).** Write one section per sub-question, then assemble the brief and the run report.

### Agents

| Role | Input | Tools | Output |
|---|---|---|---|
| **Orchestrator** | Goal, settled-state snapshot | Task creation, plan versioning | Plan versions, tasks, final assembly |
| **Search** | Sub-question + hypotheses | Full-text search over the corpus index, document listing | Up to 4 candidate documents with reasons |
| **Extract** | Sub-question + one document | Paged PDF reader, sectioned HTML reader | Claims, each with a verbatim quote, location, and stance |
| **Synthesize** | Sub-question + its verified claims | None (reasons over stored claims) | Cross-check verdicts and cited sections |

Each worker gets a fresh context for every task. No agent keeps a running transcript.

## Reproducibility

- **Pinned corpus:** a manifest plus a fetch script that verifies every file's sha256 hash.
- **Deterministic ingest:** identical source files produce an identical `corpus.db`.
- **Replay (playback):** every run records its full trace (task events, tool calls, LLM prompts and outputs) in its state store. `run.py replay` prints that trace in order, as if the run were happening, with no models, network, or API keys. It shows *what happened*; re-executing the system against recorded LLM responses is listed as next work in NOTES.
- **Chaos mode:** seeded fault injection (tool errors, malformed LLM output, a worker killed mid-task) shows recovery. The fixed seed makes the faults repeatable.
- **Tests:** `pytest -q` runs 58 tests of the queue, verification, orchestrator rules, and launcher output, with no network.

## Stack

- Python 3.13, **LangChain** (`langchain-core`): forced tool calls, `@tool` tools, s`ChatPromptTemplate`, and a callback handler that records every LLM and tool call
- **NVIDIA NIM** (free tier) via `langchain-nvidia-ai-endpoints`; each agent declares its own model (default `google/gemma-4-31b-it`)
- **SQLite** (standard library): the shared state store (WAL mode) and the corpus full-text index (FTS5)
- **Pydantic** for typed outputs, **pypdf** and **BeautifulSoup** for text extraction, **RapidFuzz** for quote matching, **pytest**

## Replay the recorded run (no API key needed)

`runs/run_20261001_151557/` holds one complete recorded run of the demo question: 23.9 min, 111 steps, 40 tasks, one plan revision, and 3 provider timeouts that were retried and recovered. Replaying it needs only the Python dependencies: no API key, no corpus download, no ingest.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python run.py replay run_20261001_151557                          # paced playback, about 25 s (20x speed, pauses capped at 0.5 s)
python run.py replay run_20261001_151557 --speed 1e9 --max-gap 0  # print everything at once
python run.py replay runs/run_20261001_151557                     # a path to any run directory also works
```

Replay prints one line per event or LLM call, timestamped from the start of the run, and then the brief:

```
t+   92.9s  extract-2     tool read_pdf({"doc_id": "ibrahim2020_nature", "start": 2, "end": 4}) -> 3 results
t+   92.9s  extract-2     LLM extract [2936->708 tokens, 53.0s]: [{"name": "ExtractedClaims", "args": {"claims": [...
t+  689.5s  synthesize-1  SQ4 verdict downgraded contested -> thin
t+  767.8s  orchestrator  plan v2: more_search SQ4 (): The verdict is 'thin' because most evidence is tagged as 'neutral'…
t+ 1051.1s  synthesize-1  task 37 transient error on attempt 1, will retry: ReadTimeout: HTTPSConnectionPool(...)…
```

The same folder can be read directly:
- `brief.md`: the brief;
- `run_report.md`: steps, timeline, plan history, retries, verification stats, prompt size per step type;
- `logs/`: one log per process;
- `state.db`: the full SQLite state, including every task, claim, event, and LLM response.

Replay is playback: it shows what happened and does not re-run the system. LLM lines are stamped with the time the call started.

## How to run

Requires Python 3.11+ and a free NVIDIA NIM key from [build.nvidia.com](https://build.nvidia.com).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                          # then set NVIDIA_API_KEY

python corpus/fetch_corpus.py                 # download the 34 open-access sources, sha256-verified (~125 MB)
python run.py ingest                          # parse PDFs/HTML -> corpus.db (chunks + FTS5 index), ~20 s
python run.py                                 # run the demo question (about 20-25 min on the free tier)'
python run.py "<question about spinosaurus>"  # run your own question about spinosaurus (this topic constraint was due to corpus limitations)
python run.py -v                              # same, and stream every process's log lines to the terminal
python run.py --resume <run_id>               # Ctrl-C a run, then continue it from state.db
python run.py replay <run_id>                 # play a recorded run back from its trace (no keys, no network)

pytest -q                                # queue, verification, orchestrator and launcher tests (no network)
python run.py "your question" --chaos    # There is a chaos mode with seeded faults such as tool errors, malformed LLM output, a killed worker. This mode is untested but can be run, just ran out of time here.

```

Each run writes to `runs/<run_id>/`: `brief.md` (the cited brief), `run_report.md` (steps, retries, recoveries, plan history, verification stats, prompt size per step type), `state.db` (everything), and `logs/` (one log per process).

### Another question or corpus

Nothing else needs to change; the corpus and the question are the only inputs.

1. **Put the documents in `corpus/raw/`** as `<id>.pdf` or `<id>.html`.
2. **Describe each one in `corpus/manifest.json`:**
   ```json
   {"id": "smith2024", "type": "pdf", "title": "...", "authors": ["Jane Smith", "..."], "year": 2024,
    "venue": "...", "license": "CC-BY-4.0", "status": "ok"}
   ```
   - Optional: `"sha256"` pins the exact bytes.
   - Optional: `"source_kind"` (`"primary"` or `"secondary"`) overrides the default, which treats PDFs as primary sources and web pages as secondary. Only primary sources with distinct first authors count as independent confirmation.
3. **Build the index and ask:** `python run.py ingest`, then `python run.py "your question"`.

`corpus/fetch_corpus.py` and `eval/corpus_labels.json` belong to the demo corpus; the system itself never reads the answer key. The planner sees the corpus as a list of titles, capped at 150. Corpora much larger than that would need a summarized listing.
