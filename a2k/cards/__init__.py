"""Loads and validates the three KB Cards this box serves."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from ..models.kbcard import KBCard

CARDS_DIR = Path(__file__).parent

_CARD_FILES = {
    "gateway": "gateway_card.json",
    "cala": "cala_card.json",
    "sayari": "sayari_card.json",
}


@lru_cache(maxsize=None)
def load_card(name: str) -> KBCard:
    if name not in _CARD_FILES:
        raise KeyError(f"Unknown card: {name!r}. Known: {sorted(_CARD_FILES)}")
    with open(CARDS_DIR / _CARD_FILES[name], encoding="utf-8") as fh:
        return KBCard.model_validate(json.load(fh))


def vendor_catalogue() -> list[dict]:
    """The vendor list shape mcp_server/server.py's a2k.listVendors tool
    returns, factored out here so gateway/engine.py's direct-discovery-agent
    path (mcp_to_agent_to_mcp branch) can build the exact same catalogue to
    pass to the agent, without a2k.listVendors and the agent's phase-1 vendor
    decision drifting apart over time."""
    vendors = []
    for source_id in ("cala", "sayari"):
        card = load_card(source_id)
        vendors.append(
            {
                "sourceId": source_id,
                "name": card.name,
                "domains": card.knowledgeProfile.domains,
                "topics": card.knowledgeProfile.topics,
                "scope": card.knowledgeProfile.coverage.scope,
                "status": card.enterprise.lifecycle.status,
                "priority": card.priority,
                "queryType": card.queryType,
            }
        )
    return vendors
