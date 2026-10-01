"""Process entry point. Every OS process in the system starts here; all logic is imported.

Subcommands:
  python run.py ingest                            # build corpus.db (store.py)
  python run.py "<question>" [--chaos]            # create a run, spawn all processes, wait
  python run.py replay <run_id>                   # playback: print a finished run's trace, no models
  python run.py --resume <run_id>                 # respawn processes against an existing state.db
  python run.py orchestrator --run <run_id>       # (spawned) orchestrator process
  python run.py worker --role <role> --run <run_id> --id <worker_id>   # (spawned) worker process

The launcher spawns the orchestrator plus search x1, extract x2, synthesize x1 as separate
OS processes (re-invoking this file with a subcommand), waits for done/degraded, then
points the user at the brief and the run report.

Replay is playback only: it reads events + llm_calls from runs/<run_id>/state.db and prints
the run in order (tasks claimed, tool calls, LLM outputs, failures and recoveries, replans,
final brief), as if it were happening. Nothing executes and no model is called.
"""
