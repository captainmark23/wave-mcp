"""Formatting and sanitization helpers for Wave MCP Server."""

import json
import re
from typing import Any


def _sanitize_md(text: str | None) -> str:
    """Escape markdown special characters while preserving paragraph breaks.

    Prevents markdown injection by escaping formatting chars and stripping
    structural markdown patterns, while keeping text readable by preserving
    paragraph breaks (double newlines).
    """
    if not text:
        return ""
    # Normalize line endings
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Strip structural markdown injection patterns
    text = re.sub(r"\n#{1,6}\s", "\n", text)  # headings
    text = re.sub(r"\n---+", "\n", text)  # horizontal rules
    text = re.sub(r"\n>", "\n", text)  # blockquotes
    text = re.sub(r"\n```", "\n", text)  # code fences
    # Preserve paragraph breaks, collapse other whitespace
    paragraphs = re.split(r"\n{2,}", text)
    sanitized = []
    for para in paragraphs:
        # Collapse single newlines to spaces within paragraphs
        para = para.replace("\n", " ").strip()
        # Escape markdown formatting characters that can inject structure.
        # Deliberately skip . - ! ( ) which are common in plain text and only
        # meaningful in narrow contexts already handled by the structural filters.
        for ch in r"\`*_{}[]#+|~":
            para = para.replace(ch, f"\\{ch}")
        if para:
            sanitized.append(para)
    return "\n\n".join(sanitized)


def _format_duration(seconds: float | int | None) -> str:
    """Convert seconds to human-readable duration."""
    if seconds is None:
        return "unknown"
    if seconds < 0:
        return "0s"
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
