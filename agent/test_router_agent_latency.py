"""Measures whether core.py's token/MCP-connection caching (see its module
docstring "Latency") actually pays off across calls -- runs the same
question N times against the deployed router agent, pinning all calls to
the *same* session (via the X-Amzn-Bedrock-AgentCore-Runtime-Session-Id
header) so they land on the same warm AgentCore Runtime container
(otherwise AWS may route each call to a fresh microVM with its own empty
cache, and this would measure nothing).

Uses a JWT Bearer token over raw HTTPS, not boto3's invoke_agent_runtime --
see test_router_agent_jwt.py's docstring for why (this Runtime's inbound
authorizer was switched from IAM to Cognito on 2026-08-19, and an
AgentCore Runtime only ever supports one inbound auth mode at a time).

Usage:
    export AGENT_CLIENT_ID=... AGENT_USERNAME=... AGENT_PASSWORD=...
    python test_router_agent_latency.py [n_calls]
"""

import sys
import time
import urllib.parse
import uuid

import httpx

from test_router_agent_jwt import REGION, AGENT_RUNTIME_ARN, _get_bearer_token

QUESTION = "¿Qué sabemos de Acme Robotics Inc.?"


def main() -> None:
    n_calls = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    # AgentCore requires the session id to be at least 33 chars -- a UUID4 (36 chars) covers that.
    session_id = str(uuid.uuid4())
    print(f"session_id={session_id}\n")

    token = _get_bearer_token()
    encoded_arn = urllib.parse.quote(AGENT_RUNTIME_ARN, safe="")
    url = f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
    }

    durations = []
    for i in range(1, n_calls + 1):
        start = time.monotonic()
        httpx.post(url, headers=headers, json={"prompt": QUESTION}, timeout=120)
        elapsed = time.monotonic() - start
        durations.append(elapsed)
        print(f"call {i}: {elapsed:.2f}s")

    print("\nFirst call pays full cold cost (container start + token fetch + MCP handshake).")
    print("Calls after that should be faster if the caching in core.py is doing its job.")
    if len(durations) > 1:
        print(f"call 1: {durations[0]:.2f}s | calls 2..{n_calls} avg: {sum(durations[1:]) / len(durations[1:]):.2f}s")


if __name__ == "__main__":
    main()
