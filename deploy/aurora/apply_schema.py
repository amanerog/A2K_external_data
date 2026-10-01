"""Applies deploy/aurora/schema.sql through the RDS Data API -- no psql and no
network path into the VPC needed, the same way a2k-box itself reaches the
database. The Data API runs one statement per call, so the file is split on
statement-ending semicolons. Idempotent, like the schema itself.

Then checks what the cache code relies on: pgvector >= 0.8 (HNSW), the three
tables, and the seed policy rows.

Usage:
    python deploy/aurora/apply_schema.py --cluster-arn <arn> --secret-arn <arn> [--database a2k_cache]

The first call after an auto-pause (0 ACU) wakes the cluster up and fails
with DatabaseResumingException for a few seconds; it is retried.
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import boto3

SCHEMA = Path(__file__).with_name("schema.sql")


def statements(sql: str) -> list[str]:
    lines = [line for line in sql.splitlines() if not line.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";\n") if s.strip().rstrip(";").strip()]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cluster-arn", required=True)
    parser.add_argument("--secret-arn", required=True)
    parser.add_argument("--database", default="a2k_cache")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "eu-west-1"))
    args = parser.parse_args()

    client = boto3.client("rds-data", region_name=args.region)

    def run(sql: str) -> dict:
        for attempt in range(12):
            try:
                return client.execute_statement(
                    resourceArn=args.cluster_arn, secretArn=args.secret_arn, database=args.database, sql=sql
                )
            except client.exceptions.DatabaseResumingException:
                print(f"  cluster resuming from auto-pause, retrying ({attempt + 1})...", flush=True)
                time.sleep(10)
        raise RuntimeError("cluster did not resume")

    for stmt in statements(SCHEMA.read_text(encoding="utf-8").rstrip().rstrip(";") + ";\n"):
        first_line = stmt.splitlines()[0]
        run(stmt.rstrip(";"))
        print(f"ok  {first_line[:80]}", flush=True)

    version = run("SELECT extversion FROM pg_extension WHERE extname = 'vector'")["records"][0][0]["stringValue"]
    major, minor = (int(x) for x in version.split(".")[:2])
    print(f"\npgvector {version} {'OK' if (major, minor) >= (0, 8) else '-- TOO OLD, need >= 0.8 for HNSW filtering'}")
    for table in ("cache_entry", "expiry_policy", "retrieval_policy", "session_last_turn"):
        count = run(f"SELECT count(*) FROM {table}")["records"][0][0]["longValue"]
        print(f"{table:17s} {count} row(s)")


if __name__ == "__main__":
    main()
