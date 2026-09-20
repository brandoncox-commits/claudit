---
name: troubleshooting
description: >-
  Reference for diagnosing Claude Code issues: skills not triggering, agents
  failing, MCP connection errors, hook problems, permissions, plugins, and common
  config mistakes. Use when something isn't working as expected.
user-invocable: false
---

# Troubleshooting Reference

Diagnostic patterns for Claude Code issues across skills, agents, MCP, hooks, and plugins.

Verified on 2026-09-19 against code.claude.com/docs/en/troubleshooting, /settings and the
pages each row concerns — 4 open claims, each marked **[UNCONFIRMED]** inline. Rows are
starting hypotheses to confirm against the user's actual setup, not verdicts. The update
commands under Diagnostic Commands were verified against /setup on 2026-09-20.

---

## Skills Not Working

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| Skill never auto-triggers | Description too vague | Add specific trigger phrases; test: "when would you use X?" |
| Skill triggers too often | Description too broad | Add negative triggers; narrow scope |
| `/skill-name` not in menu | `user-invocable: false` set | Remove flag if user-invocable is desired |
| Skill changes not applied | Stale plugin asset, or a `--plugin-dir` plugin | `SKILL.md` text in a skills directory hot-reloads automatically. For a skill folder that is also a plugin, changes to `hooks/`, `.mcp.json`, `agents/` or `output-styles/` need `/reload-plugins`, and so does any edit to a plugin loaded with `--plugin-dir` |
| Wrong file name | `skill.md` or `SKILL.MD` | Name it exactly `SKILL.md` (whether other casings load is **[UNCONFIRMED]**) |
| Folder name awkward to invoke | Spaces or capitals | Use kebab-case: `my-skill` not `My Skill` (a convention; the docs state no skill-folder naming rule) |
| Angle brackets render badly in a synced skill | Used `<` or `>` in `description` | Escape or remove them. The docs describe this only for display text on claude.ai-synced skills; no error behaviour for local frontmatter is documented |
| Skill conflicts with another | Same name at different scopes | Higher priority wins: enterprise > user > project. Plugin skills are namespaced and do not collide |
| `$ARGUMENTS` not working | Not in SKILL.md body | Add `$ARGUMENTS` in body or it auto-appends at end |
| Skill can't be preloaded into an agent | It sets `disable-model-invocation: true` | Such skills cannot be preloaded via `skills:` |

---

## Agents Not Working

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| Agent not found | Wrong `subagent_type` value | Must match the `name:` field. Plugin agents use a scoped name such as `my-plugin:agent-name` |
| Agent ignores instructions | Missing context in prompt | Agents have no conversation history; include all context |
| Agent tries to ask the user questions | `AskUserQuestion` is not available to subagents | Return a NEEDS_REVIEW-style signal and let the main session ask |
| Agent spawning other agents | Has Agent tool | Omit `Agent` from `tools`, or add it to `disallowedTools` for workers **[UNCONFIRMED for `disallowedTools`]** |
| Agent output not parsed | No output contract | Define a fixed result block (e.g. YAML) at the top of the response |
| Agent takes too long | No turn limit | Set `maxTurns:` in frontmatter; split complex tasks |
| Wrong model used | Default inheritance | Set `model:` explicitly in frontmatter |
| Agent memory not persisting | `memory:` not set | Add `memory: user` or `memory: project` to frontmatter |
| Local agent ignored in favour of another | Name collision | Project `.claude/agents/` beats user `~/.claude/agents/`, which beats plugin agents |

---

## MCP Not Working

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| Server not appearing | Config syntax error | Validate JSON; run `claude mcp list` |
| `spawn error` | Command not in PATH | Use absolute path or verify binary is installed |
| `connection refused` | Server not running | Start server; check URL/port |
| `authentication failed` | Expired/missing token | Run `claude mcp login server-name` (`claude mcp logout server-name` first to clear a stale token). There is no `claude mcp auth` command |
| Tools not showing | Server starts but no tools | Check server logs; verify tool registration |
| Env var not found | Var not in shell env | Export in shell profile; restart Claude |
| Works locally, fails in CI | Missing env vars in CI | Add secrets to CI environment |
| SSE server not connecting | Deprecated transport | Switch to `http` transport |

---

## Hooks Not Working

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| Hook never fires | Wrong event name (case-sensitive) | Check: `PreToolUse`, `PostToolUse`, `Stop`, etc. |
| Hook fires but doesn't block | Exit code not 2 | Use explicit `exit 2`; `exit 1` doesn't block |
| jq: field not found | Wrong JSON path | Debug: `cat > /tmp/hook.json` to inspect stdin |
| Command not found | Binary not in PATH | Use full path |
| Stderr not reaching Claude | Missing exit 2 | Block with `exit 2` for stderr to become feedback |
| Infinite loop | Hook triggers itself | Use specific matchers; avoid `matcher: ".*"` |
| Hook too slow | Blocking heavy operation | Move to `PostToolUse`, or set `async: true` (or `asyncRewake: true`) on a **command**-type hook — `http`/`mcp_tool` hooks have no async variant and always block |
| Hook in a plugin agent's frontmatter never runs | Not supported for plugin agents | Put it in the plugin's `hooks/hooks.json` instead |

---

## Plugin Not Working

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| `/plugin` command not found | Outdated Claude Code version | Update: `npm install -g @anthropic-ai/claude-code@latest`, or re-run the native installer |
| Plugin not loading | Wrong directory structure | All dirs at root; only `plugin.json` in `.claude-plugin/`. Check the `/plugin` Errors tab |
| Skills not showing after install | Not yet activated | Run `/reload-plugins` or restart |
| Update not arriving | `version` not bumped, or auto-update off | Third-party marketplaces have auto-update off by default — run `/plugin marketplace update` |
| LSP "executable not found" | Binary not installed | Install language server binary |
| MCP not loading from plugin | Wrong path variable | Use `${CLAUDE_PLUGIN_ROOT}` for plugin-relative paths |

---

## Permissions & Auth

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| Tool call denied unexpectedly | Mode too strict, or a rule that never matches | Check `permissions.defaultMode` in settings, or `permissionMode` in agent frontmatter; then check the rule's exact matching form (see the permission-rules reference) |
| Agent's `permissionMode` seems ignored | The parent's mode wins | If the parent session runs in `bypassPermissions`, `acceptEdits` or `auto`, that mode is forced on subagents. A child's `permissionMode` applies when the parent is in `default`, `dontAsk` or `plan`, except `bypassPermissions`, which a child can't give itself (v2.1.267+). Plugin agents cannot set `permissionMode` at all |
| `dontAsk` agent can't ask anything | Working as designed | `dontAsk` denies everything not pre-approved, including `AskUserQuestion` — never run an orchestrator in it |
| Hook blocks valid action | Overly broad matcher | Narrow matcher; add condition checking before `exit 2` |
| OAuth loop on MCP | Stale token | `claude mcp logout server-name` then `claude mcp login server-name` |

---

## Config File Locations

| What | Where |
|------|-------|
| User skills | `~/.claude/skills/` |
| Project skills | `.claude/skills/` |
| User agents | `~/.claude/agents/` |
| Project agents | `.claude/agents/` |
| User settings | `~/.claude/settings.json` |
| Project settings | `.claude/settings.json` |
| Local settings (not shared) | `.claude/settings.local.json` |
| Project MCP | `.mcp.json` (repo root) |
| User/local MCP | `~/.claude.json` |
| CLAUDE.md (user) | `~/.claude/CLAUDE.md` |
| CLAUDE.md (project) | `CLAUDE.md` (repo root) **[UNCONFIRMED]** |
| Installed plugins | `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/` |

---

## Diagnostic Commands

```bash
claude mcp list                 # active MCP servers
claude mcp get server-name      # MCP server details
/reload-plugins                 # reload plugins without restart
claude plugin validate <dir>    # validate a plugin
claude --version                # check version
claude doctor                   # read-only install + settings diagnostics
claude update                   # apply a pending update now

# `claude agents` opens the agent view for background sessions. [UNCONFIRMED]
# It is NOT a listing of agent definitions.
```

**Updating.** Native installs auto-update in the background; `claude update` applies one
immediately. Homebrew, WinGet and the apt/dnf/apk packages do **not** auto-update by default.

On an **npm** install, upgrade with:

```bash
npm install -g @anthropic-ai/claude-code@latest
```

Avoid `npm update -g` — it respects the semver range from the original install and may not
move you to the newest release. Source: code.claude.com/docs/en/setup.

---

## DO / DO NOT

- DO: check the exact file name (`SKILL.md`, not `skill.md`)
- DO: validate JSON config with a linter before debugging further
- DO: use `/reload-plugins` before restarting (faster)
- DO NOT: assume an agent can see skills you loaded in the main session — it gets only the skills it preloads or invokes itself
- DO NOT: debug MCP by guessing — check `claude mcp list` first
- DO NOT: use `exit 1` expecting it to block (only `exit 2` blocks)
