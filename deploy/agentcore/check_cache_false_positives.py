"""Batch false-positive check for the semantic answer cache (a2k/gateway/cache.py),
driven by an xlsx test set shaped like Set_Pruebas_Cache_Falsos_Positivos_v1.xlsx: a
"Cache_test_set" sheet with columns No/Grupo (ID GT v4.1)/Tema/Idioma/Tipo/Patron de
confusion/Pregunta -- one "Pregunta real (cacheada)" row per group, followed by that
group's "Similar (riesgo de falso positivo)" rows (Grupo/Tema/Idioma only populated on
the real row; forward-filled here the same way a human reading the sheet would).

For each group, runs the same two gates lookup() runs (a2k/gateway/cache.py) between
the group's real question and each of its "similar" variants -- check_cache_similarity.py
does this for a single pair typed on the command line; this is the batch version over
an entire test set, written to a CSV report:
  1. Cosine-similarity pre-filter (config.answer_cache_similarity_threshold, 0.90 by
     default).
  2. LLM equivalence check (same _EQUIVALENCE_PROMPT/_converse_json lookup() itself
     uses) -- the real precision filter. Unlike cache.py's own _questions_equivalent()
     wrapper, this script keeps the model's own "reason" text instead of discarding it,
     since that's exactly what's useful to read in a report like this one.

Every row in this test set is, by construction, a "Similar (riesgo de falso positivo)"
case -- textually close to its group's real question but a different question in
substance (different entity, inverted relation, negation, different time period, ...).
The correct outcome for every single one is "would NOT hit the cache". A row where both
gates clear anyway (`clears_threshold=True` AND `llm_equivalent=True`) is a genuine false
positive: this pair would have silently served the wrong cached answer in production.

Does NOT run the second LLM gate (_answer_satisfies) -- that needs an actual cached
*answer*, not just the two questions, and this test set is specifically about whether
these question pairs get taken as equivalent in the first place; a real cached answer
would still have to independently satisfy the new question even for a pair this script
flags. Does NOT call a2k.ask and does NOT touch the real DynamoDB cache table -- this is
a pure diagnostic over the matching logic, safe to run against real question pairs
without any risk of actually caching or serving anything.

Usage:
    pip install openpyxl  # one-off, not a project dependency -- see requirements.txt
    python check_cache_false_positives.py Set_Pruebas_Cache_Falsos_Positivos_v1.xlsx

Needs AWS credentials with bedrock:InvokeModel + bedrock:Converse, same as
check_cache_similarity.py, in whatever region AWS_REGION points at.
"""

from __future__ import annotations

import csv
import os
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import boto3  # noqa: E402
import openpyxl  # noqa: E402

from a2k.config import config  # noqa: E402
from a2k.gateway.cache import (  # noqa: E402
    _EQUIVALENCE_PROMPT,
    _converse_json,
    _cosine_similarity,
    _normalize_query,
    embed_query,
)

REGION = os.environ.get("AWS_REGION", "eu-west-1")
REAL_TYPE = "Pregunta real (cacheada)"


def _load_rows(xlsx_path: str, sheet: str) -> list[dict]:
    """Reads the test-set sheet, forward-filling Grupo/Tema/Idioma (only set on each
    group's real-question row) and carrying the current group's real-question text
    onto every row that follows it, up to the next real-question row."""
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb[sheet]
    rows = list(ws.iter_rows(values_only=True))
    header, data_rows = rows[0], rows[1:]
    idx = {name: i for i, name in enumerate(header)}

    # The pattern column's own header differs between test sets ("Patrón de
    # confusión" for false positives, "Patrón de parafraseo" for false
    # negatives) -- found by elimination instead of hardcoding either string,
    # so this loader works for both (and any future sibling test set) without
    # edits.
    _FIXED_COLUMNS = {"Nº", "Grupo (ID GT v4.1)", "Tema", "Idioma", "Tipo", "Pregunta"}
    pattern_column = next(name for name in header if name not in _FIXED_COLUMNS)

    out = []
    group, tema, idioma, real_question = None, None, None, None
    for row in data_rows:
        if row[idx["Nº"]] is None:
            continue
        grupo_cell = row[idx["Grupo (ID GT v4.1)"]]
        tema_cell = row[idx["Tema"]]
        idioma_cell = row[idx["Idioma"]]
        tipo = row[idx["Tipo"]]
        pattern = row[idx[pattern_column]]
        question = row[idx["Pregunta"]]

        if grupo_cell is not None:
            group, tema, idioma = grupo_cell, tema_cell, idioma_cell
        if tipo == REAL_TYPE:
            real_question = question

        out.append(
            {
                "n": row[idx["Nº"]],
                "group": group,
                "tema": tema,
                "idioma": idioma,
                "tipo": tipo,
                "pattern": pattern,
                "question": question,
                "real_question": real_question,
            }
        )
    return out


def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: python {os.path.basename(__file__)} <test-set.xlsx> [--sheet Cache_test_set] [--output report.csv]", file=sys.stderr)
        raise SystemExit(2)

    xlsx_path = sys.argv[1]
    sheet = "Cache_test_set"
    output_path = os.path.splitext(os.path.basename(xlsx_path))[0] + "_report.csv"
    args = sys.argv[2:]
    while args:
        flag, args = args[0], args[1:]
        if flag == "--sheet":
            sheet, args = args[0], args[1:]
        elif flag == "--output":
            output_path, args = args[0], args[1:]
        else:
            print(f"Unrecognized argument: {flag}", file=sys.stderr)
            raise SystemExit(2)

    rows = _load_rows(xlsx_path, sheet)
    similar_rows = [r for r in rows if r["tipo"] != REAL_TYPE]
    print(f"Loaded {len(rows)} row(s) ({len(similar_rows)} 'similar' pairs to test) from {xlsx_path!r}, sheet {sheet!r}")
    print(f"Threshold: {config.answer_cache_similarity_threshold:.2f}  |  Embedding model: {config.answer_cache_embedding_model_id}  |  Verify model: {config.answer_cache_verify_model_id}")
    print()

    bedrock = boto3.client("bedrock-runtime", region_name=REGION)
    threshold = config.answer_cache_similarity_threshold

    results = []
    false_positives = 0
    for i, r in enumerate(similar_rows, 1):
        real_q = _normalize_query(r["real_question"])
        new_q = _normalize_query(r["question"])

        embedding_real = embed_query(bedrock, real_q)
        embedding_new = embed_query(bedrock, new_q)
        similarity = _cosine_similarity(embedding_real, embedding_new)
        clears_threshold = similarity >= threshold

        equivalent, reason = False, "(skipped: cosine pre-filter already rejects this pair)"
        if clears_threshold:
            prompt = _EQUIVALENCE_PROMPT.format(cached_query=real_q, new_query=new_q)
            parsed = _converse_json(bedrock, prompt)
            equivalent = bool(parsed.get("equivalent", False))
            reason = parsed.get("reason", "")

        is_false_positive = clears_threshold and equivalent
        if is_false_positive:
            false_positives += 1

        status = "FALSE POSITIVE" if is_false_positive else "ok (correctly rejected)"
        print(f"[{i}/{len(similar_rows)}] #{r['n']} grupo={r['group']} ({r['pattern']}): cos={similarity:.4f} equivalent={equivalent} -> {status}")

        results.append(
            {
                "n": r["n"],
                "grupo": r["group"],
                "tema": r["tema"],
                "idioma": r["idioma"],
                "patron_confusion": r["pattern"],
                "pregunta_real": r["real_question"],
                "pregunta_similar": r["question"],
                "cosine_similarity": f"{similarity:.4f}",
                "clears_threshold": clears_threshold,
                "llm_equivalent": equivalent,
                "llm_reason": reason,
                "falso_positivo": is_false_positive,
            }
        )

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        writer.writeheader()
        writer.writerows(results)

    print()
    print(f"=== Summary: {false_positives}/{len(similar_rows)} false positive(s) -- report written to {output_path}")


if __name__ == "__main__":
    main()
