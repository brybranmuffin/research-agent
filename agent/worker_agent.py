"""Worker agent: one generic runtime hosting the three subagents (search, extract, synthesize).

Component: Subagents (Tier 1). Started as its own OS process via `run.py worker --role <role>`.

Loop: register -> poll for a claimable task of this role (idle backoff 0.5-2 s) -> claim ->
heartbeat every 10 s on a background thread -> run the role's fixed pipeline with a fresh
context -> fenced completion, or a failure classified transient | deterministic -> repeat until
the run is done/degraded. No transcript is carried between tasks.

Subagents are config objects (ROLES): tools + one fixed pipeline per task kind. Code calls the
tools; the LLM is called once or twice per task:
- search:     LLM writes <=3 queries -> fts_search -> LLM picks <=4 docs -> candidates
- extract:    read <=2 page windows around the search hits -> LLM extracts claims ->
              each claim passes the verification gate (tools.record_claim) on write
- synthesize: cross_check   deterministic verdict floor + LLM rationale (may only downgrade)
              write_section cited markdown; every [C#] must be a verified claim of this sub-question

Models: one per subagent, declared below. Built-in retries do not exist in ChatNVIDIA; the
task queue owns retries, so every one is counted.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Callable

from langchain_nvidia_ai_endpoints import ChatNVIDIA
from pydantic import BaseModel, Field

import config
from agent import memory, store, system_prompt, tools
from agent.store import DeterministicError, TransientError

SEARCH_MODEL = ChatNVIDIA(model=config.model_name("search"), temperature=0, max_completion_tokens=800, timeout=90)
EXTRACT_MODEL = ChatNVIDIA(model=config.model_name("extract"), temperature=0, max_completion_tokens=2000, timeout=90)
SYNTHESIZE_MODEL = ChatNVIDIA(model=config.model_name("synthesize"), temperature=0, max_completion_tokens=1200,
                              timeout=90)


# --------------------------------------------------------------------------- output schemas

class SearchQueries(BaseModel):
    queries: list[str] = Field(description="1-3 keyword queries")


class Pick(BaseModel):
    doc_id: str
    reason: str = Field(description="one line: why this document likely holds direct evidence")


class SearchSelection(BaseModel):
    picks: list[Pick]


class ExtractedClaim(BaseModel):
    text: str = Field(description="the claim, one sentence, in your own words")
    quote: str = Field(description="exact contiguous passage from the window, >= 8 words")
    unit: int = Field(description="page or section number from the === header")
    stance: str = Field(description="a hypothesis id, or 'neutral'")


class ExtractedClaims(BaseModel):
    claims: list[ExtractedClaim] = Field(default_factory=list)


class CrossCheck(BaseModel):
    rationale: str
    downgrade_to: str = Field(default="", description="'contested', 'thin', or empty to keep the floor")


class Section(BaseModel):
    markdown: str = Field(description="120-220 words; every factual sentence ends with [C#] markers")


CITATION = tools.CITATION


# --------------------------------------------------------------------------- pipelines

def _messages(step: str, role: str, memory_text: str, task_text: str):
    return system_prompt.build_messages(step, tool_defs=tools.describe_tools(ROLES[role].tools),
                                        memory=memory_text, task=task_text)


def run_search(ctx: store.Ctx, task, inp: dict):
    subq_id, cfg = task["subq_id"], ctx.cfg
    mem = memory.for_search(ctx.conn, subq_id)
    focus = f"\nFocus for this round: {inp['focus']}" if inp.get("focus") else ""
    q = tools.call_structured(ctx, SEARCH_MODEL, SearchQueries,
                              _messages("search.queries", "search", mem, f"Sub-question SQ{subq_id}: {inp['subq_text']}{focus}"),
                              step="search.queries")
    queries = [s.strip() for s in q.queries if s.strip()][:cfg.search_max_queries]

    found, agg = set(memory.found_docs(ctx.conn, subq_id)), {}
    for query in queries:
        hits = tools.fts_search.invoke({"query": query, "k": cfg.search_hits_per_query, "ctx": ctx},
                                       config=tools.run_config(ctx, "fts_search"))
        for rank, d in enumerate(hits):
            m = d.metadata
            if m["doc_id"] in found:
                continue
            a = agg.setdefault(m["doc_id"], {"title": m["title"], "year": m["year"], "kind": m["source_kind"],
                                             "score": 0.0, "units": {}, "snippets": []})
            a["score"] += 1.0 / (rank + 1)
            a["units"][m["unit"]] = a["units"].get(m["unit"], 0) + 1
            if len(a["snippets"]) < 2:
                a["snippets"].append(d.page_content)
    ranked = sorted(agg.items(), key=lambda kv: (-kv[1]["score"], kv[0]))[:10]
    if not ranked:
        return {"queries": queries, "selected": []}, None

    listing = "\n".join(f"- {doc_id} ({a['year']}, {a['kind']}): {a['title']}\n"
                        + "\n".join(f"    > {s}" for s in a["snippets"]) for doc_id, a in ranked)
    offered = {doc_id for doc_id, _ in ranked}

    def check(out: SearchSelection):
        bad = [p.doc_id for p in out.picks if p.doc_id not in offered]
        if bad:
            raise ValueError(f"doc_ids not among the candidates: {bad}")

    sel = tools.call_structured(
        ctx, SEARCH_MODEL, SearchSelection,
        _messages("search.rank", "search", mem,
                  f"Sub-question SQ{subq_id}: {inp['subq_text']}\nSelect up to {cfg.search_top_docs} documents.\n\n"
                  f"Candidates:\n{listing}"),
        step="search.rank", check=check)
    reasons = {p.doc_id: p.reason for p in sel.picks}
    picks = list(dict.fromkeys(p.doc_id for p in sel.picks))[:cfg.search_top_docs]
    if not picks:  # never leave a sub-question with nothing to read when search found matches
        picks = [doc_id for doc_id, _ in ranked[:2]]
        reasons = {d: "fallback: top full-text hit" for d in picks}

    rows = []
    for doc_id in picks:
        a = agg[doc_id]
        units = sorted(a["units"], key=lambda u: (-a["units"][u], u))
        rows.append((subq_id, doc_id, round(a["score"], 3), json.dumps(units), reasons.get(doc_id, ""), task["task_id"]))

    def writes(conn):
        conn.executemany("INSERT OR IGNORE INTO candidates VALUES (?, ?, ?, ?, ?, ?)", rows)

    return {"queries": queries, "selected": picks}, writes


def run_extract(ctx: store.Ctx, task, inp: dict):
    doc = tools.get_doc(ctx.conn, inp["doc_id"])
    if doc is None:
        raise DeterministicError(f"unknown document {inp['doc_id']}")
    reader = tools.read_pdf if doc["type"] == "pdf" else tools.read_html
    blocks, units_read = [], []
    for a, b in tools.page_windows(inp["hit_units"], doc["n_units"], ctx.cfg):
        for d in reader.invoke({"doc_id": doc["doc_id"], "start": a, "end": b, "ctx": ctx},
                               config=tools.run_config(ctx, reader.name)):
            if d.metadata.get("error"):
                raise DeterministicError(d.metadata["error"])
            unit = d.metadata["unit"]
            head = f"=== page {unit} ===" if doc["type"] == "pdf" else f"=== section {unit}: {d.metadata['label']} ==="
            blocks.append(f"{head}\n{d.page_content or '(no extractable text)'}")
            units_read.append(unit)

    allowed = set(memory.hypothesis_ids(ctx.conn)) | {"neutral"}

    def check(out: ExtractedClaims):
        bad = sorted({c.stance for c in out.claims} - allowed)
        if bad:
            raise ValueError(f"unknown stance(s) {bad}; use one of {sorted(allowed)}")

    task_text = (f"Sub-question SQ{task['subq_id']}: {inp['subq_text']}\n"
                 f"Document: {doc['doc_id']} ({doc['title']})\n\n" + "\n\n".join(blocks))
    out = tools.call_structured(ctx, EXTRACT_MODEL, ExtractedClaims,
                                _messages("extract", "extract", memory.for_extract(ctx.conn, task["subq_id"], doc["doc_id"]),
                                          task_text),
                                step="extract", check=check)
    claims = out.claims[:ctx.cfg.extract_max_claims]
    result = {"doc_id": doc["doc_id"], "units_read": units_read, "claims": len(claims), "verified": 0, "rejected": 0}

    def writes(conn):
        for c in claims:
            _, v = tools.record_claim(conn, ctx.cfg, actor=ctx.actor, task_id=task["task_id"], subq_id=task["subq_id"],
                                      doc_id=doc["doc_id"], text=c.text, quote=c.quote, unit=c.unit, stance=c.stance)
            result["verified" if v.verified else "rejected"] += 1

    return result, writes


def run_cross_check(ctx: store.Ctx, task, inp: dict):
    floor = tools.compute_verdict(ctx.conn, task["subq_id"])
    if floor["n_verified"] == 0:
        return {"verdict": "thin", "floor": "thin", "floor_detail": floor,
                "rationale": "No quote-verified evidence was found for this sub-question."}, None
    out = tools.call_structured(
        ctx, SYNTHESIZE_MODEL, CrossCheck,
        _messages("cross_check", "synthesize",
                  memory.for_cross_check(ctx.conn, task["subq_id"], floor, ctx.cfg.synth_max_claims),
                  f"Sub-question SQ{task['subq_id']}: {inp['subq_text']}"),
        step="cross_check")
    proposed = out.downgrade_to.strip().lower() or None
    final = tools.final_verdict(floor["verdict"], proposed)
    if proposed and proposed != floor["verdict"]:
        store.log_event(ctx.conn, ctx.actor, "verdict_downgraded" if final == proposed else "verdict_upgrade_ignored",
                        task_id=task["task_id"], subq_id=task["subq_id"], floor=floor["verdict"], proposed=proposed)
    return {"verdict": final, "floor": floor["verdict"], "rationale": out.rationale, "floor_detail": floor}, None


def run_write_section(ctx: store.Ctx, task, inp: dict):
    mem, claims = memory.for_write(ctx.conn, task["subq_id"], ctx.cfg.synth_max_claims)
    allowed = set(claims)

    def check(out: Section):
        out.markdown = tools.split_grouped_citations(out.markdown)
        cited = {int(x) for x in CITATION.findall(out.markdown)}
        if not cited:
            raise ValueError("no citation markers like [C12] found")
        if cited - allowed:
            raise ValueError(f"unknown claim ids {sorted(cited - allowed)}; cite only {sorted(allowed)}")

    out = tools.call_structured(ctx, SYNTHESIZE_MODEL, Section,
                                _messages("write_section", "synthesize", mem,
                                          f"Sub-question SQ{task['subq_id']}: {inp['subq_text']}"),
                                step="write_section", check=check)
    cited = sorted({int(x) for x in CITATION.findall(out.markdown)})

    def writes(conn):
        conn.execute(
            "INSERT INTO sections VALUES (?, (SELECT COALESCE(MAX(version), 0) + 1 FROM sections WHERE subq_id = ?), "
            "?, ?, ?, ?)", (task["subq_id"], task["subq_id"], out.markdown.strip(), json.dumps(cited),
                            task["task_id"], time.time()))

    return {"cited_claims": cited, "words": len(out.markdown.split())}, writes


@dataclass
class Role:
    tools: list
    pipelines: dict[str, Callable]   # task kind -> fixed pipeline


ROLES = {
    "search": Role([tools.fts_search, tools.list_docs], {"search": run_search}),
    "extract": Role([tools.read_pdf, tools.read_html], {"extract": run_extract}),
    "synthesize": Role([], {"cross_check": run_cross_check, "write_section": run_write_section}),
}


# --------------------------------------------------------------------------- worker loop

class Heartbeat:
    """Background thread that renews the task lease every heartbeat_s (own DB connection)."""

    def __init__(self, ctx: store.Ctx, task_id: int):
        self.ctx, self.task_id = ctx, task_id
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._beat, daemon=True)

    def _beat(self):
        conn = store.connect(store.state_path(self.ctx.run_id), attach_corpus=False)
        while not self.stopped.wait(self.ctx.cfg.heartbeat_s):
            if not store.heartbeat(conn, self.task_id, self.ctx.actor, self.ctx.cfg):
                break  # lease lost: completion will be fenced and discarded
        conn.close()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stopped.set()


def _maybe_chaos_kill(ctx: store.Ctx, n_claimed: int) -> None:
    cfg = ctx.cfg
    if not (cfg.chaos and ctx.actor == cfg.chaos_kill_worker and n_claimed == cfg.chaos_kill_on_task):
        return
    if store.count(ctx.conn, "SELECT COUNT(*) FROM events WHERE kind = 'chaos' AND detail_json LIKE '%worker_kill%'"):
        return  # once per run, even across resumes
    store.log_event(ctx.conn, ctx.actor, "chaos", task_id=ctx.task_id, fault="worker_kill", pid=os.getpid())
    print(f"[{ctx.actor}] chaos: killing this worker mid-task {ctx.task_id}", flush=True)
    os._exit(137)


def main(run_id: str, role: str, worker_id: str) -> None:
    ctx = store.open_ctx(run_id, worker_id)
    conn, cfg, spec = ctx.conn, ctx.cfg, ROLES[role]
    store.register_worker(conn, worker_id, role, os.getpid())
    idle, n_claimed = cfg.worker_poll_min_s, 0
    while store.get_run(conn)["phase"] not in store.FINAL_PHASES:
        store.touch_worker(conn, worker_id)
        task = store.claim_task(conn, role, worker_id, cfg)
        if task is None:
            time.sleep(idle)
            idle = min(idle * 1.5, cfg.worker_poll_max_s)
            continue
        idle, n_claimed = cfg.worker_poll_min_s, n_claimed + 1
        ctx.task_id, ctx.attempt, ctx.chaos_counter = task["task_id"], task["attempts"], 0
        tag = f"[{worker_id}] task {task['task_id']} {task['kind']} SQ{task['subq_id']} attempt {task['attempts']}"
        print(f"{tag}: claimed", flush=True)
        _maybe_chaos_kill(ctx, n_claimed)
        try:
            with Heartbeat(ctx, task["task_id"]):
                result, writes = spec.pipelines[task["kind"]](ctx, task, json.loads(task["input_json"]))
            ok = store.complete_task(conn, task["task_id"], worker_id, result, writes)
            print(f"{tag}: {'done' if ok else 'result discarded (lease lost)'} {json.dumps(result, default=str)[:200]}",
                  flush=True)
        except (TransientError, DeterministicError) as err:
            error_class = "transient" if isinstance(err, TransientError) else "deterministic"
            status = store.fail_task(conn, task["task_id"], worker_id, error_class, str(err), cfg)
            print(f"{tag}: {error_class} error -> {status}: {err}", flush=True)
        except Exception as err:  # a bug must fail the task, not kill the worker
            traceback.print_exc()
            status = store.fail_task(conn, task["task_id"], worker_id, "deterministic", f"{type(err).__name__}: {err}", cfg)
            print(f"{tag}: unexpected error -> {status}", flush=True)
        finally:
            ctx.task_id = None
    store.stop_worker(conn, worker_id)
