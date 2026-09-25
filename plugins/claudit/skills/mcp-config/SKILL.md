---
name: mcp-config
description: >-
  Reference for configuring MCP (Model Context Protocol) servers in Claude Code:
  transport types, scopes, auth, installation, and troubleshooting. Use when
  adding, configuring, auditing, or debugging an MCP server connection.
user-invocable: false
---

# MCP Config Reference

Patterns and standards for wiring MCP servers into Claude Code.

Verified against code.claude.com/docs/en/mcp on 2026-09-25 — 1 open claim, marked
**[UNCONFIRMED]** inline.

Two easy mistakes this file guards against: the WebSocket transport `type` is `ws`, not
`websocket`; and there is no `claude mcp auth` command or `--auth` flag — it is
`claude mcp login` / `claude mcp logout` (v2.1.186+). **[UNCONFIRMED]** The second is a
negative claim: `mcp.md` documents `claude mcp login`/`logout` but is not the CLI command
reference, so it does not settle whether `claude mcp auth` exists.

---

## What MCP Is

MCP (Model Context Protocol) lets Claude call external tools, read resources, and receive prompts from servers you control. Servers expose capabilities; Claude decides when to use them.

---

## Scope: Where Config Lives

| Scope | File | Who sees it |
|-------|------|-------------|
| **Local** | `~/.claude.json` (under that project's path; the default scope) | Only you, this project |
| **Project** | `.mcp.json` (at repo root) | All collaborators (commit this) |
| **User** | `~/.claude.json` | You, all projects |

Use project scope for shared tools; user scope for personal API keys or private servers.

---

## Transport Types

| Transport | When to use | Notes |
|-----------|-------------|-------|
| `stdio` | Local process (Python, Node, binary) | Most common; process managed by Claude |
| `http` | Remote server with stable URL | Recommended option for connecting to remote MCP servers; the most widely supported transport for cloud-based services. In JSON config, `type` also accepts `streamable-http` as an alias for `http` (the MCP spec's own name for this transport) |
| `sse` | Legacy streaming | Deprecated; use `http` instead |
| `ws` | Remote server needing a persistent bidirectional connection | The literal `type` value is **`ws`** — writing `websocket` will not work. Accepts the same `url`, `headers`, `headersHelper`, `timeout` and `alwaysLoad` fields as `http` |

---

## Config Format

### stdio (local process)

```json
{
  "mcpServers": {
    "my-tool": {
      "type": "stdio",
      "command": "python",
      "args": ["/path/to/server.py"],
      "env": {
        "API_KEY": "${MY_API_KEY}"
      }
    }
  }
}
```

### http (remote server)

```json
{
  "mcpServers": {
    "my-api": {
      "type": "http",
      "url": "https://my-mcp-server.example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${MY_TOKEN}"
      }
    }
  }
}
```

### Node package (npx)

```json
{
  "mcpServers": {
    "filesystem": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/allowed/path"]
    }
  }
}
```

### uvx (Python package)

```json
{
  "mcpServers": {
    "memory": {
      "type": "stdio",
      "command": "uvx",
      "args": ["mcp-server-memory"]
    }
  }
}
```

---

## CLI Commands

```bash
# Add a server
claude mcp add

# Add from a URL
claude mcp add --transport http my-server https://example.com/mcp

# Add stdio server
claude mcp add my-tool -- python /path/to/server.py

# List configured servers
claude mcp list

# Show server details
claude mcp get my-tool

# Remove a server
claude mcp remove my-tool

# Scope flag (default: local)
claude mcp add --scope user my-tool -- npx my-mcp-package
claude mcp add --scope project my-tool -- npx my-mcp-package
```

---

## OAuth Auth

For HTTP servers requiring OAuth:

```bash
# Add the server (there is NO --auth flag; authenticate as a separate step)
claude mcp add --transport http my-server https://example.com/mcp

# Authenticate / re-authenticate
claude mcp login my-server     # v2.1.186+, runs the OAuth flow from your shell
# or, from inside a session: /mcp

# Clear stored credentials
claude mcp logout my-server
```

Claude Code runs the OAuth flow; `claude mcp logout` clears the stored credentials.

---

## Project Server Approval

Servers in a project's `.mcp.json` need approval before they connect in interactive
sessions; in `claude -p`, Agent SDK and cloud sessions Claude Code can't show the prompt
and loads project-scoped servers without asking. The settings keys
`enabledMcpjsonServers` and `disabledMcpjsonServers` "control approval of servers defined
in a project's `.mcp.json` file"; `enableAllProjectMcpServers` approves them all. "A
cloned repository can't approve its own servers": `enableAllProjectMcpServers` or
`enabledMcpjsonServers` committed to the project's `.claude/settings.json` is ignored
until the folder is trusted. They are unrelated to `enabledMcpServers` /
`disabledMcpServers`.

## Output Limits

"Claude Code displays a warning when MCP tool output exceeds 10,000 tokens and limits
output to 25,000 tokens by default. To raise the limit, set the `MAX_MCP_OUTPUT_TOKENS`
environment variable ... the warning threshold is fixed." A tool that declares
`anthropic/maxResultSizeChars` uses that for text content instead; image output is always
subject to `MAX_MCP_OUTPUT_TOKENS`.

---

## Plugin-Bundled MCP

In this file `<dollar>` stands for a literal `$`, written that way so this skill's own text is not rewritten when it loads. When you write config for a user, type a real `$`; copying `<dollar>` verbatim produces a server that fails to start.

Bundle MCP config with a plugin in `.mcp.json` at plugin root:

```json
{
  "mcpServers": {
    "my-server": {
      "command": "<dollar>{CLAUDE_PLUGIN_ROOT}/servers/my-server",
      "args": ["--config", "<dollar>{CLAUDE_PLUGIN_ROOT}/config.json"],
      "env": {
        "DB_URL": "${DB_URL}"
      }
    }
  }
}
```

Or inline in `plugin.json`:

```json
{
  "name": "my-plugin",
  "mcpServers": {
    "my-server": {
      "command": "<dollar>{CLAUDE_PLUGIN_ROOT}/servers/my-server"
    }
  }
}
```

Use `<dollar>{CLAUDE_PLUGIN_ROOT}` for paths relative to plugin install dir.

---

## Environment Variable Patterns

| Pattern | Use |
|---------|-----|
| `"${VAR_NAME}"`, `"${VAR_NAME:-default}"` | Interpolated from shell environment variables. `${VAR:-default}` expands to `VAR` if set, otherwise `default`. The docs specify only WHERE expansion applies — `command`, `args`, `env`, `url`, `headers` — never WHEN. Do not assert "at startup". |
| Hardcoded string | Fine for non-secret values |
| Store secrets | In shell profile, not in config files |

Never hardcode API keys in `.mcp.json` if committing to git.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Server not appearing in Claude | Config syntax error | Validate JSON; check `claude mcp list` |
| `spawn error` / process not found | Command not in PATH | Use absolute path or install globally |
| `connection refused` | HTTP server not running | Start server; check URL and port |
| `authentication failed` | Missing/expired token | Run `claude mcp login server-name` |
| Tools not showing | Server started but no tools | Check server logs; verify tool registration |
| `env var not found` | Var not in shell env | Export var in shell profile; restart Claude |
| Works locally, not in CI | Env var missing in CI | Add secrets to CI environment |

---

## DO / DO NOT

- DO: use `${VAR}` for secrets in config files
- DO: commit `.mcp.json` for project-shared servers
- DO: use `stdio` for local tools, `http` for remote APIs
- DO NOT: hardcode API keys in committed config
- DO NOT: use `sse` transport (deprecated; use `http`)
- DO NOT: put per-user secrets in project-scoped `.mcp.json`
