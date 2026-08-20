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

    `entity_key` and `field` together are the join key `gateway/synthesis.py`'s
    `group_facts()` uses to detect when two providers disagree about the same
    attribute of the same real-world entity -- appropriate for naturally
    single-valued fields (e.g. `employee_count`: one true value, so >1
    distinct value among the group's facts is a real conflict).

    `multi_valued=True` opts a field out of that: the field is naturally
    one-to-many (e.g. Cala's `IS_SUBSIDIARY_OF` -- an entity can have many
    simultaneously-true subsidiaries), so `value` joins the grouping key too,
    keeping each distinct value in its own group instead of flagging the
    whole set as "disputed" just because there's more than one of them.

    Known tradeoff, not fully solved: since `value` is part of the join key,
    two sources reporting genuinely *different* values for what should be a
    single-answer relationship (e.g. Cala says entity X's direct parent is A,
    Sayari says B) land in two separate, non-disputed groups -- this design
    can no longer catch that as a conflict at all. It only still corroborates
    when both sources report the *identical* value (same group, not
    disputed, which is correct -- that's agreement, not conflict). Accepted
    2026-08-20 to kill a real, currently-occurring false-positive (many
    legitimate relationship values look identical to a real conflict without
    this), at the cost of this narrower true-negative.
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
    multi_valued: bool = False
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
