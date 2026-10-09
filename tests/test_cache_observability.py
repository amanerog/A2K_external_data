"""Tests for the cache's own cost, size and shadow-check metrics (E7.4):
a2k/cache/usage.py, metrics.size_metrics / maybe_emit_cache_size and
a2k/cache/shadow_check.py. Network-free: Bedrock is faked.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from a2k.cache import metrics, retrieval, shadow_check, usage
from a2k.cache.bedrock import converse_json
from a2k.cache.store import Activation, MemoryStore
from a2k.config import config
from test_cache_admission import FakeBedrock


def make_activation(entry_id: str, *, domain_primary: str, expires_at: datetime) -> Activation:
    return Activation(
        entry_id=entry_id, question_text="q", question_vector=[0.1] * 4, answer_text="a", original_lang="en", sources=[],
        domain_primary=domain_primary, domain_secondary=None, requested_elements=[], time_anchor={}, data_as_of=None,
        expires_at=expires_at, judge_findings={}, embedding_model="titan", admission_versions={}, entities=[],
    )


def _lines(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]


class _Answers:
    """Converse returning fixed JSON texts in turn, with token counts."""

    def __init__(self, *texts, input_tokens=2000, output_tokens=50):
        self.texts = list(texts)
        self.usage = {"inputTokens": input_tokens, "outputTokens": output_tokens}

    def converse(self, **kwargs):
        return {"output": {"message": {"content": [{"text": self.texts.pop(0)}]}}, "stopReason": "end_turn", "usage": self.usage}


# --- usage ---------------------------------------------------------------


def test_price_matching_prefers_the_longest_key_and_overrides(monkeypatch):
    assert usage.price_for("eu.anthropic.claude-haiku-4-5-20251001-v1:0") == (1.0, 5.0)
    assert usage.price_for("eu.anthropic.claude-sonnet-4-5-20250929-v1:0") == (3.0, 15.0)
    assert usage.price_for("something-else") is None
    monkeypatch.setitem(config.__dict__, "cache_model_prices", {"claude-sonnet-4-5-20250929": (3.3, 16.5)})
    assert usage.price_for("eu.anthropic.claude-sonnet-4-5-20250929-v1:0") == (3.3, 16.5)


def test_calls_add_up_in_the_tracked_usage_with_their_cost():
    client = _Answers('{"a": 1}', '{"b": 2}')
    with usage.track() as tracked:
        converse_json(client, model_id="eu.anthropic.claude-haiku-4-5-20251001-v1:0", prompt="p", max_tokens=10)
        converse_json(client, model_id="unknown-model", prompt="p", max_tokens=10)
    s = tracked.summary()
    assert (s["input_tokens"], s["output_tokens"]) == (4000, 100)
    assert s["cost_usd"] == pytest.approx((2000 * 1.0 + 50 * 5.0) / 1e6)  # only the priced model
    assert s["unpriced"] == ["unknown-model"]


def test_untracked_calls_are_not_counted():
    converse_json(_Answers('{"a": 1}'), model_id="m", prompt="p", max_tokens=10)
    assert usage.current() is None


def test_a_retry_after_a_truncated_answer_is_counted_too():
    client = _Answers('{"a": 1}', '{"a": 1}')
    first = client.converse
    client.converse = lambda **kw: {**first(**kw), "stopReason": "max_tokens"} if len(client.texts) == 2 else first(**kw)
    with usage.track() as tracked:
        converse_json(client, model_id="m", prompt="p", max_tokens=10)
    assert tracked.summary()["input_tokens"] == 4000


def test_lookup_usage_includes_the_reformulation_thread():
    """Step 1 runs in its own thread; its model call still counts in the lookup."""
    store = MemoryStore()
    store.record_turn("s1", "previous question", None, window_seconds=600)
    bedrock = FakeBedrock(raw_texts=[])
    calls = []
    original = bedrock.converse

    def converse(**kw):
        prompt = kw["messages"][0]["content"][0]["text"]
        if prompt.startswith("Two consecutive questions were asked"):
            calls.append("reformulation")
            return {"output": {"message": {"content": [{"text": '{"kind": "new", "refers_back": "", "reason": "r"}'}]}}, "usage": {"inputTokens": 300, "outputTokens": 20}}
        return original(**kw)

    bedrock.converse = converse
    settings = retrieval.RetrievalSettings(light_model_id="light", checker_model_id="checker", embedding_model_id="titan")
    result = retrieval.lookup("¿Qué dice la EBA?", bedrock_client=bedrock, store=store, settings=settings, session_id="s1")
    assert calls == ["reformulation"]
    # normalize_classify (1000/100) + reformulation (300/20) + embedding (12)
    assert (result.usage["input_tokens"], result.usage["output_tokens"]) == (1312, 120)


# --- cache size ------------------------------------------------------------


def test_memory_store_size_by_state_and_domain():
    store = MemoryStore()
    now = datetime.now(timezone.utc)
    for entry_id, domain, expires in (("a", "REG", now + timedelta(days=1)), ("b", "REG", now - timedelta(days=1)), ("c", "CRP", now + timedelta(days=1))):
        store.rows[entry_id] = {"status": "ACTIVE", "activation": make_activation(entry_id, domain_primary=domain, expires_at=expires)}
    store.rows["d"] = {"status": "PENDING"}
    assert store.size() == [
        {"state": "ACTIVE", "domain": "CRP", "entries": 1},
        {"state": "ACTIVE", "domain": "REG", "entries": 1},
        {"state": "EXPIRED", "domain": "REG", "entries": 1},
        {"state": "PENDING", "domain": "NONE", "entries": 1},
    ]


def test_size_metrics_one_total_per_state_and_one_line_per_domain():
    lines = metrics.size_metrics(
        [{"state": "ACTIVE", "domain": "CRP", "entries": 4}, {"state": "ACTIVE", "domain": "REG", "entries": 6}, {"state": "PENDING", "domain": "NONE", "entries": 1}],
        "A2K/Cache",
    )
    totals = [(x["State"], x["Entries"]) for x in lines if "Domain" not in x]
    assert totals == [("ACTIVE", 10), ("PENDING", 1)]
    per_domain = [x for x in lines if "Domain" in x]
    assert [d["Dimensions"] for x in per_domain for d in x["_aws"]["CloudWatchMetrics"]] == [[["Domain", "State"]]] * 3


def test_cache_size_is_reported_at_most_every_interval(monkeypatch, capsys):
    monkeypatch.setattr(metrics, "_size_last", 0.0)
    monkeypatch.setitem(config.__dict__, "cache_size_metric_seconds", 300.0)
    store = MemoryStore()
    store.rows["d"] = {"status": "PENDING"}
    metrics.maybe_emit_cache_size(store)
    metrics.maybe_emit_cache_size(store)
    assert len([x for x in _lines(capsys) if x.get("source") == "cache.size"]) == 2  # one total + one domain line, once


def test_a_failing_size_query_is_logged_not_raised(monkeypatch, capsys):
    monkeypatch.setattr(metrics, "_size_last", 0.0)

    class Broken:
        def size(self):
            raise RuntimeError("paused")

    metrics.maybe_emit_cache_size(Broken())
    (line,) = _lines(capsys)
    assert line["event"] == "cache.size" and "paused" in line["error"]


# --- shadow check ----------------------------------------------------------


def _run(client, **kw):
    args = dict(
        bedrock_client=client,
        model_id="eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
        question="What is Santander's CET1 ratio?",
        request_id="r1",
        entry_id="e1",
        domain="CRP",
        cosine=0.9612,
        cached_answer="12.8% at Q2 2026.",
        cached_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        fresh_answer="13.1% at Q3 2026.",
    )
    return shadow_check.run(**{**args, **kw})


def test_shadow_check_logs_the_verdict_its_metrics_and_cost(capsys):
    record = _run(_Answers('{"verdict": "cached_outdated", "reason": "Q3 figure supersedes Q2"}'))
    log, metric = _lines(capsys)
    assert log == record and log["event"] == "cache.shadow_check"
    assert (log["verdict"], log["false_hit"], log["domain"], log["cosine"]) == ("cached_outdated", True, "CRP", 0.9612)
    assert log["usage"]["cost_usd"] == pytest.approx((2000 * 3.0 + 50 * 15.0) / 1e6)
    assert (metric["Verdict"], metric["Domain"], metric["ShadowChecks"], metric["FalseHits"]) == ("cached_outdated", "CRP", 1, 1)
    assert [d["Dimensions"] for d in metric["_aws"]["CloudWatchMetrics"]] == [[["Verdict"]], [["Domain"]]]
    assert "CostUSD" in [m["Name"] for m in metric["_aws"]["CloudWatchMetrics"][0]["Metrics"]]


def test_the_prompt_carries_both_answers_and_dates():
    client = _Answers('{"verdict": "equivalent", "reason": "same"}')
    seen = {}
    converse = client.converse
    client.converse = lambda **kw: seen.update(kw) or converse(**kw)
    _run(client)
    prompt = seen["messages"][0]["content"][0]["text"]
    assert "12.8% at Q2 2026." in prompt and "13.1% at Q3 2026." in prompt and "saved on 2026-09-01" in prompt
    assert seen["inferenceConfig"]["temperature"] == 0


@pytest.mark.parametrize("verdict, false_hit", [("equivalent", False), ("cached_less_complete", False), ("fresh_less_complete", False), ("contradicts", True)])
def test_only_outdated_and_contradicting_answers_are_false_hits(verdict, false_hit, capsys):
    assert _run(_Answers(json.dumps({"verdict": verdict, "reason": "r"})))["false_hit"] is false_hit


def test_an_unknown_verdict_is_an_error_not_a_false_hit(capsys):
    record = _run(_Answers('{"verdict": "maybe"}', '{"verdict": "maybe"}'))
    assert record["verdict"] == "ERROR" and record["false_hit"] is False and "unknown verdict" in record["reason"]
