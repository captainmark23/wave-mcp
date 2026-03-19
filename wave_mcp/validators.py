"""Shared validation functions for Wave MCP Server."""

from datetime import datetime

from wave_mcp.constants import SESSION_ID_PATTERN, VALID_SESSION_TYPES


def _validate_session_id(v: str) -> str:
    """Validate that a session ID contains only safe characters."""
    v = v.strip()
    if not SESSION_ID_PATTERN.match(v):
        raise ValueError("session_id must contain only alphanumeric characters, hyphens, and underscores")
    return v


def _validate_iso_date(v: str | None, field_name: str) -> str | None:
    """Validate an optional ISO 8601 date string."""
    if v is None:
        return None
    v = v.strip()
    try:
        datetime.fromisoformat(v)
    except ValueError:
        raise ValueError(
            f"{field_name} must be a valid ISO 8601 date (e.g. '2025-01-01' or '2025-01-01T00:00:00Z')"
        ) from None
    return v


def _validate_session_type(v: str | None) -> str | None:
    """Validate an optional session type against the allowlist."""
    if v is not None:
        v = v.strip().lower()
        if v not in VALID_SESSION_TYPES:
            raise ValueError(f"session_type must be one of: {', '.join(sorted(VALID_SESSION_TYPES))}. Got: '{v}'")
    return v
