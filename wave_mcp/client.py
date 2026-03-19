"""HTTP client helpers for Wave MCP Server."""

import logging
from typing import NoReturn

import httpx
from mcp.server.fastmcp import Context

try:
    from mcp.server.fastmcp import ToolError
except ImportError:

    class ToolError(Exception):  # type: ignore[no-redef]
        """MCP tool error -- sets isError=true in the response."""

        pass

from wave_mcp.rate_limiter import _RateLimiter

logger = logging.getLogger("wave_mcp")


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
                raise ToolError(f"Validation failed -- {msg}")
            except ToolError:
                raise
            except Exception:
                raise ToolError("Request validation failed. Check your input parameters.")
        raise ToolError(f"Wave API returned status {status}.")
    if isinstance(e, httpx.TimeoutException):
        logger.warning("Wave API timeout")
        raise ToolError("Request timed out. Wave may be temporarily unavailable -- try again shortly.")
    if isinstance(e, httpx.ConnectError):
        logger.warning("Wave API connection error")
        raise ToolError("Could not connect to Wave API. Check your network connection.")
    # Generic fallback -- never expose raw exception details
    logger.error("Unexpected error communicating with Wave API: %s", type(e).__name__, exc_info=True)
    raise ToolError("An unexpected error occurred while communicating with the Wave API. Please try again.")
