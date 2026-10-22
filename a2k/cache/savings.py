"""What a cache hit saves (dashboard tasks T1.1/T1.2): the vendor tool calls
and the agent run the answer took.

A vendor answer costs its vendor tool calls (price per call, vendor_prices.json,
in EUR or USD, versioned by price_version) plus the agent's own model tokens -- Sonnet reading
everything the vendor tools returned, often the larger part. A hit avoids both.

- shadow (or a domain still in shadow): the vendor was called anyway, on this
  very ask, so the potential saving is exactly what this ask's answer cost;
- serve: the vendor is not called, so the saving is what the entry's own answer
  cost when it was admitted (cache_entry.answer_cost).

The cost is an estimate, like the cache's own (usage.py): calls x list price
and tokens x model list price, not the invoice. A vendor with no per-call
price adds its calls but no cost, and is named in `unpriced`.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..config import config
from .usage import price_for

PRICES_FILE = Path(__file__).with_name("vendor_prices.json")


@lru_cache(maxsize=1)
def vendor_prices() -> tuple[str, dict[str, float | None]]:
    """(price_version, USD per call by vendor): usd_per_call, plus
    eur_per_call converted at eur_usd. Read on the request path, so a missing
    or broken file leaves every vendor unpriced instead of raising."""
    try:
        data = json.loads(PRICES_FILE.read_text(encoding="utf-8"))
        prices = dict(data.get("usd_per_call") or {})
        for vendor, eur in (data.get("eur_per_call") or {}).items():
            prices[vendor] = None if eur is None else round(eur * float(data["eur_usd"]), 6)
        return str(data["price_version"]), prices
    except Exception as exc:  # noqa: BLE001 -- a price must never break an ask
        return f"unavailable ({type(exc).__name__})", {}


def answer_cost(content: dict[str, Any]) -> dict[str, Any]:
    """What a vendor answer took, from the agent's content: its tool calls per
    vendor and the agent's tokens. Stored with the entry and logged per ask."""
    calls: dict[str, int] = {}
    for tc in content.get("toolCalls") or []:
        if tc.get("vendor"):
            calls[tc["vendor"]] = calls.get(tc["vendor"], 0) + 1
    agent_usage = content.get("usage") or {}
    return {
        "vendor_calls": calls,
        "agent_input_tokens": int(agent_usage.get("inputTokens") or 0),
        "agent_output_tokens": int(agent_usage.get("outputTokens") or 0),
    }


def priced(cost: dict[str, Any]) -> dict[str, Any]:
    """An answer_cost in USD at the current prices."""
    version, prices = vendor_prices()
    vendor_usd, unpriced = 0.0, []
    for vendor, n in (cost.get("vendor_calls") or {}).items():
        price = prices.get(vendor)
        if price is None:
            unpriced.append(vendor)
        else:
            vendor_usd += n * price
    model_price = price_for(config.agent_model_id)
    agent_usd = None
    if model_price is not None:
        agent_usd = round((cost.get("agent_input_tokens", 0) * model_price[0] + cost.get("agent_output_tokens", 0) * model_price[1]) / 1_000_000, 6)
    return {
        "vendors": sorted(cost.get("vendor_calls") or {}),
        "vendor_calls": sum((cost.get("vendor_calls") or {}).values()),
        "vendor_cost_usd": round(vendor_usd, 6),
        "agent_cost_usd": agent_usd,
        "unpriced": sorted(unpriced),
        "price_version": version,
    }


def avoided(*, served_from: str, would_hit: bool, ask_cost: dict[str, Any] | None, entry_cost: dict[str, Any] | None) -> dict[str, Any] | None:
    """The saving of one ask: "real" when served from the cache, "potential"
    for a would-be hit that was not served; None otherwise, or when the cost
    is not known (an entry admitted before answer_cost was recorded)."""
    if served_from == "cache":
        kind, cost = "real", entry_cost
    elif would_hit:
        # The vendor answer of this ask; if it failed, what the entry cost.
        kind, cost = "potential", ask_cost or entry_cost
    else:
        return None
    if not cost:
        return None
    return {"kind": kind, **priced(cost)}


def citation_hosts(content: dict[str, Any]) -> list[str]:
    """The web domains an answer cites (T4.3), without "www."."""
    hosts = set()
    for c in content.get("citations") or []:
        host = urlparse(c.get("sourceUrl") or "").hostname
        if host:
            hosts.add(host.removeprefix("www."))
    return sorted(hosts)
