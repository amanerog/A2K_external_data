# Deploy a2k-box's MCP server to Amazon Bedrock AgentCore Runtime

This is the detailed runbook behind the "Deploy to AgentCore" section of the
top-level `README.md`. It deploys **only the MCP transport** of a2k-box
(`a2k/mcp_server/server.py`, streamable-http) as its own AgentCore Runtime
workload, so it can be registered as an MCP server target on an AgentCore
Gateway and consumed by an agent that itself runs as a separate Runtime
workload. This is unrelated to the REST transport's EKS deployment (see
`../namespace.yaml` etc.) -- K2 keeps talking to the REST transport in EKS
exactly as before; this path is for MCP-native agents on AgentCore.

Two ways to get the code deployed: **Option A** uses the `agentcore` CLI
(Node.js/npm), the path AWS's own docs lead with. **Option B** needs nothing
but `pip` (already have it) and either the AWS Console or the plain `aws`
CLI -- use this one if `npm install -g` is blocked by corporate policy, as
it was in the case this runbook was first written for.

**Not verified in this environment.** No AWS account/credentials, the
`agentcore` CLI, nor `npm` itself were available in the sandbox this runbook
was written in, so none of the AWS-side commands below (either option) were
actually executed. What *was* verified locally: `entrypoint.py` in this
directory imports `a2k.mcp_server.server` successfully with `fastapi`/
`uvicorn` blocked, which is why `requirements.txt` here omits them (see
comment in that file) -- confirming the MCP-only deploy doesn't need the
REST transport's dependencies. Treat the AWS-specific field names
(`agentcore.json`'s `entrypoint` key, exact CLI flags, IAM permission
names) as best-effort from AWS's own docs, and confirm them against what
your account actually accepts.

## Troubleshooting (confirmed against a real deploy, 2026-08)

**Symptom: `invoke_agent_runtime`/console test always times out ("Runtime
initialization time exceeded" or "MCP error -32010: Runtime health check
failed"), even though CloudWatch logs show the container starting cleanly
(`Uvicorn running on http://0.0.0.0:8000`).**

Two distinct causes, in the order we actually hit them:

1. **Execution role missing operational permissions, not just a broken
   trust policy.** If CloudWatch has *no log group at all* for the runtime,
   the execution role likely can't even call `logs:CreateLogGroup` -- it
   needs the full permissions policy in "AgentCore Runtime execution role"
   under [IAM Permissions for AgentCore
   Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html)
   (`logs:*`, `xray:*`, `cloudwatch:PutMetricData`,
   `bedrock-agentcore:GetWorkloadAccessToken*`, `bedrock:InvokeModel*`), not
   just a trust policy that lets the service assume the role.

2. **`/mcp` vs `/mcp/` (trailing slash), and a missing `/ping`.** Once (1)
   is fixed and the container demonstrably starts and keeps running, this
   is the next thing to hit -- and it's silent: FastMCP registers its
   endpoint at `/mcp` only (exact match, no redirect), so anything hitting
   `/mcp/` 404s before any of your own code runs. AWS's own docs are
   internally inconsistent about which spelling AgentCore's sidecar
   actually uses for its liveness probe -- and, per one of their
   troubleshooting pages, possibly for real MCP traffic too -- so this
   isn't safely narrowed down to "just short-circuit the probe's exact
   fingerprint" (an earlier, narrower attempt at exactly that -- matching
   only `POST /mcp/` from `127.0.0.1` with no auth header -- still failed
   the exact same way; see git history). AgentCore's generic health/idle
   contract (`GET /ping`, separate from the MCP-specific probe) was also
   simply missing -- FastMCP doesn't register it on its own.

   Fixed in `a2k/mcp_server/server.py`: `main()` no longer calls
   `mcp.run(transport="streamable-http")` directly. It builds the same
   Starlette app that call would have (`mcp.streamable_http_app()`), adds
   an ASGI middleware that rewrites `/mcp/` to `/mcp` in the scope
   *before* routing -- for every request, not a guessed fingerprint, so
   `/mcp` and `/mcp/` are indistinguishable downstream whether the caller
   is the probe, real MCP traffic, or a human with curl -- and appends an
   explicit `GET /ping` route returning `{"status": "Healthy"}`. Then
   serves it with `uvicorn` the same way FastMCP does internally.

   Verified locally against the running server: `GET /ping` -> `200
   {"status":"Healthy"}`; a real MCP `initialize` POSTed to `/mcp/` ->
   normal MCP response (previously 404); the same POSTed to `/mcp` ->
   unaffected. Root-caused with the help of [this writeup of the same
   failure](https://www.k9security.io/posts/2026/06/fix-agentcore-mcp-32010-health-check-failed/),
   generalized here after that fix's narrower version didn't hold up
   against a real redeploy.

   **If you rebuild `a2k-box-mcp.zip` from an older checkout, confirm
   `mcp_server/server.py`'s `main()` includes the `_TrailingSlashAliasMiddleware`
   and `/ping` route** -- an older zip will silently reintroduce this
   exact failure.

## Files in this directory

- `entrypoint.py` -- thin wrapper (`from a2k.mcp_server.server import main`)
  that `agentcore.json` should point its `entrypoint` field at. AgentCore
  Runtime expects an MCP server container listening at `0.0.0.0:8000/mcp`;
  `server.py` is already configured for that (`host="0.0.0.0"`,
  `stateless_http=True`, `mcp.run(transport="streamable-http")`).
- `requirements.txt` -- narrower than the repo's `Pipfile`: omits
  `fastapi`/`uvicorn` (only used by `a2k/api`, the REST transport, and by
  `a2k/cli.py`'s `run_rest`/`run_mcp` helpers -- neither of which this
  entrypoint imports, since it calls `a2k.mcp_server.server.main` directly
  rather than going through `a2k.cli`).

## 0. Prerequisites

```bash
aws configure          # credentials for the target account, if not already set
```

`npm install -g @aws/agentcore` is only needed for Option A below (step 2).
Option B needs nothing beyond `aws` and `pip`.

## 1. Inbound identity (Cognito quick setup, for testing)

AgentCore Gateway needs an inbound authorizer for the agent-facing side, and
the deployed MCP server on Runtime needs its own inbound auth too. A Cognito
user pool is the fastest way to get both for a test; swap for your real IdP
(Discovery URL / audiences / scopes) once this is beyond a prototype.

```bash
export REGION=eu-west-1          # your region
export USERNAME=test-agent
export PASSWORD="ChangeThisPassword123!"

export POOL_ID=$(aws cognito-idp create-user-pool \
  --pool-name "a2k-box-pool" \
  --policies '{"PasswordPolicy":{"MinimumLength":8}}' \
  --region $REGION | jq -r '.UserPool.Id')

export CLIENT_ID=$(aws cognito-idp create-user-pool-client \
  --user-pool-id $POOL_ID \
  --client-name "a2k-box-client" \
  --no-generate-secret \
  --explicit-auth-flows "ALLOW_USER_PASSWORD_AUTH" "ALLOW_REFRESH_TOKEN_AUTH" \
  --region $REGION | jq -r '.UserPoolClient.ClientId')

aws cognito-idp admin-create-user --user-pool-id $POOL_ID --username $USERNAME \
  --region $REGION --message-action SUPPRESS > /dev/null
aws cognito-idp admin-set-user-password --user-pool-id $POOL_ID --username $USERNAME \
  --password "$PASSWORD" --region $REGION --permanent > /dev/null

export BEARER_TOKEN=$(aws cognito-idp initiate-auth \
  --client-id "$CLIENT_ID" --auth-flow USER_PASSWORD_AUTH \
  --auth-parameters USERNAME=$USERNAME,PASSWORD="$PASSWORD" \
  --region $REGION | jq -r '.AuthenticationResult.AccessToken')

echo "Discovery URL: https://cognito-idp.$REGION.amazonaws.com/$POOL_ID/.well-known/openid-configuration"
echo "Client ID: $CLIENT_ID"
```

Keep the **Discovery URL** and **Client ID** -- `agentcore create` asks for
them next.

## 2. Build and deploy -- Option A: `agentcore` CLI (needs npm)

```bash
cd /tmp   # or wherever you want to build this, outside the repo checkout
agentcore create --protocol MCP
# prompts: project name -> a2k-box
#          Discovery URL / Client ID -> from step 1
cd a2k-box   # or whatever name you gave it
```

Copy in the full `a2k` package plus the two files from this directory:

```bash
cp -r /path/to/repo/a2k .
cp /path/to/repo/deploy/agentcore/entrypoint.py .
cp /path/to/repo/deploy/agentcore/requirements.txt .
```

Open the generated `agentcore/agentcore.json` and set its `entrypoint` field
to `entrypoint.py`. Then:

```bash
agentcore deploy
```

Packages the code, uploads to S3, creates the AgentCore Runtime, and deploys
it. Returns a Runtime ARN:

```
arn:aws:bedrock-agentcore:eu-west-1:<account-id>:runtime/a2k-box-xyz123
```

Skip to step 3.

## 2. Build and deploy -- Option B: no npm (plain `pip` + zip)

AgentCore Runtime's direct-code-deploy path doesn't run `pip install` for
you -- dependencies must already be inside the zip, built for **Linux
arm64** (what Runtime actually runs on), which `pip`'s `--platform` flag
can cross-download without needing a Linux machine, as long as every
dependency ships a prebuilt wheel for that platform (true for all of ours:
`mcp`, `pydantic`, `httpx`, `cryptography`, `python-dotenv`).

**Build the package** (PowerShell shown; the same `pip`/zip logic works in
bash with `zip -r` instead of `Compress-Archive`):

```powershell
mkdir C:\temp\a2k-agentcore-build
cd C:\temp\a2k-agentcore-build

pip install `
  --platform manylinux2014_aarch64 `
  --python-version 3.13 `
  --implementation cp `
  --only-binary=:all: `
  --target . `
  -r C:\path\to\repo\deploy\agentcore\requirements.txt

Copy-Item -Recurse C:\path\to\repo\a2k .
Copy-Item C:\path\to\repo\deploy\agentcore\entrypoint.py .

# AWS explicitly warns against shipping __pycache__: bytecode built on your
# machine's architecture/OS may not be compatible with Runtime's arm64 env.
Get-ChildItem -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force

Compress-Archive -Path * -DestinationPath ..\a2k-box-mcp.zip
```

**Deploy it -- via the console (zero CLI/API calls):**

1. AgentCore console -> **Agent Runtime** -> **Host Agent**.
2. **Source type** -> **Local Upload** -> pick `a2k-box-mcp.zip`.
3. **Runtime version** -> Python 3.13. **Entry point** -> `entrypoint.py`.
   **Execution role** -> "Create new" (simplest; avoids hand-writing an IAM
   policy).
4. **Protocol** -> MCP, if offered at this step; otherwise it's set via
   `--protocol-configuration` as in the CLI form below.
5. **Create Agent** -> **Create Endpoint** -> **Test Endpoint**.

**Or deploy it -- via plain `aws` CLI** (no `agentcore`/npm involved):

```powershell
aws s3 cp a2k-box-mcp.zip s3://<your-bucket>/a2k-box/a2k-box-mcp.zip

aws bedrock-agentcore-control create-agent-runtime `
  --agent-runtime-name a2k-box `
  --agent-runtime-artifact '{"codeConfiguration":{"code":{"s3":{"bucket":"<your-bucket>","prefix":"a2k-box/a2k-box-mcp.zip"}},"runtime":"PYTHON_3_13","entryPoint":["entrypoint.py"]}}' `
  --role-arn arn:aws:iam::<account-id>:role/<execution-role> `
  --network-configuration networkMode=PUBLIC `
  --protocol-configuration serverProtocol=MCP
```

Either way, the result is a Runtime ARN, same shape as Option A:

```
arn:aws:bedrock-agentcore:eu-west-1:<account-id>:runtime/a2k-box-xyz123
```

## 3. Test the deployed server

**With `npx` available:**

```bash
export AGENT_ARN="arn:aws:bedrock-agentcore:eu-west-1:<account-id>:runtime/a2k-box-xyz123"
npx @modelcontextprotocol/inspector
```

In the Inspector UI: transport "Streamable HTTP", URL

```
https://bedrock-agentcore.eu-west-1.amazonaws.com/runtimes/<url-encoded-ARN>/invocations?qualifier=DEFAULT
```

Authorization header: `Bearer $BEARER_TOKEN`.

**Without `npx`/npm** (uses the `mcp` package already in
`requirements.txt`, so no new install needed -- run from the repo's own
`.venv`):

```python
# test_remote_mcp.py
import asyncio, os
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def main():
    arn = os.environ["AGENT_ARN"].replace(":", "%3A").replace("/", "%2F")
    url = f"https://bedrock-agentcore.eu-west-1.amazonaws.com/runtimes/{arn}/invocations?qualifier=DEFAULT"
    headers = {"authorization": f"Bearer {os.environ['BEARER_TOKEN']}"}
    async with streamablehttp_client(url, headers, timeout=120) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print(await session.list_tools())

asyncio.run(main())
```

```bash
export AGENT_ARN="arn:aws:bedrock-agentcore:eu-west-1:<account-id>:runtime/a2k-box-xyz123"
python test_remote_mcp.py
```

Either way, you should see the 7 tools (`a2k.search`, `a2k.ask`,
`a2k.explain`, `a2k.getDocument`, `a2k.validateCitation`,
`a2k.reportConflict`, `a2k.getAuditRecord`) and the 3 card resources
(`a2k://card`, `a2k://card/cala`, `a2k://card/sayari`).

**No Bearer token, only IAM credentials?** Both options above assume a
JWT/Cognito inbound authorizer (step 1). If instead you have
`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_SESSION_TOKEN` (e.g. from
SSO) and the Runtime's inbound authorizer is IAM, use
[`test_remote_mcp_iam.py`](test_remote_mcp_iam.py) in this directory
instead -- it calls `invoke_agent_runtime` via `boto3` (SigV4-signed
automatically from those env vars), no Cognito/Bearer token needed at all.
Edit the `AGENT_RUNTIME_ARN`/`REGION` constants at the top before running.

## 4. Register it as an AgentCore Gateway target

With the Gateway already created (see AWS console: Gateways -> Create
gateway, or `CreateGateway`), add a2k-box as a target of type "AgentCore
Runtime":

```bash
aws bedrock-agentcore create-gateway-target \
  --gateway-identifier <your-gateway-id> \
  --region $REGION \
  --cli-input-json '{
    "name": "a2k-box",
    "targetConfiguration": {
      "mcp": {
        "mcpServer": {
          "endpoint": "https://bedrock-agentcore.'"$REGION"'.amazonaws.com/runtimes/<url-encoded-ARN>/invocations?qualifier=DEFAULT&accountId=<account-id>"
        }
      }
    },
    "credentialProviderConfigurations": [
      {
        "credentialProviderType": "OAUTH",
        "credentialProvider": {
          "oauthCredentialProvider": { "providerArn": "<oauth-provider-arn-in-agentcore-identity>" }
        }
      }
    ]
  }'
```

The `providerArn` is an OAuth (client-credentials, two-legged) provider
configured beforehand in AgentCore Identity, for the Gateway-to-Runtime
outbound call -- a separate setup step not covered here.

Traffic between Gateway and this Runtime-hosted MCP server stays on AWS's
internal network end to end; there is no VPC, load balancer, or TLS
certificate to provision for this path.
