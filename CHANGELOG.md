# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).
The MCP tool set (names, parameters, results) and the hook's REST endpoint are the public API.

## [Unreleased]

### Planned

- `agentbus init`: generate `.mcp.json`, the hook settings, the hook file and the protocol templates in a project.

## [0.1.0] - 2026-10-16

First public release.

### Added

- MCP server (streamable HTTP, stateless) with tools for the roster and status, messages, the task board with
  dependencies, path claims with conflict detection, and a team overview (`board`).
- REST API (`/api/health`, `/api/call/<operation>`, `/api/hook`) for the hook, the CLI and the monitor.
- Claude Code hook (`SessionStart`, `UserPromptSubmit`, `PostToolUse`) that delivers new messages into the session's
  context; a single standard-library file that can be copied into a project.
- `agentbus` command: `serve`, `watch` (live monitor), `hook`, `health`, `waitmail` and every bus operation.
- Background start/stop scripts for PowerShell and POSIX shells; the server records its PID next to its database.
- SQLite storage with a schema version and migrations; an online backup is taken before migrating.
- Configuration through command-line flags and `AGENTBUS_*` environment variables (flag > environment > default).
- Templates: agent protocol, kickoff message, `.mcp.json`, `.claude/settings.json`, a `CLAUDE.md` paragraph.

### Security

- The server binds to loopback addresses only.
- The REST API rejects browser requests (an `Origin` header, a non-JSON content type, a non-loopback `Host`), so a
  web page cannot inject messages into agents' contexts or erase data.
