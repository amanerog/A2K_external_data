# Deploy a2k-box's MCP server to Amazon Bedrock AgentCore Runtime

This is the detailed runbook behind the "Deploy to AgentCore" section of the
top-level `README.md`. It deploys **only the MCP transport** of a2k-box
(`a2k/mcp_server/server.py`, streamable-http) as its own AgentCore Runtime
workload, so it can be registered as an MCP server target on an AgentCore
Gateway and consumed by an agent that itself runs as a separate Runtime
workload. This is unrelated to the REST transport's EKS deployment (see
`../namespace.yaml` etc.) -- K2 keeps talking to the REST transport in EKS
exactly as before; this path is for MCP-native agents on AgentCore.

**Not verified in this environment.** No AWS account/credentials or the
`agentcore` CLI (Node.js) were available in the sandbox this runbook was
written in, so none of the AWS-side commands below were actually executed.
What *was* verified locally: `entrypoint.py` in this directory imports
`a2k.mcp_server.server` successfully with `fastapi`/`uvicorn` blocked, which
is why `requirements.txt` here omits them (see comment in that file) --
confirming the MCP-only deploy doesn't need the REST transport's
dependencies. Treat the AWS-specific field names (`agentcore.json`'s
`entrypoint` key, exact CLI flags) as best-effort from AWS's own docs, and
confirm them against whatever your `agentcore create` run actually
generates.

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
npm install -g @aws/agentcore
```

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

## 2. Scaffold the deployment project

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
to `entrypoint.py`.

## 3. Deploy

```bash
agentcore deploy
```

Packages the code, uploads to S3, creates the AgentCore Runtime, and deploys
it. Returns a Runtime ARN:

```
arn:aws:bedrock-agentcore:eu-west-1:<account-id>:runtime/a2k-box-xyz123
```

## 4. Test the deployed server

```bash
export AGENT_ARN="arn:aws:bedrock-agentcore:eu-west-1:<account-id>:runtime/a2k-box-xyz123"
npx @modelcontextprotocol/inspector
```

In the Inspector UI: transport "Streamable HTTP", URL

```
https://bedrock-agentcore.eu-west-1.amazonaws.com/runtimes/<url-encoded-ARN>/invocations?qualifier=DEFAULT
```

Authorization header: `Bearer $BEARER_TOKEN`. You should see the 7 tools
(`a2k.search`, `a2k.ask`, `a2k.explain`, `a2k.getDocument`,
`a2k.validateCitation`, `a2k.reportConflict`, `a2k.getAuditRecord`) and the
3 card resources (`a2k://card`, `a2k://card/cala`, `a2k://card/sayari`).

## 5. Register it as an AgentCore Gateway target

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
