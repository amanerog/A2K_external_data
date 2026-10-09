"""Where admission writes: the cache_entry row in Aurora PostgreSQL +
pgvector, and the evidence bundle in S3 (proposal Annex A1/A2).

Aurora is reached through the RDS Data API rather than a driver connection:
a2k-box runs on AgentCore Runtime in PUBLIC network mode, outside any VPC,
and the Data API is plain HTTPS + IAM (proposal Sheet 2 lists it alongside
RDS Proxy). It needs the cluster's HTTP endpoint enabled and the DB secret in
Secrets Manager -- see deploy/aurora/README.md.

Only two moments write a cache_entry row (Annex A4): `create_pending` when a
candidate is registered, and `activate`, a single conditional UPDATE that
only succeeds while the row is still PENDING. A rejected candidate is
removed with `delete_pending`, same condition.

`MemoryStore` implements the same interface in-process, for tests and for
offline runs over POC data.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol

from .expiry import default_policy
from .models import Candidate


@dataclass
class Activation:
    entry_id: str
    question_text: str
    question_vector: list[float]
    answer_text: str
    original_lang: str
    sources: list[dict[str, Any]]
    domain_primary: str
    domain_secondary: str | None
    requested_elements: list[Any]  # [{"id", "element", "expected_type", "constraints"}]
    time_anchor: dict[str, Any]
    data_as_of: date | None
    expires_at: datetime
    judge_findings: dict[str, Any]
    embedding_model: str
    admission_versions: dict[str, Any]
    entities: list[str]
    # {"original": [{"text", "sources"}], "en": [...]} -- see admission._stored_claims.
    claims: dict[str, Any] = field(default_factory=dict)


class CacheStore(Protocol):
    def create_pending(self, candidate: Candidate, evidence_uri: str) -> None: ...
    def is_pending(self, entry_id: str) -> bool: ...
    def delete_pending(self, entry_id: str) -> bool: ...
    def activate(self, activation: Activation) -> bool: ...
    def expiry_policy(self) -> tuple[dict[str, timedelta], str]: ...
    def retrieval_policy(self) -> dict[str, float]: ...
    def search(self, vector: list[float], *, min_cosine: float, embedding_model: str, limit: int) -> list[dict[str, Any]]: ...
    def deprecate(self, entry_id: str, reason: str) -> bool: ...
    def last_turn(self, session_id: str, *, window_seconds: float) -> dict[str, Any] | None: ...
    def record_turn(self, session_id: str, query: str, served_entry_id: str | None, *, window_seconds: float) -> None: ...
    def size(self) -> list[dict[str, Any]]: ...


class EvidenceStore(Protocol):
    def put(self, key: str, body: dict[str, Any]) -> str: ...


def _ts(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in vector) + "]"


def _field(cell: dict[str, Any]) -> Any:
    if cell.get("isNull"):
        return None
    for key in ("stringValue", "doubleValue", "longValue", "booleanValue"):
        if key in cell:
            return cell[key]
    return None


def _p(name: str, value: Any, type_hint: str | None = None) -> dict[str, Any]:
    if value is None:
        return {"name": name, "value": {"isNull": True}}
    param: dict[str, Any] = {"name": name, "value": {"stringValue": str(value)}}
    if type_hint:
        param["typeHint"] = type_hint
    return param


def _is_resuming(exc: Exception) -> bool:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code")
    return code == "DatabaseResumingException" or type(exc).__name__ == "DatabaseResumingException"


class AuroraStore:
    """`resume_retries`: the cluster auto-pauses at 0 ACU (10 min idle) and the
    first Data API call after a pause fails with DatabaseResumingException
    while it wakes up (~15 s). Admission runs in the background and can wait,
    so its store retries; retrieval is on the user's path and must not -- its
    store keeps the default 0 and a resuming database is simply a miss."""

    def __init__(
        self,
        *,
        rds_data_client,
        cluster_arn: str,
        secret_arn: str,
        database: str,
        resume_retries: int = 0,
        resume_wait_seconds: float = 10.0,
        sleep=time.sleep,
    ):
        self._client = rds_data_client
        self._cluster_arn = cluster_arn
        self._secret_arn = secret_arn
        self._database = database
        self._resume_retries = resume_retries
        self._resume_wait_seconds = resume_wait_seconds
        self._sleep = sleep

    def _execute(self, sql: str, params: list[dict[str, Any]]) -> dict[str, Any]:
        for attempt in range(self._resume_retries + 1):
            try:
                return self._client.execute_statement(
                    resourceArn=self._cluster_arn,
                    secretArn=self._secret_arn,
                    database=self._database,
                    sql=sql,
                    parameters=params,
                )
            except Exception as exc:
                if not _is_resuming(exc) or attempt == self._resume_retries:
                    raise
                self._sleep(self._resume_wait_seconds)
        raise AssertionError("unreachable")

    def create_pending(self, candidate: Candidate, evidence_uri: str) -> None:
        self._execute(
            """INSERT INTO cache_entry (entry_id, status, created_at, question_text_original,
                   answer_text_original, evidence_uri, vendor, data_origin, card_revision, grounded_ratio)
               VALUES (:entry_id, 'PENDING', :created_at, :question, :answer, :evidence_uri, :vendor, 'public',
                       :card_revision, CAST(:grounded_ratio AS real))""",
            [
                _p("entry_id", candidate.entry_id, "UUID"),
                _p("created_at", _ts(candidate.created_at), "TIMESTAMP"),
                _p("question", candidate.question),
                _p("answer", candidate.answer),
                _p("evidence_uri", evidence_uri),
                _p("vendor", candidate.vendor),
                _p("card_revision", candidate.card_revision),
                _p("grounded_ratio", candidate.grounded_ratio),
            ],
        )

    def is_pending(self, entry_id: str) -> bool:
        result = self._execute(
            "SELECT 1 FROM cache_entry WHERE entry_id = :entry_id AND status = 'PENDING'",
            [_p("entry_id", entry_id, "UUID")],
        )
        return bool(result.get("records"))

    def delete_pending(self, entry_id: str) -> bool:
        result = self._execute(
            "DELETE FROM cache_entry WHERE entry_id = :entry_id AND status = 'PENDING'",
            [_p("entry_id", entry_id, "UUID")],
        )
        return result.get("numberOfRecordsUpdated", 0) == 1

    def activate(self, a: Activation) -> bool:
        result = self._execute(
            """UPDATE cache_entry SET
                   status = 'ACTIVE',
                   admitted_at = now(),
                   question_text = :question_text,
                   question_vector = CAST(:question_vector AS vector),
                   answer_text = :answer_text,
                   original_lang = :original_lang,
                   sources = CAST(:sources AS jsonb),
                   domain_primary = :domain_primary,
                   domain_secondary = :domain_secondary,
                   requested_elements = CAST(:requested_elements AS jsonb),
                   time_anchor = CAST(:time_anchor AS jsonb),
                   data_as_of = :data_as_of,
                   expires_at = :expires_at,
                   judge_findings = CAST(:judge_findings AS jsonb),
                   embedding_model = :embedding_model,
                   admission_versions = CAST(:admission_versions AS jsonb),
                   entities = ARRAY(SELECT jsonb_array_elements_text(CAST(:entities AS jsonb))),
                   claims = CAST(:claims AS jsonb)
               WHERE entry_id = :entry_id AND status = 'PENDING'""",
            [
                _p("entry_id", a.entry_id, "UUID"),
                _p("question_text", a.question_text),
                _p("question_vector", _vector_literal(a.question_vector)),
                _p("answer_text", a.answer_text),
                _p("original_lang", a.original_lang),
                _p("sources", json.dumps(a.sources, ensure_ascii=False, default=str), "JSON"),
                _p("domain_primary", a.domain_primary),
                _p("domain_secondary", a.domain_secondary),
                _p("requested_elements", json.dumps(a.requested_elements, ensure_ascii=False), "JSON"),
                _p("time_anchor", json.dumps(a.time_anchor, ensure_ascii=False), "JSON"),
                _p("data_as_of", a.data_as_of.isoformat() if a.data_as_of else None, "DATE"),
                _p("expires_at", _ts(a.expires_at), "TIMESTAMP"),
                _p("judge_findings", json.dumps(a.judge_findings, ensure_ascii=False, default=str), "JSON"),
                _p("embedding_model", a.embedding_model),
                _p("admission_versions", json.dumps(a.admission_versions, ensure_ascii=False), "JSON"),
                _p("entities", json.dumps(a.entities, ensure_ascii=False), "JSON"),
                _p("claims", json.dumps(a.claims, ensure_ascii=False), "JSON"),
            ],
        )
        return result.get("numberOfRecordsUpdated", 0) == 1

    def expiry_policy(self) -> tuple[dict[str, timedelta], str]:
        result = self._execute(
            "SELECT domain, EXTRACT(EPOCH FROM validity)::bigint, policy_version FROM expiry_policy",
            [],
        )
        policy: dict[str, timedelta] = {}
        versions = set()
        for row in result.get("records") or []:
            policy[row[0]["stringValue"]] = timedelta(seconds=row[1]["longValue"])
            versions.add(row[2]["stringValue"])
        # Domains missing from the table fall back to the seed values rather
        # than failing every admission of that domain.
        merged = {**default_policy(), **policy}
        return merged, ",".join(sorted(versions)) or "default"

    def deprecate(self, entry_id: str, reason: str) -> bool:
        """PENDING or ACTIVE -> DEPRECATED; never deletes (proposal Sheet 5).
        A PENDING entry deprecated here is then dropped by admission (T6)."""
        result = self._execute(
            """UPDATE cache_entry SET status = 'DEPRECATED', status_reason = :reason
               WHERE entry_id = :entry_id AND status IN ('PENDING', 'ACTIVE')""",
            [_p("entry_id", entry_id, "UUID"), _p("reason", reason)],
        )
        return result.get("numberOfRecordsUpdated", 0) == 1

    def last_turn(self, session_id: str, *, window_seconds: float) -> dict[str, Any] | None:
        result = self._execute(
            """SELECT last_query_text, served_entry_id::text, EXTRACT(EPOCH FROM now() - served_at)::float8
               FROM session_last_turn
               WHERE session_id = :session_id AND served_at > now() - make_interval(secs => :window)""",
            [_p("session_id", session_id), {"name": "window", "value": {"doubleValue": float(window_seconds)}}],
        )
        records = result.get("records") or []
        if not records:
            return None
        query, entry_id, age = (_field(c) for c in records[0])
        return {"last_query_text": query, "served_entry_id": entry_id, "age_seconds": float(age)}

    def record_turn(self, session_id: str, query: str, served_entry_id: str | None, *, window_seconds: float) -> None:
        """One row per session, overwritten each turn. The same statement purges
        every row already outside the window, so no session record outlives it
        for longer than the gap between two asks (D10)."""
        self._execute(
            """WITH purged AS (
                   DELETE FROM session_last_turn WHERE served_at < now() - make_interval(secs => :window)
               )
               INSERT INTO session_last_turn (session_id, last_query_text, served_entry_id, served_at)
               VALUES (:session_id, :query, :served_entry_id, now())
               ON CONFLICT (session_id) DO UPDATE SET
                   last_query_text = EXCLUDED.last_query_text,
                   served_entry_id = EXCLUDED.served_entry_id,
                   served_at = EXCLUDED.served_at""",
            [
                _p("session_id", session_id),
                _p("query", query),
                _p("served_entry_id", served_entry_id, "UUID"),
                {"name": "window", "value": {"doubleValue": float(window_seconds)}},
            ],
        )

    def size(self) -> list[dict[str, Any]]:
        """Entries by state and primary domain (E7.4): PENDING, ACTIVE (still
        servable), EXPIRED (ACTIVE past expires_at -- nothing removes them
        until the cleanup pipeline exists) and DEPRECATED."""
        result = self._execute(
            """SELECT CASE WHEN status = 'ACTIVE' AND expires_at <= now() THEN 'EXPIRED' ELSE status END,
                      coalesce(domain_primary, 'NONE'), count(*)
               FROM cache_entry GROUP BY 1, 2""",
            [],
        )
        return [{"state": r[0]["stringValue"], "domain": r[1]["stringValue"], "entries": r[2]["longValue"]} for r in result.get("records") or []]

    def retrieval_policy(self) -> dict[str, float]:
        result = self._execute("SELECT domain, min_cosine FROM retrieval_policy", [])
        return {row[0]["stringValue"]: float(_field(row[1])) for row in result.get("records") or []}

    def search(self, vector: list[float], *, min_cosine: float, embedding_model: str, limit: int) -> list[dict[str, Any]]:
        """Annex A3: one SQL query with every filter next to the search."""
        result = self._execute(
            """SELECT entry_id::text, question_text, answer_text, answer_text_original, original_lang,
                      sources::text, 1 - (question_vector <=> CAST(:v AS vector)) AS cosine,
                      to_char(admitted_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'), data_as_of::text,
                      vendor, card_revision, grounded_ratio::float8, claims::text
               FROM cache_entry
               WHERE status = 'ACTIVE'
                 AND expires_at > now()
                 AND embedding_model = :model
                 AND 1 - (question_vector <=> CAST(:v AS vector)) >= :min_cosine
               ORDER BY question_vector <=> CAST(:v AS vector)
               LIMIT :limit""",
            [
                _p("v", _vector_literal(vector)),
                _p("model", embedding_model),
                {"name": "min_cosine", "value": {"doubleValue": float(min_cosine)}},
                {"name": "limit", "value": {"longValue": int(limit)}},
            ],
        )
        rows = []
        for rec in result.get("records") or []:
            values = [_field(c) for c in rec]
            admitted, as_of = values[7], values[8]
            vendor, card_revision, grounded_ratio, claims = values[9], values[10], values[11], values[12]
            rows.append(
                {
                    "entry_id": values[0],
                    "question_text": values[1],
                    "answer_text": values[2],
                    "answer_text_original": values[3],
                    "original_lang": values[4],
                    "sources": json.loads(values[5]) if values[5] else [],
                    "cosine": float(values[6]),
                    "admitted_at": datetime.fromisoformat(admitted.replace("Z", "+00:00")) if admitted else None,
                    "data_as_of": date.fromisoformat(as_of) if as_of else None,
                    "vendor": vendor,
                    "card_revision": card_revision,
                    "grounded_ratio": float(grounded_ratio) if grounded_ratio is not None else None,
                    "claims": json.loads(claims) if claims else {},
                }
            )
        return rows


class S3EvidenceStore:
    def __init__(self, *, s3_client, bucket: str):
        self._client = s3_client
        self._bucket = bucket

    def put(self, key: str, body: dict[str, Any]) -> str:
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=json.dumps(body, ensure_ascii=False, default=str).encode("utf-8"),
            ContentType="application/json",
        )
        return f"s3://{self._bucket}/{key}"


class MemoryStore:
    def __init__(self, policy: dict[str, timedelta] | None = None, min_cosine: dict[str, float] | None = None):
        self.rows: dict[str, dict[str, Any]] = {}
        self.turns: dict[str, dict[str, Any]] = {}
        self._policy = policy or default_policy()
        self._min_cosine = min_cosine or {}

    def create_pending(self, candidate: Candidate, evidence_uri: str) -> None:
        self.rows[candidate.entry_id] = {
            "status": "PENDING",
            "evidence_uri": evidence_uri,
            "vendor": candidate.vendor,
            "answer_text_original": candidate.answer,
            "card_revision": candidate.card_revision,
            "grounded_ratio": candidate.grounded_ratio,
        }

    def is_pending(self, entry_id: str) -> bool:
        return self.rows.get(entry_id, {}).get("status") == "PENDING"

    def delete_pending(self, entry_id: str) -> bool:
        if self.is_pending(entry_id):
            del self.rows[entry_id]
            return True
        return False

    def activate(self, activation: Activation) -> bool:
        if not self.is_pending(activation.entry_id):
            return False
        self.rows[activation.entry_id].update(status="ACTIVE", activation=activation, admitted_at=datetime.now(timezone.utc))
        return True

    def expiry_policy(self) -> tuple[dict[str, timedelta], str]:
        return dict(self._policy), "memory"

    def size(self) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        counts: dict[tuple[str, str], int] = {}
        for row in self.rows.values():
            a: Activation | None = row.get("activation")
            state = "EXPIRED" if row["status"] == "ACTIVE" and a is not None and a.expires_at <= now else row["status"]
            key = (state, (a.domain_primary if a else None) or "NONE")
            counts[key] = counts.get(key, 0) + 1
        return [{"state": s, "domain": d, "entries": n} for (s, d), n in sorted(counts.items())]

    def retrieval_policy(self) -> dict[str, float]:
        return dict(self._min_cosine)

    def deprecate(self, entry_id: str, reason: str) -> bool:
        row = self.rows.get(entry_id)
        if not row or row["status"] not in ("PENDING", "ACTIVE"):
            return False
        row.update(status="DEPRECATED", status_reason=reason)
        return True

    def last_turn(self, session_id: str, *, window_seconds: float) -> dict[str, Any] | None:
        turn = self.turns.get(session_id)
        if not turn:
            return None
        age = (datetime.now(timezone.utc) - turn["served_at"]).total_seconds()
        if age >= window_seconds:
            return None
        return {"last_query_text": turn["last_query_text"], "served_entry_id": turn["served_entry_id"], "age_seconds": age}

    def record_turn(self, session_id: str, query: str, served_entry_id: str | None, *, window_seconds: float) -> None:
        now = datetime.now(timezone.utc)
        self.turns = {k: v for k, v in self.turns.items() if (now - v["served_at"]).total_seconds() < window_seconds}
        self.turns[session_id] = {"last_query_text": query, "served_entry_id": served_entry_id, "served_at": now}

    def search(self, vector: list[float], *, min_cosine: float, embedding_model: str, limit: int) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        found = []
        for entry_id, row in self.rows.items():
            a: Activation | None = row.get("activation")
            if row["status"] != "ACTIVE" or a is None or a.expires_at <= now or a.embedding_model != embedding_model:
                continue
            dot = sum(x * y for x, y in zip(vector, a.question_vector))
            norm = math.sqrt(sum(x * x for x in vector)) * math.sqrt(sum(y * y for y in a.question_vector))
            cosine = dot / norm if norm else 0.0
            if cosine >= min_cosine:
                found.append(
                    {
                        "entry_id": entry_id,
                        "question_text": a.question_text,
                        "answer_text": a.answer_text,
                        "answer_text_original": row.get("answer_text_original"),
                        "original_lang": a.original_lang,
                        "sources": a.sources,
                        "cosine": cosine,
                        "admitted_at": row.get("admitted_at"),
                        "data_as_of": a.data_as_of,
                        "vendor": row.get("vendor"),
                        "card_revision": row.get("card_revision"),
                        "grounded_ratio": row.get("grounded_ratio"),
                        "claims": a.claims,
                    }
                )
        return sorted(found, key=lambda r: -r["cosine"])[:limit]


class MemoryEvidenceStore:
    def __init__(self):
        self.objects: dict[str, dict[str, Any]] = {}

    def put(self, key: str, body: dict[str, Any]) -> str:
        self.objects[key] = body
        return f"memory://{key}"
