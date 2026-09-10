"""One-off probe: calls a2k.listVendors and a2k.getCard on the deployed
a2k-box Runtime -- same IAM/boto3 pattern as test_a2k_ask_via_agent.py --
to confirm the DynamoDB-backed vendor cards (a2k/cards/__init__.py,
VENDOR_CARDS_TABLE) are wired up: right IAM permission (dynamodb:Scan) on
the Runtime's execution role, right table name/region, cards actually
readable.

Usage:
    pip install boto3   # if not already present
    python test_a2k_list_vendors.py
"""

import json
from typing import Optional

import boto3

REGION = "eu-west-1"
AGENT_RUNTIME_ARN = "arn:aws:bedrock-agentcore:eu-west-1:<ACCOUNT_ID>:runtime/a2k_external_data_mcp-A3c4F0Cyx7"
MCP_PROTOCOL_VERSION = "2025-06-18"


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
                "clientInfo": {"name": "test_a2k_list_vendors", "version": "1.0"},
            },
        },
    )
    mcp_session_id = init_response.get("mcpSessionId")

    # If DynamoDB isn't wired up right (missing dynamodb:Scan permission,
    # wrong table name/region), this comes back as an isError tool result
    # with the AccessDeniedException/ResourceNotFoundException message --
    # not a transport-level failure, so check the body even on statusCode=200.
    _call(
        client,
        payload={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "a2k.listVendors", "arguments": {}},
        },
        mcp_session_id=mcp_session_id,
    )

    _call(
        client,
        payload={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "a2k.getCard", "arguments": {}},
        },
        mcp_session_id=mcp_session_id,
    )


if __name__ == "__main__":
    main()
