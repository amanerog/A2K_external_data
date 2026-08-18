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

    # TEST-ONLY, requested explicitly 2026-08-18: when true, a2k.ask/a2k.search
    # short-circuit entirely for any request that includes (or doesn't restrict away)
    # Cala as a source -- instead of the normal cited-Facts pipeline, they return
    # Cala's own knowledge_search `content` (its LLM-synthesized prose) completely
    # unprocessed. See mcp_server/server.py's `_cala_raw_response_if_enabled`. Off by
    # default -- normal grounded behavior is unchanged unless this is explicitly set.
    cala_raw_knowledge_search: bool = field(
        default_factory=lambda: _bool_env("CALA_RAW_KNOWLEDGE_SEARCH", False)
    )

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
    def httpx_verify(self) -> str | bool:
        return self.ca_bundle or True


config = Config()
