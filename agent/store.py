"""The two databases: corpus.db (read-only sources) and state.db (per-run shared state).

Component: Memory + the handoff medium (Tier 1). Processes share nothing in memory; all
coordination goes through these files. state.db runs in WAL mode with a busy timeout and
has corpus.db ATTACHed (as `corpus`), so claims can be joined to chunks in one query.

corpus.db (built once by ingest, shared by all runs)
  documents   doc_id, type, title, authors, first_author, year, venue, source_kind, sha256, n_units
  chunks      chunk_id, doc_id, unit (page / section no.), ord, text, norm_text, is_ref
  chunks_fts  FTS5 index (porter) over non-reference chunks; rowid = chunk_id

state.db (one per run)
  run, plan_versions, subquestions, tasks, candidates, claims, sections,
  llm_calls, events, workers, rate_limiter      (schema below)

Task queue: pending -> claimed -> done | failed; an expired lease or a retry -> pending
(with backoff in not_before); a retired sub-question or degradation -> cancelled.
Claiming is one atomic UPDATE ... RETURNING. Completion is fenced: a result is accepted only
from the current owner of a still-claimed task, inside the same transaction as its writes.

Write ownership: orchestrator -> run, plan_versions, subquestions, task inserts;
workers -> only tasks they own, plus candidates, claims, sections; everyone -> events, llm_calls.
Claims are written only by tools.record_claim (the verification gate).

LangChain: TraceHandler (a BaseCallbackHandler) writes an llm_calls row for every chat-model
call and a tool_call event for every tool call, attributed via RunnableConfig metadata.
Not used: InMemoryRateLimiter, which is per-process; our processes share one key, so the
token bucket lives here in SQLite.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from langchain_core.callbacks import BaseCallbackHandler

import config

FINAL_PHASES = ("done", "degraded")
OPEN_STATUSES = ("pending", "claimed")


class TransientError(Exception):
    """Worth retrying with backoff: rate limits, timeouts, network, injected tool faults."""


class DeterministicError(Exception):
    """The same input will likely fail again: invalid output after repair, bad input."""


# --------------------------------------------------------------------------- connections

def connect(path: Path, attach_corpus: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    if attach_corpus:
        conn.execute("ATTACH DATABASE ? AS corpus", (str(config.CORPUS_DB),))
    return conn


@contextmanager
def tx(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE ... COMMIT. Joins an outer transaction if one is already open."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def run_dir(run_id: str) -> Path:
    return config.RUNS_DIR / run_id


def state_path(run_id: str) -> Path:
    return run_dir(run_id) / "state.db"


# --------------------------------------------------------------------------- state.db schema

STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS run (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    run_id TEXT NOT NULL,
    question TEXT NOT NULL,
    goal_json TEXT,
    phase TEXT NOT NULL,
    phase_started_at REAL NOT NULL,
    review_round INTEGER NOT NULL DEFAULT 0,
    config_json TEXT NOT NULL,
    degrade_reason TEXT,
    created_at REAL NOT NULL,
    session_started_at REAL NOT NULL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS plan_versions (
    version INTEGER PRIMARY KEY,
    review_round INTEGER NOT NULL,
    reason TEXT NOT NULL,
    hypotheses_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS subquestions (
    subq_id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    tests_json TEXT NOT NULL DEFAULT '[]',
    added_in_version INTEGER NOT NULL,
    retired_in_version INTEGER,
    status TEXT NOT NULL,          -- gathering | checking | checked | writing | written | insufficient | retired
    verdict TEXT,                  -- supported | contested | thin
    verdict_rationale TEXT,
    note TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,            -- search | extract | cross_check | write_section
    role TEXT NOT NULL,            -- search | extract | synthesize
    subq_id INTEGER,
    plan_version INTEGER,
    round INTEGER NOT NULL DEFAULT 0,
    input_json TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',
    owner TEXT,
    lease_until REAL,
    heartbeat_at REAL,
    attempts INTEGER NOT NULL DEFAULT 0,
    not_before REAL NOT NULL DEFAULT 0,
    error_class TEXT,
    error_msg TEXT,
    result_json TEXT,
    created_at REAL NOT NULL,
    claimed_at REAL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_tasks_claim ON tasks (status, role, not_before);
CREATE TABLE IF NOT EXISTS candidates (
    subq_id INTEGER NOT NULL,
    doc_id TEXT NOT NULL,
    score REAL,
    hit_units_json TEXT NOT NULL,
    reason TEXT,
    task_id INTEGER,
    PRIMARY KEY (subq_id, doc_id)
);
CREATE TABLE IF NOT EXISTS claims (
    claim_id INTEGER PRIMARY KEY,
    subq_id INTEGER NOT NULL,
    doc_id TEXT NOT NULL,
    chunk_id INTEGER,
    unit INTEGER,
    text TEXT NOT NULL,
    quote TEXT NOT NULL,
    stance TEXT NOT NULL,
    verified INTEGER NOT NULL,
    verify_score REAL,
    verify_method TEXT NOT NULL,   -- exact | fuzzy | neighbor | rejected
    reject_reason TEXT,
    task_id INTEGER,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claims_subq ON claims (subq_id, verified);
CREATE TABLE IF NOT EXISTS sections (
    subq_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    markdown TEXT NOT NULL,
    cited_claim_ids_json TEXT NOT NULL,
    task_id INTEGER,
    created_at REAL NOT NULL,
    PRIMARY KEY (subq_id, version)
);
CREATE TABLE IF NOT EXISTS llm_calls (
    call_id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    actor TEXT,
    task_id INTEGER,
    step TEXT,
    model TEXT,
    messages_json TEXT,
    response_text TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    latency_ms INTEGER,
    outcome TEXT NOT NULL,         -- ok | error
    error TEXT
);
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    actor TEXT NOT NULL,
    kind TEXT NOT NULL,
    task_id INTEGER,
    subq_id INTEGER,
    detail_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events (kind);
CREATE TABLE IF NOT EXISTS workers (
    worker_id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    pid INTEGER,
    started_at REAL NOT NULL,
    last_seen REAL NOT NULL,
    status TEXT NOT NULL           -- alive | dead | stopped
);
CREATE TABLE IF NOT EXISTS rate_limiter (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    tokens REAL NOT NULL,
    refilled_at REAL NOT NULL
);
"""


def create_run(run_id: str, question: str, cfg: config.Config) -> Path:
    run_dir(run_id).mkdir(parents=True, exist_ok=False)
    conn = connect(state_path(run_id))
    conn.executescript(STATE_SCHEMA)
    now = time.time()
    conn.execute(
        "INSERT INTO run (id, run_id, question, phase, phase_started_at, config_json, created_at, "
        "session_started_at) VALUES (1, ?, ?, 'plan', ?, ?, ?, ?)",
        (run_id, question, now, cfg.to_json(), now, now))
    conn.execute("INSERT INTO rate_limiter VALUES (1, ?, ?)", (cfg.rate_limit_burst, now))
    log_event(conn, "launcher", "run_created", question=question, chaos=cfg.chaos)
    conn.close()
    return state_path(run_id)


def get_run(conn: sqlite3.Connection) -> sqlite3.Row:
    return conn.execute("SELECT * FROM run WHERE id = 1").fetchone()


def set_phase(conn: sqlite3.Connection, phase: str, actor: str = "orchestrator") -> None:
    with tx(conn):
        conn.execute("UPDATE run SET phase = ?, phase_started_at = ? WHERE id = 1", (phase, time.time()))
        log_event(conn, actor, "phase_change", phase=phase)


def log_event(conn: sqlite3.Connection, actor: str, event: str, task_id: Optional[int] = None,
              subq_id: Optional[int] = None, **detail: Any) -> None:
    conn.execute(
        "INSERT INTO events (ts, actor, kind, task_id, subq_id, detail_json) VALUES (?, ?, ?, ?, ?, ?)",
        (time.time(), actor, event, task_id, subq_id, json.dumps(detail, default=str) if detail else None))


def count(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    return conn.execute(sql, params).fetchone()[0]


# --------------------------------------------------------------------------- task queue

def enqueue_task(conn: sqlite3.Connection, *, kind: str, role: str, inputs: dict, dedupe_key: str,
                 subq_id: Optional[int] = None, plan_version: Optional[int] = None,
                 round: int = 0, actor: str = "orchestrator") -> Optional[int]:
    """Insert a pending task. Returns its id, or None if dedupe_key already exists."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO tasks (kind, role, subq_id, plan_version, round, input_json, dedupe_key, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (kind, role, subq_id, plan_version, round, json.dumps(inputs), dedupe_key, time.time()))
    if cur.rowcount == 0:
        return None
    log_event(conn, actor, "task_created", task_id=cur.lastrowid, subq_id=subq_id, kind=kind)
    return cur.lastrowid


def claim_task(conn: sqlite3.Connection, role: str, owner: str, cfg: config.Config) -> Optional[sqlite3.Row]:
    """Atomically claim the oldest eligible pending task for this role."""
    now = time.time()
    with tx(conn):
        rows = conn.execute(
            """UPDATE tasks SET status = 'claimed', owner = ?, lease_until = ?, heartbeat_at = ?,
                   claimed_at = ?, attempts = attempts + 1
               WHERE task_id = (SELECT task_id FROM tasks
                                WHERE status = 'pending' AND role = ? AND not_before <= ?
                                ORDER BY task_id LIMIT 1)
               RETURNING *""",
            (owner, now + cfg.lease_s, now, now, role, now)).fetchall()
        if not rows:
            return None
        row = rows[0]
        log_event(conn, owner, "task_claimed", task_id=row["task_id"], subq_id=row["subq_id"],
                  kind=row["kind"], attempt=row["attempts"])
        return row


def heartbeat(conn: sqlite3.Connection, task_id: int, owner: str, cfg: config.Config) -> bool:
    """Extend the lease. False means the task is no longer ours (reaped or cancelled)."""
    now = time.time()
    cur = conn.execute(
        "UPDATE tasks SET heartbeat_at = ?, lease_until = ? WHERE task_id = ? AND owner = ? AND status = 'claimed'",
        (now, now + cfg.lease_s, task_id, owner))
    conn.execute("UPDATE workers SET last_seen = ? WHERE worker_id = ?", (now, owner))
    return cur.rowcount == 1


def complete_task(conn: sqlite3.Connection, task_id: int, owner: str, result: dict,
                  writes: Optional[Callable[[sqlite3.Connection], None]] = None) -> bool:
    """Fenced completion: apply `writes` and mark done only if we still own the claimed task."""
    with tx(conn):
        row = conn.execute("SELECT status, owner FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None or row["status"] != "claimed" or row["owner"] != owner:
            log_event(conn, owner, "stale_result_discarded", task_id=task_id,
                      status=row["status"] if row else None)
            return False
        if writes:
            writes(conn)
        conn.execute(
            "UPDATE tasks SET status = 'done', result_json = ?, finished_at = ?, lease_until = NULL WHERE task_id = ?",
            (json.dumps(result, default=str), time.time(), task_id))
        log_event(conn, owner, "task_done", task_id=task_id)
    return True


def _retry_decision(attempts: int, error_class: str, cfg: config.Config) -> tuple[str, float]:
    if error_class == "transient":
        if attempts > cfg.transient_retries:
            return "failed", 0.0
        backoff = cfg.transient_backoff_s[min(attempts - 1, len(cfg.transient_backoff_s) - 1)]
    else:
        if attempts > cfg.deterministic_retries:
            return "failed", 0.0
        backoff = cfg.transient_backoff_s[0]
    return "pending", time.time() + backoff


def fail_task(conn: sqlite3.Connection, task_id: int, owner: str, error_class: str, message: str,
              cfg: config.Config) -> Optional[str]:
    """Record a failure: re-queue with backoff, or mark failed once retries are used up."""
    with tx(conn):
        row = conn.execute("SELECT status, owner, attempts, subq_id FROM tasks WHERE task_id = ?",
                           (task_id,)).fetchone()
        if row is None or row["status"] != "claimed" or row["owner"] != owner:
            return None
        status, not_before = _retry_decision(row["attempts"], error_class, cfg)
        conn.execute(
            "UPDATE tasks SET status = ?, owner = NULL, lease_until = NULL, not_before = ?, error_class = ?, "
            "error_msg = ?, finished_at = ? WHERE task_id = ?",
            (status, not_before, error_class, message[:500], time.time() if status == "failed" else None, task_id))
        log_event(conn, owner, "task_retry" if status == "pending" else "task_failed", task_id=task_id,
                  subq_id=row["subq_id"], error_class=error_class, error=message[:300], attempt=row["attempts"])
        return status


def reap_tasks(conn: sqlite3.Connection, cfg: config.Config, actor: str = "orchestrator") -> list[dict]:
    """Re-queue tasks whose lease expired (worker dead/stalled) or that hit the hard timeout."""
    now = time.time()
    reaped = []
    with tx(conn):
        rows = conn.execute(
            "SELECT task_id, owner, attempts, claimed_at, lease_until, subq_id FROM tasks "
            "WHERE status = 'claimed' AND (lease_until < ? OR claimed_at < ?)",
            (now, now - cfg.task_timeout_s)).fetchall()
        for r in rows:
            reason = "lease_expired" if r["lease_until"] < now else "hard_timeout"
            status, not_before = _retry_decision(r["attempts"], "transient", cfg)
            conn.execute(
                "UPDATE tasks SET status = ?, owner = NULL, lease_until = NULL, not_before = ?, "
                "error_class = 'transient', error_msg = ?, finished_at = ? WHERE task_id = ?",
                (status, not_before, reason, now if status == "failed" else None, r["task_id"]))
            log_event(conn, actor, reason, task_id=r["task_id"], subq_id=r["subq_id"],
                      previous_owner=r["owner"], new_status=status)
            reaped.append({"task_id": r["task_id"], "reason": reason, "status": status})
    return reaped


def cancel_open_tasks(conn: sqlite3.Connection, kinds: tuple[str, ...], reason: str,
                      subq_id: Optional[int] = None) -> int:
    marks = ",".join("?" * len(kinds))
    sql = f"UPDATE tasks SET status = 'cancelled', owner = NULL, error_msg = ?, finished_at = ? " \
          f"WHERE status IN ('pending', 'claimed') AND kind IN ({marks})"
    params: list[Any] = [reason, time.time(), *kinds]
    if subq_id is not None:
        sql += " AND subq_id = ?"
        params.append(subq_id)
    return conn.execute(sql, params).rowcount


# --------------------------------------------------------------------------- workers

def register_worker(conn: sqlite3.Connection, worker_id: str, role: str, pid: int) -> None:
    now = time.time()
    conn.execute("INSERT OR REPLACE INTO workers VALUES (?, ?, ?, ?, ?, 'alive')", (worker_id, role, pid, now, now))
    log_event(conn, worker_id, "worker_started", role=role, pid=pid)


def touch_worker(conn: sqlite3.Connection, worker_id: str) -> None:
    conn.execute("UPDATE workers SET last_seen = ? WHERE worker_id = ?", (time.time(), worker_id))


def stop_worker(conn: sqlite3.Connection, worker_id: str) -> None:
    conn.execute("UPDATE workers SET status = 'stopped', last_seen = ? WHERE worker_id = ?", (time.time(), worker_id))
    log_event(conn, worker_id, "worker_stopped")


def mark_dead_workers(conn: sqlite3.Connection, cfg: config.Config) -> list[str]:
    """Workers silent for longer than a lease are presumed dead; their tasks get reaped."""
    cutoff = time.time() - cfg.lease_s
    dead = []
    with tx(conn):
        for w in conn.execute("SELECT worker_id, pid FROM workers WHERE status = 'alive' AND last_seen < ?",
                              (cutoff,)).fetchall():
            held = [r[0] for r in conn.execute(
                "SELECT task_id FROM tasks WHERE status = 'claimed' AND owner = ?", (w["worker_id"],))]
            conn.execute("UPDATE workers SET status = 'dead' WHERE worker_id = ?", (w["worker_id"],))
            log_event(conn, "orchestrator", "worker_dead", worker=w["worker_id"], pid=w["pid"], held_tasks=held)
            dead.append(w["worker_id"])
    return dead


def prepare_resume(conn: sqlite3.Connection) -> None:
    """Before respawning a run's processes: the previous session's processes are gone."""
    now = time.time()
    with tx(conn):
        conn.execute("UPDATE workers SET status = 'dead' WHERE status = 'alive'")
        n = conn.execute("UPDATE tasks SET lease_until = 0 WHERE status = 'claimed'").rowcount
        conn.execute("UPDATE run SET session_started_at = ?, phase_started_at = ? WHERE id = 1", (now, now))
        log_event(conn, "launcher", "resumed", orphaned_tasks=n)


# --------------------------------------------------------------------------- rate limiter

def acquire_rate_token(conn: sqlite3.Connection, cfg: config.Config) -> float:
    """Block until the shared (cross-process) token bucket grants one LLM call."""
    rate = cfg.rate_limit_rpm / 60.0
    waited = 0.0
    while True:
        with tx(conn):
            r = conn.execute("SELECT tokens, refilled_at FROM rate_limiter WHERE id = 1").fetchone()
            now = time.time()
            tokens = min(cfg.rate_limit_burst, r["tokens"] + (now - r["refilled_at"]) * rate)
            if tokens >= 1:
                conn.execute("UPDATE rate_limiter SET tokens = ?, refilled_at = ? WHERE id = 1", (tokens - 1, now))
                return waited
            conn.execute("UPDATE rate_limiter SET tokens = ?, refilled_at = ? WHERE id = 1", (tokens, now))
            wait = (1 - tokens) / rate
        time.sleep(wait + 0.05)
        waited += wait


# --------------------------------------------------------------------------- process context

@dataclass
class Ctx:
    """Everything one process needs: its identity, its state.db connection, the frozen config."""
    run_id: str
    actor: str
    conn: sqlite3.Connection
    cfg: config.Config
    tracer: "TraceHandler"
    task_id: Optional[int] = None
    attempt: int = 0
    chaos_counter: int = 0


def open_ctx(run_id: str, actor: str) -> Ctx:
    conn = connect(state_path(run_id))
    cfg = config.Config.from_json(get_run(conn)["config_json"])
    return Ctx(run_id, actor, conn, cfg, TraceHandler(state_path(run_id), actor))


# --------------------------------------------------------------------------- LangChain trace callback

def _message_text(msg: Any) -> str:
    if msg is None:
        return ""
    calls = getattr(msg, "tool_calls", None)
    if calls:
        return json.dumps([{"name": c["name"], "args": c["args"]} for c in calls], default=str)
    content = getattr(msg, "content", "")
    return content if isinstance(content, str) else json.dumps(content, default=str)


class TraceHandler(BaseCallbackHandler):
    """Every chat-model call -> one llm_calls row; every tool call -> one tool_call event."""

    def __init__(self, db_path: Path, actor: str):
        self.db_path, self.actor = db_path, actor
        self._local = threading.local()
        self._open: dict[Any, tuple] = {}

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._local.conn = connect(self.db_path, attach_corpus=False)
        return conn

    def on_chat_model_start(self, serialized, messages, *, run_id, metadata=None, **kwargs):
        self._open[run_id] = (time.time(), messages[0] if messages else [], metadata or {})

    def _record_llm(self, run_id, response_text, usage, outcome, error=None):
        t0, msgs, md = self._open.pop(run_id, (time.time(), [], {}))
        self._conn().execute(
            "INSERT INTO llm_calls (ts, actor, task_id, step, model, messages_json, response_text, prompt_tokens, "
            "completion_tokens, latency_ms, outcome, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (t0, self.actor, md.get("task_id"), md.get("step"), md.get("ls_model_name"),
             json.dumps([{"role": m.type, "content": m.content} for m in msgs], default=str),
             response_text, usage.get("input_tokens"), usage.get("output_tokens"),
             int((time.time() - t0) * 1000), outcome, error))

    def on_llm_end(self, response, *, run_id, **kwargs):
        msg = getattr(response.generations[0][0], "message", None) if response.generations else None
        self._record_llm(run_id, _message_text(msg), getattr(msg, "usage_metadata", None) or {}, "ok")

    def on_llm_error(self, error, *, run_id, **kwargs):
        self._record_llm(run_id, "", {}, "error", str(error)[:500])

    def on_tool_start(self, serialized, input_str, *, run_id, metadata=None, inputs=None, **kwargs):
        args = {k: v for k, v in (inputs or {}).items() if k != "ctx"}
        self._open[run_id] = (time.time(), serialized.get("name"), metadata or {}, args)

    def on_tool_end(self, output, *, run_id, **kwargs):
        t0, name, md, args = self._open.pop(run_id, (time.time(), "?", {}, {}))
        n = len(output) if isinstance(output, list) else 1
        log_event(self._conn(), self.actor, "tool_call", task_id=md.get("task_id"), tool=name, args=args,
                  results=n, latency_ms=int((time.time() - t0) * 1000))

    def on_tool_error(self, error, *, run_id, **kwargs):
        t0, name, md, args = self._open.pop(run_id, (time.time(), "?", {}, {}))
        log_event(self._conn(), self.actor, "tool_error", task_id=md.get("task_id"), tool=name, args=args,
                  error=str(error)[:300])


# --------------------------------------------------------------------------- corpus.db: ingest

CORPUS_SCHEMA = """
CREATE TABLE documents (
    doc_id TEXT PRIMARY KEY,
    type TEXT NOT NULL,            -- pdf | html
    title TEXT, authors_json TEXT, first_author TEXT, year INTEGER, venue TEXT,
    source_kind TEXT NOT NULL,     -- primary | secondary (manifest field; default: pdf -> primary, html -> secondary)
    license TEXT, sha256 TEXT,
    n_units INTEGER NOT NULL,      -- pages (pdf) or sections (html)
    unit_labels_json TEXT          -- section titles (html)
);
CREATE TABLE chunks (
    chunk_id INTEGER PRIMARY KEY,
    doc_id TEXT NOT NULL,
    unit INTEGER NOT NULL,
    ord INTEGER NOT NULL,
    text TEXT NOT NULL,
    norm_text TEXT NOT NULL,
    is_ref INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_chunks_doc ON chunks (doc_id, unit, ord);
CREATE VIRTUAL TABLE chunks_fts USING fts5(text, tokenize = 'porter unicode61');
"""

_QUOTE_MAP = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-",
                            "—": "-", "−": "-", " ": " ", "­": ""})


def normalize(text: str) -> str:
    """Canonical form for quote matching: NFKC (ligatures), quotes/dashes, de-hyphenation, case, spaces."""
    text = unicodedata.normalize("NFKC", text).translate(_QUOTE_MAP)
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)
    return re.sub(r"\s+", " ", text).strip().lower()


_REF_HEADING = re.compile(r"^\s*(references|literature cited|bibliography|references and notes|works cited)\s*$",
                          re.IGNORECASE | re.MULTILINE)
_YEAR = re.compile(r"\b(19|20)\d{2}[a-z]?\b")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[])")
SKIP_HTML_SECTIONS = {"references", "notes", "external links", "see also", "bibliography", "further reading",
                      "sources", "citations", "footnotes", "literature cited", "related", "related articles"}


def _clean_pdf_text(text: str) -> str:
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    return re.sub(r"\s+", " ", text).strip()


def _pack(pieces: list[str], max_chars: int = 1200) -> list[str]:
    """Greedily pack sentences/paragraphs into chunks of at most ~max_chars."""
    chunks, cur = [], ""
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        if len(piece) > max_chars:
            if cur:
                chunks.append(cur)
                cur = ""
            parts = _SENTENCE_END.split(piece)
            if len(parts) > 1:
                chunks.extend(_pack(parts, max_chars))
            else:  # one over-long "sentence" (tables, captions): split on whitespace
                words, buf = piece.split(), ""
                for w in words:
                    if buf and len(buf) + 1 + len(w) > max_chars:
                        chunks.append(buf)
                        buf = w
                    else:
                        buf = f"{buf} {w}".strip()
                if buf:
                    chunks.append(buf)
            continue
        if cur and len(cur) + 1 + len(piece) > max_chars:
            chunks.append(cur)
            cur = piece
        else:
            cur = f"{cur} {piece}".strip()
    if cur:
        chunks.append(cur)
    return chunks


def _looks_like_refs(chunk: str) -> bool:
    return len(chunk) >= 300 and len(_YEAR.findall(chunk)) / (len(chunk) / 1000) >= 8


def parse_pdf(path: Path) -> list[list[tuple[str, bool]]]:
    """PDF -> per page, a list of (chunk_text, is_ref). Reference lists are flagged, not dropped."""
    import logging

    from pypdf import PdfReader

    logging.getLogger("pypdf").setLevel(logging.ERROR)  # font-encoding chatter, not errors
    reader = PdfReader(str(path))
    raw_pages = []
    for page in reader.pages:
        try:
            raw_pages.append(page.extract_text() or "")
        except Exception:  # a damaged page should not sink the whole document
            raw_pages.append("")
    in_refs = False
    pages = []
    for i, raw in enumerate(raw_pages):
        body, refs = raw, ""
        if not in_refs and i >= max(1, int(len(raw_pages) * 0.3)):
            m = _REF_HEADING.search(raw)
            if m:
                body, refs, in_refs = raw[:m.start()], raw[m.start():], True
        elif in_refs:
            body, refs = "", raw
        units = [(c, _looks_like_refs(c)) for c in _pack(_SENTENCE_END.split(_clean_pdf_text(body)))]
        units += [(c, True) for c in _pack(_SENTENCE_END.split(_clean_pdf_text(refs)))]
        pages.append(units)
    return pages


def parse_html(path: Path, title: str) -> list[tuple[str, list[tuple[str, bool]]]]:
    """HTML -> list of (section_title, [(chunk_text, is_ref)]), split at h1-h3 headings."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(path.read_bytes(), "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript", "form", "figure",
                     "table", "button", "svg"]):
        tag.decompose()
    for tag in soup.select("sup.reference, .mw-editsection, .navbox, .reflist, .hatnote, .infobox"):
        tag.decompose()
    root = soup.find("article") or soup.body or soup
    sections: list[tuple[str, list[str]]] = [(title, [])]
    for el in root.find_all(["h1", "h2", "h3", "p", "li", "blockquote"]):
        text = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
        if el.name in ("h1", "h2", "h3"):
            if text:
                sections.append((text, []))
            continue
        if el.find_parent(["p", "li", "blockquote"]) is not None:
            continue
        if len(text) < (40 if el.name == "li" else 25):
            continue
        sections[-1][1].append(text)
    out = []
    for heading, paras in sections:
        if heading.strip().lower() in SKIP_HTML_SECTIONS or sum(len(p) for p in paras) < 100:
            continue
        out.append((heading, [(c, _looks_like_refs(c)) for c in _pack(paras)]))
    return out


def _first_author_key(authors: list[str]) -> str:
    """Independence key: last name of the first author, lowercased."""
    if not authors:
        return "unknown"
    return re.sub(r"[^a-z]", "", authors[0].split()[-1].lower()) or "unknown"


def insert_document(conn: sqlite3.Connection, meta: dict, units: list[list[tuple[str, bool]]],
                    labels: Optional[list[str]] = None) -> int:
    """Write one document and its chunks (units[i] = chunks of unit i+1). Returns chunks written."""
    conn.execute(
        "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (meta["id"], meta["type"], meta.get("title"), json.dumps(meta.get("authors") or []),
         _first_author_key(meta.get("authors") or []), meta.get("year"), meta.get("venue"),
         meta.get("source_kind") or ("primary" if meta["type"] == "pdf" else "secondary"),
         meta.get("license"), meta.get("sha256"),
         len(units), json.dumps(labels) if labels else None))
    n = 0
    for unit_no, chunks in enumerate(units, start=1):
        for ord_, (text, is_ref) in enumerate(chunks):
            cur = conn.execute(
                "INSERT INTO chunks (doc_id, unit, ord, text, norm_text, is_ref) VALUES (?, ?, ?, ?, ?, ?)",
                (meta["id"], unit_no, ord_, text, normalize(text), int(is_ref)))
            if not is_ref:
                conn.execute("INSERT INTO chunks_fts (rowid, text) VALUES (?, ?)", (cur.lastrowid, text))
            n += 1
    return n


def build_corpus(force: bool = False, log: Callable[[str], None] = print) -> dict:
    """Deterministic ingest: manifest + corpus/raw -> corpus.db. Never reads eval/ labels."""
    if config.CORPUS_DB.exists() and not force:
        return {"skipped": True}
    tmp = config.CORPUS_DB.with_suffix(".db.tmp")
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(str(tmp))
    conn.executescript(CORPUS_SCHEMA)
    stats = {"documents": 0, "chunks": 0, "skipped": []}
    for meta in json.loads(config.MANIFEST.read_text()):
        if meta.get("status") != "ok":
            continue
        path = config.RAW_DIR / f"{meta['id']}.{meta['type']}"
        if not path.exists():
            stats["skipped"].append(f"{meta['id']} (missing file; run corpus/fetch_corpus.py)")
            continue
        if meta.get("sha256") and hashlib.sha256(path.read_bytes()).hexdigest() != meta["sha256"]:
            stats["skipped"].append(f"{meta['id']} (sha256 mismatch)")
            continue
        if meta["type"] == "pdf":
            units, labels = parse_pdf(path), None
        else:
            sections = parse_html(path, meta.get("title") or meta["id"])
            units, labels = [chunks for _, chunks in sections], [h for h, _ in sections]
        n = insert_document(conn, meta, units, labels)
        stats["documents"] += 1
        stats["chunks"] += n
        log(f"  ingested {meta['id']:28s} {len(units):3d} units {n:4d} chunks")
    conn.commit()
    conn.close()
    tmp.replace(config.CORPUS_DB)
    return stats


# --------------------------------------------------------------------------- playback

def trace(conn: sqlite3.Connection) -> list[dict]:
    """Events and LLM calls merged in time order (the source for `run.py replay`)."""
    rows = [dict(r, source="event") for r in conn.execute("SELECT * FROM events")]
    rows += [dict(r, source="llm") for r in conn.execute(
        "SELECT call_id, ts, actor, task_id, step, model, response_text, prompt_tokens, completion_tokens, "
        "latency_ms, outcome, error FROM llm_calls")]
    return sorted(rows, key=lambda r: r["ts"])
