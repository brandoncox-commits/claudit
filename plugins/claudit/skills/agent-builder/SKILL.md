---
name: agent-builder
description: >-
  Reference for creating Claude Code subagents: frontmatter fields, permission
  mode inheritance, tool boundaries, output contracts, model selection, memory,
  nesting, and hooks. Use when writing, auditing, or designing an agent .md file
  or an agent system.
user-invocable: false
---

# Agent Builder Reference

Patterns and standards for creating Claude Code subagents.

Verified on 2026-09-19 against code.claude.com/docs/en/sub-agents, /permission-modes,
/plugins-reference and /hooks — 4 open claims, each marked **[UNCONFIRMED]** inline. A few
claims rest on runtime probes rather than the docs; each says so where it appears.

**Two kinds of guidance live here — keep them apart when auditing:**
- **Documented behaviour** — what Claude Code actually does. Getting this wrong breaks
  things. Report violations as errors.
- **Convention** (marked *Convention*) — widely useful practice that Claude Code does not
  require. Report gaps as optional suggestions, never as errors.

---

## What an Agent Is

A specialized subprocess spawned by the `Agent` tool, running in its own context window.

- Agents receive a prompt, do the work, and return a result
- Agents **cannot ask the user anything** — `AskUserQuestion` is stripped from every
  subagent. Have them return a "needs review" signal and let the main session ask
- Agents CAN spawn subagents (see Nesting) — but usually shouldn't

---

## Permission Mode Inheritance (read this first)

The most commonly missed rule. A subagent's effective mode is decided in this order —
the parent can **override the child's frontmatter entirely**:

1. Parent in `bypassPermissions` → child **forced** to `bypassPermissions`
2. Parent in `acceptEdits` → child **forced** to `acceptEdits`
3. Parent in `auto` → child inherits `auto`; the child's `permissionMode` field is **ignored**
4. Otherwise (parent in `default`, `dontAsk` or `plan`) → the child's own `permissionMode`
   applies — **except `bypassPermissions`**: a child that declares it keeps the parent's
   mode instead (v2.1.267+)
5. Otherwise → child inherits the parent's mode

**Consequence:** a child's `permissionMode` only takes effect when the parent session is
in `default`, `dontAsk` or `plan`, and a child can never give itself `bypassPermissions`
(v2.1.267+). Designs that pin specialists to specific modes must keep the orchestrator out
of `bypassPermissions`, `acceptEdits` and `auto`. Plugin-shipped agents cannot set
`permissionMode` at all.

### Mode reference

| Mode | Behaviour |
|---|---|
| `default` (alias `manual`) | Asks before most actions **[UNCONFIRMED: whether an approval is remembered per tool]** |
| `acceptEdits` | Auto-accepts file edits + common fs commands (`mkdir`, `touch`, `mv`, `cp`) in the working dir / `additionalDirectories` |
| `plan` | Read-only exploration |
| `auto` | Classifier auto-approves against the stated request |
| `dontAsk` | Never prompts; **auto-DENIES** anything not in `permissions.allow` |
| `bypassPermissions` | Skips prompts, including writes to `.git` and `.claude` |

**`dontAsk` denies `AskUserQuestion` even when explicitly allowed**, along with MCP tools
marked `requiresUserInteraction`. An orchestrator in `dontAsk` cannot talk to the user.

A subagent can never give itself `bypassPermissions`, whatever the settings: when the
parent is in `default`, `dontAsk` or `plan`, a child that declares it keeps the parent's
mode instead (v2.1.267+). Separately, `permissions.disableBypassPermissionsMode: "disable"`
(a string, not `true`) prevents `bypassPermissions` mode from being used at all. It works
from any settings scope and is most useful in managed settings, where it can't be
overridden.
`permissions.disableAutoMode` (write the nested form; set to `"disable"`) removes `auto`
from the permission-mode cycle, forces the start mode to `default`, and stops
`defaultMode: "auto"` taking effect.

Subagents inherit the parent's **permission rules** **[UNCONFIRMED beyond auto mode — the
docs state it explicitly only for auto mode: "the classifier evaluates the subagent's tool
calls with the same block and allow rules as the parent session"]**.
Block specific agents with `permissions.deny: ["Agent(agent-name)"]`, or all spawning
with `["Agent"]`.

---

## File Location & Scope Priority

```
.claude/agents/my-agent.md        ← project-scoped
~/.claude/agents/my-agent.md      ← user-scoped (all projects)
```

Highest to lowest: **managed settings → `--agents` CLI JSON → project → user → plugin**
(plugin agents are listed as `my-plugin:agent-name`; a same-named project or user agent
overrides a plugin agent).

---

## Full Frontmatter Reference

Only `name` and `description` are required.

| Field | Notes |
|-------|-------|
| `name` | Required. kebab-case, no colons. Becomes `agent_type` in hooks. |
| `description` | Required. How Claude decides to delegate. Combined descriptions over 15,000 tokens trigger a warning. |
| `tools` | Allowlist. Inherits all subagent-available tools if omitted. Supports `Agent(type1, type2)` to restrict spawnable subagents (main-session agents only — see Nesting). |
| `disallowedTools` | Denylist: "removed from inherited or specified list" (ordering relative to `tools` **[UNCONFIRMED]**). Supports `mcp__<server>` patterns. |
| `model` | `inherit` / `haiku` / `sonnet` / `opus` / `fable` / full ID (e.g. `claude-opus-5`). Omit to follow precedence. |
| `permissionMode` | `default` / `manual` / `acceptEdits` / `auto` / `dontAsk` / `bypassPermissions` / `plan`. Subject to inheritance rules above. Not available to plugin agents. |
| `maxTurns` | Max agentic turns. Output marked partial; resumable via `SendMessage`. |
| `skills` | Skill names preloaded in full at startup. Agents do NOT inherit the parent's skills. Cannot preload `disable-model-invocation: true` skills. |
| `mcpServers` | MCP servers scoped to this agent. Names or inline definitions. Not available to plugin agents. |
| `hooks` | Lifecycle hooks scoped to this agent. **All hook events are supported** (a `Stop` hook here is auto-converted to `SubagentStop`). Requires folder trust (v2.1.218+). Not available to plugin agents. |
| `memory` | `user` / `project` / `local`. Enables cross-session learning. |
| `background` | `true` keeps the agent in background even when foreground is requested. |
| `effort` | `low` / `medium` / `high` / `xhigh` / `max`. |
| `isolation` | `worktree` runs in an isolated git worktree copy. |
| `color` | Optional. `red` / `blue` / `green` / `yellow` / `purple` / `orange` / `pink` / `cyan`. There is no `magenta`. |
| `initialPrompt` | Auto-submitted first turn when run as main session (`--agent`). |
| `experimental` | e.g. `cacheTtl: 5m` or `1h` for prompt cache lifetime. |

**Plugin-shipped agents cannot use `hooks`, `mcpServers`, or `permissionMode`** — "For
security reasons, `hooks`, `mcpServers`, and `permissionMode` are not supported for
plugin-shipped agents". A plain agent `.md` copied into an
`agents/` directory **does** honour all three. Treat downloaded agent files accordingly: a
hostile one can register a shell-executing `PreToolUse` hook, add an MCP server, or declare
`bypassPermissions` (ignored from v2.1.267, honoured on older versions).

---

## Tools Stripped From All Subagents

Removed even if listed in `tools`:

`AskUserQuestion`, `EndConversation`, `EnterPlanMode`, `ExitPlanMode` (unless
`permissionMode: plan`), `ScheduleWakeup`, `TaskOutput`, `WaitForMcpServers`, `Workflow`,
and `Agent` once at the depth limit.

Because `TaskOutput` is stripped, a subagent acting as an orchestrator cannot collect
background results that way **[UNCONFIRMED — an inference; the docs list the stripping,
not this consequence]**. Spawn children in the foreground, or orchestrate from the
main session.

### Background vs foreground tool pools

**Background** subagents keep only: `Read`, `Grep`, `Glob`, `Bash`, `PowerShell`, `Edit`,
`Write`, `NotebookEdit`, `WebFetch`, `WebSearch`, `TodoWrite`, `Skill`, `ToolSearch`,
`EnterWorktree`, `ExitWorktree`, `Monitor`, `TaskStop`, `SendMessage`, `Artifact`, and
`SubagentHandback` (for a subagent that reports through it), plus all MCP tools. **Foreground** subagents get whatever the `tools` field specifies (or every
tool available to subagents, if omitted) after the first filter. Only a **fork** receives
the main conversation's exact tool pool.

---

## Nesting & Concurrency

- Subagents can spawn subagents **3 layers deep by default**.
- `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH`: `1` disables nesting, `3` is the default.
- An agent at nesting level `d` below the main session (main = 0) receives the `Agent`
  tool only if `d < MAX_DEPTH`. At the limit the tool is **silently stripped** — there is
  no "depth exceeded" error. *(Boundary behaviour confirmed by runtime probe on Claude
  Code 2.1.266.)*
  - Probe trap: an agent whose *only* tool is `Agent` is left with zero tools at the
    boundary, and the runtime refuses to spawn a zero-tool agent — so the failure appears
    one hop earlier than the real limit. Give probe agents a second, harmless tool.
- **Max 20 concurrent subagents**; `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS` to change.
  Spawning past the limit **fails immediately** with `Concurrent subagent limit reached`
  and does not queue. Spawning succeeds again once the running count drops below the
  limit.

The `Agent(worker, researcher)` parenthesised allowlist **only applies to a main-session
agent** (`claude --agent` or the `agent` setting). Inside a subagent definition the type
list is ignored — to stop a subagent spawning others, omit `Agent` from `tools`.

---

## Main-Session Agent vs Subagent

Run with `claude --agent <name>`, or the `agent` key in settings.json.

| Aspect | Main session | Subagent |
|---|---|---|
| System prompt | Body **replaces** the Claude Code system prompt | Body + appended environment details |
| Tools | Full session tools | Restricted per stripping rules |
| Context | All prior messages, memory, CLAUDE.md | Fresh: delegation message, full CLAUDE.md hierarchy (including any loaded `AGENTS.md` files), git status snapshot, preloaded skills, sibling roster — but **no prior conversation history** |
| `AskUserQuestion` | **Available** | Stripped |
| `Agent(...)` allowlist | **Honored** | Ignored |
| `initialPrompt` | Auto-submitted | N/A |

Orchestrators belong in the main session: it is the only place that keeps
`AskUserQuestion` and honors the spawn allowlist.

---

## Model Selection

*Convention* — a sensible default mapping, not a requirement:

| Model | Use for |
|---|---|
| `haiku` | Search, read-only, pattern matching, deterministic test execution |
| `sonnet` | Writing code, debugging, documentation |
| `opus` | Complex architecture and judgement |
| `inherit` | Same model as parent (the default when `model` is omitted) |

Precedence (documented): per-invocation `model` on the Agent call → definition's `model`
→ `CLAUDE_CODE_SUBAGENT_MODEL` → parent's model.

Force one model on everything:

```json
{ "env": { "CLAUDE_CODE_SUBAGENT_MODEL": "haiku", "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1" } }
```

With force on, definitions' `model` is ignored; forks and `model: inherit` skills still
run the parent's model (v2.1.257+). This is session-wide. Built-in **Explore** inherits
the parent model, capped at Opus on the Claude API; **Plan** inherits the parent model.

---

## Description Format

The `description` is how Claude decides when to delegate. Say what the agent does, when
to use it, and when NOT to.

*Convention:* adding 2–4 `<example>` blocks with different phrasings noticeably improves
delegation accuracy:

```
Use this agent when [conditions]. Examples:

<example>
Context: [Situation]
user: "[Request]"
assistant: "[How Claude responds and uses this agent]"
<commentary>
[Why this agent fits here]
</commentary>
</example>
```

---

## Tool Boundaries

Grant the minimum tools the job needs.

| Agent type | Tools | Why |
|---|---|---|
| Search | `Glob, Grep` | Read-only |
| Analysis / review | `Read, Glob, Grep` | Never modifies |
| Writer | `Read, Write, Edit, Glob, Grep` | No shell |
| Runner | `Bash` (+ `Read`) | Executes only |

---

## Persistent Memory

```yaml
memory: user      # ~/.claude/agent-memory/<agent-name>/
memory: project   # .claude/agent-memory/<agent-name>/    (shareable via git)
memory: local     # .claude/agent-memory-local/<agent-name>/
```

Enables `Read`/`Write`/`Edit` automatically and auto-loads the first 200 lines **or 25KB**
of MEMORY.md, whichever comes first. Deleting an agent file does not delete its memory
folder *(observed, not documented)* — clean both together.

---

## What Loads in Subagent Context

Loaded: own system prompt, delegation message, CLAUDE.md hierarchy including any loaded
`AGENTS.md` files (except Explore/Plan), git status snapshot, preloaded `skills`, sibling
agent roster (if `SendMessage` in tools).

Not loaded: conversation history, parent's output style, parent's auto memory, parent's
context window size.

A **fork** inherits the entire parent session state.

---

## Output Contract

*Convention* — Claude Code does not require one, but a fixed, parseable result block makes
agent output reliable to act on. A common shape:

```yaml
---
status: SUCCESS | NEEDS_REVIEW | ERROR
agent: agent-name
files_created: []
files_modified: []
signals: []            # e.g. NEEDS_REVIEW
error:
  message: ""
  recoverable: true
permission_denials:
  - tool: "Bash"
    detail: "command not in allowlist"
---
```

Reporting `permission_denials` explicitly matters most under `dontAsk`: a missing
allowlist entry is a silent denial, and an agent that quietly works around it hides a
config gap.

---

## Spawning & Resuming

```javascript
Agent({ description: "3-5 word task label", subagent_type: "agent-name",
        prompt: "Full context. No conversation history." })
Agent({ description: "Search for X", subagent_type: "Explore", model: "haiku",
        prompt: "..." })
```

**`description` and `prompt` are both required.**

**There is no `run_in_background` parameter on the Agent tool.** Backgrounding:

- Set `background: true` in the **agent's own frontmatter** to keep it in the background.
- Otherwise Claude Code decides. With fork mode on — the default in an interactive
  session — subagents run in the background. With fork mode off (`-p`/headless, and the
  Agent SDK unless enabled), it runs them in the foreground when it needs the result.
- `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` forces foreground everywhere.
- `run_in_background: true` exists only for agent-team **teammates**.

**Resume via `SendMessage`**, not an `Agent({resume:...})` parameter:

```javascript
SendMessage({ to: "<agent-id-or-name>", message: "Continue. User feedback: ..." })
```

Resuming does NOT require `SendMessage` in the target agent's own tools — the *caller*
invokes it. (Separately, a subagent's **sibling roster**, which lets it message siblings,
appears only when its own tools include `SendMessage`.)

A completed agent that receives a message auto-resumes in the background with full
history. Transcripts persist at
`~/.claude/projects/{project}/{sessionId}/subagents/agent-{agentId}.jsonl`.
Built-in Explore/Plan are one-shot and cannot resume.

---

## Hooks

Frontmatter hooks are agent-scoped and need folder trust (v2.1.218+). **All hook events
are supported**; the most common for subagents are `PreToolUse`, `PostToolUse`, and `Stop`
(auto-converted to `SubagentStop`). Not available to plugin agents.

Session-level hooks in settings.json also see subagents, and add **`SubagentStart`**
(matcher on agent name) and `SubagentStop`.

---

## Built-in Subagents

`Explore` (read-only, skips CLAUDE.md/git status), `Plan` (read-only research),
`general-purpose` (full tools). Cannot be removed. Disable with
`CLAUDE_CODE_DISABLE_EXPLORE_PLAN_AGENTS=1`, `CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS=1`
(non-interactive mode and the Agent SDK only; removes all built-in types), or
`permissions.deny: ["Agent(Explore)", "Agent(Plan)"]`.

Note the differing prefixes: `CLAUDE_CODE_` for the Explore/Plan toggle, but
`CLAUDE_AGENT_SDK_` for the remove-all-built-ins toggle.

---

## Managing Agents

```bash
/agents                    # prints a reminder to ask Claude or edit .claude/agents/
                           # directly — no interactive wizard, as of v2.1.198
claude --agents '{"reviewer": {"description":"Reviews code","prompt":"You are a code reviewer"}}'
```

`--agents` takes a JSON object keyed by agent name. A `tools` array is also accepted and
honoured *(confirmed by runtime probe; the help text's example shows only `description`
and `prompt`)*.

**File-based edits are picked up automatically within a few seconds — no restart needed.**
Claude Code watches `~/.claude/agents/` and `.claude/agents/`. Three cases still need a
restart: a scope's first agent file in an `agents` directory that did not exist at session
start; agents under a directory added via `--add-dir`/`/add-dir` (not watched); and
sessions started with `--disable-slash-commands`.

---

## DO / DO NOT

- DO: give agents the minimum tools they need
- DO: keep orchestration in the main session so `AskUserQuestion` survives
- DO: check the parent's mode before relying on a child's `permissionMode`
- DO: have agents surface permission denials rather than work around them
- DO NOT: expect a subagent to ask the user anything
- DO NOT: use `TaskOutput` from inside a subagent — it is stripped
- DO NOT: give worker agents the `Agent` tool by default
- DO NOT: put an orchestrator in `dontAsk` — it cannot escalate
- DO NOT: create an agent just to do a single Glob/Grep call
