"""Admission step 6 (proposal Sheet 3): normalise question and answer to
English, so every client benefits from every cached answer. Runs only after
the judge said FIT -- the judge always evaluates the original texts.

Names, identifiers, codes, URLs and figures are kept exactly as written.
"""

from __future__ import annotations

from .bedrock import ModelOutputError, converse_json
from .models import Normalized

PROMPT_VERSION = "normalize-v3"

# Used by retrieval's normalise-and-classify prompt, which is also what detects a
# question's language at admission (admission.py) -- not this translation prompt:
# the language stored at admission and the one detected at retrieval decide
# together whether the original or the English answer is served, so both must
# come from the same call. With this rule in both prompts, an English question
# about "Receita Federal" and a "CNPJ" was still read as Portuguese here and as
# English at retrieval (e2e check, POCv3 id 38, 2026-10-01).
LANGUAGE_RULE = """the ISO 639-1 code of the language the question's own sentences are \
written in. Names of people, companies, institutions, laws, registries and identifiers in another \
language do not count (an English question about "Receita Federal" or a "CNPJ" is "en")."""

_PROMPT = """Translate a question and its answer into English for storage in a cache.

<question>
{question}
</question>

<answer>
{answer}
</answer>

Rules:
- If a text is already in English, return it unchanged.
- Keep names of people, companies, institutions and products, identifiers (LEI, registry numbers, \
tax IDs, series codes), URLs, figures, units and dates exactly as written. Do not add, drop or \
summarise anything.
- Keep the answer's formatting (paragraphs, lists, tables).

Respond with ONLY this JSON object:
{{"question_en": "...", "answer_en": "..."}}
"""


def normalize(bedrock_client, *, model_id: str, question: str, answer: str) -> Normalized:
    raw = converse_json(
        bedrock_client,
        model_id=model_id,
        prompt=_PROMPT.format(question=question, answer=answer),
        # Room for a full translated answer (POCv3 answers: ~2.8k output tokens median).
        max_tokens=12000,
    )
    question_en = str(raw.get("question_en") or "").strip()
    answer_en = str(raw.get("answer_en") or "").strip()
    if not question_en or not answer_en:
        raise ModelOutputError("normalisation returned an empty question or answer")
    # The language is set by admission with retrieval's own detector (see LANGUAGE_RULE).
    return Normalized(original_lang="und", question_en=question_en, answer_en=answer_en)
