"""Mock MCP server standing in for Sayari's own MCP server (unknown shape --
Sayari's real MCP has not been requested/received yet). Backed by the same
fixtures `A2K_BOX_MODE=mock` uses (`adapters/fixtures/sayari_entities.json`),
so results -- including the Meridian Textiles 62%/48% UBO disagreement with
the Cala mock -- are directly comparable to today's fixture-mock mode.

Run: python -m a2k.dev_mocks.sayari_mock_mcp   (streamable-http on :9002/mcp)
"""

from __future__ import annotations

from dataclasses import asdict

from mcp.server.fastmcp import FastMCP

from ..adapters._mock_common import get_document_mock, load_entities, search_mock

KB_ID = "urn:a2k:vendor:sayari"

mcp = FastMCP(name="sayari-mock", host="0.0.0.0", port=9002, stateless_http=True)
_entities = load_entities("sayari_entities.json")


@mcp.tool(name="search")
def search(query: str, limit: int = 10) -> list[dict]:
    """Search Sayari entities matching `query`. Mirrors the real box's search() contract."""
    return [asdict(f) for f in search_mock(_entities, query, KB_ID, limit)]


@mcp.tool(name="get_document")
def get_document(document_id: str) -> dict | None:
    """Retrieve the full entity profile document behind a citation's documentId."""
    doc = get_document_mock(_entities, KB_ID, document_id, "Sayari")
    return asdict(doc) if doc else None


def main() -> None:
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
