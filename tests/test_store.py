"""Task-queue guarantees in store.py: no double claims across connections, lease expiry ->
re-queue, fenced completion (a stale worker's result is discarded), retry budgets per error
class, heartbeats keeping slow tasks alive, and the shared rate limiter."""
import json
import sqlite3
import threading
import time

import pytest

import config
from agent import store


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "CORPUS_DB", tmp_path / "corpus.db")
    c = sqlite3.connect(tmp_path / "corpus.db")
    c.executescript(store.CORPUS_SCHEMA)
    c.close()
    cfg = config.Config(lease_s=0.2, transient_backoff_s=(0, 0, 0))
    store.create_run("r1", "question?", cfg)
    return "r1", cfg


def connect(run_id):
    return store.connect(store.state_path(run_id))


def enqueue(conn, key, role="extract"):
    return store.enqueue_task(conn, kind=role, role=role, inputs={}, dedupe_key=key)


def test_concurrent_claims_never_double_claim(run):
    run_id, cfg = run
    conn = connect(run_id)
    ids = [enqueue(conn, f"k{i}") for i in range(40)]
    claimed, lock = [], threading.Lock()

    def worker(name):
        c = connect(run_id)  # separate connection = separate SQLite lock holder
        while (t := store.claim_task(c, "extract", name, cfg)) is not None:
            with lock:
                claimed.append(t["task_id"])

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(claimed) == ids


def test_duplicate_dedupe_key_is_rejected(run):
    conn = connect(run[0])
    assert enqueue(conn, "same") is not None
    assert enqueue(conn, "same") is None


def test_claims_only_matching_role(run):
    run_id, cfg = run
    conn = connect(run_id)
    enqueue(conn, "s1", role="search")
    assert store.claim_task(conn, "extract", "w", cfg) is None
    assert store.claim_task(conn, "search", "w", cfg) is not None


def test_expired_lease_requeues_and_stale_result_is_fenced(run):
    run_id, cfg = run
    conn = connect(run_id)
    tid = enqueue(conn, "k")
    store.claim_task(conn, "extract", "A", cfg)
    time.sleep(0.3)  # A dies silently: no heartbeat
    assert store.reap_tasks(conn, cfg) == [{"task_id": tid, "reason": "lease_expired", "status": "pending"}]
    b = store.claim_task(conn, "extract", "B", cfg)
    assert b["task_id"] == tid and b["attempts"] == 2

    wrote = []
    assert store.complete_task(conn, tid, "A", {"by": "A"}, writes=lambda c: wrote.append("A")) is False
    assert store.complete_task(conn, tid, "B", {"by": "B"}, writes=lambda c: wrote.append("B")) is True
    assert wrote == ["B"]
    row = conn.execute("SELECT status, result_json FROM tasks WHERE task_id = ?", (tid,)).fetchone()
    assert row["status"] == "done" and json.loads(row["result_json"]) == {"by": "B"}


def test_heartbeat_keeps_slow_task_alive(run):
    run_id, cfg = run
    conn = connect(run_id)
    tid = enqueue(conn, "slow")
    store.claim_task(conn, "extract", "A", cfg)
    for _ in range(5):  # 0.5 s of work with a 0.2 s lease
        time.sleep(0.1)
        assert store.heartbeat(conn, tid, "A", cfg)
    assert store.reap_tasks(conn, cfg) == []
    assert conn.execute("SELECT status FROM tasks WHERE task_id = ?", (tid,)).fetchone()[0] == "claimed"


@pytest.mark.parametrize("error_class, attempts_before_failure", [("transient", 4), ("deterministic", 2)])
def test_retry_budget_per_error_class(run, error_class, attempts_before_failure):
    run_id, cfg = run
    conn = connect(run_id)
    tid = enqueue(conn, "flaky")
    statuses = []
    for _ in range(attempts_before_failure):
        task = store.claim_task(conn, "extract", "w", cfg)
        assert task is not None
        statuses.append(store.fail_task(conn, tid, "w", error_class, "boom", cfg))
    assert statuses == ["pending"] * (attempts_before_failure - 1) + ["failed"]
    assert store.claim_task(conn, "extract", "w", cfg) is None


def test_rate_limiter_spaces_calls(run):
    run_id, _ = run
    conn = connect(run_id)
    cfg = config.Config(rate_limit_rpm=600, rate_limit_burst=2)  # 10 per second after a burst of 2
    conn.execute("UPDATE rate_limiter SET tokens = 2, refilled_at = ?", (time.time(),))
    start = time.time()
    for _ in range(4):
        store.acquire_rate_token(conn, cfg)
    assert time.time() - start >= 0.15
