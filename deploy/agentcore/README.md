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

**Deploy (sections 1-3), the IAM-outbound Gateway target (section 4a), live
mode, and a consuming agent are all confirmed against a real AWS account as
of 2026-08-18** -- a2k-box is live on AgentCore Runtime (real Cala/Sayari
credentials via Secrets Manager, see section 5), registered as a Gateway
target with IAM outbound auth, and `test_gateway_mcp.py` (this directory)
lists a2k-box's tools through the Gateway. `a2k.listVendors` (section 5)
exists on a2k-box's Runtime directly (confirmed via
`test_remote_mcp_iam.py`) but the Gateway's own DEFAULT-mode tool-catalog
cache needs an explicit `SynchronizeGatewayTargets` call (or re-saving the
target) before *it* reflects new/changed tools -- confirmed live 2026-08-18
that redeploying a2k-box alone isn't enough. A separate Strands-based agent
(`../../agent/`, see that directory's README) consumes this Gateway and is
itself deployed to its own AgentCore Runtime. The sandbox this runbook was
originally *written* in had no AWS account/credentials, so treat anything
still marked "not verified" below (the 4b-4e OAuth fallback in particular)
as best-effort from AWS's docs until someone actually runs it.

## Troubleshooting (confirmed against a real deploy, 2026-08)

**Symptom: `invoke_agent_runtime`/console test always times out ("Runtime
initialization time exceeded" or "MCP error -32010: Runtime health check
failed"), even though CloudWatch logs show the container starting cleanly
(`Uvicorn running on http://0.0.0.0:8000`).**

Three distinct causes, in the order we actually hit them. All three were
real and needed fixing -- (1) and (2) don't become visible/testable until
the one before is fixed, and (3) turned out to be the actual final blocker
in our case even after (1) and (2) were both already fixed correctly.

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

3. **The Runtime's protocol configuration wasn't actually set to MCP.**
   This was the one that turned out to matter in our case, confirmed
   2026-08-12 -- (1) and (2) above were both real and both necessary, but
   neither was the final blocker. If a Runtime is created via the console's
   "Host Agent" flow without explicitly choosing MCP as the protocol, it
   defaults to the generic HTTP contract -- AgentCore's proxy then expects
   your app at `/invocations` (POST) and `/ping` (GET), never touches
   `/mcp` at all, and no amount of fixing the MCP-specific paths changes
   that. CloudWatch logs look identical either way (the container starts
   fine regardless of which contract AgentCore thinks it's talking to),
   which is what made this one hard to distinguish from (2) without
   checking the config directly.

   **Check directly, don't infer from symptoms:**

   ```bash
   aws bedrock-agentcore-control get-agent-runtime \
     --agent-runtime-id <runtime-id> --region <region>
   ```

   Look at `protocolConfiguration` in the response. If creating via the
   console, look for an explicit MCP protocol selector during setup rather
   than assuming a default.

   **Also worth knowing while debugging this:** each test invocation in
   the console (and `InvokeAgentRuntime` in general) is pinned to a
   `Session ID` -- reusing an old one keeps hitting the code/config that
   session was created against, even after you redeploy. Clear/change the
   Session ID field when testing a fresh deploy, or you'll see identical
   failures that look like nothing changed even when it did.

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

Either way, you should see the 8 tools (`a2k.search`, `a2k.ask`, `a2k.listVendors`,
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

**AWS's own docs contradict each other on whether IAM/gateway-service-role
auth works for "AgentCore Runtime (HTTP)" targets.** The general
outbound-auth compatibility table lists it as supported; the VPC-egress
page, which discusses AgentCore Runtime targets specifically rather than
generically, states plainly that only two methods are supported --
no-authorization and OAuth client-credentials -- with no IAM option
mentioned. Rather than pick a side from documentation alone (see
"Troubleshooting" above for how expensive that guessing game got with the
Runtime deployment itself), **try IAM first, via the console** (4a below)
-- it's a five-minute check, and if it works it skips the entire
Cognito-M2M-client + credential-provider setup below. If it fails with an
auth error, fall back to OAuth (4b-4e).

### 4a. IAM via console -- confirmed working (2026-08-17), no OAuth setup needed

IAM Role *is* offered as an Outbound Auth option for MCP server targets --
the general compatibility table was right, the VPC-egress page (IAM not
listed) was wrong or talking about a different scenario.

1. Gateways -> **Create gateway**. Inbound Auth: **Quick create with
   Cognito** (or reuse the pool from step 1 -- this is the agent-facing
   side, unrelated to how Gateway reaches a2k-box). Permissions:
   **Create and use a new service role**.
2. In the **Target** section of the same page: **Target type** -> MCP
   server. **Endpoint** ->
   `https://bedrock-agentcore.eu-west-1.amazonaws.com/runtimes/<url-encoded-ARN>/invocations?qualifier=DEFAULT`
   -- **no `accountId` query param**: an earlier version of this doc added
   one on spec ("best-effort from AWS's docs, not verified"), and it turned
   out to cause an HTTP 400 on the initialize handshake. Neither AWS's own
   MCP-server-targets doc nor a live-tested writeup mention it.
   **Outbound Auth configurations** -> **IAM Role**. **Service** ->
   `bedrock-agentcore`. **Region** -> leave blank (defaults to the
   gateway's own Region, which matches the Runtime's here).
3. **Create gateway.**

**The auto-generated service role does *not* include invoke permission on
your specific Runtime by default** -- contrary to what you'd expect from
"create and use a new service role", target creation fails with
`Authorization error when sending message` until you add this inline
policy to that role by hand (find its name/ARN on the Gateway's own
**Permissions** tab):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "InvokeA2KBox",
      "Effect": "Allow",
      "Action": "bedrock-agentcore:InvokeAgentRuntime",
      "Resource": [
        "<a2k-box Runtime ARN>",
        "<a2k-box Runtime ARN>/*"
      ]
    }
  ]
}
```

The `/*` matters -- the actual invocation targets a subresource path
(`runtime/<id>/runtime-endpoint/DEFAULT`), not the bare Runtime ARN.

Once that policy's attached, save/re-sync the target. If a2k-box's tools
show up in the target's tool list, IAM auth worked end-to-end and 4b-4e
below aren't needed. **Whenever a2k-box's own tool list changes (new tool,
removed tool), the target needs an explicit re-sync too** -- redeploying
a2k-box's Runtime alone does not update the Gateway's own cached tool
catalog for DEFAULT-mode targets (confirmed live 2026-08-18, see section 5).

**Confirmed end-to-end 2026-08-17**: with the endpoint/policy fixes above,
[`test_gateway_mcp.py`](test_gateway_mcp.py) (this directory -- fetches a
Bearer token from the Gateway's inbound Cognito pool via client_credentials,
then does `initialize` + `tools/list` against the Gateway's own MCP
endpoint) returned all of a2k-box's tools through the Gateway.

**Tool name prefixing**: Gateway namespaces every tool by target name to
avoid collisions across multiple targets, so an agent calling through the
Gateway sees `<target-name>___a2k.ask` etc., not the bare `a2k.ask` the MCP
server itself exposes -- account for this when wiring up the agent side.

### 4b-4e. Fallback: OAuth client-credentials (not needed here, kept for reference)

### 4b. Machine-to-machine Cognito app client

The Cognito pool from step 1 was set up for a human-style login
(`ALLOW_USER_PASSWORD_AUTH`) -- client-credentials grant needs a
*different* kind of app client, one with no secret exposed to a human,
plus a Resource Server defining a custom scope (Cognito requires this for
`client_credentials` grants; it's not optional the way it was for the
password grant in step 1).

```bash
export POOL_ID="<pool id from step 1>"

# A scope the Gateway will request when exchanging its client credentials
# for a token -- the name is arbitrary, just has to match what's used below.
aws cognito-idp create-resource-server \
  --user-pool-id "$POOL_ID" \
  --identifier "a2k-box" \
  --name "a2k-box" \
  --scopes '[{"ScopeName":"invoke","ScopeDescription":"Invoke a2k-box via the Gateway"}]' \
  --region $REGION

export M2M_CLIENT_ID=$(aws cognito-idp create-user-pool-client \
  --user-pool-id "$POOL_ID" \
  --client-name "a2k-box-gateway-m2m" \
  --generate-secret \
  --allowed-o-auth-flows client_credentials \
  --allowed-o-auth-scopes "a2k-box/invoke" \
  --allowed-o-auth-flows-user-pool-client \
  --region $REGION | jq -r '.UserPoolClient.ClientId')

export M2M_CLIENT_SECRET=$(aws cognito-idp describe-user-pool-client \
  --user-pool-id "$POOL_ID" --client-id "$M2M_CLIENT_ID" \
  --region $REGION | jq -r '.UserPoolClient.ClientSecret')
```

Cognito also needs a **domain** for its OAuth endpoints to resolve (skip if
step 1 already set one up for this pool):

```bash
aws cognito-idp create-user-pool-domain \
  --domain "a2k-box-$(echo $POOL_ID | tr -d '_' | tr '[:upper:]' '[:lower:]')" \
  --user-pool-id "$POOL_ID" --region $REGION
```

### 4c. Register the OAuth2 credential provider in AgentCore Identity

```bash
aws bedrock-agentcore-control create-oauth2-credential-provider \
  --name a2k-box-gateway-oauth \
  --credential-provider-vendor CustomOAuth2 \
  --oauth2-provider-config-input '{
    "customOAuth2ProviderConfig": {
      "oauthDiscovery": {
        "discoveryUrl": "https://cognito-idp.'"$REGION"'.amazonaws.com/'"$POOL_ID"'/.well-known/openid-configuration"
      },
      "clientId": "'"$M2M_CLIENT_ID"'",
      "clientSecret": "'"$M2M_CLIENT_SECRET"'"
    }
  }' \
  --region $REGION
```

Note the `credentialProviderArn` and `secretArn` from the response -- both
needed next.

### 4d. Create the Gateway (console)

Gateways -> **Create gateway**. Inbound Auth: reuse **Quick create with
Cognito** or point at the same pool from step 1 (this is the agent-facing
side, separate from the M2M client just created for the outbound leg).
Permissions: **Create and use a new service role**.

If a custom/existing service role is used instead of letting the console
generate one, it needs this policy attached (fill in the gateway name and
the two ARNs from 4b):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "GetWorkloadAccessToken",
      "Effect": "Allow",
      "Action": ["bedrock-agentcore:GetWorkloadAccessToken"],
      "Resource": [
        "arn:aws:bedrock-agentcore:eu-west-1:396961015428:workload-identity-directory/default",
        "arn:aws:bedrock-agentcore:eu-west-1:396961015428:workload-identity-directory/default/workload-identity/<gateway-name>-*"
      ]
    },
    {
      "Sid": "GetResourceOauth2Token",
      "Effect": "Allow",
      "Action": ["bedrock-agentcore:GetResourceOauth2Token"],
      "Resource": ["<credentialProviderArn from 4b>"]
    },
    {
      "Sid": "GetSecretValue",
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue"],
      "Resource": ["<secretArn from 4b>"]
    }
  ]
}
```

### 4e. Add a2k-box as a target

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
          "oauthCredentialProvider": {
            "providerArn": "<credentialProviderArn from 4b>",
            "scopes": ["a2k-box/invoke"]
          }
        }
      }
    ]
  }'
```

Traffic between Gateway and this Runtime-hosted MCP server stays on AWS's
internal network end to end; there is no VPC, load balancer, or TLS
certificate to provision for this path.

**Not verified in this environment** -- same caveat as the rest of this
runbook: no AWS account here, so 4a-4d were not actually run. The Cognito
M2M/resource-server commands and the credential-provider JSON shape are
best-effort from AWS's docs; confirm each step's output before assuming
the next one's placeholders are correct.

## 5. Going live

Confirmed against a real deploy, 2026-08-18. Flipping `A2K_BOX_MODE` from
`mock` to `live` needs real Cala/Sayari credentials as Runtime environment
variables -- but plaintext Runtime env vars are visible to anyone with read
access to the Runtime resource, so prefer Secrets Manager over pasting
credentials in directly:

1. Create a secret (flat JSON, one object with all four keys):
   ```bash
   aws secretsmanager create-secret \
     --name a2k/box/live-credentials \
     --secret-string file:///path/to/local-only.json \
     --region $REGION
   # {"CALA_API_KEY": "...", "AUTH0_CLIENT_ID": "...", "AUTH0_CLIENT_SECRET": "..."}
   ```
   (`SAYARI_CLIENT_ID`/`SAYARI_CLIENT_SECRET` aren't needed -- `gateway/engine.py`
   wires up the MCP adapters, not the REST ones, so only `CALA_API_KEY` and the
   Auth0 pair matter here. See `config.py`'s field comments for which adapter
   uses which credential.)
2. Attach an inline policy to a2k-box's Runtime execution role, scoped to that
   secret's ARN:
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Sid": "GetBoxLiveSecret",
       "Effect": "Allow",
       "Action": "secretsmanager:GetSecretValue",
       "Resource": "arn:aws:secretsmanager:eu-west-1:<account-id>:secret:a2k/box/live-credentials-*"
     }]
   }
   ```
3. On the Runtime's environment variables: set `A2K_BOX_MODE=live` and
   `A2K_SECRETS_ARN=<the secret's full ARN from step 1>` -- do **not** also set
   `CALA_API_KEY`/`AUTH0_CLIENT_ID`/`AUTH0_CLIENT_SECRET` directly; `config.py`'s
   `_secret_env()` only falls back to Secrets Manager when the plain env var is
   unset, so leaving both would just make the plaintext one win silently.
4. Rebuild and re-upload the zip (`a2k/config.py`'s Secrets Manager fallback and
   `boto3` need to actually be in the deployed code -- see "Files in this
   directory" above for the build steps).

**Known perf issue, fixed 2026-08-18:** `CalaMcpAdapter`/`SayariMcpAdapter`
used to fully hydrate (introspection+retrieval / get_entity_summary) *every*
name-match candidate `entity_search`/`search_entities` returned, up to
a2k.ask's internal `limit=50` -- for an ambiguous query this meant up to 50
full entity fetches for one call, ~458KB/~114K tokens of Facts, enough on
its own to trip Bedrock's per-request byte limit on the consuming agent's
side. Fixed by capping hydration to the top `A2K_MAX_ENTITIES_TO_HYDRATE`
candidates (default `3`, override via that env var) regardless of the
search's own `limit` -- see `adapters/cala_mcp.py`/`adapters/sayari_mcp.py`
and `agent/test_tool_result_size.py` for how this was measured.

**`a2k.listVendors` tool, added 2026-08-18:** lets a consuming agent read
each vendor's declared coverage (`domains`/`topics`/`coverage.scope`, from
the KB Cards in `a2k/cards/*.json`) before deciding `a2k.ask`'s `sources`
param, instead of a hardcoded routing rule on the agent side. Also returns
`status` (reused from the card's existing `enterprise.lifecycle.status` --
routing agents should never select a non-`active` vendor) and `priority` (a
new field on `KBCard`, not part of the A2K-KBCard-Schema spec, lower = more
preferred -- a tie-breaker only, not yet wired into any routing logic, both
vendors currently set to `1`). See `a2k/mcp_server/server.py` and
`a2k/models/kbcard.py`.

**`CALA_RAW_KNOWLEDGE_SEARCH` test flag, added 2026-08-18:** when `true`,
`a2k.ask`/`a2k.search` bypass the normal cited-Facts pipeline for any
request that includes (or doesn't exclude) Cala as a source, returning
Cala's own `knowledge_search` `content` (its LLM-synthesized prose)
completely unprocessed instead. Requested explicitly for comparing "Cala's
own answer" against this gateway's cited synthesis -- off by default, and
normal grounded behavior is unaffected unless this is explicitly set. See
`Config.cala_raw_knowledge_search` in `config.py` and
`agent/test_cala_raw_mode.py`.

## 6. The consuming agent

A Strands-based agent that calls this Gateway (and Bedrock directly for its
own reasoning) lives in `../../agent/` -- its own README covers setup,
local testing, and deploying it to its own AgentCore Runtime. It's a
separate Runtime workload from a2k-box, with its own execution role,
inbound auth (IAM, not Cognito), and environment variables/secrets.
