"""Cala adapter -- MCP transport (Cala's own hosted MCP server, not their
REST API; see `adapters/cala.py` for the REST version this mirrors).

Confirmed live against `https://api.cala.ai/mcp/` (2026-08-10):

- Auth: header `X-API-KEY: <key>` -- identical scheme to the REST API,
  confirmed to be the same credential.
- Transport: streamable-http.
- `entity_search({"name": ..., "limit": ...})` -> `{"entities": [{id, name,
  entity_type, description}, ...]}` -- same shape as `GET /v1/entities`.
- `entity_introspection({"entity_id": ...})` -> `{"properties": [...],
  "relationships": {...}, "numerical_observations": {...}}` -- same shape
  as `GET /v1/entities/{id}/introspection`.
- `entity_retrieval({"entity_id": ..., "properties": [...]})` -> same
  response shape as `POST /v1/entities/{id}`: `{properties: {field:
  {"value": ..., "sources": [{name, document, date}]}}, ...}`. Because the
  shape is identical to the REST version, Fact-building reuses
  `cala.facts_from_entity_response` rather than duplicating it -- see that
  function's docstring.
- `knowledge_query({"input": ...})` -- **not yet confirmed live** (only
  entity_search/introspection/retrieval were exercised against the real
  account so far, blocked earlier by an account-balance error, not a
  schema question). Ported over from the REST fallback on the assumption
  the MCP tool's response shape matches `POST /v1/knowledge/query`'s
  `{"results": [...], "entities": [...] | null}` -- confirm before relying
  on this path, same TODO(live) posture as the rest of this codebase for
  anything not independently verified.

- `knowledge_search({"input": ...})` -- confirmed live 2026-08-11. Unlike
  `POST /v1/knowledge/search` on the REST side (deliberately unmapped, see
  cala.py's module docstring -- flat markdown prose with no per-sentence
  source linkage), this MCP tool returns `{"content": "<markdown answer>",
  "explainability": [{"content": "<claim, in Cala's own words>",
  "references": ["<context id>", ...]}, ...], "context": [{"id": ...,
  "content": "<synthesized fact>", "origins": [{"source": {name, url},
  "document": {name, url}}, ...]}, ...]}`. `explainability[]` is Cala's own
  decomposition of the answer into discrete, source-referenced claims --
  `facts_from_knowledge_search()` below builds one `Fact` per
  `explainability` item, quoting its `content` *verbatim* (so
  `groundedRatio` in gateway/synthesis.py stays an honest, exact count --
  we cite exactly what we quote, nothing paraphrased on our end) and citing
  the first origin of the first referenced context item, mirroring the
  `sources[0]` convention `facts_from_entity_response` already uses.

  Coarser than entity_retrieval's field-level citations: one `context` item
  can list several origins at once (e.g. 10 URLs for one aggregated fact),
  so "this exact source said this exact thing" is Cala's claim, not
  something we independently re-verify here -- same trust boundary as
  entity_retrieval's `sources[]`, just aggregated over more material.
  `CALA_SEARCH_MODE` (default `entity_first`) controls whether/when this
  tool is used at all -- see config.py.

`knowledge_search` is intentionally *not* used by concatenating `content`
(the flowing prose) into a Fact -- that would reintroduce exactly the
ungroundable-synthesis problem `cala.py` documents for the REST equivalent,
since `content` mixes multiple claims into continuous narrative text that
can't be cleanly attributed span-by-span. `explainability[]` is the part of
this tool's response that's actually structured for citation.
"""

from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from ..config import config
from ..errors import A2KError, ErrorCode
from ._mock_common import get_document_mock, load_entities, search_mock
from .base import Fact, ProviderAdapter, ProviderDocument
from .cala import MAX_PROPERTIES_PER_ENTITY, KB_ID, facts_from_entity_response

MAX_QUERY_RESULT_CACHE = 500
MAX_INTROSPECTION_CACHE = 1000

_HTTP_STATUS_RE = re.compile(r"HTTP error (\d+)")


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "result"


class CalaMcpAdapter(ProviderAdapter):
    kb_id = KB_ID
    display_name = "Cala"

    def __init__(self) -> None:
        self._mock_entities = load_entities("cala_companies.json")
        self._query_result_cache: dict[str, ProviderDocument] = {}
        self._introspection_cache: dict[str, tuple[float, list[str]]] = {}

    async def search(self, query: str, *, limit: int = 10) -> list[Fact]:
        if config.is_mock:
            return search_mock(self._mock_entities, query, self.kb_id, limit)
        return await self._live_search(query, limit)

    async def get_document(self, document_id: str) -> ProviderDocument | None:
        if config.is_mock:
            return get_document_mock(self._mock_entities, self.kb_id, document_id, self.display_name)
        return await self._live_get_document(document_id)

    # -- MCP session handling ---------------------------------------------

    def _require_live_ready(self) -> None:
        if not config.cala_live_ready:
            raise A2KError(
                ErrorCode.UPSTREAM_ERROR,
                "A2K_BOX_MODE=live but CALA_API_KEY is not set.",
                retryable=False,
            )

    def _open_session(self):
        """Async context manager yielding (read, write, _) streams for an
        MCP session. Opened once per top-level search()/get_document() call
        and reused across the several tool calls each of those needs -- not
        one connection per tool call."""
        return streamablehttp_client(
            config.cala_mcp_url, {"X-API-KEY": config.cala_api_key}, timeout=timedelta(seconds=30)
        )

    async def _call_tool(self, session: ClientSession, tool: str, arguments: dict) -> dict | None:
        """Returns the tool's `structuredContent` dict as-is. Confirmed live
        (2026-08-10) that Cala's own tools (entity_search/introspection/
        retrieval) return their JSON object directly -- e.g. entity_search's
        structuredContent is `{"entities": [...]}`, not `{"result":
        {"entities": [...]}}`. The `{"result": ...}` wrapper only appears
        for tools whose return type is a bare list/scalar rather than an
        object (that's a FastMCP convention our own dev_mocks tools trigger,
        not something Cala's real tools do -- see
        `dev_mocks/mcp_client_adapter.py` for the contrast)."""
        try:
            result = await session.call_tool(tool, arguments)
        except Exception as exc:  # connection/transport-level failure, not a tool error
            raise A2KError(ErrorCode.UPSTREAM_ERROR, f"Cala MCP tool {tool!r} unreachable: {exc}") from exc

        if not result.isError:
            return result.structuredContent

        message = result.content[0].text if result.content else f"tool {tool!r} failed"
        status_match = _HTTP_STATUS_RE.search(message)
        if status_match and status_match.group(1) == "429":
            raise A2KError(ErrorCode.RATE_LIMITED, f"Cala rate limit hit -- {tool}: {message}", retryable=True)
        if status_match and status_match.group(1) == "404":
            return None
        raise A2KError(ErrorCode.UPSTREAM_ERROR, f"Cala MCP tool {tool!r} failed: {message}")

    # -- live branch (confirmed against api.cala.ai/mcp/, 2026-08-10) -----

    async def _live_search(self, query: str, limit: int) -> list[Fact]:
        self._require_live_ready()
        async with self._open_session() as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await self._search_with_mode(session, query, limit)

    async def _search_with_mode(self, session: ClientSession, query: str, limit: int) -> list[Fact]:
        mode = config.cala_search_mode

        if mode == "knowledge_search_only":
            return await self._knowledge_search_facts(session, query, limit)

        facts = await self._entity_search_facts(session, query, limit)

        if mode == "parallel":
            facts.extend(await self._knowledge_search_facts(session, query, limit))
            return facts

        # mode == "entity_first" (default): entity lookup, then the
        # structured-query fallback, and only then knowledge_search as the
        # last resort -- same tier order as the REST adapter's fallback,
        # with knowledge_search added below it rather than replacing it.
        if facts:
            return facts

        facts = await self._live_query_fallback(session, query, limit)
        if facts:
            return facts

        return await self._knowledge_search_facts(session, query, limit)

    async def _entity_search_facts(self, session: ClientSession, query: str, limit: int) -> list[Fact]:
        candidates = await self._call_tool(session, "entity_search", {"name": query, "limit": limit})
        candidates = candidates.get("entities", []) if isinstance(candidates, dict) else (candidates or [])

        facts: list[Fact] = []
        for candidate in candidates[: config.max_entities_to_hydrate]:
            entity_id = candidate.get("id")
            entity_name = candidate.get("name", entity_id)
            if not entity_id:
                continue
            detail = await self._fetch_entity_detail(session, entity_id, entity_name)
            if detail is not None:
                facts.extend(facts_from_entity_response(detail, entity_id, entity_name, self.kb_id))
        return facts

    async def _knowledge_search_facts(self, session: ClientSession, query: str, limit: int) -> list[Fact]:
        response = await self._call_tool(session, "knowledge_search", {"input": query})
        if not isinstance(response, dict):
            return []

        context_by_id = {c["id"]: c for c in response.get("context", []) if isinstance(c, dict) and c.get("id")}
        retrieved_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        slug = _slugify(query)
        document_id = f"{self.kb_id}:doc:knowledge_search:{slug}:profile"

        facts: list[Fact] = []
        for index, item in enumerate((response.get("explainability") or [])[:limit]):
            text = item.get("content")
            if not text:
                continue

            # Mirrors facts_from_entity_response's `sources[0]` convention:
            # take the first origin of the first referenced context item as
            # the primary citation. A claim can reference several context
            # items (each potentially multi-sourced); we cite one, honestly
            # -- not all of them at once.
            primary_source: dict[str, Any] = {}
            for ref_id in item.get("references") or []:
                ctx = context_by_id.get(ref_id)
                origins = ctx.get("origins") if ctx else None
                if origins:
                    primary_source = origins[0].get("document") or origins[0].get("source") or {}
                    break

            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            facts.append(
                Fact(
                    entity_key=f"knowledge_search:{slug}",
                    entity_name=query,
                    field=f"finding_{index + 1}",
                    value=text,
                    text=text,
                    document_id=document_id,
                    title=primary_source.get("name") or f"Cala knowledge_search: {query}",
                    source_url=primary_source.get("url"),
                    source_hash=f"sha256:{digest}",
                    retrieved_at=retrieved_at,
                    source_last_updated=None,
                    kb_id=self.kb_id,
                    raw=item,
                )
            )

        if facts:
            self._cache_knowledge_search_document(document_id, query, response, retrieved_at)
        return facts

    def _cache_knowledge_search_document(
        self, document_id: str, query: str, response: dict[str, Any], retrieved_at: str
    ) -> None:
        content = response.get("content") or ""
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        self._query_result_cache[document_id] = ProviderDocument(
            document_id=document_id,
            title=f"Cala knowledge_search: {query}",
            source_url=None,
            mime_type="text/markdown",
            content=content,
            hash=f"sha256:{digest}",
            last_updated=retrieved_at,
            classification="public",
        )
        while len(self._query_result_cache) > MAX_QUERY_RESULT_CACHE:
            del self._query_result_cache[next(iter(self._query_result_cache))]

    def _cached_properties(self, entity_id: str) -> list[str] | None:
        entry = self._introspection_cache.get(entity_id)
        if entry is None:
            return None
        cached_at, properties = entry
        if time.monotonic() - cached_at >= config.cala_introspection_cache_ttl_seconds:
            del self._introspection_cache[entity_id]
            return None
        return properties

    def _cache_properties(self, entity_id: str, properties: list[str]) -> None:
        self._introspection_cache[entity_id] = (time.monotonic(), properties)
        while len(self._introspection_cache) > MAX_INTROSPECTION_CACHE:
            del self._introspection_cache[next(iter(self._introspection_cache))]

    async def _fetch_entity_detail(
        self, session: ClientSession, entity_id: str, entity_name: str
    ) -> dict[str, Any] | None:
        properties = self._cached_properties(entity_id)
        if properties is None:
            intro = await self._call_tool(session, "entity_introspection", {"entity_id": entity_id})
            if intro is None:
                return None
            properties = intro.get("properties", [])
            self._cache_properties(entity_id, properties)

        if not properties:
            return None

        return await self._call_tool(
            session,
            "entity_retrieval",
            {"entity_id": entity_id, "properties": properties[:MAX_PROPERTIES_PER_ENTITY]},
        )

    async def _live_query_fallback(self, session: ClientSession, query: str, limit: int) -> list[Fact]:
        response = await self._call_tool(session, "knowledge_query", {"input": query})
        results = (response or {}).get("results", []) if isinstance(response, dict) else []

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
                if value in (None, ""):
                    continue
                value_str = value if isinstance(value, str) else str(value)
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
                        title=f"{entity_name} (Cala knowledge_query result)",
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
            title=f"{entity_name} -- Cala knowledge_query result",
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

        async with self._open_session() as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                detail = await self._fetch_entity_detail(session, entity_id, entity_id)
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
