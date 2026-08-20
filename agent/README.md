# Cala/Sayari router agent

A [Strands Agents](https://strandsagents.com) agent that answers
company-intelligence questions by calling a2k-box's tools through the
AgentCore Gateway set up in `../deploy/agentcore/README.md` (step 4), and
Bedrock (Claude Sonnet) directly for its own reasoning. Deployed to its own
AgentCore Runtime (`a2k_agent-06B5R9CAuJ`, confirmed working end-to-end
2026-08-18) -- a separate Runtime workload from a2k-box, with its own
execution role and secrets. **Inbound auth is JWT/Cognito** (switched from
the IAM default on 2026-08-19, specifically to allow plain `curl`/Bearer-
token calls instead of requiring a SigV4-signed `boto3` call) -- see
"Calling the deployed agent" below.

**Routing**: no hardcoded rule -- `sources` is picked from each vendor's
actually-declared coverage (`domains`/`topics`/`coverage.scope`, plus
`status`/`priority`, from the KB Cards in `a2k/cards/*.json`, via
`a2k.listVendors`). **Not left to the model to decide when to look this up**:
confirmed live 2026-08-18 that it doesn't reliably call listVendors on its
own (see `test_routing_behavior.py`) -- `core.py`'s `ask()` fetches the
catalogue itself before building the Agent and injects it straight into the
system prompt, and `listVendors` is dropped from the tools the model even
sees. Only the *lookup* is deterministic; which `sourceId`(s) to pick is
still the model's judgment call -- exactly one if there's a clear single
match, several (or `sources` omitted entirely, fanning out to all active
vendors) if more than one plausibly matches or none clearly does. Inactive
(`status` != `active`) vendors are never selected. See `core.py`'s
`SYSTEM_PROMPT_TEMPLATE` and `_get_tools_and_catalogue`.

## Files in this directory

| File | What it is |
|---|---|
| `core.py` | The actual agent logic (system prompt, tool-name sanitization for Bedrock, token/MCP-connection caching) -- shared by both entrypoints below. |
| `router_agent.py` | Local CLI entrypoint. One question in, prints the answer (and Strands' tool-call trace) to stdout. |
| `entrypoint.py` | AgentCore Runtime entrypoint (`bedrock_agentcore` SDK) -- `POST /invocations` in, `{"response": "..."}` out. |
| `requirements.txt` | Deploy deps: `strands-agents`, `bedrock-agentcore`, `httpx` (`mcp`/`boto3` come in transitively). |
| `router-agent.zip` | Prebuilt deploy artifact (Linux arm64 wheels + `core.py`/`entrypoint.py`) -- see "Deploy to AgentCore Runtime" below for how to rebuild it. |
| `test_router_agent_jwt.py` | Invokes the *deployed* agent Runtime via a JWT Bearer token over raw HTTPS (this Runtime's inbound auth -- see "Calling the deployed agent" below). Replaces the old `test_router_agent_iam.py`, which stopped working once inbound auth moved off IAM. |
| `test_router_agent_latency.py` | Runs the deployed agent N times against one pinned session (`X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header), to see whether `core.py`'s caching is actually paying off across calls. |
| `test_tool_result_size.py` | Calls `a2k.ask` directly via MCP with each `sources` value, prints response byte/token size -- how the entity-hydration bug (see `../deploy/agentcore/README.md` section 5) was found. |
| `test_cala_raw_mode.py` | Calls `a2k.ask` directly via MCP (not through the LLM) and reports which response shape came back -- `content` (raw mode) vs the normal cited envelope -- to verify `CALA_RAW_KNOWLEDGE_SEARCH` independent of how the agent's own LLM might rephrase either shape. |
| `test_routing_behavior.py` | Runs the agent loop locally (needed for tool-call visibility -- see its own docstring) against three preset questions (Cala-leaning, Sayari-leaning, ambiguous) and reports the actual `sources` value passed to `ask` each time, plus the injected vendor catalogue. |
| `test_routing_behavior_deployed.py` | Same three questions against the *deployed* Runtime via a JWT Bearer token -- no tool-call trace available there, so it asks the agent to self-report which vendor(s) it used and prints the raw answers; a plausibility check, not the hard assertion the local version gives. |

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
`my-user-pool-278is5ma` -- see `../deploy/agentcore/test_gateway_mcp.py`'s
docstring if that gateway/pool is ever recreated and these need
re-deriving). `BEDROCK_MODEL_ID`
needs to be something your account actually has Bedrock access to in the
target region -- check **Bedrock console -> Model catalog**; cross-region
inference profiles are prefixed by region (e.g. `eu.anthropic....`).

Also needs normal AWS credentials in the environment (for `bedrock:InvokeModel*`)
-- whatever `boto3`'s default credential chain picks up.

**Don't confuse `CLIENT_ID`/`CLIENT_SECRET` above with `AGENT_CLIENT_ID`/
`AGENT_USERNAME`/`AGENT_PASSWORD`** used by the deployed-Runtime test
scripts (`test_router_agent_jwt.py` and friends) -- those two credential
pairs authenticate opposite legs of the pipeline. `CLIENT_ID`/`CLIENT_SECRET`
is this agent calling *out* to the Gateway (client-credentials grant, no
human involved). `AGENT_CLIENT_ID`/`AGENT_USERNAME`/`AGENT_PASSWORD` is
something calling *into* this agent's own Runtime (human-login grant,
Cognito pool created specifically for that -- see "Calling the deployed
agent" below). Neither is a Runtime environment variable; both are just
inputs to whatever script/curl you're using to talk to the respective
service.

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
server). **Inbound Auth**: the default (IAM/SigV4) works to get the Runtime
created -- confirmed live it's editable afterwards too, so switch it to
JWT/Cognito once created if you want plain-Bearer-token/`curl` access
instead of a SigV4-signed call; see "Calling the deployed agent" below for
that setup. Environment variables: see the table above. **The
auto-generated execution role does not include `bedrock:InvokeModel*`/
`Converse*` by default** -- confirmed live, target creation/first invoke
fails with `AccessDeniedException` until an inline
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

## Calling the deployed agent

**Inbound auth is JWT/Cognito, not IAM** (switched 2026-08-19, specifically
so `curl` works with a plain Bearer token instead of needing a SigV4-signed
`boto3` call -- an AgentCore Runtime supports exactly one inbound auth mode
at a time, so this Runtime can no longer be called via
`invoke_agent_runtime` at all).

This Runtime has its **own** Cognito pool -- separate from the Gateway's
`CLIENT_ID`/`CLIENT_SECRET` client-credentials pair (that one authenticates
the agent's *outbound* call to the Gateway; this one authenticates whoever
calls the agent itself, e.g. you, from a terminal). No such pool existed
before 2026-08-19; created fresh, same commands as
`../deploy/agentcore/README.md` step 1 (human-login,
`ALLOW_USER_PASSWORD_AUTH`, works from AWS CloudShell with no local AWS CLI
needed):

```bash
export REGION=eu-west-1
export USERNAME=agent-caller
export PASSWORD="ChangeThisPassword123!"

export AGENT_POOL_ID=$(aws cognito-idp create-user-pool \
  --pool-name "a2k-agent-pool" \
  --policies '{"PasswordPolicy":{"MinimumLength":8}}' \
  --region $REGION | jq -r '.UserPool.Id')

export AGENT_CLIENT_ID=$(aws cognito-idp create-user-pool-client \
  --user-pool-id $AGENT_POOL_ID \
  --client-name "a2k-agent-client" \
  --no-generate-secret \
  --explicit-auth-flows "ALLOW_USER_PASSWORD_AUTH" "ALLOW_REFRESH_TOKEN_AUTH" \
  --region $REGION | jq -r '.UserPoolClient.ClientId')

aws cognito-idp admin-create-user --user-pool-id $AGENT_POOL_ID --username $USERNAME \
  --region $REGION --message-action SUPPRESS > /dev/null
aws cognito-idp admin-set-user-password --user-pool-id $AGENT_POOL_ID --username $USERNAME \
  --password "$PASSWORD" --region $REGION --permanent > /dev/null

echo "Discovery URL: https://cognito-idp.$REGION.amazonaws.com/$AGENT_POOL_ID/.well-known/openid-configuration"
echo "Client ID: $AGENT_CLIENT_ID"
```

Then, on the Runtime's own console page (Agent Runtime -> `a2k_agent` ->
edit -> Inbound Auth -> switch IAM to JWT, paste the Discovery URL, add the
Client ID to allowed clients, save) -- confirmed editable post-creation,
2026-08-19.

**Plain curl:**

```bash
export TOKEN=$(aws cognito-idp initiate-auth \
  --client-id "$AGENT_CLIENT_ID" --auth-flow USER_PASSWORD_AUTH \
  --auth-parameters USERNAME=$USERNAME,PASSWORD="$PASSWORD" \
  --region $REGION | jq -r '.AuthenticationResult.AccessToken')

ARN="arn:aws:bedrock-agentcore:eu-west-1:396961015428:runtime/a2k_agent-06B5R9CAuJ"
ENCODED_ARN=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=''))" "$ARN")

curl -s -X POST \
  "https://bedrock-agentcore.eu-west-1.amazonaws.com/runtimes/${ENCODED_ARN}/invocations?qualifier=DEFAULT" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -d '{"prompt": "¿Qué sabemos de Acme Robotics Inc.?"}'
```

**Or the Python equivalent** (same auth, used by `test_router_agent_jwt.py`
and the other deployed-Runtime test scripts -- set
`AGENT_CLIENT_ID`/`AGENT_USERNAME`/`AGENT_PASSWORD` from the setup above):

```bash
export AGENT_CLIENT_ID="..."
export AGENT_USERNAME=agent-caller
export AGENT_PASSWORD="ChangeThisPassword123!"

python agent/test_router_agent_jwt.py "¿Qué sabemos de Acme Robotics Inc.?"
```

## Known behavior / gotchas

- **Tool name sanitization**: Bedrock's Converse API only allows
  `[a-zA-Z0-9_-]+` in tool names, but the Gateway exposes a2k-box's tools as
  `<target-name>___a2k.ask` (dot included) -- `core.py` renames dots to
  underscores for the model, while still calling the MCP server by its real
  name underneath. See `core.py`'s `_get_tools_and_catalogue`. **Match tool
  names by suffix, not equality**, when looking one up by its bare a2k-box
  name (e.g. finding `listVendors` to call it directly) -- the Gateway
  prefix means an exact match against `"a2k.listVendors"` never matches
  `"<target>___a2k.listVendors"` (confirmed live 2026-08-18, a real bug that
  shipped once before being caught).
- **`structuredContent` isn't preserved through the Gateway**: calling an
  MCP tool straight against a2k-box's own Runtime returns structured JSON
  results in `structuredContent` (confirmed via
  `../deploy/agentcore/test_remote_mcp_iam.py`), but the *same* tool called
  through the Gateway only returns `content[0]["text"]` (the JSON encoded as
  a string) -- confirmed live 2026-08-18. `core.py`'s
  `_fetch_vendor_catalogue_text` tries `structuredContent` first and falls
  back to `json.loads(content[0]["text"])`; any other code calling MCP
  tools directly (not through the Strands tool-use loop, which already
  handles this) needs the same fallback.
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
  Cala's raw `content`. `core.py`'s `SYSTEM_PROMPT_TEMPLATE` tells the model to
  reproduce that verbatim rather than paraphrase it -- but that's a prompt
  instruction, not a hard guarantee; an LLM can still alter it. If a
  downstream consumer needs Cala's prose byte-for-byte, don't route it
  through this agent at all -- call `a2k.ask` directly via MCP instead (see
  `test_cala_raw_mode.py`).
