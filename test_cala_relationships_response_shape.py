"""Ad-hoc probe against Cala's own hosted MCP server (bypasses a2k-box) --
resolves a real entity (Microsoft, by default), discovers its relationship
types via entity_introspection, then calls entity_retrieval WITH a
`relationships` body and prints the raw response -- specifically the
`relationships` section -- so adapters/cala.py's facts_from_entity_response
can be extended to build Facts from it (it currently only reads
`properties`, ignoring `relationships` entirely even though
adapters/cala_mcp.py's _fetch_entity_detail now requests it).

Usage:
    export CALA_API_KEY=...
    python test_cala_relationships_response_shape.py ["Microsoft"]
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
            entities = search_data.get("entities") or search_data.get("results") or []
            if not entities:
                print("No entities found -- can't continue.")
                return
            entity_id = entities[0].get("id") or entities[0].get("entity_id")
            print(f"Using entity_id={entity_id} ({entities[0].get('name')})\n")

            intro_result = await session.call_tool("entity_introspection", {"entity_id": entity_id})
            intro_data = intro_result.structuredContent or json.loads(intro_result.content[0].text)
            relationships = intro_data.get("relationships") or {}

            # Same conversion _build_relationship_query does: flat list per direction -> {type: {}}.
            relationship_query = {
                direction: {rel_type: {} for rel_type in relationships.get(direction, [])}
                for direction in ("outgoing", "incoming")
                if relationships.get(direction)
            }
            print(f"Requesting relationships: {json.dumps(relationship_query, indent=2)}\n")

            retrieval_result = await session.call_tool(
                "entity_retrieval",
                {
                    "entity_id": entity_id,
                    "properties": (intro_data.get("properties") or [])[:5],
                    "relationships": relationship_query,
                },
            )
            retrieval_data = retrieval_result.structuredContent or json.loads(retrieval_result.content[0].text)

            print("=== entity_retrieval: top-level keys ===")
            print(list(retrieval_data.keys()))

            print("\n=== entity_retrieval: raw `relationships` field ===")
            print(json.dumps(retrieval_data.get("relationships"), indent=2)[:5000])


if __name__ == "__main__":
    asyncio.run(main())
