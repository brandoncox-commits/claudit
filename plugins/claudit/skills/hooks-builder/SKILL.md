---
name: hooks-builder
description: >-
  Reference for creating Claude Code hooks: event types, hook types, stdin
  format, exit codes, blocking vs non-blocking, and common patterns. Use when
  writing, auditing, or debugging hooks in settings.json, agent frontmatter, or
  plugin hooks.json.
user-invocable: false
---

# Hooks Builder Reference

Patterns for creating Claude Code hooks that react to tool calls and session events.

Verified against code.claude.com/docs/en/hooks and /docs/en/hooks-guide on 2026-09-25 —
0 open claims. A few details rest on a runtime probe of `PostToolUse` stdin; each says so
where it appears.

Audit note: the full `/docs/en/hooks` page is long and web fetches of it can truncate
before the per-event subsections. The shorter `/docs/en/hooks-guide.md` states many of the
same facts and fetches whole.

**Hooks are executable config.** A `settings.json` `PreToolUse` entry runs a shell command
on every matching tool call in every session, **including inside every subagent**, and
fires regardless of permission rules. Never propose one without a plain-English statement
of what it will do.

---

## What Hooks Do

Hooks run shell commands, HTTP requests, MCP tool calls, or model evaluations in response
to Claude's actions. They can observe, block, or inject feedback into the workflow.

---

## Hook Events (30+, growing — check the live docs table for the full current list)

| Event | When it fires |
|-------|--------------|
| `PreToolUse` | Before Claude calls any tool. Can block it |
| `PostToolUse` | After a tool call **succeeds** |
| `PostToolUseFailure` | After a tool call **fails** |
| `Notification` | Claude Code sends a notification (not only desktop pop-ups) |
| `Stop` | Claude finishes its response |
| `SubagentStop` | A subagent finishes |
| `PreCompact` | Before context compaction |
| `UserPromptSubmit` | When user submits a message |
| `SubagentStart` | A subagent starts (matcher on agent name, e.g. `^code-reviewer$`) |
| `TeammateIdle` | An agent-team teammate is about to go idle |
| `TaskCreated` / `TaskCompleted` | Shared task-list transitions (agent teams) |

Most common: `PreToolUse` (block dangerous ops), `PostToolUse` (run lint/test after edits).

Others include `Setup`, `SessionStart`, `SessionEnd`, `PermissionRequest`,
`PermissionDenied`, `UserPromptExpansion`, `MessageDisplay`, `PreModelSwitch` /
`PostModelSwitch`, `PostCompact`, `PostToolBatch`, `StopFailure`, `InstructionsLoaded`,
`FileChanged`, `CwdChanged`, `DirectoryAdded`, `ConfigChange`, and the
worktree/elicitation events.

Scoping: hooks in `settings.json` are session-wide and **do see subagents**. Hooks in
agent frontmatter are scoped to that agent and **support all hook events** (a `Stop` hook
there is auto-converted to `SubagentStop`). **Only project-level subagent frontmatter
hooks require the containing folder to be trusted (v2.1.218+)** — hooks from user-level
subagents in `~/.claude/agents/` and from definitions passed via `--agents` run without
that step. **Plugin-shipped agents cannot declare frontmatter `hooks` at all** — but a
plain agent `.md` file copied into `.claude/agents/` can, which is why downloaded agent
files must be checked for this field.

---

## Hook Types

| Type | What it does | Use for |
|------|-------------|---------|
| `command` | Runs a shell command. Communicates via stdout, stderr and exit code | Lint, format, git, notifications, deterministic blocking |
| `http` | POSTs the event data to a URL; communicates via the response body | External webhooks, logging |
| `mcp_tool` | Calls a tool on an already-connected MCP server | Delegating checks to an MCP-connected service |
| `prompt` | **Single-turn LLM evaluation.** Sends your `prompt` text plus the hook input to a Claude model (Haiku by default; override with `model`), which returns a JSON decision | Judgment calls a regex can't make, e.g. a `Stop` hook checking whether all tasks are done |
| `agent` | **Multi-turn verification with tool access.** Experimental and may change | Verification that needs to read files or run checks |

A `prompt` hook has no `command` field and returns only an `ok` / `reason` decision. It
cannot add `additionalContext`; its `reason` reaches Claude only when it blocks on an event
that feeds the reason back (e.g. `Stop`). To add context for Claude, use a `command` hook
whose output carries `hookSpecificOutput.additionalContext` (see below).

Default timeouts: `command`, `http`, `mcp_tool` 10 minutes (lowered to 30 seconds for
`UserPromptSubmit`, `PreModelSwitch`, `PostModelSwitch`, and 10 seconds for
`MessageDisplay`); `prompt` 30 seconds; `agent` 60 seconds. **`SessionEnd` hooks of any
type share a single 1.5-second budget instead**, raised to match a longer per-hook
`timeout` up to 60 seconds. Override per hook with `timeout` (seconds).

Only `command` hooks support asynchronous execution (via `async` / `asyncRewake`) —
`http` and `mcp_tool` hooks have no async variant and always block.

When several hooks match one event, **all of them run to completion** before results are
merged; one hook's `deny` does not stop its siblings' side effects. For `PreToolUse`
decisions the most restrictive answer wins, in the order `deny`, `defer`, `ask`, `allow`.

---

## Exit Codes (command hooks)

| Exit code | Meaning |
|-----------|---------|
| `0` | Allow. **Plain-text stdout goes to the debug log only and Claude never sees it** (JSON output is parsed instead — see "Adding context for Claude") — except for `UserPromptSubmit`, `UserPromptExpansion`, `SessionStart`, and `PostModelSwitch`, where plain-text stdout is added as context Claude can act on. stderr on exit 0 also goes to the debug log only |
| `2` | **Block** the action. The blocking message is the JSON decision's `reason` when present, otherwise stderr |
| Any other | Non-blocking for most events; the action proceeds. For events that use the standard decision model, valid JSON on stdout that passes schema validation still takes effect whatever the code (a JSON `deny` still denies); plain text, empty stdout, or JSON that fails validation is a non-blocking error. Exit 2 is the only code that blocks by code alone, and even a JSON `allow` cannot override it. `exit 1` does **not** block on its own. Exceptions: any non-zero exit from `WorktreeCreate` fails worktree creation whatever the JSON says, and from `WorktreeRemove` fails removal; events that discard hook output (e.g. `StopFailure`) ignore JSON on every code |

Do not build an "informational" exit-0 hook expecting a **plain-text** message to reach
Claude — for every event except `UserPromptSubmit`, `UserPromptExpansion`, `SessionStart`
and `PostModelSwitch`, plain stdout is silently swallowed into the debug log. That covers
plain text only: JSON `additionalContext` is a separate channel (below), and a `PreToolUse`
or `PostToolUse` hook that uses it correctly does reach Claude.

A gate that can't run fails open. A missing or non-executable script (shell exit 127), a crash, or a timeout is a non-blocking error and the tool call goes ahead, so a mistyped path leaves a policy hook silently disabled. After installing a blocking hook, trigger it once on purpose and confirm the block happens.

### Adding context for Claude

For `UserPromptSubmit`, return JSON with `additionalContext` **nested inside
`hookSpecificOutput`** — at the top level it is silently ignored:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "Current branch: release-42. Deploy freeze until Friday."
  }
}
```

`additionalContext` is not limited to those four events. On `PostToolUse`, a hook that
exits 0 and prints `{"hookSpecificOutput": {"hookEventName": "PostToolUse",
"additionalContext": "..."}}` has that text appended to the tool result, where Claude sees
it. The Agent SDK hooks page, which uses the same JSON format as command hooks, says: "For
`PostToolUse` hooks, you can set `additionalContext` to append information to the tool
result" (https://code.claude.com/docs/en/agent-sdk/hooks). On `PreToolUse`, "Text from
`additionalContext` is kept from every hook and passed to Claude together"
(https://code.claude.com/docs/en/hooks-guide).

**Output size cap.** "A hook's `additionalContext`, `systemMessage`, and
`initialUserMessage` strings, and its plain stdout, are capped at 10,000 characters"
(https://code.claude.com/docs/en/hooks). Each string is measured on its own. Over the
limit, Claude Code saves the output to a file in the session directory and passes a
preview of up to the first 2,000 characters plus the path; "this cap has no setting or
environment variable to raise it", and "Claude Code doesn't ask Claude to read the file".
Keep anything Claude must always see under the cap.

`PostToolUse` and `Stop` hooks block with a top-level `decision: "block"`;
`PermissionRequest` uses `hookSpecificOutput.decision.behavior`. Check the reference's
decision-control table for each event.

---

## stdin JSON Format

Every hook receives JSON via stdin. Fields vary by event.

**Common fields**: `session_id`, `prompt_id`, `transcript_path`, `cwd`, `scratchpad_dir`
(v2.1.257+; absent when the session has no scratchpad), `permission_mode` (not every event
receives it; Manual mode arrives as `"default"`), `effort` (tool-use-context events only,
e.g. `PreToolUse`, `PostToolUse`, `Stop`, `SubagentStop`), and `hook_event_name`. Inside a
subagent the input also carries `agent_id` and `agent_type`.

### PreToolUse

```json
{
  "session_id": "abc123",
  "prompt_id": "550e8400-...",
  "transcript_path": "/path/to/transcript.jsonl",
  "cwd": "/path/to/project",
  "permission_mode": "default",
  "hook_event_name": "PreToolUse",
  "tool_name": "Edit",
  "tool_input": {
    "file_path": "/path/to/file.ts",
    "old_string": "...",
    "new_string": "..."
  },
  "tool_use_id": "toolu_01ABC..."
}
```

### PostToolUse

Same common fields as `PreToolUse`, plus `tool_response` carrying the tool's output.
**`tool_response` does NOT appear on `PreToolUse`** — the tool has not run yet.

Confirmed by runtime probe (2026-09-09): the complete top-level key list observed on a real
`PostToolUse` payload, in order:

```
session_id, transcript_path, cwd, prompt_id, permission_mode, effort,
hook_event_name, tool_name, tool_input, tool_response, tool_use_id, duration_ms
```

Observed shape for a `Read` call (values elided):

```json
{
  "hook_event_name": "PostToolUse",
  "tool_name": "Read",
  "tool_input": { "file_path": "…" },
  "tool_response": {
    "type": "text",
    "file": { "filePath": "…", "content": "…", "numLines": 137,
              "startLine": 1, "totalLines": 137 }
  },
  "tool_use_id": "toolu_…",
  "duration_ms": 15
}
```

> `duration_ms` is now documented, though as a `PostToolUse`/`PostToolUseFailure`-specific
> field rather than in the common-fields table: "Optional. Tool execution time in
> milliseconds. Excludes time spent in permission prompts and PreToolUse hooks."

> Some older third-party and example skills call this field `tool_result`. The runtime
> says `tool_response`.

### UserPromptSubmit

```json
{
  "session_id": "abc123",
  "prompt": "user's text here"
}
```

The field is `prompt` (not `message`, not `user_prompt`). `/docs/en/hooks-guide` states:
"`UserPromptSubmit` hooks get the `prompt` text instead."

---

## Config Locations

### settings.json (user or project)

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [
          {
            "type": "command",
            "command": "jq -r '.tool_input.file_path' | xargs -I{} npx eslint --fix -- \"{}\""
          }
        ]
      }
    ]
  }
}
```

### Agent frontmatter (scoped to agent; not available to plugin agents)

```yaml
hooks:
  PostToolUse:
    - matcher: "Edit"
      hooks:
        - type: command
          command: "jq -r '.tool_input.file_path' | xargs -I{} python lint.py \"{}\""
```

### Skill frontmatter (registered for the rest of the session once the skill is invoked)

```yaml
---
name: secure-operations
description: Perform operations with security checks
hooks:
  PreToolUse:
    - matcher: "Bash"
      hooks:
        - type: command
          command: "./scripts/security-check.sh"
---
```

Unlike agent-frontmatter hooks, which are removed when that subagent finishes, skill
hooks stay registered for the rest of the session once invoked — including turns after
the skill's own turn. Add `once: true` on a hook to remove it after its first successful
run.

### Plugin `hooks/hooks.json` (in the plugin root, or inline in `plugin.json`)

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "jq -r '.tool_input.command' | grep -qE 'rm -rf' && exit 2 || exit 0"
          }
        ]
      }
    ]
  }
}
```

To disable all hooks, set `"disableAllHooks": true`. Hooks in managed settings still run
unless it is also set there.

---

## Matcher Patterns

```json
"matcher": "Edit"            // Exact tool name
"matcher": "Edit|Write"      // OR of tool names
"matcher": ".*"              // All tools (use sparingly)
"matcher": "Bash"            // Only Bash commands
```

Without a matcher, a hook fires on every occurrence of its event.

---

## Common Hook Patterns

### Auto-lint after file edit

```json
{
  "PostToolUse": [{
    "matcher": "Edit|Write",
    "hooks": [{
      "type": "command",
      "command": "jq -r '.tool_input.file_path' | xargs -I{} eslint --fix -- \"{}\" 2>/dev/null || true"
    }]
  }]
}
```

### Block dangerous Bash commands

```json
{
  "PreToolUse": [{
    "matcher": "Bash",
    "hooks": [{
      "type": "command",
      "command": "jq -r '.tool_input.command' | grep -qE '(rm -rf|DROP TABLE|git push( .*)? (--force|-f)( |$))' && { echo 'Blocked: dangerous command' >&2; exit 2; } || exit 0"
    }]
  }]
}
```

### Notify on stop

```json
{
  "Stop": [{
    "hooks": [{
      "type": "command",
      "command": "notify-send 'Claude' 'Task complete'"
    }]
  }]
}
```

### Check completeness before stopping (prompt hook)

```json
{
  "Stop": [{
    "hooks": [{
      "type": "prompt",
      "prompt": "Check if all tasks are complete. If not, respond with {\"ok\": false, \"reason\": \"what remains to be done\"}."
    }]
  }]
}
```

### Inject dynamic context on prompt submit (command hook)

```json
{
  "UserPromptSubmit": [{
    "hooks": [{
      "type": "command",
      "command": "echo \"Current git branch: $(git branch --show-current)\""
    }]
  }]
}
```

Plain-text stdout on exit 0 is added as context for `UserPromptSubmit`; for structured
output use `hookSpecificOutput.additionalContext`.

### HTTP webhook

```json
{
  "PostToolUse": [{
    "matcher": "Write",
    "hooks": [{
      "type": "http",
      "url": "https://my-logger.example.com/events",
      "headers": { "Authorization": "Bearer ${LOGGER_TOKEN}" },
      "allowedEnvVars": ["LOGGER_TOKEN"]
    }]
  }]
}
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Hook never fires | Wrong event name | Check spelling and match the docs' casing exactly (e.g. `PreToolUse`) |
| Hook fires but doesn't block | Exit code not 2 | Use `exit 2` explicitly; `exit 1` doesn't block |
| jq: field not found | Wrong JSON path | Print stdin first: `cat > /tmp/hook-debug.json` |
| Command not found | Binary not in PATH | Use a full path |
| Hook runs but stderr missing | Claude doesn't see it | Write to stderr: `echo "msg" >&2` with exit 2 |
| `additionalContext` ignored | Placed at top level of the JSON | Nest it inside `hookSpecificOutput` |
| `prompt` hook has no effect as context | `prompt` hooks return a decision, they don't inject text | Use a `command` hook for context |
| Infinite loop | Hook triggers itself | Use specific matchers; avoid `matcher: ".*"` |

---

## DO / DO NOT

- DO: read the tool name from `jq -r '.tool_name'` before acting
- DO: use `exit 2` + stderr to give Claude actionable feedback when blocking
- DO: use specific matchers (`Edit|Write`) not `".*"`
- DO: test hooks with `cat > /tmp/hook.json` to inspect stdin
- DO NOT: use blocking hooks for slow operations (they delay Claude)
- DO NOT: suppress all errors with `|| true` in blocking hooks
- DO NOT: run hooks that modify files Claude is currently editing (race condition)
- DO NOT: rely on one hook's `deny` to suppress a sibling hook's side effects
