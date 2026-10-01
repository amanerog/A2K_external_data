"""Tests for the cache (a2k/cache/): admission, retrieval, and the gateway's
off / shadow / serve modes.

No AWS: MemoryStore/MemoryEvidenceStore stand in for Aurora and S3, and a
fake Bedrock client answers each Converse call according to which prompt it
received (classifier, judge or translation), so every test controls the
model's output exactly.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from a2k.cache import admission, decision, expiry, filters
from a2k.cache.bedrock import ModelOutputError, converse_json, parse_json_object
from a2k.cache.judge import parse_findings
from a2k.cache.models import Candidate, Classification
from a2k.cache.store import MemoryEvidenceStore, MemoryStore
import a2k.config as config_module
from a2k.config import Config

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
URL = "https://www.eba.europa.eu/guidelines/internal-governance"
QUOTE = "The management body should promote a sound risk culture"


def _tool_output(text_payload: dict) -> dict:
    # Shape the agent's ToolCallLogger records: an MCP result whose text is
    # itself a JSON document serialized into a string.
    return {"status": "success", "content": [{"text": json.dumps(text_payload, ensure_ascii=False)}]}


def make_candidate(**overrides) -> Candidate:
    fields = dict(
        entry_id="11111111-1111-1111-1111-111111111111",
        question="¿Qué responsabilidades atribuye la EBA al órgano de administración respecto a la cultura de riesgos?",
        answer="Según las Directrices de la EBA sobre gobierno interno, el órgano de administración debe promover, "
        "supervisar y evaluar la cultura de riesgos de la entidad, y comunicar sus valores fundamentales. " * 2,
        vendor="cala",
        tool_calls=[
            {
                "vendor": "cala",
                "toolName": "knowledge_search",
                "input": {"input": "EBA management body risk culture"},
                "output": _tool_output({"content": f"{QUOTE} and its core values (café ñ).", "context": [{"origins": [{"document": {"url": URL}}]}]}),
                "status": "success",
                "errorType": None,
            },
            {"vendor": "cala", "toolName": "AskContent", "input": {"answer": "..."}, "output": "ok", "status": "success", "errorType": None},
        ],
        citations=[{"title": "EBA GL internal governance", "sourceUrl": URL, "documentId": None}],
        created_at=NOW,
    )
    fields.update(overrides)
    return Candidate(**fields)


CLASSIFICATION = {
    "domain_primary": "REG",
    "domain_secondary": None,
    "requested_elements": ["responsibilities of the management body regarding risk culture"],
    "entities": ["EBA"],
    "time_anchor": {"type": "relative"},
}


def judge_output(**overrides) -> dict:
    out = {
        "elements": [{"element": "responsibilities", "status": "covered", "evidence_quote": QUOTE}],
        "core": {c: {"result": "pass", "reason": "ok"} for c in ("C1", "C2", "C3", "C4")},
        "modules": {m: {"result": "pass", "reason": "ok"} for m in ("REG-1", "REG-2", "REG-3")},
        "data_kind": "snapshot",
        "data_as_of": "2026-09-20",
    }
    out.update(overrides)
    return out


class FakeBedrock:
    def __init__(self, classification=None, judge=None, normalized=None, raw_texts=None, judge_queue=None, lang="es", domains=("REG", None)):
        self.classification = classification or CLASSIFICATION
        self.judge = judge or judge_output()
        self.judge_queue = list(judge_queue or [])  # successive judge outputs, then self.judge
        self.lang = lang
        self.domains = domains
        self.normalized = normalized or {"original_lang": "es", "question_en": "What responsibilities...?", "answer_en": "Under the EBA..."}
        self.raw_texts = list(raw_texts or [])  # overrides, consumed first
        self.calls: list[str] = []

    def converse(self, *, modelId, messages, inferenceConfig):
        prompt = messages[0]["content"][0]["text"]
        if self.raw_texts:
            text = self.raw_texts.pop(0)
            kind = "raw"
        elif prompt.startswith("You classify questions"):
            text, kind = json.dumps(self.classification), "classify"
        elif prompt.startswith("You decide whether an answer"):
            text, kind = json.dumps(self.judge_queue.pop(0) if self.judge_queue else self.judge), "judge"
        elif prompt.startswith("A question has just been sent"):
            text = json.dumps({"original_lang": self.lang, "question_en": "What responsibilities...?", "domain_primary": self.domains[0], "domain_secondary": self.domains[1]})
            kind = "normalize_classify"
        elif prompt.startswith("Translate a question"):
            text, kind = json.dumps(self.normalized), "normalize"
        else:
            raise AssertionError(f"unexpected prompt: {prompt[:60]}")
        self.calls.append(kind)
        return {"output": {"message": {"content": [{"reasoningContent": {}}, {"text": text}]}}, "stopReason": "end_turn"}

    def invoke_model(self, *, modelId, body):
        self.calls.append("embed")

        class _Body:
            def read(self_inner):
                return json.dumps({"embedding": [0.1] * 1024}).encode()

        return {"body": _Body()}


SETTINGS = admission.AdmissionSettings(light_model_id="light", judge_model_id="judge", embedding_model_id="titan")


# --- filters -----------------------------------------------------------------


def test_filters_pass_and_verify_citation_inside_nested_json():
    result = filters.run(make_candidate(), min_chars=150)
    assert result.passed
    assert [s["sourceUrl"] for s in result.verified_sources] == [URL]


def test_t1_rejects_a_failed_tool_call():
    c = make_candidate()
    c.tool_calls[0]["status"] = "error"
    assert filters.run(c, min_chars=150).failed == "T1"


def test_t2_rejects_short_answer_and_fallback_message():
    assert filters.run(make_candidate(answer="Too short."), min_chars=150).failed == "T2"
    fallback = "x" * 200
    assert filters.run(make_candidate(answer=fallback), min_chars=150, fallback_messages=(fallback,)).failed == "T2"


def test_t4_rejects_answer_without_vendor_data():
    c = make_candidate()
    c.tool_calls[0]["output"] = {"content": [{"text": "   "}]}
    assert filters.run(c, min_chars=150).failed == "T4"


def test_t5_rejects_a_citation_not_in_tool_output():
    c = make_candidate(citations=[{"title": "made up", "sourceUrl": "https://example.com/invented"}])
    result = filters.run(c, min_chars=150)
    assert result.failed == "T5" and "invented" in result.reason


def test_t5_rejects_answer_with_no_source():
    assert filters.run(make_candidate(citations=[]), min_chars=150).failed == "T5"


# --- decision rule -----------------------------------------------------------


def _decide(judge_raw, classification=CLASSIFICATION, require_quotes=True, evidence=QUOTE):
    cls = Classification(**classification)
    return decision.decide(parse_findings(judge_raw, cls), cls, evidence=evidence, require_verified_quotes=require_quotes)


def test_decision_fit_when_everything_passes():
    assert _decide(judge_output()).fit


def test_decision_not_fit_on_partial_element():
    out = judge_output(elements=[{"element": "x", "status": "partial", "evidence_quote": QUOTE}])
    assert not _decide(out).fit


def test_decision_not_applicable_allowed_outside_rsk_but_not_in_rsk():
    out = judge_output()
    out["modules"]["REG-2"] = {"result": "not_applicable", "reason": "no version asked"}
    assert _decide(out).fit

    rsk = {**CLASSIFICATION, "domain_primary": "RSK"}
    out = judge_output(modules={m: {"result": "pass"} for m in ("RSK-1", "RSK-2", "RSK-3", "RSK-4")})
    out["modules"]["RSK-4"] = {"result": "not_applicable"}
    verdict = _decide(out, classification=rsk)
    assert not verdict.fit and "RSK-4" in verdict.reason


def test_decision_skipped_check_counts_as_failed():
    out = judge_output()
    del out["core"]["C3"]
    assert "C3" in _decide(out).reason


def test_decision_rejects_quote_not_in_evidence_unless_disabled():
    out = judge_output(elements=[{"element": "x", "status": "covered", "evidence_quote": "a sentence the vendor never returned"}])
    assert not _decide(out).fit
    assert _decide(out, require_quotes=False).fit


def test_quote_shortened_with_ellipsis_still_verifies_in_order():
    ok = judge_output(elements=[{"element": "x", "status": "covered", "evidence_quote": "The management body... a sound risk culture..."}])
    assert _decide(ok).fit
    swapped = judge_output(elements=[{"element": "x", "status": "covered", "evidence_quote": "a sound risk culture … The management body"}])
    assert not _decide(swapped).fit
    only_dots = judge_output(elements=[{"element": "x", "status": "covered", "evidence_quote": "..."}])
    assert not _decide(only_dots).fit


def test_quote_match_ignores_markdown_and_typographic_quotes():
    from a2k.cache.decision import normalize_for_quote, quote_in_evidence

    corpus = normalize_for_quote('Sources identified him as **"Conspirator 2"** \u2014 Nu Colombia\u2019s application')
    assert quote_in_evidence("identified him as 'Conspirator 2' - Nu Colombia's application", corpus)
    assert not quote_in_evidence("identified him as the main conspirator", corpus)


def test_quote_match_ignores_whitespace_and_case():
    out = judge_output(elements=[{"element": "x", "status": "covered", "evidence_quote": "the MANAGEMENT body   should\npromote"}])
    assert _decide(out).fit


# --- expiry ------------------------------------------------------------------


def _cls(**kw):
    return Classification(**{**CLASSIFICATION, **kw})


def test_expiry_from_data_date_and_shorter_domain():
    policy = expiry.default_policy()
    assert expiry.expires_at(_cls(), date(2026, 9, 20), now=NOW, policy=policy) == datetime(2026, 10, 20, tzinfo=timezone.utc)
    # REG (30 d) + RSK (3 d) -> 3 days, from now when there is no data date.
    assert expiry.expires_at(_cls(domain_secondary="RSK"), None, now=NOW, policy=policy) == NOW + timedelta(days=3)


def test_expiry_of_current_data_counts_from_admission_not_from_the_vendor_capture():
    policy = expiry.default_policy()
    captured_long_ago = date(2026, 7, 2)
    assert expiry.expires_at(_cls(), captured_long_ago, now=NOW, policy=policy, data_kind="current") == NOW + timedelta(days=30)
    assert expiry.expires_at(_cls(), captured_long_ago, now=NOW, policy=policy, data_kind="snapshot") == datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_admit_current_data_with_old_capture_date_is_activated():
    bedrock = FakeBedrock(judge=judge_output(data_kind="current", data_as_of="2026-07-02"))
    result, store, _, _ = _admit(bedrock=bedrock)
    assert result.outcome == "ACTIVE"
    activation = store.rows[result.entry_id]["activation"]
    assert activation.data_as_of == date(2026, 7, 2)  # kept, to show how old the data is
    assert activation.judge_findings["data_kind"] == "current"


def test_judge_without_data_kind_counts_as_snapshot():
    findings = parse_findings({k: v for k, v in judge_output().items() if k != "data_kind"}, Classification(**CLASSIFICATION))
    assert findings.data_kind == "snapshot"


def test_expiry_historical_and_future_dates():
    policy = expiry.default_policy()
    hist = _cls(time_anchor={"type": "absolute", "start": "2023-10-01", "end": "2023-12-31"}, domain_primary="MED")
    assert expiry.expires_at(hist, date(2024, 1, 15), now=NOW, policy=policy) == datetime(2025, 1, 14, tzinfo=timezone.utc)
    # A data date after now is a misread: counted from now, not from the future.
    assert expiry.expires_at(_cls(), date(2027, 1, 1), now=NOW, policy=policy) == NOW + timedelta(days=30)


# --- judge output parsing and Bedrock JSON -----------------------------------


def test_judge_prompt_includes_the_answer_citations():
    from a2k.cache.judge import build_prompt

    prompt = build_prompt(question="q", answer="a", evidence="e", classification=Classification(**CLASSIFICATION), citations=make_candidate().citations)
    assert "EBA GL internal governance | " + URL in prompt
    assert "(not available for this answer" in build_prompt(question="q", answer="a", evidence="e", classification=Classification(**CLASSIFICATION))


def test_judge_element_count_mismatch_is_an_error():
    with pytest.raises(ModelOutputError):
        parse_findings(judge_output(elements=[]), Classification(**CLASSIFICATION))


def test_parse_json_object_tolerates_fences_and_prose():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('Here it is: {"a": 1} done') == {"a": 1}


def test_converse_json_retries_once_on_bad_json():
    fake = FakeBedrock(raw_texts=['{"a": "unterminated', '{"a": 2}'])
    assert converse_json(fake, model_id="m", prompt="p", max_tokens=10) == {"a": 2}
    fake = FakeBedrock(raw_texts=["nope", "still nope"])
    with pytest.raises(ModelOutputError):
        converse_json(fake, model_id="m", prompt="p", max_tokens=10)


# --- admit() end to end ------------------------------------------------------


def _admit(candidate=None, bedrock=None, store=None):
    store = store or MemoryStore()
    evidence_store = MemoryEvidenceStore()
    bedrock = bedrock or FakeBedrock()
    candidate = candidate or make_candidate()
    result = admission.admit(candidate, bedrock_client=bedrock, store=store, evidence_store=evidence_store, settings=SETTINGS)
    return result, store, evidence_store, bedrock


def test_admit_fit_answer_becomes_active(capsys):
    result, store, evidence_store, bedrock = _admit()
    assert result.outcome == "ACTIVE"
    row = store.rows[result.entry_id]
    assert row["status"] == "ACTIVE"
    activation = row["activation"]
    assert activation.question_text == "What responsibilities...?"
    assert activation.original_lang == "es"
    assert activation.domain_primary == "REG"
    assert activation.data_as_of == date(2026, 9, 20)
    assert [s["sourceUrl"] for s in activation.sources] == [URL]
    assert activation.admission_versions["judge_core"] == "judge-core-v6"
    assert activation.admission_versions["rule"] == "rule-v3"
    assert len(activation.question_vector) == 1024
    assert bedrock.calls == ["classify", "judge", "judge", "normalize", "normalize_classify", "embed"]  # FIT is confirmed
    # Evidence bundle written, without the agent's own AskContent call.
    (bundle,) = evidence_store.objects.values()
    assert [tc["tool"] for tc in bundle["tool_calls"]] == ["knowledge_search"]
    log = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert log["event"] == "cache.admission" and log["outcome"] == "ACTIVE" and "judge" in log["timings_ms"]


def test_admit_not_fit_deletes_pending_row():
    bedrock = FakeBedrock(judge=judge_output(elements=[{"element": "x", "status": "missing", "evidence_quote": None}]))
    result, store, _, _ = _admit(bedrock=bedrock)
    assert result.outcome == "NOT_FIT"
    assert result.entry_id not in store.rows
    assert bedrock.calls == ["classify", "judge"]  # no translation or embedding paid


def test_admit_filtered_answer_never_calls_a_model():
    result, store, _, bedrock = _admit(candidate=make_candidate(citations=[]))
    assert result.outcome == "FILTERED" and result.reason.startswith("T5")
    assert bedrock.calls == []
    assert result.entry_id not in store.rows


def test_admit_rejects_oversized_evidence_before_the_judge():
    settings = admission.AdmissionSettings(light_model_id="l", judge_model_id="j", embedding_model_id="t", max_evidence_chars=10)
    bedrock = FakeBedrock()
    result = admission.admit(make_candidate(), bedrock_client=bedrock, store=MemoryStore(), evidence_store=MemoryEvidenceStore(), settings=settings)
    assert result.outcome == "FILTERED" and "too large" in result.reason
    assert bedrock.calls == []


def test_admit_stale_data_is_not_activated():
    bedrock = FakeBedrock(judge=judge_output(data_as_of="2020-01-01"))
    result, store, _, _ = _admit(bedrock=bedrock)
    assert result.outcome == "STALE"
    assert result.entry_id not in store.rows


class _DeprecatedDuringJudge(MemoryStore):
    """The user reformulated while the judge was running (T6): the row is
    deprecated between the pre-judge check and activation."""

    def __init__(self):
        super().__init__()
        self.checks = 0

    def is_pending(self, entry_id):
        self.checks += 1
        if self.checks > 1:
            self.rows[entry_id]["status"] = "DEPRECATED"
        return super().is_pending(entry_id)


def test_admit_does_not_reactivate_a_deprecated_entry():
    store = _DeprecatedDuringJudge()
    result, store, _, _ = _admit(store=store)
    assert result.outcome == "SUPERSEDED"
    assert store.rows[result.entry_id]["status"] == "DEPRECATED"


def test_admit_model_error_is_logged_not_raised():
    bedrock = FakeBedrock(classification={"domain_primary": "NOPE", "requested_elements": ["x"]})
    result, store, _, _ = _admit(bedrock=bedrock)
    assert result.outcome == "ERROR" and "unknown domain" in result.reason
    assert result.entry_id not in store.rows


def test_submit_runs_in_background_and_tracks_in_flight():
    import threading

    release = threading.Event()
    done = threading.Event()

    def deps():
        release.wait(5)
        return FakeBedrock(), MemoryStore(), MemoryEvidenceStore()

    original = admission.admit

    def tracking_admit(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        finally:
            done.set()

    admission.admit = tracking_admit
    try:
        admission.submit(make_candidate(), settings=SETTINGS, deps=deps)
        assert admission.in_flight() == 1  # returned before the work ran
        release.set()
        assert done.wait(5)
    finally:
        admission.admit = original
    for _ in range(50):
        if admission.in_flight() == 0:
            break
        import time

        time.sleep(0.01)
    assert admission.in_flight() == 0


# --- gateway integration -----------------------------------------------------


@pytest.fixture
def admission_on(monkeypatch):
    monkeypatch.setattr(Config, "cache_ready", property(lambda self: True))
    submitted = []
    monkeypatch.setattr(admission, "submit", lambda candidate, **kw: submitted.append(candidate))
    return submitted


def _content(vendors):
    return {
        "answer": "An answer",
        "citations": [],
        "toolCalls": [{"vendor": v, "toolName": "t", "status": "success"} for v in vendors],
    }


def test_engine_submits_single_allowed_vendor_answers_only(admission_on):
    from a2k.gateway.engine import engine

    entry_id = engine._submit_admission("q", _content(["cala"]))
    assert engine._submit_admission("q", _content(["sayari"])) is None  # not allowed (D11)
    assert engine._submit_admission("q", _content(["cala", "sayari"])) is None  # fan-out
    assert engine._submit_admission("q", _content([])) is None  # no vendor data
    assert [c.vendor for c in admission_on] == ["cala"]
    assert admission_on[0].question == "q"
    assert entry_id == admission_on[0].entry_id


def test_ping_reports_busy_while_admitting(monkeypatch):
    from a2k.mcp_server import server

    monkeypatch.setattr(admission, "in_flight", lambda: 1)
    assert json.loads(asyncio.run(server._ping(None)).body)["status"] == "HealthyBusy"
    monkeypatch.setattr(admission, "in_flight", lambda: 0)
    assert json.loads(asyncio.run(server._ping(None)).body)["status"] == "Healthy"


# --- per-domain switch ------------------------------------------------------


def test_switched_off_domain_stops_before_the_judge():
    settings = admission.AdmissionSettings(light_model_id="l", judge_model_id="j", embedding_model_id="t", disabled_domains=("REG",))
    bedrock = FakeBedrock()
    store = MemoryStore()
    result = admission.admit(make_candidate(), bedrock_client=bedrock, store=store, evidence_store=MemoryEvidenceStore(), settings=settings)
    assert result.outcome == "DOMAIN_OFF"
    assert bedrock.calls == ["classify"]  # judge not paid
    assert result.entry_id not in store.rows


def test_domain_mode_is_the_stricter_of_global_and_domain(monkeypatch):
    from a2k.config import _parse_domain_modes

    modes = _parse_domain_modes("med=off, RSK=shadow, REG=serve, XXX=bogus")
    assert modes == {"MED": "off", "RSK": "shadow", "REG": "serve"}
    monkeypatch.setitem(config_module.config.__dict__, "cache_domain_modes", modes)
    monkeypatch.setitem(config_module.config.__dict__, "cache_mode", "shadow")
    c = config_module.config
    assert c.domain_mode("REG") == "shadow"  # cannot go beyond the global mode
    assert c.domain_mode("MED") == "off"
    assert c.domain_mode("CRP") == "shadow"  # not listed: follows the global mode
    monkeypatch.setitem(c.__dict__, "cache_mode", "serve")
    assert c.domain_mode("REG") == "serve" and c.domain_mode("RSK") == "shadow"
    assert c.cache_disabled_domains == ("MED",)


# --- confirming judgement ------------------------------------------------------


def test_fit_not_confirmed_by_the_second_judgement_is_not_admitted():
    second = judge_output(elements=[{"element": "x", "status": "partial", "evidence_quote": QUOTE}])
    bedrock = FakeBedrock(judge_queue=[judge_output(), second])
    result, store, _, _ = _admit(bedrock=bedrock)
    assert result.outcome == "NOT_FIT" and result.reason.startswith("not confirmed by the second judgement")
    assert bedrock.calls == ["classify", "judge", "judge"]  # no translation paid
    assert result.entry_id not in store.rows


def test_not_fit_does_not_pay_a_second_judgement():
    bedrock = FakeBedrock(judge=judge_output(elements=[{"element": "x", "status": "missing", "evidence_quote": None}]))
    _admit(bedrock=bedrock)
    assert bedrock.calls.count("judge") == 1


def test_confirmation_can_be_switched_off():
    settings = admission.AdmissionSettings(light_model_id="l", judge_model_id="j", embedding_model_id="t", confirm_fit=False)
    bedrock = FakeBedrock()
    result = admission.admit(make_candidate(), bedrock_client=bedrock, store=MemoryStore(), evidence_store=MemoryEvidenceStore(), settings=settings)
    assert result.outcome == "ACTIVE" and bedrock.calls.count("judge") == 1


def test_stored_language_comes_from_the_retrieval_detector():
    """The translation call no longer decides the language: an English question
    about Brazilian registries was read as Portuguese there and English at
    retrieval (e2e, id 38), so the original answer was never served."""
    result, store, _, _ = _admit(bedrock=FakeBedrock(lang="en"))
    assert store.rows[result.entry_id]["activation"].original_lang == "en"
