"""AgentCore Runtime entrypoint for the mcp_to_agent_to_mcp branch --
invoked by a2k-box (gateway/engine.py), not by a human/curl caller or by
the Gateway the way the other entrypoints in this directory are. Additive:
entrypoint.py/entrypoint_generic.py/entrypoint_v3.py and everything they
call in core.py are untouched by this file.

Invocation payload contract (a2k-box builds this, see gateway/engine.py):

    {
      "operation": "ask" | "search",
      "query": "<query text, already vendor-agnostic -- direct_agent.py's
                 phase-2 per-vendor agent decides how to shape it for
                 whatever tool it ends up calling>",
      "sources": ["cala", "sayari"] | null,   # null -> phase 1 decides
      "catalogue": [ {"sourceId": ..., "status": ..., "domains": [...],
                       "topics": [...], "scope": ..., "queryType": ...}, ... ]
                    # same shape a2k.listVendors already returns -- a2k-box
                    # passes it straight through, no re-fetch needed here.
      "requestId": "<a2k-box's own gateway/engine.py requestId for this
                     call, see engine.py's _request_id() -- threaded through
                     to observability.py as session_id below purely so the
                     agent.tool_call log lines / EMF metrics this process
                     emits can be correlated with a2k-box's own audit record
                     for the same request, same idea as core.py's
                     session_id param>
    }

Response: `{"ok": true, "content": {...}}` (AskContent's or SearchContent's
shape, see direct_agent.py) or `{"ok": false, "error": "<message>"}` on
failure -- gateway/engine.py maps either into the full CitedResponseEnvelope
(it owns requestId/audit/freshness/accessDecision regardless of which shape
comes back).

Auth: unlike entrypoint.py/entrypoint_v3.py (JWT/Cognito inbound, meant for
human curl testing), nothing here is aware of *how* it was authenticated --
that's entirely the Runtime's own inbound-auth configuration, same as every
other entrypoint in this directory. See a2k/config.py's agent_pool_* fields
and this branch's plan for how a2k-box authenticates its side of the call.
"""

from __future__ import annotations

import asyncio
import os

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.runtime.context import RequestContext

import direct_agent

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload: dict, context: RequestContext) -> dict:
    operation = payload.get("operation", "ask")
    query = payload.get("query", "")
    sources = payload.get("sources")
    catalogue = payload.get("catalogue") or []
    # session_id: a2k-box's own requestId, so agent/observability.py's logs/
    # metrics for this call can be correlated with a2k-box's audit record for
    # the same request. internal_client is hardcoded, not read from the
    # payload -- this entrypoint's only legitimate caller is a2k-box itself
    # (see module docstring), unlike entrypoint.py/entrypoint_v3.py which
    # take internal_client from whoever's actually calling them.
    session_id = payload.get("requestId")

    try:
        content = asyncio.run(
            direct_agent.handle(
                operation=operation,
                query=query,
                sources=sources,
                catalogue=catalogue,
                model_id=os.environ["BEDROCK_MODEL_ID"],
                region=os.environ.get("AWS_REGION", "eu-west-1"),
                session_id=session_id,
                internal_client="a2k-box",
            )
        )
    except Exception as exc:
        # Plain, honest failure -- gateway/engine.py turns this into a normal
        # A2KError/ErrorObject on its side (same UPSTREAM_ERROR shape it
        # already uses for adapter failures on the Gateway-mediated path),
        # never fabricates a response to cover for it.
        return {"ok": False, "error": str(exc)}

    return {"ok": True, "content": content}


if __name__ == "__main__":
    app.run()
