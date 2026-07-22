"""A2K-KCP error model (A2K-KCP-Consumption 4.md, section 17)."""

from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"
    UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
    UNSUPPORTED_OPERATION = "UNSUPPORTED_OPERATION"
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    AUTHORIZATION_FAILED = "AUTHORIZATION_FAILED"
    OBO_ASSERTION_REQUIRED = "OBO_ASSERTION_REQUIRED"
    OBO_ASSERTION_INVALID = "OBO_ASSERTION_INVALID"
    ACCESS_DENIED = "ACCESS_DENIED"
    NOT_FOUND = "NOT_FOUND"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    AMBIGUOUS_QUERY = "AMBIGUOUS_QUERY"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    STALE_SOURCE = "STALE_SOURCE"
    GROUNDING_VIOLATION = "GROUNDING_VIOLATION"
    STRICT_GROUNDING_UNSUPPORTED = "STRICT_GROUNDING_UNSUPPORTED"
    CONFLICT_DETECTED = "CONFLICT_DETECTED"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"
    PAGINATION_CURSOR_INVALID = "PAGINATION_CURSOR_INVALID"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    SCHEMA_VALIDATION_FAILED = "SCHEMA_VALIDATION_FAILED"
    SIGNATURE_REQUIRED = "SIGNATURE_REQUIRED"
    SIGNATURE_INVALID = "SIGNATURE_INVALID"
    REPLAY_DETECTED = "REPLAY_DETECTED"
    REGULATED_FEATURE_UNSUPPORTED = "REGULATED_FEATURE_UNSUPPORTED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class A2KError(Exception):
    """Raised by gateway/adapter code; caught at the transport edge and turned
    into ``{ok: false, error: {...}}`` per the KCP error model."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }
