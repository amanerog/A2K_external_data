# Aurora + pgvector for the cache

What a2k-box needs before `CACHE_MODE=shadow` (or `serve`) does anything. The
code is in `a2k/cache/`; the design is the cache lifecycle proposal
(Propuesta_cache_CdV, Sheets 2–4 and Annex A1–A4).

| `CACHE_MODE` | Retrieval | User gets | Admission |
|---|---|---|---|
| `off` (default) | — | vendor answer | — |
| `shadow` (phase 1) | runs on every `ask`, in parallel with the vendor call; logged | **always the vendor answer** | every vendor answer |
| `serve` | runs first, within `CACHE_TIME_BUDGET_SECONDS` (D15) | the cached answer on a hit, otherwise the vendor's | every vendor answer |

Only `ask` is cached (`search` returns passages, not an answer).

**Reformulation (retrieval step 1).** When the caller passes `sessionId` to
`a2k.ask` (K2: the id of the end user's conversation), the previous turn of
that session is kept in `session_last_turn`. A question within
`CACHE_REFORMULATION_WINDOW_SECONDS` (600, D5) of it is classified by the light
model as reformulation / follow-up / new; a reformulation deprecates the entry
served on the previous turn (`immediate_reformulation`) before searching. In
Aurora rather than the proposal's DynamoDB, to keep the cache in one store;
every write purges rows outside the window (D10). Without `sessionId`, the
session is taken from the `mcp-session-id` header, or else from AgentCore's
`x-amzn-bedrock-agentcore-runtime-session-id` (prefixed `mcp:` / `agentcore:`);
the serving log's `session_source` says which, so the re-ask rate (D14) can be
computed on caller-provided sessions only (`session_source = "caller"`).

## 1. Database

- **Aurora PostgreSQL** (Serverless v2 is enough for shadow mode), engine
  version with **pgvector ≥ 0.8** (HNSW).
- **RDS Data API enabled** on the cluster (*Modify → Connectivity → Data API*).
  a2k-box runs on AgentCore Runtime in `PUBLIC` network mode, outside any VPC,
  so it reaches Aurora over the Data API (HTTPS + IAM), not a driver
  connection.
- The DB credentials in **Secrets Manager** (the secret RDS creates for the
  cluster works as is).
- A database for the cache (default name `a2k_cache`), then run the schema
  once — it is idempotent:

  ```bash
  aws rds-data execute-statement --resource-arn "$AURORA_CLUSTER_ARN" \
    --secret-arn "$AURORA_SECRET_ARN" --database postgres \
    --sql "CREATE DATABASE a2k_cache"
  # then schema.sql, statement by statement (the Data API runs one per call),
  # or from any psql session with network access to the cluster:
  psql "$AURORA_URL/a2k_cache" -f deploy/aurora/schema.sql
  ```

## 2. Evidence bucket

An S3 bucket for the evidence bundles (`cache-evidence/yyyy/mm/<entry_id>.json`).
The bundle of a rejected answer is kept, so the judge can be re-run on it
when it is tuned; add a lifecycle rule to expire `cache-evidence/` objects
after the retention you settle on (D10).

## 3. a2k-box Runtime

Environment variables:

| Variable | Value |
|---|---|
| `CACHE_MODE` | `shadow` |
| `AURORA_CLUSTER_ARN` | the cluster ARN |
| `AURORA_SECRET_ARN` | the secret ARN |
| `AURORA_DATABASE` | `a2k_cache` (default) |
| `CACHE_EVIDENCE_BUCKET` | the bucket name |
| `ADMISSION_ALLOWED_VENDORS` | `cala` (default, D11) |
| `ADMISSION_LIGHT_MODEL_ID` | default `eu.anthropic.claude-haiku-4-5-20251001-v1:0` |
| `ADMISSION_JUDGE_MODEL_ID` | default `eu.anthropic.claude-sonnet-5` |
| `VENDOR_FALLBACK_MESSAGE` | only if changed -- must match the agent Runtime's value (E4.9) |

Optional: `CACHE_REFORMULATION_WINDOW_SECONDS` (600, D5), `CACHE_DOMAIN_MODES` (per-domain switch, e.g. `MED=off,RSK=shadow`;
a domain never goes beyond `CACHE_MODE`; an `off` domain skips the judge at
admission and the search at retrieval), `CACHE_TIME_BUDGET_SECONDS` (10, D15), `CACHE_CHECKER_MODEL_ID`
(Sonnet 4.5, the Checker as tested, D4), `CACHE_EMBEDDING_MODEL_ID` (Titan v2),
`ADMISSION_MIN_ANSWER_CHARS` (150, D17),
`ADMISSION_MAX_EVIDENCE_CHARS` (400000), `ADMISSION_REQUIRE_VERIFIED_QUOTES`
(true), `ADMISSION_CONFIRM_FIT` (true: a FIT verdict needs a second, confirming
judgement), `ADMISSION_WORKERS` (2).

Execution-role permissions to add:

- `rds-data:ExecuteStatement` on the cluster ARN (admission writes, retrieval reads)
- `secretsmanager:GetSecretValue` on the DB secret
- `s3:PutObject` on `arn:aws:s3:::<bucket>/cache-evidence/*`
- `bedrock:InvokeModel` / `bedrock:Converse` on the light model, the judge
  model, the Checker model and Titan v2 (inference profiles and their
  underlying models)

The DynamoDB answer cache (`ANSWER_CACHE_*`) is gone; its table can be
deleted once nothing reads it.

## 4. Checking it works

Two kinds of JSON log line in the Runtime's CloudWatch log group.

Admission, one per candidate -- `outcome` is `ACTIVE`, `NOT_FIT`, `FILTERED`,
`STALE`, `SUPERSEDED` or `ERROR`:

```
fields @timestamp, outcome, reason, domains.0, timings_ms.judge
| filter event = "cache.admission"
| sort @timestamp desc
```

The serving log, one per `ask` -- in shadow mode `would_hit` is what the cache
would have served, `served_from` is always `vendor`, and `vendor_entry_id`
points to the admission candidate (its S3 evidence bundle holds the fresh
vendor answer to compare a would-be hit with):

```
fields @timestamp, would_hit, entry_id, cosine, threshold, total_ms, within_budget, error
| filter event = "cache.retrieval"
| stats count(*) as asks, sum(would_hit) as would_hits, pct(total_ms, 95) as p95_ms by bin(1d)
```

Re-ask rate (D14), from the same log -- asks with a session whose previous
turn was a reformulation:

```
fields reformulation.kind as kind
| filter event = "cache.retrieval" and ispresent(session_id)
| stats count(*) as asks, sum(kind = "reformulation") as reasks, sum(kind = "reformulation") / count(*) * 100 as reask_pct by domains.0
```

Rows in Aurora: `SELECT status, count(*) FROM cache_entry GROUP BY 1`.
