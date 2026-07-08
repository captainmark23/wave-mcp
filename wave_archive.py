#!/usr/bin/env python3
"""wave-archive — create and maintain a local backup of your Wave sessions.

A small, cross-platform CLI built directly on the official Wave REST API
(https://api.wave.co/v1). It is the one capability the hosted MCP server at
mcp.wave.co does not provide: writing a complete, offline archive of your
recordings (metadata, summaries, transcripts, and optionally audio) to disk.

For ambient access to recordings inside Claude/ChatGPT/Cursor, use the
official hosted MCP server instead — see the README. This tool is for backups,
knowledge-base backfills, and cron jobs.

Auth: set WAVE_API_KEY to a token from
https://app.wave.co/settings/integrations/api (tokens start with "wave_api_").
The token needs the sessions:read, transcripts:read, and — for --include-audio
— media:read scopes.

Example:
    export WAVE_API_KEY=wave_api_...
    wave-archive --output-dir ~/Documents/Wave --folder work --include-audio
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

__version__ = "3.0.0"

logger = logging.getLogger("wave_archive")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

API_BASE_URL = "https://api.wave.co/v1"
DEFAULT_TIMEOUT = 30.0  # seconds; generous for the API
DOWNLOAD_TIMEOUT = 300.0  # seconds; audio files can be large

MAX_PAGINATION_PAGES = 500  # safety cap against runaway pagination loops
LIST_PAGE_LIMIT = 100  # Wave API hard limit per page
MAX_BULK_SESSIONS = 50  # Wave /sessions/bulk accepts up to 50 IDs
MAX_AUDIO_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB safety cap per file

# Session IDs must be safe for use as a path component (prevents traversal).
# Wave uses prefixed ULIDs such as "sess_01HX4R7MT8PQZ6VCN9ABYF2KJM".
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

# Directories a backup must never be written into, across platforms.
_BLOCKED_DIRS = {
    "/",
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/boot",
    "/dev",
    "/proc",
    "/sys",
    "/var",
    "/System",
    "/Library",
    "/private",
}

_BEARER_RE = re.compile(r"Bearer \S+")
_TOKEN_RE = re.compile(r"wave_api_\S+")


class WaveError(Exception):
    """A user-facing error; message is safe to print (no secrets)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _redact(msg: str) -> str:
    """Strip anything that looks like a token out of a string."""
    msg = _BEARER_RE.sub("Bearer [REDACTED]", msg)
    return _TOKEN_RE.sub("wave_api_[REDACTED]", msg)


def sanitize_md(text: str | None) -> str:
    """Escape markdown so archived transcripts/summaries can't inject structure.

    Preserves paragraph breaks for readability while neutralizing headings,
    rules, blockquotes, code fences, and inline formatting characters.
    """
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n#{1,6}\s", "\n", text)  # headings
    text = re.sub(r"\n---+", "\n", text)  # horizontal rules
    text = re.sub(r"\n>", "\n", text)  # blockquotes
    text = re.sub(r"\n```", "\n", text)  # code fences
    paragraphs = re.split(r"\n{2,}", text)
    out = []
    for para in paragraphs:
        para = para.replace("\n", " ").strip()
        for ch in r"\`*_{}[]#+|~":
            para = para.replace(ch, f"\\{ch}")
        if para:
            out.append(para)
    return "\n\n".join(out)


def format_duration(seconds: float | int | None) -> str:
    """Convert seconds to a human-readable duration."""
    if seconds is None:
        return "unknown"
    seconds = int(seconds)
    if seconds < 0:
        return "0s"
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins}m"


def safe_folder_name(title: str, timestamp: str, session_id: str) -> str:
    """Build a filesystem-safe, collision-resistant folder name for a session."""
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        date_prefix = dt.strftime("%Y%m%d")
    except (ValueError, AttributeError):
        date_prefix = "00000000"
    safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title or "")
    safe_title = safe_title.lstrip(".")
    safe_title = re.sub(r"\s+", "-", safe_title.strip())[:80].rstrip("-") or "untitled"
    sid_suffix = re.sub(r"[^A-Za-z0-9]", "", session_id or "")[-8:] or "unknown"
    return f"{date_prefix}_{safe_title}_{sid_suffix}"


def validate_output_dir(path: str) -> Path:
    """Resolve and validate the archive directory is not a system location."""
    p = Path(path).expanduser()
    resolved = p.resolve()
    resolved_str = str(resolved)
    for blocked in _BLOCKED_DIRS:
        if resolved_str == blocked or resolved_str.startswith(blocked.rstrip("/") + "/"):
            # Allow user home dirs that merely start with /var on some systems? No —
            # keep it strict; users pass an explicit path under their home normally.
            if blocked == "/" and resolved != Path("/"):
                continue
            raise WaveError(f"Refusing to write archive into a system directory: {blocked}")
    return resolved


def validate_https(url: str) -> None:
    """Reject non-HTTPS download URLs to avoid SSRF via open redirects."""
    from urllib.parse import urlparse

    if urlparse(url).scheme != "https":
        raise WaveError("Refusing to download from a non-HTTPS URL.")


def write_private(path: Path, content: str) -> None:
    """Write text with owner-only permissions where the OS supports it."""
    path.write_text(content, encoding="utf-8")
    with contextlib.suppress(OSError, NotImplementedError):
        os.chmod(path, 0o600)  # best effort; limited on Windows


def get_token() -> str:
    """Resolve the Wave API token from the environment (or macOS Keychain)."""
    for var in ("WAVE_API_KEY", "WAVE_API_TOKEN"):
        token = os.environ.get(var, "").strip()
        if token:
            return token
    # Optional macOS Keychain fallback for existing installs.
    if sys.platform == "darwin":
        import subprocess

        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-a", "wave-mcp", "-s", "wave-api-token", "-w"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Keychain lookup failed: %s", exc)
    raise WaveError(
        "No Wave API token found. Set WAVE_API_KEY to a token from https://app.wave.co/settings/integrations/api"
    )


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------


class WaveClient:
    """Thin synchronous wrapper over the Wave REST API with 429 backoff."""

    def __init__(self, token: str, base_url: str = API_BASE_URL, max_retries: int = 4):
        self._client = httpx.Client(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": f"wave-archive/{__version__}",
            },
            timeout=DEFAULT_TIMEOUT,
        )
        self._max_retries = max_retries

    def __enter__(self) -> WaveClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Issue a request, retrying on 429/5xx with exponential backoff."""
        for attempt in range(self._max_retries + 1):
            try:
                resp = self._client.request(method, path, **kwargs)
            except httpx.TimeoutException as exc:
                raise WaveError("Request to Wave timed out — try again shortly.") from exc
            except httpx.ConnectError as exc:
                raise WaveError("Could not connect to the Wave API — check your network.") from exc

            if (resp.status_code == 429 or resp.status_code >= 500) and attempt < self._max_retries:
                delay = self._retry_delay(resp, attempt)
                logger.warning(
                    "Wave API %s — backing off %.1fs (attempt %d/%d)",
                    resp.status_code,
                    delay,
                    attempt + 1,
                    self._max_retries,
                )
                time.sleep(delay)
                continue
            self._raise_for_status(resp)
            if resp.content:
                return resp.json()
            return {}
        raise WaveError("Wave API request failed after retries.")

    @staticmethod
    def _retry_delay(resp: httpx.Response, attempt: int) -> float:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
        return float(2 ** (attempt + 1))  # 2, 4, 8, 16

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.is_success:
            return
        code = resp.status_code
        detail = ""
        try:
            body = resp.json()
            detail = body.get("error", {}).get("message") or body.get("message") or ""
        except (ValueError, AttributeError):
            pass
        if code == 401:
            raise WaveError(
                "Authentication failed (401). Your WAVE_API_KEY may be invalid or expired. "
                "Generate a new one at https://app.wave.co/settings/integrations/api"
            )
        if code == 403:
            raise WaveError(
                "Permission denied (403). Your token is likely missing a scope "
                "(need sessions:read, transcripts:read, and media:read for audio)."
            )
        if code == 404:
            raise WaveError(f"Not found (404). {detail}".strip())
        raise WaveError(_redact(f"Wave API returned {code}. {detail}".strip()))

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.request("POST", path, **kwargs)

    def stream_download(self, url: str, dest: Path) -> int:
        """Stream a signed media URL to disk, enforcing HTTPS and a size cap."""
        validate_https(url)
        bytes_written = 0
        try:
            with (
                httpx.Client(timeout=DOWNLOAD_TIMEOUT, follow_redirects=True) as dl,
                dl.stream("GET", url) as resp,
            ):
                resp.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=65536):
                        bytes_written += len(chunk)
                        if bytes_written > MAX_AUDIO_DOWNLOAD_BYTES:
                            raise WaveError(
                                f"Media exceeds the {MAX_AUDIO_DOWNLOAD_BYTES // (1024 * 1024)} MB "
                                "cap — download aborted."
                            )
                        f.write(chunk)
        except Exception:
            dest.unlink(missing_ok=True)  # never leave a partial file
            raise
        return bytes_written


# ---------------------------------------------------------------------------
# Archive logic
# ---------------------------------------------------------------------------


def resolve_folder_id(client: WaveClient, folder: str) -> str | None:
    """Match a folder by id or case-insensitive name; return its id."""
    data = client.get("/folders")
    folders = data.get("folders", [])
    for f in folders:
        if f.get("id") == folder or (f.get("name", "").lower() == folder.lower()):
            return f.get("id")
    names = ", ".join(f.get("name", "?") for f in folders) or "(none)"
    raise WaveError(f"Folder '{folder}' not found. Available folders: {names}")


def list_sessions(client: WaveClient, since: str | None, folder_id: str | None) -> list[dict]:
    """Auto-paginate the /sessions endpoint into a single list."""
    sessions: list[dict] = []
    params: dict[str, Any] = {"limit": str(LIST_PAGE_LIMIT)}
    if since:
        params["since"] = since
    if folder_id:
        params["folder"] = folder_id

    for page in range(1, MAX_PAGINATION_PAGES + 1):
        data = client.get("/sessions", params=params)
        batch = data.get("sessions", [])
        sessions.extend(batch)
        logger.info("Listed page %d (%d sessions so far)", page, len(sessions))
        next_cursor = data.get("next_cursor")
        if not data.get("has_more") or not next_cursor or not batch:
            break
        params["cursor"] = next_cursor
    else:
        logger.warning("Stopped at pagination cap of %d pages", MAX_PAGINATION_PAGES)

    sessions.sort(key=lambda s: s.get("timestamp", ""))
    return sessions


def already_archived_ids(archive_dir: Path) -> set[str]:
    """Collect session ids already present in the archive (for incrementality)."""
    ids: set[str] = set()
    for meta_path in archive_dir.glob("*/metadata.json"):
        try:
            ids.add(json.loads(meta_path.read_text()).get("id", ""))
        except (ValueError, OSError) as exc:
            logger.warning("Ignoring unreadable metadata at %s: %s", meta_path, exc)
    ids.discard("")
    return ids


def write_session(archive_dir: Path, session: dict) -> dict:
    """Write one exported session's files; return its metadata record."""
    sid = session.get("id", "unknown")
    title = session.get("title", "Untitled")
    timestamp = session.get("timestamp", "")
    folder_name = safe_folder_name(title, timestamp, sid)
    folder_path = archive_dir / folder_name
    folder_path.mkdir(parents=True, exist_ok=True)

    metadata: dict[str, Any] = {
        "id": sid,
        "title": title,
        "timestamp": timestamp,
        "duration_seconds": session.get("duration_seconds"),
        "duration_human": format_duration(session.get("duration_seconds")),
        "type": session.get("type", "unknown"),
        "platform": session.get("platform") or "",
        "folder": folder_name,
    }
    if session.get("phone"):
        metadata["phone"] = session["phone"]

    dur = format_duration(session.get("duration_seconds"))
    if session.get("summary"):
        write_private(
            folder_path / "summary.md",
            f"# {sanitize_md(title)}\n\n**Date:** {timestamp}  \n**Duration:** {dur}  \n\n"
            f"## Summary\n\n{sanitize_md(session['summary'])}\n",
        )
        metadata["has_summary"] = True
    if session.get("transcript"):
        write_private(
            folder_path / "transcript.md",
            f"# Transcript: {sanitize_md(title)}\n\n**Date:** {timestamp}  \n**Duration:** {dur}  \n\n"
            f"---\n\n{sanitize_md(session['transcript'])}\n",
        )
        metadata["has_transcript"] = True

    write_private(folder_path / "metadata.json", json.dumps(metadata, indent=2, default=str))
    return metadata


def download_audio(client: WaveClient, archive_dir: Path, meta: dict) -> bool:
    """Download audio for one archived session if not already present."""
    folder_path = archive_dir / meta["folder"]
    if any(folder_path.glob("audio.*")):
        return False
    media = client.get(f"/sessions/{meta['id']}/media")
    audio_url = media.get("audio_url")
    if not audio_url:
        return False
    client.stream_download(audio_url, folder_path / "audio.m4a")
    return True


def archive(
    client: WaveClient,
    output_dir: str,
    *,
    since: str | None = None,
    folder: str | None = None,
    include_audio: bool = False,
) -> dict:
    """Run an incremental archive and return a summary dict."""
    archive_dir = validate_output_dir(output_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)

    folder_id = resolve_folder_id(client, folder) if folder else None

    sessions = list_sessions(client, since, folder_id)
    existing = already_archived_ids(archive_dir)
    new_sessions = [s for s in sessions if s.get("id") not in existing]
    logger.info("%d listed, %d already archived, %d new", len(sessions), len(existing), len(new_sessions))

    archived: list[dict] = []
    errors: list[dict] = []

    for start in range(0, len(new_sessions), MAX_BULK_SESSIONS):
        batch = new_sessions[start : start + MAX_BULK_SESSIONS]
        batch_ids = [s["id"] for s in batch if _SESSION_ID_PATTERN.match(s.get("id", ""))]
        skipped = [s.get("id") for s in batch if not _SESSION_ID_PATTERN.match(s.get("id", ""))]
        for bad in skipped:
            errors.append({"id": bad, "error": "unsafe session id — skipped"})
        if not batch_ids:
            continue
        logger.info("Exporting %d sessions (%d/%d)", len(batch_ids), start, len(new_sessions))
        try:
            export = client.post(
                "/sessions/bulk",
                json={"session_ids": batch_ids, "include_transcript": True, "include_summary": True},
            )
        except WaveError as exc:
            errors.append({"id": f"batch[{start}:{start + len(batch_ids)}]", "error": _redact(str(exc))})
            continue
        for s in export.get("sessions", []):
            try:
                archived.append(write_session(archive_dir, s))
            except OSError as exc:
                errors.append({"id": s.get("id", "?"), "error": f"write failed: {exc}"})
        errors.extend(export.get("errors", []))

    audio_count = 0
    if include_audio:
        for i, meta in enumerate(archived, 1):
            logger.info("Audio %d/%d", i, len(archived))
            try:
                if download_audio(client, archive_dir, meta):
                    audio_count += 1
            except WaveError as exc:
                logger.debug("Audio skipped for %s: %s", meta["id"], _redact(str(exc)))

    total_archived = sum(1 for _ in archive_dir.glob("*/metadata.json"))
    summary = {
        "archive_date": datetime.now(UTC).isoformat(),
        "location": str(archive_dir),
        "total_sessions": total_archived,
        "new_this_run": len(archived),
        "audio_downloaded": audio_count,
        "errors": len(errors),
        "error_details": errors[:20],
    }
    (archive_dir / "index.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wave-archive",
        description="Back up your Wave sessions to a local folder via the official REST API.",
    )
    parser.add_argument("--output-dir", "-o", required=True, help="Directory to write the archive into.")
    parser.add_argument("--since", help="Only archive sessions after this ISO 8601 date (e.g. 2025-01-01).")
    parser.add_argument("--folder", help="Only archive sessions in this Wave folder (id or name).")
    parser.add_argument("--include-audio", action="store_true", help="Also download audio (needs media:read).")
    parser.add_argument("--json", action="store_true", help="Print the run summary as JSON.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose progress logging to stderr.")
    parser.add_argument("--version", action="version", version=f"wave-archive {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s [wave-archive] %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    try:
        token = get_token()
        with WaveClient(token) as client:
            summary = archive(
                client,
                args.output_dir,
                since=args.since,
                folder=args.folder,
                include_audio=args.include_audio,
            )
    except WaveError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"Archive location:  {summary['location']}")
        print(f"Total sessions:    {summary['total_sessions']}")
        print(f"New this run:      {summary['new_this_run']}")
        print(f"Audio downloaded:  {summary['audio_downloaded']}")
        print(f"Errors:            {summary['errors']}")
        for err in summary["error_details"]:
            print(f"  - {err.get('id', '?')}: {err.get('error', '?')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
