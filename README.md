# wave-archive

Local backup tooling for [Wave](https://wave.co) — call recording, transcription, and meeting intelligence.

> **This repo used to ship a custom MCP server for Wave.** Wave now runs an
> **official hosted MCP server** and a versioned REST API, so the custom server
> has been retired. For using your recordings inside Claude/ChatGPT/Cursor,
> **enable the official MCP** (below). This repo now keeps only the one thing the
> hosted MCP doesn't do: writing a complete **offline archive** of your sessions
> to disk. See [CHANGELOG.md](CHANGELOG.md) for the migration.

---

## For teams: enable the official Wave MCP server

The official hosted MCP server (`https://mcp.wave.co`) is the recommended way for
your organisation to bring Wave recordings into LLM clients. It beats a
self-hosted server on every axis that matters for a team:

- **Per-user OAuth** — each teammate signs in with their own Wave account and sees
  only their own recordings. No shared API token to distribute, store, or rotate.
- **Cross-platform** — works in Claude (web, desktop, mobile), Claude Code,
  ChatGPT (Developer Mode / Apps), Cursor, Windsurf, Zed, and Zapier.
- **Zero maintenance** — Wave operates it and it tracks new API features
  automatically.
- **Read-only, scoped** to the authenticated user.

### Requirements
- A Wave plan that includes API access: **Plus, Pro, Team, Enterprise, or Edu**.
- Each user enables **Developer Mode** in the Wave app: **Settings → Advanced**.

### Enable it in a client
Add `https://mcp.wave.co` as an MCP connector using **OAuth**:

- **Claude (desktop/web):** Settings → Connectors → **Add custom connector** →
  paste `https://mcp.wave.co` → choose **OAuth** → sign in.
- **Claude Code:** add the connector, then run `/mcp` and complete the OAuth flow
  in your browser.
- **ChatGPT / Cursor / Windsurf / Zed:** add a custom MCP/connector pointing at
  `https://mcp.wave.co` and authenticate via OAuth.

Once connected, your recordings, transcripts, summaries, folders, and semantic
search are available to the assistant as tools — no token wrangling.

### Rolling it out to the org
1. Confirm the workspace/team is on an API-enabled plan.
2. Ask each member to enable Developer Mode and add the connector (steps above).
3. Because auth is per-user OAuth, there's nothing central to provision — access
   is automatically scoped to what each person can already see in Wave.

Docs: <https://api.wave.co/mcp> · <https://wave.co/agents>

---

## When you need more than the MCP: the REST API and this CLI

The hosted MCP is read-only and ambient — great for "ask about my meetings," but
it does not write files, run on a schedule, or write back metadata. For those,
use the **official REST API** directly:

| Need | Use |
|---|---|
| Recordings ambient in an LLM client | Official MCP (`mcp.wave.co`) |
| Server-side integration, cron, webhooks | REST API (`api.wave.co`) |
| Write back title/notes/tags/favorite | REST API `PATCH /v1/sessions/{id}` |
| React to new recordings | REST API webhooks (`session.completed`) |
| **Full offline backup of your sessions** | **`wave-archive` (this repo)** |

Full REST reference: <https://api.wave.co/reference> · OpenAPI at
<https://api.wave.co/v1/openapi.json>. Wave also publishes an official CLI
(`npm i -g @waveai/cli`).

---

## `wave-archive` — local backup CLI

`wave-archive` walks your Wave sessions via the official REST API and writes an
organized, **incremental** archive to a local folder: metadata, AI summaries,
speaker-attributed transcripts, and (optionally) audio.

### Requirements
- Python 3.11+
- A Wave API token from
  [Wave Settings → Integrations → API](https://app.wave.co/settings/integrations/api).
  Tokens start with `wave_api_`. Grant the scopes you need:
  - `sessions:read` and `transcripts:read` — required
  - `media:read` — only if you use `--include-audio`

### Install
```bash
git clone https://github.com/captainmark23/wave-mcp.git
cd wave-mcp
uv sync            # or: pip install -e .
```

### Authenticate
The token is read from the `WAVE_API_KEY` environment variable (cross-platform):

```bash
export WAVE_API_KEY='wave_api_...'
```

(For backwards compatibility it also accepts `WAVE_API_TOKEN`, and on macOS it
falls back to the legacy Keychain entry `wave-mcp / wave-api-token` if present.)

### Use
```bash
# Back up everything to ~/Documents/Wave (safe to re-run; only fetches new sessions)
wave-archive --output-dir ~/Documents/Wave

# Only sessions in your "work" folder, with audio
wave-archive -o ~/Documents/Wave --folder work --include-audio

# Only sessions after a date, print a JSON summary
wave-archive -o ~/Documents/Wave --since 2025-01-01 --json
```

Run `wave-archive --help` for all options. If not installed as a script, run it
directly: `python wave_archive.py --output-dir ...`.

### Output layout
```
Wave/
  20250418_Weekly-product-sync_XF2KJM/
    metadata.json
    summary.md
    transcript.md
    audio.m4a          # only with --include-audio
  20250419_Team-standup_A9QW3Z/
    ...
  index.json           # run summary: counts, errors, timestamp
```

The archive is incremental — a session whose folder already contains a
`metadata.json` is skipped, so re-running only fetches what's new. Ideal as a
cron job.

### Scoping work vs. personal (folders)
If one Wave account mixes work and personal recordings, organise the ones your
backup should include into a **folder** in the Wave app, then pass
`--folder work`. Only sessions in that folder are archived — a clean,
auditable default-deny boundary with no server-side tagging required.

### Automate it
```bash
# Nightly backup (crontab)
0 2 * * *  WAVE_API_KEY=wave_api_... /path/to/.venv/bin/wave-archive -o /backups/wave >> /var/log/wave-archive.log 2>&1
```
For event-driven backups, register a `session.completed` webhook on the REST API
and trigger `wave-archive` from your handler.

## Security
- Token read from the environment (or macOS Keychain), never written to disk.
- Session IDs validated against a strict pattern before use as path components
  (prevents path traversal); unsafe IDs are skipped and reported.
- Archive directory is validated so it cannot be a system location.
- Audio is only downloaded over HTTPS, with a 2 GB per-file cap; partial files
  are cleaned up on failure.
- Transcript and summary content is sanitized against markdown injection before
  being written to disk.
- Summary/transcript/metadata files are written with owner-only permissions
  (`0600`) where the OS supports it.
- API errors and logs redact anything resembling a token.

## Rate limits
The REST API allows 60 requests/minute and 10,000/day per token. The CLI backs
off automatically on `429`/`5xx` (honouring `Retry-After`).

## Development
```bash
uv run pytest              # tests
uv run ruff check .        # lint
```

## License
MIT
