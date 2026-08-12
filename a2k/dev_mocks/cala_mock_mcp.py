"""Mock MCP server standing in for CALA's own MCP server (unknown shape --
CALA's real MCP has not been requested/received yet, see resumen-desarrollo
timeline). Backed by the exact same fixtures `A2K_BOX_MODE=mock` uses
(`adapters/fixtures/cala_entities.json`), so results are directly
comparable to what `CalaAdapter` already returns in fixture-mock mode --
the only thing under test here is the transport (MCP instead of in-process
Python), not the data.

Run: python -m a2k.dev_mocks.cala_mock_mcp   (streamable-http on :9001/mcp)
"""

from __future__ import annotations

from dataclasses import asdict

from mcp.server.fastmcp import FastMCP

from ..adapters._mock_common import get_document_mock, load_entities, search_mock

KB_ID = "urn:a2k:vendor:cala"

mcp = FastMCP(name="cala-mock", host="0.0.0.0", port=9001, stateless_http=True)
_entities = load_entities("cala_companies.json")


@mcp.tool(name="search")
def search(query: str, limit: int = 10) -> list[dict]:
    """Search CALA entities matching `query`. Mirrors the real box's search() contract."""
    return [asdict(f) for f in search_mock(_entities, query, KB_ID, limit)]


@mcp.tool(name="get_document")
def get_document(document_id: str) -> dict | None:
    """Retrieve the full entity profile document behind a citation's documentId."""
    doc = get_document_mock(_entities, KB_ID, document_id, "Cala")
    return asdict(doc) if doc else None


def main() -> None:
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
