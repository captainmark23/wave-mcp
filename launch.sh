#!/bin/bash
# Wave MCP launcher — retrieves API token from macOS Keychain instead of plaintext config.
export WAVE_API_TOKEN="$(security find-generic-password -a "wave-mcp" -s "wave-api-token" -w 2>/dev/null)"

if [ -z "$WAVE_API_TOKEN" ]; then
  echo "ERROR: Wave API token not found in Keychain." >&2
  echo "Add it with: security add-generic-password -a wave-mcp -s wave-api-token -w YOUR_TOKEN" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec uv run --directory "$SCRIPT_DIR" python wave_server.py
