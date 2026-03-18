# wave-mcp

MCP server for [Wave](https://wave.co) — call recording, transcription, and meeting intelligence via Claude.

## Features

- **List sessions** — browse recent meetings with date, duration, type, and platform
- **Get session details** — full metadata including AI summary, notes, and tags
- **Get transcripts** — speaker-attributed transcripts with timestamps
- **Semantic search** — find sessions by topic using natural language
- **Bulk export** — batch-process up to 50 sessions with transcripts and summaries
- **Media URLs** — get signed audio/video download links
- **Account info** — check subscription status and session count
- **Update sessions** — edit titles, notes, tags, and favorites

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- A Wave API token — generate one at [Wave Settings](https://app.wave.co/settings/integrations/api)

## Installation

```bash
git clone https://github.com/captainmark23/wave-mcp.git
cd wave-mcp
```

### Store your API token in macOS Keychain

The server retrieves your API token from the macOS Keychain at startup — no plaintext config files.

```bash
security add-generic-password -a wave-mcp -s wave-api-token -w 'YOUR_API_TOKEN'
```

To update or rotate your token:

```bash
security delete-generic-password -a wave-mcp -s wave-api-token
security add-generic-password -a wave-mcp -s wave-api-token -w 'YOUR_NEW_TOKEN'
```

## Configuration

Add to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "wave-mcp": {
      "command": "/path/to/wave-mcp/launch.sh"
    }
  }
}
```

Restart Claude Desktop after adding the config.

## Tools (8)

| Tool | Description | Read-only |
|---|---|---|
| `wave_list_sessions` | List recent call recordings with pagination | Yes |
| `wave_get_session` | Get full session details including summary and notes | Yes |
| `wave_get_transcript` | Get speaker-attributed transcript with timestamps | Yes |
| `wave_search_sessions` | Semantic search across all sessions | Yes |
| `wave_get_stats` | Aggregated statistics (counts, durations, breakdowns) | Yes |
| `wave_bulk_export` | Export up to 50 sessions at once | Yes |
| `wave_get_media` | Get signed audio/video URLs (expire in ~1 hour) | Yes |
| `wave_get_account` | Account profile, subscription status, session count | Yes |
| `wave_update_session` | Update title, notes, tags, or favorite status | No |

All tools support `response_format` parameter: `'markdown'` (default, human-readable) or `'json'` (structured data).

## Security

- API token stored in macOS Keychain, never in plaintext files
- Session ID validation prevents path traversal (alphanumeric + hyphens/underscores only)
- Client-side rate limiting (50 requests/min) protects your Wave API quota
- Markdown output is sanitized against injection
- Error messages are actionable but never expose raw exceptions or internal details
- Signed media URLs are flagged as sensitive in responses

## Rate Limits

- **Client-side:** 50 requests/minute (configurable)
- **Wave API:** 60 requests/minute, 1,000/day

## Troubleshooting

### Server disconnected in Claude Desktop

Ensure your API token is stored in the Keychain:

```bash
security find-generic-password -a wave-mcp -s wave-api-token -w
```

If it returns an error, add the token (see Installation above).

### Authentication errors

Your token may be expired. Generate a new one at [Wave Settings](https://app.wave.co/settings/integrations/api) and update the Keychain entry.

### uv not found

Install uv: `brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`

## License

MIT
