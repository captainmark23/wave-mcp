"""Module-level constants for Wave MCP Server."""

import re
from enum import StrEnum

# ---------------------------------------------------------------------------
# API and limits
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
MAX_AUDIO_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB

# Directories that must never be written to -- used by path validators
_BLOCKED_DIRS = {
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/var",
    "/System",
    "/Library",
    "/private",
    "/private/etc",
    "/private/var",
    "/private/tmp",
}

# Session IDs must be alphanumeric with hyphens/underscores only -- prevents path traversal
SESSION_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")

# Known session types -- allowlist for defense-in-depth
# Includes types observed in real Wave data (recording, recovery, podcast)
# as well as types documented in the Wave API (meeting, call, webinar, etc.)
VALID_SESSION_TYPES = {
    "recording",
    "recovery",
    "podcast",  # Common in practice
    "meeting",
    "call",
    "webinar",
    "interview",
    "presentation",  # API-documented
}

# UUID pattern for detecting valid vs corrupted session IDs in search results
_UUID_PATTERN = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


# ---------------------------------------------------------------------------
# Response format enum
# ---------------------------------------------------------------------------


class ResponseFormat(StrEnum):
    """Output format for tool responses."""

    MARKDOWN = "markdown"
    JSON = "json"
