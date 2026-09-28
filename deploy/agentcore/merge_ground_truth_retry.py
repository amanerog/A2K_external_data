"""Folds a retry run's output (run_ground_truth_eval.py --ids ... --output retry.csv,
re-run for rows that failed with an upstream/call-level error the first time) back
into the original results CSV -- replaces each row in `base` whose `id` also appears
in `retry`, but only when the retry row actually succeeded this time (no
UPSTREAM_ERROR in its own `error` column); a retry that failed again leaves the
original (still-failed) row untouched rather than overwriting a real error with
another one, so nothing about the original failure gets silently lost.

Usage:
    python merge_ground_truth_retry.py ground_truth_v3_cala_results.csv ground_truth_v3_cala_retry_results.csv
    python merge_ground_truth_retry.py base.csv retry.csv --output merged.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

_UPSTREAM_ERROR_PREFIX = '{"code": "UPSTREAM_ERROR"'


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("base", help="Original results CSV")
    parser.add_argument("retry", help="Retry run's output CSV (same columns, subset of ids)")
    parser.add_argument("--output", default=None, help="Where to write the merged CSV (default: overwrite base)")
    args = parser.parse_args()

    base_path, retry_path = Path(args.base), Path(args.retry)
    for p in (base_path, retry_path):
        if not p.exists():
            print(f"error: {p} not found", file=sys.stderr)
            sys.exit(1)
    output_path = Path(args.output) if args.output else base_path

    with base_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        base_rows = list(reader)

    with retry_path.open(newline="", encoding="utf-8") as f:
        retry_rows = {r["id"]: r for r in csv.DictReader(f)}

    replaced, still_failing, not_in_retry = 0, 0, 0
    merged = []
    for row in base_rows:
        retry_row = retry_rows.pop(row["id"], None)
        if retry_row is None:
            merged.append(row)
            continue
        if retry_row.get("error", "").startswith(_UPSTREAM_ERROR_PREFIX):
            still_failing += 1
            merged.append(row)  # keep the original failure, don't overwrite with another failure
        else:
            replaced += 1
            merged.append(retry_row)

    not_in_retry = len(retry_rows)  # any retry ids that didn't match a base row at all

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(merged)

    print(f"Merged: {replaced} row(s) replaced with successful retries, {still_failing} still failing (kept original), {len(base_rows)} total -- wrote {output_path}")
    if not_in_retry:
        print(f"Note: {not_in_retry} id(s) in {retry_path} had no matching row in {base_path} -- ignored")


if __name__ == "__main__":
    main()
