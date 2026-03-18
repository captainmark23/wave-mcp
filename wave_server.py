#!/usr/bin/env python3
"""
MCP Server for Wave — call recording, transcription, and meeting intelligence.

Provides tools to list sessions, pull transcripts, search across meetings,
export in bulk, and manage session metadata via Wave's REST API.
"""

import asyncio
import json
import logging
import logging.handlers
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, NoReturn, Optional

import httpx
from mcp.server.fastmcp import Context, FastMCP

try:
    from mcp.server.fastmcp import ToolError
except ImportError:

    class ToolError(Exception):
        """MCP tool error — sets isError=true in the response."""

        pass
from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("wave_mcp")
logger.setLevel(logging.INFO)
_log_fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s")

# CRITICAL: Use stderr for console logging — stdout is reserved for MCP stdio transport
_stderr_handler = logging.StreamHandler(sys.stderr)
_stderr_handler.setFormatter(_log_fmt)
logger.addHandler(_stderr_handler)

# Persistent file logging with rotation (5MB max, 3 backups)
_log_dir = Path.home() / ".wave-mcp"
_log_dir.mkdir(parents=True, exist_ok=True)
_file_handler = logging.handlers.RotatingFileHandler(
    _log_dir / "wave-mcp.log",
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8",
)
_file_handler.setFormatter(_log_fmt)
logger.addHandler(_file_handler)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

API_BASE_URL = "https://api.wave.co/v1"
DEFAULT_TIMEOUT = 30.0
MAX_PAGINATION_LIMIT = 100
DEFAULT_PAGINATION_LIMIT = 20
MAX_BULK_SESSIONS = 50
MAX_SEARCH_RESULTS = 50
MAX_TAGS = 20
MAX_TAG_LENGTH = 100
MAX_TITLE_LENGTH = 500
MAX_NOTES_LENGTH = 50_000
MAX_CURSOR_LENGTH = 500

# Session IDs must be alphanumeric with hyphens/underscores only — prevents path traversal
SESSION_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")

# Known session types — allowlist for defense-in-depth
VALID_SESSION_TYPES = {"meeting", "call", "webinar", "interview", "presentation"}


# ---------------------------------------------------------------------------
# Response format enum
# ---------------------------------------------------------------------------

class ResponseFormat(str, Enum):
    """Output format for tool responses."""
    MARKDOWN = "markdown"
    JSON = "json"


# ---------------------------------------------------------------------------
# Rate limiter (asyncio-safe)
# ---------------------------------------------------------------------------

class _RateLimiter:
    """Sliding-window rate limiter with asyncio lock for concurrency safety."""

    def __init__(self, max_per_minute: int = 50):
        self._max = max_per_minute
        self._timestamps: list[float] = []
        self._lock = asyncio.Lock()

    async def check(self) -> bool:
        """Return True if a request is allowed, False if rate-limited."""
        async with self._lock:
            now = time.monotonic()
            self._timestamps = [t for t in self._timestamps if now - t < 60]
            if len(self._timestamps) >= self._max:
                return False
            self._timestamps.append(now)
            return True

    @property
    def remaining(self) -> int:
        now = time.monotonic()
        active = [t for t in self._timestamps if now - t < 60]
        return max(0, self._max - len(active))


# ---------------------------------------------------------------------------
# Lifespan — persistent HTTP client with auth
# ---------------------------------------------------------------------------

@dataclass
class AppContext:
    """Shared state available to all tools via lifespan."""
    client: httpx.AsyncClient
    rate_limiter: _RateLimiter = field(default_factory=_RateLimiter)


@asynccontextmanager
async def app_lifespan(server: FastMCP):
    """Create a persistent httpx client with Wave auth for the server lifetime."""
    token = os.environ.get("WAVE_API_TOKEN", "")
    if not token:
        raise RuntimeError(
            "WAVE_API_TOKEN environment variable is required. "
            "Generate one at https://app.wave.co/settings/integrations/api"
        )

    async with httpx.AsyncClient(
        base_url=API_BASE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        timeout=DEFAULT_TIMEOUT,
    ) as client:
        logger.info("Wave MCP server started — connected to %s", API_BASE_URL)
        yield AppContext(client=client)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "wave_mcp",
    instructions=(
        "Wave MCP server — access call recordings, transcripts, and meeting data. "
        "Use wave_list_sessions to browse recent meetings, wave_get_transcript to "
        "pull full speaker-attributed transcripts for analysis, and wave_search_sessions "
        "to find discussions by topic. Combine with wave_bulk_export for batch processing. "
        "All tools support response_format='json' for structured output or 'markdown' (default) "
        "for human-readable output."
    ),
    lifespan=app_lifespan,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_client(ctx: Context) -> httpx.AsyncClient:
    """Extract the httpx client from lifespan context."""
    return ctx.request_context.lifespan_context.client


def _get_rate_limiter(ctx: Context) -> _RateLimiter:
    """Extract the rate limiter from lifespan context."""
    return ctx.request_context.lifespan_context.rate_limiter


async def _check_rate_limit(ctx: Context) -> None:
    """Raise ToolError if rate-limited."""
    rl = _get_rate_limiter(ctx)
    if not await rl.check():
        raise ToolError(
            "Client-side rate limit reached (50 requests/min). "
            "Wait a moment before retrying. This protects your Wave API quota "
            "(60 req/min, 1000/day)."
        )


def _sanitize_md(text: str | None) -> str:
    """Escape markdown special characters and structural newlines in API-sourced data.

    Prevents markdown injection by escaping formatting chars and collapsing
    embedded newlines that could inject headings, horizontal rules, or block elements.
    """
    if text is None:
        return ""
    # Replace newlines with spaces to prevent structural markdown injection
    # (e.g., injected \\n---\\n or \\n# Heading)
    text = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    # Escape characters that could alter markdown rendering
    for ch in r"\`*_{}[]()#+-.!|>~":
        text = text.replace(ch, f"\\{ch}")
    return text


def _handle_api_error(e: Exception) -> NoReturn:
    """Raise ToolError with actionable messages for common failure modes.

    Raising ToolError sets isError=true in the MCP response so clients
    can distinguish errors from normal tool output.
    """
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        logger.warning("Wave API error: status=%s url=%s", status, e.request.url.path)
        if status == 401:
            raise ToolError(
                "Authentication failed. Your Wave API token may be invalid or expired. "
                "Generate a new one at https://app.wave.co/settings/integrations/api"
            )
        if status == 403:
            raise ToolError(
                "Permission denied. Your token may lack the required scope for this operation. "
                "Check token permissions at https://app.wave.co/settings/integrations/api"
            )
        if status == 404:
            raise ToolError("Session not found. Please verify the session ID is correct.")
        if status == 429:
            raise ToolError(
                "Rate limit exceeded (60 requests/min, 1000/day). "
                "Wait a moment before retrying."
            )
        if status == 422:
            try:
                detail = e.response.json()
                msg = detail.get("message", detail.get("detail", "Invalid request parameters"))
                raise ToolError(f"Validation failed — {msg}")
            except ToolError:
                raise
            except Exception:
                raise ToolError("Request validation failed. Check your input parameters.")
        raise ToolError(f"Wave API returned status {status}.")
    if isinstance(e, httpx.TimeoutException):
        logger.warning("Wave API timeout")
        raise ToolError("Request timed out. Wave may be temporarily unavailable — try again shortly.")
    if isinstance(e, httpx.ConnectError):
        logger.warning("Wave API connection error")
        raise ToolError("Could not connect to Wave API. Check your network connection.")
    # Generic fallback — never expose raw exception details
    logger.error("Unexpected error communicating with Wave API: %s", type(e).__name__, exc_info=True)
    raise ToolError("An unexpected error occurred while communicating with the Wave API. Please try again.")


def _format_duration(seconds: float | int | None) -> str:
    """Convert seconds to human-readable duration."""
    if seconds is None:
        return "unknown"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins}m"


def _format_session_list_md(sessions: list[dict]) -> str:
    """Format a list of sessions as readable markdown."""
    if not sessions:
        return "No sessions found."
    lines = []
    for s in sessions:
        title = _sanitize_md(s.get("title", "Untitled"))
        sid = s.get("id", "")
        ts = s.get("timestamp", "unknown date")
        dur = _format_duration(s.get("duration_seconds"))
        stype = s.get("type", "unknown")
        platform = s.get("platform") or ""
        platform_str = f" ({_sanitize_md(platform)})" if platform else ""
        lines.append(f"- **{title}** — {ts} · {dur} · {stype}{platform_str}  \n  `id: {sid}`")
    return "\n".join(lines)


def _format_session_detail_md(s: dict) -> str:
    """Format a single session's full details as markdown."""
    lines = [f"# {_sanitize_md(s.get('title', 'Untitled'))}"]
    lines.append("")
    lines.append(f"**ID:** `{s.get('id', '')}`")
    lines.append(f"**Date:** {s.get('timestamp', 'unknown')}")
    lines.append(f"**Duration:** {_format_duration(s.get('duration_seconds'))}")
    lines.append(f"**Type:** {s.get('type', 'unknown')}")
    if s.get("platform"):
        lines.append(f"**Platform:** {_sanitize_md(s['platform'])}")
    if s.get("language"):
        lines.append(f"**Language:** {_sanitize_md(s['language'])}")
    lines.append(f"**Favorite:** {'Yes' if s.get('favorite') else 'No'}")
    if s.get("tags"):
        lines.append(f"**Tags:** {', '.join(_sanitize_md(t) for t in s['tags'])}")
    if s.get("summary"):
        lines.append(f"\n## Summary\n{_sanitize_md(s['summary'])}")
    if s.get("notes"):
        lines.append(f"\n## Notes\n{_sanitize_md(s['notes'])}")
    return "\n".join(lines)


def _json_response(data: Any) -> str:
    """Serialize data to a compact JSON string for structured responses."""
    return json.dumps(data, indent=2, default=str)


# ---------------------------------------------------------------------------
# Validators (shared)
# ---------------------------------------------------------------------------

def _validate_session_id(v: str) -> str:
    """Validate that a session ID contains only safe characters."""
    v = v.strip()
    if not SESSION_ID_PATTERN.match(v):
        raise ValueError(
            "session_id must contain only alphanumeric characters, hyphens, and underscores"
        )
    return v


def _validate_iso_date(v: str | None, field_name: str) -> str | None:
    """Validate an optional ISO 8601 date string."""
    if v is None:
        return None
    v = v.strip()
    try:
        datetime.fromisoformat(v)
    except ValueError:
        raise ValueError(f"{field_name} must be a valid ISO 8601 date (e.g. '2025-01-01' or '2025-01-01T00:00:00Z')")
    return v


# ---------------------------------------------------------------------------
# Input Models
# ---------------------------------------------------------------------------

class ListSessionsInput(BaseModel):
    """Parameters for listing Wave sessions."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    limit: Optional[int] = Field(
        default=DEFAULT_PAGINATION_LIMIT,
        description="Number of sessions to return (1–100, default 20)",
        ge=1, le=MAX_PAGINATION_LIMIT,
    )
    cursor: Optional[str] = Field(
        default=None,
        description="Pagination cursor from a previous response's next_cursor",
        max_length=MAX_CURSOR_LENGTH,
    )
    since: Optional[str] = Field(
        default=None,
        description="Only return sessions newer than this ISO 8601 date (e.g. '2025-01-01')",
    )
    session_type: Optional[str] = Field(
        default=None,
        description="Filter by session type: 'meeting', 'call', 'webinar', 'interview', or 'presentation'",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for human-readable (default) or 'json' for structured data",
    )

    @field_validator("since")
    @classmethod
    def validate_since(cls, v: str | None) -> str | None:
        return _validate_iso_date(v, "since")

    @field_validator("cursor")
    @classmethod
    def validate_cursor(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.strip()
            if not v:
                return None
            # Cursors should be printable ASCII — reject control chars
            if not v.isprintable():
                raise ValueError("cursor must contain only printable characters")
        return v

    @field_validator("session_type")
    @classmethod
    def validate_session_type(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.strip().lower()
            if v not in VALID_SESSION_TYPES:
                raise ValueError(
                    f"session_type must be one of: {', '.join(sorted(VALID_SESSION_TYPES))}. "
                    f"Got: '{v}'"
                )
        return v


class GetSessionInput(BaseModel):
    """Parameters for retrieving a single session."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    session_id: str = Field(
        ..., description="The Wave session ID to retrieve (e.g. 'ses_abc123')", min_length=1
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for human-readable (default) or 'json' for structured data",
    )

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, v: str) -> str:
        return _validate_session_id(v)


class GetTranscriptInput(BaseModel):
    """Parameters for retrieving a session transcript."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    session_id: str = Field(
        ..., description="The Wave session ID whose transcript to retrieve", min_length=1
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for human-readable (default) or 'json' for structured data",
    )

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, v: str) -> str:
        return _validate_session_id(v)


class SearchSessionsInput(BaseModel):
    """Parameters for semantic search across sessions."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(
        ...,
        description=(
            "Natural language search query — describe what you're looking for "
            "(e.g. 'pricing discussion with Acme Corp', 'action items from last standup')"
        ),
        min_length=1,
        max_length=500,
    )
    limit: Optional[int] = Field(
        default=10,
        description="Max results to return (1–50, default 10)",
        ge=1, le=MAX_SEARCH_RESULTS,
    )
    offset: Optional[int] = Field(
        default=0,
        description="Number of results to skip for pagination (default 0)",
        ge=0,
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for human-readable (default) or 'json' for structured data",
    )

    @field_validator("query")
    @classmethod
    def validate_query(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Search query cannot be empty")
        return v.strip()


class GetStatsInput(BaseModel):
    """Parameters for aggregated session statistics."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    since: Optional[str] = Field(
        default=None,
        description="Start date for stats period (ISO 8601). Defaults to 30 days ago.",
    )
    until: Optional[str] = Field(
        default=None,
        description="End date for stats period (ISO 8601). Defaults to now.",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for human-readable (default) or 'json' for structured data",
    )

    @field_validator("since")
    @classmethod
    def validate_since(cls, v: str | None) -> str | None:
        return _validate_iso_date(v, "since")

    @field_validator("until")
    @classmethod
    def validate_until(cls, v: str | None) -> str | None:
        return _validate_iso_date(v, "until")


class BulkExportInput(BaseModel):
    """Parameters for bulk session export."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    session_ids: list[str] = Field(
        ...,
        description="List of session IDs to export (1–50)",
        min_length=1,
        max_length=MAX_BULK_SESSIONS,
    )
    include_transcript: bool = Field(
        default=True,
        description="Include full transcript text in each session (default true)",
    )
    include_summary: bool = Field(
        default=True,
        description="Include AI summary in each session (default true)",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for human-readable (default) or 'json' for structured data",
    )

    @field_validator("session_ids")
    @classmethod
    def validate_session_ids(cls, v: list[str]) -> list[str]:
        return [_validate_session_id(sid) for sid in v]


class GetAccountInput(BaseModel):
    """Parameters for retrieving Wave account info."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for human-readable (default) or 'json' for structured data",
    )


class GetMediaInput(BaseModel):
    """Parameters for retrieving signed media URLs."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    session_id: str = Field(
        ..., description="The Wave session ID to get audio/video URLs for", min_length=1
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for human-readable (default) or 'json' for structured data",
    )

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, v: str) -> str:
        return _validate_session_id(v)


class UpdateSessionInput(BaseModel):
    """Parameters for updating session metadata."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    session_id: str = Field(
        ..., description="The Wave session ID to update", min_length=1
    )
    title: Optional[str] = Field(
        default=None,
        description="New session title (max 500 chars)",
        max_length=MAX_TITLE_LENGTH,
    )
    notes: Optional[str] = Field(
        default=None,
        description="New session notes (max 50,000 chars)",
        max_length=MAX_NOTES_LENGTH,
    )
    tags: Optional[list[str]] = Field(
        default=None,
        description="Replace tags (max 20 tags, each max 100 chars)",
        max_length=MAX_TAGS,
    )
    favorite: Optional[bool] = Field(
        default=None,
        description="Set or unset favorite status",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for human-readable (default) or 'json' for structured data",
    )

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, v: str) -> str:
        return _validate_session_id(v)

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            for tag in v:
                if len(tag) > MAX_TAG_LENGTH:
                    raise ValueError(f"Tag '{tag[:20]}...' exceeds {MAX_TAG_LENGTH} character limit")
        return v


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool(
    name="wave_list_sessions",
    annotations={
        "title": "List Wave Sessions",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def wave_list_sessions(params: ListSessionsInput, ctx: Context) -> str:
    """List your recent Wave call recordings and meetings.

    Returns sessions sorted by date (newest first) with title, date, duration,
    type, and platform. Use the cursor from the response to paginate through
    older sessions. Use the 'since' parameter to filter by date.

    Args:
        params (ListSessionsInput): Validated input parameters containing:
            - limit (Optional[int]): Number of sessions to return, 1–100 (default 20).
            - cursor (Optional[str]): Pagination cursor from a previous response.
            - since (Optional[str]): ISO 8601 date — only return sessions after this date.
            - session_type (Optional[str]): Filter by type: 'meeting', 'call', 'webinar', 'interview', 'presentation'.
            - response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: Response in the requested format.

        JSON schema (response_format='json'):
        {
            "total_count": int,           # Total sessions matching filters (if available from API)
            "count": int,                 # Number of sessions in this page
            "has_more": bool,             # Whether more pages are available
            "next_cursor": str | null,    # Cursor for the next page (null if no more)
            "sessions": [
                {
                    "id": str,                    # Session ID (e.g. "ses_abc123")
                    "title": str,                 # Session title
                    "timestamp": str,             # ISO 8601 date
                    "duration_seconds": int,      # Duration in seconds
                    "type": str,                  # Session type
                    "platform": str | null        # Platform name or null
                }
            ]
        }

        Markdown format (response_format='markdown'):
        Bulleted list with title, date, duration, type, platform, and session ID.
        Includes pagination note if more sessions are available.

    Examples:
        - List recent meetings: params with limit=10
        - Filter by type: params with session_type='call', since='2025-01-01'
        - Paginate: params with cursor='2025-01-15T10:00:00Z' from a previous response

    Errors:
        - 401: Invalid or expired API token.
        - 429: Rate limit exceeded.
    """
    await _check_rate_limit(ctx)
    try:
        client = _get_client(ctx)
        query_params: dict[str, Any] = {}
        if params.limit is not None:
            query_params["limit"] = str(params.limit)
        if params.cursor:
            query_params["cursor"] = params.cursor
        if params.since:
            query_params["since"] = params.since
        if params.session_type:
            query_params["type"] = params.session_type

        logger.info("wave_list_sessions: limit=%s since=%s type=%s", params.limit, params.since, params.session_type)
        resp = await client.get("/sessions", params=query_params)
        resp.raise_for_status()
        data = resp.json()

        sessions = data.get("sessions", [])
        has_more = data.get("has_more", False)
        next_cursor = data.get("next_cursor")
        total_count = data.get("total_count")

        if params.response_format == ResponseFormat.JSON:
            return _json_response({
                "total_count": total_count,
                "count": len(sessions),
                "has_more": has_more,
                "next_cursor": next_cursor,
                "sessions": [
                    {
                        "id": s.get("id"),
                        "title": s.get("title"),
                        "timestamp": s.get("timestamp"),
                        "duration_seconds": s.get("duration_seconds"),
                        "type": s.get("type"),
                        "platform": s.get("platform"),
                    }
                    for s in sessions
                ],
            })

        lines = [f"## Wave Sessions ({len(sessions)} returned)\n"]
        lines.append(_format_session_list_md(sessions))

        if has_more and next_cursor:
            lines.append(f"\n---\n*More sessions available.* Use `cursor: \"{next_cursor}\"` to load the next page.")

        return "\n".join(lines)
    except Exception as e:
        _handle_api_error(e)


@mcp.tool(
    name="wave_get_session",
    annotations={
        "title": "Get Wave Session Details",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def wave_get_session(params: GetSessionInput, ctx: Context) -> str:
    """Get full details for a single Wave session including summary, notes, and tags.

    Use this to review a specific meeting's metadata and AI-generated summary
    before deciding whether to pull the full transcript.

    Args:
        params (GetSessionInput): Validated input parameters containing:
            - session_id (str): The Wave session ID to retrieve (e.g. 'ses_abc123').
            - response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: Response in the requested format.

        JSON schema (response_format='json'):
        {
            "id": str,                    # Session ID
            "title": str,                 # Session title
            "timestamp": str,             # ISO 8601 date
            "duration_seconds": int,      # Duration in seconds
            "type": str,                  # Session type
            "platform": str | null,       # Platform name
            "language": str | null,       # Detected language
            "favorite": bool,             # Favorite status
            "tags": [str],                # List of tags
            "summary": str | null,        # AI-generated summary
            "notes": str | null           # User notes
        }

        Markdown format (response_format='markdown'):
        Formatted session details with headings for summary and notes.

    Examples:
        - Get session details: params with session_id='ses_abc123'
        - Get as JSON for processing: params with session_id='ses_abc123', response_format='json'

    Errors:
        - 404: Session not found.
        - 401: Invalid or expired API token.
    """
    await _check_rate_limit(ctx)
    try:
        client = _get_client(ctx)
        logger.info("wave_get_session: id=%s", params.session_id)
        resp = await client.get(f"/sessions/{params.session_id}")
        resp.raise_for_status()
        data = resp.json()

        if params.response_format == ResponseFormat.JSON:
            return _json_response({
                "id": data.get("id"),
                "title": data.get("title"),
                "timestamp": data.get("timestamp"),
                "duration_seconds": data.get("duration_seconds"),
                "type": data.get("type"),
                "platform": data.get("platform"),
                "language": data.get("language"),
                "favorite": data.get("favorite", False),
                "tags": data.get("tags", []),
                "summary": data.get("summary"),
                "notes": data.get("notes"),
            })

        return _format_session_detail_md(data)
    except Exception as e:
        _handle_api_error(e)


@mcp.tool(
    name="wave_get_transcript",
    annotations={
        "title": "Get Session Transcript",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def wave_get_transcript(params: GetTranscriptInput, ctx: Context) -> str:
    """Get the full transcript of a Wave session with speaker-attributed segments.

    Returns the complete conversation text with speaker names and timestamps.
    This is the primary tool for extracting action items, decisions, follow-ups,
    and meeting insights. Each segment includes who spoke, when, and what they said.

    Args:
        params (GetTranscriptInput): Validated input parameters containing:
            - session_id (str): The Wave session ID whose transcript to retrieve.
            - response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: Response in the requested format.

        JSON schema (response_format='json'):
        {
            "session_id": str,            # The session this transcript belongs to
            "segment_count": int,         # Number of transcript segments
            "segments": [
                {
                    "speaker": str,       # Speaker name (e.g. "Alice")
                    "start": float,       # Start time in seconds
                    "end": float | null,  # End time in seconds (if available)
                    "text": str           # What was said
                }
            ]
        }

        Markdown format (response_format='markdown'):
        Speaker-attributed transcript with timestamps in MM:SS format.

    Examples:
        - Get transcript: params with session_id='ses_abc123'
        - Get as JSON for NLP processing: params with session_id='ses_abc123', response_format='json'

    Errors:
        - 404: Session not found or transcript not yet available.
        - 401: Invalid or expired API token.
    """
    await _check_rate_limit(ctx)
    try:
        client = _get_client(ctx)
        logger.info("wave_get_transcript: id=%s", params.session_id)
        resp = await client.get(f"/sessions/{params.session_id}/transcript")
        resp.raise_for_status()
        data = resp.json()

        segments = data.get("segments")

        if params.response_format == ResponseFormat.JSON:
            return _json_response({
                "session_id": params.session_id,
                "segment_count": len(segments) if segments else 0,
                "segments": [
                    {
                        "speaker": seg.get("speaker", "Unknown"),
                        "start": seg.get("start", 0),
                        "end": seg.get("end"),
                        "text": seg.get("text", ""),
                    }
                    for seg in (segments or [])
                ],
            })

        if segments:
            lines = ["## Transcript\n"]
            for seg in segments:
                speaker = _sanitize_md(seg.get("speaker", "Unknown"))
                start = seg.get("start", 0)
                text = _sanitize_md(seg.get("text", ""))
                mins, secs = divmod(int(start), 60)
                lines.append(f"**{speaker}** [{mins:02d}:{secs:02d}]  \n{text}\n")
            return "\n".join(lines)

        transcript = data.get("transcript", "")
        if transcript:
            return f"## Transcript\n\n{_sanitize_md(transcript)}"

        return "No transcript available for this session."
    except Exception as e:
        _handle_api_error(e)


@mcp.tool(
    name="wave_search_sessions",
    annotations={
        "title": "Search Wave Sessions",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def wave_search_sessions(params: SearchSessionsInput, ctx: Context) -> str:
    """Semantic search across all your Wave sessions using natural language.

    Finds sessions by meaning, not just keywords. Great for queries like
    'budget discussion with marketing', 'what did we decide about the launch date',
    or 'conversations mentioning the new API'. Returns relevance-ranked results
    with matching snippets.

    Args:
        params (SearchSessionsInput): Validated input parameters containing:
            - query (str): Natural language search query (1–500 chars).
            - limit (Optional[int]): Maximum results to return, 1–50 (default 10).
            - offset (Optional[int]): Number of results to skip for pagination (default 0).
            - response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: Response in the requested format.

        JSON schema (response_format='json'):
        {
            "query": str,                 # The search query used
            "total": int,                 # Total number of matches
            "count": int,                 # Results in this response
            "offset": int,                # Current offset
            "has_more": bool,             # Whether more results exist beyond offset + count
            "next_offset": int | null,    # Offset for the next page (null if no more)
            "results": [
                {
                    "id": str,            # Session ID
                    "title": str,         # Session title
                    "timestamp": str,     # ISO 8601 date
                    "type": str,          # Session type
                    "similarity": float,  # Relevance score (0–1)
                    "snippet": str        # Matching text excerpt
                }
            ]
        }

        Markdown format (response_format='markdown'):
        Ranked results with titles, dates, relevance percentages, and snippets.

    Examples:
        - Search by topic: params with query='pricing discussion with Acme'
        - Paginate results: params with query='budget', limit=10, offset=10
        - Use when: "Find all meetings where we discussed the product launch"
        - Don't use when: You already have the session ID (use wave_get_session instead)

    Errors:
        - 401: Invalid or expired API token.
        - 422: Malformed query.
    """
    await _check_rate_limit(ctx)
    try:
        client = _get_client(ctx)
        logger.info("wave_search_sessions: query='%s' limit=%s offset=%s", params.query[:50], params.limit, params.offset)
        resp = await client.post(
            "/sessions/search",
            json={"query": params.query, "limit": params.limit, "offset": params.offset},
        )
        resp.raise_for_status()
        data = resp.json()

        results = data.get("results", [])
        total = data.get("total", 0)
        offset = params.offset or 0
        has_more = total > offset + len(results)
        next_offset = offset + len(results) if has_more else None

        if params.response_format == ResponseFormat.JSON:
            return _json_response({
                "query": params.query,
                "total": total,
                "count": len(results),
                "offset": offset,
                "has_more": has_more,
                "next_offset": next_offset,
                "results": [
                    {
                        "id": r.get("id"),
                        "title": r.get("title"),
                        "timestamp": r.get("timestamp"),
                        "type": r.get("type"),
                        "similarity": r.get("similarity"),
                        "snippet": r.get("snippet"),
                    }
                    for r in results
                ],
            })

        if not results:
            return f"No sessions found matching: \"{_sanitize_md(params.query)}\""

        lines = [f"## Search Results for \"{_sanitize_md(params.query)}\" ({total} total matches)\n"]
        for r in results:
            title = _sanitize_md(r.get("title", "Untitled"))
            sid = r.get("id", "")
            ts = r.get("timestamp", "unknown date")
            stype = r.get("type", "")
            similarity = r.get("similarity", 0)
            snippet = _sanitize_md(r.get("snippet", ""))
            pct = f"{similarity * 100:.0f}%" if isinstance(similarity, (int, float)) else str(similarity)

            lines.append(f"### {title}")
            lines.append(f"**Date:** {ts} · **Type:** {stype} · **Relevance:** {pct}")
            if snippet:
                lines.append(f"> {snippet}")
            lines.append(f"`id: {sid}`\n")

        if has_more:
            lines.append(f"---\n*{total - offset - len(results)} more results available.* Use `offset: {next_offset}` to load the next page.")

        return "\n".join(lines)
    except Exception as e:
        _handle_api_error(e)


@mcp.tool(
    name="wave_get_stats",
    annotations={
        "title": "Get Session Statistics",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def wave_get_stats(params: GetStatsInput, ctx: Context) -> str:
    """Get aggregated statistics about your Wave sessions over a date range.

    Returns total session count, total and average duration, and breakdowns
    by session type and platform. Defaults to the last 30 days if no dates given.

    Args:
        params (GetStatsInput): Validated input parameters containing:
            - since (Optional[str]): Start date (ISO 8601). Defaults to 30 days ago.
            - until (Optional[str]): End date (ISO 8601). Defaults to now.
            - response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: Response in the requested format.

        JSON schema (response_format='json'):
        {
            "period": {
                "since": str,             # Period start date (ISO 8601)
                "until": str              # Period end date (ISO 8601)
            },
            "total_sessions": int,        # Total sessions in period
            "total_duration_seconds": int, # Total duration in seconds
            "average_duration_seconds": int, # Average duration in seconds
            "sessions_by_type": {         # Breakdown by session type
                "meeting": int,
                "call": int
            },
            "sessions_by_platform": {     # Breakdown by platform
                "zoom": int,
                "teams": int
            }
        }

        Markdown format (response_format='markdown'):
        Summary with headings for period, totals, type breakdown, and platform breakdown.

    Examples:
        - Last 30 days: params with no date filters
        - Specific range: params with since='2025-01-01', until='2025-03-01'

    Errors:
        - 401: Invalid or expired API token.
        - 422: Invalid date format.
    """
    await _check_rate_limit(ctx)
    try:
        client = _get_client(ctx)
        query_params: dict[str, str] = {}
        if params.since:
            query_params["since"] = params.since
        if params.until:
            query_params["until"] = params.until

        logger.info("wave_get_stats: since=%s until=%s", params.since, params.until)
        resp = await client.get("/sessions/stats", params=query_params)
        resp.raise_for_status()
        data = resp.json()

        if params.response_format == ResponseFormat.JSON:
            return _json_response(data)

        period = data.get("period", {})
        lines = [
            "## Wave Session Statistics\n",
            f"**Period:** {period.get('since', '?')} to {period.get('until', '?')}",
            f"**Total Sessions:** {data.get('total_sessions', 0)}",
            f"**Total Duration:** {_format_duration(data.get('total_duration_seconds'))}",
            f"**Average Duration:** {_format_duration(data.get('average_duration_seconds'))}",
        ]

        by_type = data.get("sessions_by_type", {})
        if by_type:
            lines.append("\n### By Type")
            for t, count in by_type.items():
                lines.append(f"- **{_sanitize_md(t)}:** {count}")

        by_platform = data.get("sessions_by_platform", {})
        if by_platform:
            lines.append("\n### By Platform")
            for p, count in by_platform.items():
                lines.append(f"- **{_sanitize_md(p)}:** {count}")

        return "\n".join(lines)
    except Exception as e:
        _handle_api_error(e)


@mcp.tool(
    name="wave_bulk_export",
    annotations={
        "title": "Bulk Export Sessions",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def wave_bulk_export(params: BulkExportInput, ctx: Context) -> str:
    """Export multiple Wave sessions at once (up to 50) with optional transcripts and summaries.

    Use this for batch processing — e.g., pulling all sessions from a week
    to generate a consolidated action item list or meeting digest. More efficient
    than calling wave_get_session individually for each one.

    Args:
        params (BulkExportInput): Validated input parameters containing:
            - session_ids (list[str]): List of session IDs to export (1–50).
            - include_transcript (bool): Include full transcript text (default true).
            - include_summary (bool): Include AI summary (default true).
            - response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: Response in the requested format.

        JSON schema (response_format='json'):
        {
            "count": int,                 # Number of successfully exported sessions
            "error_count": int,           # Number of sessions that failed
            "sessions": [
                {
                    "id": str,
                    "title": str,
                    "timestamp": str,
                    "duration_seconds": int,
                    "type": str,
                    "platform": str | null,
                    "summary": str | null,       # Included if include_summary=true
                    "transcript": str | null      # Included if include_transcript=true
                }
            ],
            "errors": [
                {
                    "id": str,            # Session ID that failed
                    "error": str          # Error description
                }
            ]
        }

        Markdown format (response_format='markdown'):
        Formatted details for each session with collapsible transcripts.

    Examples:
        - Export week's meetings: params with session_ids=['ses_a', 'ses_b', 'ses_c']
        - Summaries only (faster): params with session_ids=[...], include_transcript=false

    Errors:
        - 404: One or more session IDs not found (reported per-session in errors array).
        - 401: Invalid or expired API token.
    """
    await _check_rate_limit(ctx)
    try:
        client = _get_client(ctx)
        logger.info("wave_bulk_export: %d sessions requested", len(params.session_ids))
        await ctx.report_progress(0.1, "Requesting bulk export from Wave...")

        resp = await client.post(
            "/sessions/bulk",
            json={
                "session_ids": params.session_ids,
                "include_transcript": params.include_transcript,
                "include_summary": params.include_summary,
            },
        )
        resp.raise_for_status()
        data = resp.json()

        sessions = data.get("sessions", [])
        errors = data.get("errors", [])

        await ctx.report_progress(0.8, "Formatting results...")

        if params.response_format == ResponseFormat.JSON:
            return _json_response({
                "count": len(sessions),
                "error_count": len(errors),
                "sessions": sessions,
                "errors": errors,
            })

        lines = [f"## Bulk Export ({len(sessions)} sessions)\n"]

        for s in sessions:
            lines.append(f"### {_sanitize_md(s.get('title', 'Untitled'))}")
            lines.append(f"**ID:** `{s.get('id')}` · **Date:** {s.get('timestamp', '?')} · **Duration:** {_format_duration(s.get('duration_seconds'))}")
            if s.get("summary"):
                lines.append(f"\n**Summary:** {_sanitize_md(s['summary'])}")
            if s.get("transcript"):
                transcript = _sanitize_md(s["transcript"])
                if len(transcript) > 2000:
                    transcript = transcript[:2000] + "\n\n*\\[Transcript truncated — use wave\\_get\\_transcript for the full text\\]*"
                lines.append(f"\n<details><summary>Transcript</summary>\n\n{transcript}\n</details>")
            lines.append("")

        if errors:
            lines.append("### Errors")
            for err in errors:
                lines.append(f"- `{err.get('id')}`: {_sanitize_md(err.get('error', 'Unknown error'))}")

        return "\n".join(lines)
    except Exception as e:
        _handle_api_error(e)


@mcp.tool(
    name="wave_get_media",
    annotations={
        "title": "Get Session Media URLs",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def wave_get_media(params: GetMediaInput, ctx: Context) -> str:
    """Get signed audio and video URLs for a Wave session.

    Returns temporary download/playback URLs that expire after 1 hour.
    Use when you need to reference the actual recording media.

    Args:
        params (GetMediaInput): Validated input parameters containing:
            - session_id (str): The Wave session ID to get media URLs for.
            - response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: Response in the requested format.

        JSON schema (response_format='json'):
        {
            "id": str,                    # Session ID
            "audio_url": str | null,      # Signed audio URL (expires in ~1 hour)
            "video_url": str | null,      # Signed video URL (expires in ~1 hour)
            "expires_at": str,            # ISO 8601 expiration timestamp
            "warning": str                # Security reminder about URL sensitivity
        }

        Markdown format (response_format='markdown'):
        Audio and video URLs with expiration info and sensitivity warning.

    Examples:
        - Get media links: params with session_id='ses_abc123'

    Errors:
        - 404: Session not found.
        - 401: Invalid or expired API token.

    Security:
        URLs contain embedded authentication — treat them as sensitive credentials.
        Do not share them publicly or include them in logs.
    """
    await _check_rate_limit(ctx)
    try:
        client = _get_client(ctx)
        logger.info("wave_get_media: id=%s", params.session_id)
        resp = await client.get(f"/sessions/{params.session_id}/media")
        resp.raise_for_status()
        data = resp.json()

        audio_url = data.get("audio_url")
        video_url = data.get("video_url")
        expires = data.get("expires_at", "unknown")

        if params.response_format == ResponseFormat.JSON:
            return _json_response({
                "id": data.get("id", params.session_id),
                "audio_url": audio_url,
                "video_url": video_url,
                "expires_at": expires,
                "warning": "These URLs contain embedded authentication tokens. Do not share publicly.",
            })

        lines = [f"## Media for Session `{data.get('id', params.session_id)}`\n"]

        if audio_url:
            lines.append(f"**Audio:** {audio_url}")
        else:
            lines.append("**Audio:** Not available")

        if video_url:
            lines.append(f"**Video:** {video_url}")
        else:
            lines.append("**Video:** Not available")

        lines.append(f"\n*URLs expire at {expires} (approximately 1 hour from now).*")
        lines.append("\n**SENSITIVE:** These URLs contain embedded authentication tokens. Do not share them publicly or include them in logs.")

        return "\n".join(lines)
    except Exception as e:
        _handle_api_error(e)


@mcp.tool(
    name="wave_get_account",
    annotations={
        "title": "Get Wave Account Info",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def wave_get_account(params: GetAccountInput, ctx: Context) -> str:
    """Get your Wave account profile, subscription status, and session count.

    Use this to verify your connection to Wave is working and check your account status.

    Args:
        params (GetAccountInput): Validated input parameters containing:
            - response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: Response in the requested format.

        JSON schema (response_format='json'):
        {
            "user_id": str,               # Your Wave user ID
            "subscription_active": bool,  # Whether your subscription is active
            "session_count": int          # Total number of sessions in your account
        }

        Markdown format (response_format='markdown'):
        Account summary with user ID, subscription status, and session count.

    Examples:
        - Verify connection: just call this tool with default params
        - Get as JSON: params with response_format='json'
        - Use when: "Am I connected to Wave?" or "How many recordings do I have?"
        - Don't use when: You need session details (use wave_list_sessions instead)

    Errors:
        - 401: Invalid or expired API token.
    """
    await _check_rate_limit(ctx)
    try:
        client = _get_client(ctx)
        logger.info("wave_get_account called")
        resp = await client.get("/account")
        resp.raise_for_status()
        data = resp.json()

        if params.response_format == ResponseFormat.JSON:
            return _json_response({
                "user_id": data.get("user_id", "unknown"),
                "subscription_active": data.get("subscription_active", False),
                "session_count": data.get("session_count", 0),
            })

        lines = [
            "## Wave Account\n",
            f"**User ID:** `{data.get('user_id', 'unknown')}`",
            f"**Subscription Active:** {'Yes' if data.get('subscription_active') else 'No'}",
            f"**Total Sessions:** {data.get('session_count', 0)}",
        ]
        return "\n".join(lines)
    except Exception as e:
        _handle_api_error(e)


@mcp.tool(
    name="wave_update_session",
    annotations={
        "title": "Update Session Metadata",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def wave_update_session(params: UpdateSessionInput, ctx: Context) -> str:
    """Update a Wave session's title, notes, tags, or favorite status.

    Use this to annotate sessions with action items, follow-up notes, or tags
    for organization after reviewing a transcript.

    Args:
        params (UpdateSessionInput): Validated input parameters containing:
            - session_id (str): The Wave session ID to update.
            - title (Optional[str]): New session title (max 500 chars).
            - notes (Optional[str]): New session notes (max 50,000 chars).
            - tags (Optional[list[str]]): Replace all tags (max 20 tags, each max 100 chars).
            - favorite (Optional[bool]): Set or unset favorite status.
            - response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: Response in the requested format.

        JSON schema (response_format='json'):
        {
            "id": str,                    # Session ID that was updated
            "updated_fields": [str],      # List of fields that were changed
            "updated_at": str             # ISO 8601 timestamp of the update
        }

        Markdown format (response_format='markdown'):
        Confirmation message with list of updated fields and timestamp.

    Examples:
        - Add notes: params with session_id='ses_abc123', notes='Follow up with client by Friday'
        - Tag a session: params with session_id='ses_abc123', tags=['q1-review', 'sales']
        - Use when: Annotating a meeting after reviewing its transcript
        - Don't use when: You want to read session details (use wave_get_session instead)

    Errors:
        - 404: Session not found.
        - 422: Validation error on input fields.
        - 401: Invalid or expired API token.
    """
    await _check_rate_limit(ctx)
    try:
        client = _get_client(ctx)

        body: dict[str, Any] = {}
        if params.title is not None:
            body["title"] = params.title
        if params.notes is not None:
            body["notes"] = params.notes
        if params.tags is not None:
            body["tags"] = params.tags
        if params.favorite is not None:
            body["favorite"] = params.favorite

        if not body:
            raise ToolError("No fields to update. Provide at least one of: title, notes, tags, favorite.")

        logger.info("wave_update_session: id=%s fields=%s", params.session_id, list(body.keys()))
        resp = await client.patch(f"/sessions/{params.session_id}", json=body)
        resp.raise_for_status()
        data = resp.json()

        updated = data.get("updated_fields", list(body.keys()))
        updated_at = data.get("updated_at", "now")

        if params.response_format == ResponseFormat.JSON:
            return _json_response({
                "id": data.get("id", params.session_id),
                "updated_fields": updated,
                "updated_at": updated_at,
            })

        return (
            f"Session `{data.get('id', params.session_id)}` updated successfully.\n"
            f"**Updated fields:** {', '.join(updated)}\n"
            f"**Updated at:** {updated_at}"
        )
    except Exception as e:
        _handle_api_error(e)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
