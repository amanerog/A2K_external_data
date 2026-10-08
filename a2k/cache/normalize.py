"""Admission step 6 (proposal Sheet 3): normalise question and answer to
English, so every client benefits from every cached answer. Runs only after
the judge said FIT -- the judge always evaluates the original texts.

The claims of the answer are translated with it (so a cache hit served in
English can still carry them). The question is not translated here: its language and its English version
come from retrieval's own normalise-and-classify call (admission.py), so a
stored question is read exactly as a new one will be -- same language
detection, same translation, hence the closest embedding for a repeat. This
module translates only the answer.

Names, identifiers, codes, URLs and figures are kept exactly as written.
"""

from __future__ import annotations

import json

from .bedrock import ModelOutputError, converse_json

PROMPT_VERSION = "normalize-v5"

# Used by retrieval's normalise-and-classify prompt. Language detection used to
# be part of this module's translation too, and with this same rule an English
# question about "Receita Federal" and a "CNPJ" was read as Portuguese here and
# as English at retrieval (e2e check, POCv3 id 38, 2026-10-01) -- so the
# original answer would never have been served. Hence one detector for both.
LANGUAGE_RULE = """the ISO 639-1 code of the language the question's own sentences are \
written in. Names of people, companies, institutions, laws, registries and identifiers in another \
language do not count (an English question about "Receita Federal" or a "CNPJ" is "en")."""

_PROMPT = """Translate an answer, and the claims made in it, into English for storage in a cache.

<answer>
{answer}
</answer>

<claims>
{claims}
</claims>

Rules:
- If the text is already in English, return it unchanged.
- Keep names of people, companies, institutions and products, identifiers (LEI, registry numbers, \
tax IDs, series codes), URLs, figures, units and dates exactly as written. Do not add, drop or \
summarise anything.
- Keep the formatting (paragraphs, lists, tables).
- Each claim is a passage of the answer. Translate the answer first; then give, for each claim in \
the same order, the passage of YOUR translated answer that renders it, copied verbatim from \
answer_en. Exactly one per claim.

Respond with ONLY this JSON object:
{{"answer_en": "...", "claims_en": ["...", "..."]}}
"""


def translate(bedrock_client, *, model_id: str, answer: str, claims: list[str] = ()) -> tuple[str, list[str]]:
    """(answer_en, claims_en). claims_en is empty unless the model returned
    exactly one string per claim -- a claim is located in the served answer
    later, so a missing one only means that answer is served with fewer."""
    raw = converse_json(
        bedrock_client,
        model_id=model_id,
        prompt=_PROMPT.format(answer=answer, claims=json.dumps(list(claims), ensure_ascii=False, indent=1)),
        # Room for a full translated answer (POCv3 answers: ~2.8k output tokens median) and its claims.
        max_tokens=16000,
    )
    answer_en = str(raw.get("answer_en") or "").strip()
    if not answer_en:
        raise ModelOutputError("translation returned an empty answer")
    claims_en = raw.get("claims_en")
    if not isinstance(claims_en, list) or len(claims_en) != len(claims) or not all(isinstance(c, str) for c in claims_en):
        claims_en = []
    return answer_en, [c.strip() for c in claims_en]
