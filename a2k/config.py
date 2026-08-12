"""Environment-driven configuration for the A2K box.

Mock mode is the default because no live Cala/Sayari credentials exist yet
(see plan decision log). Flipping to live mode requires only setting
A2K_BOX_MODE=live plus the provider credentials below -- adapters read this
module, never os.environ directly, so the mock/live branch lives in one place.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class Config:
    mode: str = field(default_factory=lambda: os.environ.get("A2K_BOX_MODE", "mock").lower())

    cala_api_key: str | None = field(default_factory=lambda: os.environ.get("CALA_API_KEY"))
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

    sayari_client_id: str | None = field(default_factory=lambda: os.environ.get("SAYARI_CLIENT_ID"))
    sayari_client_secret: str | None = field(
        default_factory=lambda: os.environ.get("SAYARI_CLIENT_SECRET")
    )
    sayari_base_url: str = field(
        default_factory=lambda: os.environ.get("SAYARI_BASE_URL", "https://api.sayari.com")
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
    def httpx_verify(self) -> str | bool:
        return self.ca_bundle or True


config = Config()
