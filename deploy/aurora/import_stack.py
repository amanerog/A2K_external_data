"""Imports the cache's Aurora cluster, its subnet group and security group and
the evidence bucket, created by hand (dev, 1 Oct 2026), into the stack of
cache-infra.yaml without changing them (E2.1). Every parameter is read from
the live resources, so the stack describes what exists; the closing drift
report says where the template and the live configuration still differ.

Usage (credentials for the account in the environment):
    python deploy/aurora/import_stack.py [--stack a2k-cache] [--cluster-id a2k-cache] \\
        --bucket a2k-cache-evidence-396961015428-euw1            # plan only
    python deploy/aurora/import_stack.py --bucket ... --apply
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import boto3

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from cfn_import import import_resources  # noqa: E402

TEMPLATE = HERE / "cache-infra.yaml"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stack", default="a2k-cache")
    parser.add_argument("--cluster-id", default="a2k-cache")
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "eu-west-1"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    rds = boto3.client("rds", region_name=args.region)
    cluster = rds.describe_db_clusters(DBClusterIdentifier=args.cluster_id)["DBClusters"][0]
    group = rds.describe_db_subnet_groups(DBSubnetGroupName=cluster["DBSubnetGroup"])["DBSubnetGroups"][0]
    (sg,) = [g["VpcSecurityGroupId"] for g in cluster["VpcSecurityGroups"]]
    (instance,) = [m["DBInstanceIdentifier"] for m in cluster["DBClusterMembers"]]
    scaling = cluster["ServerlessV2ScalingConfiguration"]
    boto3.client("s3", region_name=args.region).head_bucket(Bucket=args.bucket)

    expected = {"subnet group": (group["DBSubnetGroupName"], f"{args.cluster_id}-subnets"), "instance": (instance, f"{args.cluster_id}-1")}
    for what, (live, template) in expected.items():
        if live != template:
            sys.exit(f"{what} is {live!r}; cache-infra.yaml names it {template!r} -- adjust the template first")

    parameters = {
        "ClusterId": args.cluster_id,
        "EngineVersion": cluster["EngineVersion"],
        "VpcId": group["VpcId"],
        "SubnetIds": ",".join(s["SubnetIdentifier"] for s in group["Subnets"]),
        "MinCapacity": f"{scaling['MinCapacity']:g}",
        "MaxCapacity": f"{scaling['MaxCapacity']:g}",
        "SecondsUntilAutoPause": str(scaling.get("SecondsUntilAutoPause", 600)),
        "DeletionProtection": str(cluster["DeletionProtection"]).lower(),
        "EvidenceBucketName": args.bucket,
    }
    print(f"Cluster {cluster['DBClusterArn']} ({cluster['EngineVersion']}), instance {instance}, security group {sg}")
    import_resources(
        boto3.client("cloudformation", region_name=args.region),
        stack=args.stack,
        template=TEMPLATE.read_text(encoding="utf-8"),
        parameters=parameters,
        resources=[
            {"ResourceType": "AWS::RDS::DBSubnetGroup", "LogicalResourceId": "SubnetGroup", "ResourceIdentifier": {"DBSubnetGroupName": group["DBSubnetGroupName"]}},
            {"ResourceType": "AWS::EC2::SecurityGroup", "LogicalResourceId": "SecurityGroup", "ResourceIdentifier": {"Id": sg}},
            {"ResourceType": "AWS::RDS::DBCluster", "LogicalResourceId": "Cluster", "ResourceIdentifier": {"DBClusterIdentifier": args.cluster_id}},
            {"ResourceType": "AWS::RDS::DBInstance", "LogicalResourceId": "Instance", "ResourceIdentifier": {"DBInstanceIdentifier": instance}},
            {"ResourceType": "AWS::S3::Bucket", "LogicalResourceId": "EvidenceBucket", "ResourceIdentifier": {"BucketName": args.bucket}},
            {"ResourceType": "AWS::S3::BucketPolicy", "LogicalResourceId": "EvidenceBucketPolicy", "ResourceIdentifier": {"Bucket": args.bucket}},
        ],
        apply=args.apply,
    )


if __name__ == "__main__":
    main()
