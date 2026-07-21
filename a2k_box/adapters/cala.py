"""Cala adapter (https://cala.ai -- structured company/entity data platform).

Confirmed against the live OpenAPI spec at `https://api.cala.ai/openapi.json`
and the endpoint docs under `docs.cala.ai/api-reference/api-v1/*` (fetched
2026-07-20). The shapes below are not guesses:

- Auth: header `X-API-KEY: <key>` (not a Bearer token).
- `GET /v1/entities?name=...&limit=...` -> `{"entities": [{id, name,
  entity_type, description}, ...]}` -- fuzzy name search, no request body.
- `GET /v1/entities/{entity_id}/introspection` -> `{"properties": [...],
  "relationships": {...}, "numerical_observations": {...}}` -- the available
  field names for that entity.
- `POST /v1/entities/{entity_id}` requires a body naming which `properties`
  to return (an empty/omitted list returns none) -> `GetEntityResponse` with
  `properties: {field: {"value": ..., "sources": [{date, document, name}]}}`.
- `POST /v1/knowledge/query` accepts natural language or Cala QL dot-notation
  (`{"input": "..."}`) -> `{"results": [{...arbitrary projected fields...}],
  "entities": [...] | null}`. Used as a *fallback* (see `search()` below) for
  queries that don't name one specific entity -- e.g. "startups in Spain with
  funding between 10M and 50M" -- since named-entity search has nothing to
  match in that case.

Two things this file deliberately does *not* map, to keep this adapter's
contract (`ProviderAdapter.search`/`.get_document`) uniform with
`SayariAdapter` and to preserve exact, checkable grounding (see
`gateway/synthesis.py`):

- `POST /v1/knowledge/search` -- returns markdown prose already synthesized
  by Cala's own model. Folding that into our own `claims`/`citations` model
  would mean re-extracting discrete assertions out of someone else's
  free-text answer, which is exactly the ungroundable-synthesis problem this
  gateway's architecture avoids by never running an LLM inside the box.
- `numerical_observations` (time-series metrics on an entity).

Known limitation carried over from the entity-lookup design: `entity_key`
for Cala facts is Cala's own UUID, and Sayari's `entity_key` is Sayari's own
identifier -- the two never match for the same real-world company in *live*
mode, so cross-provider conflict detection (`gateway/conflict.py`) only
actually exercises in mock mode today, where the fixtures share slugs by
hand. Real cross-provider entity resolution (matching by name+jurisdiction,
LEI, etc.) is a follow-up, not something this file solves.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from ..config import config
from ..errors import A2KError, ErrorCode
from ._mock_common import get_document_mock, load_entities, search_mock
from .base import Fact, ProviderAdapter, ProviderDocument

KB_ID = "urn:a2k:vendor:cala"

# Cap on how many introspected properties we ask for per entity: keeps the
# POST body and response bounded even for entities with a very rich schema.
MAX_PROPERTIES_PER_ENTITY = 25

# /v1/knowledge/query result rows aren't independently addressable resources
# (no entity id to re-fetch later), so we cache the documents we synthesize
# from them in-process for getDocument. Pod-local and best-effort, same
# honesty tradeoff as the audit JSONL file -- see README.
MAX_QUERY_RESULT_CACHE = 500

# Cache of introspection results (which property *names* exist for an
# entity_id) -- see config.cala_introspection_cache_ttl_seconds. Bounded the
# same way as the other in-process caches in this file.
MAX_INTROSPECTION_CACHE = 1000


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "result"


# Confirmed live 2026-07-21: Cala represents "we have no data for this field"
# as the literal string "<UNKNOWN>" rather than omitting the key or using
# null (e.g. `permid` on an entity with no external PermID on file). Without
# this filter that sentinel becomes a bogus Fact ("X's permid is <UNKNOWN>").
_UNKNOWN_VALUE_SENTINEL = "<unknown>"


def _is_unknown_sentinel(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() == _UNKNOWN_VALUE_SENTINEL


def _wrap_http_error(exc: httpx.HTTPError, context: str) -> A2KError:
    """Confirmed live 2026-07-21: Cala's rate limit (free-tier credits) trips
    fast once a query touches several entities, since each one costs an
    introspection call plus a detail call. A 429 is a transient, retryable
    condition -- distinct from a real upstream failure -- so it gets its own
    error code rather than being lumped into UPSTREAM_ERROR/retryable=False,
    which would tell the caller (K2, or a human) not to bother retrying."""
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        retry_after = exc.response.headers.get("retry-after")
        details = {"retryAfterSeconds": retry_after} if retry_after else {}
        return A2KError(
            ErrorCode.RATE_LIMITED, f"Cala rate limit hit -- {context}: {exc}", retryable=True, details=details
        )
    return A2KError(ErrorCode.UPSTREAM_ERROR, f"{context}: {exc}")


class CalaAdapter(ProviderAdapter):
    kb_id = KB_ID
    display_name = "Cala"

    def __init__(self) -> None:
        self._mock_entities = load_entities("cala_companies.json")
        self._query_result_cache: dict[str, ProviderDocument] = {}
        # entity_id -> (cached_at_monotonic, property names). Never stores
        # values -- only which fields exist -- so cache hits never make an
        # answer stale, they only skip re-discovering the schema.
        self._introspection_cache: dict[str, tuple[float, list[str]]] = {}

    async def search(self, query: str, *, limit: int = 10) -> list[Fact]:
        if config.is_mock:
            return search_mock(self._mock_entities, query, self.kb_id, limit)
        return await self._live_search(query, limit)

    async def get_document(self, document_id: str) -> ProviderDocument | None:
        if config.is_mock:
            return get_document_mock(self._mock_entities, self.kb_id, document_id, self.display_name)
        return await self._live_get_document(document_id)

    # -- live branch (confirmed against api.cala.ai/openapi.json) -------------

    def _headers(self) -> dict[str, str]:
        return {"X-API-KEY": config.cala_api_key, "Content-Type": "application/json"}

    def _require_live_ready(self) -> None:
        if not config.cala_live_ready:
            raise A2KError(
                ErrorCode.UPSTREAM_ERROR,
                "A2K_BOX_MODE=live but CALA_API_KEY is not set.",
                retryable=False,
            )

    async def _live_search(self, query: str, limit: int) -> list[Fact]:
        self._require_live_ready()
        async with httpx.AsyncClient(
            base_url=config.cala_base_url, headers=self._headers(), timeout=15
        ) as client:
            try:
                resp = await client.get("/v1/entities", params={"name": query, "limit": limit})
                resp.raise_for_status()
                candidates = resp.json().get("entities", [])
            except httpx.HTTPError as exc:
                raise _wrap_http_error(exc, "Cala GET /v1/entities failed") from exc

            facts: list[Fact] = []
            for candidate in candidates[:limit]:
                entity_id = candidate.get("id")
                entity_name = candidate.get("name", entity_id)
                if not entity_id:
                    continue
                detail = await self._fetch_entity_detail(client, entity_id, entity_name)
                if detail is not None:
                    facts.extend(self._facts_from_entity_response(detail, entity_id, entity_name))

            if facts:
                return facts

            # No named entity matched -- this is likely a filter/listing
            # query ("startups in Spain with funding 10M-50M") rather than a
            # lookup for one company. Fall back to the structured query
            # endpoint instead of returning nothing.
            return await self._live_query_fallback(client, query, limit)

    def _cached_properties(self, entity_id: str) -> list[str] | None:
        entry = self._introspection_cache.get(entity_id)
        if entry is None:
            return None
        cached_at, properties = entry
        if time.monotonic() - cached_at > config.cala_introspection_cache_ttl_seconds:
            del self._introspection_cache[entity_id]
            return None
        return properties

    def _cache_properties(self, entity_id: str, properties: list[str]) -> None:
        self._introspection_cache[entity_id] = (time.monotonic(), properties)
        while len(self._introspection_cache) > MAX_INTROSPECTION_CACHE:
            del self._introspection_cache[next(iter(self._introspection_cache))]

    async def _fetch_entity_detail(
        self, client: httpx.AsyncClient, entity_id: str, entity_name: str
    ) -> dict[str, Any] | None:
        """Introspect (schema: which properties exist -- cached, see
        `config.cala_introspection_cache_ttl_seconds`) then query (values --
        always fetched fresh, never cached). POST /v1/entities/{id} requires
        naming the properties you want; there is no "give me everything"
        mode.
        """
        properties = self._cached_properties(entity_id)
        if properties is None:
            try:
                introspect_resp = await client.get(f"/v1/entities/{entity_id}/introspection")
                if introspect_resp.status_code == 404:
                    return None
                introspect_resp.raise_for_status()
                properties = introspect_resp.json().get("properties", [])
            except httpx.HTTPError as exc:
                raise _wrap_http_error(exc, f"Cala GET /v1/entities/{entity_id}/introspection failed") from exc
            self._cache_properties(entity_id, properties)

        if not properties:
            return None

        try:
            detail_resp = await client.post(
                f"/v1/entities/{entity_id}", json={"properties": properties[:MAX_PROPERTIES_PER_ENTITY]}
            )
            if detail_resp.status_code == 404:
                return None
            detail_resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise _wrap_http_error(exc, f"Cala POST /v1/entities/{entity_id} failed") from exc
        return detail_resp.json()

    def _facts_from_entity_response(
        self, detail: dict[str, Any], entity_id: str, entity_name: str
    ) -> list[Fact]:
        entity_key = str(entity_id)
        entity_name = detail.get("name") or entity_name
        document_id = f"{self.kb_id}:doc:{entity_key}:profile"
        retrieved_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        facts: list[Fact] = []

        for field_name, entry in (detail.get("properties") or {}).items():
            if not isinstance(entry, dict):
                continue
            value = entry.get("value")
            if value in (None, "") or _is_unknown_sentinel(value):
                continue
            value_str = value if isinstance(value, str) else json.dumps(value, default=str)
            sources = entry.get("sources") or []
            primary_source = sources[0] if sources else {}

            text = f"{entity_name} -- {field_name.replace('_', ' ')}: {value_str} (per Cala)."
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            facts.append(
                Fact(
                    entity_key=entity_key,
                    entity_name=entity_name,
                    field=str(field_name),
                    value=value_str,
                    text=text,
                    document_id=document_id,
                    title=primary_source.get("name") or primary_source.get("document") or f"{entity_name} (Cala)",
                    # Cala's `sources` entries carry a document/date, not a
                    # resolvable URL -- nothing to put here honestly.
                    source_url=None,
                    source_hash=f"sha256:{digest}",
                    retrieved_at=retrieved_at,
                    source_last_updated=primary_source.get("date"),
                    kb_id=self.kb_id,
                    raw=entry,
                )
            )
        return facts

    async def _live_query_fallback(self, client: httpx.AsyncClient, query: str, limit: int) -> list[Fact]:
        try:
            resp = await client.post("/v1/knowledge/query", json={"input": query})
            resp.raise_for_status()
            results = resp.json().get("results", [])
        except httpx.HTTPError as exc:
            raise _wrap_http_error(exc, "Cala POST /v1/knowledge/query failed") from exc

        retrieved_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        facts: list[Fact] = []
        for index, row in enumerate(results[:limit]):
            if not isinstance(row, dict) or not row:
                continue
            entity_name = str(row.get("name") or row.get("title") or f"result {index + 1}")
            slug = _slugify(entity_name)
            entity_key = f"query:{slug}"
            document_id = f"{self.kb_id}:doc:query:{slug}:profile"

            row_facts = []
            for field_name, value in row.items():
                if value in (None, "") or _is_unknown_sentinel(value):
                    continue
                value_str = value if isinstance(value, str) else json.dumps(value, default=str)
                text = f"{entity_name} -- {field_name.replace('_', ' ')}: {value_str} (per Cala, filtered query result)."
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                row_facts.append(
                    Fact(
                        entity_key=entity_key,
                        entity_name=entity_name,
                        field=str(field_name),
                        value=value_str,
                        text=text,
                        document_id=document_id,
                        title=f"{entity_name} (Cala knowledge/query result)",
                        source_url=None,
                        source_hash=f"sha256:{digest}",
                        retrieved_at=retrieved_at,
                        source_last_updated=None,
                        kb_id=self.kb_id,
                        raw=row,
                    )
                )
            if row_facts:
                self._cache_query_result_document(document_id, entity_name, row, retrieved_at)
                facts.extend(row_facts)
        return facts

    def _cache_query_result_document(
        self, document_id: str, entity_name: str, row: dict[str, Any], retrieved_at: str
    ) -> None:
        content = "\n".join(f"{field}: {value}" for field, value in row.items())
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        self._query_result_cache[document_id] = ProviderDocument(
            document_id=document_id,
            title=f"{entity_name} -- Cala knowledge/query result",
            source_url=None,
            mime_type="text/plain",
            content=content,
            hash=f"sha256:{digest}",
            last_updated=retrieved_at,
            classification="public",
        )
        while len(self._query_result_cache) > MAX_QUERY_RESULT_CACHE:
            del self._query_result_cache[next(iter(self._query_result_cache))]

    async def _live_get_document(self, document_id: str) -> ProviderDocument | None:
        cached = self._query_result_cache.get(document_id)
        if cached is not None:
            return cached

        self._require_live_ready()
        try:
            entity_id = document_id.split(":")[-2]
        except IndexError:
            return None

        async with httpx.AsyncClient(
            base_url=config.cala_base_url, headers=self._headers(), timeout=15
        ) as client:
            detail = await self._fetch_entity_detail(client, entity_id, entity_id)
        if detail is None:
            return None

        properties = detail.get("properties") or {}
        content = "\n".join(
            f"{field}: {entry.get('value')}" for field, entry in properties.items() if isinstance(entry, dict)
        )
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return ProviderDocument(
            document_id=document_id,
            title=f"{detail.get('name', entity_id)} -- Cala entity profile",
            source_url=None,
            mime_type="text/plain",
            content=content,
            hash=f"sha256:{digest}",
            last_updated=None,
            classification="public",
        )
