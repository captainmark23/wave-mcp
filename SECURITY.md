# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 3.x     | Yes                |
| ≤ 2.x   | No (custom MCP server retired) |

## Reporting a Vulnerability

If you discover a security vulnerability in wave-archive, please report it responsibly:

1. **Do not** open a public GitHub issue
2. Email the maintainer directly or use GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
3. Include a description of the vulnerability, steps to reproduce, and potential impact
4. Allow reasonable time for a fix before public disclosure

## Security Design

- API tokens are read from the environment (`WAVE_API_KEY`), with an optional
  macOS Keychain fallback; they are never written to disk by the tool
- Session IDs are validated against a strict pattern before use as path
  components to prevent path traversal; unsafe IDs are skipped and reported
- The archive directory is validated so it cannot be a system location
- Audio is only downloaded over HTTPS, with a per-file size cap; partial files
  are removed on failure
- Transcript and summary content is sanitized to prevent markdown injection
  before being written to disk, and written with owner-only (`0600`) permissions
- Error messages and logs redact anything resembling an API token
- The client backs off on rate-limit and server errors to avoid quota exhaustion
