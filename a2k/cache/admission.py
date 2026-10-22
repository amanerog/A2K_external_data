"""Admission of a vendor answer into the cache (proposal Sheet 3, Annex A4),
run in the background after the user already has the answer.

Steps, numbered as in the proposal:
  1  register the candidate: evidence bundle to S3, PENDING row in Aurora
  2  technical filters T1, T2, T4, T5 (+ an evidence-size cap)
     -- T6: skip the paid steps if the row is no longer PENDING
  3  domain classifier (light model) -- a switched-off domain stops here,
     and so does a question that is not self-contained (NOT FIT)
  4  judge (judge model) -- ADMISSION_MODE "basic" (default): covers the
     question, right subject, grounding only in ADMISSION_GROUNDING_DOMAINS
     (judge_basic.py); "strict": the per-domain judge (judge.py, prompts/)
  5  decision rule in code -- NOT FIT deletes the PENDING row; in strict
     mode a FIT verdict is confirmed by a second judgement (confirm_fit)
  6  normalise to English (light model): the question with retrieval's own
     normalise-and-classify call (language + question_en), then the answer
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

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import classifier, decision, filters, judge, judge_basic, metrics, normalize, retrieval, usage
from ..logline import emit, query_hash
from .bedrock import embed
from .evidence import bundle, cap_sections, cited_evidence_text, evidence_key, evidence_text
from .expiry import expires_at
from .models import AdmissionResult, Candidate, Normalized
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
    # "basic" (default, decided 2026-10-06): the answer covers what was asked,
    # about the right subject, with at least one source (T5); grounding (C3)
    # only in grounding_domains. "strict": the delivered judge with its domain
    # modules, verified quotes and confirm_fit (prompts/, judge.py).
    mode: str = "basic"
    grounding_domains: tuple[str, ...] = ("RSK",)
    # Basic mode: what the judge reads (decided 2026-10-09, after
    # measure_cited_evidence.py on 20 recorded answers: the outcome changed as
    # often as between two identical full runs -- 18/20 -- at 58% less cost
    # for Linkup). "full": the whole raw tool output. "cited": only what the
    # answer cites (evidence.cited_evidence_text). "capped": cited, and outside
    # the grounding domains each tool call capped at evidence_cap_chars -- there
    # the judge only looks for the date of the data. Strict mode always reads
    # the full output. The S3 bundle always keeps it whole.
    evidence: str = "capped"
    evidence_cap_chars: int = 20_000

    def versions(self, expiry_policy_version: str) -> dict[str, str]:
        strict = self.mode == "strict"
        return {
            "mode": self.mode,
            "classifier": classifier.PROMPT_VERSION,
            "judge_core": judge.CORE_VERSION if strict else judge_basic.PROMPT_VERSION,
            "judge_modules": judge.MODULES_VERSION if strict else "none",
            "rule": decision.RULE_VERSION if strict else decision.BASIC_RULE_VERSION,
            "normalize_question": retrieval.PROMPT_VERSION,
            "normalize": normalize.PROMPT_VERSION,
            "expiry_policy": expiry_policy_version,
            "light_model": self.light_model_id,
            "judge_model": self.judge_model_id,
            "evidence": "full" if strict else (f"{self.evidence}-{self.evidence_cap_chars}" if self.evidence == "capped" else self.evidence),
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
    """Steps 2-8. Never touches storage; `still_pending` is the T6 hook.
    Never raises: a failing step (a denied or failed model call) ends as
    outcome ERROR, keeping the timings and findings of the steps before it."""
    result = AdmissionResult(entry_id=candidate.entry_id, outcome="ERROR", reason="")
    evaluation = Evaluation(result=result)
    try:
        return _evaluate_steps(
            candidate, evaluation, bedrock_client=bedrock_client, settings=settings,
            expiry_policy=expiry_policy, still_pending=still_pending, now=now, embed_enabled=embed_enabled,
        )
    except Exception as exc:  # noqa: BLE001
        result.outcome, result.reason = "ERROR", f"{type(exc).__name__}: {exc}"
        return evaluation


def _evaluate_steps(
    candidate: Candidate,
    evaluation: Evaluation,
    *,
    bedrock_client,
    settings: AdmissionSettings,
    expiry_policy: dict[str, timedelta],
    still_pending: Callable[[], bool],
    now: Callable[[], datetime],
    embed_enabled: bool,
) -> Evaluation:
    result = evaluation.result
    timer = _Timer(result)

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
    # Basic mode checks the size of what its judge will read, once the
    # domain is known (_judge_evidence); strict mode reads it all.
    if settings.mode == "strict" and _too_large(evidence, result, settings):
        return evaluation
    result.verified_sources = checked.verified_sources or []
    # The agent itself says the answer does not give all that was asked: not
    # worth caching, and no judge is paid to find it out (2026-10-08).
    if candidate.coverage in ("partial", "none"):
        result.outcome, result.reason = "FILTERED", f"the agent reports coverage {candidate.coverage}"
        return evaluation

    if not still_pending():
        result.outcome, result.reason = "SUPERSEDED", "T6: entry no longer PENDING (deprecated meanwhile)"
        return evaluation

    with timer.step("classify"):
        result.classification = classifier.classify(
            bedrock_client, model_id=settings.light_model_id, question=candidate.question, today=now().date()
        )
    if result.classification.domain_primary in settings.disabled_domains:
        result.outcome, result.reason = "DOMAIN_OFF", f"domain {result.classification.domain_primary} is switched off"
        return evaluation
    # Decided 2026-10-05: a question that depends on earlier turns ("and its
    # LEI?") is useless to other users however good its answer -- no judge.
    if not result.classification.self_contained:
        result.outcome, result.reason = "NOT_FIT", "question not self-contained (depends on earlier turns)"
        return evaluation

    if settings.mode != "strict":
        grounding = any(d in settings.grounding_domains for d in result.classification.domains)
        judged = _judge_evidence(candidate, evidence, grounding=grounding, settings=settings, result=result)
        if _too_large(judged, result, settings):
            return evaluation
        with timer.step("judge"):
            result.findings = judge_basic.judge(
                bedrock_client,
                model_id=settings.judge_model_id,
                question=candidate.question,
                answer=candidate.answer,
                evidence=judged,
                classification=result.classification,
                citations=candidate.citations,
                today=now().date(),
                grounding=grounding,
            )
        verdict = decision.decide_basic(result.findings)
        if not verdict.fit:
            result.outcome, result.reason = "NOT_FIT", verdict.reason
            return evaluation
        return _expire_normalize_embed(candidate, evaluation, bedrock_client=bedrock_client, settings=settings,
                                       expiry_policy=expiry_policy, now=now, embed_enabled=embed_enabled, timer=timer)

    with timer.step("judge"):
        result.findings = judge.judge(
            bedrock_client,
            model_id=settings.judge_model_id,
            question=candidate.question,
            answer=candidate.answer,
            evidence=evidence,
            classification=result.classification,
            citations=candidate.citations,
            today=now().date(),
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
                today=now().date(),
            )
        confirmed = decision.decide(result.confirmation, result.classification, evidence=evidence, require_verified_quotes=settings.require_verified_quotes)
        if not confirmed.fit:
            result.outcome, result.reason = "NOT_FIT", f"not confirmed by the second judgement: {confirmed.reason}"
            return evaluation

    return _expire_normalize_embed(candidate, evaluation, bedrock_client=bedrock_client, settings=settings,
                                   expiry_policy=expiry_policy, now=now, embed_enabled=embed_enabled, timer=timer)


def _expire_normalize_embed(
    candidate: Candidate,
    evaluation: Evaluation,
    *,
    bedrock_client,
    settings: AdmissionSettings,
    expiry_policy: dict[str, timedelta],
    now: Callable[[], datetime],
    embed_enabled: bool,
    timer: _Timer,
) -> Evaluation:
    """Steps 8, 6 and 7, after a FIT verdict in either mode."""
    result = evaluation.result
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

    # The question goes through retrieval's very own call: the language stored
    # here is compared with the one detected for a new question to choose the
    # original or the English answer, and the stored question_en is what new
    # questions, translated the same way, are matched against.
    with timer.step("normalize_question"):
        original_lang, question_en, _ = retrieval.normalize_and_classify(
            bedrock_client, model_id=settings.light_model_id, question=candidate.question
        )
    with timer.step("normalize"):
        answer_en, claims_en = normalize.translate(
            bedrock_client, model_id=settings.light_model_id, answer=candidate.answer, claims=[c["text"] for c in candidate.claims]
        )
    result.normalized = Normalized(original_lang=original_lang, question_en=question_en, answer_en=answer_en, claims_en=claims_en)

    if embed_enabled:
        with timer.step("embed"):
            evaluation.question_vector = embed(bedrock_client, model_id=settings.embedding_model_id, text=result.normalized.question_en)

    result.outcome, result.reason = "ACTIVE", "FIT"
    return evaluation


def _too_large(evidence: str, result: AdmissionResult, settings: AdmissionSettings) -> bool:
    if len(evidence) <= settings.max_evidence_chars:
        return False
    result.outcome = "FILTERED"
    result.reason = f"evidence too large for the judge: {len(evidence):,} chars > {settings.max_evidence_chars:,}"
    return True


def _judge_evidence(candidate: Candidate, full: str, *, grounding: bool, settings: AdmissionSettings, result: AdmissionResult) -> str:
    """What the basic judge reads (AdmissionSettings.evidence); recorded in
    result.evidence for the log."""
    if settings.evidence == "full":
        text, how = full, {"mode": "full"}
    else:
        text, how = cited_evidence_text(candidate)
        if how["mode"] == "cited" and settings.evidence == "capped" and not grounding:
            capped = cap_sections(text, settings.evidence_cap_chars)
            if len(capped) < len(text):
                text, how = capped, {**how, "mode": "capped", "cap_chars": settings.evidence_cap_chars}
    result.evidence = {**how, "chars": len(text), "full_chars": len(full)}
    return text


def _stored_claims(claims: list[dict[str, Any]], claims_en: list[str]) -> dict[str, Any]:
    """{"original": [{"text", "sources"}], "en": [...]}: each served version of
    the answer keeps its own claims, located in it again when served."""
    original = [{"text": c["text"], "sources": list(c.get("sources") or [])} for c in claims if c.get("text")]
    english = [{"text": t, "sources": list(c.get("sources") or [])} for c, t in zip(claims, claims_en) if t] if claims_en else []
    return {"original": original, "en": english}


def _findings_dict(f) -> dict[str, Any]:
    return {
        "elements": [asdict(e) for e in f.elements],
        "core": f.core,
        "modules": f.modules,
        "unsupported_claims": f.unsupported_claims,
        "justification": f.justification,
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


def log_result(result: AdmissionResult, *, candidate: Candidate, evidence_uri: str | None, mode: str | None = None) -> None:
    c = result.classification
    record = {
        "event": "cache.admission",
        "entry_id": result.entry_id,
        "query_sha256": query_hash(candidate.question),
        "vendor": candidate.vendor,
        "outcome": result.outcome,
        "reason": result.reason,
        "mode": mode,
        "domains": c.domains if c else None,
        "original_lang": result.normalized.original_lang if result.normalized else None,
        "requested_elements": [asdict(e) for e in c.requested_elements] if c else None,
        "self_contained": c.self_contained if c else None,
        # [{"name", "kind", "identifiers"}] -- the most asked-for entities (dashboard T4.1).
        "entities": c.entities if c else None,
        "findings": _findings_json(result),
        "expires_at": result.expires_at.isoformat() if result.expires_at else None,
        "evidence_uri": evidence_uri,
        "timings_ms": result.timings_ms,
        "evidence": result.evidence,
        "usage": tracked.summary() if (tracked := usage.current()) else None,
    }
    emit(record)
    metrics.emit_admission(record)


def admit(
    candidate: Candidate,
    *,
    bedrock_client,
    store: CacheStore,
    evidence_store: EvidenceStore,
    settings: AdmissionSettings,
) -> AdmissionResult:
    """Steps 1-9. Never raises: an admission failure only means an answer
    that does not get cached; the user already has it. Its log line carries
    the tokens and estimated cost of its model calls; afterwards the cache
    size is reported, at most every config.cache_size_metric_seconds."""
    with usage.track():
        result = _admit_steps(candidate, bedrock_client=bedrock_client, store=store, evidence_store=evidence_store, settings=settings)
    metrics.maybe_emit_cache_size(store)
    return result


def _admit_steps(
    candidate: Candidate,
    *,
    bedrock_client,
    store: CacheStore,
    evidence_store: EvidenceStore,
    settings: AdmissionSettings,
) -> AdmissionResult:
    evidence_uri = None
    try:
        evidence_uri = evidence_store.put(evidence_key(candidate), bundle(candidate))
        store.create_pending(candidate, evidence_uri)
    except Exception as exc:  # noqa: BLE001
        result = AdmissionResult(candidate.entry_id, "ERROR", f"register: {type(exc).__name__}: {exc}")
        log_result(result, candidate=candidate, evidence_uri=evidence_uri, mode=settings.mode)
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
                    requested_elements=[asdict(e) for e in c.requested_elements],
                    time_anchor=c.time_anchor,
                    data_as_of=f.data_as_of,
                    expires_at=result.expires_at,
                    judge_findings=_findings_json(result) or {},
                    embedding_model=settings.embedding_model_id,
                    admission_versions=settings.versions(policy_version),
                    # text[] reserved for invalidation by entity: names and identifiers.
                    entities=[t for e in c.entities for t in (e["name"], *e["identifiers"])],
                    claims=_stored_claims(candidate.claims, n.claims_en),
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
    log_result(result, candidate=candidate, evidence_uri=evidence_uri, mode=settings.mode)
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


def run_in_background(fn: Callable[[], None], *, workers: int) -> None:
    """Runs `fn` in the cache's thread pool and returns at once; counted in
    in_flight() (so /ping reports HealthyBusy) until it finishes. `fn` must
    handle its own errors."""
    global _in_flight
    with _executor_lock:
        _in_flight += 1

    def _run() -> None:
        global _in_flight
        try:
            fn()
        finally:
            with _executor_lock:
                _in_flight -= 1

    _get_executor(workers).submit(_run)


def submit(candidate: Candidate, *, settings: AdmissionSettings, deps: Callable[[], tuple[Any, CacheStore, EvidenceStore]]) -> None:
    """Returns immediately. `deps` builds (bedrock_client, store,
    evidence_store) lazily inside the worker, so nothing on the request path
    touches AWS."""

    def _admit() -> None:
        try:
            bedrock_client, store, evidence_store = deps()
            admit(candidate, bedrock_client=bedrock_client, store=store, evidence_store=evidence_store, settings=settings)
        except Exception as exc:  # noqa: BLE001
            log_result(AdmissionResult(candidate.entry_id, "ERROR", f"deps: {type(exc).__name__}: {exc}"), candidate=candidate, evidence_uri=None)

    run_in_background(_admit, workers=settings.workers)
