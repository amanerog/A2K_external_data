"""Brings resources created by hand under a CloudFormation stack without
touching them: an IMPORT change set, then an update that only adds the
template's Outputs (an import may not carry them), then drift detection to
show where the template and the live configuration differ.

Shared by deploy/aurora/import_stack.py and deploy/agentcore/runtimes_stack.py.
Without `apply` it stops after printing the import change set.
"""

from __future__ import annotations

import time

from botocore.exceptions import ClientError


def _without_outputs(template: str) -> str:
    head, sep, _ = template.partition("\nOutputs:")
    return head + "\n" if sep else template


def import_resources(
    cfn,
    *,
    stack: str,
    template: str,
    parameters: dict[str, str],
    resources: list[dict],
    apply: bool,
    secret_parameters: frozenset[str] = frozenset(),
) -> None:
    """`resources`: [{"ResourceType", "LogicalResourceId", "ResourceIdentifier"}].
    Values of `secret_parameters` are never printed."""
    params = [{"ParameterKey": k, "ParameterValue": v} for k, v in parameters.items()]
    for k, v in sorted(parameters.items()):
        print(f"  parameter {k} = {'***' if k in secret_parameters else v}")
    name = f"import-{int(time.time())}"
    cfn.create_change_set(
        StackName=stack,
        ChangeSetName=name,
        ChangeSetType="IMPORT",
        TemplateBody=_without_outputs(template),
        Parameters=params,
        ResourcesToImport=resources,
        Capabilities=["CAPABILITY_NAMED_IAM"],
    )
    try:
        cfn.get_waiter("change_set_create_complete").wait(StackName=stack, ChangeSetName=name, WaiterConfig={"Delay": 5, "MaxAttempts": 120})
    except Exception:  # noqa: BLE001 -- the reason is in the change set
        cs = cfn.describe_change_set(StackName=stack, ChangeSetName=name)
        raise SystemExit(f"import change set {cs['Status']}: {cs.get('StatusReason')}")
    cs = cfn.describe_change_set(StackName=stack, ChangeSetName=name)
    for c in cs["Changes"]:
        rc = c["ResourceChange"]
        print(f"  {rc['Action']:<7} {rc['LogicalResourceId']:<22} {rc['ResourceType']}  {rc.get('PhysicalResourceId', '')}")
    if not apply:
        print(f"\nPlan only -- change set {name} left unexecuted on stack {stack}. Re-run with --apply.")
        return

    cfn.execute_change_set(StackName=stack, ChangeSetName=name)
    cfn.get_waiter("stack_import_complete").wait(StackName=stack, WaiterConfig={"Delay": 10, "MaxAttempts": 180})
    print(f"\nIMPORTED into {stack}")

    try:
        cfn.update_stack(
            StackName=stack,
            TemplateBody=template,
            Parameters=[{"ParameterKey": k, "UsePreviousValue": True} for k in parameters],
            Capabilities=["CAPABILITY_NAMED_IAM"],
        )
        cfn.get_waiter("stack_update_complete").wait(StackName=stack, WaiterConfig={"Delay": 10, "MaxAttempts": 180})
        print("Outputs added")
    except ClientError as exc:
        if "No updates are to be performed" not in str(exc):
            raise
    report_drift(cfn, stack)


def report_drift(cfn, stack: str) -> None:
    detection = cfn.detect_stack_drift(StackName=stack)["StackDriftDetectionId"]
    while (status := cfn.describe_stack_drift_detection_status(StackDriftDetectionId=detection))["DetectionStatus"] == "DETECTION_IN_PROGRESS":
        time.sleep(3)
    print(f"\nDrift: {status.get('StackDriftStatus')} ({status['DetectionStatus']})")
    for d in cfn.describe_stack_resource_drifts(StackName=stack)["StackResourceDrifts"]:
        print(f"  {d['StackResourceDriftStatus']:<12} {d['LogicalResourceId']}")
        for diff in d.get("PropertyDifferences", []):
            print(f"      {diff['DifferenceType']:<8} {diff['PropertyPath']}: template {diff['ExpectedValue']!r} / live {diff['ActualValue']!r}")
