"""Orchestrator agent: supervisor (no LLM, every tick) + planner (LLM, only at phase barriers).

Component: Orchestration + handoff (Tier 1). Started as its own OS process via
`run.py orchestrator`. The single writer of the plan: only this process creates tasks or
changes the plan. It is stateless between ticks: every decision is re-derived from state.db,
so after a crash or resume it simply continues from the stored phase.

Phases: plan -> gather -> crosscheck -> review -> (gather again | write) -> assemble -> done | degraded

Supervisor (every tick): mark silent workers dead, reap expired leases / hard timeouts,
turn search candidates into extract tasks, detect barriers, enforce budgets and phase timeouts.

Planner (LLM at barriers only; outputs typed actions that this code applies and validates):
- plan:     research brief (goal + acceptance criteria), 2-4 hypotheses, 4-6 sub-questions
- review:   more_search | add_subquestion | retire | complete (max 2 rounds, max 8 sub-questions);
            every applied revision is a new plan version with its reasons
- assemble: the brief's bottom line; the brief and run report are rendered from state.db

Graceful degradation: a budget hit or phase timeout cancels open research tasks and jumps to
writing with whatever is verified; every planner step has a deterministic fallback.
"""
from __future__ import annotations

import json
import re
import time
from typing import Optional

from langchain_nvidia_ai_endpoints import ChatNVIDIA
from pydantic import BaseModel, Field

import config
from agent import memory, store, system_prompt, tools
from agent.store import DeterministicError, TransientError

PLANNER_MODEL = ChatNVIDIA(model=config.model_name("planner"), temperature=0, max_completion_tokens=2000, timeout=90)
ACTOR = "orchestrator"
RESEARCH_KINDS = ("search", "extract", "cross_check")


# --------------------------------------------------------------------------- planner output schemas

class Hypothesis(BaseModel):
    id: str = Field(description="short snake_case id, e.g. aquatic_pursuit")
    description: str


class SubQuestion(BaseModel):
    text: str
    tests: list[str] = Field(default_factory=list, description="hypothesis ids this sub-question tests")


class Plan(BaseModel):
    goal: str
    acceptance_criteria: list[str]
    hypotheses: list[Hypothesis]
    subquestions: list[SubQuestion]


class ReviewAction(BaseModel):
    type: str = Field(description="more_search | add_subquestion | retire | complete")
    subq_id: Optional[int] = Field(default=None, description="target sub-question (more_search, retire)")
    text: str = Field(default="", description="search focus (more_search) or the new sub-question (add_subquestion)")
    reason: str


class ReviewDecision(BaseModel):
    actions: list[ReviewAction] = Field(default_factory=list)


class BottomLine(BaseModel):
    text: str = Field(description="120-200 words")


# --------------------------------------------------------------------------- helpers

def log(msg: str) -> None:
    print(f"[orchestrator] {msg}", flush=True)


def open_count(conn, kinds: tuple[str, ...]) -> int:
    marks = ",".join("?" * len(kinds))
    return store.count(conn, f"SELECT COUNT(*) FROM tasks WHERE status IN ('pending', 'claimed') AND kind IN ({marks})",
                       kinds)


def plan_version(conn) -> int:
    return conn.execute("SELECT COALESCE(MAX(version), 0) FROM plan_versions").fetchone()[0]


def active_subqs(conn):
    return conn.execute("SELECT * FROM subquestions WHERE retired_in_version IS NULL ORDER BY subq_id").fetchall()


def enqueue_search(conn, subq_id: int, text: str, version: int, round_: int, focus: str = "") -> Optional[int]:
    return store.enqueue_task(conn, kind="search", role="search", subq_id=subq_id, plan_version=version, round=round_,
                              inputs={"subq_text": text, "focus": focus}, dedupe_key=f"search:{subq_id}:{round_}")


def planner_call(ctx: store.Ctx, schema, step: str, memory_text: str, task_text: str, check=None):
    """Structured planner call with the same retry policy as worker tasks (it runs in-process)."""
    msgs = system_prompt.build_messages(step, memory=memory_text, task=task_text)
    attempt = 0
    while True:
        attempt += 1
        try:
            return tools.call_structured(ctx, PLANNER_MODEL, schema, msgs, step=step, check=check)
        except TransientError as err:
            if attempt > ctx.cfg.transient_retries:
                raise
            error, wait = str(err), ctx.cfg.transient_backoff_s[min(attempt - 1, len(ctx.cfg.transient_backoff_s) - 1)]
        except DeterministicError as err:
            if attempt > ctx.cfg.deterministic_retries:
                raise
            error, wait = str(err), ctx.cfg.transient_backoff_s[0]
        store.log_event(ctx.conn, ACTOR, "planner_retry", step=step, attempt=attempt, error=error[:300])
        time.sleep(wait)


# --------------------------------------------------------------------------- phase: plan

def check_plan(cfg: config.Config):
    def check(plan: Plan):
        ids = [h.id for h in plan.hypotheses]
        if not 2 <= len(ids) <= 4:
            raise ValueError(f"need 2-4 hypotheses, got {len(ids)}")
        if len(set(ids)) != len(ids) or any(not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", i) or i == "neutral" for i in ids):
            raise ValueError(f"hypothesis ids must be unique snake_case and not 'neutral': {ids}")
        if len(plan.subquestions) < cfg.min_initial_subqs:
            raise ValueError(f"need at least {cfg.min_initial_subqs} sub-questions, got {len(plan.subquestions)}")
    return check


def phase_plan(ctx: store.Ctx) -> None:
    conn, cfg = ctx.conn, ctx.cfg
    question = store.get_run(conn)["question"]
    try:
        plan = planner_call(ctx, Plan, "planner.plan", memory.for_plan(conn), f"Research question: {question}",
                            check=check_plan(cfg))
        hyps = [h.model_dump() for h in plan.hypotheses]
        ids = {h["id"] for h in hyps}
        subqs = [(s.text.strip(), [t for t in s.tests if t in ids]) for s in plan.subquestions[:cfg.max_initial_subqs]]
        goal = {"goal": plan.goal, "acceptance_criteria": plan.acceptance_criteria}
        reason = "initial plan"
    except (TransientError, DeterministicError) as err:
        hyps = [{"id": "yes", "description": "The evidence supports an affirmative answer."},
                {"id": "no", "description": "The evidence supports a negative answer."}]
        subqs, goal = [(question, ["yes", "no"])], {"goal": question, "acceptance_criteria": []}
        reason = f"fallback plan: planner failed ({str(err)[:200]})"
        store.log_event(conn, ACTOR, "planner_fallback", step="planner.plan", error=str(err)[:300])
    with store.tx(conn):
        conn.execute("UPDATE run SET goal_json = ? WHERE id = 1", (json.dumps(goal),))
        conn.execute("INSERT INTO plan_versions VALUES (1, 0, ?, ?, ?)", (reason, json.dumps(hyps), time.time()))
        for text, tests in subqs:
            sid = conn.execute("INSERT INTO subquestions (text, tests_json, added_in_version, status) "
                               "VALUES (?, ?, 1, 'gathering')", (text, json.dumps(tests))).lastrowid
            enqueue_search(conn, sid, text, 1, 0)
        store.log_event(conn, ACTOR, "plan_version", version=1, reason=reason,
                        hypotheses=[h["id"] for h in hyps], subquestions=[t for t, _ in subqs])
        store.set_phase(conn, "gather")
    log(f"plan v1: {len(hyps)} hypotheses, {len(subqs)} sub-questions")


# --------------------------------------------------------------------------- supervisor + gather / crosscheck

UNEXPANDED_SQL = """SELECT c.subq_id, c.doc_id, c.hit_units_json, s.text FROM candidates c
    JOIN subquestions s ON s.subq_id = c.subq_id
    WHERE s.retired_in_version IS NULL
      AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.dedupe_key = 'extract:' || c.subq_id || ':' || c.doc_id)
    ORDER BY c.subq_id, c.score DESC"""


def supervise(ctx: store.Ctx) -> None:
    conn, cfg = ctx.conn, ctx.cfg
    for w in store.mark_dead_workers(conn, cfg):
        log(f"worker {w} presumed dead (no heartbeat for {cfg.lease_s:.0f}s)")
    for r in store.reap_tasks(conn, cfg):
        log(f"task {r['task_id']} {r['reason']} -> {r['status']}")
    run = store.get_run(conn)
    if run["phase"] == "gather" and not run["degrade_reason"]:
        rows = conn.execute(UNEXPANDED_SQL).fetchall()
        if rows:
            with store.tx(conn):
                for r in rows:
                    store.enqueue_task(conn, kind="extract", role="extract", subq_id=r["subq_id"],
                                       plan_version=plan_version(conn), round=run["review_round"],
                                       inputs={"subq_text": r["text"], "doc_id": r["doc_id"],
                                               "hit_units": json.loads(r["hit_units_json"])},
                                       dedupe_key=f"extract:{r['subq_id']}:{r['doc_id']}")


def phase_gather(ctx: store.Ctx) -> None:
    conn = ctx.conn
    if open_count(conn, ("search", "extract")) or conn.execute(UNEXPANDED_SQL).fetchone():
        return  # barrier not reached
    with store.tx(conn):
        round_ = store.get_run(conn)["review_round"]
        for s in conn.execute("SELECT subq_id, text FROM subquestions WHERE status = 'gathering' "
                              "AND retired_in_version IS NULL").fetchall():
            store.enqueue_task(conn, kind="cross_check", role="synthesize", subq_id=s["subq_id"],
                               plan_version=plan_version(conn), round=round_, inputs={"subq_text": s["text"]},
                               dedupe_key=f"cross_check:{s['subq_id']}:{round_}")
            conn.execute("UPDATE subquestions SET status = 'checking' WHERE subq_id = ?", (s["subq_id"],))
        store.set_phase(conn, "crosscheck")
    log("gather barrier reached -> crosscheck")


def phase_crosscheck(ctx: store.Ctx) -> None:
    conn = ctx.conn
    if open_count(conn, ("cross_check",)):
        return
    with store.tx(conn):
        for s in conn.execute("SELECT subq_id FROM subquestions WHERE status = 'checking'").fetchall():
            sid = s["subq_id"]
            t = conn.execute("SELECT status, result_json FROM tasks WHERE kind = 'cross_check' AND subq_id = ? "
                             "ORDER BY task_id DESC LIMIT 1", (sid,)).fetchone()
            if t is not None and t["status"] == "done":
                res = json.loads(t["result_json"])
                verdict, rationale = res["verdict"], res["rationale"]
            else:
                verdict = tools.compute_verdict(conn, sid)["verdict"]
                rationale = (f"Cross-check task {t['status'] if t else 'missing'}; "
                             "verdict computed deterministically from verified claims.")
            conn.execute("UPDATE subquestions SET verdict = ?, verdict_rationale = ?, status = 'checked' "
                         "WHERE subq_id = ?", (verdict, rationale, sid))
            store.log_event(conn, ACTOR, "verdict", subq_id=sid, verdict=verdict)
        store.set_phase(conn, "review")
    log("crosscheck barrier reached -> review")


# --------------------------------------------------------------------------- phase: review (replanning)

def phase_review(ctx: store.Ctx) -> None:
    conn, cfg = ctx.conn, ctx.cfg
    run = store.get_run(conn)
    if run["review_round"] >= cfg.max_review_rounds:
        store.log_event(conn, ACTOR, "review_skipped", reason="maximum review rounds reached")
        return start_write(ctx)
    active = active_subqs(conn)
    max_new = max(0, min(cfg.max_new_subqs_per_round, cfg.max_total_subqs - len(active)))
    task = (f"Decide the next actions.\nLimits: at most {max_new} add_subquestion actions this round. "
            f"Valid sub-question ids: {', '.join(str(s['subq_id']) for s in active)}.")
    try:
        decision = planner_call(ctx, ReviewDecision, "planner.review", memory.for_review(conn), task)
    except (TransientError, DeterministicError) as err:
        store.log_event(conn, ACTOR, "planner_fallback", step="planner.review", error=str(err)[:300])
        return start_write(ctx)
    apply_review(ctx, decision, max_new)


def apply_review(ctx: store.Ctx, decision: ReviewDecision, max_new: int) -> None:
    conn = ctx.conn
    run = store.get_run(conn)
    new_round, version = run["review_round"] + 1, plan_version(conn) + 1
    active = {s["subq_id"]: s for s in active_subqs(conn)}
    applied, rejected, adds, new_work = [], [], 0, False
    with store.tx(conn):
        for a in decision.actions:
            kind = a.type.strip().lower()
            if kind == "complete":
                applied.append(f"complete: {a.reason}")
                break
            if kind == "more_search" and a.subq_id in active:
                if enqueue_search(conn, a.subq_id, active[a.subq_id]["text"], version, new_round, focus=a.text or a.reason):
                    conn.execute("UPDATE subquestions SET status = 'gathering' WHERE subq_id = ?", (a.subq_id,))
                    applied.append(f"more_search SQ{a.subq_id} ({a.text}): {a.reason}")
                    new_work = True
            elif kind == "add_subquestion" and a.text.strip() and adds < max_new:
                if a.text.strip().lower() in {s["text"].strip().lower() for s in active.values()}:
                    rejected.append((a, "duplicate of an existing sub-question"))
                    continue
                sid = conn.execute("INSERT INTO subquestions (text, added_in_version, status) VALUES (?, ?, 'gathering')",
                                   (a.text.strip(), version)).lastrowid
                enqueue_search(conn, sid, a.text.strip(), version, new_round)
                applied.append(f"add SQ{sid} '{a.text.strip()}': {a.reason}")
                adds, new_work = adds + 1, True
            elif kind == "retire" and a.subq_id in active and len(active) > 1:
                conn.execute("UPDATE subquestions SET retired_in_version = ?, status = 'retired' WHERE subq_id = ?",
                             (version, a.subq_id))
                store.cancel_open_tasks(conn, RESEARCH_KINDS, "sub-question retired", subq_id=a.subq_id)
                active.pop(a.subq_id)
                applied.append(f"retire SQ{a.subq_id}: {a.reason}")
            else:
                rejected.append((a, "invalid type, unknown sub-question, or limit reached"))
        for a, why in rejected:
            store.log_event(conn, ACTOR, "review_action_rejected", action=a.model_dump(), why=why)
        changes = [x for x in applied if not x.startswith("complete")]
        if changes:
            hyps = conn.execute("SELECT hypotheses_json FROM plan_versions ORDER BY version DESC LIMIT 1").fetchone()[0]
            conn.execute("INSERT INTO plan_versions VALUES (?, ?, ?, ?, ?)",
                         (version, new_round, "; ".join(changes), hyps, time.time()))
            store.log_event(conn, ACTOR, "plan_version", version=version, reason="; ".join(changes))
        conn.execute("UPDATE run SET review_round = ? WHERE id = 1", (new_round,))
        store.log_event(conn, ACTOR, "review", round=new_round, applied=applied, rejected=len(rejected))
        if new_work:
            store.set_phase(conn, "gather")
        else:
            start_write(ctx)
    log(f"review round {new_round}: {applied or ['no actions']}")


# --------------------------------------------------------------------------- write / assemble

def diagnose(conn, sid: int) -> str:
    q = lambda sql: store.count(conn, sql, (sid,))
    ex_total = q("SELECT COUNT(*) FROM tasks WHERE kind = 'extract' AND subq_id = ?")
    ex_bad = q("SELECT COUNT(*) FROM tasks WHERE kind = 'extract' AND subq_id = ? AND status IN ('failed', 'cancelled')")
    search_bad = q("SELECT COUNT(*) FROM tasks WHERE kind = 'search' AND subq_id = ? AND status IN ('failed', 'cancelled')")
    parts = [f"{q('SELECT COUNT(*) FROM candidates WHERE subq_id = ?')} documents selected by search",
             f"{ex_bad} of {ex_total} extract tasks failed or were cancelled",
             f"{q('SELECT COUNT(*) FROM claims WHERE subq_id = ? AND verified = 0')} claims rejected by quote verification"]
    if search_bad:
        parts.insert(0, f"{search_bad} search tasks failed or were cancelled")
    return "Insufficient evidence: no quote-verified claims (" + "; ".join(parts) + ")."


def start_write(ctx: store.Ctx) -> None:
    conn = ctx.conn
    with store.tx(conn):
        for s in active_subqs(conn):
            sid = s["subq_id"]
            if not store.count(conn, "SELECT COUNT(*) FROM claims WHERE subq_id = ? AND verified = 1", (sid,)):
                conn.execute("UPDATE subquestions SET status = 'insufficient', verdict = 'thin', note = ? WHERE subq_id = ?",
                             (diagnose(conn, sid), sid))
                continue
            if s["status"] in ("gathering", "checking") or s["verdict"] is None:  # never (re-)checked
                floor = tools.compute_verdict(conn, sid)
                conn.execute("UPDATE subquestions SET verdict = ?, verdict_rationale = ? WHERE subq_id = ?",
                             (floor["verdict"], "Verdict computed deterministically (cross-check skipped).", sid))
            store.enqueue_task(conn, kind="write_section", role="synthesize", subq_id=sid,
                               plan_version=plan_version(conn), inputs={"subq_text": s["text"]},
                               dedupe_key=f"write:{sid}")
            conn.execute("UPDATE subquestions SET status = 'writing' WHERE subq_id = ?", (sid,))
        store.set_phase(conn, "write")
    log("-> write")


def phase_write(ctx: store.Ctx) -> None:
    conn = ctx.conn
    if open_count(conn, ("write_section",)):
        return
    with store.tx(conn):
        for s in conn.execute("SELECT subq_id FROM subquestions WHERE status = 'writing'").fetchall():
            has = store.count(conn, "SELECT COUNT(*) FROM sections WHERE subq_id = ?", (s["subq_id"],))
            conn.execute("UPDATE subquestions SET status = ?, note = ? WHERE subq_id = ?",
                         ("written" if has else "insufficient",
                          None if has else "The section writer failed; the verified evidence is listed instead.",
                          s["subq_id"]))
        store.set_phase(conn, "assemble")
    log("write barrier reached -> assemble")


def phase_assemble(ctx: store.Ctx) -> None:
    conn = ctx.conn
    run = store.get_run(conn)
    bottom = None
    if store.count(conn, "SELECT COUNT(*) FROM sections"):
        try:
            bottom = planner_call(ctx, BottomLine, "planner.assemble", memory.for_assemble(conn),
                                  f"Research question: {run['question']}").text.strip()
        except (TransientError, DeterministicError) as err:
            store.log_event(conn, ACTOR, "planner_fallback", step="planner.assemble", error=str(err)[:300])
    if not bottom:
        bottom = "Automatic summary (the planner could not write one): " + "; ".join(
            f"SQ{s['subq_id']} is {s['verdict'] or 'thin'}" for s in active_subqs(conn)) + "."
    store.log_event(conn, ACTOR, "bottom_line", text=bottom)  # kept so the brief can be re-rendered
    conn.execute("UPDATE run SET finished_at = ? WHERE id = 1", (time.time(),))
    out = store.run_dir(ctx.run_id)
    (out / "brief.md").write_text(render_brief(conn, bottom))
    (out / "run_report.md").write_text(render_report(conn))
    store.set_phase(conn, "degraded" if store.get_run(conn)["degrade_reason"] else "done")
    log(f"brief written to {out / 'brief.md'}")


# --------------------------------------------------------------------------- budgets + degradation

def degrade(ctx: store.Ctx, reason: str) -> None:
    conn = ctx.conn
    with store.tx(conn):
        conn.execute("UPDATE run SET degrade_reason = COALESCE(degrade_reason, ?) WHERE id = 1", (reason,))
        n = store.cancel_open_tasks(conn, RESEARCH_KINDS, f"cancelled: {reason}")
        store.log_event(conn, ACTOR, "degrade", reason=reason, cancelled_tasks=n)
        start_write(ctx)
    log(f"DEGRADED: {reason} (cancelled {n} open tasks)")


def check_limits(ctx: store.Ctx) -> None:
    conn, cfg = ctx.conn, ctx.cfg
    run = store.get_run(conn)
    now, phase = time.time(), run["phase"]
    if phase in ("plan", "gather", "crosscheck", "review") and not run["degrade_reason"]:
        n_tasks = store.count(conn, "SELECT COUNT(*) FROM tasks")
        n_llm = store.count(conn, "SELECT COUNT(*) FROM llm_calls")
        reason = None
        if n_tasks >= cfg.budget_tasks:
            reason = f"task budget reached ({n_tasks}/{cfg.budget_tasks})"
        elif n_llm >= cfg.budget_llm_calls:
            reason = f"LLM call budget reached ({n_llm}/{cfg.budget_llm_calls})"
        elif now - run["session_started_at"] >= cfg.budget_wall_s:
            reason = f"wall-clock budget reached ({cfg.budget_wall_s / 60:.0f} min)"
        elif phase in ("gather", "crosscheck") and now - run["phase_started_at"] >= cfg.phase_timeout_s:
            reason = f"phase '{phase}' timed out after {cfg.phase_timeout_s / 60:.0f} min"
        if reason:
            degrade(ctx, reason)
    elif phase == "write" and now - run["phase_started_at"] >= cfg.phase_timeout_s:
        with store.tx(conn):
            conn.execute("UPDATE run SET degrade_reason = COALESCE(degrade_reason, ?) WHERE id = 1",
                         ("phase 'write' timed out",))
            store.cancel_open_tasks(conn, ("write_section",), "cancelled: write phase timed out")
        phase_write(ctx)


PHASES = {"plan": phase_plan, "gather": phase_gather, "crosscheck": phase_crosscheck, "review": phase_review,
          "write": phase_write, "assemble": phase_assemble}


def main(run_id: str) -> None:
    ctx = store.open_ctx(run_id, ACTOR)
    store.log_event(ctx.conn, ACTOR, "orchestrator_started", phase=store.get_run(ctx.conn)["phase"])
    log(f"started in phase {store.get_run(ctx.conn)['phase']}")
    while (phase := store.get_run(ctx.conn)["phase"]) not in store.FINAL_PHASES:
        supervise(ctx)
        check_limits(ctx)
        PHASES[store.get_run(ctx.conn)["phase"]](ctx)
        time.sleep(ctx.cfg.supervisor_tick_s)
    log(f"finished: {phase}")


# --------------------------------------------------------------------------- rendering

def _cite(conn, claim_id: int) -> Optional[str]:
    c = conn.execute("SELECT doc_id, unit FROM claims WHERE claim_id = ?", (claim_id,)).fetchone()
    return f"{c['doc_id']}, {memory.location(conn, c['doc_id'], c['unit'])}" if c else None


def render_citations(conn, text: str, subq_id: int) -> str:
    """[C12] / [C12, C15] -> [doc, p.N]. Bare claim ids a model leaked into prose ('C25') are
    rendered too, but only when they are real claim ids of this sub-question."""
    text = tools.split_grouped_citations(text or "")
    text = tools.CITATION.sub(lambda m: f"[{_cite(conn, int(m.group(1))) or m.group(0)}]", text)
    own = {r[0] for r in conn.execute("SELECT claim_id FROM claims WHERE subq_id = ?", (subq_id,))}
    return re.sub(r"\bC(\d+)\b", lambda m: _cite(conn, int(m.group(1))) if int(m.group(1)) in own else m.group(0), text)


def render_brief(conn, bottom_line: str) -> str:
    run = store.get_run(conn)
    n_docs = store.count(conn, "SELECT COUNT(*) FROM corpus.documents")
    lines = [f"# {run['question']}", "",
             f"*Research brief written by a multi-agent system from a local corpus of {n_docs} documents "
             f"(run `{run['run_id']}`). Every citation points to a quote that was machine-checked against "
             f"the source; quotes are listed in the evidence appendix.*", ""]
    if run["degrade_reason"]:
        lines += [f"> **Degraded run:** {run['degrade_reason']}. The brief uses only the evidence verified "
                  "before the cut-off.", ""]
    lines += ["## Bottom line", "", bottom_line, "", "## Hypotheses considered", ""]
    lines += [f"- `{h['id']}`: {h['description']}" for h in memory.hypotheses(conn)]
    lines += ["", "## Evidence by sub-question", "",
              "| # | Sub-question | Verdict | Verified claims | Independent primary sources |", "|---|---|---|---|---|"]
    subqs = active_subqs(conn)
    for s in subqs:
        floor = tools.compute_verdict(conn, s["subq_id"])
        lines.append(f"| SQ{s['subq_id']} | {s['text']} | {s['verdict'] or 'thin'} | {floor['n_verified']} | "
                     f"{len(floor['independent_primary_sources'])} |")
    cited: set[int] = set()
    for s in subqs:
        lines += ["", f"### SQ{s['subq_id']}. {s['text']}", "",
                  f"**Verdict: {s['verdict'] or 'thin'}.** "
                  f"{render_citations(conn, s['verdict_rationale'], s['subq_id'])}".strip(), ""]
        section = conn.execute("SELECT markdown, cited_claim_ids_json FROM sections WHERE subq_id = ? "
                               "ORDER BY version DESC LIMIT 1", (s["subq_id"],)).fetchone()
        if section:
            markdown = tools.split_grouped_citations(section["markdown"])
            cited.update(int(x) for x in tools.CITATION.findall(markdown))
            lines.append(render_citations(conn, markdown, s["subq_id"]))
        else:
            if s["note"]:
                lines.append(f"> {s['note']}")
            for c in memory.verified_claims(conn, s["subq_id"], 10):
                cited.add(c["claim_id"])
                lines.append(f"- {c['text']} [{c['doc_id']}, {memory.location(conn, c['doc_id'], c['unit'])}]")
    lines += ["", "## Evidence appendix (verified quotes)", ""]
    for cid in sorted(cited):
        c = conn.execute("SELECT * FROM claims WHERE claim_id = ?", (cid,)).fetchone()
        if c:
            lines.append(f"- **[{c['doc_id']}, {memory.location(conn, c['doc_id'], c['unit'])}]** "
                         f"({c['stance']}, {c['verify_method']} match): \"{c['quote']}\"")
    docs = sorted({conn.execute("SELECT doc_id FROM claims WHERE claim_id = ?", (cid,)).fetchone()[0] for cid in cited})
    lines += ["", "## Sources", ""]
    for d in docs:
        m = tools.get_doc(conn, d)
        lines.append(f"- `{d}`: {m['title']} ({m['year']}). {m['venue']}.")
    return "\n".join(lines) + "\n"


def render_report(conn) -> str:
    run = store.get_run(conn)
    ev = lambda kind: store.count(conn, "SELECT COUNT(*) FROM events WHERE kind = ?", (kind,))
    n_llm = store.count(conn, "SELECT COUNT(*) FROM llm_calls")
    n_tools = ev("tool_call")
    duration = (run["finished_at"] or time.time()) - run["created_at"]
    lines = [f"# Run report: {run['run_id']}", "", f"**Question:** {run['question']}", "",
             f"- Final status: **{'degraded' if run['degrade_reason'] else 'done'}**"
             + (f" ({run['degrade_reason']})" if run["degrade_reason"] else ""),
             f"- Duration: {duration / 60:.1f} min",
             f"- Steps: **{n_llm + n_tools}** ({n_llm} LLM calls + {n_tools} tool calls)",
             f"- Models: {json.loads(run['config_json'])['models']}", "", "## Timeline", ""]
    for e in conn.execute("SELECT ts, detail_json FROM events WHERE kind = 'phase_change' ORDER BY event_id"):
        lines.append(f"- t+{(e['ts'] - run['created_at']) / 60:5.1f} min: {json.loads(e['detail_json'])['phase']}")

    lines += ["", "## Plan history", ""]
    for p in conn.execute("SELECT * FROM plan_versions ORDER BY version"):
        lines.append(f"- **v{p['version']}** (review round {p['review_round']}): {p['reason']}")
    lines += ["", "## Sub-questions", "", "| # | Status | Verdict | Verified | Rejected | Docs read |", "|---|---|---|---|---|---|"]
    for s in conn.execute("SELECT * FROM subquestions ORDER BY subq_id"):
        sid = s["subq_id"]
        lines.append(f"| SQ{sid} | {s['status']} | {s['verdict'] or '-'} | "
                     f"{store.count(conn, 'SELECT COUNT(*) FROM claims WHERE subq_id = ? AND verified = 1', (sid,))} | "
                     f"{store.count(conn, 'SELECT COUNT(*) FROM claims WHERE subq_id = ? AND verified = 0', (sid,))} | "
                     f"{store.count(conn, 'SELECT COUNT(*) FROM candidates WHERE subq_id = ?', (sid,))} |")

    lines += ["", "## Tasks", "", "| Kind | Done | Failed | Cancelled | Retried |", "|---|---|---|---|---|"]
    for kind in ("search", "extract", "cross_check", "write_section"):
        c = lambda status: store.count(conn, "SELECT COUNT(*) FROM tasks WHERE kind = ? AND status = ?", (kind, status))
        retried = store.count(conn, "SELECT COUNT(*) FROM tasks WHERE kind = ? AND attempts > 1", (kind,))
        lines.append(f"| {kind} | {c('done')} | {c('failed')} | {c('cancelled')} | {retried} |")

    lines += ["", "## Failures and recovery", "", "| Event | Count |", "|---|---|"]
    for kind in ("task_retry", "task_failed", "lease_expired", "hard_timeout", "worker_dead", "stale_result_discarded",
                 "chaos", "schema_invalid", "schema_repaired", "planner_retry", "planner_fallback",
                 "verdict_downgraded", "verdict_upgrade_ignored", "review_action_rejected", "degrade"):
        lines.append(f"| {kind} | {ev(kind)} |")
    for e in conn.execute("SELECT * FROM events WHERE kind = 'chaos' ORDER BY event_id"):
        t = conn.execute("SELECT status, owner, attempts FROM tasks WHERE task_id = ?", (e["task_id"],)).fetchone()
        last = conn.execute("SELECT actor FROM events WHERE kind = 'task_done' AND task_id = ?", (e["task_id"],)).fetchone()
        lines.append(f"- chaos `{json.loads(e['detail_json'])['fault']}` in task {e['task_id']} ({e['actor']}): "
                     f"task ended **{t['status'] if t else '?'}** after {t['attempts'] if t else '?'} attempts"
                     + (f", completed by {last['actor']}" if last else ""))

    total = store.count(conn, "SELECT COUNT(*) FROM claims")
    lines += ["", "## Verification", ""]
    for m in ("exact", "fuzzy", "neighbor", "rejected"):
        lines.append(f"- {m}: {store.count(conn, 'SELECT COUNT(*) FROM claims WHERE verify_method = ?', (m,))}")
    rejected = store.count(conn, "SELECT COUNT(*) FROM claims WHERE verified = 0")
    lines.append(f"- Rejection rate (quotes that could not be found in the source): "
                 f"{rejected}/{total} = {100 * rejected / total:.0f}%" if total else "- No claims extracted.")

    lines += ["", "## Context size over the run", "",
              "No agent carries a transcript, so a call's prompt size should depend on its step type, not on how "
              "far into the run it happens. Within each step type, early and late calls should look the same:", "",
              "| Step | Calls | First half avg prompt tokens | Second half avg | Max | Avg latency (s) |",
              "|---|---|---|---|---|---|"]
    for step in [r[0] for r in conn.execute("SELECT step FROM llm_calls GROUP BY step ORDER BY MIN(call_id)")]:
        rows = conn.execute("SELECT prompt_tokens, latency_ms FROM llm_calls WHERE step = ? AND prompt_tokens IS NOT NULL "
                            "ORDER BY call_id", (step,)).fetchall()
        if not rows:
            continue
        toks = [r[0] for r in rows]
        half = max(1, len(toks) // 2)
        first, second = toks[:half], toks[half:] or toks[:half]
        lines.append(f"| {step} | {len(toks)} | {sum(first) / len(first):.0f} | {sum(second) / len(second):.0f} | "
                     f"{max(toks)} | {sum(r[1] for r in rows) / len(rows) / 1000:.1f} |")
    return "\n".join(lines) + "\n"
