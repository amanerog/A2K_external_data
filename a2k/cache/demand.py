"""Data-universe log lines (dashboard block 6, tasks T4.1/T4.3): what is
asked about and what the answers cite -- the candidates for a bulk purchase
(Phase II) and for ingesting at the source (Phase III).

The entities of a question are in its cache.admission line and the cited web
domains in its cache.retrieval line, but as lists, and Logs Insights cannot
unnest a list: a "top 50 entities" needs one line per entity. So:

- cache.entity, one per entity the classifier found in an admitted
  candidate's question, with the domain, the vendor, the outcome and what
  the vendor answer cost (savings.py) -- the demand for that entity and its
  price;
- cache.source, one per web domain a vendor answer cites, with the domain
  and the vendors that answered.

Only questions that reach admission name their entities: a would-be hit is
not admitted, so an entity asked about again and found in the cache is not
counted again. With an empty cache (a ground-truth run after a deprecation)
every question is admitted.
"""

from __future__ import annotations

import re
from typing import Any

from ..logline import emit
from . import savings


def name_key(name: str) -> str:
    """The entity name to group by: case, spacing and trailing punctuation
    are not a different entity ("Revolut Ltd." and "revolut  ltd")."""
    return re.sub(r"\s+", " ", name).strip().rstrip(".,;").casefold()


def entity_lines(*, entities: list[dict[str, Any]] | None, query_sha256: str, entry_id: str | None, domain: str | None, vendor: str | None, outcome: str, answer_cost: dict[str, Any] | None) -> list[dict[str, Any]]:
    cost_usd = None
    if answer_cost:
        priced = savings.priced(answer_cost)
        if not priced["unpriced"] and priced["agent_cost_usd"] is not None:
            cost_usd = round(priced["vendor_cost_usd"] + priced["agent_cost_usd"], 6)
    lines = []
    for e in entities or []:
        name = str(e.get("name") or "").strip()
        if not name:
            continue
        lines.append({
            "event": "cache.entity",
            "query_sha256": query_sha256,
            "entry_id": entry_id,
            "Domain": domain or "NONE",
            "vendor": vendor,
            "outcome": outcome,
            "name": name,
            "name_key": name_key(name),
            "kind": e.get("kind"),
            "identifiers": e.get("identifiers") or [],
            # What the vendor answer to this question cost (vendor calls + agent), if fully priced.
            "answer_cost_usd": cost_usd,
        })
    return lines


def source_lines(*, hosts: list[str], query_sha256: str, request_id: str, domain: str | None, vendors: list[str]) -> list[dict[str, Any]]:
    return [
        {"event": "cache.source", "query_sha256": query_sha256, "request_id": request_id, "Domain": domain or "NONE", "host": host, "vendors": ",".join(vendors) or "none"}
        for host in hosts
    ]


def emit_all(lines: list[dict[str, Any]]) -> None:
    for line in lines:
        emit(line)
