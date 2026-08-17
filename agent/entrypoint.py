"""AgentCore Runtime entrypoint for the Cala/Sayari router agent.

Copy this file (and core.py, requirements.txt in this same directory) into
the project folder that `agentcore create --protocol HTTP` scaffolds (or
into the zip for a direct-code deploy). Point agentcore.json's `entrypoint`
field at this file. See README.md "Deploy to AgentCore Runtime" in this
directory.

Unlike a2k-box's MCP entrypoint (../deploy/agentcore/entrypoint.py), this
uses the `bedrock_agentcore` SDK's BedrockAgentCoreApp, which implements
the /invocations (POST) and /ping (GET) contract AgentCore Runtime expects
for HTTP-protocol workloads -- no manual routing needed here.

Needs these environment variables set on the Runtime (see README):
    GATEWAY_URL, CLIENT_ID, CLIENT_SECRET, BEDROCK_MODEL_ID
Plus a `bedrock:InvokeModel*`/`bedrock:Converse*` permission on that model
for the Runtime's own execution role -- this agent calls Bedrock directly
(unlike a2k-box, which is purely a tool server with no model of its own).

Invocation payload contract: POST /invocations with {"prompt": "<question>"},
returns {"response": "<answer text>"}.
"""

from __future__ import annotations

import os

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from core import ask

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload: dict) -> dict:
    question = payload.get("prompt", "")
    answer = ask(
        question,
        gateway_url=os.environ["GATEWAY_URL"],
        client_id=os.environ["CLIENT_ID"],
        client_secret=os.environ["CLIENT_SECRET"],
        model_id=os.environ["BEDROCK_MODEL_ID"],
        region=os.environ.get("AWS_REGION", "eu-west-1"),
        silent=True,
    )
    return {"response": answer}


if __name__ == "__main__":
    app.run()
