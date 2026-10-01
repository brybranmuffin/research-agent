"""Verification gate in tools.py: exact / normalized / fuzzy / neighbouring-page matching,
rejection of paraphrases, short and non-contiguous quotes, and the verdict rules
(contested outranks supported, independence = distinct first authors of primary sources,
the LLM may only downgrade)."""
import sqlite3

import pytest

import config
from agent import store, tools

CFG = config.Config()


@pytest.fixture
def conn(tmp_path, monkeypatch):
    path = tmp_path / "corpus.db"
    monkeypatch.setattr(config, "CORPUS_DB", path)
    c = sqlite3.connect(path)
    c.executescript(store.CORPUS_SCHEMA)
    page = lambda text: [(text, False)]
    store.insert_document(c, {"id": "ibrahim2020", "type": "pdf", "authors": ["Nizar Ibrahim"]}, [
        page("Spinosaurus had a long neck and a crocodile-like snout suited to catching fish."),
        page("The dense limb bones lacking open medullary cavities are interpreted as an adaptation "
             "for buoyancy control in water."),
        page("The ﬂat-bottomed pedal unguals suggest paddling in shallow water habitats near the shore."),
        page("Computational models show the animal would have been unstable and too buoyant to dive."),
    ])
    store.insert_document(c, {"id": "ibrahim2014", "type": "pdf", "authors": ["N. Ibrahim"]}, [page("x " * 50)])
    store.insert_document(c, {"id": "sereno2022", "type": "pdf", "authors": ["Paul C. Sereno"]}, [page("y " * 50)])
    store.insert_document(c, {"id": "henderson2018", "type": "pdf", "authors": ["Donald M. Henderson"]}, [page("z " * 50)])
    store.insert_document(c, {"id": "wiki", "type": "html", "authors": ["Wikipedia contributors"]}, [page("w " * 50)])
    store.insert_document(c, {"id": "news", "type": "html", "authors": ["A Reporter"]}, [page("n " * 50)])
    c.commit()
    c.close()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(store.STATE_SCHEMA)
    conn.execute("ATTACH DATABASE ? AS corpus", (str(path),))
    return conn


def verify(conn, quote, unit, doc="ibrahim2020"):
    return tools.verify_quote(conn, quote, doc, unit, CFG)


def test_exact_match_on_cited_page(conn):
    v = verify(conn, "dense limb bones lacking open medullary cavities are interpreted as an adaptation", 2)
    assert (v.verified, v.method, v.unit) == (True, "exact", 2)


def test_ligatures_quotes_and_case_are_normalized(conn):
    v = verify(conn, "the flat-bottomed pedal unguals suggest paddling in shallow water", 3)
    assert (v.verified, v.method) == (True, "exact")


def test_small_extraction_noise_passes_fuzzy(conn):
    v = verify(conn, "lacking open medulary cavities are interpreted as an adaptation for buoyancy", 2)
    assert v.verified and v.method == "fuzzy" and v.score >= CFG.fuzzy_threshold


def test_off_by_one_page_is_corrected(conn):
    v = verify(conn, "dense limb bones lacking open medullary cavities are interpreted as an adaptation", 1)
    assert (v.verified, v.method, v.unit) == (True, "neighbor", 2)


def test_quote_two_pages_away_is_rejected(conn):
    assert not verify(conn, "computational models show the animal would have been unstable and too buoyant", 1).verified


def test_paraphrase_is_rejected(conn):
    assert not verify(conn, "its heavy bones helped it control buoyancy while it swam underwater", 2).verified


def test_short_and_noncontiguous_quotes_are_rejected(conn):
    assert "shorter" in verify(conn, "dense limb bones", 2).reason
    assert "ellipsis" in verify(conn, "the dense limb bones ... buoyancy control in water today", 2).reason


def test_invalid_location_is_rejected(conn):
    assert not verify(conn, "dense limb bones lacking open medullary cavities are interpreted", 99).verified


def add_claim(conn, doc, stance, verified=1, subq=1):
    conn.execute("INSERT INTO claims (subq_id, doc_id, unit, text, quote, stance, verified, verify_method, created_at) "
                 "VALUES (?, ?, 1, 't', 'q', ?, ?, 'exact', 0)", (subq, doc, stance, verified))


def test_contested_outranks_supported(conn):
    add_claim(conn, "ibrahim2020", "aquatic")
    add_claim(conn, "henderson2018", "aquatic")
    add_claim(conn, "sereno2022", "wading")
    assert tools.compute_verdict(conn, 1)["verdict"] == "contested"


def test_same_first_author_is_not_independent(conn):
    add_claim(conn, "ibrahim2020", "aquatic")
    add_claim(conn, "ibrahim2014", "aquatic")
    assert tools.compute_verdict(conn, 1)["verdict"] == "thin"


def test_two_independent_primary_sources_support(conn):
    add_claim(conn, "ibrahim2020", "aquatic")
    add_claim(conn, "henderson2018", "aquatic")
    add_claim(conn, "sereno2022", "neutral")
    assert tools.compute_verdict(conn, 1)["verdict"] == "supported"


def test_secondary_sources_do_not_add_independence(conn):
    add_claim(conn, "ibrahim2020", "aquatic")
    add_claim(conn, "wiki", "aquatic")
    add_claim(conn, "news", "aquatic")
    assert tools.compute_verdict(conn, 1)["verdict"] == "thin"


def test_rejected_claims_are_ignored(conn):
    add_claim(conn, "ibrahim2020", "aquatic")
    add_claim(conn, "henderson2018", "aquatic")
    add_claim(conn, "sereno2022", "wading", verified=0)
    assert tools.compute_verdict(conn, 1)["verdict"] == "supported"


@pytest.mark.parametrize("floor, proposed, expected", [
    ("supported", "contested", "contested"), ("supported", "thin", "thin"), ("contested", "thin", "thin"),
    ("thin", "supported", "thin"), ("contested", "supported", "contested"), ("contested", None, "contested")])
def test_llm_may_only_downgrade(floor, proposed, expected):
    assert tools.final_verdict(floor, proposed) == expected


def test_page_windows_center_on_hits_and_do_not_overlap():
    assert tools.page_windows([5, 7], 20, CFG) == [(4, 6), (7, 9)]
    assert tools.page_windows([6, 5, 9], 20, CFG) == [(5, 7), (8, 10)]
    assert tools.page_windows([1], 2, CFG) == [(1, 2)]


def test_grouped_citations_are_split_for_validation_and_rendering():
    assert tools.split_grouped_citations("A [C12, C15]. B [C3]. C [C1; C2 ,C9].") == \
        "A [C12][C15]. B [C3]. C [C1][C2][C9]."
