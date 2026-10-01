"""Run configuration and model setup. Imported by run.py and every process.

- Load .env: API keys, per-agent model names, rate limit.
- Defaults for every orchestration parameter (README "Orchestration parameters"):
    sub-questions 4-6 / max 8 (+2 per round), review rounds max 2, search <=3 queries & top 4
    docs, extract <=2 page windows of ~3 pages, heartbeat 10s / lease 45s / timeout 300s,
    retries transient 3 (2s, 8s, 30s) / deterministic 2, schema repair 1, 30 req/min,
    supervisor tick 1s, worker poll 0.5-2s, phase timeout 10 min,
    budgets 120 tasks / 250 LLM calls / 30 min. Workers: search x1, extract x2, synthesize x1.
- Paths: corpus/, corpus.db, runs/<run_id>/state.db.
- Chaos: seed and fault plan (--chaos).
- The resolved config is frozen into state.db at run creation (resume uses it).

No model factory: each agent file declares its own model (see orchestrator_agent.py and
worker_agent.py). This file only supplies the values they read from .env.
"""
