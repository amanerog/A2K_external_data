"""The two cache Runtimes (a2k-box and the agent) under CloudFormation (E2.1).

    export   writes cache-runtimes.yaml from the live Runtimes: role, network,
             protocol, inbound auth, request headers, lifecycle, entry point
             and environment variables exactly as they are. Secret-looking
             variables (SECRET/KEY/PASSWORD/TOKEN) become NoEcho parameters and
             never reach the file; the code location and the cache switches
             (CACHE_MODE, CACHE_DOMAIN_MODES) are parameters too.
    import   imports both Runtimes into the stack, with every parameter read
             from the live Runtimes -- nothing changes in them. Refuses if
             cache-runtimes.yaml no longer matches the live configuration
             (re-run export, review the diff, commit).

Once imported, update_cache_runtime.py deploys through the stack (new code
key, switch parameters); other changes go in cache-runtimes.yaml and
`aws cloudformation deploy`.

Usage (credentials for the account in the environment):
    python deploy/agentcore/runtimes_stack.py export
    python deploy/agentcore/runtimes_stack.py import [--stack a2k-cache-runtimes] [--apply]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import boto3
import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
from cfn_import import import_resources  # noqa: E402
from create_cache_runtimes import SECRET_LIKE  # noqa: E402
from update_cache_runtime import SWITCHES, find_runtime  # noqa: E402

TEMPLATE = HERE / "cache-runtimes.yaml"
STACK = "a2k-cache-runtimes"
# Logical id in the stack -> Runtime name.
RUNTIMES = {"Box": "a2k_cache_mcp", "Agent": "a2k_cache_agent"}


class _NoAliases(yaml.SafeDumper):
    """CloudFormation rejects YAML aliases (&id001), which PyYAML writes for
    any object that appears twice."""

    def ignore_aliases(self, data: Any) -> bool:
        return True


def _pascal(value: Any) -> Any:
    if isinstance(value, dict):
        return {k[:1].upper() + k[1:]: _pascal(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_pascal(v) for v in value]
    return value


def _env_param(logical: str, var: str) -> str:
    return logical + "Env" + "".join(part.capitalize() for part in var.split("_"))


def runtime_resource(logical: str, rt: dict) -> tuple[dict, dict[str, dict], dict[str, str], set[str]]:
    """(resource, parameter declarations, parameter values, secret parameter names)."""
    code = rt["agentRuntimeArtifact"]["codeConfiguration"]
    params: dict[str, dict] = {
        f"{logical}CodeBucket": {"Type": "String"},
        f"{logical}CodeKey": {"Type": "String", "Description": "S3 key of the code zip (update_cache_runtime.py sets it)"},
    }
    values = {f"{logical}CodeBucket": code["code"]["s3"]["bucket"], f"{logical}CodeKey": code["code"]["s3"]["prefix"]}
    secrets: set[str] = set()
    env_live = dict(rt.get("environmentVariables") or {})
    env: dict[str, Any] = {}
    switches = SWITCHES if any(k.startswith("CACHE_") for k in env_live) else {}
    for var in sorted(set(env_live) | set(switches)):
        if var in switches:
            name = logical + switches[var]
            # No Default: the template must not depend on the mode the Runtime is in.
            params[name] = {"Type": "String", "Description": f"{var} on {rt['agentRuntimeName']}"}
            values[name] = env_live.get(var, "")
            env[var] = {"Ref": name}
        elif SECRET_LIKE.search(var):
            name = _env_param(logical, var)
            params[name] = {"Type": "String", "NoEcho": True}
            values[name] = env_live[var]
            secrets.add(name)
            env[var] = {"Ref": name}
        else:
            env[var] = env_live[var]

    props: dict[str, Any] = {
        "AgentRuntimeName": rt["agentRuntimeName"],
        "RoleArn": rt["roleArn"],
        "AgentRuntimeArtifact": {
            "CodeConfiguration": {
                "Code": {"S3": {"Bucket": {"Ref": f"{logical}CodeBucket"}, "Prefix": {"Ref": f"{logical}CodeKey"}}},
                "EntryPoint": code["entryPoint"],
                "Runtime": code["runtime"],
            }
        },
        "EnvironmentVariables": env,
    }
    if rt.get("description"):
        props["Description"] = rt["description"]
    if rt.get("protocolConfiguration"):
        props["ProtocolConfiguration"] = rt["protocolConfiguration"]["serverProtocol"]
    for key in ("networkConfiguration", "authorizerConfiguration", "requestHeaderConfiguration", "lifecycleConfiguration"):
        if rt.get(key):
            props[key[:1].upper() + key[1:]] = _pascal(rt[key])
    resource = {"Type": "AWS::BedrockAgentCore::Runtime", "DeletionPolicy": "Retain", "UpdateReplacePolicy": "Retain", "Properties": props}
    return resource, params, values, secrets


def build(control) -> tuple[str, dict[str, str], set[str], dict[str, str]]:
    """(template text, parameter values, secret parameter names, logical id -> runtime id)."""
    resources, params, values, secrets, ids = {}, {}, {}, set(), {}
    for logical, name in RUNTIMES.items():
        rt = find_runtime(control, name)
        resources[logical], p, v, s = runtime_resource(logical, rt)
        params.update(p)
        values.update(v)
        secrets |= s
        ids[logical] = rt["agentRuntimeId"]
    template = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "a2k cache Runtimes (E2.1): a2k-box (Box) and the agent (Agent). Generated from the live Runtimes by "
        "deploy/agentcore/runtimes_stack.py export; secrets are NoEcho parameters.",
        "Parameters": params,
        "Resources": resources,
        "Outputs": {f"{logical}RuntimeArn": {"Value": {"Fn::GetAtt": [logical, "AgentRuntimeArn"]}} for logical in RUNTIMES},
    }
    return yaml.dump(template, Dumper=_NoAliases, sort_keys=False, allow_unicode=True, width=200), values, secrets, ids


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("export", "import"))
    parser.add_argument("--stack", default=STACK)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "eu-west-1"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    control = boto3.client("bedrock-agentcore-control", region_name=args.region)
    template, values, secrets, ids = build(control)
    cfn = boto3.client("cloudformation", region_name=args.region)
    if args.command == "export":
        cfn.validate_template(TemplateBody=template)
        TEMPLATE.write_text(template, encoding="utf-8")
        print(f"wrote {TEMPLATE} ({', '.join(f'{k}={v}' for k, v in ids.items())}); {len(secrets)} secret parameter(s) left out of the file")
        return

    if not TEMPLATE.exists() or TEMPLATE.read_text(encoding="utf-8") != template:
        sys.exit(f"{TEMPLATE.name} does not match the live Runtimes -- run `export`, review the diff and commit it first")
    import_resources(
        cfn,
        stack=args.stack,
        template=template,
        parameters=values,
        resources=[
            {"ResourceType": "AWS::BedrockAgentCore::Runtime", "LogicalResourceId": logical, "ResourceIdentifier": {"AgentRuntimeId": rid}}
            for logical, rid in ids.items()
        ],
        apply=args.apply,
        secret_parameters=frozenset(secrets),
    )


if __name__ == "__main__":
    main()
