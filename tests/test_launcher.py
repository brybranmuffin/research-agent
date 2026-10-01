"""Launcher output: the --verbose log follower and the shared event formatter."""
import json

import pytest

import run


def test_log_tail_yields_only_new_complete_lines(tmp_path):
    log = tmp_path / "extract-1.log"
    log.write_text("line from a previous session\n")
    tail = run.LogTail(tmp_path)  # starts at the current end: a resumed run does not reprint old lines
    with open(log, "a") as f:
        f.write("[extract-1] task 1: claimed\n[extract-1] task 1: do")
    assert list(tail.new_lines()) == [("extract-1", "[extract-1] task 1: claimed")]
    with open(log, "a") as f:  # the partial line is completed later
        f.write("ne\n")
    assert list(tail.new_lines()) == [("extract-1", "[extract-1] task 1: done")]
    (tmp_path / "search-1.log").write_text("first line of a new file\n")  # created after start: read from 0
    assert list(tail.new_lines()) == [("search-1", "first line of a new file")]
    assert list(tail.new_lines()) == []


@pytest.mark.parametrize("kind", run.LIVE_EVENTS)
def test_every_live_event_kind_has_a_readable_line(kind):
    row = {"kind": kind, "detail_json": json.dumps({"error": "x" * 500}), "task_id": 7, "subq_id": 2}
    line = run.describe_event(row)
    assert line != kind and not line.startswith(f"{kind} {{")  # a dedicated formatter, not the raw-JSON fallback
    assert len(line) < 260                 # long details are truncated


def test_review_line_lists_actions_without_repeating_reasons():
    row = {"kind": "review", "task_id": None, "subq_id": None, "detail_json": json.dumps(
        {"round": 1, "applied": ["more_search SQ4 (focus): a very long reason " * 5, "complete: done"]})}
    assert run.describe_event(row) == "review round 1: more_search SQ4 (focus), complete"
