"""Shadow check (E7.4, D14's false-hit rate): when the cache finds an entry
but the answer is not served from it -- shadow mode, or a domain still in
shadow -- the user gets a fresh vendor answer to the same question. That pair
is the only first-hand evidence of whether the hit would have been right, so
it is compared, in the background, by the Checker model.

Verdicts:
- equivalent: both give the same facts for what the question asks.
- cached_less_complete: the cached answer covers less of the question; what
  it does say agrees with the fresh one.
- fresh_less_complete: the other way round -- the cache would have done better.
- cached_outdated: the cached facts were true, but the fresh answer has newer
  ones (a later period, an updated figure or status).
- contradicts: the two disagree in a way time does not explain, or the cached
  answer is about another subject.

A false hit is cached_outdated or contradicts. The fresh answer is the
reference, not the truth: a contradiction can be the vendor's fault, which
is why the cached and fresh answers' entry and request are in the log.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timezone
from typing import Any

from . import metrics, usage
from ..logline import emit, query_hash
from .bedrock import ModelOutputError, converse_json

VERSION = "shadow-check-v1"
VERDICTS = ("equivalent", "cached_less_complete", "fresh_less_complete", "cached_outdated", "contradicts")
FALSE_HIT = frozenset({"cached_outdated", "contradicts"})

_PROMPT = """Two answers to the same question are below. Answer A was saved earlier and \
would have been reused; answer B was fetched from the data provider just now. Decide \
whether reusing answer A would have given the person asking the right information.

Today is {today}. Answer A was saved on {cached_on}.

Question:
{question}

Answer A (saved):
{cached}

Answer B (fresh):
{fresh}

Compare only what the question asks for -- not wording, length, order or language.
Choose one verdict:
- "equivalent": both give the same facts for what the question asks.
- "cached_less_complete": A covers less of what is asked than B, and nothing in A \
contradicts B.
- "fresh_less_complete": B covers less of what is asked than A, and nothing in A \
contradicts B.
- "cached_outdated": A's facts refer to an earlier period or an earlier state than B's \
(an older figure, status or event that B updates).
- "contradicts": A and B disagree in a way a later date does not explain, or A is about \
a different subject.

Respond with ONLY this JSON, no other text:
{{"verdict": "one of the five", "reason": "one short sentence naming the fact that decides it"}}
"""


def compare(
    bedrock_client, *, model_id: str, question: str, cached_answer: str, fresh_answer: str, today: date, cached_on: date | None
) -> tuple[str, str]:
    out = converse_json(
        bedrock_client,
        model_id=model_id,
        prompt=_PROMPT.format(
            today=today.isoformat(),
            cached_on=cached_on.isoformat() if cached_on else "an unknown date",
            question=question,
            cached=cached_answer,
            fresh=fresh_answer,
        ),
        max_tokens=400,
        temperature=0,
    )
    verdict = str(out.get("verdict") or "")
    if verdict not in VERDICTS:
        raise ModelOutputError(f"unknown verdict {verdict!r}")
    return verdict, str(out.get("reason") or "")


def run(
    *,
    bedrock_client,
    model_id: str,
    question: str,
    request_id: str,
    entry_id: str,
    domain: str | None,
    cosine: float,
    cached_answer: str,
    cached_at: datetime | None,
    fresh_answer: str,
) -> dict[str, Any]:
    """Compares, logs one cache.shadow_check line and its metrics, and returns
    the record. Never raises."""
    t0 = time.monotonic()
    verdict, reason = "ERROR", ""
    with usage.track() as tracked:
        try:
            verdict, reason = compare(
                bedrock_client,
                model_id=model_id,
                question=question,
                cached_answer=cached_answer,
                fresh_answer=fresh_answer,
                today=datetime.now(timezone.utc).date(),
                cached_on=cached_at.date() if cached_at else None,
            )
        except Exception as exc:  # noqa: BLE001 -- a check must never affect a request
            reason = f"{type(exc).__name__}: {exc}"
    record = {
        "event": "cache.shadow_check",
        "version": VERSION,
        "request_id": request_id,
        "query_sha256": query_hash(question),
        "entry_id": entry_id,
        "domain": domain,
        "cosine": round(cosine, 4),
        "verdict": verdict,
        "false_hit": verdict in FALSE_HIT,
        "reason": reason,
        "model": model_id,
        "ms": int((time.monotonic() - t0) * 1000),
        "usage": tracked.summary(),
    }
    emit(record)
    metrics.emit_shadow_check(record)
    return record
