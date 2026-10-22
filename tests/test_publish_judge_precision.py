"""Tests for deploy/observability/publish_judge_precision.py: the join of a
ground-truth results CSV with the cache.admission lines and the
cache.ground_truth lines it writes. Network-free.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from a2k.logline import query_hash

_spec = importlib.util.spec_from_file_location("publish_judge_precision", Path(__file__).parents[1] / "deploy/observability/publish_judge_precision.py")
pjp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pjp)


def _adm(query, outcome, domain="CRP", entry="e"):
    return {"query_sha256": query_hash(query), "entry_id": entry, "outcome": outcome, "domain": domain}


def test_verdicts_are_keyed_by_the_hash_the_box_logs(tmp_path):
    results = tmp_path / "r.csv"
    results.write_text("id,query,verdict,final_verdict\n1,¿Quién es el CEO de Revolut?,acceptable,NOT_ACCEPTABLE\n2,,ACCEPTABLE,\n", encoding="utf-8")
    assert pjp.load_verdicts(results, "verdict") == {query_hash("¿Quién es el CEO de Revolut?"): "ACCEPTABLE"}
    assert pjp.load_verdicts(results, "final_verdict") == {query_hash("¿Quién es el CEO de Revolut?"): "NOT_ACCEPTABLE"}


def test_a_question_asked_twice_is_judged_on_its_last_admission():
    rows = [_adm("q", "NOT_FIT", entry="old"), _adm("q", "ACTIVE", entry="new")]
    assert pjp.latest_admissions(rows)[query_hash("q")]["entry_id"] == "new"


def test_precision_counts_only_active_graded_answers():
    verdicts = {query_hash(q): v for q, v in [("a", "ACCEPTABLE"), ("b", "NOT_ACCEPTABLE"), ("c", "ACCEPTABLE"), ("d", "ACCEPTABLE"), ("e", "PENDING_MANUAL_REVIEW"), ("f", "ACCEPTABLE")]}
    admissions = pjp.latest_admissions([
        _adm("a", "ACTIVE"),
        _adm("b", "ACTIVE"),
        _adm("c", "ACTIVE", domain="REG"),
        _adm("d", "NOT_FIT"),  # a good answer the judge turned away
        _adm("e", "ACTIVE"),  # not graded: says nothing about the judge
    ])  # "f": no admission (a would-be hit, or a vendor error) -- not counted
    per_domain, matched = pjp.judge_precision(verdicts, admissions)
    assert matched == 5
    assert per_domain == {"CRP": {"active": 2, "active_ok": 1, "missed_good": 1}, "REG": {"active": 1, "active_ok": 1, "missed_good": 0}}


def test_the_lines_match_what_the_go_no_go_table_reads():
    lines = pjp.ground_truth_lines({"CRP": {"active": 3, "active_ok": 2, "missed_good": 1}, "MED": {"active": 0, "active_ok": 0, "missed_good": 2}}, "2026-11-20")
    assert lines == [
        {"event": "cache.ground_truth", "Domain": "CRP", "GroundTruthRun": "2026-11-20", "JudgeEvaluated": 3, "JudgeMissedGood": 1, "JudgePrecisionPct": 66.7},
        # No ACTIVE answer graded: no precision rather than 0 %.
        {"event": "cache.ground_truth", "Domain": "MED", "GroundTruthRun": "2026-11-20", "JudgeEvaluated": 0, "JudgeMissedGood": 2},
    ]
    dashboard = (Path(__file__).parents[1] / "deploy/observability/cache-dashboard.yaml").read_text(encoding="utf-8")
    for field in ("cache.ground_truth", "JudgePrecisionPct", "JudgeEvaluated", "GroundTruthRun"):
        assert field in dashboard
    assert json.loads(json.dumps(lines[0]))["Domain"] == "CRP"
