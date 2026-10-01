"""Process entry point. Every OS process in the system starts here; all logic is imported.

  python run.py                                  # run the demo question
  python run.py "<question>" [--chaos]           # create a run, spawn all processes, wait
  python run.py --resume <run_id>                # respawn processes against an existing state.db
  python run.py replay <run_id> [--speed N]      # playback: print a recorded run's trace, no models
  python run.py ingest                           # (re)build corpus.db from corpus/raw + manifest
  python run.py orchestrator --run <run_id>      # (spawned) orchestrator process
  python run.py worker --role <role> --run <run_id> --id <worker_id>   # (spawned) worker process

The launcher spawns the orchestrator plus search x1, extract x2, synthesize x1 as separate OS
processes (re-invoking this file with a subcommand), prints live progress from state.db, waits
for done/degraded, and points at the brief and the run report. It never restarts a process:
recovery (lease expiry, re-queueing) is the orchestrator's job.

Replay is playback only: it reads events + llm_calls from runs/<run_id>/state.db and prints the
run in order, as if it were happening. Nothing executes and no model is called.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

import config

DEFAULT_QUESTION = "Was Spinosaurus an aquatic pursuit predator, and how strong is the evidence?"
NOTABLE = ("plan_version", "review", "verdict", "lease_expired", "hard_timeout", "worker_dead", "chaos",
           "task_failed", "degrade", "planner_fallback", "stale_result_discarded")


# --------------------------------------------------------------------------- launcher

def _spawn(run_id: str, name: str, args: list[str]) -> subprocess.Popen:
    from agent import store
    logs = store.run_dir(run_id) / "logs"
    logs.mkdir(exist_ok=True)
    out = open(logs / f"{name}.log", "a")
    return subprocess.Popen([sys.executable, str(config.ROOT / "run.py"), *args], stdout=out,
                            stderr=subprocess.STDOUT, cwd=config.ROOT, env={**os.environ, "PYTHONUNBUFFERED": "1"})


def _status(conn, started: float) -> str:
    from agent import store
    run = store.get_run(conn)
    by = dict(conn.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status").fetchall())
    claims = conn.execute("SELECT COUNT(*), COALESCE(SUM(verified), 0) FROM claims").fetchone()
    return (f"[{(time.time() - started) / 60:4.1f} min] phase={run['phase']:<10} tasks: "
            f"{by.get('pending', 0)} queued, {by.get('claimed', 0)} running, {by.get('done', 0)} done, "
            f"{by.get('failed', 0)} failed | LLM calls {store.count(conn, 'SELECT COUNT(*) FROM llm_calls')} | "
            f"claims verified {claims[1]}/{claims[0]}")


def _describe(e) -> str:
    d = json.loads(e["detail_json"] or "{}")
    return {
        "plan_version": lambda: f"plan v{d.get('version')}: {d.get('reason')}",
        "review": lambda: f"review round {d.get('round')}: {d.get('applied') or 'no actions'}",
        "verdict": lambda: f"SQ{e['subq_id']} verdict: {d.get('verdict')}",
        "lease_expired": lambda: f"task {e['task_id']} lease expired (owner {d.get('previous_owner')}) -> {d.get('new_status')}",
        "hard_timeout": lambda: f"task {e['task_id']} hit the hard timeout -> {d.get('new_status')}",
        "worker_dead": lambda: f"worker {d.get('worker')} (pid {d.get('pid')}) presumed dead; held {d.get('held_tasks')}",
        "chaos": lambda: f"CHAOS injected: {d.get('fault')} in task {e['task_id']}",
        "task_failed": lambda: f"task {e['task_id']} failed permanently ({d.get('error_class')}): {str(d.get('error'))[:100]}",
        "degrade": lambda: f"DEGRADED: {d.get('reason')} (cancelled {d.get('cancelled_tasks')} tasks)",
        "planner_fallback": lambda: f"planner fallback at {d.get('step')}: {str(d.get('error'))[:100]}",
        "stale_result_discarded": lambda: f"stale result for task {e['task_id']} discarded (fenced)",
    }.get(e["kind"], lambda: e["kind"])()


def launch(run_id: str) -> int:
    from agent import store
    conn = store.connect(store.state_path(run_id), attach_corpus=False)
    cfg = config.Config.from_json(store.get_run(conn)["config_json"])
    procs = {"orchestrator": _spawn(run_id, "orchestrator", ["orchestrator", "--run", run_id])}
    for role, worker_id in cfg.worker_ids():
        procs[worker_id] = _spawn(run_id, worker_id, ["worker", "--role", role, "--run", run_id, "--id", worker_id])
    print(f"run {run_id}: spawned {', '.join(f'{n} (pid {p.pid})' for n, p in procs.items())}")
    print(f"logs: {store.run_dir(run_id) / 'logs'}\n")
    started = time.time()
    last_event = store.count(conn, "SELECT COALESCE(MAX(event_id), 0) FROM events")
    last_status, last_print = "", 0.0
    try:
        while procs["orchestrator"].poll() is None:
            marks = ",".join("?" * len(NOTABLE))
            for e in conn.execute(f"SELECT * FROM events WHERE event_id > ? AND kind IN ({marks}) ORDER BY event_id",
                                  (last_event, *NOTABLE)).fetchall():
                print(f"  * {_describe(e)}")
            last_event = store.count(conn, "SELECT COALESCE(MAX(event_id), 0) FROM events")
            status = _status(conn, started)
            if status != last_status and time.time() - last_print >= 5:
                print(status)
                last_status, last_print = status, time.time()
            time.sleep(1)
    except KeyboardInterrupt:
        for p in procs.values():
            p.terminate()
        print(f"\ninterrupted. Resume with: python run.py --resume {run_id}")
        return 130
    deadline = time.time() + 15
    for p in procs.values():
        try:
            p.wait(timeout=max(0.1, deadline - time.time()))
        except subprocess.TimeoutExpired:
            p.terminate()
    run = store.get_run(conn)
    print(_status(conn, started))
    if run["phase"] not in store.FINAL_PHASES:
        print(f"\norchestrator exited early (code {procs['orchestrator'].returncode}); see logs. "
              f"Resume with: python run.py --resume {run_id}")
        return 1
    out = store.run_dir(run_id)
    print(f"\nfinished: {run['phase']}" + (f" ({run['degrade_reason']})" if run["degrade_reason"] else ""))
    print(f"brief:      {out / 'brief.md'}\nrun report: {out / 'run_report.md'}\nplayback:   python run.py replay {run_id}")
    return 0


def cmd_new_run(question: str, chaos: bool) -> int:
    from agent import store
    if not config.CORPUS_DB.exists():
        print("corpus.db not found; ingesting corpus/raw ...")
        store.build_corpus()
    run_id = datetime.now().strftime("run_%Y%m%d_%H%M%S")
    store.create_run(run_id, question, config.Config(chaos=chaos))
    print(f"question: {question}" + ("  [chaos mode]" if chaos else ""))
    return launch(run_id)


def cmd_resume(run_id: str) -> int:
    from agent import store
    conn = store.connect(store.state_path(run_id), attach_corpus=False)
    if store.get_run(conn)["phase"] in store.FINAL_PHASES:
        print(f"run {run_id} already finished; see {store.run_dir(run_id)}")
        return 0
    store.prepare_resume(conn)
    print(f"resuming {run_id} from phase '{store.get_run(conn)['phase']}'")
    return launch(run_id)


# --------------------------------------------------------------------------- playback

def _playback_line(r) -> str:
    if r["source"] == "llm":
        text = (r["response_text"] or r["error"] or "").replace("\n", " ")
        return (f"LLM {r['step']} [{r['prompt_tokens']}->{r['completion_tokens']} tokens, "
                f"{(r['latency_ms'] or 0) / 1000:.1f}s]: {text[:160]}")
    d = json.loads(r["detail_json"] or "{}")
    kind = r["kind"]
    simple = {
        "run_created": f"run created: {d.get('question')}",
        "phase_change": f"== phase: {d.get('phase')} ==",
        "task_created": f"queued task {r['task_id']} ({d.get('kind')}, SQ{r['subq_id']})",
        "task_claimed": f"claimed task {r['task_id']} ({d.get('kind')}, SQ{r['subq_id']}, attempt {d.get('attempt')})",
        "task_done": f"completed task {r['task_id']}",
        "task_retry": f"task {r['task_id']} {d.get('error_class')} error, will retry: {str(d.get('error'))[:100]}",
        "tool_call": f"tool {d.get('tool')}({json.dumps(d.get('args'))[:100]}) -> {d.get('results')} results",
        "tool_error": f"tool {d.get('tool')} raised: {str(d.get('error'))[:100]}",
        "worker_started": f"worker started (role {d.get('role')}, pid {d.get('pid')})",
        "worker_stopped": "worker stopped",
        "schema_invalid": f"invalid {d.get('step')} output (attempt {d.get('attempt')}): {str(d.get('error'))[:100]}",
        "schema_repaired": f"{d.get('step')} output repaired",
        "verify_reject": f"quote rejected ({d.get('doc_id')}): {d.get('reason')}",
        "verdict_downgraded": f"SQ{r['subq_id']} verdict downgraded {d.get('floor')} -> {d.get('proposed')}",
    }
    if kind in simple:
        return simple[kind]
    if kind in NOTABLE:
        return _describe(r)
    return f"{kind} {json.dumps(d)[:140] if d else ''}"


def cmd_replay(run: str, speed: float, max_gap: float) -> int:
    """`run` is a run id under runs/ or a path to a run directory (e.g. a committed example)."""
    from pathlib import Path

    from agent import store
    path = Path(run) / "state.db" if (Path(run) / "state.db").exists() else store.state_path(run)
    if not path.exists():
        print(f"no such run: {run}")
        return 1
    rows = store.trace(store.connect(path, attach_corpus=False))
    t0 = prev = rows[0]["ts"]
    for r in rows:
        time.sleep(min(max(0.0, r["ts"] - prev) / speed, max_gap))
        prev = r["ts"]
        print(f"t+{r['ts'] - t0:7.1f}s  {r['actor']:<13} {_playback_line(r)}", flush=True)
    brief = path.parent / "brief.md"
    if brief.exists():
        print(f"\n{'=' * 80}\n{brief.read_text()}")
    return 0


# --------------------------------------------------------------------------- CLI

def main(argv: list[str]) -> int:
    if argv and argv[0] == "ingest":
        from agent import store
        stats = store.build_corpus(force=True)
        print(f"corpus.db: {stats['documents']} documents, {stats['chunks']} chunks; skipped: {stats['skipped'] or 'none'}")
        return 0
    if argv and argv[0] == "orchestrator":
        a = argparse.ArgumentParser(prog="run.py orchestrator")
        a.add_argument("--run", required=True)
        from agent import orchestrator_agent
        orchestrator_agent.main(a.parse_args(argv[1:]).run)
        return 0
    if argv and argv[0] == "worker":
        a = argparse.ArgumentParser(prog="run.py worker")
        a.add_argument("--role", required=True, choices=["search", "extract", "synthesize"])
        a.add_argument("--run", required=True)
        a.add_argument("--id", required=True)
        args = a.parse_args(argv[1:])
        from agent import worker_agent
        worker_agent.main(args.run, args.role, args.id)
        return 0
    if argv and argv[0] == "replay":
        a = argparse.ArgumentParser(prog="run.py replay")
        a.add_argument("run", help="run id under runs/, or a path to a run directory")
        a.add_argument("--speed", type=float, default=20.0, help="time compression factor (default 20x)")
        a.add_argument("--max-gap", type=float, default=0.5, help="longest pause between lines, seconds")
        args = a.parse_args(argv[1:])
        return cmd_replay(args.run, args.speed, args.max_gap)
    a = argparse.ArgumentParser(prog="run.py", description="Multi-agent research brief over a local corpus.")
    a.add_argument("question", nargs="?", default=DEFAULT_QUESTION)
    a.add_argument("--chaos", action="store_true", help="seeded fault injection (tool errors, bad LLM output, a killed worker)")
    a.add_argument("--resume", metavar="RUN_ID", help="continue an interrupted run")
    args = a.parse_args(argv)
    return cmd_resume(args.resume) if args.resume else cmd_new_run(args.question, args.chaos)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
