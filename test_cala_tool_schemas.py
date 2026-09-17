"""Ad-hoc probe against Cala's own hosted MCP server (bypasses a2k-box) --
prints the full inputSchema for entity_search/entity_introspection/
entity_retrieval, to check whether entity_retrieval actually accepts a
`relationships` parameter alongside `properties` (adapters/cala_mcp.py's
_fetch_entity_detail currently only ever requests `properties`, discarding
introspection's `relationships` category entirely -- this checks whether
that's fixable by just adding a `relationships` arg, or needs a different
approach).

Usage:
    export CALA_API_KEY=...
    python test_cala_tool_schemas.py
"""

import asyncio
import json
import os

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

CALA_MCP_URL = os.environ.get("CALA_MCP_URL", "https://api.cala.ai/mcp/")
TOOLS_OF_INTEREST = {"entity_search", "entity_introspection", "entity_retrieval"}


async def main() -> None:
    api_key = os.environ["CALA_API_KEY"]
    async with streamablehttp_client(CALA_MCP_URL, {"X-API-KEY": api_key}) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            for tool in result.tools:
                if tool.name in TOOLS_OF_INTEREST:
                    print(f"=== {tool.name} ===")
                    print(f"description: {tool.description}\n")
                    print(f"inputSchema: {json.dumps(tool.inputSchema, indent=2)}")
                    print()


if __name__ == "__main__":
    asyncio.run(main())
