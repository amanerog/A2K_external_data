"""Tests for cache retrieval (a2k/cache/retrieval.py) and for how the
gateway uses it in each CACHE_MODE: in shadow the user always gets the vendor
answer and the serving log says what the cache would have served; in serve a
hit replaces the vendor call.

No AWS: entries are admitted into a MemoryStore with the same fake Bedrock
the admission tests use, extended with the retrieval and Checker prompts.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date, datetime, timedelta, timezone

import pytest

from a2k.cache import admission, retrieval
from a2k.cache.store import MemoryEvidenceStore, MemoryStore
from a2k.config import Config, config
from a2k.gateway.engine import GatewayEngine
from a2k.models.request import A2KRequest
from test_cache_admission import SETTINGS, FakeBedrock, make_candidate

RETRIEVAL_SETTINGS = retrieval.RetrievalSettings(light_model_id="light", checker_model_id="checker", embedding_model_id="titan")


class RetrievalBedrock(FakeBedrock):
    def __init__(self, *, equivalent=True, satisfies=True, **kw):
        super().__init__(**kw)
        self.equivalent = equivalent
        self.satisfies = satisfies

    def converse(self, *, modelId, messages, inferenceConfig):
        prompt = messages[0]["content"][0]["text"]
        if prompt.startswith("Decide whether these two questions"):
            text, kind = json.dumps({"equivalent": self.equivalent, "reason": "r1"}), "filter1"
        elif prompt.startswith("A caller previously asked"):
            text, kind = json.dumps({"satisfies": self.satisfies, "reason": "r2"}), "filter2"
        else:
            return super().converse(modelId=modelId, messages=messages, inferenceConfig=inferenceConfig)
        self.calls.append(kind)
        return {"output": {"message": {"content": [{"text": text}]}}, "stopReason": "end_turn"}


def store_with_entry(**store_kw) -> tuple[MemoryStore, str]:
    store = MemoryStore(**store_kw)
    result = admission.admit(make_candidate(), bedrock_client=RetrievalBedrock(), store=store, evidence_store=MemoryEvidenceStore(), settings=SETTINGS)
    assert result.outcome == "ACTIVE"
    return store, result.entry_id


# --- retrieval.lookup --------------------------------------------------------


def test_hit_serves_original_answer_in_the_same_language():
    store, entry_id = store_with_entry()
    bedrock = RetrievalBedrock(lang="es")
    result = retrieval.lookup("¿Qué responsabilidades...?", bedrock_client=bedrock, store=store, settings=RETRIEVAL_SETTINGS)
    assert result.error is None
    assert result.hit.entry_id == entry_id
    assert result.hit.served_version == "original"
    assert result.hit.answer.startswith("Según las Directrices")
    assert result.hit.sources and result.threshold == 0.90
    assert bedrock.calls == ["normalize_classify", "embed", "filter1", "filter2"]


def test_hit_serves_english_answer_to_another_language():
    store, _ = store_with_entry()
    result = retrieval.lookup("What responsibilities...?", bedrock_client=RetrievalBedrock(lang="en"), store=store, settings=RETRIEVAL_SETTINGS)
    assert result.hit.served_version == "english"
    assert result.hit.answer == "Under the EBA..."


def test_checker_filter_1_rejects_without_paying_filter_2():
    store, entry_id = store_with_entry()
    bedrock = RetrievalBedrock(equivalent=False)
    result = retrieval.lookup("q", bedrock_client=bedrock, store=store, settings=RETRIEVAL_SETTINGS)
    assert result.hit is None
    assert result.candidates == [{"entry_id": entry_id, "cosine": 1.0}]
    assert result.checks == [{"entry_id": entry_id, "equivalent": False, "equivalent_reason": "r1"}]
    assert "filter2" not in bedrock.calls


def test_threshold_is_the_stricter_of_two_domains():
    store, _ = store_with_entry(min_cosine={"REG": 0.92, "MED": 1.01})
    result = retrieval.lookup("q", bedrock_client=RetrievalBedrock(domains=("REG", "MED")), store=store, settings=RETRIEVAL_SETTINGS)
    assert result.threshold == 1.01
    assert result.candidates == [] and result.hit is None


def test_expired_and_non_active_entries_are_never_candidates():
    store, entry_id = store_with_entry()
    store.rows[entry_id]["activation"].expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert retrieval.lookup("q", bedrock_client=RetrievalBedrock(), store=store, settings=RETRIEVAL_SETTINGS).candidates == []

    store, entry_id = store_with_entry()
    store.rows[entry_id]["status"] = "DEPRECATED"
    assert retrieval.lookup("q", bedrock_client=RetrievalBedrock(), store=store, settings=RETRIEVAL_SETTINGS).candidates == []


def test_lookup_never_raises():
    class Broken(MemoryStore):
        def search(self, *a, **kw):
            raise RuntimeError("database down")

    result = retrieval.lookup("q", bedrock_client=RetrievalBedrock(), store=Broken(), settings=RETRIEVAL_SETTINGS)
    assert result.hit is None and "database down" in result.error


# --- gateway modes -----------------------------------------------------------

VENDOR_CONTENT = {
    "answer": "Fresh vendor answer, long enough to matter.",
    "claims": [],
    "citations": [{"title": "vendor doc", "sourceUrl": "https://vendor.example/doc"}],
    "groundedRatio": 0.9,
    "conflicts": [],
    "toolCalls": [{"vendor": "cala", "toolName": "knowledge_search", "status": "success"}],
}

HIT = retrieval.CacheHit(
    entry_id="cached-1",
    question_text="What responsibilities...?",
    answer="Cached answer.",
    served_version="original",
    sources=[{"title": "EBA GL", "sourceUrl": "https://www.eba.europa.eu/gl", "documentId": None}],
    cosine=0.97,
    admitted_at=datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc),
    data_as_of=date(2026, 9, 20),
)


@pytest.fixture
def gateway(monkeypatch):
    """GatewayEngine with the vendor call, retrieval and admission faked.
    `state` records what happened; set state["mode"] before calling."""
    state = {"mode": "shadow", "vendor_calls": 0, "lookups": 0, "submitted": [], "hit": HIT, "lookup_delay": 0.0}

    monkeypatch.setattr(Config, "cache_ready", property(lambda self: state["mode"] in ("shadow", "serve")))
    monkeypatch.setattr(Config, "cache_serving", property(lambda self: state["mode"] == "serve"))
    # The global mode follows state["mode"]; per-domain modes come from
    # cache_domain_modes (empty unless a test sets it).
    monkeypatch.setattr(Config, "cache_mode", property(lambda self: state["mode"]), raising=False)
    monkeypatch.delitem(config.__dict__, "cache_mode")
    monkeypatch.setitem(config.__dict__, "cache_domain_modes", {})

    async def fake_call_agent(self, operation, query, sources, request_id):
        state["vendor_calls"] += 1
        return dict(VENDOR_CONTENT)

    def fake_lookup_sync(self, query):
        state["lookups"] += 1
        time.sleep(state["lookup_delay"])
        return retrieval.RetrievalResult(original_lang="es", question_en="q", domains=["REG"], threshold=0.9, hit=state["hit"], timings_ms={"embed": 5})

    monkeypatch.setattr(GatewayEngine, "_call_agent", fake_call_agent)
    monkeypatch.setattr(GatewayEngine, "_cache_lookup_sync", fake_lookup_sync)
    monkeypatch.setattr(admission, "submit", lambda candidate, **kw: state["submitted"].append(candidate))
    return GatewayEngine(), state


def _serving_log(capsys) -> list[dict]:
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    return [line for line in lines if line.get("event") == "cache.retrieval"]


async def test_shadow_always_answers_from_the_vendor_and_logs_the_would_be_hit(gateway, capsys):
    engine, state = gateway
    envelope = await engine._ask_via_agent(A2KRequest(operation="ask", query="¿Qué responsabilidades...?"))

    assert envelope.answer == VENDOR_CONTENT["answer"]
    assert state["vendor_calls"] == 1 and state["lookups"] == 1
    assert len(state["submitted"]) == 1  # the vendor answer still goes to admission
    (log,) = _serving_log(capsys)
    assert log["served_from"] == "vendor" and log["would_hit"] is True
    assert log["entry_id"] == "cached-1"
    assert log["vendor_entry_id"] == state["submitted"][0].entry_id


async def test_shadow_retrieval_runs_in_parallel_with_the_vendor_call(gateway, monkeypatch):
    engine, state = gateway
    state["lookup_delay"] = 0.3

    async def slow_vendor(self, operation, query, sources, request_id):
        await asyncio.sleep(0.3)
        return dict(VENDOR_CONTENT)

    monkeypatch.setattr(GatewayEngine, "_call_agent", slow_vendor)
    t0 = time.monotonic()
    await engine._ask_via_agent(A2KRequest(operation="ask", query="q"))
    # ~0.3 s, not 0.6 s: the lookup did not add to the vendor time.
    assert time.monotonic() - t0 < 0.55


async def test_serve_hit_skips_the_vendor(gateway, capsys):
    engine, state = gateway
    state["mode"] = "serve"
    envelope = await engine._ask_via_agent(A2KRequest(operation="ask", query="q"))

    assert envelope.answer == "Cached answer."
    assert state["vendor_calls"] == 0 and state["submitted"] == []
    assert envelope.citations[0].sourceUrl == "https://www.eba.europa.eu/gl"
    assert envelope.citations[0].retrievedAt == "2026-09-25T08:00:00Z"
    assert envelope.freshness.validAsOf == "2026-09-20" and envelope.freshness.stale is False
    (log,) = _serving_log(capsys)
    assert log["served_from"] == "cache"


async def test_serve_miss_goes_to_the_vendor_and_admission(gateway, capsys):
    engine, state = gateway
    state["mode"], state["hit"] = "serve", None
    envelope = await engine._ask_via_agent(A2KRequest(operation="ask", query="q"))
    assert envelope.answer == VENDOR_CONTENT["answer"]
    assert state["vendor_calls"] == 1 and len(state["submitted"]) == 1
    (log,) = _serving_log(capsys)
    assert log["served_from"] == "vendor" and log["would_hit"] is False


async def test_serve_lookup_over_time_budget_goes_to_the_vendor(gateway, monkeypatch, capsys):
    engine, state = gateway
    state["mode"], state["lookup_delay"] = "serve", 0.5
    monkeypatch.setitem(config.__dict__, "cache_time_budget_seconds", 0.05)
    envelope = await engine._ask_via_agent(A2KRequest(operation="ask", query="q"))
    assert envelope.answer == VENDOR_CONTENT["answer"]
    (log,) = _serving_log(capsys)
    assert "time budget" in log["error"]


async def test_off_mode_does_nothing(gateway, capsys):
    engine, state = gateway
    state["mode"] = "off"
    envelope = await engine._ask_via_agent(A2KRequest(operation="ask", query="q"))
    assert envelope.answer == VENDOR_CONTENT["answer"]
    assert state["lookups"] == 0 and state["submitted"] == []
    assert _serving_log(capsys) == []


# --- Aurora auto-pause --------------------------------------------------------


class _ResumingError(Exception):
    def __init__(self):
        self.response = {"Error": {"Code": "DatabaseResumingException"}}


class _FlakyRdsData:
    def __init__(self, failures):
        self.failures = failures
        self.calls = 0

    def execute_statement(self, **kw):
        self.calls += 1
        if self.calls <= self.failures:
            raise _ResumingError()
        return {"records": [], "numberOfRecordsUpdated": 1}


def _aurora(client, retries):
    from a2k.cache.store import AuroraStore

    return AuroraStore(rds_data_client=client, cluster_arn="c", secret_arn="s", database="d", resume_retries=retries, sleep=lambda s: None)


def test_admission_store_waits_for_a_paused_cluster():
    client = _FlakyRdsData(failures=2)
    assert _aurora(client, retries=9).delete_pending("x") is True
    assert client.calls == 3


def test_retrieval_store_does_not_wait():
    client = _FlakyRdsData(failures=1)
    with pytest.raises(_ResumingError):
        _aurora(client, retries=0).retrieval_policy()
    assert client.calls == 1


def test_other_errors_are_not_retried():
    class Broken:
        calls = 0

        def execute_statement(self, **kw):
            Broken.calls += 1
            raise RuntimeError("syntax error")

    with pytest.raises(RuntimeError):
        _aurora(Broken(), retries=9).retrieval_policy()
    assert Broken.calls == 1


# --- per-domain switch ------------------------------------------------------


def test_retrieval_stops_after_classifying_a_switched_off_domain():
    store, _ = store_with_entry()
    settings = retrieval.RetrievalSettings(light_model_id="l", checker_model_id="c", embedding_model_id="titan", disabled_domains=("REG",))
    bedrock = RetrievalBedrock()
    result = retrieval.lookup("q", bedrock_client=bedrock, store=store, settings=settings)
    assert result.hit is None and "REG" in result.skipped
    assert bedrock.calls == ["normalize_classify"]


async def test_serve_mode_does_not_serve_a_domain_still_in_shadow(gateway, monkeypatch, capsys):
    engine, state = gateway
    state["mode"] = "serve"
    monkeypatch.setitem(config.__dict__, "cache_domain_modes", {"REG": "shadow"})
    envelope = await engine._ask_via_agent(A2KRequest(operation="ask", query="q"))
    assert envelope.answer == VENDOR_CONTENT["answer"]
    assert state["vendor_calls"] == 1
    (log,) = _serving_log(capsys)
    assert log["would_hit"] is True and log["served_from"] == "vendor" and log["domain_mode"] == "shadow"
