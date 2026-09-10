"""One-off probe: calls a2k.search on the mcp_to_agent_to_mcp branch's
a2k-box Runtime (IAM/SigV4, same pattern as test_remote_mcp_iam.py in this
directory) to confirm the full chain works end to end -- same chain as
test_a2k_ask_via_agent.py, just the `search` operation instead of `ask`:

    external caller (this script, IAM)
      -> a2k-box's MCP (a2k.search)
      -> gateway/engine.py's _call_agent() (Cognito JWT)
      -> the direct-discovery agent Runtime
      -> Cala's/Sayari's own MCP (live tool discovery)

Usage:
    pip install boto3   # if not already present
    python test_a2k_search_via_agent.py "Acme Robotics"
    python test_a2k_search_via_agent.py "Acme Robotics" cala        # sources=["cala"]
    python test_a2k_search_via_agent.py "Acme Robotics" cala,sayari 5   # + limit=5
"""

import json
import sys
from typing import Optional

import boto3

REGION = "eu-west-1"
AGENT_RUNTIME_ARN = "arn:aws:bedrock-agentcore:eu-west-1:<ACCOUNT_ID>:runtime/a2k_external_data_mcp-A3c4F0Cyx7"
MCP_PROTOCOL_VERSION = "2025-06-18"

QUERY = sys.argv[1] if len(sys.argv) > 1 else "Acme Robotics"
SOURCES = sys.argv[2].split(",") if len(sys.argv) > 2 else None
LIMIT = int(sys.argv[3]) if len(sys.argv) > 3 else None


def _call(client, *, payload: dict, mcp_session_id: Optional[str] = None) -> dict:
    kwargs = dict(
        agentRuntimeArn=AGENT_RUNTIME_ARN,
        qualifier="DEFAULT",
        payload=json.dumps(payload).encode("utf-8"),
        contentType="application/json",
        accept="application/json, text/event-stream",
        mcpProtocolVersion=MCP_PROTOCOL_VERSION,
    )
    if mcp_session_id:
        kwargs["mcpSessionId"] = mcp_session_id

    response = client.invoke_agent_runtime(**kwargs)
    body = response["response"].read().decode("utf-8")
    print(f"--- {payload['method']} -> statusCode={response['statusCode']} mcpSessionId={response.get('mcpSessionId')} ---")
    print(body)
    print()
    return response


def main() -> None:
    client = boto3.client("bedrock-agentcore", region_name=REGION)

    init_response = _call(
        client,
        payload={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "test_a2k_search_via_agent", "version": "1.0"},
            },
        },
    )
    mcp_session_id = init_response.get("mcpSessionId")

    # tools/list first -- its body gets printed above by _call() itself.
    # Check that output for the real tool name (calling a2k-box's Runtime
    # directly like this, it should just be "a2k.search" bare, not
    # Gateway-prefixed -- but confirm before assuming, same reasoning as
    # core.py's suffix matching elsewhere in this repo) and adjust the
    # tools/call below if it doesn't come back bare.
    _call(
        client,
        payload={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        mcp_session_id=mcp_session_id,
    )

    arguments = {"query": QUERY}
    if SOURCES is not None:
        arguments["sources"] = SOURCES
    if LIMIT is not None:
        arguments["limit"] = LIMIT

    _call(
        client,
        payload={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "a2k.search", "arguments": arguments},
        },
        mcp_session_id=mcp_session_id,
    )


if __name__ == "__main__":
    main()
