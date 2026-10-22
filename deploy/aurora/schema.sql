-- Cache lifecycle schema (Propuesta_cache_CdV, Annex A1/A2).
-- Aurora PostgreSQL with pgvector >= 0.8 (HNSW). Idempotent: safe to re-run.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS cache_entry (
    entry_id               uuid PRIMARY KEY,
    status                 text NOT NULL CHECK (status IN ('PENDING', 'ACTIVE', 'DEPRECATED')),
    status_reason          text,
    created_at             timestamptz NOT NULL DEFAULT now(),
    admitted_at            timestamptz,
    -- question and answer (English) + originals
    question_text          text,
    question_vector        vector(1024),
    answer_text            text,
    original_lang          text,
    question_text_original text NOT NULL,
    answer_text_original   text NOT NULL,
    sources                jsonb,
    -- classification and judgement
    domain_primary         text,
    domain_secondary       text,
    requested_elements     jsonb,
    time_anchor            jsonb,
    data_as_of             date,
    expires_at             timestamptz,
    judge_findings         jsonb,
    -- traceability and versions
    evidence_uri           text NOT NULL,
    embedding_model        text,
    admission_versions     jsonb,
    -- reserved for later stages
    entities               text[],
    vendor                 text,
    data_origin            text NOT NULL DEFAULT 'public' CHECK (data_origin IN ('public', 'private')),
    -- A2K-KCP 0.1 section 9.4: an entry is invalidated when its vendor's KB Card revision changes.
    card_revision          text,
    -- The groundedRatio the agent self-reported for the answer, served back with it.
    grounded_ratio         real,
    -- The agent's claims per served version: {"original": [{"text", "sources"}], "en": [...]}.
    claims                 jsonb,
    -- What the vendor answer took: {"vendor_calls": {vendor: n}, "agent_input_tokens", "agent_output_tokens"}
    -- -- what serving the entry saves (a2k/cache/savings.py).
    answer_cost            jsonb
);
-- Columns added after the first deployment (CREATE TABLE IF NOT EXISTS does not add them).
ALTER TABLE cache_entry ADD COLUMN IF NOT EXISTS card_revision text;
ALTER TABLE cache_entry ADD COLUMN IF NOT EXISTS grounded_ratio real;
ALTER TABLE cache_entry ADD COLUMN IF NOT EXISTS claims jsonb;
ALTER TABLE cache_entry ADD COLUMN IF NOT EXISTS answer_cost jsonb;

-- Retrieval only ever searches ACTIVE entries (Annex A3).
CREATE INDEX IF NOT EXISTS cache_entry_vector_active
    ON cache_entry USING hnsw (question_vector vector_cosine_ops)
    WHERE status = 'ACTIVE';
CREATE INDEX IF NOT EXISTS cache_entry_status_expires ON cache_entry (status, expires_at);
-- Orphan PENDING cleanup (cleanup step 1, D8: older than 2 h).
CREATE INDEX IF NOT EXISTS cache_entry_pending_created ON cache_entry (created_at) WHERE status = 'PENDING';

-- Session memory for the reformulation check (proposal Sheet 5, retrieval
-- step 1). In Aurora rather than the proposal's DynamoDB table, to keep the
-- cache in one store. Only the last turn of each session; rows older than the
-- reformulation window (D5) are purged by every write and never read (D10).
CREATE TABLE IF NOT EXISTS session_last_turn (
    session_id      text PRIMARY KEY,
    last_query_text text NOT NULL,
    served_entry_id uuid,
    served_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS session_last_turn_served_at ON session_last_turn (served_at);

CREATE TABLE IF NOT EXISTS expiry_policy (
    domain         text PRIMARY KEY,
    validity       interval NOT NULL,
    policy_version text NOT NULL
);

CREATE TABLE IF NOT EXISTS retrieval_policy (
    domain         text PRIMARY KEY,
    min_cosine     real NOT NULL CHECK (min_cosine BETWEEN 0 AND 1),
    policy_version text NOT NULL
);

-- Starting values decided in the working session of 2026-09-30.
-- D6: expiry per domain (+ historical data).
INSERT INTO expiry_policy (domain, validity, policy_version) VALUES
    ('REG', interval '30 days', 'd6-2026-09-30'),
    ('MKT', interval '7 days', 'd6-2026-09-30'),
    ('TAL', interval '30 days', 'd6-2026-09-30'),
    ('MAC', interval '30 days', 'd6-2026-09-30'),
    ('CRP', interval '30 days', 'd6-2026-09-30'),
    ('RSK', interval '3 days', 'd6-2026-09-30'),
    ('MED', interval '1 day', 'd6-2026-09-30'),
    ('historical', interval '365 days', 'd6-2026-09-30')
ON CONFLICT (domain) DO NOTHING;

-- D3: 0.90 in every domain until there is data.
INSERT INTO retrieval_policy (domain, min_cosine, policy_version) VALUES
    ('REG', 0.90, 'd3-2026-09-30'),
    ('MKT', 0.90, 'd3-2026-09-30'),
    ('TAL', 0.90, 'd3-2026-09-30'),
    ('MAC', 0.90, 'd3-2026-09-30'),
    ('CRP', 0.90, 'd3-2026-09-30'),
    ('RSK', 0.90, 'd3-2026-09-30'),
    ('MED', 0.90, 'd3-2026-09-30')
ON CONFLICT (domain) DO NOTHING;
