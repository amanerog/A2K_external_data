"""Admission decision rule (proposal Annex A6), in code on the judge's
findings -- the model reports, this decides. Binary: FIT or NOT FIT.

FIT only if all of:
- every requested element is "covered" (C1 at element level);
- the four core checks C1-C4 pass;
- every loaded domain-module check passes, or is "not_applicable" -- except
  in strict domains (RSK), where every check always applies;
- if `require_verified_quotes`, every covered element's evidence quote is
  found in the raw tool output (C3 checked in code, not only by the model).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .evidence import normalize_for_match
from .models import Classification, JudgeFindings
from .taxonomy import DOMAINS

RULE_VERSION = "rule-v3"


@dataclass
class Decision:
    fit: bool
    reason: str


_ELLIPSIS = re.compile(r"\s*(?:\.\.\.|…|\[\.\.\.\])\s*")

# Formatting the judge drops or changes when it quotes, none of which changes
# what the passage says: markdown emphasis/code/headings, backslash escapes,
# curly vs straight quotes and apostrophes, long dashes. Removed on both sides
# before matching. In the POCv3 runs (2026-10-01) this was 20+ of the 29
# unverified quotes, and the reason several answers flipped between runs.
_MARKDOWN = re.compile(r"[*_`#\\]+")
_QUOTE_CHARS = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": "'", "\u201d": "'", "\u00ab": "'", "\u00bb": "'", '"': "'", "\u2013": "-", "\u2014": "-"})


def normalize_for_quote(text: str) -> str:
    return normalize_for_match(_MARKDOWN.sub("", text.translate(_QUOTE_CHARS)))


def quote_in_evidence(quote: str, corpus: str) -> bool:
    """`corpus` already normalised with normalize_for_quote. The judge is asked not to, but sometimes
    shortens a quote with "..." -- each fragment between ellipses must then
    appear in the evidence, in order; the ellipses themselves are ignored."""
    position = 0
    fragments = [normalize_for_quote(f) for f in _ELLIPSIS.split(quote)]
    fragments = [f for f in fragments if f]
    if not fragments:
        return False
    for fragment in fragments:
        found = corpus.find(fragment, position)
        if found == -1:
            return False
        position = found + len(fragment)
    return True


def verify_quotes(findings: JudgeFindings, evidence: str) -> None:
    corpus = normalize_for_quote(evidence)
    for element in findings.elements:
        if element.evidence_quote:
            element.quote_verified = quote_in_evidence(element.evidence_quote, corpus)


def decide(findings: JudgeFindings, classification: Classification, *, evidence: str, require_verified_quotes: bool) -> Decision:
    verify_quotes(findings, evidence)

    not_covered = [e.element for e in findings.elements if e.status != "covered"]
    if not_covered:
        return Decision(False, f"elements not fully covered: {'; '.join(not_covered)}")

    failed_core = [cid for cid, check in findings.core.items() if check["result"] != "pass"]
    if failed_core:
        return Decision(False, f"core checks failed: {', '.join(failed_core)}")

    strict_prefixes = {f"{code}-" for code in classification.domains if DOMAINS[code].strict}
    failed_modules = []
    for cid, check in findings.modules.items():
        strict = any(cid.startswith(p) for p in strict_prefixes)
        allowed = {"pass"} if strict else {"pass", "not_applicable"}
        if check["result"] not in allowed:
            failed_modules.append(cid)
    if failed_modules:
        return Decision(False, f"domain checks failed: {', '.join(failed_modules)}")

    if require_verified_quotes:
        unverified = [e.element for e in findings.elements if not e.quote_verified]
        if unverified:
            return Decision(False, f"evidence quotes not found in tool output: {'; '.join(unverified)}")

    return Decision(True, "all checks passed")
