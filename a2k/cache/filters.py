"""Technical filters T1, T2, T4, T5 (proposal Annex A5): fixed rules, no
model, run before anything that costs money.

T1 rejects an answer whose tool calls all failed, or that cites a page a
failed call could not read (relaxed 2026-10-08, see t1_calls_ok).

T3 was retired in the working session (D17). T6 ("not already rejected by the
user") is not a check here: it is the conditional activation in admission.py,
which only succeeds if the row is still PENDING -- a reformulation that
deprecated it meanwhile makes that update a no-op.

T2 also discards the Vendor Agent's fixed fallback message (E4.9 / D17,
VENDOR_FALLBACK_MESSAGE), by exact match. The proposal's other T2 case, an
answer cut at max_tokens, cannot reach admission: Strands raises
MaxTokensReachedException instead of returning a partial structured answer,
and the agent turns any failed vendor run into that same fallback message
(agent/test_direct_agent_fallback.py pins this).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .evidence import normalize_for_match, tool_output_text, vendor_tool_calls
from .models import Candidate


@dataclass
class FilterResult:
    passed: bool
    failed: str | None = None  # "T1".."T5"
    reason: str = ""
    verified_sources: list[dict[str, Any]] | None = None


def _failed(tc: dict[str, Any]) -> bool:
    return tc.get("status") != "success" or bool(tc.get("errorType"))


def _input_urls(tc: dict[str, Any]) -> list[str]:
    """URLs a tool call was asked to read (a page fetch), wherever they sit in its input."""
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            found.extend(_URL.findall(value))
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    walk(tc.get("input"))
    return found


def _cited(candidate: Candidate) -> set[str]:
    refs = [r for c in candidate.citations for r in (c.get("sourceUrl"), c.get("documentId")) if r]
    refs += _URL.findall(candidate.answer or "")
    return {v for r in refs for v in _url_variants(r)}


def t1_calls_ok(candidate: Candidate) -> FilterResult:
    """A failed call rejects the answer only if nothing succeeded, or if the
    answer cites what that call failed to read. Until 2026-10-08 any failed
    call rejected it: one page fetch out of several returning 403 is routine
    for web search (14 of Linkup's 120 POCv3 answers), and the answer is then
    built from the calls that did succeed -- which the judge checks it against."""
    calls = vendor_tool_calls(candidate.tool_calls)
    failed = [tc for tc in calls if _failed(tc)]
    if not failed:
        return FilterResult(True)
    if len(failed) == len(calls):
        return FilterResult(False, "T1", f"every tool call failed: {', '.join(str(tc.get('toolName')) for tc in failed)}")
    cited = _cited(candidate)
    unread = sorted({url for tc in failed for url in _input_urls(tc) if any(v in cited for v in _url_variants(url))})
    if unread:
        return FilterResult(False, "T1", f"the answer cites pages its tool calls failed to read: {', '.join(unread[:3])}")
    return FilterResult(True)


def t2_answer_complete(candidate: Candidate, *, min_chars: int, fallback_messages: tuple[str, ...] = ()) -> FilterResult:
    answer = (candidate.answer or "").strip()
    if len(answer) < min_chars:
        return FilterResult(False, "T2", f"answer shorter than {min_chars} characters ({len(answer)})")
    if any(answer == msg.strip() for msg in fallback_messages):
        return FilterResult(False, "T2", "answer is the Vendor Agent fallback message")
    return FilterResult(True)


def t4_from_vendor_data(candidate: Candidate) -> FilterResult:
    for tc in vendor_tool_calls(candidate.tool_calls):
        if tool_output_text(tc.get("output")).strip():
            return FilterResult(True)
    return FilterResult(False, "T4", "no tool call returned content: answer is not backed by vendor data")


_URL = re.compile(r"https?://[^\s)\]>\"'`,]+")


def _url_variants(url: str) -> list[str]:
    url = url.rstrip(".;:")
    bare = re.sub(r"^https?://", "", url).rstrip("/")
    return [normalize_for_match(url), normalize_for_match(bare)]


def t5_sources_real(candidate: Candidate) -> FilterResult:
    """Every source the answer cites -- the agent's citations (sourceUrl /
    documentId) plus any URL written into the answer text -- must appear
    literally in some tool output; a source that is not there is treated as
    invented and the answer is not cached. At least one source is required.
    The verified citations are what gets stored as the entry's `sources`."""
    corpus = tool_corpus(candidate.tool_calls)

    def found(value: str) -> bool:
        return source_found(value, corpus)

    verified: list[dict[str, Any]] = []
    invented: list[str] = []
    for citation in candidate.citations:
        refs = [r for r in (citation.get("sourceUrl"), citation.get("documentId")) if r]
        if not refs:
            continue
        if any(found(r) for r in refs):
            verified.append({**citation, "vendor": candidate.vendor})
        else:
            invented.append(refs[0])

    for url in _URL.findall(candidate.answer or ""):
        if not found(url):
            invented.append(url)

    if invented:
        return FilterResult(False, "T5", f"sources not found in tool output: {', '.join(sorted(set(invented))[:5])}")
    if not verified:
        return FilterResult(False, "T5", "answer cites no source")
    return FilterResult(True, verified_sources=verified)


def run(candidate: Candidate, *, min_chars: int, fallback_messages: tuple[str, ...] = (), check_sources: bool = True) -> FilterResult:
    """`check_sources=False` skips T5 -- only for offline runs over answers
    recorded without their citations (the POC .jsonl files)."""
    checks = [
        lambda: t1_calls_ok(candidate),
        lambda: t2_answer_complete(candidate, min_chars=min_chars, fallback_messages=fallback_messages),
        lambda: t4_from_vendor_data(candidate),
    ]
    if check_sources:
        checks.append(lambda: t5_sources_real(candidate))
    for check in checks:
        result = check()
        if not result.passed:
            return result
    return result


# --- citations checked against the tool output, on every live answer ---------


def tool_corpus(tool_calls: list[dict[str, Any]]) -> str:
    """What the vendor tools returned, normalised for source lookups -- plus
    the URLs of the pages that successful calls read (a page fetch carries
    its URL in its input, not in the page it returns)."""
    calls = vendor_tool_calls(tool_calls)
    read = [url for tc in calls if not _failed(tc) for url in _input_urls(tc)]
    return normalize_for_match("\n".join([tool_output_text(tc.get("output")) for tc in calls] + read))


def source_found(ref: str, corpus: str) -> bool:
    return any(v and v in corpus for v in _url_variants(ref))


def unverified_citations(citations: list[dict[str, Any]], tool_calls: list[dict[str, Any]]) -> list[int]:
    """Indexes of the citations whose URL (or, with none, documentId) appears
    in no vendor tool output -- written by the agent, not returned by the
    vendor. Routing run 2026-10-08: 9 answers cited URLs with a rewritten
    path (reuters.com/business/finance/... for the returned
    reuters.com/sustainability/...)."""
    corpus = tool_corpus(tool_calls)
    out = []
    for i, citation in enumerate(citations):
        ref = citation.get("sourceUrl") or citation.get("documentId")
        if ref and not source_found(ref, corpus):
            out.append(i)
    return out
