"""Wave MCP tool definitions."""

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import Context

try:
    from mcp.server.fastmcp import ToolError  # type: ignore[attr-defined]
except ImportError:

    class ToolError(Exception):  # type: ignore[no-redef]
        """MCP tool error -- sets isError=true in the response."""

        pass


from wave_mcp.client import (
    _check_rate_limit,
    _get_client,
    _get_rate_limiter,
    _handle_api_error,
)
from wave_mcp.constants import (
    _UUID_PATTERN,
    MAX_BULK_SESSIONS,
    ResponseFormat,
)
from wave_mcp.formatters import (
    _format_duration,
    _format_session_detail_md,
    _format_session_list_md,
    _json_response,
    _sanitize_md,
)
from wave_mcp.models import (
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
from wave_mcp.server import mcp

logger = logging.getLogger("wave_mcp")


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

    IMPORTANT: This endpoint only returns completed sessions that have summaries.
    It may return significantly fewer sessions than the total in your account
    (e.g., 96 out of 216). Sessions that are still processing, lack summaries,
    or were created via recovery may be excluded. To discover ALL sessions
    including those not returned here, use wave_search_sessions with broad
    queries, or use wave_list_all_sessions for a comprehensive crawl.

    Args:
        params (ListSessionsInput): Validated input parameters containing:
            - limit (Optional[int]): Number of sessions to return, 1-100 (default 20).
            - cursor (Optional[str]): Pagination cursor from a previous response.
            - since (Optional[str]): ISO 8601 date -- only return sessions after this date.
            - session_type (Optional[str]): Filter by type: 'meeting', 'call', 'webinar', 'interview', 'presentation'.
            - response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: Response in the requested format.

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
            return _json_response(
                {
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
                }
            )

        lines = [f"## Wave Sessions ({len(sessions)} returned)\n"]
        lines.append(_format_session_list_md(sessions))

        if has_more and next_cursor:
            lines.append(f'\n---\n*More sessions available.* Use `cursor: "{next_cursor}"` to load the next page.')

        return "\n".join(lines)
    except Exception as e:
        if isinstance(e, ToolError):
            raise
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
            return _json_response(
                {
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
                }
            )

        return _format_session_detail_md(data)
    except Exception as e:
        if isinstance(e, ToolError):
            raise
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
            return _json_response(
                {
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
                }
            )

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
        if isinstance(e, ToolError):
            raise
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
            - query (str): Natural language search query (1-500 chars).
            - limit (Optional[int]): Maximum results to return, 1-50 (default 10).
            - offset (Optional[int]): Number of results to skip for pagination (default 0).
            - response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: Response in the requested format.

    Errors:
        - 401: Invalid or expired API token.
        - 422: Malformed query.
    """
    await _check_rate_limit(ctx)
    try:
        client = _get_client(ctx)
        logger.info(
            "wave_search_sessions: query='%s' limit=%s offset=%s", params.query[:50], params.limit, params.offset
        )
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

        # Flag results with corrupted (non-UUID) IDs
        for r in results:
            rid = r.get("id", "")
            if rid and not _UUID_PATTERN.match(rid):
                r["_corrupted_id"] = True
                logger.warning(
                    "Search returned non-UUID session ID (length=%d chars) -- session is inaccessible via API", len(rid)
                )

        if params.response_format == ResponseFormat.JSON:
            return _json_response(
                {
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
                            **(
                                {"warning": "Non-UUID session ID -- this session cannot be accessed via the API"}
                                if r.get("_corrupted_id")
                                else {}
                            ),
                        }
                        for r in results
                    ],
                }
            )

        if not results:
            return f'No sessions found matching: "{_sanitize_md(params.query)}"'

        lines = [f'## Search Results for "{_sanitize_md(params.query)}" ({total} total matches)\n']
        for r in results:
            title = _sanitize_md(r.get("title", "Untitled"))
            sid = r.get("id", "")
            ts = r.get("timestamp", "unknown date")
            stype = r.get("type", "")
            similarity = r.get("similarity", 0)
            snippet = _sanitize_md(r.get("snippet", ""))
            pct = f"{similarity * 100:.0f}%" if isinstance(similarity, (int, float)) else str(similarity)

            lines.append(f"### {title}")
            lines.append(f"**Date:** {ts} . **Type:** {stype} . **Relevance:** {pct}")
            if snippet:
                lines.append(f"> {snippet}")
            if r.get("_corrupted_id"):
                lines.append(f"`id: {sid}` -- **Non-UUID ID: this session cannot be accessed via the API**\n")
            else:
                lines.append(f"`id: {sid}`\n")

        if has_more:
            lines.append(
                f"---\n*{total - offset - len(results)} more results available.* Use `offset: {next_offset}` to load the next page."
            )

        return "\n".join(lines)
    except Exception as e:
        if isinstance(e, ToolError):
            raise
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
        if isinstance(e, ToolError):
            raise
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

    Use this for batch processing -- e.g., pulling all sessions from a week
    to generate a consolidated action item list or meeting digest. More efficient
    than calling wave_get_session individually for each one.

    Args:
        params (BulkExportInput): Validated input parameters containing:
            - session_ids (list[str]): List of session IDs to export (1-50).
            - include_transcript (bool): Include full transcript text (default true).
            - include_summary (bool): Include AI summary (default true).
            - response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: Response in the requested format.

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
            return _json_response(
                {
                    "count": len(sessions),
                    "error_count": len(errors),
                    "sessions": sessions,
                    "errors": errors,
                }
            )

        lines = [f"## Bulk Export ({len(sessions)} sessions)\n"]

        for s in sessions:
            lines.append(f"### {_sanitize_md(s.get('title', 'Untitled'))}")
            lines.append(
                f"**ID:** `{s.get('id')}` . **Date:** {s.get('timestamp', '?')} . **Duration:** {_format_duration(s.get('duration_seconds'))}"
            )
            if s.get("summary"):
                lines.append(f"\n**Summary:** {_sanitize_md(s['summary'])}")
            if s.get("transcript"):
                transcript = _sanitize_md(s["transcript"])
                if len(transcript) > 2000:
                    transcript = (
                        transcript[:2000]
                        + "\n\n*\\[Transcript truncated -- use wave\\_get\\_transcript for the full text\\]*"
                    )
                lines.append(f"\n<details><summary>Transcript</summary>\n\n{transcript}\n</details>")
            lines.append("")

        if errors:
            lines.append("### Errors")
            for err in errors:
                lines.append(f"- `{err.get('id')}`: {_sanitize_md(err.get('error', 'Unknown error'))}")

        return "\n".join(lines)
    except Exception as e:
        if isinstance(e, ToolError):
            raise
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

    Errors:
        - 404: Session not found.
        - 401: Invalid or expired API token.

    Security:
        URLs contain embedded authentication -- treat them as sensitive credentials.
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
            return _json_response(
                {
                    "id": data.get("id", params.session_id),
                    "audio_url": audio_url,
                    "video_url": video_url,
                    "expires_at": expires,
                    "warning": "These URLs contain embedded authentication tokens. Do not share publicly.",
                }
            )

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
        lines.append(
            "\n**SENSITIVE:** These URLs contain embedded authentication tokens. Do not share them publicly or include them in logs."
        )

        return "\n".join(lines)
    except Exception as e:
        if isinstance(e, ToolError):
            raise
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
            return _json_response(
                {
                    "user_id": data.get("user_id", "unknown"),
                    "subscription_active": data.get("subscription_active", False),
                    "session_count": data.get("session_count", 0),
                }
            )

        lines = [
            "## Wave Account\n",
            f"**User ID:** `{data.get('user_id', 'unknown')}`",
            f"**Subscription Active:** {'Yes' if data.get('subscription_active') else 'No'}",
            f"**Total Sessions:** {data.get('session_count', 0)}",
        ]
        return "\n".join(lines)
    except Exception as e:
        if isinstance(e, ToolError):
            raise
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
            return _json_response(
                {
                    "id": data.get("id", params.session_id),
                    "updated_fields": updated,
                    "updated_at": updated_at,
                }
            )

        return (
            f"Session `{data.get('id', params.session_id)}` updated successfully.\n"
            f"**Updated fields:** {', '.join(updated)}\n"
            f"**Updated at:** {updated_at}"
        )
    except Exception as e:
        if isinstance(e, ToolError):
            raise
        _handle_api_error(e)


@mcp.tool(
    name="wave_list_all_sessions",
    annotations={
        "title": "List All Wave Sessions",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def wave_list_all_sessions(params: ListAllSessionsInput, ctx: Context) -> str:
    """List ALL Wave sessions by auto-paginating through the entire session history.

    Unlike wave_list_sessions (which returns one page at a time), this tool
    automatically follows cursor-based pagination to collect every session the
    API returns. Note that the list endpoint only returns completed sessions
    with summaries -- use wave_search_sessions to discover additional sessions
    that may not appear here.

    Args:
        params (ListAllSessionsInput): Validated input parameters containing:
            - since (Optional[str]): Only sessions newer than this ISO 8601 date.
            - session_type (Optional[str]): Filter by type.
            - response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: All sessions in the requested format with total count.
    """
    await _check_rate_limit(ctx)
    try:
        client = _get_client(ctx)
        all_sessions: list[dict] = []

        query_params: dict[str, Any] = {"limit": "100"}
        if params.since:
            query_params["since"] = params.since
        if params.session_type:
            query_params["type"] = params.session_type

        max_pages = 100  # Safety limit to prevent infinite pagination loops
        page = 0
        while True:
            page += 1
            if page > max_pages:
                logger.warning("wave_list_all_sessions: hit max page limit (%d), stopping", max_pages)
                break
            logger.info("wave_list_all_sessions: fetching page %d (total so far: %d)", page, len(all_sessions))
            await ctx.report_progress(
                len(all_sessions) / max(len(all_sessions) + 100, 1), f"Fetched {len(all_sessions)} sessions..."
            )

            rl = _get_rate_limiter(ctx)
            if not await rl.check():
                # Wait briefly and retry rather than failing
                await asyncio.sleep(2)
                if not await rl.check():
                    raise ToolError("Rate limit reached while paginating. Try again in a moment.")

            resp = await client.get("/sessions", params=query_params)
            resp.raise_for_status()
            data = resp.json()

            sessions = data.get("sessions", [])
            all_sessions.extend(sessions)

            has_more = data.get("has_more", False)
            next_cursor = data.get("next_cursor")
            if not has_more or not next_cursor or not sessions:
                break
            query_params["cursor"] = next_cursor

        # Sort oldest first
        all_sessions.sort(key=lambda s: s.get("timestamp", ""))

        if params.response_format == ResponseFormat.JSON:
            return _json_response(
                {
                    "total_count": len(all_sessions),
                    "sessions": [
                        {
                            "id": s.get("id"),
                            "title": s.get("title"),
                            "timestamp": s.get("timestamp"),
                            "duration_seconds": s.get("duration_seconds"),
                            "type": s.get("type"),
                            "platform": s.get("platform"),
                        }
                        for s in all_sessions
                    ],
                }
            )

        lines = [f"## All Wave Sessions ({len(all_sessions)} total)\n"]
        lines.append(_format_session_list_md(all_sessions))
        return "\n".join(lines)
    except Exception as e:
        if isinstance(e, ToolError):
            raise
        _handle_api_error(e)


@mcp.tool(
    name="wave_discover_and_export",
    annotations={
        "title": "Discover and Export Sessions",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def wave_discover_and_export(params: DiscoverAndExportInput, ctx: Context) -> str:
    """Search for sessions by topic and export them in one step.

    Combines wave_search_sessions and wave_bulk_export into a single operation.
    Discovers sessions matching your query, then fetches full details including
    transcripts and summaries. Skips sessions with corrupted (non-UUID) IDs.

    Args:
        params (DiscoverAndExportInput): Validated input parameters containing:
            - query (str): Search query to find sessions.
            - max_results (int): Max sessions to export (1-50, default 10).
            - include_transcript (bool): Include transcripts (default true).
            - include_summary (bool): Include summaries (default true).
            - response_format (ResponseFormat): 'json' (default) or 'markdown'.

    Returns:
        str: Exported sessions with full content.
    """
    await _check_rate_limit(ctx)
    try:
        client = _get_client(ctx)

        # Step 1: Search for matching sessions
        await ctx.report_progress(0.1, "Searching for sessions...")
        resp = await client.post(
            "/sessions/search",
            json={"query": params.query, "limit": params.max_results},
        )
        resp.raise_for_status()
        search_data = resp.json()
        results = search_data.get("results", [])

        if not results:
            return "No sessions found matching your query."

        # Filter out corrupted IDs
        valid_ids = []
        skipped = []
        for r in results:
            rid = r.get("id", "")
            if _UUID_PATTERN.match(rid):
                valid_ids.append(rid)
            else:
                skipped.append({"id": rid, "title": r.get("title", "?"), "reason": "non-UUID session ID"})

        if not valid_ids:
            return "Found sessions but all have corrupted IDs that cannot be accessed via the API."

        # Step 2: Bulk export the valid sessions
        await _check_rate_limit(ctx)
        await ctx.report_progress(0.4, f"Exporting {len(valid_ids)} sessions...")
        resp = await client.post(
            "/sessions/bulk",
            json={
                "session_ids": valid_ids,
                "include_transcript": params.include_transcript,
                "include_summary": params.include_summary,
            },
        )
        resp.raise_for_status()
        export_data = resp.json()

        sessions = export_data.get("sessions", [])
        errors = export_data.get("errors", [])

        if params.response_format == ResponseFormat.JSON:
            return _json_response(
                {
                    "query": params.query,
                    "discovered": len(results),
                    "exported": len(sessions),
                    "skipped_corrupted_ids": skipped,
                    "sessions": sessions,
                    "errors": errors,
                }
            )

        lines = [f'## Discover & Export: "{_sanitize_md(params.query)}"']
        lines.append(f"Found {len(results)} matches, exported {len(sessions)}\n")
        for s in sessions:
            lines.append(f"### {_sanitize_md(s.get('title', 'Untitled'))}")
            lines.append(f"`{s.get('id')}` . {s.get('timestamp', '?')} . {_format_duration(s.get('duration_seconds'))}")
            if s.get("summary"):
                summary_preview = _sanitize_md(s["summary"])[:500]
                lines.append(f"\n{summary_preview}{'...' if len(s['summary']) > 500 else ''}\n")
        if skipped:
            lines.append("\n**Skipped (corrupted IDs):**")
            for sk in skipped:
                lines.append(f"- {_sanitize_md(sk['title'])}: {sk['reason']}")
        return "\n".join(lines)
    except Exception as e:
        if isinstance(e, ToolError):
            raise
        _handle_api_error(e)


@mcp.tool(
    name="wave_download_audio",
    annotations={
        "title": "Download Session Audio",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def wave_download_audio(params: DownloadAudioInput, ctx: Context) -> str:
    """Download the audio recording for a Wave session to a local file.

    Fetches a signed audio URL (valid for 1 hour) and downloads the audio
    file to the specified local path. The file is typically .m4a format.

    Args:
        params (DownloadAudioInput): Validated input parameters containing:
            - session_id (str): The Wave session ID.
            - output_path (str): Local file path to save the audio.

    Returns:
        str: Confirmation with file path and size, or error if no audio available.
    """
    await _check_rate_limit(ctx)
    try:
        client = _get_client(ctx)

        # Get signed URL
        await ctx.report_progress(0.1, "Getting signed audio URL...")
        resp = await client.get(f"/sessions/{params.session_id}/media")
        resp.raise_for_status()
        media = resp.json()

        audio_url = media.get("audio_url")
        if not audio_url:
            raise ToolError(f"No audio available for session {params.session_id}.")

        # Download the file
        await ctx.report_progress(0.3, "Downloading audio...")
        output = Path(params.output_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        async with (
            httpx.AsyncClient(timeout=300.0, follow_redirects=True) as dl_client,
            dl_client.stream("GET", audio_url) as dl_resp,
        ):
            dl_resp.raise_for_status()
            with open(output, "wb") as f:
                async for chunk in dl_resp.aiter_bytes(chunk_size=65536):
                    f.write(chunk)

        size_mb = output.stat().st_size / (1024 * 1024)
        logger.info("wave_download_audio: saved %s (%.1f MB)", output, size_mb)

        return (
            f"Audio downloaded successfully.\n"
            f"**File:** {output}\n"
            f"**Size:** {size_mb:.1f} MB\n"
            f"**Session:** {params.session_id}"
        )
    except Exception as e:
        if isinstance(e, ToolError):
            raise
        _handle_api_error(e)


@mcp.tool(
    name="wave_export_archive",
    annotations={
        "title": "Export Full Local Archive",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def wave_export_archive(params: ExportArchiveInput, ctx: Context) -> str:
    """Create a complete local archive of all your Wave sessions.

    Downloads metadata, summaries, and transcripts for every accessible session
    into organized folders named YYYYMMDD_Session-Title/. Each folder contains
    metadata.json, summary.md, and transcript.md. Optionally downloads audio.

    This is an incremental operation -- sessions already archived (detected by
    existing metadata.json) are skipped, so it's safe to re-run.

    Note: Sessions with corrupted (non-UUID) IDs in Wave's database are
    automatically skipped, as the API cannot access them. These are rare
    but exist due to a Wave data integrity issue.

    Args:
        params (ExportArchiveInput): Validated input parameters containing:
            - output_dir (str): Directory for the archive.
            - since (Optional[str]): Only archive sessions after this date.
            - include_audio (bool): Download audio files (default false).
            - response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: Summary of archived sessions with counts and any errors.
    """
    await _check_rate_limit(ctx)
    try:
        client = _get_client(ctx)
        archive_dir = Path(params.output_dir)
        archive_dir.mkdir(parents=True, exist_ok=True)

        # Step 1: Collect all session IDs via list endpoint
        await ctx.report_progress(0.05, "Listing all sessions...")
        all_sessions: list[dict] = []
        query_params: dict[str, Any] = {"limit": "100"}
        if params.since:
            query_params["since"] = params.since

        max_pages = 100  # Safety limit to prevent infinite pagination loops
        page = 0
        while True:
            page += 1
            if page > max_pages:
                logger.warning("wave_export_archive: hit max page limit (%d), stopping", max_pages)
                break
            rl = _get_rate_limiter(ctx)
            if not await rl.check():
                await asyncio.sleep(2)
                if not await rl.check():
                    raise ToolError("Rate limit exceeded during archive pagination. Try again later.")
            resp = await client.get("/sessions", params=query_params)
            resp.raise_for_status()
            data = resp.json()
            sessions = data.get("sessions", [])
            all_sessions.extend(sessions)
            has_more = data.get("has_more", False)
            next_cursor = data.get("next_cursor")
            if not has_more or not next_cursor or not sessions:
                break
            query_params["cursor"] = next_cursor

        all_sessions.sort(key=lambda s: s.get("timestamp", ""))

        # Detect already-archived sessions
        existing_ids: set[str] = set()
        for meta_path in archive_dir.glob("*/metadata.json"):
            try:
                meta = json.loads(meta_path.read_text())
                existing_ids.add(meta.get("id", ""))
            except Exception as exc:
                logger.warning("Skipping corrupted metadata at %s: %s", meta_path, exc)

        new_sessions = [s for s in all_sessions if s.get("id") not in existing_ids]
        logger.info(
            "wave_export_archive: %d total, %d already archived, %d new",
            len(all_sessions),
            len(existing_ids),
            len(new_sessions),
        )

        if not new_sessions:
            return f"Archive is up to date. {len(existing_ids)} sessions already archived in {archive_dir}"

        # Step 2: Export in batches of 50 via bulk endpoint
        archived = []
        errors = []
        total = len(new_sessions)

        for batch_start in range(0, total, MAX_BULK_SESSIONS):
            batch = new_sessions[batch_start : batch_start + MAX_BULK_SESSIONS]
            batch_ids = [s["id"] for s in batch if _UUID_PATTERN.match(s.get("id", ""))]

            if not batch_ids:
                continue

            pct = (batch_start / total) * 0.8 + 0.1
            await ctx.report_progress(
                pct, f"Exporting batch {batch_start // MAX_BULK_SESSIONS + 1} ({len(archived)}/{total} done)..."
            )

            rl = _get_rate_limiter(ctx)
            if not await rl.check():
                await asyncio.sleep(2)
                if not await rl.check():
                    raise ToolError("Rate limit exceeded during batch export. Try again later.")

            try:
                resp = await client.post(
                    "/sessions/bulk",
                    json={"session_ids": batch_ids, "include_transcript": True, "include_summary": True},
                )
                resp.raise_for_status()
                export_data = resp.json()
            except Exception as exc:
                logger.warning("Bulk export failed for batch of %d sessions: %s", len(batch_ids), exc)
                raw_err = str(exc)[:200]
                # Strip potential auth tokens from error messages
                sanitized_err = re.sub(r"Bearer [A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", raw_err)
                errors.append(
                    {"id": f"batch ({len(batch_ids)} sessions)", "error": sanitized_err, "session_ids": batch_ids}
                )
                continue

            for s in export_data.get("sessions", []):
                sid = s.get("id", "unknown")
                title = s.get("title", "Untitled")
                timestamp = s.get("timestamp", "")

                try:
                    dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    date_prefix = dt.strftime("%Y%m%d")
                except Exception:
                    date_prefix = "00000000"

                safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title)
                safe_title = safe_title.lstrip(".")
                safe_title = re.sub(r"\s+", "-", safe_title.strip())[:80].rstrip("-") or "untitled"
                sid_suffix = sid[:8] if sid else "unknown"
                folder_name = f"{date_prefix}_{safe_title}_{sid_suffix}"
                folder_path = archive_dir / folder_name
                folder_path.mkdir(parents=True, exist_ok=True)

                # Save metadata
                metadata = {
                    "id": sid,
                    "title": title,
                    "timestamp": timestamp,
                    "duration_seconds": s.get("duration_seconds"),
                    "duration_human": _format_duration(s.get("duration_seconds")),
                    "type": s.get("type", "unknown"),
                    "platform": s.get("platform") or "",
                    "folder": folder_name,
                }

                # Save summary
                if s.get("summary"):
                    (folder_path / "summary.md").write_text(
                        f"# {title}\n\n**Date:** {timestamp}  \n**Duration:** {_format_duration(s.get('duration_seconds'))}  \n\n## Summary\n\n{s['summary']}\n",
                        encoding="utf-8",
                    )
                    metadata["has_summary"] = True

                # Save transcript
                if s.get("transcript"):
                    (folder_path / "transcript.md").write_text(
                        f"# Transcript: {title}\n\n**Date:** {timestamp}  \n**Duration:** {_format_duration(s.get('duration_seconds'))}  \n\n---\n\n{s['transcript']}\n",
                        encoding="utf-8",
                    )
                    metadata["has_transcript"] = True

                (folder_path / "metadata.json").write_text(
                    json.dumps(metadata, indent=2, default=str), encoding="utf-8"
                )
                archived.append(metadata)

            for err in export_data.get("errors", []):
                errors.append(err)

        # Step 3: Optionally download audio
        audio_count = 0
        if params.include_audio:
            for i, meta in enumerate(archived):
                sid = meta["id"]
                folder_path = archive_dir / meta["folder"]
                if list(folder_path.glob("audio.*")):
                    continue  # Already has audio

                pct = 0.9 + (i / max(len(archived), 1)) * 0.09
                await ctx.report_progress(pct, f"Downloading audio {i + 1}/{len(archived)}...")

                try:
                    rl = _get_rate_limiter(ctx)
                    if not await rl.check():
                        await asyncio.sleep(2)
                        if not await rl.check():
                            raise ToolError("Rate limit exceeded during audio download. Try again later.")
                    resp = await client.get(f"/sessions/{sid}/media")
                    resp.raise_for_status()
                    audio_url = resp.json().get("audio_url")
                    if audio_url:
                        async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as dl:
                            async with dl.stream("GET", audio_url) as audio_resp:
                                audio_resp.raise_for_status()
                                with open(folder_path / "audio.m4a", "wb") as f:
                                    async for chunk in audio_resp.aiter_bytes(chunk_size=65536):
                                        f.write(chunk)
                            audio_count += 1
                except Exception as exc:
                    logger.debug("Audio download skipped for %s: %s", sid, exc)

        # Step 4: Create index
        all_archived = []
        for meta_path in sorted(archive_dir.glob("*/metadata.json")):
            try:
                all_archived.append(json.loads(meta_path.read_text()))
            except Exception as exc:
                logger.warning("Skipping corrupted metadata at %s: %s", meta_path, exc)

        index = {
            "archive_date": datetime.now(UTC).isoformat(),
            "total_sessions": len(all_archived),
            "new_this_run": len(archived),
            "errors": len(errors),
            "audio_downloaded": audio_count,
        }
        (archive_dir / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")

        if params.response_format == ResponseFormat.JSON:
            return _json_response({**index, "error_details": errors})

        result = (
            f"## Archive Export Complete\n\n"
            f"**Location:** {archive_dir}\n"
            f"**Total sessions in archive:** {len(all_archived)}\n"
            f"**New this run:** {len(archived)}\n"
            f"**Audio files downloaded:** {audio_count}\n"
            f"**Errors:** {len(errors)}\n"
        )
        if errors:
            result += "\n**Error details:**\n"
            for err in errors[:10]:
                result += f"- `{err.get('id', '?')}`: {err.get('error', '?')}\n"
        return result
    except Exception as e:
        if isinstance(e, ToolError):
            raise
        _handle_api_error(e)
