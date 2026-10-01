"""Orchestrator rules that do not need an LLM: planner review actions are validated and applied
by code (limits, unknown ids, duplicates), every applied revision becomes a new plan version,
and degradation cancels open research and moves straight to writing."""
import json
import sqlite3
import time
import warnings

import pytest

import config
from agent import store

with warnings.catch_warnings():
    warnings.simplefilter("ignore")  # ChatNVIDIA warns when no API key is set; no network is used
    from agent import orchestrator_agent as orch


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "CORPUS_DB", tmp_path / "corpus.db")
    c = sqlite3.connect(tmp_path / "corpus.db")
    c.executescript(store.CORPUS_SCHEMA)
    c.close()
    store.create_run("r1", "Was it aquatic?", config.Config())
    ctx = store.open_ctx("r1", "orchestrator")
    conn = ctx.conn
    conn.execute("INSERT INTO plan_versions VALUES (1, 0, 'initial plan', ?, ?)",
                 (json.dumps([{"id": "aquatic", "description": "a"}, {"id": "wading", "description": "w"}]), time.time()))
    for text in ("Tail?", "Bones?", "Isotopes?"):
        conn.execute("INSERT INTO subquestions (text, added_in_version, status, verdict) VALUES (?, 1, 'checked', 'thin')",
                     (text,))
    store.set_phase(conn, "review")
    return ctx


def action(**kw):
    return orch.ReviewAction(**{"reason": "because", **kw})


def test_review_actions_are_validated_and_versioned(ctx):
    decision = orch.ReviewDecision(actions=[
        action(type="more_search", subq_id=1, text="caudal vertebrae"),
        action(type="more_search", subq_id=99),                      # unknown sub-question
        action(type="add_subquestion", text="Is the bone-density method valid?"),
        action(type="add_subquestion", text="Were the nostrils retracted?"),
        action(type="add_subquestion", text="A third new question?"),  # over the per-round limit
        action(type="teleport"),                                      # not an action type
    ])
    orch.apply_review(ctx, decision, max_new=2)
    conn = ctx.conn
    assert store.get_run(conn)["phase"] == "gather"
    assert store.get_run(conn)["review_round"] == 1
    assert store.count(conn, "SELECT COUNT(*) FROM subquestions") == 5
    assert store.count(conn, "SELECT COUNT(*) FROM tasks WHERE kind = 'search' AND round = 1") == 3
    assert store.count(conn, "SELECT COUNT(*) FROM events WHERE kind = 'review_action_rejected'") == 3
    v2 = conn.execute("SELECT reason FROM plan_versions WHERE version = 2").fetchone()[0]
    assert "more_search SQ1" in v2 and "add SQ4" in v2 and "add SQ5" in v2


def test_duplicate_subquestion_is_rejected(ctx):
    orch.apply_review(ctx, orch.ReviewDecision(actions=[action(type="add_subquestion", text="  tail?  ")]), max_new=2)
    assert store.count(ctx.conn, "SELECT COUNT(*) FROM subquestions") == 3
    assert store.get_run(ctx.conn)["phase"] == "write"  # nothing new to research


def test_complete_moves_to_write_without_new_plan_version(ctx):
    orch.apply_review(ctx, orch.ReviewDecision(actions=[action(type="complete")]), max_new=2)
    assert store.get_run(ctx.conn)["phase"] == "write"
    assert store.count(ctx.conn, "SELECT COUNT(*) FROM plan_versions") == 1


def test_degrade_cancels_research_and_writes_with_what_is_verified(ctx):
    conn = ctx.conn
    store.enqueue_task(conn, kind="extract", role="extract", subq_id=1, inputs={}, dedupe_key="e1")
    orch.degrade(ctx, "LLM call budget reached")
    run = store.get_run(conn)
    assert run["phase"] == "write" and run["degrade_reason"] == "LLM call budget reached"
    assert conn.execute("SELECT status FROM tasks WHERE dedupe_key = 'e1'").fetchone()[0] == "cancelled"
    # no verified claims anywhere -> every sub-question is explicitly marked insufficient, with the cause
    notes = [r[0] for r in conn.execute("SELECT note FROM subquestions WHERE status = 'insufficient'")]
    assert len(notes) == 3 and all(n.startswith("Insufficient evidence") for n in notes)


def test_resume_reaps_orphaned_tasks_immediately(ctx):
    conn = ctx.conn
    tid = store.enqueue_task(conn, kind="search", role="search", subq_id=1, inputs={}, dedupe_key="s1")
    store.register_worker(conn, "search-1", "search", 123)
    store.claim_task(conn, "search", "search-1", ctx.cfg)
    store.prepare_resume(conn)  # the previous session's processes are gone
    assert store.reap_tasks(conn, ctx.cfg)[0]["task_id"] == tid
    assert conn.execute("SELECT status FROM workers WHERE worker_id = 'search-1'").fetchone()[0] == "dead"
