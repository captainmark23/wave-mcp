"""
Tests for wave_mcp package — covers pure functions, validators, and the rate limiter.

Run with: pytest test_wave_server.py -v
"""

import asyncio

import pytest

from wave_mcp.constants import _BLOCKED_DIRS
from wave_mcp.formatters import _format_duration, _sanitize_md
from wave_mcp.rate_limiter import _RateLimiter
from wave_mcp.validators import _validate_iso_date, _validate_session_id

# ---------------------------------------------------------------------------
# _validate_session_id
# ---------------------------------------------------------------------------


class TestValidateSessionId:
    def test_valid_uuid(self):
        sid = "abc123-def-456"
        assert _validate_session_id(sid) == sid

    def test_valid_alphanumeric(self):
        assert _validate_session_id("session123") == "session123"

    def test_valid_with_underscores(self):
        assert _validate_session_id("my_session_01") == "my_session_01"

    def test_strips_whitespace(self):
        assert _validate_session_id("  abc123  ") == "abc123"

    def test_rejects_path_traversal(self):
        with pytest.raises(ValueError, match="alphanumeric"):
            _validate_session_id("../etc/passwd")

    def test_rejects_slashes(self):
        with pytest.raises(ValueError, match="alphanumeric"):
            _validate_session_id("foo/bar")

    def test_rejects_spaces_in_id(self):
        with pytest.raises(ValueError, match="alphanumeric"):
            _validate_session_id("foo bar")

    def test_rejects_null_bytes(self):
        with pytest.raises(ValueError, match="alphanumeric"):
            _validate_session_id("abc\x00def")

    def test_rejects_empty_after_strip(self):
        with pytest.raises(ValueError, match="alphanumeric"):
            _validate_session_id("   ")

    def test_rejects_special_chars(self):
        with pytest.raises(ValueError, match="alphanumeric"):
            _validate_session_id("session;rm -rf /")


# ---------------------------------------------------------------------------
# _validate_iso_date
# ---------------------------------------------------------------------------


class TestValidateIsoDate:
    def test_none_returns_none(self):
        assert _validate_iso_date(None, "since") is None

    def test_valid_date(self):
        assert _validate_iso_date("2025-01-15", "since") == "2025-01-15"

    def test_valid_datetime(self):
        result = _validate_iso_date("2025-01-15T10:30:00", "since")
        assert result == "2025-01-15T10:30:00"

    def test_valid_datetime_with_tz(self):
        result = _validate_iso_date("2025-01-15T10:30:00+00:00", "since")
        assert result == "2025-01-15T10:30:00+00:00"

    def test_strips_whitespace(self):
        assert _validate_iso_date("  2025-01-15  ", "since") == "2025-01-15"

    def test_rejects_invalid_format(self):
        with pytest.raises(ValueError, match="valid ISO 8601"):
            _validate_iso_date("not-a-date", "since")

    def test_rejects_garbage(self):
        with pytest.raises(ValueError, match="valid ISO 8601"):
            _validate_iso_date("foo bar baz", "start_date")

    def test_field_name_in_error(self):
        with pytest.raises(ValueError, match="my_field"):
            _validate_iso_date("invalid", "my_field")


# ---------------------------------------------------------------------------
# _sanitize_md
# ---------------------------------------------------------------------------


class TestSanitizeMd:
    def test_none_returns_empty(self):
        assert _sanitize_md(None) == ""

    def test_plain_text_unchanged_content(self):
        # Letters and digits pass through; only special chars get escaped
        result = _sanitize_md("hello world")
        assert "hello" in result
        assert "world" in result

    def test_newlines_collapsed(self):
        result = _sanitize_md("line1\nline2\nline3")
        assert "\n" not in result

    def test_carriage_returns_collapsed(self):
        result = _sanitize_md("line1\r\nline2\rline3")
        assert "\n" not in result
        assert "\r" not in result

    def test_markdown_heading_injection(self):
        """Ensure injected heading markers are escaped."""
        result = _sanitize_md("# Injected Heading")
        assert result != "# Injected Heading"
        assert "\\#" in result

    def test_markdown_link_injection(self):
        result = _sanitize_md("[click](http://evil.com)")
        assert "\\[" in result
        assert "\\]" in result

    def test_backtick_escaped(self):
        result = _sanitize_md("code: `rm -rf /`")
        assert "\\`" in result

    def test_asterisk_escaped(self):
        result = _sanitize_md("**bold**")
        assert "\\*" in result

    def test_horizontal_rule_injection(self):
        """Newline + --- could inject a horizontal rule — dashes are stripped."""
        result = _sanitize_md("text\n---\nmore")
        # The --- pattern is stripped by the structural injection filter,
        # which turns "text\n---\nmore" into "text\n\nmore" (paragraph break preserved).
        assert "---" not in result
        assert "\\-\\-\\-" not in result
        assert "text" in result
        assert "more" in result

    def test_paragraph_breaks_preserved(self):
        """Double newlines (paragraph breaks) should be preserved."""
        result = _sanitize_md("paragraph one\n\nparagraph two")
        assert "\n\n" in result
        assert "paragraph one" in result
        assert "paragraph two" in result

    def test_single_newlines_collapsed_within_paragraph(self):
        """Single newlines within a paragraph should collapse to spaces."""
        result = _sanitize_md("line one\nline two\nline three")
        # All within one paragraph — single newlines collapse
        assert "\n" not in result
        assert "line one line two line three" in result

    def test_heading_injection_stripped(self):
        """Injected heading markers after newlines are stripped."""
        result = _sanitize_md("text\n# Injected\nmore")
        # The \n# pattern is stripped, so "# " becomes just a space
        assert "\\# Injected" not in result.replace("\\", "")

    def test_blockquote_injection_stripped(self):
        """Injected blockquotes after newlines are stripped."""
        result = _sanitize_md("text\n> quote\nmore")
        # The \n> pattern is stripped
        assert "\n>" not in result

    def test_code_fence_injection_stripped(self):
        """Injected code fences after newlines are stripped."""
        result = _sanitize_md("text\n```\ncode\n```\nmore")
        # The \n``` pattern is stripped
        assert "\n```" not in result


# ---------------------------------------------------------------------------
# _format_duration
# ---------------------------------------------------------------------------


class TestFormatDuration:
    def test_none(self):
        assert _format_duration(None) == "unknown"

    def test_zero(self):
        assert _format_duration(0) == "0s"

    def test_seconds_only(self):
        assert _format_duration(45) == "45s"

    def test_minutes_and_seconds(self):
        assert _format_duration(125) == "2m 5s"

    def test_exact_minute(self):
        assert _format_duration(60) == "1m 0s"

    def test_hours(self):
        assert _format_duration(3661) == "1h 1m"

    def test_large_value(self):
        result = _format_duration(7200)
        assert result == "2h 0m"

    def test_float_input(self):
        assert _format_duration(90.7) == "1m 30s"


# ---------------------------------------------------------------------------
# _BLOCKED_DIRS constant
# ---------------------------------------------------------------------------


class TestBlockedDirs:
    def test_is_a_set(self):
        assert isinstance(_BLOCKED_DIRS, set)

    def test_contains_critical_dirs(self):
        for d in ["/etc", "/usr", "/bin", "/sbin", "/var", "/System", "/Library"]:
            assert d in _BLOCKED_DIRS, f"Missing blocked dir: {d}"

    def test_contains_private_dirs(self):
        for d in ["/private", "/private/etc", "/private/var", "/private/tmp"]:
            assert d in _BLOCKED_DIRS, f"Missing blocked dir: {d}"


# ---------------------------------------------------------------------------
# _RateLimiter
# ---------------------------------------------------------------------------


class TestRateLimiter:
    @pytest.fixture
    def event_loop_policy(self):
        """Ensure we have an event loop for async tests."""
        return asyncio.DefaultEventLoopPolicy()

    @pytest.mark.asyncio
    async def test_allows_requests_under_limit(self):
        rl = _RateLimiter(max_per_minute=5)
        for _ in range(5):
            assert await rl.check() is True

    @pytest.mark.asyncio
    async def test_blocks_when_limit_reached(self):
        rl = _RateLimiter(max_per_minute=3)
        for _ in range(3):
            await rl.check()
        assert await rl.check() is False

    @pytest.mark.asyncio
    async def test_remaining_property(self):
        rl = _RateLimiter(max_per_minute=5)
        assert rl.remaining == 5
        await rl.check()
        assert rl.remaining == 4
        await rl.check()
        assert rl.remaining == 3

    @pytest.mark.asyncio
    async def test_single_request_limit(self):
        rl = _RateLimiter(max_per_minute=1)
        assert await rl.check() is True
        assert await rl.check() is False

    @pytest.mark.asyncio
    async def test_default_limit_is_50(self):
        rl = _RateLimiter()
        assert rl._max == 50
