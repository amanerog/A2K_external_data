"""Seeds/manages the DynamoDB table a2k/cards/__init__.py reads vendor KB
Cards from (see that module's docstring, and the top-level README's "Vendor
cards from DynamoDB"). Schema: partition key `sourceId` (String), one
attribute `cardJson` per item holding the full KBCard as a JSON string --
validated against the same `KBCard` pydantic model the box itself uses
before writing, so a malformed card fails here, not at MCP-serving time.

Usage:
    # One-off: create the table (on-demand billing, no capacity to size).
    python deploy/seed_vendor_cards.py --table a2k-vendor-cards --create-table

    # Migrate the two vendors already shipped as local JSON files
    # (a2k/cards/cala_card.json, a2k/cards/sayari_card.json) into the table --
    # the normal way to get started once the table exists.
    python deploy/seed_vendor_cards.py --table a2k-vendor-cards --seed-existing

    # Add (or update) one vendor from an arbitrary KBCard JSON file --
    # this is the "add a vendor with zero code changes" workflow the whole
    # feature exists for.
    python deploy/seed_vendor_cards.py --table a2k-vendor-cards \\
        --card path/to/newvendor_card.json --source-id newvendor

    # List what's currently in the table.
    python deploy/seed_vendor_cards.py --table a2k-vendor-cards --list

Table name can also come from VENDOR_CARDS_TABLE (same env var the box
itself reads) instead of --table.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import boto3

REPO_ROOT = Path(__file__).resolve().parent.parent
CARDS_DIR = REPO_ROOT / "a2k" / "cards"

_LOCAL_VENDOR_CARD_FILES = {
    "cala": "cala_card.json",
    "sayari": "sayari_card.json",
}


def _validate_card(card: dict, *, source: str) -> None:
    """Validates against the box's own KBCard model -- imported lazily so
    this script only needs `a2k` importable (repo root on sys.path), not a
    full runtime environment, for the parts that don't need it (--list)."""
    sys.path.insert(0, str(REPO_ROOT))
    from a2k.models.kbcard import KBCard

    try:
        KBCard.model_validate(card)
    except Exception as exc:
        raise SystemExit(f"error: {source} is not a valid KBCard: {exc}")


def _create_table(dynamodb, table_name: str) -> None:
    print(f"Creating table {table_name!r} (on-demand billing, partition key sourceId)...")
    table = dynamodb.create_table(
        TableName=table_name,
        KeySchema=[{"AttributeName": "sourceId", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "sourceId", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    table.wait_until_exists()
    print("Table active.")


def _put_card(dynamodb, table_name: str, source_id: str, card: dict) -> None:
    _validate_card(card, source=source_id)
    dynamodb.Table(table_name).put_item(Item={"sourceId": source_id, "cardJson": json.dumps(card)})
    print(f"Put sourceId={source_id!r} (name={card.get('name')!r}, status={card['enterprise']['lifecycle']['status']!r})")


def _seed_existing(dynamodb, table_name: str) -> None:
    for source_id, filename in _LOCAL_VENDOR_CARD_FILES.items():
        with open(CARDS_DIR / filename, encoding="utf-8") as fh:
            card = json.load(fh)
        _put_card(dynamodb, table_name, source_id, card)


def _list_cards(dynamodb, table_name: str) -> None:
    table = dynamodb.Table(table_name)
    scan_kwargs: dict = {}
    count = 0
    while True:
        response = table.scan(**scan_kwargs)
        for item in response.get("Items", []):
            card = json.loads(item["cardJson"])
            status = card.get("enterprise", {}).get("lifecycle", {}).get("status")
            print(f"  {item['sourceId']:15s} name={card.get('name')!r:40s} status={status!r}")
            count += 1
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key
    print(f"{count} card(s) in {table_name!r}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--table", default=os.environ.get("VENDOR_CARDS_TABLE"), help="DynamoDB table name")
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "eu-west-1",
        help="AWS region (default: AWS_REGION/AWS_DEFAULT_REGION env var, else eu-west-1 -- same region as every other AWS resource in this repo)",
    )
    parser.add_argument("--create-table", action="store_true", help="Create the table if it doesn't exist yet")
    parser.add_argument("--seed-existing", action="store_true", help="Load the local cala_card.json/sayari_card.json into the table")
    parser.add_argument("--card", help="Path to a KBCard JSON file to put (use with --source-id)")
    parser.add_argument("--source-id", help="sourceId to store --card under")
    parser.add_argument("--list", action="store_true", help="List every card currently in the table")
    args = parser.parse_args()

    if not args.table:
        print("error: --table or VENDOR_CARDS_TABLE is required", file=sys.stderr)
        sys.exit(1)
    if bool(args.card) != bool(args.source_id):
        print("error: --card and --source-id must be given together", file=sys.stderr)
        sys.exit(1)
    if not any([args.create_table, args.seed_existing, args.card, args.list]):
        print("error: nothing to do -- pass --create-table, --seed-existing, --card/--source-id, and/or --list", file=sys.stderr)
        sys.exit(1)

    dynamodb = boto3.resource("dynamodb", region_name=args.region)

    if args.create_table:
        _create_table(dynamodb, args.table)
    if args.seed_existing:
        _seed_existing(dynamodb, args.table)
    if args.card:
        with open(args.card, encoding="utf-8") as fh:
            card = json.load(fh)
        _put_card(dynamodb, args.table, args.source_id, card)
    if args.list:
        _list_cards(dynamodb, args.table)


if __name__ == "__main__":
    main()
