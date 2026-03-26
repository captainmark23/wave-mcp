"""
Tests for wave_mcp package — covers pure functions, validators, and the rate limiter.

Run with: pytest test_wave_server.py -v
"""

import asyncio
from unittest.mock import MagicMock

import httpx
import pytest

from wave_mcp.client import _handle_api_error, _redact_tokens, _validate_download_url, ToolError
from wave_mcp.constants import _BLOCKED_DIRS
from wave_mcp.formatters import _format_duration, _sanitize_md
from wave_mcp.rate_limiter import _RateLimiter
from wave_mcp.validators import _validate_iso_date, _validate_output_path, _validate_session_id

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


# ---------------------------------------------------------------------------
# _redact_tokens
# ---------------------------------------------------------------------------


class TestRedactTokens:
    def test_redacts_bearer_token(self):
        msg = "Error: Bearer sk_live_abc123 was invalid"
        result = _redact_tokens(msg)
        assert "sk_live_abc123" not in result
        assert "Bearer [REDACTED]" in result

    def test_redacts_bearer_with_url(self):
        msg = "Bearer https://attacker.com/exfil"
        result = _redact_tokens(msg)
        assert "attacker.com" not in result
        assert "Bearer [REDACTED]" in result

    def test_no_token_unchanged(self):
        msg = "Simple error message"
        assert _redact_tokens(msg) == msg

    def test_multiple_tokens(self):
        msg = "Bearer tok1 and Bearer tok2"
        result = _redact_tokens(msg)
        assert "tok1" not in result
        assert "tok2" not in result
        assert result.count("[REDACTED]") == 2


# ---------------------------------------------------------------------------
# _handle_api_error
# ---------------------------------------------------------------------------


class TestHandleApiError:
    def _make_http_error(self, status: int) -> httpx.HTTPStatusError:
        request = MagicMock(spec=httpx.Request)
        response = MagicMock(spec=httpx.Response)
        response.status_code = status
        return httpx.HTTPStatusError("error", request=request, response=response)

    def test_401_raises_auth_error(self):
        with pytest.raises(ToolError, match="Authentication failed"):
            _handle_api_error(self._make_http_error(401))

    def test_403_raises_permission_error(self):
        with pytest.raises(ToolError, match="Permission denied"):
            _handle_api_error(self._make_http_error(403))

    def test_404_raises_not_found(self):
        with pytest.raises(ToolError, match="not found"):
            _handle_api_error(self._make_http_error(404))

    def test_429_raises_rate_limit(self):
        with pytest.raises(ToolError, match="Rate limit"):
            _handle_api_error(self._make_http_error(429))

    def test_500_raises_generic_status(self):
        with pytest.raises(ToolError, match="status 500"):
            _handle_api_error(self._make_http_error(500))

    def test_timeout_raises_timeout_error(self):
        with pytest.raises(ToolError, match="timed out"):
            _handle_api_error(httpx.ReadTimeout("timeout"))

    def test_connect_error(self):
        with pytest.raises(ToolError, match="Could not connect"):
            _handle_api_error(httpx.ConnectError("refused"))

    def test_generic_error_no_details_leaked(self):
        with pytest.raises(ToolError, match="unexpected error"):
            _handle_api_error(RuntimeError("internal secret details"))


# ---------------------------------------------------------------------------
# _validate_output_path
# ---------------------------------------------------------------------------


class TestValidateOutputPath:
    def test_valid_absolute_path(self):
        result = _validate_output_path("/Users/me/Wave/audio.m4a", "output_path")
        assert result == "/Users/me/Wave/audio.m4a"

    def test_strips_whitespace(self):
        result = _validate_output_path("  /Users/me/file.txt  ", "output_path")
        assert result == "/Users/me/file.txt"

    def test_rejects_relative_path(self):
        with pytest.raises(ValueError, match="absolute path"):
            _validate_output_path("relative/path.txt", "output_path")

    def test_rejects_blocked_etc(self):
        with pytest.raises(ValueError, match="/etc/"):
            _validate_output_path("/etc/passwd", "output_path")

    def test_rejects_blocked_system(self):
        with pytest.raises(ValueError, match="/System/"):
            _validate_output_path("/System/Library/file", "output_path")

    def test_field_name_in_error(self):
        with pytest.raises(ValueError, match="my_field"):
            _validate_output_path("relative", "my_field")


# ---------------------------------------------------------------------------
# _validate_download_url
# ---------------------------------------------------------------------------


class TestValidateDownloadUrl:
    def test_https_allowed(self):
        _validate_download_url("https://cdn.wave.co/audio/abc.m4a")

    def test_http_rejected(self):
        with pytest.raises(ToolError, match="non-HTTPS"):
            _validate_download_url("http://evil.com/audio.m4a")

    def test_ftp_rejected(self):
        with pytest.raises(ToolError, match="non-HTTPS"):
            _validate_download_url("ftp://files.example.com/audio.m4a")

    def test_file_scheme_rejected(self):
        with pytest.raises(ToolError, match="non-HTTPS"):
            _validate_download_url("file:///etc/passwd")
