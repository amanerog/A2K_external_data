"""Detached response signatures (A2K-KCP-Consumption 4.md, section 14.3, Level 4).

Honesty note (see README "Simplificaciones"): canonicalization here is a
practical approximation of JCS/RFC 8785 (sorted keys, no whitespace, UTF-8) --
not a certified RFC 8785 implementation (no Unicode NFC/number-format edge
cases handled). `jws` carries a raw base64url Ed25519 signature over that
canonical payload, not a full JWS compact serialization. Good enough to prove
"this exact response left this gateway unmodified"; not a drop-in replacement
for a vetted JOSE library in a real production deployment.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ..config import config

SIGNED_FIELDS = [
    "a2kVersion",
    "operation",
    "sourceKbId",
    "answer",
    "claims",
    "citations",
    "grounding",
    "freshness",
    "accessDecision",
    "audit",
]

_KEY_FRAGMENT = "gateway-key-1"

_private_key: Ed25519PrivateKey | None = None


def _load_or_create_key() -> Ed25519PrivateKey:
    path = config.signing_key_path
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return serialization.load_pem_private_key(path.read_bytes(), password=None)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.write_bytes(pem)
    return key


def get_private_key() -> Ed25519PrivateKey:
    global _private_key
    if _private_key is None:
        _private_key = _load_or_create_key()
    return _private_key


def _kid() -> str:
    return f"{config.public_url}/.well-known/jwks.json#{_KEY_FRAGMENT}"


def canonicalize(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign_envelope(envelope_dict: dict[str, Any]) -> dict[str, Any]:
    subset = {k: envelope_dict.get(k) for k in SIGNED_FIELDS}
    payload = canonicalize(subset)
    signature = get_private_key().sign(payload)
    return {
        "alg": "EdDSA",
        "kid": _kid(),
        "canonicalization": "sorted-keys-json-utf8 (JCS/RFC8785 approximation)",
        "signedFields": SIGNED_FIELDS,
        "jws": base64.urlsafe_b64encode(signature).decode("ascii"),
    }


def verify_envelope(envelope_dict: dict[str, Any], jws_b64: str) -> bool:
    subset = {k: envelope_dict.get(k) for k in SIGNED_FIELDS}
    payload = canonicalize(subset)
    public_key = get_private_key().public_key()
    try:
        public_key.verify(base64.urlsafe_b64decode(jws_b64), payload)
        return True
    except Exception:
        return False


def jwks() -> dict[str, Any]:
    public_key = get_private_key().public_key()
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    x = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return {
        "keys": [
            {
                "kty": "OKP",
                "crv": "Ed25519",
                "x": x,
                "kid": _kid(),
                "use": "sig",
                "alg": "EdDSA",
            }
        ]
    }
