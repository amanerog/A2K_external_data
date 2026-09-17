"""Unit tests for direct_agent.py's pure merge functions
(_merge_ask_contents/_merge_search_contents) -- the citationIndexes
re-indexing across multiple vendors' results is exactly the kind of
off-by-one-prone logic worth covering directly, and unlike phase 1/phase 2
(_decide_vendors_sync/_run_vendor_agent_sync, which need live Bedrock) this
needs no network access at all.

Not part of the main `pytest tests/` suite (pyproject.toml's testpaths is
scoped to a2k/'s own tests/ directory) -- run from within this directory:

    cd agent && python -m pytest test_direct_agent_merge.py -v

(same shared .venv as the rest of agent/'s test_*.py scripts -- see this
directory's README "Setup").
"""

import pytest

import direct_agent as da


def test_merge_ask_contents_reindexes_citations_across_vendors(monkeypatch):
    # Two vendors -> _merge_ask_contents runs the conflict-check pass, which
    # normally makes a live Bedrock call (_check_conflicts_sync) -- stub it
    # out since this test is about citation re-indexing, not conflict
    # detection (see test_merge_ask_contents_conflict_check_is_invoked below
    # for that).
    monkeypatch.setattr(da, "_check_conflicts_sync", lambda claims, vendor_ids, *, model_id, region: [])

    cala = da.AskContent(
        answer="Cala says X.",
        claims=[da.ClaimOut(text="claim from cala", citationIndexes=[0])],
        citations=[da.CitationOut(title="Cala doc", sourceUrl="https://cala.example/1")],
        groundedRatio=0.9,
    )
    sayari = da.AskContent(
        answer="Sayari says Y.",
        claims=[da.ClaimOut(text="claim from sayari", citationIndexes=[0])],
        citations=[da.CitationOut(title="Sayari doc", sourceUrl="https://sayari.example/1")],
        groundedRatio=0.8,
    )

    merged = da._merge_ask_contents([cala, sayari], ["cala", "sayari"], model_id="unused", region="eu-west-1")

    assert merged["answer"] == "Cala says X.\n\nSayari says Y."
    assert len(merged["citations"]) == 2
    # sayari's claim originally pointed at index 0 of its OWN citations list;
    # after merging, cala's single citation occupies index 0, so sayari's
    # claim must be re-pointed to index 1, not left at 0 (which would now
    # wrongly point at cala's citation).
    assert merged["claims"][0]["citationIndexes"] == [0]
    assert merged["claims"][1]["citationIndexes"] == [1]
    assert merged["groundedRatio"] == pytest.approx((0.9 + 0.8) / 2)


def test_merge_ask_contents_conflict_check_is_invoked_for_multiple_vendors(monkeypatch):
    calls = []

    def fake_check_conflicts(claims, vendor_ids, *, model_id, region):
        calls.append((claims, vendor_ids))
        return [{"thisClaimIndex": 0, "otherClaimIndex": 1, "nature": "value-conflict", "assessment": "a", "rationale": "b"}]

    monkeypatch.setattr(da, "_check_conflicts_sync", fake_check_conflicts)

    cala = da.AskContent(answer="X", claims=[da.ClaimOut(text="cala claim")], citations=[], groundedRatio=1.0)
    sayari = da.AskContent(answer="Y", claims=[da.ClaimOut(text="sayari claim")], citations=[], groundedRatio=1.0)

    merged = da._merge_ask_contents([cala, sayari], ["cala", "sayari"], model_id="unused", region="eu-west-1")

    assert len(calls) == 1
    # each merged claim is tagged with the vendor it actually came from, in order
    assert calls[0][1] == ["cala", "sayari"]
    assert merged["conflicts"][0]["nature"] == "value-conflict"


def test_merge_ask_contents_single_vendor_skips_conflict_check():
    cala = da.AskContent(
        answer="Cala says X.",
        claims=[da.ClaimOut(text="claim", citationIndexes=[])],
        citations=[],
        groundedRatio=1.0,
    )
    merged = da._merge_ask_contents([cala], ["cala"], model_id="unused", region="eu-west-1")
    # Only one vendor queried -- nothing to compare, and no Bedrock call should
    # be attempted (would fail/hang without real credentials if it were).
    assert merged["conflicts"] == []


def test_merge_ask_contents_empty_input_is_safe():
    merged = da._merge_ask_contents([], [], model_id="unused", region="eu-west-1")
    assert merged == {"answer": None, "claims": [], "citations": [], "groundedRatio": 0.0, "conflicts": []}


def test_merge_search_contents_reindexes_citations_across_vendors():
    cala = da.SearchContent(
        passages=[da.PassageOut(text="p1", citationIndexes=[0])],
        citations=[da.CitationOut(title="Cala doc")],
    )
    sayari = da.SearchContent(
        passages=[da.PassageOut(text="p2", citationIndexes=[0])],
        citations=[da.CitationOut(title="Sayari doc")],
    )
    merged = da._merge_search_contents([cala, sayari])

    assert len(merged["citations"]) == 2
    assert merged["passages"][0]["citationIndexes"] == [0]
    assert merged["passages"][1]["citationIndexes"] == [1]
