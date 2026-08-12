"""A ProviderAdapter that speaks MCP instead of REST.

This is the shape that answers the real question: can gateway/engine.py
consume CALA/Sayari over MCP with *zero* changes to engine.py,
audit.py, or conflict.py? ProviderAdapter is already the abstraction that
makes this possible -- engine.py only ever calls `.search()`/
`.get_document()` and gets back `Fact`/`ProviderDocument` objects; it has
no idea (and no code path that cares) whether those came from `httpx`
REST calls (today's cala.py/sayari.py) or an MCP tool call (this class).

Talks to the mock servers in this package today. Once CALA/Sayari hand
over real MCP endpoints, this is the class to adapt: swap `tool="search"`/
`tool="get_document"` for their real tool names, and `_fact_from_dict`/
`_document_from_dict` for however their response fields actually map --
everything else (this class's shape, and all of gateway/engine.py) stays
the same.
"""

from __future__ import annotations

from datetime import timedelta

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from ..adapters.base import Fact, ProviderAdapter, ProviderDocument


class McpClientAdapter(ProviderAdapter):
    def __init__(self, *, url: str, kb_id: str, display_name: str) -> None:
        self.url = url
        self.kb_id = kb_id
        self.display_name = display_name

    async def _call_tool(self, tool: str, arguments: dict) -> dict | list | None:
        async with streamablehttp_client(self.url, timeout=timedelta(seconds=30)) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool, arguments)
                if result.isError:
                    raise RuntimeError(f"{self.display_name} MCP tool {tool!r} failed: {result.content}")
                return result.structuredContent.get("result") if result.structuredContent else None

    async def search(self, query: str, *, limit: int = 10) -> list[Fact]:
        raw_facts = await self._call_tool("search", {"query": query, "limit": limit})
        return [Fact(**f) for f in (raw_facts or [])]

    async def get_document(self, document_id: str) -> ProviderDocument | None:
        raw_doc = await self._call_tool("get_document", {"document_id": document_id})
        return ProviderDocument(**raw_doc) if raw_doc else None
