#!/usr/bin/env python3
"""Wave MCP Server -- entry point.

This module re-exports key symbols from the wave_mcp package for
backward compatibility with existing imports (e.g., tests).
"""

from wave_mcp.constants import (  # noqa: F401
    _BLOCKED_DIRS,
    _UUID_PATTERN,
    API_BASE_URL,
    DEFAULT_PAGINATION_LIMIT,
    DEFAULT_TIMEOUT,
    MAX_BULK_SESSIONS,
    MAX_CURSOR_LENGTH,
    MAX_NOTES_LENGTH,
    MAX_PAGINATION_LIMIT,
    MAX_SEARCH_RESULTS,
    MAX_TAG_LENGTH,
    MAX_TAGS,
    MAX_TITLE_LENGTH,
    ResponseFormat,
    SESSION_ID_PATTERN,
    VALID_SESSION_TYPES,
)
from wave_mcp.client import (  # noqa: F401
    _check_rate_limit,
    _get_client,
    _get_rate_limiter,
    _handle_api_error,
)
from wave_mcp.formatters import (  # noqa: F401
    _format_duration,
    _format_session_detail_md,
    _format_session_list_md,
    _json_response,
    _sanitize_md,
)
from wave_mcp.models import (  # noqa: F401
    BulkExportInput,
    DiscoverAndExportInput,
    DownloadAudioInput,
    ExportArchiveInput,
    GetAccountInput,
    GetMediaInput,
    GetSessionInput,
    GetStatsInput,
    GetTranscriptInput,
    ListAllSessionsInput,
    ListSessionsInput,
    SearchSessionsInput,
    UpdateSessionInput,
)
from wave_mcp.rate_limiter import _RateLimiter  # noqa: F401
from wave_mcp.server import main, mcp  # noqa: F401
from wave_mcp.validators import (  # noqa: F401
    _validate_iso_date,
    _validate_session_id,
    _validate_session_type,
)

# Import tools to register them with the mcp instance
import wave_mcp.tools  # noqa: F401, E402

if __name__ == "__main__":
    main()
