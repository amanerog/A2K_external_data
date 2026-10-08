"""Validates the K2-facing KB Card (a2k/cards/kcp01/service_card.json, the one
a2k://card serves) and its EndpointBinding against the A2K 0.1 JSON Schemas of
the reference package (sgt-ap11702-a2k).

Usage:
    python deploy/k2/validate.py --a2k-repo ../sgt-ap11702-a2k-main
(or with the `a2k` package installed: python deploy/k2/validate.py)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILES = {
    "kb-card": HERE.parent.parent / "a2k" / "cards" / "kcp01" / "service_card.json",
    "endpoint-binding": HERE / "endpoint_binding.a2k-0.1.json",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--a2k-repo", type=Path, help="clone of sgt-ap11702-a2k, if the a2k package is not installed")
    args = parser.parse_args()
    if args.a2k_repo:
        sys.path.insert(0, str(args.a2k_repo / "src"))
    from a2k_kbcard.schema.validation import SchemaValidationError, validate_by_name

    failed = False
    for schema, path in FILES.items():
        try:
            validate_by_name(json.loads(path.read_text(encoding="utf-8")), schema)
            print(f"OK       {path.name} ({schema})")
        except SchemaValidationError as exc:
            failed = True
            print(f"INVALID  {path.name} ({schema})")
            for error in exc.errors:
                print(f"         {error['instancePath'] or '/'}: {error['message']}")
    # Validator-side rule (EndpointBinding): resource is REQUIRED when auth.mode is not none.
    card = json.loads(FILES["kb-card"].read_text(encoding="utf-8"))
    binding = json.loads(FILES["endpoint-binding"].read_text(encoding="utf-8"))
    if card["auth"]["mode"] != "none" and "resource" not in binding:
        failed = True
        print("INVALID  binding needs `resource` when auth.mode is not none")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
