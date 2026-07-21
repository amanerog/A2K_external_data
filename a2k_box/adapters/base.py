"""Provider adapter interface.

Both mock and live branches of `CalaAdapter` / `SayariAdapter` return the same
`Fact` shape, so `gateway/synthesis.py` and `gateway/conflict.py` never need to
know which mode produced a given fact.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Fact:
    """One atomic, citable statement about an entity, as returned by a provider.

    `entity_key` and `field` together are the join key used by
    `gateway/conflict.py` to detect when two providers disagree about the
    same attribute of the same real-world entity.
    """

    entity_key: str
    entity_name: str
    field: str
    value: str
    text: str
    document_id: str
    title: str
    source_url: str | None
    source_hash: str
    retrieved_at: str
    source_last_updated: str | None
    kb_id: str
    classification: str = "public"
    score: float = 1.0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderDocument:
    document_id: str
    title: str
    source_url: str | None
    mime_type: str
    content: str
    hash: str
    last_updated: str | None
    classification: str = "public"


class ProviderAdapter(ABC):
    kb_id: str
    display_name: str

    @abstractmethod
    async def search(self, query: str, *, limit: int = 10) -> list[Fact]:
        """Return facts about entities matching `query`, best-effort ranked."""

    @abstractmethod
    async def get_document(self, document_id: str) -> ProviderDocument | None:
        """Retrieve the synthesized entity-profile document behind a citation."""
