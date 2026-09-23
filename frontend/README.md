# A2K frontend

A small FastAPI app that lets a human ask a question through a web form and see the full
`CitedResponseEnvelope` a2k-box's **already-deployed** MCP Runtime returns -- answer, claims,
citations, `groundedRatio`, freshness (including whether the data is stale), conflicts between
sources, and the audit trail (including whether the answer was served from the semantic answer
cache, see `a2k/gateway/cache.py`). It talks directly to that Runtime over MCP
(`boto3.invoke_agent_runtime`) -- it does not run a2k-box itself, and it is not a2k-box's own
REST transport (`a2k/api/rest.py`) either. Nothing here changes a2k-box or the agent; this is
purely a consumer of what's already running.

Runs locally (see "Run locally" below) or deployed to EKS (see "EKS deployment" below).

## Why there's a backend at all, not just a static page

a2k-box's MCP Runtime uses IAM/SigV4 inbound auth (every script in `../deploy/agentcore/`
confirms this -- they all call `boto3.client("bedrock-agentcore").invoke_agent_runtime`, never a
bearer token). A browser can't sign SigV4 requests without exposing AWS credentials
client-side, so this small FastAPI app holds those credentials server-side and exposes a plain
JSON API (`/api/ask`, `/api/search`) the browser's own JS calls instead.

## Run locally

```bash
cd frontend
pip install -r requirements.txt
```

Needs AWS credentials in the environment able to call `bedrock-agentcore:InvokeAgentRuntime` on
a2k-box's Runtime -- the same credentials any script in `../deploy/agentcore/` already needs
(your own AWS CLI/SSO session; nothing new to set up if you've already run those scripts).

```bash
cd grc
uvicorn app:app --reload --port 8000
```

Open `http://localhost:8000`. (`app.py`/`a2k_client.py` live in `grc/`, not at this directory's
root -- see "Project layout" below for why.)

## Configuration

| Var | Default | What it's for |
|---|---|---|
| `A2K_BOX_RUNTIME_ARN` | the current a2k-box MCP Runtime ARN (`a2k_external_data_mcp-...`) | Which Runtime to call -- override if a2k-box is ever redeployed under a different Runtime. |
| `AWS_REGION` | `eu-west-1` | Region for both the Bedrock AgentCore client and the Runtime ARN above. |

See `grc/a2k_client.py`'s own top for the exact defaults.

## Project layout

```
frontend/
  grc/                # actual application code -- app.py, a2k_client.py, static/, templates/
  tests/              # pytest, kept out of grc/ so it's excluded from Sonar's `sonar.sources`
  Dockerfile, entrypoint.sh, Pipfile(.lock), pyproject.toml, requirements(-dev).txt
  deploy/             # EKS manifests
```

The `grc/` split (rather than a flat layout with `app.py` at this directory's root) is **not**
a stylistic choice -- Santander's shared CI/CD pipeline's "Sonar Scan with Sonar-Scanner" step
hardcodes `-Dsonar.sources=./grc` as a command-line argument, and CLI `-D` properties always win
over anything in `sonar-project.properties`. A local `sonar-project.properties` override was
tried first and confirmed *not* to work (the CLI arg silently wins); moving the actual code into
`grc/` is what actually gets a green Sonar scan. `pyproject.toml`'s `[tool.setuptools]` and
`entrypoint.sh`'s `cd grc` are both there because of this.

## Tests and coverage

```bash
cd frontend
pip install -r requirements-dev.txt
pytest --cov --cov-report=xml --cov-report=term-missing
```

`tests/test_app.py` covers the FastAPI routes (`/health`, `/`, `/api/ask`, `/api/search`,
including the transport-error -> 502 path) against a monkeypatched `a2k_client`; `tests/
test_a2k_client.py` covers `a2k_client.py`'s own session/SSE-parsing logic against a fake
`bedrock-agentcore` client -- no real AWS call, no network, in either file. Both import `app`/
`a2k_client` straight from `grc/` via `pyproject.toml`'s `pythonpath = ["grc"]`, no install
needed first.

`--cov` bare (no `=grc`, no `=.`) is deliberate, matching exactly what this pipeline's own
`PYTHON_BUILD_COMMAND` step runs -- it falls back to `[tool.coverage.run]`'s `source = ["."]` in
`pyproject.toml`. **Do not change that to `source = ["grc"]`, and do not run `--cov=grc`
locally** -- either one rebases `coverage.xml`'s recorded filenames to be relative to `grc/`
itself (`filename="app.py"`), but Sonar resolves `coverage.xml` paths relative to the *project
base directory* (matching `sonar.sources=./grc`), so an un-prefixed `app.py` matches nothing and
silently counts as uncovered. Confirmed live: this exact mismatch is what produced a "43.5%
Coverage on New Code" quality-gate failure despite 100% real coverage -- `source = ["."]` is
what makes `coverage.xml` come out with `filename="grc/app.py"`, which actually matches.

`pytest-cov`'s `--cov-report=xml` writes `coverage.xml` in Cobertura format at this directory's
root -- this pipeline's Sonar step already passes `-Dsonar.python.coverage.reportPaths=./coverage.xml`
itself (see `sonar-project.properties`'s own comment), so nothing further needs wiring here
beyond making sure this command actually runs and produces that file in CI, which
`PYTHON_BUILD_COMMAND`'s `pipenv run python -m pytest ...` step already does. `requirements-dev.txt`
is dev/test-only -- never installed in the Docker image (the Dockerfile's `pipenv requirements`
reads `Pipfile`'s `[packages]` only, not `[dev-packages]`).

## How a request flows

1. Browser submits the form -> `app.js` `fetch()`s `POST /api/ask` (or `/api/search`) with
   `{"query": ..., "sources": [...]}` (`sources` empty = let the agent decide, same as omitting
   it on a raw MCP call).
2. `grc/app.py` opens a **fresh MCP session** (`a2k_client.new_session()`) and calls the tool
   (`a2k_client.call_ask()`/`call_search()`) -- one new session per request, not pooled/reused
   across requests; see the plan this was built from for why (each web request is independent
   and short-lived, and session reuse across concurrent users would need isolation this doesn't
   have yet).
3. The parsed `CitedResponseEnvelope` JSON goes straight back to the browser -- `app.py` doesn't
   reshape it, `app.js` renders it directly by the field names `a2k/models/envelope.py` already
   defines.

A request can take anywhere from a few seconds (cache hit) to roughly a minute (a live vendor
lookup through the agent) -- the UI shows a loading state for this, it's expected, not a bug.

## EKS deployment

Single FastAPI process serving both the JSON API and the static/template files -- one container,
no split frontend/backend services. Uses Santander's standard base image/pipenv build (`Dockerfile`,
`produban/python-313-ubi9` -- **not** the root `Dockerfile`'s `python-314-ubi9`/multi-stage-wheel
pattern; the two are deliberately different images, don't conflate them), and a `Deployment` +
`Service` under `deploy/`. Given this is an internal/test tool, not a production surface, there's
no `ConfigMap`/`HPA` here -- `a2k_client.py`'s own env-var defaults (`AGENT_RUNTIME_ARN`, region)
are already correct for the currently-deployed a2k-box Runtime, and traffic doesn't need
autoscaling.

**Build and push the image** (context is this directory, not the repo root):

```bash
cd frontend
docker build -t a2k-frontend:latest .
docker tag a2k-frontend:latest <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/a2k-frontend:latest
aws ecr get-login-password --region <REGION> | docker login --username AWS --password-stdin <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com
docker push <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/a2k-frontend:latest
```

**IAM role for IRSA** -- the one thing that *does* carry over from local dev to EKS: however the
pod's IAM identity is set up, it needs the same `bedrock-agentcore:InvokeAgentRuntime` permission
on a2k-box's Runtime that your local AWS credentials already need above -- no code change,
`boto3`'s default credential chain picks either up the same way. Create a role trusted by this
cluster's OIDC provider, scoped to exactly that one action on a2k-box's Runtime ARN, then fill
its ARN into `deploy/serviceaccount.yaml`'s `eks.amazonaws.com/role-arn` annotation before
applying it.

**Apply the manifests** (`deploy/`):

```bash
kubectl apply -f deploy/namespace.yaml
kubectl apply -f deploy/serviceaccount.yaml   # edit the role-arn annotation first
kubectl apply -f deploy/deployment.yaml       # edit the `image:` field first
kubectl apply -f deploy/service.yaml
```

Reachable inside the cluster at `http://a2k-frontend.a2k-frontend.svc.cluster.local:8080` --
front it with whatever this cluster's usual ingress/route mechanism is for a human-facing tool
(not set up here, cluster-specific).

**What's already handled in the manifests:** non-root container user (matching a2k-box's own
`runAsUser: 10001` convention on this cluster), `readOnlyRootFilesystem` with an `emptyDir` for
`/tmp`, resource requests/limits, and readiness/liveness probes against `/health`
(`app.py`'s own -- a shallow "is this process up" check, deliberately not a live a2k-box MCP
round trip; see that endpoint's docstring).

Not verified in this environment: the Docker image was not actually built here (no Docker daemon
available), and the manifests were not applied against a real cluster. What *was* verified: all
four YAML files parse and carry the required `apiVersion`/`kind`/`metadata.name` fields, and
`frontend/Pipfile.lock` was generated for real (against public PyPI, since the corporate Nexus
mirror this Dockerfile points at isn't reachable from outside Santander's network) -- re-lock it
(`pipenv lock`) from an environment with Nexus access before trusting it in production, though
Nexus's own `pypi-public` naming suggests it's a pull-through cache of the same public index, so
the resolved versions/hashes should already match.
