# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 2.x     | Yes                |
| 1.x     | No                 |

## Reporting a Vulnerability

If you discover a security vulnerability in wave-mcp, please report it responsibly:

1. **Do not** open a public GitHub issue
2. Email the maintainer directly or use GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
3. Include a description of the vulnerability, steps to reproduce, and potential impact
4. Allow reasonable time for a fix before public disclosure

## Security Design

- API tokens are stored in the macOS Keychain, never in config files or environment variables at rest
- Session IDs are validated against a strict alphanumeric pattern to prevent path traversal
- File download paths are validated to block writes to system directories
- Markdown output is sanitized to prevent injection
- Error messages never expose raw exception details or internal state
- Client-side rate limiting protects against accidental API quota exhaustion
- Session titles and user content are not written to log files
