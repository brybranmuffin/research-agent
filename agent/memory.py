"""Memory views: pull the relevant facts from the datastores for one agent step.

Component: Workspace context + memory retrieval (Tier 2). Views are computed, never accumulated:
no agent sees a transcript of earlier calls, so prompt size stays flat however long the run is.
Skipped for now: compaction/summarization and long-term (cross-run) memory.

Rules for every view: settled facts only (completed tasks, verified claims), deterministic
ordering by id, hard caps with "... N more" markers. Reads state.db and the attached corpus.db;
never reads eval/corpus_labels.json.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Optional

from agent import store


def _cap(lines: list[str], n: int, what: str) -> list[str]:
    return lines[:n] + ([f"... {len(lines) - n} more {what}"] if len(lines) > n else [])


def hypotheses(conn: sqlite3.Connection) -> list[dict]:
    row = conn.execute("SELECT hypotheses_json FROM plan_versions ORDER BY version DESC LIMIT 1").fetchone()
    return json.loads(row[0]) if row else []


def hypothesis_ids(conn: sqlite3.Connection) -> list[str]:
    return [h["id"] for h in hypotheses(conn)]


def _hypotheses_block(conn: sqlite3.Connection) -> str:
    lines = [f"- {h['id']}: {h['description']}" for h in hypotheses(conn)]
    return "Hypotheses (claim stances):\n" + "\n".join(lines + ["- neutral: background, or takes no side"])


def location(conn: sqlite3.Connection, doc_id: str, unit: Optional[int]) -> str:
    doc = conn.execute("SELECT type, unit_labels_json FROM corpus.documents WHERE doc_id = ?", (doc_id,)).fetchone()
    if doc is None or unit is None:
        return "?"
    if doc["type"] == "pdf":
        return f"p.{unit}"
    labels = json.loads(doc["unit_labels_json"] or "[]")
    return f"§{unit} {labels[unit - 1]}" if 0 < unit <= len(labels) else f"§{unit}"


def found_docs(conn: sqlite3.Connection, subq_id: int) -> list[str]:
    return [r[0] for r in conn.execute("SELECT doc_id FROM candidates WHERE subq_id = ? ORDER BY doc_id", (subq_id,))]


def verified_claims(conn: sqlite3.Connection, subq_id: int, cap: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM claims WHERE subq_id = ? AND verified = 1 ORDER BY claim_id LIMIT ?", (subq_id, cap)).fetchall()


def claims_block(conn: sqlite3.Connection, subq_id: int, cap: int) -> tuple[str, dict[int, sqlite3.Row]]:
    rows = verified_claims(conn, subq_id, cap)
    total = store.count(conn, "SELECT COUNT(*) FROM claims WHERE subq_id = ? AND verified = 1", (subq_id,))
    lines = [f"[C{r['claim_id']}] {r['doc_id']} {location(conn, r['doc_id'], r['unit'])} (stance: {r['stance']}): "
             f"{r['text']}\n    quote: \"{r['quote'][:300]}\"" for r in rows]
    if total > len(rows):
        lines.append(f"... {total - len(rows)} more verified claims not shown")
    return "Verified claims:\n" + ("\n".join(lines) if lines else "(none)"), {r["claim_id"]: r for r in rows}


# --------------------------------------------------------------------------- planner views

def for_plan(conn: sqlite3.Connection) -> str:
    docs = conn.execute("SELECT doc_id, year, source_kind, title FROM corpus.documents "
                        "ORDER BY source_kind, year, doc_id").fetchall()
    lines = _cap([f"- {d['doc_id']} ({d['year']}, {d['source_kind']}): {d['title']}" for d in docs], 150, "documents")
    return f"Local corpus ({len(docs)} documents; workers can search and read ONLY these):\n" + "\n".join(lines)


def subquestion_summary(conn: sqlite3.Connection, s: sqlite3.Row) -> str:
    sid = s["subq_id"]
    by_stance = conn.execute(
        "SELECT stance, GROUP_CONCAT(DISTINCT doc_id) AS docs, COUNT(*) AS n FROM claims "
        "WHERE subq_id = ? AND verified = 1 GROUP BY stance ORDER BY stance", (sid,)).fetchall()
    rejected = store.count(conn, "SELECT COUNT(*) FROM claims WHERE subq_id = ? AND verified = 0", (sid,))
    failed = store.count(conn, "SELECT COUNT(*) FROM tasks WHERE subq_id = ? AND status = 'failed'", (sid,))
    evidence = "; ".join(f"{r['stance']}: {r['n']} claims from {r['docs']}" for r in by_stance) or "no verified claims"
    lines = [f"SQ{sid} [{s['status']}] verdict={s['verdict'] or 'pending'}: {s['text']}",
             f"    evidence: {evidence}; {rejected} claims rejected by quote check; {failed} failed tasks",
             f"    documents read: {', '.join(found_docs(conn, sid)) or 'none'}"]
    if s["verdict_rationale"]:
        lines.append(f"    cross-check: {s['verdict_rationale'][:400]}")
    return "\n".join(lines)


def for_review(conn: sqlite3.Connection) -> str:
    run = store.get_run(conn)
    goal = json.loads(run["goal_json"] or "{}")
    version = conn.execute("SELECT MAX(version) FROM plan_versions").fetchone()[0]
    active = conn.execute("SELECT * FROM subquestions WHERE retired_in_version IS NULL ORDER BY subq_id").fetchall()
    retired = conn.execute("SELECT subq_id, text FROM subquestions WHERE retired_in_version IS NOT NULL").fetchall()
    budgets = json.loads(run["config_json"])
    n_tasks = store.count(conn, "SELECT COUNT(*) FROM tasks")
    n_llm = store.count(conn, "SELECT COUNT(*) FROM llm_calls")
    parts = [f"Goal: {goal.get('goal', run['question'])}",
             _hypotheses_block(conn),
             f"Plan version {version}, review round {run['review_round'] + 1} of {budgets['max_review_rounds']}.",
             "Sub-questions:", *[subquestion_summary(conn, s) for s in active]]
    if retired:
        parts.append("Retired: " + "; ".join(f"SQ{r['subq_id']} {r['text']}" for r in retired))
    parts.append(f"Budget used: {n_tasks}/{budgets['budget_tasks']} tasks, "
                 f"{n_llm}/{budgets['budget_llm_calls']} LLM calls.")
    return "\n".join(parts)


def for_assemble(conn: sqlite3.Connection) -> str:
    run = store.get_run(conn)
    parts = [f"Research question: {run['question']}", _hypotheses_block(conn)]
    for s in conn.execute("SELECT * FROM subquestions WHERE retired_in_version IS NULL ORDER BY subq_id"):
        section = conn.execute("SELECT markdown FROM sections WHERE subq_id = ? ORDER BY version DESC LIMIT 1",
                               (s["subq_id"],)).fetchone()
        body = section["markdown"] if section else (s["note"] or "No section was written.")
        parts.append(f"SQ{s['subq_id']} ({s['verdict'] or 'no verdict'}): {s['text']}\n{body}")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- worker views

def for_search(conn: sqlite3.Connection, subq_id: int) -> str:
    found = found_docs(conn, subq_id)
    return "\n".join([_hypotheses_block(conn),
                      "Documents already selected for this sub-question (do not aim for these again): "
                      + (", ".join(found) if found else "none")])


def for_extract(conn: sqlite3.Connection, subq_id: int, doc_id: str) -> str:
    d = conn.execute("SELECT * FROM corpus.documents WHERE doc_id = ?", (doc_id,)).fetchone()
    return "\n".join([_hypotheses_block(conn),
                      f"Document: {d['doc_id']} | {d['title']} | {d['year']} | {d['venue']} | {d['source_kind']} source"])


def for_cross_check(conn: sqlite3.Connection, subq_id: int, floor: dict, cap: int) -> str:
    block, _ = claims_block(conn, subq_id, cap)
    rules = ("Floor rules: contested = verified claims for >= 2 different hypotheses; supported = >= 2 independent "
             "primary sources (distinct first authors) and no opposing claim; thin = otherwise. "
             "Web pages never count as independent sources.")
    return "\n".join([_hypotheses_block(conn), rules,
                      f"Verdict floor: {floor['verdict']} (independent primary sources: "
                      f"{', '.join(floor['independent_primary_sources']) or 'none'})", block])


def for_write(conn: sqlite3.Connection, subq_id: int, cap: int) -> tuple[str, dict[int, sqlite3.Row]]:
    s = conn.execute("SELECT * FROM subquestions WHERE subq_id = ?", (subq_id,)).fetchone()
    block, claims = claims_block(conn, subq_id, cap)
    return "\n".join([_hypotheses_block(conn),
                      f"Verdict: {s['verdict'] or 'thin'}. Cross-check rationale: {s['verdict_rationale'] or 'n/a'}",
                      block]), claims
