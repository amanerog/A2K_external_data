"""Environment-driven configuration for the A2K box.

Mock mode is the default so a fresh checkout with no credentials configured
still runs. Flipping to live mode requires only setting A2K_BOX_MODE=live
plus the provider credentials below -- adapters read this module, never
os.environ directly, so the mock/live branch lives in one place. The
deployed a2k-box Runtime itself has run in live mode, credentials sourced
from Secrets Manager, since 2026-08-18 -- see _secret_env()/A2K_SECRETS_ARN
below.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent

A2K_VERSION = "0.6-baseline"
CONFORMANCE_LEVEL = 4


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@lru_cache(maxsize=1)
def _secrets_manager_bundle() -> dict[str, str]:
    """Live-credential fallback for env vars not set directly -- lets AgentCore
    Runtime deploys point A2K_SECRETS_ARN at a Secrets Manager secret (a flat
    JSON object of the same env var names below) instead of putting
    CALA_API_KEY/AUTH0_CLIENT_ID/AUTH0_CLIENT_SECRET in plaintext Runtime
    environment variables, which are visible to anyone with read access to
    the Runtime resource. Local/.env-based dev is unaffected: _secret_env
    checks os.environ first, so this is only ever consulted when a name is
    genuinely unset."""
    secret_arn = os.environ.get("A2K_SECRETS_ARN")
    if not secret_arn:
        return {}
    import boto3

    client = boto3.client("secretsmanager")
    return json.loads(client.get_secret_value(SecretId=secret_arn)["SecretString"])


def _secret_env(name: str) -> str | None:
    return os.environ.get(name) or _secrets_manager_bundle().get(name)


def _parse_domain_modes(raw: str) -> dict[str, str]:
    """"MED=off, RSK=shadow" -> {"MED": "off", "RSK": "shadow"}. An entry with
    an unknown mode is ignored (and printed), never guessed."""
    modes: dict[str, str] = {}
    for item in raw.split(","):
        if not item.strip():
            continue
        domain, _, mode = item.partition("=")
        mode = mode.strip().lower()
        if mode in ("off", "shadow", "serve") and domain.strip():
            modes[domain.strip().upper()] = mode
        else:
            print(f"a2k-box: ignoring CACHE_DOMAIN_MODES entry {item.strip()!r}", flush=True)
    return modes


@dataclass(frozen=True)
class Config:
    mode: str = field(default_factory=lambda: os.environ.get("A2K_BOX_MODE", "mock").lower())

    cala_api_key: str | None = field(default_factory=lambda: _secret_env("CALA_API_KEY"))
    cala_base_url: str = field(
        default_factory=lambda: os.environ.get("CALA_BASE_URL", "https://api.cala.ai")
    )
    # Cala's own hosted MCP server (confirmed live 2026-08-10): streamable-http,
    # same X-API-KEY auth as the REST API above. See adapters/cala_mcp.py.
    cala_mcp_url: str = field(
        default_factory=lambda: os.environ.get("CALA_MCP_URL", "https://api.cala.ai/mcp/")
    )
    # Which Cala MCP tool(s) CalaMcpAdapter.search() uses:
    #   entity_first (default) -- entity_search->entity_retrieval (field-level,
    #     independently verifiable citations, see cala_mcp.py); knowledge_query
    #     if no named entity matches; knowledge_search only as a last resort.
    #   parallel -- entity_first's flow AND knowledge_search every time, facts
    #     from both merged. More coverage per call, more Cala API calls too.
    #   knowledge_search_only -- skip straight to knowledge_search.
    # knowledge_search's citations are coarser than entity_retrieval's (Cala's
    # own explainability decomposition, not our own field-level lookup) --
    # see cala_mcp.py's module docstring for what that trades off.
    cala_search_mode: str = field(
        default_factory=lambda: os.environ.get("CALA_SEARCH_MODE", "entity_first")
    )
    # How long to trust a cached /introspection result (which fields *exist*
    # for an entity) before re-fetching it. Schema-level metadata, not data --
    # the actual property *values* are never cached, always fetched fresh.
    # Default 24h; lower it if Cala's schema for tracked entities changes
    # faster than that in practice, raise it to cut API calls further.
    cala_introspection_cache_ttl_seconds: int = field(
        default_factory=lambda: int(os.environ.get("CALA_INTROSPECTION_CACHE_TTL_SECONDS", "86400"))
    )

    # entity_search/search_entities' `limit` controls how many name-match candidates
    # come back -- it does not mean "fully hydrate this many entities" (each hydration
    # is a separate introspection+retrieval/get_entity_summary round-trip). Confirmed
    # live 2026-08-18: a2k.ask's internal limit=50 (gateway/engine.py) meant up to 50
    # candidates each got fully hydrated for one query, ~458KB/~114K tokens of Facts for
    # a single a2k.ask call -- enough on its own to blow Bedrock's per-request byte limit.
    # See adapters/cala_mcp.py and adapters/sayari_mcp.py.
    max_entities_to_hydrate: int = field(
        default_factory=lambda: int(os.environ.get("A2K_MAX_ENTITIES_TO_HYDRATE", "3"))
    )

    # Cala's entity_retrieval accepts a `relationships` param (confirmed live 2026-08-19
    # against the real inputSchema) -- {"outgoing": {"IS_ULTIMATE_PARENT_OF": {}, ...},
    # "incoming": {"IS_SUBSIDIARY_OF": {}, ...}}, built from the relationship type names
    # entity_introspection discovers for that entity. Unset (default) requests every
    # discovered type in both directions -- one entity_retrieval call handles all of them,
    # so unlike max_entities_to_hydrate there's no per-call multiplication risk from doing
    # this by default. Set this if a specific entity's relationship fan-out ever produces a
    # response large enough to matter (Microsoft alone had 12 outgoing + 21 incoming types
    # -- confirmed live, not yet a problem in practice, but the knob exists for when it is).
    cala_max_relationship_types: int | None = field(
        default_factory=lambda: (
            int(os.environ["A2K_CALA_MAX_RELATIONSHIP_TYPES"])
            if os.environ.get("A2K_CALA_MAX_RELATIONSHIP_TYPES")
            else None
        )
    )

    # TEST-ONLY, requested explicitly 2026-08-18: when true, a2k.ask/a2k.search
    # short-circuit entirely for any request that includes (or doesn't restrict away)
    # Cala as a source -- instead of the normal cited-Facts pipeline, they return
    # Cala's own knowledge_search `content` (its LLM-synthesized prose) completely
    # unprocessed. See mcp_server/server.py's `_cala_raw_response_if_enabled`. Off by
    # default -- normal grounded behavior is unchanged unless this is explicitly set.
    cala_raw_knowledge_search: bool = field(
        default_factory=lambda: _bool_env("CALA_RAW_KNOWLEDGE_SEARCH", False)
    )

    # The MCP surface K2 sees is A2K-KCP 0.1: a2k.ask and a2k.search only
    # (a2k/kcp01/). The pre-0.1 tools -- vendor-forced ask/search, listVendors,
    # explain, getDocument, validateCitation, reportConflict, getAuditRecord and
    # the per-vendor card resources -- stay available under `internal.*` names
    # only when this is on: for our own evaluation scripts on test runtimes,
    # never on the runtime K2 calls. Off by default.
    internal_tools: bool = field(default_factory=lambda: _bool_env("A2K_INTERNAL_TOOLS", False))
    # Hosts of the vendors' own APIs: a citation pointing there is an internal
    # record of the vendor, not an origin document, and never reaches K2.
    vendor_api_hosts: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            h.strip().lower()
            for h in os.environ.get("VENDOR_API_HOSTS", "api.cala.ai,api.sayari.com,api.linkup.so").split(",")
            if h.strip()
        )
    )

    # Opt-in call tracing (gateway/tracing.py) -- off by default, since dumping
    # every request and upstream tool call/result to stdout is noisy and
    # results can be large (see max_entities_to_hydrate's history; tracing.py
    # truncates previews for the same reason). Added 2026-08-21 after
    # debugging the Cala relationships fix blind, with zero visibility into
    # what adapters/cala_mcp.py was actually sending/receiving.
    trace_calls: bool = field(default_factory=lambda: _bool_env("A2K_TRACE_CALLS", False))

    sayari_client_id: str | None = field(default_factory=lambda: _secret_env("SAYARI_CLIENT_ID"))
    sayari_client_secret: str | None = field(
        default_factory=lambda: _secret_env("SAYARI_CLIENT_SECRET")
    )
    sayari_base_url: str = field(
        default_factory=lambda: os.environ.get("SAYARI_BASE_URL", "https://api.sayari.com")
    )

    # Sayari's own hosted MCP server (confirmed live 2026-08-12): streamable-
    # http, Auth0 client-credentials auth -- a *separate* credential/grant
    # from the REST API's sayari_client_id/secret above (confirmed: REST
    # credentials are rejected on the MCP audience). See adapters/sayari_mcp.py.
    sayari_mcp_url: str = field(
        default_factory=lambda: os.environ.get("SAYARI_MCP_URL", "https://mcp.sayari.com/mcp")
    )
    sayari_auth0_client_id: str | None = field(default_factory=lambda: _secret_env("AUTH0_CLIENT_ID"))
    sayari_auth0_client_secret: str | None = field(
        default_factory=lambda: _secret_env("AUTH0_CLIENT_SECRET")
    )

    # -- Direct-discovery agent path (mcp_to_agent_to_mcp branch) -------------
    # a2k.ask/a2k.search (gateway/engine.py) call out to the agent's own
    # AgentCore Runtime instead of the deterministic Cala/Sayari adapters --
    # see agent/README.md "Calling the deployed agent" for why this can't go
    # through boto3's invoke_agent_runtime (that needs IAM inbound auth; the
    # agent's Runtime has JWT/Cognito inbound auth instead, switched
    # 2026-08-19 specifically to disable the IAM path). So this authenticates
    # against the agent's own Cognito pool via client-credentials -- a new M2M
    # app client on that pool, distinct from the Gateway's own inbound Cognito
    # client-credentials pair (agent/core.py's COGNITO_TOKEN_URL/SCOPE) --
    # and calls the agent's HTTPS /invocations URL directly with the resulting
    # Bearer token, same shape as agent/README.md's "Plain curl" example.
    agent_pool_client_id: str | None = field(default_factory=lambda: _secret_env("AGENT_POOL_CLIENT_ID"))
    agent_pool_client_secret: str | None = field(
        default_factory=lambda: _secret_env("AGENT_POOL_CLIENT_SECRET")
    )
    agent_pool_token_url: str | None = field(
        default_factory=lambda: os.environ.get("AGENT_POOL_TOKEN_URL")
    )
    agent_pool_scope: str | None = field(default_factory=lambda: os.environ.get("AGENT_POOL_SCOPE"))
    # The agent Runtime's invocation URL, e.g.
    # "https://bedrock-agentcore.eu-west-1.amazonaws.com/runtimes/<url-encoded-ARN>/invocations?qualifier=DEFAULT"
    # -- see agent/README.md's "Plain curl" example for how this is built from
    # the Runtime ARN. Kept as a single pre-built URL rather than an ARN this
    # module re-encodes, since URL-encoding a colon-and-slash-heavy ARN is
    # easy to get subtly wrong and there's only ever one agent Runtime to
    # point at.
    agent_runtime_url: str | None = field(default_factory=lambda: os.environ.get("AGENT_RUNTIME_URL"))

    # Vendor KB Cards (a2k/cards/__init__.py) -- DynamoDB table name, partition
    # key `sourceId` (string), one attribute `cardJson` per item holding the
    # full KBCard as a JSON string. Unset (the default) means "no DynamoDB
    # access configured" -- cards/__init__.py falls back to the local
    # cala_card.json/sayari_card.json files instead, same as before this
    # existed, so local dev and the test suite need no AWS access at all.
    # Deliberately NOT a _secret_env() -- a table name isn't a credential.
    vendor_cards_table: str | None = field(default_factory=lambda: os.environ.get("VENDOR_CARDS_TABLE"))
    # How long a DynamoDB Scan's result is trusted before the next call
    # re-fetches -- see cards/__init__.py's module docstring for why this is
    # a short lazy-refresh TTL, not a scheduled daily job.
    vendor_cards_cache_ttl_seconds: int = field(
        default_factory=lambda: int(os.environ.get("VENDOR_CARDS_CACHE_TTL_SECONDS", "600"))
    )

    # -- Cache (a2k/cache/, Propuesta_cache_CdV) -------------------------------
    # off    -- no cache at all: every ask goes to the vendor, nothing admitted.
    # shadow -- phase 1: retrieval runs on every ask and the serving log says
    #           what the cache would have served, but the user always gets the
    #           vendor answer; vendor answers go through admission.
    # serve  -- a cache hit is served; a miss goes to the vendor and admission.
    # Anything that needs Aurora/S3 also stays off until they are configured
    # (see cache_ready).
    cache_mode: str = field(default_factory=lambda: os.environ.get("CACHE_MODE", "off").strip().lower())
    # Per-domain switch (proposal Sheet 6), e.g. "MED=off,RSK=shadow". A domain
    # not listed follows CACHE_MODE, and no domain can go beyond it: with
    # CACHE_MODE=shadow, "REG=serve" still means shadow. See domain_mode().
    # Environment variable for now (a restart to change it); AppConfig later.
    cache_domain_modes: dict[str, str] = field(default_factory=lambda: _parse_domain_modes(os.environ.get("CACHE_DOMAIN_MODES", "")))
    cache_embedding_model_id: str = field(
        default_factory=lambda: os.environ.get("CACHE_EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0")
    )
    # The retrieval Checker (two filters), kept as tested (D4).
    cache_checker_model_id: str = field(
        default_factory=lambda: os.environ.get("CACHE_CHECKER_MODEL_ID", "eu.anthropic.claude-sonnet-4-5-20250929-v1:0")
    )
    # D15: time the synchronous cache path may take before the question goes
    # to the vendor. Enforced in serve mode; in shadow mode it is only recorded
    # (within_budget). 10 s, not the proposal's provisional 3 s: the first real
    # end-to-end run (2026-10-01) took ~5.5 s with one candidate, ~4 s of it in
    # the Checker's two calls -- at 3 s every candidate would have timed out.
    cache_time_budget_seconds: float = field(
        default_factory=lambda: float(os.environ.get("CACHE_TIME_BUDGET_SECONDS", "10"))
    )
    # D5: reformulation window -- a question within this many seconds of the
    # previous turn of the same session (a2k.ask sessionId) is checked as a
    # possible reformulation. Also how long a session record is kept (D10).
    cache_reformulation_window_seconds: float = field(
        default_factory=lambda: float(os.environ.get("CACHE_REFORMULATION_WINDOW_SECONDS", "600"))
    )
    # E7.4: CloudWatch namespace of the cache metrics (a2k/cache/metrics.py,
    # Embedded Metric Format on stdout); empty turns them off.
    cache_metrics_namespace: str = field(default_factory=lambda: os.environ.get("CACHE_METRICS_NAMESPACE", "A2K/Cache").strip())
    # Price overrides for the cost estimate (a2k/cache/usage.py), JSON:
    # {"model id substring": [USD per M input tokens, USD per M output tokens]}.
    cache_model_prices: dict[str, tuple[float, float]] = field(
        default_factory=lambda: {k: tuple(v) for k, v in json.loads(os.environ.get("CACHE_MODEL_PRICES") or "{}").items()}
    )
    # How often, at most, admission reports the cache size by state (seconds).
    cache_size_metric_seconds: float = field(default_factory=lambda: float(os.environ.get("CACHE_SIZE_METRIC_SECONDS", "300")))
    # Shadow check (E7.4, false hits): a would-be hit that is not served is
    # compared, in the background, with the fresh vendor answer.
    cache_shadow_check: bool = field(default_factory=lambda: os.environ.get("CACHE_SHADOW_CHECK", "true").strip().lower() == "true")

    # Aurora PostgreSQL + pgvector, reached through the RDS Data API (see
    # a2k/cache/store.py for why not a driver connection). Schema in
    # deploy/aurora/schema.sql. ARNs are not credentials -- the DB password
    # lives in the secret they point to.
    aurora_cluster_arn: str | None = field(default_factory=lambda: os.environ.get("AURORA_CLUSTER_ARN"))
    aurora_secret_arn: str | None = field(default_factory=lambda: os.environ.get("AURORA_SECRET_ARN"))
    aurora_database: str = field(default_factory=lambda: os.environ.get("AURORA_DATABASE", "a2k_cache"))
    cache_evidence_bucket: str | None = field(default_factory=lambda: os.environ.get("CACHE_EVIDENCE_BUCKET"))
    # D11: only vendors whose contract allows storing and reusing their
    # answers. Cala from the start; Sayari/Linkup once legal confirms.
    admission_allowed_vendors: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            v.strip().lower() for v in os.environ.get("ADMISSION_ALLOWED_VENDORS", "cala").split(",") if v.strip()
        )
    )
    # Light model: classifier, translation to English, and retrieval's
    # normalise-and-classify call.
    admission_light_model_id: str = field(
        default_factory=lambda: os.environ.get("ADMISSION_LIGHT_MODEL_ID", "eu.anthropic.claude-haiku-4-5-20251001-v1:0")
    )
    admission_judge_model_id: str = field(
        default_factory=lambda: os.environ.get("ADMISSION_JUDGE_MODEL_ID", "eu.anthropic.claude-sonnet-5")
    )
    admission_min_answer_chars: int = field(default_factory=lambda: int(os.environ.get("ADMISSION_MIN_ANSWER_CHARS", "150")))
    admission_max_evidence_chars: int = field(
        default_factory=lambda: int(os.environ.get("ADMISSION_MAX_EVIDENCE_CHARS", "400000"))
    )
    admission_require_verified_quotes: bool = field(
        default_factory=lambda: _bool_env("ADMISSION_REQUIRE_VERIFIED_QUOTES", True)
    )
    admission_workers: int = field(default_factory=lambda: int(os.environ.get("ADMISSION_WORKERS", "2")))
    # Second, confirming judge call on FIT verdicts -- see AdmissionSettings.confirm_fit.
    admission_confirm_fit: bool = field(default_factory=lambda: _bool_env("ADMISSION_CONFIRM_FIT", True))
    # "basic" (default) or "strict" -- see a2k/cache/judge_basic.py.
    admission_mode: str = field(default_factory=lambda: os.environ.get("ADMISSION_MODE", "basic").strip().lower())
    # Basic mode: domains where every statement must be grounded in the tool output (C3).
    admission_grounding_domains: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            d.strip().upper() for d in os.environ.get("ADMISSION_GROUNDING_DOMAINS", "RSK").split(",") if d.strip()
        )
    )
    # What the agent answers when a vendor is unavailable (agent/direct_agent.py,
    # E4.9 / D17). Filter T2 discards it by exact match, so it must be the same
    # value as the agent Runtime's own VENDOR_FALLBACK_MESSAGE.
    vendor_fallback_message: str = field(
        default_factory=lambda: os.environ.get(
            "VENDOR_FALLBACK_MESSAGE", "Actualmente no se encuentra disponible esta información. Inténtelo más tarde."
        )
    )

    audit_log_path: Path = field(
        default_factory=lambda: Path(
            os.environ.get("A2K_AUDIT_LOG_PATH", str(REPO_ROOT / "audit.jsonl"))
        )
    )
    # Level 3+/S2 concept (KCP section 11); this gateway targets Level 2 with
    # public/S0 data, so plain append-only logging is sufficient -- see
    # README "Conformance". Flip on if this box is ever repointed at
    # non-public data.
    immutable_audit: bool = field(default_factory=lambda: _bool_env("A2K_IMMUTABLE_AUDIT", False))
    audit_stdout: bool = field(default_factory=lambda: _bool_env("A2K_AUDIT_STDOUT", True))

    # httpx bundles its own certifi CA store and, unlike `requests`, does not
    # read SSL_CERT_FILE/REQUESTS_CA_BUNDLE on its own -- on networks with a
    # TLS-inspecting corporate proxy (self-signed root injected into the
    # chain), outbound calls to Cala/Sayari fail CERTIFICATE_VERIFY_FAILED
    # unless we point verify= at that CA bundle explicitly.
    ca_bundle: str | None = field(
        default_factory=lambda: os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    )

    host: str = field(default_factory=lambda: os.environ.get("A2K_BOX_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(os.environ.get("A2K_BOX_PORT", "8080")))
    public_url: str = field(
        default_factory=lambda: os.environ.get("A2K_BOX_PUBLIC_URL", "http://localhost:8080")
    )

    @property
    def is_mock(self) -> bool:
        return self.mode != "live"

    @property
    def cala_live_ready(self) -> bool:
        return bool(self.cala_api_key)

    @property
    def sayari_live_ready(self) -> bool:
        return bool(self.sayari_client_id and self.sayari_client_secret)

    @property
    def sayari_mcp_live_ready(self) -> bool:
        return bool(self.sayari_auth0_client_id and self.sayari_auth0_client_secret)

    @property
    def agent_call_ready(self) -> bool:
        return bool(
            self.agent_pool_client_id
            and self.agent_pool_client_secret
            and self.agent_pool_token_url
            and self.agent_runtime_url
        )

    @property
    def httpx_verify(self) -> str | bool:
        return self.ca_bundle or True

    @property
    def cache_ready(self) -> bool:
        """shadow or serve, with Aurora and the evidence bucket configured."""
        return bool(
            self.cache_mode in ("shadow", "serve")
            and self.aurora_cluster_arn
            and self.aurora_secret_arn
            and self.cache_evidence_bucket
        )

    @property
    def cache_serving(self) -> bool:
        return self.cache_ready and self.cache_mode == "serve"

    def domain_mode(self, domain: str) -> str:
        """Effective mode of one data domain: the stricter of CACHE_MODE and
        its own CACHE_DOMAIN_MODES entry."""
        order = ("off", "shadow", "serve")
        global_mode = self.cache_mode if self.cache_mode in order else "off"
        own = self.cache_domain_modes.get(domain.upper(), global_mode)
        return order[min(order.index(global_mode), order.index(own))]

    @property
    def cache_disabled_domains(self) -> tuple[str, ...]:
        return tuple(sorted(d for d, m in self.cache_domain_modes.items() if self.domain_mode(d) == "off"))


config = Config()
