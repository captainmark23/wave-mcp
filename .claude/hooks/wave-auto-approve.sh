#!/bin/bash
# Wave MCP auto-approval hook
# Auto-approves read-only tools, requires manual approval for write operations.

INPUT=$(cat)
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty')

# Auto-approve read-only Wave tools
if echo "$TOOL_NAME" | grep -qE "wave_(get_|list_|search_|bulk_export)"; then
  echo '{"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}'
  exit 0
fi

# Require manual approval for write/filesystem operations
if echo "$TOOL_NAME" | grep -qE "wave_(update|create|delete|download|export)"; then
  echo '{"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "ask"}}}'
  exit 0
fi

# Default: allow any other wave_ tools
echo '{"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}'
exit 0
