# A2K Box -- Cala + Sayari gateway for K2

A Python "box" that speaks **A2K-KCP** (the consumption protocol described in
`A2K-KCP-Consumption 4.md`) over two transports -- **REST (HTTPS+JSON)** and
**MCP (stdio)** -- and answers company-intelligence questions by fanning out
to two third-party providers:

- **[Cala](https://cala.ai)** -- financial/legal/regulatory data: SEC/EDGAR
  filings, OFAC sanctions, business registries, beneficial ownership.
- **[Sayari](https://sayari.com)** -- corporate ownership/risk entity graph:
  entity resolution, ownership graphs, relationships, adverse-media/risk
  screening.

K2 asks the box one question (`ask`/`search`/`explain`/`getDocument`) and
gets back a single, cited, signed `CitedResponseEnvelope` -- the same shape
whether it called REST or MCP. This is the **Gateway** topology from
`A2K-Overview`: the box fans out, never silently resolves disagreement
between Cala and Sayari, and always shows its citations.

No live credentials exist yet, so the box runs in **mock mode** by default,
backed by fixture data in `a2k/adapters/fixtures/`. Flipping to live mode
is a config change, not a code change (see "Mock to live" below).

## Quickstart

```bash
python3.11 -m venv .venv   # any Python >= 3.10; project was built against 3.13
source .venv/bin/activate
pip install -e ".[dev]"

# REST transport
python -m a2k.api
# -> http://localhost:8000  (card at /.well-known/a2k-card.json)

# MCP transport (stdio) -- run in a separate process, or let K2 launch it
python -m a2k.mcp_server
```

Try it:

```bash
curl -s -X POST http://localhost:8000/a2k/ask \
  -H 'Content-Type: application/json' \
  -d '{"query": "Meridian Textiles ownership"}' | python3 -m json.tool
```

That query deliberately hits fixture data where Cala and Sayari disagree
(62% vs 48% ultimate beneficial ownership) -- the response's `conflicts` and
`conflictReport` fields show the disagreement surfaced, not resolved.

Run the test suite:

```bash
pytest -q
```

## Wiring into K2

**MCP (recommended for an agent tool integration).** Add the box as an MCP
server in K2's tool config, e.g.:

```json
{
  "mcpServers": {
    "a2k-box": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["-m", "a2k.mcp_server"],
      "cwd": "/absolute/path/to/this/repo"
    }
  }
}
```

This exposes resource `a2k://card` (+ `a2k://card/cala`, `a2k://card/sayari`)
and tools `a2k.search`, `a2k.ask`, `a2k.explain`, `a2k.getDocument`,
`a2k.validateCitation`, `a2k.reportConflict`, `a2k.getAuditRecord`. Wiring
this into K2's actual process config is out of scope here since that config
doesn't live in this repository -- the snippet above is what needs adding on
the K2 side.

**REST.** Point K2 at `http://localhost:8000` and speak A2K-KCP directly:
`GET /.well-known/a2k-card.json`, `POST /a2k/{operation}` (fans out to both
providers), `POST /a2k/{cala|sayari}/{operation}` (single provider),
`POST /a2k/streamAsk` (SSE).

## Mock to live

Copy `.env.example` to `.env` and fill in credentials, then set
`A2K_BOX_MODE=live`:

```bash
cp .env.example .env
# edit .env: A2K_BOX_MODE=live, CALA_API_KEY=..., SAYARI_CLIENT_ID=..., SAYARI_CLIENT_SECRET=...
```

`ProviderAdapter.search()`/`.get_document()` have the identical signature in
mock and live mode (`a2k/adapters/base.py`), so nothing above the
adapters changes.

- **Cala (`adapters/cala.py`) -- confirmed, not just best-effort.** Verified
  against the real OpenAPI spec at `api.cala.ai/openapi.json` and the
  `docs.cala.ai/api-reference/api-v1/*` pages (2026-07-20): auth is an
  `X-API-KEY` header (not Bearer, as originally guessed), entity search is
  `GET /v1/entities`, and fetching an entity's facts is a two-call sequence
  -- `GET /v1/entities/{id}/introspection` to learn which fields exist, then
  `POST /v1/entities/{id}` naming the ones to return (Cala gives back none if
  you don't ask by name). `tests/test_cala_live_adapter.py` exercises this
  against a fake transport built from that confirmed schema, so it's tested
  without needing a real key. Also verified against a real account
  (2026-07-21, e.g. `search("Apple")` returning real headquarters/LEI/CIK/
  employee-count facts), which surfaced two things the schema alone didn't
  reveal: Cala represents "no data for this field" as the literal string
  `"<UNKNOWN>"` rather than omitting the key or using `null` (filtered out
  in `_facts_from_entity_response`/`_live_query_fallback` -- otherwise it
  becomes a bogus fact like "Apple's permid is unknown"), and the free tier's
  rate limit trips fast since each entity costs two calls (introspect +
  detail) -- mapped to `RATE_LIMITED`/`retryable: true` with `Retry-After`
  captured into `error.details` when Cala sends it, instead of a generic
  non-retryable `UPSTREAM_ERROR`.

  To cut down on that rate-limit exposure, `/introspection` results (which
  property *names* exist for an entity -- schema, not data) are cached
  in-process per `entity_id` for `CALA_INTROSPECTION_CACHE_TTL_SECONDS`
  (default 24h, see `.env.example`). This only skips re-discovering the
  schema; the actual property *values* are always re-fetched via
  `POST /v1/entities/{id}` on every call, so a cache hit never makes an
  answer stale -- it only avoids repeating a request that, in practice,
  returns the same field *names* every time.

  Named-entity search is only half the picture: a question that doesn't name
  one company ("startups in Spain with funding 10M-50M") has nothing for
  `GET /v1/entities` to match, so when that returns zero results `search()`
  falls back to `POST /v1/knowledge/query` (Cala's structured filter/listing
  endpoint, natural language or dot-notation in, projected rows out). Each
  result row becomes its own set of `Fact`s the same way an entity's
  properties do. Deliberately *not* mapped: `POST /v1/knowledge/search`,
  which returns markdown prose already synthesized by Cala's own model --
  folding that into our `claims`/`citations` model would mean re-extracting
  discrete assertions out of someone else's free-text answer, exactly the
  ungroundable-synthesis problem this gateway avoids by never running an LLM
  inside the box (see "Grounding" below). Also not mapped:
  `numerical_observations` (time-series metrics on an entity).

  One caveat worth knowing before relying on cross-provider conflict
  detection in live mode: `entity_key` for a Cala fact is Cala's own UUID
  (or, for a query-fallback row, a slug of its `name`), and Sayari's
  `entity_key` is Sayari's own identifier -- for the same real-world company
  the two will not match each other. `gateway/conflict.py` only ever
  actually fires in mock mode today, where the fixtures share slugs by hand.
  Real cross-provider entity resolution (matching by name + jurisdiction,
  LEI, etc.) is unbuilt.
- **Sayari (`adapters/sayari.py`) -- still best-effort.** Built from
  `documentation.sayari.com`'s prose description (OAuth2 client-credentials,
  REST, JSON) without a discoverable OpenAPI spec or example payloads, so
  the exact paths/fields are still marked `# TODO(live)`. Apply the same
  treatment as Cala once real credentials or docs turn up: pin down the
  confirmed shape, then replace the guess with a schema-matched test.

## Deploy to EKS

K2's agent runtime only speaks plain REST/HTTP to external tools (no MCP
client), so the box deploys as its own containerized service in the same
EKS cluster and K2 calls it over the cluster-internal network -- no changes
to K2's own container or config beyond pointing it at the Service URL. The
MCP stdio transport is *not* deployed this way: stdio requires the client
to spawn the server as a local child process, which only makes sense
embedded inside K2's own container, not as an independent K8s workload --
not needed here since K2 doesn't have an MCP client anyway.

**Build and push the image:**

```bash
docker build -t a2k-box:latest .
docker tag a2k-box:latest <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/a2k-box:latest
aws ecr get-login-password --region <REGION> | docker login --username AWS --password-stdin <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com
docker push <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/a2k-box:latest
```

**Signing key -- generate once, share across all replicas.** The gateway
signs every response (Level 4, section 14.3); if each pod auto-generated
its own key on first boot, a response signed by one replica couldn't be
verified against the JWKS served by another. Generate it once locally and
push it in as a Secret instead of letting the container mint one:

```bash
python -c "from a2k.gateway import signing; signing.get_private_key()"  # writes keys/gateway_ed25519.pem
kubectl create namespace a2k-box
kubectl create secret generic a2k-box-signing-key -n a2k-box \
  --from-file=gateway_ed25519.pem=./keys/gateway_ed25519.pem
```

Keep that local `keys/gateway_ed25519.pem` somewhere safe (it's gitignored) --
regenerating it later invalidates every previously issued signature and
requires re-pushing the Secret to all clusters.

**Apply the manifests** (`deploy/`):

```bash
kubectl apply -f deploy/namespace.yaml
kubectl apply -f deploy/configmap.yaml
kubectl apply -f deploy/deployment.yaml   # edit the `image:` field first
kubectl apply -f deploy/service.yaml
kubectl apply -f deploy/hpa.yaml          # optional
```

This brings the box up in **mock mode** (`A2K_BOX_MODE: "mock"` in
`configmap.yaml`), reachable inside the cluster at
`http://a2k-box.a2k-box.svc.cluster.local:8000`. Point K2's tool config at
that URL and it can call `/a2k/ask`, `/a2k/search`, etc. exactly like the
`curl` examples above.

**Switching to live mode:** copy `deploy/secret-provider-creds.example.yaml`
to `deploy/secret-provider-creds.yaml` (gitignored, never commit real
credentials), fill in `CALA_API_KEY`/`SAYARI_CLIENT_ID`/`SAYARI_CLIENT_SECRET`,
apply it, then flip `A2K_BOX_MODE` to `"live"` in `configmap.yaml` and
re-apply.

**What's already handled in the manifests:** non-root container user,
`readOnlyRootFilesystem` with `emptyDir` scratch space for `/tmp` and the
local audit file, resource requests/limits, readiness/liveness probes
against `/`, and an optional HPA. The durable audit trail is the stdout
copy of every record (`A2K_AUDIT_STDOUT=true`, see `gateway/audit.py`) --
wire your cluster's log aggregation (CloudWatch, or whatever EKS add-on is
in use) to pick it up; the local JSONL file is pod-local and lost on
restart, kept only for local/dev convenience.

Not verified in this environment: the Docker image was not actually built
here (no Docker daemon available in this sandbox) and the manifests were
not applied against a real cluster (this machine's `kubectl` context points
at an unrelated AKS cluster, left untouched). What *was* verified: a
non-editable `pip install .` into a clean venv -- the same install path the
Dockerfile uses -- correctly includes the fixtures/cards and boots; and all
six YAML files parse and carry the required `apiVersion`/`kind`/`metadata.name`
fields. Build and apply for real before trusting this in production.

## Conformance -- A2K-KCP Level 4

What's implemented:

- All four operations (`search`, `ask`, `explain`, `getDocument`) with the
  full cited-response envelope.
- `TextQuoteSelector` citations with `sourceHash`, `retrievedAt`, and
  per-citation `dataLineage`.
- Deterministic, exactly-measured grounding: `answer` is built only from
  verbatim citation quotes (`a2k/gateway/synthesis.py`), so
  `groundedRatio` is an exact character count, not an estimate, and
  `strictGrounding` is satisfiable on demand.
- Cross-source conflict detection and reporting (`gateway/conflict.py`):
  when Cala and Sayari disagree, both positions are surfaced via
  `conflicts[]` and a full `conflictReport` -- never resolved silently
  (A2K-KCP section 12.4).
- Signed responses: Ed25519 detached signatures over the fields KCP section
  14.3 specifies, with a `/.well-known/jwks.json` verification endpoint
  (`gateway/signing.py`).
- Append-only audit trail (`gateway/audit.py`), `logTarget:
  "local-jsonl-worm-sim"`.
- The full KCP error model (`errors.py`) and access-leakage-safe `NOT_FOUND`
  handling.

Two deliberate simplifications (this is a two-provider box, not a full
enterprise Catalog+Gateway deployment):

1. **`/a2k/streamAsk` streams transport, not generation.** There is no LLM
   inside the box writing tokens incrementally -- `answer` is fully computed
   first, then chunked over SSE as `text_chunk` events with a real, signed
   `proof_footer` at the end (KCP section 16). A client validating the
   footer gets genuine verification; it just isn't watching genuine
   incremental generation.
2. **No Catalog.** Signed *requests* (section 14.4) and Catalog-issued card
   signatures/attestations are out of scope -- there's no Catalog in a
   fixed two-provider box. Response signing (14.3) is implemented.

Security tier: both KB Cards declare `dataClassification: "public"`
(commercial/public company data) -> **tier S0** per
`A2K-KBCard-Schema` section 4.4.1, so `oboAssertionToken` is accepted and
logged but not cryptographically validated. If Cala or Sayari data is ever
paired with something non-public (e.g. an internal risk score), revisit this
before reusing the cards as-is.

Also honest about the signing implementation: canonicalization is a
practical sorted-keys-JSON approximation of JCS/RFC 8785, not a certified
implementation, and `jws` is a raw base64url Ed25519 signature rather than a
full JWS compact serialization. Good enough to prove "this response left
this gateway unmodified"; swap in a vetted JOSE library before treating it
as a compliance control.

## Project layout

```
a2k/
  config.py, errors.py          environment config, KCP error codes
  models/                       Pydantic: envelope, KB Card, requests
  adapters/                     Cala + Sayari clients (mock + live), fixtures
  gateway/                      synthesis, conflict detection, signing, audit, engine
  cards/                        the three KB Cards (gateway, cala, sayari)
  api/rest.py                   REST transport (FastAPI)
  mcp_server/server.py          MCP transport (stdio)
tests/                          pytest suite (29 tests)
deploy/                         Kubernetes manifests (EKS deployment, see above)
Dockerfile, .dockerignore       container image for the REST service
```

`gateway/engine.py`'s `GatewayEngine` is the single place that knows how to
answer a query; `api/rest.py` and `mcp_server/server.py` are thin transport
adapters over it, so REST and MCP are guaranteed to return identical
envelopes for the same request.
