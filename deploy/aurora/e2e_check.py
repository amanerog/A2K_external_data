"""End-to-end check of the cache against the real Aurora (Data API) and S3 --
the part the unit tests cannot cover (they use MemoryStore).

Part 1, no models: a synthetic entry through every SQL path the code uses --
create PENDING, is_pending, conditional activate (twice: the second must be a
no-op, that is T6), the pgvector search with its filters, the two policy
tables, delete_pending on a non-PENDING row (must be a no-op), the session
memory (record_turn / last_turn) and deprecation by reformulation.

Part 2, with models: real admission of a few POC answers (S3 evidence +
PENDING row + classifier + judge + translation + embedding + activation),
then a real retrieval of the same question, which should find each admitted
entry. T5 is skipped, as in run_admission_offline.py: the POC .jsonl files
have no citations.

Everything it creates is deleted at the end (rows and S3 objects) unless
--keep is given. Synthetic rows use embedding_model "e2e-synthetic", so real
retrieval never sees them even mid-run.

Usage (from the repo root):
    python deploy/aurora/e2e_check.py --cluster-arn $AURORA_CLUSTER_ARN \\
        --secret-arn $AURORA_SECRET_ARN --bucket $CACHE_EVIDENCE_BUCKET \\
        --jsonl <path>/ground_truth_pocv3_cala_v6_results.jsonl [--ids 38,1] [--skip-models] [--keep]
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
for path in (_REPO_ROOT, os.path.join(_REPO_ROOT, "deploy", "agentcore")):
    if path not in sys.path:
        sys.path.insert(0, path)

import boto3  # noqa: E402

from a2k.cache import admission, retrieval  # noqa: E402
from a2k.cache.models import Candidate  # noqa: E402
from a2k.cache.store import Activation, AuroraStore, S3EvidenceStore  # noqa: E402
from a2k.config import config  # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{('  -- ' + detail) if detail else ''}", flush=True)
    if not ok:
        FAILURES.append(name)


def part1_sql(store: AuroraStore) -> list[str]:
    print("\n== Part 1: SQL paths through the Data API (no models)")
    entry_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    candidate = Candidate(
        entry_id=entry_id,
        question="e2e synthetic question ¿ñ?",
        answer="e2e synthetic answer with 'quotes' and ñ",
        vendor="e2e",
        tool_calls=[],
        citations=[],
        created_at=now,
    )
    vector = [1 / math.sqrt(1024)] * 1024

    store.create_pending(candidate, "s3://e2e/none.json")
    check("create_pending + is_pending", store.is_pending(entry_id))

    activation = Activation(
        entry_id=entry_id,
        question_text="e2e synthetic question",
        question_vector=vector,
        answer_text="e2e synthetic answer",
        original_lang="es",
        sources=[{"title": "e2e", "sourceUrl": "https://example.com/e2e"}],
        domain_primary="REG",
        domain_secondary=None,
        requested_elements=["one", "two"],
        time_anchor={"type": "none"},
        data_as_of=None,
        expires_at=now + timedelta(days=1),
        judge_findings={"e2e": True},
        embedding_model="e2e-synthetic",
        admission_versions={"e2e": "1"},
        entities=["E2E Corp", "Ñandú S.A."],
    )
    check("activate (conditional UPDATE) succeeds on PENDING", store.activate(activation))
    check("activate again is a no-op (T6)", not store.activate(activation))
    check("is_pending is false once ACTIVE", not store.is_pending(entry_id))
    check("delete_pending does not touch an ACTIVE row", not store.delete_pending(entry_id))

    rows = store.search(vector, min_cosine=0.9, embedding_model="e2e-synthetic", limit=5)
    found = [r for r in rows if r["entry_id"] == entry_id]
    check("pgvector search finds the entry", bool(found), f"{len(rows)} row(s)")
    if found:
        r = found[0]
        check("cosine ~1.0", abs(r["cosine"] - 1.0) < 1e-4, f"{r['cosine']:.6f}")
        check("original answer round-trips (unicode, quotes)", r["answer_text_original"] == candidate.answer)
        check("sources jsonb round-trips", r["sources"] == activation.sources)
        check("admitted_at set by the database", r["admitted_at"] is not None)
    check("search filters by embedding model", not store.search(vector, min_cosine=0.9, embedding_model="other-model", limit=5))

    session = f"e2e-{uuid.uuid4()}"
    try:
        store.record_turn(session, "e2e question ñ", entry_id, window_seconds=600)
        turn = store.last_turn(session, window_seconds=600)
        check("record_turn + last_turn (session memory)", bool(turn) and turn["served_entry_id"] == entry_id and turn["last_query_text"] == "e2e question ñ")
        store.record_turn(session, "e2e question 2", None, window_seconds=600)
        turn = store.last_turn(session, window_seconds=600)
        check("record_turn overwrites the session's row (null entry)", bool(turn) and turn["served_entry_id"] is None)
        check("last_turn ignores rows outside the window", store.last_turn(session, window_seconds=0) is None)
        check("deprecate an ACTIVE entry (reformulation)", store.deprecate(entry_id, "immediate_reformulation"))
        check("deprecate again is a no-op", not store.deprecate(entry_id, "immediate_reformulation"))
        check("a deprecated entry is not searchable", not [r for r in store.search(vector, min_cosine=0.9, embedding_model="e2e-synthetic", limit=5) if r["entry_id"] == entry_id])
    finally:
        store._execute("DELETE FROM session_last_turn WHERE session_id = :s", [{"name": "s", "value": {"stringValue": session}}])

    policy = store.retrieval_policy()
    check("retrieval_policy has the 7 domains (D3)", len(policy) == 7 and all(abs(v - 0.90) < 1e-6 for v in policy.values()), str(policy))
    expiry, version = store.expiry_policy()
    check("expiry_policy readable (D6)", expiry["MED"] == timedelta(days=1) and expiry["historical"] == timedelta(days=365), version)
    return [entry_id]


def part3_reformulation(store_admission, store_retrieval, bedrock) -> list[str]:
    """Retrieval step 1 with real models (Haiku classifier, Titan, the Checker):
    a real, searchable entry is planted, then a session asks it, follows up,
    and reformulates. Returns the entry id for cleanup."""
    from a2k.cache.bedrock import embed

    print("\n== Part 3: reformulation with real models (no admission, no S3)")
    settings = retrieval.RetrievalSettings(
        light_model_id=config.admission_light_model_id,
        checker_model_id=config.cache_checker_model_id,
        embedding_model_id=config.cache_embedding_model_id,
    )
    q1 = "What is the registered address of Nu Pagamentos S.A. (CNPJ 18.236.120/0001-58) according to Receita Federal?"
    answer = (
        "According to Receita Federal do Brasil, Nu Pagamentos S.A. (CNPJ 18.236.120/0001-58) is registered at "
        "Rua Capote Valente, 120, Pinheiros, São Paulo/SP, CEP 05409-000. Its cadastral status is Ativa."
    )
    entry_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    store_admission.create_pending(
        Candidate(entry_id=entry_id, question=q1, answer=answer, vendor="e2e", tool_calls=[], citations=[], created_at=now),
        "s3://e2e/none.json",
    )
    store_admission.activate(
        Activation(
            entry_id=entry_id,
            question_text=q1,
            question_vector=embed(bedrock, model_id=config.cache_embedding_model_id, text=q1),
            answer_text=answer,
            original_lang="en",
            sources=[{"title": "Receita Federal", "sourceUrl": "https://solucoes.receita.fazenda.gov.br/"}],
            domain_primary="CRP",
            domain_secondary=None,
            requested_elements=["registered address"],
            time_anchor={"type": "relative"},
            data_as_of=None,
            expires_at=now + timedelta(hours=1),
            judge_findings={"e2e": True},
            embedding_model=config.cache_embedding_model_id,
            admission_versions={"e2e": "1"},
            entities=["Nu Pagamentos S.A."],
        )
    )
    session = f"e2e-{uuid.uuid4()}"
    window = settings.reformulation_window_seconds
    try:
        first = retrieval.lookup(q1, bedrock_client=bedrock, store=store_retrieval, settings=settings, session_id=session)
        check("first turn: no previous turn in the session", first.reformulation == {"previous_turn": False}, str(first.reformulation))
        check("first turn: the planted entry is a hit", first.hit is not None and first.hit.entry_id == entry_id, f"error={first.error} checks={first.checks}")
        store_admission.record_turn(session, q1, entry_id, window_seconds=window)

        follow = retrieval.lookup(
            "And what is its main registered economic activity?", bedrock_client=bedrock, store=store_retrieval, settings=settings, session_id=session
        )
        r = follow.reformulation or {}
        check("follow-up is classified as such and deprecates nothing", r.get("kind") == "follow_up" and not r.get("deprecated"), str(r))
        store_admission.record_turn(session, q1, entry_id, window_seconds=window)

        reask = retrieval.lookup(
            "That's not what I asked -- I need the address registered for Nu Pagamentos, CNPJ 18.236.120/0001-58.",
            bedrock_client=bedrock,
            store=store_retrieval,
            settings=settings,
            session_id=session,
        )
        r = reask.reformulation or {}
        check("reformulation is classified as such", r.get("kind") == "reformulation", str(r))
        check("the served entry was deprecated", r.get("deprecated") is True)
        check("the deprecated entry is not served again", not [c for c in reask.candidates if c["entry_id"] == entry_id], str(reask.candidates))
        status = store_admission._execute(
            "SELECT status, status_reason FROM cache_entry WHERE entry_id = CAST(:id AS uuid)", [{"name": "id", "value": {"stringValue": entry_id}}]
        )["records"][0]
        check("status_reason immediate_reformulation", [c.get("stringValue") for c in status] == ["DEPRECATED", "immediate_reformulation"], str(status))
        print(f"        timings: first {first.timings_ms} | reformulation {reask.timings_ms}")
    finally:
        store_admission._execute("DELETE FROM session_last_turn WHERE session_id = :s", [{"name": "s", "value": {"stringValue": session}}])
    return [entry_id]


def load_records(path: str, ids: list[str]) -> list[dict]:
    wanted = set(ids)
    return [r for r in map(json.loads, open(path, encoding="utf-8")) if r["id"] in wanted and r.get("ok") and r.get("answer")]


def part2_models(store_admission, store_retrieval, evidence_store, bedrock, records, confirm_fit=True) -> tuple[list[str], list[str]]:
    print("\n== Part 2: real admission + retrieval with models")
    settings = admission.AdmissionSettings(
        light_model_id=config.admission_light_model_id,
        judge_model_id=config.admission_judge_model_id,
        embedding_model_id=config.cache_embedding_model_id,
        check_sources=False,
        confirm_fit=confirm_fit,
    )
    rsettings = retrieval.RetrievalSettings(
        light_model_id=config.admission_light_model_id,
        checker_model_id=config.cache_checker_model_id,
        embedding_model_id=config.cache_embedding_model_id,
    )
    entry_ids, keys = [], []
    for record in records:
        candidate = Candidate(
            entry_id=str(uuid.uuid4()),
            question=record["query"],
            answer=record["answer"],
            vendor="cala",
            tool_calls=record.get("toolCalls") or [],
            citations=record.get("citations") or [],
            created_at=datetime.now(timezone.utc),
        )
        entry_ids.append(candidate.entry_id)
        keys.append(f"cache-evidence/{candidate.created_at:%Y/%m}/{candidate.entry_id}.json")
        print(f"\n  id {record['id']}: {record['query'][:80]}")
        result = admission.admit(candidate, bedrock_client=bedrock, store=store_admission, evidence_store=evidence_store, settings=settings)
        check(f"id {record['id']} admission ran without ERROR", result.outcome != "ERROR", f"{result.outcome}: {result.reason[:120]}")
        if result.outcome != "ACTIVE":
            print("        (not admitted, so no retrieval to check -- pick another id if you want this path covered)")
            continue
        lookup = retrieval.lookup(record["query"], bedrock_client=bedrock, store=store_retrieval, settings=rsettings)
        hit = lookup.hit
        check(
            f"id {record['id']} retrieval finds its own entry",
            hit is not None and hit.entry_id == candidate.entry_id,
            f"error={lookup.error} candidates={lookup.candidates} checks={[{k: v for k, v in c.items() if not k.endswith('reason')} for c in lookup.checks]}",
        )
        for c in lookup.checks:
            for key in ("equivalent", "satisfies"):
                if c.get(key) is False:
                    print(f"        Checker {key}=False: {c.get(key + '_reason')}")
        if hit:
            check(f"id {record['id']} served in the original language", hit.served_version == "original", f"{hit.served_version}, cosine {hit.cosine:.3f}")
        print(f"        timings: admission {result.timings_ms} | retrieval {lookup.timings_ms}")
    return entry_ids, keys


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cluster-arn", required=True)
    parser.add_argument("--secret-arn", required=True)
    parser.add_argument("--database", default="a2k_cache")
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--jsonl", help="POC results .jsonl for part 2")
    parser.add_argument("--ids", default="38,1", help="POC ids to admit in part 2 (38 and 1 were FIT offline)")
    parser.add_argument("--skip-models", action="store_true", help="skip part 2 (admission with models)")
    parser.add_argument("--reformulation", action="store_true", help="also run part 3: reformulation with real models")
    parser.add_argument("--keep", action="store_true", help="do not delete what was created")
    parser.add_argument(
        "--no-confirm",
        action="store_true",
        help="single judgement (no confirming second call): makes it likelier the retrieval path gets exercised",
    )
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "eu-west-1"))
    args = parser.parse_args()

    rds = boto3.client("rds-data", region_name=args.region)
    aurora = dict(rds_data_client=rds, cluster_arn=args.cluster_arn, secret_arn=args.secret_arn, database=args.database)
    store_admission = AuroraStore(**aurora, resume_retries=9)
    store_retrieval = AuroraStore(**aurora)
    s3 = boto3.client("s3", region_name=args.region)

    created_ids: list[str] = []
    created_keys: list[str] = []
    try:
        created_ids += part1_sql(store_admission)
        if args.reformulation:
            created_ids += part3_reformulation(store_admission, store_retrieval, boto3.client("bedrock-runtime", region_name=args.region))
        if not args.skip_models:
            if not args.jsonl:
                parser.error("--jsonl is required unless --skip-models")
            records = load_records(args.jsonl, args.ids.split(","))
            check("POC records loaded", len(records) == len(args.ids.split(",")), f"{len(records)} found")
            ids, keys = part2_models(
                store_admission,
                store_retrieval,
                S3EvidenceStore(s3_client=s3, bucket=args.bucket),
                boto3.client("bedrock-runtime", region_name=args.region),
                records,
                confirm_fit=not args.no_confirm,
            )
            created_ids += ids
            created_keys += keys
            for key in keys:
                try:
                    s3.head_object(Bucket=args.bucket, Key=key)
                    check(f"evidence in S3: {key}", True)
                except Exception as exc:  # noqa: BLE001
                    check(f"evidence in S3: {key}", False, type(exc).__name__)
    finally:
        if args.keep:
            print(f"\n--keep: left {len(created_ids)} row(s) and {len(created_keys)} S3 object(s) in place: {created_ids}")
        else:
            for entry_id in created_ids:
                store_admission._execute("DELETE FROM cache_entry WHERE entry_id = CAST(:id AS uuid)", [{"name": "id", "value": {"stringValue": entry_id}}])
            for key in created_keys:
                s3.delete_object(Bucket=args.bucket, Key=key)
            print(f"\ncleanup: deleted {len(created_ids)} row(s) and {len(created_keys)} S3 object(s)")

    print(f"\n{'ALL CHECKS PASSED' if not FAILURES else f'{len(FAILURES)} CHECK(S) FAILED: ' + '; '.join(FAILURES)}")
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()
