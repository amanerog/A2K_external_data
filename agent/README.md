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
| `observability.py` | Structured logs (one JSON line per tool call) + CloudWatch EMF metrics (one per `ask()` call), both printed to stdout -- see "Observability" below. |
| `router_agent.py` | Local CLI entrypoint. One question in, prints the answer (and Strands' tool-call trace) to stdout. |
| `entrypoint.py` | AgentCore Runtime entrypoint (`bedrock_agentcore` SDK) -- `POST /invocations` in, `{"response": "..."}` out. |
| `requirements.txt` | Deploy deps: `strands-agents`, `bedrock-agentcore`, `httpx` (`mcp`/`boto3` come in transitively). |
| `router-agent.zip` | Prebuilt deploy artifact (Linux arm64 wheels + `core.py`/`entrypoint.py`) -- see "Deploy to AgentCore Runtime" below for how to rebuild it. |
| `test_router_agent_jwt.py` | Invokes the *deployed* agent Runtime via a JWT Bearer token over raw HTTPS (this Runtime's inbound auth -- see "Calling the deployed agent" below). Replaces the old `test_router_agent_iam.py`, which stopped working once inbound auth moved off IAM. |
| `test_router_agent_latency.py` | Runs the deployed agent N times against one pinned session (`X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header), to see whether `core.py`'s caching is actually paying off across calls. |
| `test_tool_result_size.py` | Calls `a2k.ask` directly via MCP with each `sources` value, prints response byte/token size -- how the entity-hydration bug (see `../deploy/agentcore/README.md` section 5) was found. |
| `test_sayari_probe.py` | Sayari probe, three modes: free-text query (`search_entities`+`get_entity_summary` direct / `a2k.search`+`a2k.ask` via Gateway), `--entity-id <id>` (skips the name search, calls `get_entity_summary`/`a2k.getDocument` directly), `--schema` (dumps every Sayari MCP tool's real `inputSchema`, straight from Sayari, no a2k-box involved -- for checking what parameters a tool actually supports rather than guessing from what the adapter happens to use). Needs a *separate* credential pair for the direct-to-Sayari parts (`AUTH0_CLIENT_ID`/`AUTH0_CLIENT_SECRET`) from the Gateway parts (`CLIENT_ID`/`CLIENT_SECRET`) -- see the script's docstring. |
| `test_cala_raw_mode.py` | Calls `a2k.ask` directly via MCP (not through the LLM) and reports which response shape came back -- `content` (raw mode) vs the normal cited envelope -- to verify `CALA_RAW_KNOWLEDGE_SEARCH` independent of how the agent's own LLM might rephrase either shape. |
| `test_routing_behavior.py` | Runs the agent loop locally (needed for tool-call visibility -- see its own docstring) against three preset questions (Cala-leaning, Sayari-leaning, ambiguous) and reports the actual `sources` value passed to `ask` each time, plus the injected vendor catalogue. |
| `test_routing_behavior_deployed.py` | Same three questions against the *deployed* Runtime via a JWT Bearer token -- no tool-call trace available there, so it asks the agent to self-report which vendor(s) it used and prints the raw answers; a plausibility check, not the hard assertion the local version gives. |
| `direct_agent.py` | The `mcp_to_agent_to_mcp` branch's two-phase logic (vendor decision, then a live vendor-MCP call) -- used only by `entrypoint_a2k.py` below, not by `core.py`'s Gateway-mediated path. See "The direct-discovery agent" below. |
| `vendor_mcp_client.py` | Direct connection helpers for Cala's/Sayari's own MCP servers (bypasses a2k-box/the Gateway entirely) -- used by `direct_agent.py`. |
| `entrypoint_a2k.py` | AgentCore Runtime entrypoint for the direct-discovery path -- called by a2k-box's `gateway/engine.py`, not by a human/curl caller. See "The direct-discovery agent" below. |
| `router-agent-direct-v2.zip` | Prebuilt deploy artifact for `entrypoint_a2k.py` -- see "The direct-discovery agent" below for how to rebuild it (and a gotcha worth reading before you do). |

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
cp /path/to/repo/agent/entrypoint.py /path/to/repo/agent/core.py \
  /path/to/repo/agent/observability.py .
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

## The direct-discovery agent (`entrypoint_a2k.py`, `mcp_to_agent_to_mcp` branch)

A second, additive entrypoint in this same directory -- **not** called by a
human/curl the way `entrypoint.py` above is. Its only caller is a2k-box's
own `gateway/engine.py` (`_call_agent()`), invoked in place of the
deterministic `adapters/cala_mcp.py`/`adapters/sayari_mcp.py` +
`gateway/synthesis.py` path. See `direct_agent.py`'s own module docstring
for the full two-phase design: a cheap tool-less vendor decision first,
then a live MCP connection straight to whichever vendor(s) were chosen --
Cala's/Sayari's own MCP servers, bypassing a2k-box's adapters and the
Gateway entirely (`vendor_mcp_client.py`).

**Files this entrypoint needs in its zip** -- narrower than `entrypoint.py`
above, but with one easy-to-miss addition:

| File | Why |
|---|---|
| `entrypoint_a2k.py` | The entry point itself. |
| `direct_agent.py` | The two-phase vendor-decision/vendor-call logic. |
| `vendor_mcp_client.py` | Direct Cala/Sayari MCP connection helpers. |
| `observability.py` | Same tool-call logs/EMF metrics as `entrypoint.py`'s path (see "Observability" above) -- `direct_agent.py`'s `handle()` wires `session_id` from a2k-box's own `requestId` (threaded through `engine.py`'s `_call_agent()`) and a hardcoded `internal_client="a2k-box"` (this entrypoint has exactly one legitimate caller, unlike `entrypoint.py`/`entrypoint_v3.py` which read it from whoever's actually calling them). |
| `core.py` | **Easy to forget -- it isn't the entry point and this path never touches the Gateway** -- but `vendor_mcp_client.py` still imports `secret_env` from it (see that file's own docstring, which already said so before this got missed once). Omitting it crashes the container at import time, before it ever binds a port -- AgentCore's proxy then returns a bare `424 Failed Dependency` on *every* invoke, with no traceback visible anywhere obvious (confirmed live 2026-09-07; the endpoint still reports **READY**, so don't rule this out just because the console says the Runtime is healthy -- READY only means the control plane accepted the deploy, not that the container is actually serving). If you hit a 424 here after a redeploy, check this before anything else. |

Build the same way as `entrypoint.py`'s zip (`requirements.txt` is shared,
no new packages needed):

```bash
mkdir /tmp/router-agent-direct-build && cd /tmp/router-agent-direct-build
pip install --platform manylinux2014_aarch64 --python-version 3.13 \
  --implementation cp --only-binary=:all: --target . \
  -r /path/to/repo/agent/requirements.txt
cp /path/to/repo/agent/entrypoint_a2k.py /path/to/repo/agent/direct_agent.py \
  /path/to/repo/agent/vendor_mcp_client.py /path/to/repo/agent/core.py \
  /path/to/repo/agent/observability.py .
find . -type d -name "__pycache__" -exec rm -rf {} +
zip -r ../router-agent-direct.zip .
```

Console: **Local Upload** -> the zip above, **Entry point**
`entrypoint_a2k.py`, **Protocol** HTTP (same as `entrypoint.py` -- this is
an agent, not an MCP server). Deployed live as its own Runtime, separate
from `a2k_agent-06B5R9CAuJ` above:

```
arn:aws:bedrock-agentcore:eu-west-1:<ACCOUNT_ID>:runtime/hosted_router_agent-cQvidi4ixE
```

**Inbound auth is JWT/Cognito**, same `a2k-agent-pool` as `entrypoint.py`'s
Runtime, but a *different* app client -- a machine-to-machine
(client-credentials) client, not the human-login one "Calling the deployed
agent" below sets up, since the only caller here is a2k-box itself, not a
person with a terminal. a2k-box's own `a2k/config.py` holds this side's
credentials (`agent_pool_client_id`/`agent_pool_client_secret`/
`agent_pool_token_url`/`agent_pool_scope` -- `AGENT_POOL_*` env vars) and
`gateway/engine.py`'s `_get_agent_token()` fetches the token the same way
`core.py`'s `get_bearer_token()` fetches the Gateway one.

**Environment variables** -- `BEDROCK_MODEL_ID`/`AWS_REGION` same as the
table above, plus the vendor credentials `vendor_mcp_client.py` reads via
`secret_env()` (plain env var, or the `AGENT_SECRETS_ARN` Secrets Manager
bundle -- same fallback `CLIENT_ID`/`CLIENT_SECRET` use):

| Var | Used by |
|---|---|
| `CALA_API_KEY` | `connect_cala()` -- `X-API-KEY` header, same credential a2k-box's own `adapters/cala_mcp.py` uses. |
| `AUTH0_CLIENT_ID` / `AUTH0_CLIENT_SECRET` | `connect_sayari()` -- Auth0 client-credentials grant against `sayari.auth0.com`, same credential a2k-box's `adapters/sayari_mcp.py` uses (a *separate* Auth0 grant from Sayari's REST API -- see that adapter's own docstring). |

**Testing it**: no human-facing curl flow for this Runtime (see "Calling
the deployed agent" below for why that exists for `entrypoint.py` but not
here). Test the whole chain through a2k-box's own MCP instead --
`../deploy/agentcore/test_a2k_ask_via_agent.py` calls a2k-box's `a2k.ask`,
which calls this Runtime underneath.

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

ARN="arn:aws:bedrock-agentcore:eu-west-1:<ACCOUNT_ID>:runtime/a2k_agent-06B5R9CAuJ"
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

## Observability

`observability.py` prints two kinds of JSON line to stdout on every `ask()`
call -- no extra plumbing needed, same convention a2k-box uses
(`gateway/audit.py`, `gateway/tracing.py`): CloudWatch Logs picks up
anything a Runtime container writes to stdout automatically.

**Tool-call logs** (`event: "agent.tool_call"`, one per tool the model
actually calls -- normally just the one `a2k.ask` call):

| Field | Where it comes from |
|---|---|
| `agent_timestamp` | Same as `tool_end_time` below. |
| `session_id` | Passed into `ask()` -- from `context.session_id` (AgentCore Runtime's own session header) when deployed, a fresh `uuid4()` per run from the local CLI. |
| `chosen_vendor` | The `sources` argument the model passed to `a2k.ask`, straight from Strands' `BeforeToolCallEvent.tool_use["input"]`. `null` means either fan-out-to-all-vendors (`sources` omitted, a valid routing outcome -- see "Routing" above) or a non-`ask` tool call. |
| `tool_start_time` / `tool_end_time` | Wall-clock timestamps bracketing the tool call, from Strands' `BeforeToolCallEvent`/`AfterToolCallEvent` hooks. |
| `tool_http_status` | **Approximated** -- MCP tool results don't carry a real HTTP status. 200 on `ToolResult.status == "success"`, otherwise the real code from an `httpx.HTTPStatusError` if that's the underlying exception, else 502. See `observability.py`'s module docstring. |
| `error_type` | The exception's class name, `"ToolError"` for a non-exception tool failure (`ToolResult.status == "error"`), or `null`. |
| `internal_client` | From the `X-Internal-Client` request header when deployed (case-insensitive lookup, falls back to `"unknown"` if the caller doesn't set it); `"local-cli"` from `router_agent.py`. |

**Query metrics** (CloudWatch EMF, one line per `ask()` call, namespace
`A2K/RouterAgent` by default -- override with `A2K_AGENT_METRICS_NAMESPACE`):
`QueryCount`, `ErrorCount`, `Latency` (ms), `InvocationCount` (tool calls
made during this query), `ClientCount` -- all dimensioned by
`internal_client`. EMF means CloudWatch auto-extracts these from the log
line itself; no `cloudwatch:PutMetricData` call on the request path, and no
extra IAM permission beyond the `logs:*` the execution role already needs
(see `../deploy/agentcore/README.md`'s IAM troubleshooting section).
`ClientCount` is 1-per-query, not a running distinct-client gauge -- SUM/
group-by `internal_client` for volume per client, or run CloudWatch
Contributor Insights against the tool-call logs' `internal_client` field
for a true distinct count.

Callers that want to show up as anything other than `"unknown"` in these
logs/metrics need to set the `X-Internal-Client` header on their
`InvokeAgentRuntime`/HTTPS call -- there's no default identity for "which
internal system is calling us" the way there is for `session_id`.

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
