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

    signing_key_path: Path = field(
        default_factory=lambda: Path(
            os.environ.get("A2K_SIGNING_KEY_PATH", str(REPO_ROOT / "keys" / "gateway_ed25519.pem"))
        )
    )
    audit_log_path: Path = field(
        default_factory=lambda: Path(
            os.environ.get("A2K_AUDIT_LOG_PATH", str(REPO_ROOT / "audit.jsonl"))
        )
    )
    immutable_audit: bool = field(default_factory=lambda: _bool_env("A2K_IMMUTABLE_AUDIT", True))
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
