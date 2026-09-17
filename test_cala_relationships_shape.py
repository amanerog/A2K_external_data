"""Ad-hoc probe against Cala's own hosted MCP server (bypasses a2k-box) --
resolves a real entity (Microsoft, by default) via entity_search, then calls
entity_introspection on it and prints the raw `relationships` field, to see
its exact shape before wiring it into entity_retrieval's `relationships`
param (which expects {"outgoing": {...}, "incoming": {...}}, per
test_cala_tool_schemas.py's findings).

Usage:
    export CALA_API_KEY=...
    python test_cala_relationships_shape.py ["Microsoft"]
"""

import asyncio
import json
import os
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

CALA_MCP_URL = os.environ.get("CALA_MCP_URL", "https://api.cala.ai/mcp/")
QUERY = sys.argv[1] if len(sys.argv) > 1 else "Microsoft"


async def main() -> None:
    api_key = os.environ["CALA_API_KEY"]
    async with streamablehttp_client(CALA_MCP_URL, {"X-API-KEY": api_key}) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            search_result = await session.call_tool("entity_search", {"name": QUERY})
            search_data = search_result.structuredContent or json.loads(search_result.content[0].text)
            print("=== entity_search result ===")
            print(json.dumps(search_data, indent=2)[:1500])

            entities = search_data.get("entities") or search_data.get("results") or []
            if not entities:
                print("\nNo entities found -- can't continue.")
                return
            entity_id = entities[0].get("id") or entities[0].get("entity_id")
            print(f"\nUsing entity_id={entity_id}\n")

            intro_result = await session.call_tool("entity_introspection", {"entity_id": entity_id})
            intro_data = intro_result.structuredContent or json.loads(intro_result.content[0].text)

            print("=== entity_introspection: top-level keys ===")
            print(list(intro_data.keys()))

            print("\n=== entity_introspection: raw `relationships` field ===")
            print(json.dumps(intro_data.get("relationships"), indent=2)[:3000])


if __name__ == "__main__":
    asyncio.run(main())
