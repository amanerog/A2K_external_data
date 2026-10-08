"""The service's A2K 0.1 KB Card: the only card K2 sees (served as the MCP
resource a2k://card). The vendor cards stay internal, in the pre-0.1 format.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

CARD_PATH = Path(__file__).resolve().parent.parent / "cards" / "kcp01" / "service_card.json"


@cache
def service_card() -> dict[str, Any]:
    return json.loads(CARD_PATH.read_text(encoding="utf-8"))


def kb_id() -> str:
    return service_card()["id"]


def purpose_allowed(purpose: str) -> bool:
    """A2K-KCP 0.1 section 3.6 against the card's policies: a disallowed
    purpose, or one not listed when allowedUse is declared, is refused."""
    policies = service_card().get("policies") or {}
    if purpose in (policies.get("disallowedUse") or []):
        return False
    allowed = policies.get("allowedUse")
    return allowed is None or purpose in allowed


def point_in_time_supported() -> bool:
    return bool(((service_card().get("knowledgeProfile") or {}).get("pointInTime") or {}).get("supported"))
