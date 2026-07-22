"""Append-only audit trail (A2K-KCP-Consumption 4.md, section 11).

Honesty note: `logTarget` is labeled "local-jsonl-worm-sim" because this is a
plain append-only JSONL file, not real WORM storage -- nothing here prevents
an operator with filesystem access from editing it. A production regulated
deployment would point this at actual immutable object storage, a SIEM, or an
append-only ledger, per the KB Card's `audit.logTarget`.

Deployment note: every record is also printed to stdout as a single JSON
line. In Kubernetes/EKS this is the record that actually survives a pod
restart and is comparable across replicas -- the local JSONL file
(`config.audit_log_path`) is pod-local and best-effort, kept mainly for
local/dev use, so a write failure there (e.g. a read-only container
filesystem) does not fail the request.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone

from ..config import config
from ..models.envelope import Audit


def write_audit(
    *,
    request_id: str | None,
    session_id: str | None,
    agent_id: str | None,
    user_id: str | None,
    source_kb_id: str,
    operation: str,
    policy_decision: str,
    decision_reason: str | None,
    citation_ids: list[str],
) -> Audit:
    log_target = "local-jsonl-worm-sim" if config.immutable_audit else "local-jsonl"
    record = Audit(
        requestId=request_id,
        sessionId=session_id,
        agentId=agent_id,
        userId=user_id,
        sourceKbId=source_kb_id,
        sourceKbVersion="mock-fixtures-2026-07-14" if config.is_mock else "live",
        operation=operation,
        policyDecision=policy_decision,
        decisionReason=decision_reason,
        citationIds=citation_ids,
        logged=True,
        loggedAt=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        logTarget=log_target,
        immutableLogRequired=config.immutable_audit,
        logRef=str(uuid.uuid4()),
    )

    line = record.model_dump_json()

    if config.audit_stdout:
        print(line, file=sys.stdout, flush=True)

    try:
        config.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config.audit_log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        print(f"a2k-box: could not write local audit file: {exc}", file=sys.stderr, flush=True)

    return record
