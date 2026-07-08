"""Tests for the wave-archive CLI."""

import json

import pytest

import wave_archive as wa

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_redact_strips_bearer_and_token():
    assert "REDACTED" in wa._redact("Authorization: Bearer abc.def.ghi")
    assert "wave_api_secret" not in wa._redact("token wave_api_secret123 leaked")


def test_sanitize_md_neutralizes_injection():
    out = wa.sanitize_md("intro\n\n# Heading\n\n```code fence```\n> quote")
    # No line in the output renders as a heading, code fence, or blockquote.
    for line in out.split("\n"):
        stripped = line.lstrip()
        assert not stripped.startswith("# ")
        assert not stripped.startswith("```")
        assert not stripped.startswith(">")
    # Inline formatting chars are backslash-escaped rather than left live.
    assert wa.sanitize_md("*bold*").startswith("\\*")
    assert wa.sanitize_md("# lead").startswith("\\#")


def test_sanitize_md_preserves_paragraphs():
    out = wa.sanitize_md("para one\n\npara two")
    assert out == "para one\n\npara two"


def test_sanitize_md_empty():
    assert wa.sanitize_md(None) == ""
    assert wa.sanitize_md("") == ""


@pytest.mark.parametrize(
    "seconds,expected",
    [(None, "unknown"), (-5, "0s"), (45, "45s"), (90, "1m 30s"), (3661, "1h 1m")],
)
def test_format_duration(seconds, expected):
    assert wa.format_duration(seconds) == expected


def test_safe_folder_name_normal():
    name = wa.safe_folder_name("Weekly Sync", "2025-04-18T16:00:00Z", "sess_01HX4R7MT8PQZ6VCN9ABYF2KJM")
    assert name.startswith("20250418_Weekly-Sync_")
    assert "/" not in name and "\\" not in name


def test_safe_folder_name_path_traversal_blocked():
    name = wa.safe_folder_name("../../etc/passwd", "bad-date", "sess_x")
    assert ".." not in name
    assert "/" not in name
    assert name.startswith("00000000_")


def test_safe_folder_name_empty_title():
    name = wa.safe_folder_name("", "2025-01-01T00:00:00Z", "sess_abc")
    assert "untitled" in name


def test_validate_output_dir_rejects_system_dirs():
    for bad in ("/etc", "/usr/local", "/System/x"):
        with pytest.raises(wa.WaveError):
            wa.validate_output_dir(bad)


def test_validate_output_dir_allows_tmp(tmp_path):
    resolved = wa.validate_output_dir(str(tmp_path / "archive"))
    assert str(resolved).startswith(str(tmp_path))


def test_validate_https_rejects_http():
    with pytest.raises(wa.WaveError):
        wa.validate_https("http://insecure/audio.m4a")
    wa.validate_https("https://ok/audio.m4a")  # no raise


def test_get_token_from_env(monkeypatch):
    monkeypatch.setenv("WAVE_API_KEY", "wave_api_fromkey")
    monkeypatch.delenv("WAVE_API_TOKEN", raising=False)
    assert wa.get_token() == "wave_api_fromkey"


def test_get_token_missing(monkeypatch):
    monkeypatch.delenv("WAVE_API_KEY", raising=False)
    monkeypatch.delenv("WAVE_API_TOKEN", raising=False)
    monkeypatch.setattr(wa.sys, "platform", "linux")
    with pytest.raises(wa.WaveError):
        wa.get_token()


def test_write_private_sets_perms(tmp_path):
    p = tmp_path / "f.txt"
    wa.write_private(p, "secret")
    assert p.read_text() == "secret"
    # 0o600 on POSIX
    assert (p.stat().st_mode & 0o777) == 0o600


# ---------------------------------------------------------------------------
# Fake client + archive orchestration
# ---------------------------------------------------------------------------


class FakeClient:
    """Stand-in for WaveClient that serves canned responses."""

    def __init__(self, sessions, bulk, folders=None, media=None):
        self._sessions = sessions
        self._bulk = bulk
        self._folders = folders or {"folders": []}
        self._media = media or {}
        self.downloads = []

    def get(self, path, **kwargs):
        if path == "/folders":
            return self._folders
        if path == "/sessions":
            # No pagination in tests: single page.
            return {"sessions": self._sessions, "has_more": False, "next_cursor": None}
        if path.endswith("/media"):
            return self._media
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path, **kwargs):
        if path == "/sessions/bulk":
            return self._bulk
        raise AssertionError(f"unexpected POST {path}")

    def stream_download(self, url, dest):
        self.downloads.append((url, dest))
        dest.write_bytes(b"audio")
        return 5


def _session(sid, title="Meeting", ts="2025-04-18T16:00:00Z"):
    return {"id": sid, "title": title, "timestamp": ts, "duration_seconds": 60, "type": "meeting"}


def test_archive_writes_tree(tmp_path):
    sid = "sess_01HX4R7MT8PQZ6VCN9ABYF2KJM"
    client = FakeClient(
        sessions=[_session(sid)],
        bulk={"sessions": [{**_session(sid), "summary": "## Overview\nhi", "transcript": "Josh: hello"}]},
    )
    summary = wa.archive(client, str(tmp_path / "arc"))
    assert summary["new_this_run"] == 1
    assert summary["total_sessions"] == 1
    folders = list((tmp_path / "arc").glob("2025*"))
    assert len(folders) == 1
    assert (folders[0] / "metadata.json").exists()
    assert (folders[0] / "summary.md").exists()
    assert (folders[0] / "transcript.md").exists()
    assert json.loads((folders[0] / "metadata.json").read_text())["id"] == sid


def test_archive_is_incremental(tmp_path):
    sid = "sess_abc123"
    client = FakeClient(sessions=[_session(sid)], bulk={"sessions": [_session(sid)]})
    wa.archive(client, str(tmp_path / "arc"))
    # Second run: session already present, nothing new.
    summary = wa.archive(client, str(tmp_path / "arc"))
    assert summary["new_this_run"] == 0
    assert summary["total_sessions"] == 1


def test_archive_skips_unsafe_ids(tmp_path):
    client = FakeClient(
        sessions=[_session("../evil")],
        bulk={"sessions": []},
    )
    summary = wa.archive(client, str(tmp_path / "arc"))
    assert summary["new_this_run"] == 0
    assert any("unsafe" in e["error"] for e in summary["error_details"])


def test_archive_include_audio(tmp_path):
    sid = "sess_withaudio"
    client = FakeClient(
        sessions=[_session(sid)],
        bulk={"sessions": [_session(sid)]},
        media={"audio_url": "https://media/audio.m4a"},
    )
    summary = wa.archive(client, str(tmp_path / "arc"), include_audio=True)
    assert summary["audio_downloaded"] == 1
    assert len(client.downloads) == 1


def test_resolve_folder_id_by_name(tmp_path):
    client = FakeClient(
        sessions=[],
        bulk={"sessions": []},
        folders={"folders": [{"id": "fld_1", "name": "Work"}]},
    )
    assert wa.resolve_folder_id(client, "work") == "fld_1"


def test_resolve_folder_id_not_found():
    client = FakeClient(sessions=[], bulk={"sessions": []}, folders={"folders": []})
    with pytest.raises(wa.WaveError):
        wa.resolve_folder_id(client, "nope")


def test_write_session_sanitizes_injection(tmp_path):
    arc = tmp_path / "arc"
    arc.mkdir()
    meta = wa.write_session(
        arc,
        {"id": "sess_x", "title": "T", "timestamp": "2025-01-01T00:00:00Z", "summary": "# pwned\n```rm -rf```"},
    )
    summary_md = (arc / meta["folder"] / "summary.md").read_text()
    # Injected heading and code fence are escaped, so they can't render as structure.
    assert "\\# pwned" in summary_md
    assert "```rm -rf```" not in summary_md
