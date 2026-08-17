# Prototype agent: Cala/Sayari router over the AgentCore Gateway

`router_agent.py` is a **local-only prototype**, not yet deployed to
AgentCore Runtime. It's a [Strands Agents](https://strandsagents.com) agent
that connects to the AgentCore Gateway set up in
`../deploy/agentcore/README.md` (step 4), discovers a2k-box's 7 tools
through it, and answers a question by calling `a2k.ask` -- steering it
towards Cala or Sayari via the tool's own `sources` param, based on a
routing rule in the system prompt (national -> Cala, international ->
Sayari, ambiguous -> both). See the module docstring in `router_agent.py`
for the full reasoning; this is a placeholder rule meant to be replaced
once there's real usage data to base routing on.

## Setup

`strands-agents` was installed into this repo's `.venv` (python3.11 --
**not** `.venv/bin/python`, which is symlinked to python3.13 in this repo's
venv and won't see it):

```bash
.venv/bin/python3.11 -m pip install strands-agents   # already done as of 2026-08-17
```

## Environment variables

| Var | Where it comes from |
|---|---|
| `CLIENT_ID` / `CLIENT_SECRET` | The Gateway's inbound-auth Cognito app client (same one used for `../deploy/agentcore/test_gateway_mcp.py`) |
| `GATEWAY_URL` | The Gateway's MCP endpoint, e.g. `https://gateway-mcp-sayari-cala-asbehc9rcm.gateway.bedrock-agentcore.eu-west-1.amazonaws.com/mcp` |
| `BEDROCK_MODEL_ID` | A model ID/inference-profile ARN your account has Bedrock access to in `eu-west-1` (or set `AWS_REGION` to match wherever you do). Not defaulted here -- check **Bedrock console -> Model access** for what's actually enabled on this account before picking one. |

Also needs normal AWS credentials in the environment (for the `bedrock:InvokeModel*`
call) -- whatever `boto3`'s default credential chain picks up (SSO profile, env vars, etc).

## Run

```bash
export CLIENT_ID="..."
export CLIENT_SECRET="..."
export GATEWAY_URL="https://gateway-mcp-sayari-cala-asbehc9rcm.gateway.bedrock-agentcore.eu-west-1.amazonaws.com/mcp"
export BEDROCK_MODEL_ID="..."

.venv/bin/python3.11 agent/router_agent.py "¿Qué sabemos de Repsol?"
.venv/bin/python3.11 agent/router_agent.py "What's the ownership structure behind Sinopec's international subsidiaries?"
```

The first should route to Cala (national/domestic), the second to Sayari
(international/ownership) -- watch the tool-call names Strands prints to
confirm which one the model actually picked.

## Next steps (not done yet)

- Deploy this to AgentCore Runtime, same pattern as `a2k-box` itself
  (`../deploy/agentcore/README.md` sections 2-3), once the routing logic is
  validated locally.
- Replace the national/international placeholder rule with whatever routing
  logic actually reflects how Cala/Sayari coverage differs in practice.
