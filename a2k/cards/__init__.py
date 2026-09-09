"""Loads and validates the KB Cards this box serves.

The gateway's own card (`load_card("gateway")`) is always the local
`gateway_card.json` -- a single, non-scaling entity, nothing to look up by
count. Vendor cards (`cala`, `sayari`, and any future vendor) come from
DynamoDB when `config.vendor_cards_table` is set (see top-level README's
"Vendor cards from DynamoDB"), falling back to the local
`cala_card.json`/`sayari_card.json` files otherwise -- local/.env dev and
this repo's test suite need zero AWS access to keep working, same as
before this existed.

Adding a vendor no longer means shipping a code change: drop a new item in
the DynamoDB table (`deploy/seed_vendor_cards.py` writes them) and it shows
up in `vendor_catalogue()` -- and therefore in a2k.listVendors, a2k.getCard,
and the catalogue direct_agent.py's phase 1 uses to route -- on this
process's next cache refresh, no redeploy. Only the metadata catalogue is
DB-backed this way; connecting to a *new* vendor's own MCP server still
needs code (agent/vendor_mcp_client.py's connect_cala()/connect_sayari())
-- deliberately out of scope here, see the branch's own notes on why that
half is a bigger change.

DynamoDB reads are cached in-process with a short TTL
(`config.vendor_cards_cache_ttl_seconds`, default 10 min) -- the same
`(value, expiry_epoch)` lazy-refresh shape as `agent/core.py`'s
`_token_cache` and `agent/vendor_mcp_client.py`'s `_sayari_token_cache`,
checked and refreshed on the next call after expiry, not a scheduled
background job. No lock around the cache write: a Scan is read-only and
idempotent, so two concurrent cache misses both re-Scanning and each
overwriting the module-level cache is a wasted duplicate fetch, not a
correctness bug -- same reasoning `vendor_mcp_client.py`'s own comment
gives for skipping a lock there.
"""

from __future__ import annotations

import json
import time
from functools import lru_cache
from pathlib import Path

from ..config import config
from ..models.kbcard import KBCard

CARDS_DIR = Path(__file__).parent

_GATEWAY_CARD_FILE = "gateway_card.json"

# Local fallback only -- consulted when config.vendor_cards_table is unset
# (local dev, tests). Once DynamoDB is configured, this dict is never read;
# a vendor added there does NOT need an entry here too unless you also want
# it available with VENDOR_CARDS_TABLE unset.
_LOCAL_VENDOR_CARD_FILES = {
    "cala": "cala_card.json",
    "sayari": "sayari_card.json",
}


@lru_cache(maxsize=None)
def _load_local_card(filename: str) -> KBCard:
    with open(CARDS_DIR / filename, encoding="utf-8") as fh:
        return KBCard.model_validate(json.load(fh))


# (expiry_epoch, {sourceId: KBCard}) -- see module docstring for the caching
# rationale. Module-level, not a Config field: this is runtime-fetched data,
# not configuration.
_vendor_cards_cache: tuple[float, dict[str, KBCard]] | None = None


def _fetch_vendor_cards_from_dynamo() -> dict[str, KBCard]:
    import boto3

    table = boto3.resource("dynamodb").Table(config.vendor_cards_table)
    cards: dict[str, KBCard] = {}
    scan_kwargs: dict = {}
    while True:
        response = table.scan(**scan_kwargs)
        for item in response.get("Items", []):
            cards[item["sourceId"]] = KBCard.model_validate(json.loads(item["cardJson"]))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key
    return cards


def _vendor_cards() -> dict[str, KBCard]:
    if not config.vendor_cards_table:
        return {name: _load_local_card(fname) for name, fname in _LOCAL_VENDOR_CARD_FILES.items()}

    global _vendor_cards_cache
    now = time.monotonic()
    if _vendor_cards_cache is not None and _vendor_cards_cache[0] > now:
        return _vendor_cards_cache[1]

    cards = _fetch_vendor_cards_from_dynamo()
    _vendor_cards_cache = (now + config.vendor_cards_cache_ttl_seconds, cards)
    return cards


def load_card(name: str) -> KBCard:
    if name == "gateway":
        return _load_local_card(_GATEWAY_CARD_FILE)
    cards = _vendor_cards()
    if name not in cards:
        raise KeyError(f"Unknown card: {name!r}. Known: {['gateway', *sorted(cards)]}")
    return cards[name]


def vendor_catalogue() -> list[dict]:
    """The vendor list shape mcp_server/server.py's a2k.listVendors tool
    returns, factored out here so gateway/engine.py's direct-discovery-agent
    path (mcp_to_agent_to_mcp branch) can build the exact same catalogue to
    pass to the agent, without a2k.listVendors and the agent's phase-1 vendor
    decision drifting apart over time. Vendor count/identity isn't hardcoded
    here -- whatever _vendor_cards() returns (DynamoDB or local fallback,
    see module docstring) is what gets listed."""
    vendors = []
    for source_id, card in sorted(_vendor_cards().items()):
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
