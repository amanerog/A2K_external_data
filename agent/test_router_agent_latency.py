"""Measures whether core.py's token/MCP-connection caching (see its module
docstring "Latency") actually pays off across calls -- runs the same
question N times against the deployed router agent, pinning all calls to
the *same* runtimeSessionId so they land on the same warm AgentCore Runtime
container (otherwise AWS may route each call to a fresh microVM with its
own empty cache, and this would measure nothing).

Usage:
    python test_router_agent_latency.py [n_calls]
"""

import json
import sys
import time
import uuid

import boto3

REGION = "eu-west-1"
AGENT_RUNTIME_ARN = "arn:aws:bedrock-agentcore:eu-west-1:396961015428:runtime/a2k_agent-06B5R9CAuJ"
QUESTION = "¿Qué sabemos de Acme Robotics Inc.?"


def main() -> None:
    n_calls = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    # AgentCore requires runtimeSessionId to be at least 33 chars -- a UUID4 (36 chars) covers that.
    session_id = str(uuid.uuid4())
    print(f"runtimeSessionId={session_id}\n")

    client = boto3.client("bedrock-agentcore", region_name=REGION)
    durations = []

    for i in range(1, n_calls + 1):
        start = time.monotonic()
        response = client.invoke_agent_runtime(
            agentRuntimeArn=AGENT_RUNTIME_ARN,
            qualifier="DEFAULT",
            runtimeSessionId=session_id,
            payload=json.dumps({"prompt": QUESTION}).encode("utf-8"),
            contentType="application/json",
            accept="application/json",
        )
        response["response"].read()  # drain the body; content itself isn't what we're measuring here
        elapsed = time.monotonic() - start
        durations.append(elapsed)
        print(f"call {i}: {elapsed:.2f}s")

    print("\nFirst call pays full cold cost (container start + token fetch + MCP handshake).")
    print("Calls after that should be faster if the caching in core.py is doing its job.")
    if len(durations) > 1:
        print(f"call 1: {durations[0]:.2f}s | calls 2..{n_calls} avg: {sum(durations[1:]) / len(durations[1:]):.2f}s")


if __name__ == "__main__":
    main()
