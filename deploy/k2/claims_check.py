"""How the agent's verbatim claims and coverage turn out on a deployed Runtime.

For each question, one internal.ask call (A2K_INTERNAL_TOOLS=true on the
Runtime): the internal envelope carries the agent's claims and coverage as it
wrote them. Each is then put through the A2K 0.1 adapter locally -- the same
code a2k.ask runs -- and the 0.1 envelope is validated. Reported per question
and in total: claims written, claims located in the answer, claims citing a
usable source, coverage, the 0.1 outcome and grounding.

Usage:
    export A2K_BOX_RUNTIME_ARN=...
    python deploy/k2/claims_check.py --a2k-repo ../sgt-ap11702-a2k-main \\
        --gt ../A2K_external_data/deploy/agentcore/Ejecutable_GT_POCv3.csv --ids 1,13,38,54,57 \\
        [--sources cala] --output claims_check.jsonl
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent))

from smoke_kcp01 import Session  # noqa: E402


def questions(path: Path, ids: list[str]) -> dict[str, str]:
    text = path.read_text(encoding="utf-8-sig")
    rows = csv.reader(text.splitlines(), delimiter=";")
    next(rows)
    found = {row[0]: row[2] for row in rows if row and row[0] in ids}
    return {i: found[i] for i in ids if i in found}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--a2k-repo", type=Path)
    parser.add_argument("--runtime-arn", default=os.environ.get("A2K_BOX_RUNTIME_ARN"))
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "eu-west-1"))
    parser.add_argument("--gt", type=Path, required=True, help="Ejecutable_GT_POCv3.csv (;-separated: ID;Use case;question;...)")
    parser.add_argument("--ids", required=True)
    parser.add_argument("--sources", default=None, help="force vendors, e.g. cala (default: the agent picks)")
    parser.add_argument("--output", default="claims_check.jsonl")
    args = parser.parse_args()
    if not args.runtime_arn:
        sys.exit("set A2K_BOX_RUNTIME_ARN or --runtime-arn")
    if args.a2k_repo:
        sys.path.insert(0, str(args.a2k_repo / "src"))
    from a2k_kbcard.schema.validation import SchemaValidationError, validate_by_name

    from a2k.kcp01 import adapter
    from a2k.kcp01.adapter import AskRequest, _locate
    from a2k.models.envelope import CitedResponseEnvelope

    session = Session(args.runtime_arn, args.region)
    totals: Counter = Counter()
    with open(args.output, "w", encoding="utf-8") as out:
        for qid, query in questions(args.gt, args.ids.split(",")).items():
            arguments = {"query": query}
            if args.sources:
                arguments["sources"] = args.sources.split(",")
            t0 = time.monotonic()
            try:
                internal = session.call("internal.ask", arguments)[0]
            except Exception as exc:  # noqa: BLE001
                print(f"id={qid} ERROR {exc}")
                continue
            seconds = time.monotonic() - t0
            env = CitedResponseEnvelope.model_validate(internal)
            answer = env.answer or ""
            usable = {c["id"] for c in adapter._citations(env)}
            located = [c for c in env.claims if _locate(c.text, answer)]
            with_source = [c for c in located if any(cid in usable for cid in c.citationIds)]
            request = AskRequest.model_validate({"a2kVersion": "0.1", "operation": "ask", "requestId": f"cc-{qid}", "query": query})
            kcp = adapter.ask_response(request, env)
            try:
                validate_by_name(kcp, "ask-response")
                valid = True
            except SchemaValidationError:
                valid = False
            data = kcp.get("data") or {}
            row = {
                "id": qid, "seconds": round(seconds, 1), "coverage": env.coverage, "missing": env.missing,
                "claims": len(env.claims), "located": len(located), "with_source": len(with_source),
                "outcome": data.get("outcome") or (kcp.get("error") or {}).get("code"),
                "grounding": (data.get("grounding") or {}).get("status"), "valid": valid,
                "unlocated": [c.text for c in env.claims if c not in located][:5],
            }
            out.write(json.dumps({**row, "kcp01": kcp}, ensure_ascii=False) + "\n")
            print(f"id={qid} {row['seconds']}s coverage={row['coverage']} claims {row['located']}/{row['claims']} located, "
                  f"{row['with_source']} with a usable source -> {row['outcome']}, grounding={row['grounding']}, valid={valid}", flush=True)
            totals.update(claims=row["claims"], located=row["located"], with_source=row["with_source"], answers=1, valid=valid)
            totals[f"outcome:{row['outcome']}"] += 1
    if totals["answers"]:
        print(f"\n{totals['answers']} answers, claims located {totals['located']}/{totals['claims']} "
              f"({totals['located'] / max(1, totals['claims']):.0%}), with a usable source {totals['with_source']}, "
              f"valid 0.1 {totals['valid']}/{totals['answers']}, "
              + ", ".join(f"{k[8:]}={v}" for k, v in totals.items() if k.startswith("outcome:")))


if __name__ == "__main__":
    main()
