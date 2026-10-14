"""Password hashing for the page's users (deploy/lambda/users.py writes
them, app.py checks them): PBKDF2-HMAC-SHA256 from the standard library,
stored as "pbkdf2_sha256$<iterations>$<salt b64>$<hash b64>". Never the
password itself."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 600_000  # OWASP's 2023 figure for PBKDF2-HMAC-SHA256


def _derive(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def hash_password(password: str, *, iterations: int = ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = _derive(password, salt, iterations)
    return f"{ALGORITHM}${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt, digest = stored.split("$")
        if algorithm != ALGORITHM:
            return False
        expected = base64.b64decode(digest)
        return hmac.compare_digest(_derive(password, base64.b64decode(salt), int(iterations)), expected)
    except (ValueError, TypeError):
        return False


def new_password() -> str:
    """A random password to hand to a new user (24 URL-safe characters)."""
    return secrets.token_urlsafe(18)
