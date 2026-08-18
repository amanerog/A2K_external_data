# Cala/Sayari router agent

A [Strands Agents](https://strandsagents.com) agent that answers
company-intelligence questions by calling a2k-box's tools through the
AgentCore Gateway set up in `../deploy/agentcore/README.md` (step 4), and
Bedrock (Claude Sonnet) directly for its own reasoning. Deployed to its own
AgentCore Runtime (`a2k_agent-06B5R9CAuJ`, confirmed working end-to-end
2026-08-18) -- a separate Runtime workload from a2k-box, with its own
execution role, inbound auth (IAM, not Cognito), and secrets.

**Routing**: no hardcoded rule. The agent calls `a2k.listVendors` before its
first `ask`/`search` call to read each vendor's actually-declared coverage
(`domains`/`topics`/`coverage.scope`, plus `status`/`priority`, from the KB
Cards in `a2k/cards/*.json`) and picks the `ask` tool's `sources` param based
on that -- exactly one `sourceId` if there's a clear single match, several
(or `sources` omitted entirely, fanning out to all active vendors) if more
than one plausibly matches or none clearly does. Inactive (`status` !=
`active`) vendors are never selected. See `core.py`'s `SYSTEM_PROMPT`.

## Files in this directory

| File | What it is |
|---|---|
| `core.py` | The actual agent logic (system prompt, tool-name sanitization for Bedrock, token/MCP-connection caching) -- shared by both entrypoints below. |
| `router_agent.py` | Local CLI entrypoint. One question in, prints the answer (and Strands' tool-call trace) to stdout. |
| `entrypoint.py` | AgentCore Runtime entrypoint (`bedrock_agentcore` SDK) -- `POST /invocations` in, `{"response": "..."}` out. |
| `requirements.txt` | Deploy deps: `strands-agents`, `bedrock-agentcore`, `httpx` (`mcp`/`boto3` come in transitively). |
| `router-agent.zip` | Prebuilt deploy artifact (Linux arm64 wheels + `core.py`/`entrypoint.py`) -- see "Deploy to AgentCore Runtime" below for how to rebuild it. |
| `test_gateway_mcp.py` | Raw MCP probe against the Gateway (Cognito token + `initialize`/`tools/list`) -- no agent/LLM involved, just confirms the Gateway->a2k-box path works. |
| `test_router_agent_iam.py` | Invokes the *deployed* agent Runtime via `boto3`'s `invoke_agent_runtime` (IAM/SigV4, this Runtime's inbound auth). |
| `test_router_agent_latency.py` | Runs the deployed agent N times against one pinned `runtimeSessionId`, to see whether `core.py`'s caching is actually paying off across calls. |
| `test_tool_result_size.py` | Calls `a2k.ask` directly via MCP with each `sources` value, prints response byte/token size -- how the entity-hydration bug (see `../deploy/agentcore/README.md` section 5) was found. |
| `test_cala_raw_mode.py` | Calls `a2k.ask` directly via MCP (not through the LLM) and reports which response shape came back -- `content` (raw mode) vs the normal cited envelope -- to verify `CALA_RAW_KNOWLEDGE_SEARCH` independent of how the agent's own LLM might rephrase either shape. |

## Setup

`strands-agents`, `bedrock-agentcore`, and `boto3` are installed in this
repo's shared `.venv` (python3.11 -- `.venv/bin/python`/`python3` were
re-symlinked from python3.13 to python3.11 to match, so plain `python`
inside an activated venv works fine now).

## Environment variables

| Var | Local (`router_agent.py`) | Runtime (`entrypoint.py`) |
|---|---|---|
| `GATEWAY_URL` | required | required (plain env var, not secret) |
| `BEDROCK_MODEL_ID` | required | required (plain env var, not secret) |
| `CLIENT_ID` / `CLIENT_SECRET` | required, plain env vars | prefer `AGENT_SECRETS_ARN` instead (see below) |
| `AGENT_SECRETS_ARN` | not used | ARN of a Secrets Manager secret `{"CLIENT_ID": "...", "CLIENT_SECRET": "..."}` -- Runtime env vars are visible to anyone with read access to the Runtime resource, unlike a Secrets Manager value gated by its own IAM policy. `core.py`'s `secret_env()` checks the plain env var first, so this is only consulted when `CLIENT_ID`/`CLIENT_SECRET` are genuinely unset. |
| `AWS_REGION` | optional, defaults `eu-west-1` | same |

`CLIENT_ID`/`CLIENT_SECRET` come from the Gateway's inbound-auth Cognito app
client (the `gateway-mcp-sayari-cala` resource server on pool
`my-user-pool-278is5ma` -- see `test_gateway_mcp.py`'s docstring if that
gateway/pool is ever recreated and these need re-deriving). `BEDROCK_MODEL_ID`
needs to be something your account actually has Bedrock access to in the
target region -- check **Bedrock console -> Model catalog**; cross-region
inference profiles are prefixed by region (e.g. `eu.anthropic....`).

Also needs normal AWS credentials in the environment (for `bedrock:InvokeModel*`)
-- whatever `boto3`'s default credential chain picks up.

## Run locally

```bash
export CLIENT_ID="..."
export CLIENT_SECRET="..."
export GATEWAY_URL="https://gateway-mcp-sayari-cala-asbehc9rcm.gateway.bedrock-agentcore.eu-west-1.amazonaws.com/mcp"
export BEDROCK_MODEL_ID="eu.anthropic.claude-sonnet-4-5-20250929-v1:0"

python agent/router_agent.py "¿Qué sabemos de Acme Robotics Inc.?"
```

(`Acme Robotics Inc.`/`Meridian Textiles Ltd`/`Nordic Cold Chain AS` are the
only entities in a2k-box's mock fixtures -- use a real company name if
a2k-box is running in `live` mode.)

## Deploy to AgentCore Runtime

Same build pattern as a2k-box's own deploy (`../deploy/agentcore/README.md`
section 2, Option B) -- Linux arm64 wheels, no npm needed:

```bash
mkdir /tmp/router-agent-build && cd /tmp/router-agent-build
pip install --platform manylinux2014_aarch64 --python-version 3.13 \
  --implementation cp --only-binary=:all: --target . \
  -r /path/to/repo/agent/requirements.txt
cp /path/to/repo/agent/entrypoint.py /path/to/repo/agent/core.py .
find . -type d -name "__pycache__" -exec rm -rf {} +
zip -r ../router-agent.zip .
```

Console: **AgentCore -> Agent Runtime -> Host Agent -> Local Upload** ->
`router-agent.zip`. **Runtime version** Python 3.13, **Entry point**
`entrypoint.py`, **Protocol** HTTP (not MCP -- this is an agent, not an MCP
server). **Inbound Auth**: leave the default (IAM/SigV4) -- no Cognito setup
needed for this Runtime; consume it with `boto3`'s `invoke_agent_runtime`
like `test_router_agent_iam.py` does. Environment variables: see the table
above. **The auto-generated execution role does not include
`bedrock:InvokeModel*`/`Converse*` by default** -- confirmed live, target
creation/first invoke fails with `AccessDeniedException` until an inline
policy is added by hand:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "InvokeModel",
    "Effect": "Allow",
    "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:Converse", "bedrock:ConverseStream"],
    "Resource": [
      "arn:aws:bedrock:eu-west-1:<account-id>:inference-profile/<your BEDROCK_MODEL_ID>",
      "arn:aws:bedrock:*::foundation-model/<the underlying model id, no region/account prefix>"
    ]
  }]
}
```

The second `Resource` entry matters for cross-region inference profiles:
routing can land the actual `ConverseStream` call in a *different* region
than the Gateway/Runtime (confirmed live -- an `eu.` profile routed to
`eu-north-1` while everything else here runs in `eu-west-1`), so the
foundation-model ARN needs a region wildcard, not the Runtime's own region.
If your organization has data-residency constraints, don't just widen this
to `*` without checking with whoever owns that policy first.

Test it:

```bash
python agent/test_router_agent_iam.py "¿Qué sabemos de Acme Robotics Inc.?"
```

## Known behavior / gotchas

- **Tool name sanitization**: Bedrock's Converse API only allows
  `[a-zA-Z0-9_-]+` in tool names, but the Gateway exposes a2k-box's tools as
  `<target-name>___a2k.ask` (dot included) -- `core.py` renames dots to
  underscores for the model, while still calling the MCP server by its real
  name underneath. See `core.py`'s `_get_tools`.
- **Latency**: `core.py` caches the Cognito bearer token (until
  `expires_in`) and the Gateway MCP connection + tool list at module scope,
  since AgentCore Runtime keeps a container warm across invocations within a
  session -- see `core.py`'s module docstring "Latency" and
  `test_router_agent_latency.py`. A much bigger latency win came from fixing
  an entity-hydration bug on a2k-box's side (`../deploy/agentcore/README.md`
  section 5) -- the caching alone only shaved a few seconds; that fix cut
  the per-call floor roughly in half.
- **`CALA_RAW_KNOWLEDGE_SEARCH` (a2k-box env var, not this agent's)**: when
  set, `a2k.ask`'s response shape changes from the normal cited envelope to
  Cala's raw `content`. `core.py`'s `SYSTEM_PROMPT` tells the model to
  reproduce that verbatim rather than paraphrase it -- but that's a prompt
  instruction, not a hard guarantee; an LLM can still alter it. If a
  downstream consumer needs Cala's prose byte-for-byte, don't route it
  through this agent at all -- call `a2k.ask` directly via MCP instead (see
  `test_cala_raw_mode.py`).
