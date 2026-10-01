"""Memory views: pulls the relevant facts from the datastores for a given agent and task.

Component: Workspace context + Memory retrieval (Tier 2). Views are computed, never accumulated.
Skipped for now: compaction / summarization and long-term (cross-run) memory.

build_memory(role, task) -> a compact, deterministic text block for the prompt's memory slot:
- planner:     goal + research brief, current plan version, per sub-question status /
               verdict / verified-claim counts, failed tasks, budget usage.
               SETTLED facts only (no in-flight counts), so decisions don't depend on timing.
- search:      sub-question + hypotheses + docs already found for it (avoid repeats).
- extract:     sub-question + hypotheses + the document's metadata and target pages.
- synthesize:  sub-question + its verified claims (top N by source independence).

Rules: deterministic ordering (by id), hard caps with "...N more" markers, no history of
previous calls -> prompt size stays flat across the run.

Reads from store.py (state.db + attached corpus.db). Never reads eval/corpus_labels.json.
"""
