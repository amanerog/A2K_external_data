"""Admission step A (proposal Annex A6): a light model reads only the
question -- never the answer -- and prepares the judge: which domain modules
to load, the list of requested elements the judge checks one by one in place
of a reference answer, the entities (for C4 and future invalidation) and the
time anchor (for expiry).
"""

from __future__ import annotations

from .bedrock import ModelOutputError, converse_json
from .models import Classification
from .taxonomy import DOMAINS, taxonomy_for_prompt

PROMPT_VERSION = "classifier-v1"

_PROMPT = """You classify questions sent to an external-data service for a bank. Read ONLY the \
question below and describe what it asks for. Do not answer it.

Data domains:
{taxonomy}

Question:
<question>
{question}
</question>

Return:
- domain_primary: the code of the domain the question mainly belongs to.
- domain_secondary: the code of a second domain only if the question clearly also asks for data \
of that domain, otherwise null.
- requested_elements: the distinct pieces of information the question asks for, each as a short \
noun phrase in English. Split compound questions ("does X have sanctions, and where is he a \
director?" -> two elements). Include constraints the question states (a period, a source, an \
identifier) inside the element they apply to.
- entities: the companies, people, institutions, products or series the question names, as \
written.
- time_anchor: {{"type": "none"}} if the question gives no time reference; {{"type": "relative"}} \
for "current", "latest", "recent", "last 30 days" and similar; {{"type": "absolute", "start": \
"YYYY-MM-DD", "end": "YYYY-MM-DD"}} for an explicit past date or period (e.g. "Q4 2023" -> \
2023-10-01 to 2023-12-31).

Respond with ONLY this JSON object:
{{"domain_primary": "...", "domain_secondary": null, "requested_elements": ["..."], \
"entities": ["..."], "time_anchor": {{"type": "none"}}}}
"""


def classify(bedrock_client, *, model_id: str, question: str) -> Classification:
    raw = converse_json(
        bedrock_client,
        model_id=model_id,
        prompt=_PROMPT.format(taxonomy=taxonomy_for_prompt(), question=question),
        max_tokens=2000,
    )
    primary = str(raw.get("domain_primary") or "").upper()
    if primary not in DOMAINS:
        raise ModelOutputError(f"classifier returned unknown domain {primary!r}")
    secondary = raw.get("domain_secondary")
    secondary = str(secondary).upper() if secondary else None
    if secondary not in DOMAINS or secondary == primary:
        secondary = None
    elements = [str(e).strip() for e in raw.get("requested_elements") or [] if str(e).strip()]
    if not elements:
        raise ModelOutputError("classifier returned no requested elements")
    anchor = raw.get("time_anchor") or {"type": "none"}
    if not isinstance(anchor, dict) or anchor.get("type") not in ("none", "relative", "absolute"):
        anchor = {"type": "none"}
    return Classification(
        domain_primary=primary,
        domain_secondary=secondary,
        requested_elements=elements,
        entities=[str(e) for e in raw.get("entities") or []],
        time_anchor=anchor,
    )
