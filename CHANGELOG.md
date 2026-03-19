# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0] - 2026-03-18

### Added
- **wave_list_all_sessions** — auto-paginates through entire session history in one call
- **wave_discover_and_export** — combines search + bulk export into a single operation, auto-skips corrupted IDs
- **wave_download_audio** — downloads audio recording to a local file path with signed URL handling
- **wave_export_archive** — creates a complete local archive (metadata, summaries, transcripts, optional audio) with incremental updates
- Corrupted session ID detection in search results — warns when Wave returns non-UUID session IDs that cannot be accessed via API
- Path validation on audio download (blocks system directories, requires absolute paths)
- Pagination safety limit (max 100 pages) to prevent infinite loops
- UUID pattern matching for session ID integrity checks

### Changed
- **BREAKING:** Session type allowlist expanded — added `recording`, `recovery`, `podcast` (the types Wave actually uses in practice). Previous allowlist only had `meeting`, `call`, `webinar`, `interview`, `presentation` which rejected valid filters.
- `wave_list_sessions` docstring now warns that the endpoint only returns completed sessions with summaries (may return far fewer than total account sessions)
- `wave_discover_and_export` defaults to markdown format (consistent with all other tools)
- Batch-level errors in archive export now reported once per batch instead of duplicated per-session
- Sensitive session titles removed from log messages (privacy improvement)
- Silent JSON parse errors in archive metadata now log warnings

### Fixed
- Session type validation now accepts the types Wave actually returns (`recording`, `recovery`, `podcast`)
- Missing rate limit check at start of `wave_export_archive`
- Silent exception swallowing in metadata parsing and index building

## [1.0.0] - 2026-03-17

### Added
- Initial release with 9 tools: list_sessions, get_session, get_transcript, search_sessions, get_stats, bulk_export, get_media, get_account, update_session
- macOS Keychain token storage (no plaintext config files)
- Client-side rate limiting (50 req/min sliding window)
- Markdown injection sanitization
- Session ID validation (prevents path traversal)
- Pydantic input validation with strict constraints
- Rotating file logs (5MB max, 3 backups)
- Dual response format (markdown/JSON) on all tools
