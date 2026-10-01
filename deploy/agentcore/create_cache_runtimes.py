"""Creates the two AgentCore Runtimes for the cache (shadow mode) next to the
existing ones, copying their configuration so that no secret has to be typed,
printed or stored anywhere:

  1. the agent   -- cloned from the current agent Runtime (HTTP, Cognito JWT
                    inbound auth, same environment), new code:
                    agent/router-agent-direct-cache-v1.zip
  2. a2k-box     -- cloned from the current MCP Runtime (MCP, IAM auth, same
                    environment), new code: deploy/agentcore/a2k-box-mcp-cache-v1.zip,
                    AGENT_RUNTIME_URL pointing at the NEW agent, plus the cache
                    variables (CACHE_MODE=shadow, Aurora, evidence bucket)

The existing Runtimes are only read, never changed. Without --apply it prints
the plan (secret-looking variables masked) and creates nothing.

--attach-policy also puts the cache permissions on the a2k-box role as an
inline policy (A2KCacheAccess): Data API on the cluster, the DB secret, the
evidence bucket, and the Bedrock models admission/retrieval call.

Usage (from the repo root, with the cache variables in .env):
    set -a; source .env; set +a
    python deploy/agentcore/create_cache_runtimes.py            # plan only
    python deploy/agentcore/create_cache_runtimes.py --apply --attach-policy
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import boto3

REPO = Path(__file__).resolve().parents[2]
AGENT_ZIP = REPO / "agent" / "router-agent-direct-cache-v1.zip"
MCP_ZIP = REPO / "deploy" / "agentcore" / "a2k-box-mcp-cache-v1.zip"
SECRET_LIKE = re.compile("SECRET|KEY|PASSWORD|TOKEN", re.I)

# Models admission and retrieval call from a2k-box (config.py defaults).
CACHE_MODELS = (
    "eu.anthropic.claude-haiku-4-5-20251001-v1:0",  # light model
    "eu.anthropic.claude-sonnet-5",  # judge
    "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",  # Checker
)


def masked(env: dict[str, str]) -> dict[str, str]:
    return {k: ("***" if SECRET_LIKE.search(k) else v) for k, v in sorted(env.items())}


def runtime_url(arn: str, region: str) -> str:
    return f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{quote(arn, safe='')}/invocations?qualifier=DEFAULT"


def cache_policy(*, account: str, region: str, cluster_arn: str, secret_arn: str, bucket: str) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Sid": "AuroraDataApi", "Effect": "Allow", "Action": "rds-data:ExecuteStatement", "Resource": cluster_arn},
            {"Sid": "AuroraSecret", "Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": secret_arn},
            {"Sid": "EvidenceBucket", "Effect": "Allow", "Action": "s3:PutObject", "Resource": f"arn:aws:s3:::{bucket}/cache-evidence/*"},
            {
                "Sid": "CacheModels",
                "Effect": "Allow",
                "Action": ["bedrock:InvokeModel", "bedrock:Converse"],
                "Resource": [f"arn:aws:bedrock:{region}:{account}:inference-profile/{m}" for m in CACHE_MODELS]
                + [
                    # Cross-region inference profiles can route to any EU region.
                    "arn:aws:bedrock:*::foundation-model/anthropic.claude-*",
                    f"arn:aws:bedrock:{region}::foundation-model/amazon.titan-embed-text-v2:0",
                ],
            },
        ],
    }


def wait_ready(control, runtime_id: str) -> dict:
    for _ in range(90):
        rt = control.get_agent_runtime(agentRuntimeId=runtime_id)
        status = rt["status"]
        if status == "READY":
            return rt
        if status.endswith("FAILED"):
            raise RuntimeError(f"{runtime_id}: {status} {rt.get('failureReason', '')}")
        print(f"  {runtime_id}: {status}...", flush=True)
        time.sleep(10)
    raise TimeoutError(f"{runtime_id} not READY after 15 min")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source-agent", default="hosted_router_agent-cQvidi4ixE")
    parser.add_argument("--source-mcp", default="a2k_external_data_mcp-A3c4F0Cyx7")
    parser.add_argument("--agent-name", default="a2k_cache_agent")
    parser.add_argument("--mcp-name", default="a2k_cache_mcp")
    parser.add_argument("--agent-role-arn", help="default: the source agent's role")
    parser.add_argument("--mcp-role-arn", help="default: the source MCP Runtime's role")
    parser.add_argument("--cache-mode", default="shadow", choices=["off", "shadow", "serve"])
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "eu-west-1"))
    parser.add_argument("--apply", action="store_true", help="create the Runtimes (default: plan only)")
    parser.add_argument("--attach-policy", action="store_true", help="put the A2KCacheAccess inline policy on the a2k-box role")
    args = parser.parse_args()

    missing = [v for v in ("AURORA_CLUSTER_ARN", "AURORA_SECRET_ARN", "CACHE_EVIDENCE_BUCKET") if not os.environ.get(v)]
    if missing:
        sys.exit(f"missing {', '.join(missing)} -- run `set -a; source .env; set +a` first")
    for z in (AGENT_ZIP, MCP_ZIP):
        if not z.exists():
            sys.exit(f"missing {z}")

    control = boto3.client("bedrock-agentcore-control", region_name=args.region)
    account = boto3.client("sts").get_caller_identity()["Account"]
    src_agent = control.get_agent_runtime(agentRuntimeId=args.source_agent)
    src_mcp = control.get_agent_runtime(agentRuntimeId=args.source_mcp)
    artifact_bucket = src_agent["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"]["bucket"]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    agent_key = f"a2k-cache/{stamp}-{AGENT_ZIP.name}"
    mcp_key = f"a2k-cache/{stamp}-{MCP_ZIP.name}"

    agent_role = args.agent_role_arn or src_agent["roleArn"]
    mcp_role = args.mcp_role_arn or src_mcp["roleArn"]
    agent_env = dict(src_agent.get("environmentVariables") or {})
    mcp_env = dict(src_mcp.get("environmentVariables") or {})
    mcp_env.update(
        {
            "CACHE_MODE": args.cache_mode,
            "AURORA_CLUSTER_ARN": os.environ["AURORA_CLUSTER_ARN"],
            "AURORA_SECRET_ARN": os.environ["AURORA_SECRET_ARN"],
            "AURORA_DATABASE": os.environ.get("AURORA_DATABASE", "a2k_cache"),
            "CACHE_EVIDENCE_BUCKET": os.environ["CACHE_EVIDENCE_BUCKET"],
            "AGENT_RUNTIME_URL": "<set to the new agent once it exists>",
        }
    )
    policy = cache_policy(
        account=account,
        region=args.region,
        cluster_arn=os.environ["AURORA_CLUSTER_ARN"],
        secret_arn=os.environ["AURORA_SECRET_ARN"],
        bucket=os.environ["CACHE_EVIDENCE_BUCKET"],
    )

    print("== Plan")
    print(f"code bucket: s3://{artifact_bucket}/ (same as the existing Runtimes)")
    print(f"\n1. agent Runtime {args.agent_name!r}: {AGENT_ZIP.name} -> s3://{artifact_bucket}/{agent_key}")
    print(f"   role {agent_role}, HTTP, auth copied from {args.source_agent}: {json.dumps(src_agent.get('authorizerConfiguration'))}")
    print(f"   env: {json.dumps(masked(agent_env), indent=6)}")
    print(f"\n2. a2k-box Runtime {args.mcp_name!r}: {MCP_ZIP.name} -> s3://{artifact_bucket}/{mcp_key}")
    print(f"   role {mcp_role}, MCP, IAM auth (as {args.source_mcp})")
    print(f"   env: {json.dumps(masked(mcp_env), indent=6)}")
    print(f"\n3. inline policy A2KCacheAccess on role {mcp_role.split('/')[-1]} {'(will be attached)' if args.attach_policy else '(not attached: --attach-policy)'}:")
    print(json.dumps(policy, indent=2))

    if not args.apply:
        print("\nPlan only -- nothing created. Re-run with --apply (and --attach-policy) to create.")
        return

    s3 = boto3.client("s3", region_name=args.region)
    if args.attach_policy:
        boto3.client("iam").put_role_policy(
            RoleName=mcp_role.split("/")[-1], PolicyName="A2KCacheAccess", PolicyDocument=json.dumps(policy)
        )
        print("\nattached A2KCacheAccess")

    print("\n== Creating the agent Runtime")
    s3.upload_file(str(AGENT_ZIP), artifact_bucket, agent_key)
    created = control.create_agent_runtime(
        agentRuntimeName=args.agent_name,
        description="A2K vendor agent -- cache-admission branch (shadow mode)",
        agentRuntimeArtifact={
            "codeConfiguration": {
                "code": {"s3": {"bucket": artifact_bucket, "prefix": agent_key}},
                "runtime": src_agent["agentRuntimeArtifact"]["codeConfiguration"]["runtime"],
                "entryPoint": ["entrypoint_a2k.py"],
            }
        },
        roleArn=agent_role,
        networkConfiguration=src_agent["networkConfiguration"],
        protocolConfiguration={"serverProtocol": "HTTP"},
        authorizerConfiguration=src_agent["authorizerConfiguration"],
        environmentVariables=agent_env,
    )
    agent = wait_ready(control, created["agentRuntimeId"])
    agent_arn = agent["agentRuntimeArn"]
    print(f"  READY: {agent_arn}")

    print("\n== Creating the a2k-box Runtime")
    mcp_env["AGENT_RUNTIME_URL"] = runtime_url(agent_arn, args.region)
    s3.upload_file(str(MCP_ZIP), artifact_bucket, mcp_key)
    created = control.create_agent_runtime(
        agentRuntimeName=args.mcp_name,
        description=f"A2K a2k-box MCP server -- cache-admission branch, CACHE_MODE={args.cache_mode}",
        agentRuntimeArtifact={
            "codeConfiguration": {
                "code": {"s3": {"bucket": artifact_bucket, "prefix": mcp_key}},
                "runtime": src_mcp["agentRuntimeArtifact"]["codeConfiguration"]["runtime"],
                "entryPoint": ["entrypoint.py"],
            }
        },
        roleArn=mcp_role,
        networkConfiguration=src_mcp["networkConfiguration"],
        protocolConfiguration={"serverProtocol": "MCP"},
        environmentVariables=mcp_env,
    )
    mcp = wait_ready(control, created["agentRuntimeId"])
    print(f"  READY: {mcp['agentRuntimeArn']}")

    print("\n== Done")
    print(f"agent   : {agent_arn}")
    print(f"a2k-box : {mcp['agentRuntimeArn']}")
    print("Point run_ground_truth_eval.py (or any client) at the a2k-box ARN to test shadow mode.")


if __name__ == "__main__":
    main()
