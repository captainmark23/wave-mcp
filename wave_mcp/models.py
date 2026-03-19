"""Pydantic input models for Wave MCP Server tools."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from wave_mcp.constants import (
    _BLOCKED_DIRS,
    DEFAULT_PAGINATION_LIMIT,
    MAX_BULK_SESSIONS,
    MAX_CURSOR_LENGTH,
    MAX_NOTES_LENGTH,
    MAX_PAGINATION_LIMIT,
    MAX_SEARCH_RESULTS,
    MAX_TAG_LENGTH,
    MAX_TAGS,
    MAX_TITLE_LENGTH,
    ResponseFormat,
)
from wave_mcp.validators import (
    _validate_iso_date,
    _validate_session_id,
    _validate_session_type,
)


class ListSessionsInput(BaseModel):
    """Parameters for listing Wave sessions."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    limit: int | None = Field(
        default=DEFAULT_PAGINATION_LIMIT,
        description="Number of sessions to return (1-100, default 20)",
        ge=1,
        le=MAX_PAGINATION_LIMIT,
    )
    cursor: str | None = Field(
        default=None,
        description="Pagination cursor from a previous response's next_cursor",
        max_length=MAX_CURSOR_LENGTH,
    )
    since: str | None = Field(
        default=None,
        description="Only return sessions newer than this ISO 8601 date (e.g. '2025-01-01')",
    )
    session_type: str | None = Field(
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
            # Cursors should be printable ASCII -- reject control chars
            if not v.isprintable():
                raise ValueError("cursor must contain only printable characters")
        return v

    @field_validator("session_type")
    @classmethod
    def validate_session_type(cls, v: str | None) -> str | None:
        return _validate_session_type(v)


class GetSessionInput(BaseModel):
    """Parameters for retrieving a single session."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    session_id: str = Field(..., description="The Wave session ID to retrieve (e.g. 'ses_abc123')", min_length=1)
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

    session_id: str = Field(..., description="The Wave session ID whose transcript to retrieve", min_length=1)
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
            "Natural language search query -- describe what you're looking for "
            "(e.g. 'pricing discussion with Acme Corp', 'action items from last standup')"
        ),
        min_length=1,
        max_length=500,
    )
    limit: int | None = Field(
        default=10,
        description="Max results to return (1-50, default 10)",
        ge=1,
        le=MAX_SEARCH_RESULTS,
    )
    offset: int | None = Field(
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

    since: str | None = Field(
        default=None,
        description="Start date for stats period (ISO 8601). Defaults to 30 days ago.",
    )
    until: str | None = Field(
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
        description="List of session IDs to export (1-50)",
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

    session_id: str = Field(..., description="The Wave session ID to get audio/video URLs for", min_length=1)
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

    session_id: str = Field(..., description="The Wave session ID to update", min_length=1)
    title: str | None = Field(
        default=None,
        description="New session title (max 500 chars)",
        max_length=MAX_TITLE_LENGTH,
    )
    notes: str | None = Field(
        default=None,
        description="New session notes (max 50,000 chars)",
        max_length=MAX_NOTES_LENGTH,
    )
    tags: list[str] | None = Field(
        default=None,
        description="Replace tags (max 20 tags, each max 100 chars)",
        max_length=MAX_TAGS,
    )
    favorite: bool | None = Field(
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


class ListAllSessionsInput(BaseModel):
    """Parameters for listing ALL sessions via auto-pagination."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    since: str | None = Field(
        default=None,
        description="Only return sessions newer than this ISO 8601 date (e.g. '2025-01-01')",
    )
    session_type: str | None = Field(
        default=None,
        description="Filter by session type: 'recording', 'recovery', 'podcast', 'meeting', 'call', etc.",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' (default) or 'json'",
    )

    @field_validator("since")
    @classmethod
    def validate_since(cls, v: str | None) -> str | None:
        return _validate_iso_date(v, "since")

    @field_validator("session_type")
    @classmethod
    def validate_session_type(cls, v: str | None) -> str | None:
        return _validate_session_type(v)


class DiscoverAndExportInput(BaseModel):
    """Parameters for discovering sessions via search and exporting them."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(
        ...,
        description="Search query to discover sessions (e.g. 'meetings about budget Q4')",
        min_length=1,
        max_length=500,
    )
    max_results: int | None = Field(
        default=10,
        description="Max sessions to discover and export (1-50, default 10)",
        ge=1,
        le=MAX_BULK_SESSIONS,
    )
    include_transcript: bool = Field(default=True, description="Include transcripts (default true)")
    include_summary: bool = Field(default=True, description="Include summaries (default true)")
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' (default) or 'json'",
    )


class DownloadAudioInput(BaseModel):
    """Parameters for downloading audio to a local file.

    The output_path must be an absolute path. Parent directories are created
    automatically. Existing files at the path will be overwritten.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    session_id: str = Field(..., description="The Wave session ID to download audio for", min_length=1)
    output_path: str = Field(
        ...,
        description="Absolute local file path to save the audio (e.g. '/Users/me/Wave/audio.m4a')",
        min_length=1,
    )

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, v: str) -> str:
        return _validate_session_id(v)

    @field_validator("output_path")
    @classmethod
    def validate_output_path(cls, v: str) -> str:
        v = v.strip()
        if "\x00" in v:
            raise ValueError("output_path must not contain null bytes")
        p = Path(v)
        if not p.is_absolute():
            raise ValueError("output_path must be an absolute path (e.g. '/Users/me/Wave/audio.m4a')")
        # Resolve symlinks before checking against blocked directories
        resolved = str(p.resolve())
        for part in _BLOCKED_DIRS:
            if resolved.startswith(part + "/") or resolved == part:
                raise ValueError(f"output_path must not be inside {part}/")
        return v


class ExportArchiveInput(BaseModel):
    """Parameters for exporting a full local archive of all Wave sessions."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    output_dir: str = Field(
        ...,
        description="Directory to save the archive (e.g. '/Users/me/Documents/Wave'). Created if it doesn't exist.",
        min_length=1,
    )
    since: str | None = Field(
        default=None,
        description="Only archive sessions newer than this ISO 8601 date",
    )
    include_audio: bool = Field(
        default=False,
        description="Download audio files for each session (default false -- can be slow for large archives)",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' (default) or 'json'",
    )

    @field_validator("output_dir")
    @classmethod
    def validate_output_dir(cls, v: str) -> str:
        v = v.strip()
        if "\x00" in v:
            raise ValueError("output_dir must not contain null bytes")
        p = Path(v)
        if not p.is_absolute():
            raise ValueError("output_dir must be an absolute path (e.g. '/Users/me/Documents/Wave')")
        # Resolve symlinks before checking against blocked directories
        resolved = str(p.resolve())
        for part in _BLOCKED_DIRS:
            if resolved.startswith(part + "/") or resolved == part:
                raise ValueError(f"output_dir must not be inside {part}/")
        return v

    @field_validator("since")
    @classmethod
    def validate_since(cls, v: str | None) -> str | None:
        return _validate_iso_date(v, "since")
