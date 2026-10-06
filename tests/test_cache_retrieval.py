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

    def converse(self, *, modelId, messages, inferenceConfig, **kw):
        prompt = messages[0]["content"][0]["text"]
        if prompt.startswith("Decide whether these two questions"):
            text, kind = json.dumps({"equivalent": self.equivalent, "reason": "r1"}), "filter1"
        elif prompt.startswith("A caller previously asked"):
            text, kind = json.dumps({"satisfies": self.satisfies, "reason": "r2"}), "filter2"
        else:
            return super().converse(modelId=modelId, messages=messages, inferenceConfig=inferenceConfig, **kw)
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
    state = {"mode": "shadow", "vendor_calls": 0, "lookups": 0, "submitted": [], "hit": HIT, "lookup_delay": 0.0, "session_ids": [], "turns": [], "max_ages": []}

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

    def fake_lookup_sync(self, query, session_id=None, max_age=None):
        state["lookups"] += 1
        state["session_ids"].append(session_id)
        state["max_ages"].append(max_age)
        time.sleep(state["lookup_delay"])
        return retrieval.RetrievalResult(original_lang="es", question_en="q", domains=["REG"], threshold=0.9, hit=state["hit"], timings_ms={"embed": 5})

    monkeypatch.setattr(GatewayEngine, "_call_agent", fake_call_agent)
    monkeypatch.setattr(GatewayEngine, "_cache_lookup_sync", fake_lookup_sync)
    monkeypatch.setattr(admission, "submit", lambda candidate, **kw: state["submitted"].append(candidate))
    monkeypatch.setattr(GatewayEngine, "_record_turn", lambda self, sid, q, entry: state["turns"].append((sid, q, entry)))
    return GatewayEngine(), state


def _serving_log(capsys) -> list[dict]:
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    return [line for line in lines if line.get("event") == "cache.retrieval"]


async def test_shadow_always_answers_from_the_vendor_and_logs_the_would_be_hit(gateway, capsys):
    engine, state = gateway
    envelope = await engine._ask_via_agent(A2KRequest(operation="ask", query="¿Qué responsabilidades...?"))

    assert envelope.answer == VENDOR_CONTENT["answer"]
    assert state["vendor_calls"] == 1 and state["lookups"] == 1
    # An equivalent entry is already cached: its vendor answer is not admitted again.
    assert state["submitted"] == []
    (log,) = _serving_log(capsys)
    assert log["served_from"] == "vendor" and log["would_hit"] is True
    assert log["entry_id"] == "cached-1" and log["vendor_entry_id"] is None


async def test_shadow_miss_sends_the_vendor_answer_to_admission(gateway, capsys):
    engine, state = gateway
    state["hit"] = None
    await engine._ask_via_agent(A2KRequest(operation="ask", query="q"))
    assert len(state["submitted"]) == 1
    (log,) = _serving_log(capsys)
    assert log["would_hit"] is False and log["vendor_entry_id"] == state["submitted"][0].entry_id


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
    assert state["vendor_calls"] == 1 and state["submitted"] == []  # no duplicate of the cached entry
    (log,) = _serving_log(capsys)
    assert log["would_hit"] is True and log["served_from"] == "vendor" and log["domain_mode"] == "shadow"


# --- reformulation (retrieval step 1) and session memory ---------------------


class ReformulationBedrock(RetrievalBedrock):
    def __init__(self, *, kind="reformulation", **kw):
        super().__init__(**kw)
        self.kind = kind

    def converse(self, *, modelId, messages, inferenceConfig, **kw):
        prompt = messages[0]["content"][0]["text"]
        if prompt.startswith("Two consecutive questions"):
            self.calls.append("reformulation")
            return {"output": {"message": {"content": [{"text": json.dumps({"kind": self.kind, "reason": "same ask"})}]}}, "stopReason": "end_turn"}
        return super().converse(modelId=modelId, messages=messages, inferenceConfig=inferenceConfig, **kw)


def test_reformulation_deprecates_the_served_entry_before_searching():
    store, entry_id = store_with_entry()
    store.record_turn("s1", "¿Qué responsabilidades atribuye la EBA...?", entry_id, window_seconds=600)
    bedrock = ReformulationBedrock(kind="reformulation")
    result = retrieval.lookup("Repito: ¿qué exige la EBA al órgano de administración?", bedrock_client=bedrock, store=store, settings=RETRIEVAL_SETTINGS, session_id="s1")
    assert result.reformulation["kind"] == "reformulation" and result.reformulation["deprecated"] is True
    assert store.rows[entry_id]["status"] == "DEPRECATED" and store.rows[entry_id]["status_reason"] == "immediate_reformulation"
    # The deprecated entry is not found again for the reformulated question.
    assert result.candidates == [] and result.hit is None


def test_follow_up_does_not_deprecate():
    store, entry_id = store_with_entry()
    store.record_turn("s1", "q1", entry_id, window_seconds=600)
    result = retrieval.lookup("q2", bedrock_client=ReformulationBedrock(kind="follow_up"), store=store, settings=RETRIEVAL_SETTINGS, session_id="s1")
    assert result.reformulation["kind"] == "follow_up" and "deprecated" not in result.reformulation
    assert store.rows[entry_id]["status"] == "ACTIVE" and result.hit is not None


def test_previous_turn_outside_the_window_calls_no_model():
    store, entry_id = store_with_entry()
    store.record_turn("s1", "q1", entry_id, window_seconds=600)
    store.turns["s1"]["served_at"] -= timedelta(minutes=11)
    bedrock = ReformulationBedrock()
    result = retrieval.lookup("q2", bedrock_client=bedrock, store=store, settings=RETRIEVAL_SETTINGS, session_id="s1")
    assert result.reformulation == {"previous_turn": False}
    assert "reformulation" not in bedrock.calls and store.rows[entry_id]["status"] == "ACTIVE"


def test_without_a_session_id_nothing_is_checked():
    store, entry_id = store_with_entry()
    store.record_turn("s1", "q1", entry_id, window_seconds=600)
    bedrock = ReformulationBedrock()
    result = retrieval.lookup("q2", bedrock_client=bedrock, store=store, settings=RETRIEVAL_SETTINGS)
    assert result.reformulation is None and "reformulation" not in bedrock.calls


def test_reformulation_of_a_pending_candidate_keeps_it_out_of_the_cache():
    """Shadow mode: the user saw the vendor answer, whose candidate is still
    being admitted; the reformulation deprecates it and admission drops it (T6)."""
    from test_cache_admission import make_candidate as mk

    store = MemoryStore()
    candidate = mk()
    store.create_pending(candidate, "memory://x")
    store.record_turn("s1", candidate.question, candidate.entry_id, window_seconds=600)
    retrieval.lookup("q again", bedrock_client=ReformulationBedrock(), store=store, settings=RETRIEVAL_SETTINGS, session_id="s1")
    assert store.rows[candidate.entry_id]["status"] == "DEPRECATED"
    assert not store.is_pending(candidate.entry_id)  # so admission's conditional activation is a no-op (T6)


def test_record_turn_keeps_one_row_per_session_and_purges_old_ones():
    store = MemoryStore()
    store.record_turn("old", "q", None, window_seconds=600)
    store.turns["old"]["served_at"] -= timedelta(minutes=11)
    store.record_turn("s1", "q1", "e1", window_seconds=600)
    store.record_turn("s1", "q2", "e2", window_seconds=600)
    assert set(store.turns) == {"s1"} and store.turns["s1"]["served_entry_id"] == "e2"


async def test_engine_passes_the_session_and_records_the_turn(gateway):
    from a2k.models.request import RequestMetadata

    engine, state = gateway
    req = A2KRequest(operation="ask", query="q", requestMetadata=RequestMetadata(sessionId="conv-42"))
    await engine._ask_via_agent(req)
    assert state["session_ids"] == ["conv-42"]
    # Shadow with a would-be hit: the turn points at the cached entry, as in serve mode.
    assert state["turns"] == [("conv-42", "q", "cached-1")]
    # Shadow miss: it points at the admission candidate made from the vendor answer.
    state["hit"] = None
    await engine._ask_via_agent(A2KRequest(operation="ask", query="q2", requestMetadata=RequestMetadata(sessionId="conv-43")))
    assert state["turns"][-1] == ("conv-43", "q2", state["submitted"][0].entry_id)


async def test_engine_serve_hit_records_the_served_cache_entry(gateway):
    from a2k.models.request import RequestMetadata

    engine, state = gateway
    state["mode"] = "serve"
    await engine._ask_via_agent(A2KRequest(operation="ask", query="q", requestMetadata=RequestMetadata(sessionId="conv-7")))
    assert state["turns"] == [("conv-7", "q", "cached-1")]


def test_mcp_tool_accepts_session_id(monkeypatch):
    from a2k.mcp_server import server

    seen = {}

    async def fake_ask(req):
        seen["session"] = req.requestMetadata.sessionId if req.requestMetadata else None

        class _Env:
            def model_dump(self, **kw):
                return {}

        return _Env()

    monkeypatch.setattr(server.engine, "ask", fake_ask)
    monkeypatch.setattr(server, "_dump", lambda env: {})
    asyncio.run(server.a2k_ask(query="q", sessionId="conv-1"))
    assert seen["session"] == "conv-1"
    asyncio.run(server.a2k_ask(query="q"))
    assert seen["session"] is None


def test_session_falls_back_to_transport_headers():
    from types import SimpleNamespace

    from a2k.mcp_server.server import _session_id

    def ctx(headers):
        return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers=headers)))

    assert _session_id("conv-1", ctx({"mcp-session-id": "m1"})) == "conv-1"  # the caller's wins
    assert _session_id(None, ctx({"mcp-session-id": "m1", "x-amzn-bedrock-agentcore-runtime-session-id": "a1"})) == "mcp:m1"
    assert _session_id(None, ctx({"x-amzn-bedrock-agentcore-runtime-session-id": "a1"})) == "agentcore:a1"
    assert _session_id(None, ctx({})) is None
    assert _session_id(None, None) is None


async def test_serving_log_says_where_the_session_came_from(gateway, capsys):
    from a2k.models.request import RequestMetadata

    engine, _ = gateway
    for sid in ("conv-1", "mcp:abc"):
        await engine._ask_via_agent(A2KRequest(operation="ask", query="q", requestMetadata=RequestMetadata(sessionId=sid)))
    assert [log["session_source"] for log in _serving_log(capsys)] == ["caller", "mcp"]


# --- A2K-KCP 0.1: card revision, freshness limit, strict grounding, asOf --------


def _admit_with(**candidate_kw) -> tuple[MemoryStore, str]:
    store = MemoryStore()
    result = admission.admit(make_candidate(**candidate_kw), bedrock_client=RetrievalBedrock(), store=store, evidence_store=MemoryEvidenceStore(), settings=SETTINGS)
    assert result.outcome == "ACTIVE"
    return store, result.entry_id


def test_entry_of_an_older_card_revision_is_deprecated_not_served():
    store, entry_id = _admit_with(card_revision="rev-1")
    result = retrieval.lookup("q", bedrock_client=RetrievalBedrock(), store=store, settings=RETRIEVAL_SETTINGS, card_revision_of=lambda vendor: "rev-2")
    assert result.hit is None
    assert result.excluded == [{"entry_id": entry_id, "reason": "card_revision_changed"}]
    assert store.rows[entry_id]["status"] == "DEPRECATED" and store.rows[entry_id]["status_reason"] == "card_revision_changed"


def test_entry_of_the_current_card_revision_or_with_none_recorded_is_served():
    for recorded in ("rev-2", None):
        store, entry_id = _admit_with(card_revision=recorded)
        result = retrieval.lookup("q", bedrock_client=RetrievalBedrock(), store=store, settings=RETRIEVAL_SETTINGS, card_revision_of=lambda vendor: "rev-2")
        assert result.hit is not None and result.hit.entry_id == entry_id and result.excluded == []


def test_freshness_limit_excludes_entries_with_older_evidence():
    store, entry_id = _admit_with()  # data_as_of 2026-09-20 in the fake judge
    old = retrieval.lookup("q", bedrock_client=RetrievalBedrock(), store=store, settings=RETRIEVAL_SETTINGS, max_evidence_age=timedelta(hours=1))
    assert old.hit is None and old.excluded == [{"entry_id": entry_id, "reason": "evidence_too_old"}]
    assert store.rows[entry_id]["status"] == "ACTIVE"  # excluded for this caller only, not deprecated
    roomy = retrieval.lookup("q", bedrock_client=RetrievalBedrock(), store=store, settings=RETRIEVAL_SETTINGS, max_evidence_age=timedelta(days=3650))
    assert roomy.hit is not None


def test_hit_carries_the_grounded_ratio_recorded_at_admission():
    store, _ = _admit_with(grounded_ratio=0.5)
    result = retrieval.lookup("q", bedrock_client=RetrievalBedrock(), store=store, settings=RETRIEVAL_SETTINGS)
    assert result.hit.grounded_ratio == 0.5


async def test_served_hit_reports_the_recorded_grounded_ratio_never_more(gateway):
    from dataclasses import replace

    engine, state = gateway
    state["mode"] = "serve"
    state["hit"] = replace(HIT, grounded_ratio=0.5)
    envelope = await engine._ask_via_agent(A2KRequest(operation="ask", query="q"))
    assert envelope.answer == "Cached answer." and envelope.grounding.groundedRatio == 0.5
    state["hit"] = HIT  # admitted before the ratio was recorded
    envelope = await engine._ask_via_agent(A2KRequest(operation="ask", query="q"))
    assert envelope.grounding.groundedRatio == 0.75 and envelope.grounding.strictGroundingSatisfied is False


async def test_strict_grounding_request_skips_the_cache_but_is_still_admitted(gateway, capsys, monkeypatch):
    from a2k.models.request import Requirements

    engine, state = gateway
    # A vendor answer the agent reports as fully grounded, so strict grounding holds for it.
    monkeypatch.setitem(VENDOR_CONTENT, "groundedRatio", 1.0)
    state["mode"] = "serve"
    req = A2KRequest(operation="ask", query="q", requirements=Requirements(strictGrounding=True))
    envelope = await engine._ask_via_agent(req)
    assert envelope.answer == VENDOR_CONTENT["answer"]
    assert state["lookups"] == 0 and state["vendor_calls"] == 1 and len(state["submitted"]) == 1
    (log,) = _serving_log(capsys)
    assert log["skipped"] == "strict grounding requested" and log["would_hit"] is False


async def test_point_in_time_request_skips_the_cache_and_admission(gateway, capsys):
    from a2k.models.request import RequestContext

    engine, state = gateway
    req = A2KRequest(operation="ask", query="q", context=RequestContext(dateContext="2024-06-01T00:00:00Z"))
    await engine._ask_via_agent(req)
    assert state["lookups"] == 0 and state["submitted"] == []
    (log,) = _serving_log(capsys)
    assert log["skipped"].startswith("point-in-time")


async def test_freshness_limit_reaches_retrieval_and_admission_records_card_and_ratio(gateway):
    from a2k.models.request import Requirements

    engine, state = gateway
    state["hit"] = None
    await engine._ask_via_agent(A2KRequest(operation="ask", query="q", requirements=Requirements(maxStalenessHours=24)))
    assert state["max_ages"] == [timedelta(hours=24)]
    (candidate,) = state["submitted"]
    assert candidate.grounded_ratio == 0.9 and candidate.card_revision  # the local Cala card's lastUpdated


# --- full shadow run fixes (2026-10-06) ------------------------------------------


def test_reformulation_guard_different_identifiers_skip_the_model():
    from a2k.cache import reformulation

    class NoModel:
        def converse(self, **kw):
            raise AssertionError("the model must not be called")

    kind, reason = reformulation.classify(
        NoModel(), model_id="m",
        previous="What information is registered for the company with tax code P3701000F?",
        current="What information is registered for the company with tax code B43995760?",
    )
    assert kind == "new" and "identifiers" in reason


def test_reformulation_identifiers_ignore_dates_and_keep_tax_ids():
    from a2k.cache.reformulation import identifiers

    assert identifiers("between 2026-01-01 and 2026-03-31, as of 31/12/2024") == set()
    assert identifiers("CNPJ 18.236.120/0001-58, LEI 5493001KJTIIGC8Y1R12") == {"18236120000158", "5493001KJTIIGC8Y1R12"}
    # Same identifier on both sides: the model decides (it may be a real reformulation).
    assert identifiers("status of P3701000F") & identifiers("registered data for P3701000F")


def test_reformulation_prompt_has_no_literal_backslashes():
    from a2k.cache.reformulation import _PROMPT

    assert "\\" not in _PROMPT


def test_checker_is_told_today_and_when_the_answer_was_produced():
    store, _ = store_with_entry()
    seen = []

    class Recording(RetrievalBedrock):
        def converse(self, *, modelId, messages, inferenceConfig, **kw):
            seen.append(messages[0]["content"][0]["text"])
            return super().converse(modelId=modelId, messages=messages, inferenceConfig=inferenceConfig, **kw)

    retrieval.lookup("q", bedrock_client=Recording(), store=store, settings=RETRIEVAL_SETTINGS)
    checker_prompts = [p for p in seen if p.startswith(("Decide whether these two questions", "A caller previously asked"))]
    today = datetime.now(timezone.utc).date().isoformat()
    assert len(checker_prompts) == 2 and all(f"Today is {today}" in p and f"produced, on {today}" in p for p in checker_prompts)


def test_log_lines_stay_whole_under_concurrency():
    import contextlib
    import io
    import threading

    from a2k.logline import emit

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        threads = [threading.Thread(target=lambda n=n: [emit({"event": "x", "n": n, "i": i, "pad": "y" * 500}) for i in range(200)]) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    lines = buffer.getvalue().splitlines()
    assert len(lines) == 1600 and all(json.loads(line)["event"] == "x" for line in lines)
