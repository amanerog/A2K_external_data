"""Tests for deploy/agentcore/vendor_self_consistency.py: question loading,
selection by domain and the summary. Network-free.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from a2k.logline import query_hash

_spec = importlib.util.spec_from_file_location("vendor_self_consistency", Path(__file__).parents[1] / "deploy/agentcore/vendor_self_consistency.py")
vsc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vsc)


def test_loads_the_ground_truth_input_csv(tmp_path):
    path = tmp_path / "gt.csv"
    path.write_text('ID;Use case;Client quesiton;Expected answer;Source\n30;FIU;"¿La empresa X tiene\nquiebras?";No;BOE\n31;FIU;;x;y\n', encoding="utf-8")
    assert vsc.load_questions(path) == [{"id": "30", "query": "¿La empresa X tiene\nquiebras?"}]


def test_loads_a_results_csv(tmp_path):
    path = tmp_path / "r.csv"
    path.write_text("id,query,verdict\n7,Who owns Revolut?,ACCEPTABLE\n", encoding="utf-8")
    assert vsc.load_questions(path) == [{"id": "7", "query": "Who owns Revolut?"}]


def test_selects_by_ids_domain_and_limit():
    qs = [{"id": str(i), "query": f"q{i}"} for i in range(5)]
    domain_of = vsc.domains_by_hash([
        {"query_sha256": query_hash("q0"), "domain": "CRP"},
        {"query_sha256": query_hash("q1"), "domain": "REG"},
        {"query_sha256": query_hash("q2"), "domain": "REG"},
        {"query_sha256": query_hash("q2"), "domain": "CRP"},  # the latest line wins
        {"query_sha256": query_hash("q3"), "domain": "CRP"},
    ])
    assert [q["id"] for q in vsc.select(qs, ids=None, domain_of=domain_of, domain="CRP", limit=None)] == ["0", "2", "3"]
    assert [q["id"] for q in vsc.select(qs, ids={"2", "3", "4"}, domain_of=domain_of, domain="CRP", limit=1)] == ["2"]
    assert len(vsc.select(qs, ids=None, domain_of=None, domain=None, limit=None)) == 5


def test_summary_counts_false_hit_verdicts_over_the_compared_ones():
    results = [{"verdict": v} for v in ["equivalent"] * 6 + ["contradicts"] * 2 + ["cached_outdated", "cached_less_complete", "ERROR"]]
    s = vsc.summary(results)
    assert (s["compared"], s["false_hits"], s["false_hit_pct"]) == (10, 3, 30.0)
    low, high = s["ci95"]
    assert low < 30.0 < high and (low, high) == (10.8, 60.3)


def test_wilson_of_nothing_is_zero():
    assert vsc.wilson(0, 0) == (0.0, 0.0)
