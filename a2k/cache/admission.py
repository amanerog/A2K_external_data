"""Admission of a vendor answer into the cache (proposal Sheet 3, Annex A4),
run in the background after the user already has the answer.

Steps, numbered as in the proposal:
  1  register the candidate: evidence bundle to S3, PENDING row in Aurora
  2  technical filters T1, T2, T4, T5 (+ an evidence-size cap)
     -- T6: skip the paid steps if the row is no longer PENDING
  3  domain classifier (light model) -- a switched-off domain stops here
  4  per-domain judge (judge model)
  5  decision rule in code -- NOT FIT deletes the PENDING row; a FIT
     verdict is confirmed by a second judgement (confirm_fit)
  6  normalise question and answer to English (light model)
  7  embed question_en (Titan v2)
  8  expiry from expiry_policy -- already expired deletes the row
  9  activate: one conditional UPDATE, only if still PENDING

Step 8 runs before 6 and 7 here: expiry only needs the classifier and judge
outputs, and an answer that is already stale should not pay for translation.

`evaluate()` runs steps 2-8 with no storage at all, so the same code can be
run offline over POC answers. `admit()` adds steps 1 and 9 around it.
`submit()` hands a candidate to a small thread pool and returns at once;
`in_flight()` is what /ping reports as HealthyBusy, so AgentCore does not
reclaim the session while an admission is still running.

Every admission ends in one JSON log line (`"event": "cache.admission"`)
with its outcome, the reason, the judge findings and per-step timings -- the
raw material to tune the judge afterwards.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import classifier, decision, filters, judge, normalize, retrieval
from .bedrock import embed
from .evidence import bundle, evidence_key, evidence_text
from .expiry import expires_at
from .models import AdmissionResult, Candidate
from .store import Activation, CacheStore, EvidenceStore


@dataclass(frozen=True)
class AdmissionSettings:
    light_model_id: str
    judge_model_id: str
    embedding_model_id: str
    min_answer_chars: int = 150  # D17
    # ~100k tokens. Above it the judge is not called and the answer is not
    # cached: preferring to leave a good answer out (proposal Sheet 3).
    max_evidence_chars: int = 400_000
    require_verified_quotes: bool = True
    fallback_messages: tuple[str, ...] = ()
    allowed_vendors: tuple[str, ...] = ("cala",)  # D11
    workers: int = 2
    # T5. Off only for offline runs over answers recorded without citations.
    check_sources: bool = True
    # Domains switched off (CACHE_DOMAIN_MODES): admission stops after the
    # classifier, before the judge is paid (proposal Annex A6).
    disabled_domains: tuple[str, ...] = ()
    # A FIT verdict is confirmed by a second, independent judge call; the
    # answer is admitted only if both say FIT. The judge cannot be made
    # deterministic (Sonnet 5 takes no temperature) and two identical POCv3
    # runs disagreed on ~7% of answers -- the borderline ones, exactly those
    # a cache should not hold. Only FIT verdicts pay the second call (~1 in 5).
    confirm_fit: bool = True

    def versions(self, expiry_policy_version: str) -> dict[str, str]:
        return {
            "classifier": classifier.PROMPT_VERSION,
            "judge_core": judge.CORE_VERSION,
            "judge_modules": judge.MODULES_VERSION,
            "rule": decision.RULE_VERSION,
            "normalize": normalize.PROMPT_VERSION,
            "expiry_policy": expiry_policy_version,
            "light_model": self.light_model_id,
            "judge_model": self.judge_model_id,
        }


class _Timer:
    def __init__(self, result: AdmissionResult):
        self._result = result

    def step(self, name: str):
        timer = self

        class _Step:
            def __enter__(self_inner):
                self_inner.t0 = time.monotonic()

            def __exit__(self_inner, *exc):
                timer._result.timings_ms[name] = int((time.monotonic() - self_inner.t0) * 1000)
                return False

        return _Step()


@dataclass
class Evaluation:
    result: AdmissionResult
    question_vector: list[float] = field(default_factory=list)


def evaluate(
    candidate: Candidate,
    *,
    bedrock_client,
    settings: AdmissionSettings,
    expiry_policy: dict[str, timedelta],
    still_pending: Callable[[], bool] = lambda: True,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    embed_enabled: bool = True,
) -> Evaluation:
    """Steps 2-8. Never touches storage; `still_pending` is the T6 hook."""
    result = AdmissionResult(entry_id=candidate.entry_id, outcome="ERROR", reason="")
    timer = _Timer(result)
    evaluation = Evaluation(result=result)

    with timer.step("filters"):
        checked = filters.run(
            candidate,
            min_chars=settings.min_answer_chars,
            fallback_messages=settings.fallback_messages,
            check_sources=settings.check_sources,
        )
        evidence = evidence_text(candidate)
    if not checked.passed:
        result.outcome, result.reason = "FILTERED", f"{checked.failed}: {checked.reason}"
        return evaluation
    if len(evidence) > settings.max_evidence_chars:
        result.outcome = "FILTERED"
        result.reason = f"evidence too large for the judge: {len(evidence):,} chars > {settings.max_evidence_chars:,}"
        return evaluation
    result.verified_sources = checked.verified_sources or []

    if not still_pending():
        result.outcome, result.reason = "SUPERSEDED", "T6: entry no longer PENDING (deprecated meanwhile)"
        return evaluation

    with timer.step("classify"):
        result.classification = classifier.classify(bedrock_client, model_id=settings.light_model_id, question=candidate.question)
    if result.classification.domain_primary in settings.disabled_domains:
        result.outcome, result.reason = "DOMAIN_OFF", f"domain {result.classification.domain_primary} is switched off"
        return evaluation

    with timer.step("judge"):
        result.findings = judge.judge(
            bedrock_client,
            model_id=settings.judge_model_id,
            question=candidate.question,
            answer=candidate.answer,
            evidence=evidence,
            classification=result.classification,
            citations=candidate.citations,
        )

    verdict = decision.decide(result.findings, result.classification, evidence=evidence, require_verified_quotes=settings.require_verified_quotes)
    if not verdict.fit:
        result.outcome, result.reason = "NOT_FIT", verdict.reason
        return evaluation

    if settings.confirm_fit:
        with timer.step("judge_confirm"):
            result.confirmation = judge.judge(
                bedrock_client,
                model_id=settings.judge_model_id,
                question=candidate.question,
                answer=candidate.answer,
                evidence=evidence,
                classification=result.classification,
                citations=candidate.citations,
            )
        confirmed = decision.decide(result.confirmation, result.classification, evidence=evidence, require_verified_quotes=settings.require_verified_quotes)
        if not confirmed.fit:
            result.outcome, result.reason = "NOT_FIT", f"not confirmed by the second judgement: {confirmed.reason}"
            return evaluation

    with timer.step("expiry"):
        result.expires_at = expires_at(
            result.classification,
            result.findings.data_as_of,
            now=now(),
            policy=expiry_policy,
            data_kind=result.findings.data_kind,
        )
    if result.expires_at <= now():
        result.outcome, result.reason = "STALE", f"data already expired at admission (expires_at {result.expires_at.isoformat()})"
        return evaluation

    with timer.step("normalize"):
        result.normalized = normalize.normalize(bedrock_client, model_id=settings.light_model_id, question=candidate.question, answer=candidate.answer)
    # The stored language decides, against the language retrieval detects for a
    # new question, whether the original or the English answer is served -- so
    # it comes from the very same call retrieval makes, not from translation.
    with timer.step("detect_language"):
        result.normalized.original_lang, _, _ = retrieval.normalize_and_classify(
            bedrock_client, model_id=settings.light_model_id, question=candidate.question
        )

    if embed_enabled:
        with timer.step("embed"):
            evaluation.question_vector = embed(bedrock_client, model_id=settings.embedding_model_id, text=result.normalized.question_en)

    result.outcome, result.reason = "ACTIVE", "FIT"
    return evaluation


def _findings_dict(f) -> dict[str, Any]:
    return {
        "elements": [asdict(e) for e in f.elements],
        "core": f.core,
        "modules": f.modules,
        "data_as_of": f.data_as_of.isoformat() if f.data_as_of else None,
        "data_kind": f.data_kind,
    }


def _findings_json(result: AdmissionResult) -> dict[str, Any] | None:
    if result.findings is None:
        return None
    out = _findings_dict(result.findings)
    if result.confirmation is not None:
        out["confirmation"] = _findings_dict(result.confirmation)
    return out


def log_result(result: AdmissionResult, *, candidate: Candidate, evidence_uri: str | None) -> None:
    c = result.classification
    record = {
        "event": "cache.admission",
        "entry_id": result.entry_id,
        "vendor": candidate.vendor,
        "outcome": result.outcome,
        "reason": result.reason,
        "domains": c.domains if c else None,
        "original_lang": result.normalized.original_lang if result.normalized else None,
        "requested_elements": c.requested_elements if c else None,
        "findings": _findings_json(result),
        "expires_at": result.expires_at.isoformat() if result.expires_at else None,
        "evidence_uri": evidence_uri,
        "timings_ms": result.timings_ms,
    }
    print(json.dumps(record, ensure_ascii=False, default=str), file=sys.stdout, flush=True)


def admit(
    candidate: Candidate,
    *,
    bedrock_client,
    store: CacheStore,
    evidence_store: EvidenceStore,
    settings: AdmissionSettings,
) -> AdmissionResult:
    """Steps 1-9. Never raises: an admission failure only means an answer
    that does not get cached; the user already has it."""
    evidence_uri = None
    try:
        evidence_uri = evidence_store.put(evidence_key(candidate), bundle(candidate))
        store.create_pending(candidate, evidence_uri)
    except Exception as exc:  # noqa: BLE001
        result = AdmissionResult(candidate.entry_id, "ERROR", f"register: {type(exc).__name__}: {exc}")
        log_result(result, candidate=candidate, evidence_uri=evidence_uri)
        return result

    result = AdmissionResult(candidate.entry_id, "ERROR", "")
    try:
        policy, policy_version = store.expiry_policy()
        evaluation = evaluate(
            candidate,
            bedrock_client=bedrock_client,
            settings=settings,
            expiry_policy=policy,
            still_pending=lambda: store.is_pending(candidate.entry_id),
        )
        result = evaluation.result
        if result.outcome == "ACTIVE":
            c, f, n = result.classification, result.findings, result.normalized
            assert c and f and n and result.expires_at
            activated = store.activate(
                Activation(
                    entry_id=candidate.entry_id,
                    question_text=n.question_en,
                    question_vector=evaluation.question_vector,
                    answer_text=n.answer_en,
                    original_lang=n.original_lang,
                    sources=result.verified_sources,
                    domain_primary=c.domain_primary,
                    domain_secondary=c.domain_secondary,
                    requested_elements=c.requested_elements,
                    time_anchor=c.time_anchor,
                    data_as_of=f.data_as_of,
                    expires_at=result.expires_at,
                    judge_findings=_findings_json(result) or {},
                    embedding_model=settings.embedding_model_id,
                    admission_versions=settings.versions(policy_version),
                    entities=c.entities,
                )
            )
            if not activated:
                result.outcome, result.reason = "SUPERSEDED", "T6: entry no longer PENDING at activation"
    except Exception as exc:  # noqa: BLE001
        result.outcome, result.reason = "ERROR", f"{type(exc).__name__}: {exc}"

    if result.outcome != "ACTIVE":
        try:
            store.delete_pending(candidate.entry_id)
        except Exception as exc:  # noqa: BLE001 -- the daily cleanup removes orphans (D8)
            result.reason += f" (PENDING row not deleted: {type(exc).__name__})"
    log_result(result, candidate=candidate, evidence_uri=evidence_uri)
    return result


# --- Background execution ----------------------------------------------------

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()
_in_flight = 0


def in_flight() -> int:
    return _in_flight


def _get_executor(workers: int) -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="cache-admission")
        return _executor


def submit(candidate: Candidate, *, settings: AdmissionSettings, deps: Callable[[], tuple[Any, CacheStore, EvidenceStore]]) -> None:
    """Returns immediately. `deps` builds (bedrock_client, store,
    evidence_store) lazily inside the worker, so nothing on the request path
    touches AWS."""
    global _in_flight
    with _executor_lock:
        _in_flight += 1

    def _run() -> None:
        global _in_flight
        try:
            bedrock_client, store, evidence_store = deps()
            admit(candidate, bedrock_client=bedrock_client, store=store, evidence_store=evidence_store, settings=settings)
        except Exception as exc:  # noqa: BLE001
            log_result(AdmissionResult(candidate.entry_id, "ERROR", f"deps: {type(exc).__name__}: {exc}"), candidate=candidate, evidence_uri=None)
        finally:
            with _executor_lock:
                _in_flight -= 1

    _get_executor(settings.workers).submit(_run)
