"""Cala adapter (https://cala.ai -- financial/legal/regulatory data platform).

Confirmed by documentation research (docs.cala.ai, 2026-07-14): Cala exposes
`/v1/knowledge/search` (markdown + citations), `/v1/knowledge/query`
(structured JSON rows), `/v1/entities` (name lookup) and
`/v1/entities/{entity_id}` (entity detail), authenticated with an API key.
Exact request/response schemas were not published at fetch time, so the live
branch below is a best-effort mapping -- flagged with TODO(live) where the
response shape needs confirming once CALA_API_KEY is available.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

import httpx

from ..config import config
from ..errors import A2KError, ErrorCode
from ._mock_common import get_document_mock, load_entities, search_mock
from .base import Fact, ProviderAdapter, ProviderDocument

KB_ID = "urn:a2k:vendor:cala"


class CalaAdapter(ProviderAdapter):
    kb_id = KB_ID
    display_name = "Cala"

    def __init__(self) -> None:
        self._mock_entities = load_entities("cala_companies.json")

    async def search(self, query: str, *, limit: int = 10) -> list[Fact]:
        if config.is_mock:
            return search_mock(self._mock_entities, query, self.kb_id, limit)
        return await self._live_search(query, limit)

    async def get_document(self, document_id: str) -> ProviderDocument | None:
        if config.is_mock:
            return get_document_mock(self._mock_entities, self.kb_id, document_id, self.display_name)
        return await self._live_get_document(document_id)

    # -- live branch (best-effort; needs validation against real credentials) --

    async def _live_search(self, query: str, limit: int) -> list[Fact]:
        if not config.cala_live_ready:
            raise A2KError(
                ErrorCode.UPSTREAM_ERROR,
                "A2K_BOX_MODE=live but CALA_API_KEY is not set.",
                retryable=False,
            )
        headers = {"Authorization": f"Bearer {config.cala_api_key}"}
        async with httpx.AsyncClient(base_url=config.cala_base_url, headers=headers, timeout=15) as client:
            try:
                resp = await client.get("/v1/entities", params={"name": query, "limit": limit})
                resp.raise_for_status()
                payload = resp.json()
            except httpx.HTTPError as exc:
                raise A2KError(ErrorCode.UPSTREAM_ERROR, f"Cala /v1/entities request failed: {exc}") from exc

            # TODO(live): confirm the exact list envelope Cala returns (this
            # defensively handles either a bare list or a {"data": [...]} wrapper).
            candidates = payload.get("data", payload) if isinstance(payload, dict) else payload
            facts: list[Fact] = []
            for candidate in candidates[:limit]:
                entity_id = candidate.get("id") or candidate.get("entity_id")
                if not entity_id:
                    continue
                try:
                    detail_resp = await client.get(f"/v1/entities/{entity_id}")
                    detail_resp.raise_for_status()
                except httpx.HTTPError as exc:
                    raise A2KError(
                        ErrorCode.UPSTREAM_ERROR, f"Cala /v1/entities/{entity_id} request failed: {exc}"
                    ) from exc
                facts.extend(self._facts_from_live_entity(detail_resp.json(), entity_id))
            return facts

    def _facts_from_live_entity(self, detail: dict[str, Any], entity_id: str) -> list[Fact]:
        # TODO(live): map Cala's real field names once confirmed; this assumes
        # a flat dict of attribute -> value plus a "name" key, which matches
        # the "verified, structured facts" framing from the Cala site copy.
        entity_key = str(entity_id)
        entity_name = detail.get("name", entity_key)
        document_id = f"{self.kb_id}:doc:{entity_key}:profile"
        retrieved_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        facts = []
        for field_name, value in detail.items():
            if field_name in {"id", "entity_id", "name"} or value in (None, ""):
                continue
            text = f"{entity_name} -- {field_name}: {value} (per Cala)."
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            facts.append(
                Fact(
                    entity_key=entity_key,
                    entity_name=entity_name,
                    field=str(field_name),
                    value=str(value),
                    text=text,
                    document_id=document_id,
                    title=f"{entity_name} -- Cala entity record",
                    source_url=detail.get("source_url"),
                    source_hash=f"sha256:{digest}",
                    retrieved_at=retrieved_at,
                    source_last_updated=detail.get("last_updated"),
                    kb_id=self.kb_id,
                    raw=detail,
                )
            )
        return facts

    async def _live_get_document(self, document_id: str) -> ProviderDocument | None:
        if not config.cala_live_ready:
            raise A2KError(
                ErrorCode.UPSTREAM_ERROR,
                "A2K_BOX_MODE=live but CALA_API_KEY is not set.",
                retryable=False,
            )
        try:
            entity_id = document_id.split(":")[-2]
        except IndexError:
            return None
        headers = {"Authorization": f"Bearer {config.cala_api_key}"}
        async with httpx.AsyncClient(base_url=config.cala_base_url, headers=headers, timeout=15) as client:
            try:
                resp = await client.get(f"/v1/entities/{entity_id}")
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise A2KError(ErrorCode.UPSTREAM_ERROR, f"Cala getDocument failed: {exc}") from exc
            detail = resp.json()
        content = "\n".join(f"{k}: {v}" for k, v in detail.items() if k not in {"id", "entity_id"})
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return ProviderDocument(
            document_id=document_id,
            title=f"{detail.get('name', entity_id)} -- Cala entity profile",
            source_url=detail.get("source_url"),
            mime_type="text/plain",
            content=content,
            hash=f"sha256:{digest}",
            last_updated=detail.get("last_updated"),
            classification="public",
        )
