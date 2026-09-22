# A2K frontend

A small FastAPI app that lets a human ask a question through a web form and see the full
`CitedResponseEnvelope` a2k-box's **already-deployed** MCP Runtime returns -- answer, claims,
citations, `groundedRatio`, freshness (including whether the data is stale), conflicts between
sources, and the audit trail (including whether the answer was served from the semantic answer
cache, see `a2k/gateway/cache.py`). It talks directly to that Runtime over MCP
(`boto3.invoke_agent_runtime`) -- it does not run a2k-box itself, and it is not a2k-box's own
REST transport (`a2k/api/rest.py`) either. Nothing here changes a2k-box or the agent; this is
purely a consumer of what's already running.

**Scope, on purpose**: this runs locally only, for now. Deploying it to EKS is a deliberate
later step (see "EKS deployment" below) -- don't add Kubernetes manifests here until that's
actually decided.

## Why there's a backend at all, not just a static page

a2k-box's MCP Runtime uses IAM/SigV4 inbound auth (every script in `../deploy/agentcore/`
confirms this -- they all call `boto3.client("bedrock-agentcore").invoke_agent_runtime`, never a
bearer token). A browser can't sign SigV4 requests without exposing AWS credentials
client-side, so this small FastAPI app holds those credentials server-side and exposes a plain
JSON API (`/api/ask`, `/api/search`) the browser's own JS calls instead.

## Run locally

```bash
cd frontend
pip install -r requirements.txt
```

Needs AWS credentials in the environment able to call `bedrock-agentcore:InvokeAgentRuntime` on
a2k-box's Runtime -- the same credentials any script in `../deploy/agentcore/` already needs
(your own AWS CLI/SSO session; nothing new to set up if you've already run those scripts).

```bash
uvicorn app:app --reload --port 8000
```

Open `http://localhost:8000`.

## Configuration

| Var | Default | What it's for |
|---|---|---|
| `A2K_BOX_RUNTIME_ARN` | the current a2k-box MCP Runtime ARN (`a2k_external_data_mcp-...`) | Which Runtime to call -- override if a2k-box is ever redeployed under a different Runtime. |
| `AWS_REGION` | `eu-west-1` | Region for both the Bedrock AgentCore client and the Runtime ARN above. |

See `a2k_client.py`'s own top for the exact defaults.

## How a request flows

1. Browser submits the form -> `app.js` `fetch()`s `POST /api/ask` (or `/api/search`) with
   `{"query": ..., "sources": [...]}` (`sources` empty = let the agent decide, same as omitting
   it on a raw MCP call).
2. `app.py` opens a **fresh MCP session** (`a2k_client.new_session()`) and calls the tool
   (`a2k_client.call_ask()`/`call_search()`) -- one new session per request, not pooled/reused
   across requests; see the plan this was built from for why (each web request is independent
   and short-lived, and session reuse across concurrent users would need isolation this doesn't
   have yet).
3. The parsed `CitedResponseEnvelope` JSON goes straight back to the browser -- `app.py` doesn't
   reshape it, `app.js` renders it directly by the field names `a2k/models/envelope.py` already
   defines.

A request can take anywhere from a few seconds (cache hit) to roughly a minute (a live vendor
lookup through the agent) -- the UI shows a loading state for this, it's expected, not a bug.

## EKS deployment (not done here -- for when that's actually next)

This is a single FastAPI process serving both the API and the static/template files -- one
container is enough, no split frontend/backend services needed. When that step comes, mirror the
root `Dockerfile` + `deploy/*.yaml` pattern already proven for a2k-box itself: a `Deployment`
(one or two replicas) + a `Service`, pointed at an image built from this directory instead of
`a2k/`. Given this is an internal/test tool, not a production surface, skip
`hpa.yaml`/`configmap.yaml`-style extras unless a real need for them shows up -- don't build
that ahead of time.

The one thing that *does* carry over from local dev to EKS: however the pod's IAM identity is
set up (IRSA), it needs the same `bedrock-agentcore:InvokeAgentRuntime` permission on a2k-box's
Runtime that your local AWS credentials already need above -- no code change, `boto3`'s default
credential chain picks either up the same way.
