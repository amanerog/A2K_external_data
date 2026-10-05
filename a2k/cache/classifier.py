"""Admission step A (proposal Annex A6): a light model reads only the
question -- never the answer -- and prepares the judge: which domain modules
to load, the list of requested elements the judge checks one by one in place
of a reference answer, the entities (for C4 and future invalidation), the
time anchor (for expiry) and whether the question stands on its own.

The prompt is prompts/clasificador_admision_v1.md, used as delivered: system
prompt as-is, the question and today's date in the user message.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from . import prompts
from .bedrock import ModelOutputError, converse_json
from .models import Classification, RequestedElement
from .taxonomy import CLASSIFIER_PROMPT, DOMAINS

PROMPT_VERSION = CLASSIFIER_PROMPT

ANCHOR_KINDS = ("relative", "absolute", "none")


def user_message(question: str, today: date) -> str:
    return f"<question>\n{question}\n</question>\n\n<today>{today.isoformat()}</today>"


def _elements(raw: Any) -> list[RequestedElement]:
    elements = []
    for i, item in enumerate(raw or [], start=1):
        if isinstance(item, dict):
            text = str(item.get("element") or "").strip()
            if text:
                elements.append(
                    RequestedElement(
                        id=str(item.get("id") or f"E{i}"),
                        element=text,
                        expected_type=str(item.get("expected_type") or "text"),
                        constraints=str(item.get("constraints") or ""),
                    )
                )
        elif str(item).strip():
            elements.append(RequestedElement(id=f"E{i}", element=str(item).strip()))
    return elements


def _entities(raw: Any) -> list[dict[str, Any]]:
    out = []
    for item in raw or []:
        if isinstance(item, dict) and item.get("name"):
            out.append({"name": str(item["name"]), "kind": str(item.get("kind") or "other"), "identifiers": list(item.get("identifiers") or [])})
        elif isinstance(item, str) and item.strip():
            out.append({"name": item.strip(), "kind": "other", "identifiers": []})
    return out


def parse(raw: dict[str, Any]) -> Classification:
    primary = str(raw.get("domain_primary") or "").upper()
    if primary not in DOMAINS:
        raise ModelOutputError(f"classifier returned unknown domain {primary!r}")
    secondary = raw.get("domain_secondary")
    secondary = str(secondary).upper() if secondary else None
    if secondary not in DOMAINS or secondary == primary:
        secondary = None
    elements = _elements(raw.get("requested_elements"))
    if not elements:
        raise ModelOutputError("classifier returned no requested elements")
    anchor = raw.get("time_anchor")
    if not isinstance(anchor, dict) or anchor.get("kind") not in ANCHOR_KINDS:
        anchor = {"kind": "none", "text": "", "from": None, "to": None, "entirely_past": False}
    return Classification(
        domain_primary=primary,
        domain_secondary=secondary,
        requested_elements=elements,
        entities=_entities(raw.get("entities")),
        time_anchor=anchor,
        # Only an explicit false stops admission: a missing field is not a reason to drop an answer.
        self_contained=raw.get("self_contained") is not False,
        question_type=raw.get("question_type"),
        depth=raw.get("depth"),
        language=raw.get("language"),
        notes=str(raw.get("notes") or ""),
        raw=raw,
    )


def classify(bedrock_client, *, model_id: str, question: str, today: date) -> Classification:
    raw = converse_json(
        bedrock_client,
        model_id=model_id,
        system=prompts.block(CLASSIFIER_PROMPT),
        prompt=user_message(question, today),
        temperature=0,
        max_tokens=3000,
    )
    return parse(raw)
