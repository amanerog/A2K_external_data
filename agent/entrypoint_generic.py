"""AgentCore Runtime entrypoint for the "generic model" experiment -- same
invocation contract as entrypoint.py, but calls `core.ask(generic=True)`
instead of the normal path.

Why this is a *separate* file/Runtime rather than an env var on the real
deploy: it's meant to answer a specific question ("does a2k-box's own MCP
contract hold up without any of core.py's prompt engineering propping it
up?", see the "expose the MCP externally" discussion) by comparing this
Runtime's behavior against the real one side by side -- mixing the two into
one toggle-able deployment would make that comparison harder, not easier,
and risks the generic path accidentally being used for real traffic if the
toggle is ever left on by mistake.

`generic=True` (see core.py's `ask()`/`_get_tools_and_catalogue()`):
    - Swaps SYSTEM_PROMPT_TEMPLATE for GENERIC_SYSTEM_PROMPT (a few generic
      sentences, none of the vendor-routing/query-shape guidance the real
      prompt carries).
    - Does not pre-fetch/inject the vendor catalogue.
    - Does not drop a2k.listVendors from the tools handed to the model -- the
      model has to discover and decide to call it on its own, relying only on
      a2k-box's own `instructions`/tool docstrings (a2k/mcp_server/server.py)
      to know it should.

Everything else (auth, Secrets Manager fallback, observability wiring,
invocation payload contract) is identical to entrypoint.py -- see that
file's docstring for the details not repeated here.
"""

from __future__ import annotations

import os

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.runtime.context import RequestContext

from core import ask, secret_env

app = BedrockAgentCoreApp()

_INTERNAL_CLIENT_HEADER = "x-internal-client"


@app.entrypoint
def invoke(payload: dict, context: RequestContext) -> dict:
    question = payload.get("prompt", "")
    headers = context.request_headers or {}
    internal_client = next(
        (value for key, value in headers.items() if key.lower() == _INTERNAL_CLIENT_HEADER), "unknown"
    )
    answer = ask(
        question,
        gateway_url=os.environ["GATEWAY_URL"],
        client_id=secret_env("CLIENT_ID"),
        client_secret=secret_env("CLIENT_SECRET"),
        model_id=os.environ["BEDROCK_MODEL_ID"],
        region=os.environ.get("AWS_REGION", "eu-west-1"),
        silent=True,
        session_id=context.session_id,
        internal_client=internal_client,
        generic=True,
    )
    return {"response": answer}


if __name__ == "__main__":
    app.run()
