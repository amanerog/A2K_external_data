"""Points an existing AgentCore Runtime at a new code zip, keeping everything
else exactly as it is: role, network, protocol, inbound auth, request-header
configuration and environment variables (secrets included, never printed).
Optional --set KEY=VALUE adds or changes environment variables.

Without --apply it prints the plan (secret-looking variables masked) and
changes nothing.

A Runtime imported into a CloudFormation stack (runtimes_stack.py) is
deployed through the stack instead, so the stack stays the source of truth:
the zip goes to S3 and the stack's <Logical>CodeKey parameter points at it;
--set only takes the variables that are stack parameters (SWITCHES). Any
other change goes in cache-runtimes.yaml and `aws cloudformation deploy`.

Usage (from the repo root):
    python deploy/agentcore/update_cache_runtime.py --runtime-name a2k_cache_mcp \\
        --zip deploy/agentcore/a2k-box-mcp-cache-v2.zip            # plan only
    python deploy/agentcore/update_cache_runtime.py --runtime-name a2k_cache_mcp \\
        --zip deploy/agentcore/a2k-box-mcp-cache-v2.zip --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from create_cache_runtimes import masked, wait_ready  # noqa: E402

# Environment variables that are parameters of the runtimes stack (logical id
# + this suffix), so switching the cache mode is a parameter change, not a
# template edit (runtimes_stack.py).
SWITCHES = {"CACHE_MODE": "CacheMode", "CACHE_DOMAIN_MODES": "CacheDomainModes"}

# Configuration copied as-is from the current Runtime, when present.
_KEEP = ("networkConfiguration", "protocolConfiguration", "authorizerConfiguration", "requestHeaderConfiguration", "lifecycleConfiguration")


def find_runtime(control, name: str) -> dict:
    token = None
    while True:
        page = control.list_agent_runtimes(**({"nextToken": token} if token else {}))
        for rt in page.get("agentRuntimes", []):
            if rt["agentRuntimeName"] == name:
                return control.get_agent_runtime(agentRuntimeId=rt["agentRuntimeId"])
        token = page.get("nextToken")
        if not token:
            sys.exit(f"no Runtime named {name!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runtime-name", required=True)
    parser.add_argument("--zip", required=True, type=Path)
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="add/change an environment variable")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "eu-west-1"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.zip.exists():
        sys.exit(f"missing {args.zip}")

    control = boto3.client("bedrock-agentcore-control", region_name=args.region)
    rt = find_runtime(control, args.runtime_name)
    code = rt["agentRuntimeArtifact"]["codeConfiguration"]
    bucket = code["code"]["s3"]["bucket"]
    key = f"a2k-cache/{datetime.now():%Y%m%d-%H%M%S}-{args.zip.name}"
    env = dict(rt.get("environmentVariables") or {})
    for item in args.set:
        name, _, value = item.partition("=")
        env[name] = value

    cfn = boto3.client("cloudformation", region_name=args.region)
    owner = owning_stack(cfn, rt["agentRuntimeId"])
    if owner:
        deploy_through_stack(cfn, control, rt, owner, zip_path=args.zip, key=key, bucket=bucket, sets=args.set, region=args.region, apply=args.apply)
        return

    print(f"Runtime  : {rt['agentRuntimeArn']} (version {rt.get('agentRuntimeVersion')}, {rt['status']})")
    print(f"code     : s3://{bucket}/{code['code']['s3']['prefix']}  ->  s3://{bucket}/{key}")
    print(f"entry    : {code['entryPoint']} ({code['runtime']}), role {rt['roleArn']}")
    print(f"kept     : {', '.join(k for k in _KEEP if rt.get(k))}")
    print(f"env      : {json.dumps(masked(env), indent=2)}")
    if not args.apply:
        print("\nPlan only -- nothing changed. Re-run with --apply.")
        return

    boto3.client("s3", region_name=args.region).upload_file(str(args.zip), bucket, key)
    control.update_agent_runtime(
        agentRuntimeId=rt["agentRuntimeId"],
        agentRuntimeArtifact={"codeConfiguration": {**code, "code": {"s3": {"bucket": bucket, "prefix": key}}}},
        roleArn=rt["roleArn"],
        environmentVariables=env,
        **{k: rt[k] for k in _KEEP if rt.get(k)},
    )
    updated = wait_ready(control, rt["agentRuntimeId"])
    print(f"\nREADY: version {updated.get('agentRuntimeVersion')}")


def owning_stack(cfn, runtime_id: str) -> tuple[str, str] | None:
    """(stack name, logical id) of the stack the Runtime was imported into, if any."""
    try:
        resources = cfn.describe_stack_resources(PhysicalResourceId=runtime_id)["StackResources"]
    except ClientError:
        return None
    return (resources[0]["StackName"], resources[0]["LogicalResourceId"]) if resources else None


def deploy_through_stack(cfn, control, rt: dict, owner: tuple[str, str], *, zip_path: Path, key: str, bucket: str, sets: list[str], region: str, apply: bool) -> None:
    stack, logical = owner
    declared = {p["ParameterKey"] for p in cfn.describe_stacks(StackName=stack)["Stacks"][0]["Parameters"]}
    changes = {f"{logical}CodeKey": key}
    for item in sets:
        name, _, value = item.partition("=")
        if name not in SWITCHES or logical + SWITCHES[name] not in declared:
            sys.exit(f"{name} is not a parameter of stack {stack}: edit cache-runtimes.yaml and run `aws cloudformation deploy`")
        changes[logical + SWITCHES[name]] = value
    print(f"Runtime  : {rt['agentRuntimeArn']} -- managed by stack {stack} ({logical})")
    print(f"code     : s3://{bucket}/{key}")
    for name, value in changes.items():
        print(f"parameter: {name} = {value}")
    if not apply:
        print("\nPlan only -- nothing changed. Re-run with --apply.")
        return
    boto3.client("s3", region_name=region).upload_file(str(zip_path), bucket, key)
    cfn.update_stack(
        StackName=stack,
        UsePreviousTemplate=True,
        Parameters=[{"ParameterKey": p, "ParameterValue": changes[p]} if p in changes else {"ParameterKey": p, "UsePreviousValue": True} for p in sorted(declared)],
        Capabilities=["CAPABILITY_NAMED_IAM"],
    )
    cfn.get_waiter("stack_update_complete").wait(StackName=stack, WaiterConfig={"Delay": 10, "MaxAttempts": 180})
    updated = wait_ready(control, rt["agentRuntimeId"])
    print(f"\nREADY: version {updated.get('agentRuntimeVersion')} (stack {stack} updated)")


if __name__ == "__main__":
    main()
