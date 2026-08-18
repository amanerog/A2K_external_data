"""Sayari adapter -- MCP transport (Sayari's own hosted MCP server, not
their REST API; see `adapters/sayari.py` for the REST version).

Confirmed live against `https://mcp.sayari.com/mcp` (2026-08-12), following
the connection pattern from Sayari's own `sayari_mcp_client.ipynb`:

- Auth: OAuth2 client-credentials via **Auth0** -- `POST
  https://sayari.auth0.com/oauth/token` with `{client_id, client_secret,
  audience: "https://mcp.sayari.com/", grant_type: "client_credentials"}`
  -> Bearer token (24h). This is a *separate* credential/grant from the
  REST API's `sayari_client_id`/`sayari_client_secret` -- confirmed by
  Sayari's own notebook troubleshooting table: REST-scoped credentials are
  rejected on the MCP audience with a 403 ("You need to create a
  client-grant"). Do not conflate the two in config -- see
  `config.sayari_auth0_client_id`/`sayari_auth0_client_secret`.
- Transport: streamable-http, `Authorization: Bearer <token>` header.
- `search_entities({"query": ..., "limit": ...})` -> `structuredContent` is
  `{"result": "<JSON string>"}` -- **double-encoded**, unlike Cala's tools
  (whose structuredContent is the object directly). Must `json.loads()`
  the inner string. Decoded shape: `{"tool": "search_entities", "returned":
  N, "items": [{"entity_id", "label", "type", "quality_flags",
  "next_action": {"tool": "get_entity_summary", "args": {"entity_id"}}},
  ...]}`.
- `get_entity_summary({"entity_id": ...})` -> condensed profile (faster/
  cheaper than get_entity_profile, no relationships): `{"entity_id",
  "label", "type", "closed", "attributes": {"names": [...], "addresses":
  [...], "countries": [...], "company_type": ...}, "risk": {"sanctioned":
  bool, "pep": bool, "risk_levels": [...]}, "sources": [...plain database
  names...], "entity_url": "https://graph.sayari.com/resource/entity/..."}`.

  Citation granularity is coarser than Cala's `entity_retrieval`: Sayari's
  `sources` here just names which trade databases contributed to the
  profile as a whole -- it does not say which field came from which source
  the way Cala's per-property `sources[]` does. The only per-entity
  citable anchor is `entity_url` (a real, resolvable, entity-specific
  page), so every Fact from a given entity cites the same URL -- honest
  about being entity-level, not field-level, grounding.

Deliberately not mapped in this first pass:

- `check_watchlist` -- not a same-entity sanctions flag. It's a graph
  traversal returning *other* entities on watchlists connected to this one
  (with `path_length`), a materially different shape from a per-entity
  Fact -- confirmed live, 50 connected entities returned for one query.
  `get_entity_summary`'s own `risk.sanctioned`/`risk.pep` booleans cover
  the "is this entity itself flagged" case already.
- `get_entity_profile`, `find_beneficial_owners`, `get_record`, the trade/
  shipment search tools, and the ontology/reference lookup tools -- richer
  investigative tools, out of scope for the baseline `search()`/
  `get_document()` contract this adapter implements. `get_record` in
  particular would give sharper per-fact citations than `entity_url` if
  a future pass threads record_ids through -- noted for later, not solved
  here.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from ..config import config
from ..errors import A2KError, ErrorCode
from ._mock_common import get_document_mock, load_entities, search_mock
from .base import Fact, ProviderAdapter, ProviderDocument

KB_ID = "urn:a2k:vendor:sayari"

AUTH0_TOKEN_URL = "https://sayari.auth0.com/oauth/token"
AUTH0_AUDIENCE = "https://mcp.sayari.com/"

# Fields on get_entity_summary deliberately not turned into Facts: `sources`
# (database names, not a fact about the entity), `trade_count`,
# `investigation_notes`/`_hints`/`_guidance` (meta-instructions written for
# an LLM caller, not data about the company).
_SUMMARY_TOP_LEVEL_FIELDS = ("type", "closed")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def facts_from_entity_summary(summary: dict[str, Any], kb_id: str) -> list[Fact]:
    entity_id = summary.get("entity_id")
    entity_name = summary.get("label") or entity_id
    entity_url = summary.get("entity_url")
    document_id = f"{kb_id}:doc:{entity_id}:profile"
    retrieved_at = _now_iso()
    facts: list[Fact] = []

    def add_fact(field_name: str, value: Any) -> None:
        if value is None or value == "" or value == [] or value == {}:
            return
        if isinstance(value, str):
            value_str = value
        elif isinstance(value, list):
            value_str = ", ".join(str(v) for v in value)
        else:
            value_str = str(value)
        text = f"{entity_name} -- {field_name.replace('_', ' ')}: {value_str} (per Sayari)."
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        facts.append(
            Fact(
                entity_key=str(entity_id),
                entity_name=entity_name,
                field=field_name,
                value=value_str,
                text=text,
                document_id=document_id,
                title=f"{entity_name} (Sayari)",
                source_url=entity_url,
                source_hash=f"sha256:{digest}",
                retrieved_at=retrieved_at,
                source_last_updated=None,
                kb_id=kb_id,
                raw={field_name: value},
            )
        )

    for field_name in _SUMMARY_TOP_LEVEL_FIELDS:
        add_fact(field_name, summary.get(field_name))

    for field_name, value in (summary.get("attributes") or {}).items():
        add_fact(field_name, value)

    risk = summary.get("risk") or {}
    add_fact("sanctioned", risk.get("sanctioned"))
    add_fact("pep", risk.get("pep"))

    return facts


class SayariMcpAdapter(ProviderAdapter):
    kb_id = KB_ID
    display_name = "Sayari"

    def __init__(self) -> None:
        self._mock_entities = load_entities("sayari_entities.json")
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    async def search(self, query: str, *, limit: int = 10) -> list[Fact]:
        if config.is_mock:
            return search_mock(self._mock_entities, query, self.kb_id, limit)
        return await self._live_search(query, limit)

    async def get_document(self, document_id: str) -> ProviderDocument | None:
        if config.is_mock:
            return get_document_mock(self._mock_entities, self.kb_id, document_id, self.display_name)
        return await self._live_get_document(document_id)

    # -- auth ---------------------------------------------------------------

    def _require_live_ready(self) -> None:
        if not config.sayari_mcp_live_ready:
            raise A2KError(
                ErrorCode.UPSTREAM_ERROR,
                "A2K_BOX_MODE=live but AUTH0_CLIENT_ID/AUTH0_CLIENT_SECRET are not set.",
                retryable=False,
            )

    async def _get_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        async with httpx.AsyncClient(timeout=30, verify=config.httpx_verify) as client:
            try:
                resp = await client.post(
                    AUTH0_TOKEN_URL,
                    json={
                        "client_id": config.sayari_auth0_client_id,
                        "client_secret": config.sayari_auth0_client_secret,
                        "audience": AUTH0_AUDIENCE,
                        "grant_type": "client_credentials",
                    },
                    headers={"accept": "application/json"},
                )
                resp.raise_for_status()
                payload = resp.json()
            except httpx.HTTPError as exc:
                raise A2KError(ErrorCode.UPSTREAM_ERROR, f"Sayari MCP Auth0 token request failed: {exc}") from exc
        self._token = payload["access_token"]
        self._token_expires_at = time.monotonic() + float(payload.get("expires_in", 86400)) - 30
        return self._token

    def _open_session(self, http_client: httpx.AsyncClient):
        """Async context manager yielding (read, write, get_session_id) for
        an MCP session over the given (already Bearer-authenticated)
        httpx client -- mirrors sayari_mcp_client.ipynb's `sayari_session()`."""
        return streamable_http_client(config.sayari_mcp_url, http_client=http_client)

    async def _call_tool(self, session: ClientSession, tool: str, arguments: dict) -> Any:
        """Returns the tool's decoded result. Sayari's structuredContent is
        `{"result": "<JSON string>"}` for at least search_entities -- unwrap
        and json.loads() when that shape is seen; pass through unchanged
        otherwise (get_entity_summary was observed returning the object
        directly, see module docstring)."""
        try:
            result = await session.call_tool(tool, arguments)
        except Exception as exc:
            raise A2KError(ErrorCode.UPSTREAM_ERROR, f"Sayari MCP tool {tool!r} unreachable: {exc}") from exc

        if result.isError:
            message = result.content[0].text if result.content else f"tool {tool!r} failed"
            raise A2KError(ErrorCode.UPSTREAM_ERROR, f"Sayari MCP tool {tool!r} failed: {message}")

        data = result.structuredContent
        if isinstance(data, dict) and isinstance(data.get("result"), str):
            return json.loads(data["result"])
        return data

    # -- live branch (confirmed against mcp.sayari.com/mcp, 2026-08-12) -----

    async def _live_search(self, query: str, limit: int) -> list[Fact]:
        self._require_live_ready()
        token = await self._get_token()
        headers = {"Authorization": f"Bearer {token}"}

        async with httpx.AsyncClient(headers=headers, timeout=120.0, verify=config.httpx_verify) as http_client:
            async with self._open_session(http_client) as streams:
                read, write = streams[0], streams[1]
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    search_result = await self._call_tool(session, "search_entities", {"query": query, "limit": limit})
                    items = (search_result or {}).get("items", []) if isinstance(search_result, dict) else []

                    facts: list[Fact] = []
                    for item in items[: config.max_entities_to_hydrate]:
                        entity_id = item.get("entity_id")
                        if not entity_id:
                            continue
                        summary = await self._call_tool(session, "get_entity_summary", {"entity_id": entity_id})
                        if isinstance(summary, dict):
                            facts.extend(facts_from_entity_summary(summary, self.kb_id))
                    return facts

    async def _live_get_document(self, document_id: str) -> ProviderDocument | None:
        self._require_live_ready()
        try:
            entity_id = document_id.split(":")[-2]
        except IndexError:
            return None

        token = await self._get_token()
        headers = {"Authorization": f"Bearer {token}"}

        async with httpx.AsyncClient(headers=headers, timeout=120.0, verify=config.httpx_verify) as http_client:
            async with self._open_session(http_client) as streams:
                read, write = streams[0], streams[1]
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    summary = await self._call_tool(session, "get_entity_summary", {"entity_id": entity_id})

        if not isinstance(summary, dict):
            return None

        attributes = summary.get("attributes") or {}
        content_lines = [f"{k}: {v}" for k, v in attributes.items()]
        risk = summary.get("risk") or {}
        content_lines.append(f"sanctioned: {risk.get('sanctioned')}")
        content_lines.append(f"pep: {risk.get('pep')}")
        content = "\n".join(content_lines)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()

        return ProviderDocument(
            document_id=document_id,
            title=f"{summary.get('label', entity_id)} -- Sayari entity profile",
            source_url=summary.get("entity_url"),
            mime_type="text/plain",
            content=content,
            hash=f"sha256:{digest}",
            last_updated=None,
            classification="public",
        )
