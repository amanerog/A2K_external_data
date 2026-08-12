"""End-to-end local validation: does gateway/engine.py work unmodified when
its adapters fetch data over MCP instead of REST?

Prerequisite -- start both mock servers first, each in its own terminal:
    python -m a2k.dev_mocks.cala_mock_mcp
    python -m a2k.dev_mocks.sayari_mock_mcp

Then run this:
    python -m a2k.dev_mocks.run_local_test

`GatewayEngine` is constructed completely normally (`engine.py` is not
touched); only `.adapters` is swapped afterward for `McpClientAdapter`
instances pointed at the two mock servers above -- this is the same
`dict[str, ProviderAdapter]` shape `engine.py` already expects, so nothing
downstream (fan-out, conflict detection, synthesis, audit) knows
or cares that the data arrived over MCP this time instead of REST.

"Meridian Textiles ownership" is the same query the existing test suite
uses to exercise conflict detection (Cala says 62% UBO, Sayari says 48%) --
if the printed envelope's `conflicts`/`conflictReport` still show that
disagreement, and `responseSignature` is still populated, the MCP-client
path is functionally equivalent to the REST/fixture path for everything
gateway/engine.py is responsible for.
"""

from __future__ import annotations

import asyncio
import json

from ..gateway.engine import GatewayEngine
from ..models.request import A2KRequest
from .mcp_client_adapter import McpClientAdapter


async def main() -> None:
    engine = GatewayEngine()
    engine.adapters = {
        "cala": McpClientAdapter(url="http://localhost:9001/mcp", kb_id="urn:a2k:vendor:cala", display_name="Cala"),
        "sayari": McpClientAdapter(
            url="http://localhost:9002/mcp", kb_id="urn:a2k:vendor:sayari", display_name="Sayari"
        ),
    }

    req = A2KRequest(operation="ask", query="Meridian Textiles ownership")
    envelope = await engine.ask(req)

    print(envelope.model_dump_json(indent=2))
    print()
    print("=" * 70)
    print(f"ok:                {envelope.ok}")
    print(f"conflicts found:   {len(envelope.conflicts)}")
    print(f"signature present: {envelope.responseSignature is not None}")
    print(f"audit logged:      {envelope.audit.logged if envelope.audit else None}")


if __name__ == "__main__":
    asyncio.run(main())
