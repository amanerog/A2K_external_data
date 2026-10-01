"""Retrieval (proposal Sheet 4): is this question already answered in the
cache, and with what?

  2  one light-model call: the question in English, its language, its domain
  3  embed question_en (Titan v2)
  4  search Aurora: ACTIVE, not expired, current embedding model, cosine >=
     the threshold of the question's domain (the stricter if it has two), top 5
  5  Checker on the candidates, best cosine first, until one passes both filters
  6  hit: the original answer if the question is in that language, otherwise
     the English one, always with its sources

Step 1 (reformulation check) and the session memory are not built yet.

`lookup()` never raises: any failure is recorded in the result and the
caller goes to the vendor, as today. What it returns is also what the
serving log records, in shadow mode as well as when serving.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from . import checker
from .bedrock import ModelOutputError, converse_json, embed
from .normalize import LANGUAGE_RULE
from .store import CacheStore
from .taxonomy import DOMAINS, taxonomy_for_prompt

PROMPT_VERSION = "retrieval-normalize-classify-v2"

_PROMPT = """A question has just been sent to an external-data service for a bank. Prepare it for \
a cache lookup. Do not answer it.

<question>
{question}
</question>

Data domains:
{taxonomy}

Return:
- original_lang: {language_rule}
- question_en: the question in English. If it is already in English, unchanged. Keep names of \
people, companies, institutions and products, identifiers, codes, figures and dates exactly as \
written.
- domain_primary: the code of the domain the question mainly belongs to.
- domain_secondary: a second domain code only if the question clearly also asks for data of that \
domain, otherwise null.

Respond with ONLY this JSON object:
{{"original_lang": "es", "question_en": "...", "domain_primary": "...", "domain_secondary": null}}
"""


@dataclass(frozen=True)
class RetrievalSettings:
    light_model_id: str
    checker_model_id: str
    embedding_model_id: str
    default_min_cosine: float = 0.90  # D3, when a domain has no retrieval_policy row
    max_candidates: int = 5
    # Domains switched off (CACHE_DOMAIN_MODES): no search, no Checker.
    disabled_domains: tuple[str, ...] = ()


@dataclass
class CacheHit:
    entry_id: str
    question_text: str
    answer: str
    served_version: str  # "original" | "english"
    sources: list[dict[str, Any]]
    cosine: float
    admitted_at: datetime | None
    data_as_of: date | None


@dataclass
class RetrievalResult:
    original_lang: str | None = None
    question_en: str | None = None
    domains: list[str] = field(default_factory=list)
    threshold: float | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)  # [{"entry_id", "cosine"}]
    checks: list[dict[str, Any]] = field(default_factory=list)
    hit: CacheHit | None = None
    skipped: str | None = None  # why the lookup stopped early on purpose
    error: str | None = None
    timings_ms: dict[str, int] = field(default_factory=dict)

    @property
    def total_ms(self) -> int:
        return sum(self.timings_ms.values())


def normalize_and_classify(bedrock_client, *, model_id: str, question: str) -> tuple[str, str, list[str]]:
    raw = converse_json(
        bedrock_client,
        model_id=model_id,
        prompt=_PROMPT.format(question=question, taxonomy=taxonomy_for_prompt(), language_rule=LANGUAGE_RULE),
        max_tokens=1500,
    )
    question_en = str(raw.get("question_en") or "").strip()
    primary = str(raw.get("domain_primary") or "").upper()
    if not question_en or primary not in DOMAINS:
        raise ModelOutputError(f"normalise/classify returned question_en={question_en!r}, domain={primary!r}")
    secondary = str(raw.get("domain_secondary") or "").upper() or None
    domains = [primary] + ([secondary] if secondary in DOMAINS and secondary != primary else [])
    return str(raw.get("original_lang") or "und").lower()[:8], question_en, domains


def lookup(question: str, *, bedrock_client, store: CacheStore, settings: RetrievalSettings) -> RetrievalResult:
    result = RetrievalResult()
    step_start = time.monotonic()

    def lap(name: str) -> None:
        nonlocal step_start
        now = time.monotonic()
        result.timings_ms[name] = int((now - step_start) * 1000)
        step_start = now

    try:
        result.original_lang, result.question_en, result.domains = normalize_and_classify(
            bedrock_client, model_id=settings.light_model_id, question=question
        )
        lap("normalize_classify")
        if result.domains[0] in settings.disabled_domains:
            result.skipped = f"domain {result.domains[0]} is switched off"
            return result

        vector = embed(bedrock_client, model_id=settings.embedding_model_id, text=result.question_en)
        lap("embed")

        policy = store.retrieval_policy()
        result.threshold = max(policy.get(d, settings.default_min_cosine) for d in result.domains)
        rows = store.search(vector, min_cosine=result.threshold, embedding_model=settings.embedding_model_id, limit=settings.max_candidates)
        result.candidates = [{"entry_id": r["entry_id"], "cosine": round(r["cosine"], 4)} for r in rows]
        lap("search")

        for row in rows:
            check: dict[str, Any] = {"entry_id": row["entry_id"]}
            result.checks.append(check)
            check["equivalent"], check["equivalent_reason"] = checker.questions_equivalent(
                bedrock_client, model_id=settings.checker_model_id, cached_query=row["question_text"], new_query=result.question_en
            )
            if not check["equivalent"]:
                continue
            check["satisfies"], check["satisfies_reason"] = checker.answer_satisfies(
                bedrock_client,
                model_id=settings.checker_model_id,
                cached_query=row["question_text"],
                cached_answer=row["answer_text"],
                new_query=result.question_en,
            )
            if check["satisfies"]:
                original = row.get("original_lang") == result.original_lang and row.get("answer_text_original")
                result.hit = CacheHit(
                    entry_id=row["entry_id"],
                    question_text=row["question_text"],
                    answer=row["answer_text_original"] if original else row["answer_text"],
                    served_version="original" if original else "english",
                    sources=row.get("sources") or [],
                    cosine=row["cosine"],
                    admitted_at=row.get("admitted_at"),
                    data_as_of=row.get("data_as_of"),
                )
                break
        lap("checker")
    except Exception as exc:  # noqa: BLE001 -- the cache must never break a request
        result.error = f"{type(exc).__name__}: {exc}"
        result.hit = None
    return result
