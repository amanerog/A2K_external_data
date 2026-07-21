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
