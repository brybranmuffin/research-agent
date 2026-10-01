"""Run configuration. Imported by run.py and every process.

- Loads .env (API key, per-agent model names, rate limit).
- Defines every orchestration parameter (README "Orchestration parameters").
- A run's resolved Config is frozen into its state.db at creation; every process of that
  run (including after a resume) reads the frozen copy, never the live defaults.

There is no model factory: each agent file declares its own model and reads only its
model name from here.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
CORPUS_DIR = ROOT / "corpus"
MANIFEST = CORPUS_DIR / "manifest.json"
RAW_DIR = CORPUS_DIR / "raw"
CORPUS_DB = ROOT / "corpus.db"
RUNS_DIR = ROOT / "runs"

load_dotenv(ROOT / ".env")

DEFAULT_MODEL = "google/gemma-4-31b-it"


def model_name(agent: str) -> str:
    """Model id for one agent (planner | search | extract | synthesize), from .env."""
    return os.getenv(f"NIM_MODEL_{agent.upper()}") or DEFAULT_MODEL


@dataclass
class Config:
    # Plan shape
    min_initial_subqs: int = 4
    max_initial_subqs: int = 6
    max_total_subqs: int = 8
    max_new_subqs_per_round: int = 2
    max_review_rounds: int = 2

    # Work per sub-question
    search_max_queries: int = 3
    search_hits_per_query: int = 10
    search_top_docs: int = 4
    extract_max_windows: int = 2
    extract_window_units: int = 3
    extract_max_chars_per_unit: int = 5000
    extract_max_claims: int = 6
    synth_max_claims: int = 15

    # Timing and failure handling
    heartbeat_s: float = 10
    lease_s: float = 45
    task_timeout_s: float = 300
    llm_timeout_s: float = 90
    transient_retries: int = 3
    transient_backoff_s: tuple = (2, 8, 30)
    deterministic_retries: int = 1
    schema_repairs: int = 1
    rate_limit_rpm: int = int(os.getenv("RATE_LIMIT_RPM", "30"))
    rate_limit_burst: int = 4
    supervisor_tick_s: float = 1
    worker_poll_min_s: float = 0.5
    worker_poll_max_s: float = 2
    phase_timeout_s: float = 600

    # Budgets (graceful degradation when exceeded)
    budget_tasks: int = 120
    budget_llm_calls: int = 250
    budget_wall_s: float = 1800

    # Verification
    quote_min_words: int = 8
    fuzzy_threshold: float = 90

    # Processes
    workers: dict = field(default_factory=lambda: {"search": 1, "extract": 2, "synthesize": 1})

    # Chaos (seeded fault injection)
    chaos: bool = False
    chaos_seed: int = 7
    chaos_tool_error_rate: float = 0.15
    chaos_malformed_rate: float = 0.15
    chaos_kill_worker: str = "extract-1"
    chaos_kill_on_task: int = 3

    # Recorded for the run report; each agent file reads its own model name
    models: dict = field(default_factory=lambda: {
        a: model_name(a) for a in ("planner", "search", "extract", "synthesize")})

    def worker_ids(self) -> list[tuple[str, str]]:
        """[(role, worker_id)], e.g. [("extract", "extract-1"), ("extract", "extract-2"), ...]."""
        return [(role, f"{role}-{i}") for role, n in self.workers.items() for i in range(1, n + 1)]

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> "Config":
        data = json.loads(text)
        data["transient_backoff_s"] = tuple(data["transient_backoff_s"])
        return cls(**data)
