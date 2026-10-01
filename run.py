"""Process entry point. Every OS process in the system starts here; all logic is imported.

  python run.py                                  # run the demo question
  python run.py "<question>" [--chaos] [-v]      # create a run, spawn all processes, wait
  python run.py --resume <run_id> [-v]           # respawn processes against an existing state.db
  python run.py replay <run_id> [--speed N]      # playback: print a recorded run's trace, no models
  python run.py ingest                           # (re)build corpus.db from corpus/raw + manifest
  python run.py orchestrator --run <run_id>      # (spawned) orchestrator process
  python run.py worker --role <role> --run <run_id> --id <worker_id>   # (spawned) worker process

The launcher spawns the orchestrator plus search x1, extract x2, synthesize x1 as separate OS
processes (re-invoking this file with a subcommand) and follows the run from state.db:
  - a status line (phase, task counts, LLM calls, verified claims) whenever it changes,
    plus a heartbeat every 30 s;
  - one "*" line per notable event: phase changes, plan revisions, verdicts, retries, lease
    expiries, dead workers, rejected quotes, repaired outputs, chaos faults, degradation;
  - with --verbose, every per-process log line as it is written, prefixed by process name.
It never restarts a process: recovery (lease expiry, re-queueing) is the orchestrator's job.

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
from pathlib import Path

import config

DEFAULT_QUESTION = "Was Spinosaurus an aquatic pursuit predator, and how strong is the evidence?"
LIVE_EVENTS = ("phase_change", "plan_version", "review", "review_skipped", "review_action_rejected", "verdict",
               "verdict_downgraded", "verdict_upgrade_ignored", "task_retry", "task_failed", "lease_expired",
               "hard_timeout", "worker_dead", "stale_result_discarded", "chaos", "schema_invalid", "schema_repaired",
               "verify_reject", "planner_retry", "planner_fallback", "degrade", "resumed")
HEARTBEAT_S = 30
MIN_STATUS_GAP_S = 3


# --------------------------------------------------------------------------- event formatting

def _cut(text, n: int = 160) -> str:
    text = str(text if text is not None else "")
    return text if len(text) <= n else text[:n - 1] + "…"


def describe_event(e) -> str:
    """One readable line per event row. Shared by the live launcher feed and replay."""
    d = json.loads(e["detail_json"] or "{}")
    tid, sq = e["task_id"], e["subq_id"]
    lines = {
        "run_created": lambda: f"run created: {d.get('question')}",
        "orchestrator_started": lambda: f"orchestrator started in phase '{d.get('phase')}'",
        "resumed": lambda: f"run resumed; {d.get('orphaned_tasks')} orphaned tasks will be re-queued",
        "phase_change": lambda: f"== phase: {d.get('phase')} ==",
        "plan_version": lambda: f"plan v{d.get('version')}: {_cut(d.get('reason'))}",
        "review": lambda: f"review round {d.get('round')}: "
                          + (", ".join(a.split(":")[0] for a in d.get("applied") or []) or "no actions"),
        "review_skipped": lambda: f"review skipped: {d.get('reason')}",
        "review_action_rejected": lambda: f"review action rejected ({d.get('why')}): {(d.get('action') or {}).get('type')}",
        "verdict": lambda: f"SQ{sq} verdict: {d.get('verdict')}",
        "verdict_downgraded": lambda: f"SQ{sq} verdict downgraded {d.get('floor')} -> {d.get('proposed')}",
        "verdict_upgrade_ignored": lambda: f"SQ{sq} verdict upgrade ignored ({d.get('floor')} -> {d.get('proposed')})",
        "task_created": lambda: f"queued task {tid} ({d.get('kind')}, SQ{sq})",
        "task_claimed": lambda: f"claimed task {tid} ({d.get('kind')}, SQ{sq}, attempt {d.get('attempt')})",
        "task_done": lambda: f"completed task {tid}",
        "task_retry": lambda: f"task {tid} {d.get('error_class')} error on attempt {d.get('attempt')}, will retry: "
                              f"{_cut(d.get('error'), 90)}",
        "task_failed": lambda: f"task {tid} failed permanently ({d.get('error_class')}): {_cut(d.get('error'), 90)}",
        "lease_expired": lambda: f"task {tid} lease expired (owner {d.get('previous_owner')}) -> {d.get('new_status')}",
        "hard_timeout": lambda: f"task {tid} hit the hard timeout -> {d.get('new_status')}",
        "stale_result_discarded": lambda: f"stale result for task {tid} discarded (its lease was lost)",
        "worker_started": lambda: f"worker started (role {d.get('role')}, pid {d.get('pid')})",
        "worker_stopped": lambda: "worker stopped",
        "worker_dead": lambda: f"worker {d.get('worker')} (pid {d.get('pid')}) presumed dead; held tasks {d.get('held_tasks')}",
        "tool_call": lambda: f"tool {d.get('tool')}({_cut(json.dumps(d.get('args')), 100)}) -> {d.get('results')} results",
        "tool_error": lambda: f"tool {d.get('tool')} raised: {_cut(d.get('error'), 100)}",
        "schema_invalid": lambda: f"invalid {d.get('step')} output (attempt {d.get('attempt')}), asking for a repair: "
                                  f"{_cut(d.get('error'), 80)}",
        "schema_repaired": lambda: f"{d.get('step')} output repaired",
        "verify_reject": lambda: f"quote rejected in {d.get('doc_id')}: {d.get('reason')}",
        "chaos": lambda: f"CHAOS injected: {d.get('fault')} in task {tid}",
        "planner_retry": lambda: f"planner retry at {d.get('step')} (attempt {d.get('attempt')}): {_cut(d.get('error'), 80)}",
        "planner_fallback": lambda: f"planner fallback at {d.get('step')}: {_cut(d.get('error'), 100)}",
        "degrade": lambda: f"DEGRADED: {d.get('reason')} (cancelled {d.get('cancelled_tasks')} tasks)",
        "bottom_line": lambda: f"bottom line written ({len(str(d.get('text', '')).split())} words)",
    }
    fmt = lines.get(e["kind"])
    return fmt() if fmt else f"{e['kind']} {_cut(json.dumps(d), 140) if d else ''}".strip()


# --------------------------------------------------------------------------- launcher

def _spawn(run_id: str, name: str, args: list[str]) -> subprocess.Popen:
    from agent import store
    out = open(store.run_dir(run_id) / "logs" / f"{name}.log", "a")
    return subprocess.Popen([sys.executable, str(config.ROOT / "run.py"), *args], stdout=out,
                            stderr=subprocess.STDOUT, cwd=config.ROOT, env={**os.environ, "PYTHONUNBUFFERED": "1"})


def _status(conn) -> str:
    """Status without a timestamp, so 'changed' means the run actually moved."""
    from agent import store
    run = store.get_run(conn)
    by = dict(conn.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status").fetchall())
    claims = conn.execute("SELECT COUNT(*), COALESCE(SUM(verified), 0) FROM claims").fetchone()
    return (f"phase={run['phase']:<10} tasks: {by.get('pending', 0)} queued, {by.get('claimed', 0)} running, "
            f"{by.get('done', 0)} done, {by.get('failed', 0)} failed | "
            f"LLM calls {store.count(conn, 'SELECT COUNT(*) FROM llm_calls')} | claims verified {claims[1]}/{claims[0]}")


class LogTail:
    """Follows every per-process log file and yields new complete lines (used by --verbose).
    Starts at the current end of each file, so a resumed run does not reprint old lines."""

    def __init__(self, logs_dir: Path):
        self.dir = logs_dir
        self.offsets = {p: p.stat().st_size for p in logs_dir.glob("*.log")}

    def new_lines(self):
        for path in sorted(self.dir.glob("*.log")):
            start = self.offsets.get(path, 0)
            with open(path, "rb") as fh:
                fh.seek(start)
                data = fh.read()
            end = data.rfind(b"\n") + 1  # only complete lines; a partial line waits for the next poll
            if not end:
                continue
            self.offsets[path] = start + end
            for line in data[:end].decode("utf-8", "replace").splitlines():
                yield path.stem, line


def launch(run_id: str, verbose: bool = False) -> int:
    from agent import store
    conn = store.connect(store.state_path(run_id), attach_corpus=False)
    cfg = config.Config.from_json(store.get_run(conn)["config_json"])
    logs = store.run_dir(run_id) / "logs"
    logs.mkdir(exist_ok=True)
    tail = LogTail(logs) if verbose else None
    procs = {"orchestrator": _spawn(run_id, "orchestrator", ["orchestrator", "--run", run_id])}
    for role, worker_id in cfg.worker_ids():
        procs[worker_id] = _spawn(run_id, worker_id, ["worker", "--role", role, "--run", run_id, "--id", worker_id])
    print(f"run {run_id}: spawned {', '.join(f'{n} (pid {p.pid})' for n, p in procs.items())}")
    print(f"logs: {logs}" + ("" if verbose else "   (add --verbose to stream them here)") + "\n")

    started = time.time()
    marks = ",".join("?" * len(LIVE_EVENTS))
    last_event = store.count(conn, "SELECT COALESCE(MAX(event_id), 0) FROM events")
    last_status, last_print = "", 0.0

    def follow(force_status: bool = False) -> None:
        nonlocal last_event, last_status, last_print
        for e in conn.execute(f"SELECT * FROM events WHERE event_id > ? AND kind IN ({marks}) ORDER BY event_id",
                              (last_event, *LIVE_EVENTS)).fetchall():
            print(f"  * {e['actor']:<13} {describe_event(e)}")
        last_event = store.count(conn, "SELECT COALESCE(MAX(event_id), 0) FROM events")
        if tail:
            for name, line in tail.new_lines():
                prefix = f"[{name}] "
                print(f"    {name:<13}| {_cut(line[len(prefix):] if line.startswith(prefix) else line, 220)}")
        status, now = _status(conn), time.time()
        changed = status != last_status and now - last_print >= MIN_STATUS_GAP_S
        if force_status or changed or now - last_print >= HEARTBEAT_S:
            print(f"[{(now - started) / 60:4.1f} min] {status}")
            last_status, last_print = status, now

    try:
        while procs["orchestrator"].poll() is None:
            follow()
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
    follow(force_status=True)  # flush the final events, log lines and status
    run = store.get_run(conn)
    if run["phase"] not in store.FINAL_PHASES:
        print(f"\norchestrator exited early (code {procs['orchestrator'].returncode}); see logs. "
              f"Resume with: python run.py --resume {run_id}")
        return 1
    out = store.run_dir(run_id)
    print(f"\nfinished: {run['phase']}" + (f" ({run['degrade_reason']})" if run["degrade_reason"] else ""))
    print(f"brief:      {out / 'brief.md'}\nrun report: {out / 'run_report.md'}\nplayback:   python run.py replay {run_id}")
    return 0


def cmd_new_run(question: str, chaos: bool, verbose: bool) -> int:
    from agent import store
    if not config.CORPUS_DB.exists():
        print("corpus.db not found; ingesting corpus/raw ...")
        store.build_corpus()
    run_id = datetime.now().strftime("run_%Y%m%d_%H%M%S")
    store.create_run(run_id, question, config.Config(chaos=chaos))
    print(f"question: {question}" + ("  [chaos mode]" if chaos else ""))
    return launch(run_id, verbose)


def cmd_resume(run_id: str, verbose: bool) -> int:
    from agent import store
    conn = store.connect(store.state_path(run_id), attach_corpus=False)
    if store.get_run(conn)["phase"] in store.FINAL_PHASES:
        print(f"run {run_id} already finished; see {store.run_dir(run_id)}")
        return 0
    store.prepare_resume(conn)
    print(f"resuming {run_id} from phase '{store.get_run(conn)['phase']}'")
    return launch(run_id, verbose)


# --------------------------------------------------------------------------- playback

def _playback_line(r) -> str:
    if r["source"] != "llm":
        return describe_event(r)
    text = (r["response_text"] or r["error"] or "").replace("\n", " ")
    return (f"LLM {r['step']} [{r['prompt_tokens']}->{r['completion_tokens']} tokens, "
            f"{(r['latency_ms'] or 0) / 1000:.1f}s]: {text[:160]}")


def cmd_replay(run: str, speed: float, max_gap: float) -> int:
    """`run` is a run id under runs/ or a path to a run directory (e.g. a committed example)."""
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
    sys.stdout.reconfigure(line_buffering=True)  # live output even when piped (tee, CI, nohup)
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
    a.add_argument("-v", "--verbose", action="store_true", help="stream every process's log lines to the terminal")
    args = a.parse_args(argv)
    return cmd_resume(args.resume, args.verbose) if args.resume else cmd_new_run(args.question, args.chaos, args.verbose)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
