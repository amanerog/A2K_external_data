"""Shared fixture-loading and matching logic for the mock branches of both
adapters. Cala's and Sayari's fixture files share one shape (see the two JSON
files in `fixtures/`), so the matching/document-synthesis logic is written
once here instead of duplicated in `cala.py` and `sayari.py`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .base import Fact, ProviderDocument

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_entities(filename: str) -> list[dict[str, Any]]:
    with open(FIXTURES_DIR / filename, encoding="utf-8") as fh:
        return json.load(fh)["entities"]


def _matches(entity: dict[str, Any], query_lower: str) -> bool:
    candidates = [entity["name"].lower(), *[a.lower() for a in entity.get("aliases", [])]]
    return any(c in query_lower or query_lower in c for c in candidates)


def profile_document_id(kb_id: str, entity_key: str) -> str:
    return f"{kb_id}:doc:{entity_key}:profile"


def build_profile_document(entity: dict[str, Any], kb_id: str, provider_label: str) -> ProviderDocument:
    content = "\n".join(f["text"] for f in entity["facts"])
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return ProviderDocument(
        document_id=profile_document_id(kb_id, entity["entity_key"]),
        title=f"{entity['name']} -- {provider_label} entity profile",
        source_url=None,
        mime_type="text/plain",
        content=content,
        hash=f"sha256:{digest}",
        last_updated=max((f["source_last_updated"] for f in entity["facts"]), default=None),
        classification="public",
    )


def facts_for_entity(entity: dict[str, Any], kb_id: str) -> list[Fact]:
    doc_id = profile_document_id(kb_id, entity["entity_key"])
    retrieved_at = now_iso()
    facts = []
    for raw in entity["facts"]:
        digest = hashlib.sha256(raw["text"].encode("utf-8")).hexdigest()
        facts.append(
            Fact(
                entity_key=entity["entity_key"],
                entity_name=entity["name"],
                field=raw["field"],
                value=raw["value"],
                text=raw["text"],
                document_id=doc_id,
                title=raw["source_title"],
                source_url=raw["source_url"],
                source_hash=f"sha256:{digest}",
                retrieved_at=retrieved_at,
                source_last_updated=raw["source_last_updated"],
                kb_id=kb_id,
                raw=raw,
            )
        )
    return facts


def search_mock(entities: list[dict[str, Any]], query: str, kb_id: str, limit: int) -> list[Fact]:
    query_lower = query.lower()
    matched = [e for e in entities if _matches(e, query_lower)]
    facts: list[Fact] = []
    for entity in matched:
        facts.extend(facts_for_entity(entity, kb_id))
    return facts[:limit] if limit else facts


def get_document_mock(
    entities: list[dict[str, Any]], kb_id: str, document_id: str, provider_label: str
) -> ProviderDocument | None:
    for entity in entities:
        if profile_document_id(kb_id, entity["entity_key"]) == document_id:
            return build_profile_document(entity, kb_id, provider_label)
    return None
