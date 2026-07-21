"""Sayari adapter (https://sayari.com -- corporate ownership/risk entity graph).

Confirmed by documentation research (documentation.sayari.com, 2026-07-14):
REST API, JSON-encoded, OAuth2 client-credentials auth, resource-oriented
URLs, entity search/resolution and a relationships graph. Sayari does not
publish exact endpoint paths in the crawlable docs, so the live branch below
targets the conventional `/oauth/token` + `/v1/entity/search` +
`/v1/entity/{id}` shape used by comparable entity-graph APIs -- flagged
TODO(live) for confirmation once SAYARI_CLIENT_ID/SECRET are available.
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from ..config import config
from ..errors import A2KError, ErrorCode
from ._mock_common import get_document_mock, load_entities, search_mock
from .base import Fact, ProviderAdapter, ProviderDocument

KB_ID = "urn:a2k:vendor:sayari"


class SayariAdapter(ProviderAdapter):
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

    # -- live branch (best-effort; needs validation against real credentials) --

    async def _get_token(self, client: httpx.AsyncClient) -> str:
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        try:
            resp = await client.post(
                "/oauth/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": config.sayari_client_id,
                    "client_secret": config.sayari_client_secret,
                },
            )
            resp.raise_for_status()
            payload = resp.json()
        except httpx.HTTPError as exc:
            raise A2KError(ErrorCode.UPSTREAM_ERROR, f"Sayari OAuth2 token request failed: {exc}") from exc
        self._token = payload["access_token"]
        self._token_expires_at = time.monotonic() + float(payload.get("expires_in", 3600)) - 30
        return self._token

    async def _live_search(self, query: str, limit: int) -> list[Fact]:
        if not config.sayari_live_ready:
            raise A2KError(
                ErrorCode.UPSTREAM_ERROR,
                "A2K_BOX_MODE=live but SAYARI_CLIENT_ID/SAYARI_CLIENT_SECRET are not set.",
                retryable=False,
            )
        async with httpx.AsyncClient(base_url=config.sayari_base_url, timeout=15) as client:
            token = await self._get_token(client)
            headers = {"Authorization": f"Bearer {token}"}
            try:
                # TODO(live): confirm exact search path/params once credentials exist.
                resp = await client.get(
                    "/v1/entity/search", params={"q": query, "limit": limit}, headers=headers
                )
                resp.raise_for_status()
                payload = resp.json()
            except httpx.HTTPError as exc:
                raise A2KError(ErrorCode.UPSTREAM_ERROR, f"Sayari entity search failed: {exc}") from exc

            candidates = payload.get("data", payload) if isinstance(payload, dict) else payload
            facts: list[Fact] = []
            for candidate in candidates[:limit]:
                entity_id = candidate.get("id")
                if not entity_id:
                    continue
                try:
                    detail_resp = await client.get(f"/v1/entity/{entity_id}", headers=headers)
                    detail_resp.raise_for_status()
                except httpx.HTTPError as exc:
                    raise A2KError(
                        ErrorCode.UPSTREAM_ERROR, f"Sayari entity detail request failed: {exc}"
                    ) from exc
                facts.extend(self._facts_from_live_entity(detail_resp.json(), entity_id))
            return facts

    def _facts_from_live_entity(self, detail: dict[str, Any], entity_id: str) -> list[Fact]:
        # TODO(live): map Sayari's real attribute/relationship schema once confirmed.
        entity_key = str(entity_id)
        entity_name = detail.get("name", entity_key)
        document_id = f"{self.kb_id}:doc:{entity_key}:profile"
        retrieved_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        facts = []
        for field_name, value in detail.items():
            if field_name in {"id", "name"} or value in (None, ""):
                continue
            text = f"{entity_name} -- {field_name}: {value} (per Sayari)."
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            facts.append(
                Fact(
                    entity_key=entity_key,
                    entity_name=entity_name,
                    field=str(field_name),
                    value=str(value),
                    text=text,
                    document_id=document_id,
                    title=f"{entity_name} -- Sayari entity record",
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
        if not config.sayari_live_ready:
            raise A2KError(
                ErrorCode.UPSTREAM_ERROR,
                "A2K_BOX_MODE=live but SAYARI_CLIENT_ID/SAYARI_CLIENT_SECRET are not set.",
                retryable=False,
            )
        try:
            entity_id = document_id.split(":")[-2]
        except IndexError:
            return None
        async with httpx.AsyncClient(base_url=config.sayari_base_url, timeout=15) as client:
            token = await self._get_token(client)
            headers = {"Authorization": f"Bearer {token}"}
            try:
                resp = await client.get(f"/v1/entity/{entity_id}", headers=headers)
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise A2KError(ErrorCode.UPSTREAM_ERROR, f"Sayari getDocument failed: {exc}") from exc
            detail = resp.json()
        content = "\n".join(f"{k}: {v}" for k, v in detail.items() if k != "id")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return ProviderDocument(
            document_id=document_id,
            title=f"{detail.get('name', entity_id)} -- Sayari entity profile",
            source_url=detail.get("source_url"),
            mime_type="text/plain",
            content=content,
            hash=f"sha256:{digest}",
            last_updated=detail.get("last_updated"),
            classification="public",
        )
