"""Wave MCP Server -- FastMCP app creation, lifespan, and entry point."""

import logging
import logging.handlers
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

from wave_mcp.constants import API_BASE_URL, DEFAULT_TIMEOUT
from wave_mcp.rate_limiter import _RateLimiter

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("wave_mcp")
logger.setLevel(logging.INFO)
_log_fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s")

# CRITICAL: Use stderr for console logging -- stdout is reserved for MCP stdio transport
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
# Lifespan -- persistent HTTP client with auth
# ---------------------------------------------------------------------------


@dataclass
class AppContext:
    """Shared state available to all tools via lifespan."""

    client: httpx.AsyncClient
    rate_limiter: _RateLimiter = field(default_factory=_RateLimiter)


def _get_api_token() -> str:
    """Get Wave API token from env var or macOS Keychain."""
    token = os.environ.get("WAVE_API_TOKEN", "").strip()
    if token:
        return token
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-a", "wave-mcp", "-s", "wave-api-token", "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception as exc:
        logger.debug("Keychain lookup failed: %s", exc)
    raise RuntimeError(
        "Wave API token not found. Set WAVE_API_TOKEN env var or add to macOS Keychain:\n"
        "  security add-generic-password -a wave-mcp -s wave-api-token -w YOUR_TOKEN"
    )


@asynccontextmanager
async def app_lifespan(server: FastMCP):
    """Create a persistent httpx client with Wave auth for the server lifetime."""
    token = _get_api_token()

    async with httpx.AsyncClient(
        base_url=API_BASE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        timeout=DEFAULT_TIMEOUT,
    ) as client:
        logger.info("Wave MCP server started -- connected to %s", API_BASE_URL)
        yield AppContext(client=client)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "wave_mcp",
    instructions=(
        "Wave MCP server -- access call recordings, transcripts, and meeting data. "
        "Use wave_list_sessions to browse recent meetings, wave_get_transcript to "
        "pull full speaker-attributed transcripts for analysis, and wave_search_sessions "
        "to find discussions by topic. Combine with wave_bulk_export for batch processing. "
        "All tools support response_format='json' for structured output or 'markdown' (default) "
        "for human-readable output."
    ),
    lifespan=app_lifespan,
)


def main() -> None:
    """Run the Wave MCP server."""
    # Import tools to register them with the mcp instance
    import wave_mcp.tools  # noqa: F401

    mcp.run(transport="stdio")
