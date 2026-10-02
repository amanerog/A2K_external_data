"""Admission step 6 (proposal Sheet 3): normalise question and answer to
English, so every client benefits from every cached answer. Runs only after
the judge said FIT -- the judge always evaluates the original texts.

The question is not translated here: its language and its English version
come from retrieval's own normalise-and-classify call (admission.py), so a
stored question is read exactly as a new one will be -- same language
detection, same translation, hence the closest embedding for a repeat. This
module translates only the answer.

Names, identifiers, codes, URLs and figures are kept exactly as written.
"""

from __future__ import annotations

from .bedrock import ModelOutputError, converse_json

PROMPT_VERSION = "normalize-v4"

# Used by retrieval's normalise-and-classify prompt. Language detection used to
# be part of this module's translation too, and with this same rule an English
# question about "Receita Federal" and a "CNPJ" was read as Portuguese here and
# as English at retrieval (e2e check, POCv3 id 38, 2026-10-01) -- so the
# original answer would never have been served. Hence one detector for both.
LANGUAGE_RULE = """the ISO 639-1 code of the language the question's own sentences are \
written in. Names of people, companies, institutions, laws, registries and identifiers in another \
language do not count (an English question about "Receita Federal" or a "CNPJ" is "en")."""

_PROMPT = """Translate an answer into English for storage in a cache.

<answer>
{answer}
</answer>

Rules:
- If the text is already in English, return it unchanged.
- Keep names of people, companies, institutions and products, identifiers (LEI, registry numbers, \
tax IDs, series codes), URLs, figures, units and dates exactly as written. Do not add, drop or \
summarise anything.
- Keep the formatting (paragraphs, lists, tables).

Respond with ONLY this JSON object:
{{"answer_en": "..."}}
"""


def translate_answer(bedrock_client, *, model_id: str, answer: str) -> str:
    raw = converse_json(
        bedrock_client,
        model_id=model_id,
        prompt=_PROMPT.format(answer=answer),
        # Room for a full translated answer (POCv3 answers: ~2.8k output tokens median).
        max_tokens=12000,
    )
    answer_en = str(raw.get("answer_en") or "").strip()
    if not answer_en:
        raise ModelOutputError("translation returned an empty answer")
    return answer_en
