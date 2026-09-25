---
name: agent-teams
description: >-
  Reference for Claude Code experimental agent teams: enabling teams, display
  modes, the shared task list, teammate messaging, permission behaviour, and
  when to use teams vs subagents. Use when designing, auditing, or debugging a
  multi-agent team, or deciding between teams and subagents.
user-invocable: false
---

# Agent Teams Reference

Verified against code.claude.com/docs/en/agent-teams on 2026-09-25 — 3 open claims, each
marked **[UNCONFIRMED]** inline.

---

## Status: Experimental, off by default

```json
{ "env": { "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1" } }
```

Without it: no team is set up, no team directories are written, and Claude does not
spawn or propose teammates.

`TeamCreate` and `TeamDelete` **no longer exist** (removed in v2.1.178). Teams are
created implicitly at session start; cleanup is automatic on exit. The `team_name`
input on the Agent tool is accepted but ignored.

---

## Read this before enabling: three traps

1. **Enabling teams changes ordinary delegation.** While teams are on, a subagent that
   Claude *names* launches as a **teammate**, not a subagent. Claude names subagents on
   its own, so teams can form during delegation you never framed as team work.
2. **Teammates start in the lead's permission mode, except `dontAsk`, which they never
   inherit, and you cannot set per-teammate modes at spawn time** (you can change an
   individual teammate's mode afterwards). Teammate permission prompts bubble up to the
   **lead session**. If a design depends on per-agent `permissionMode` from the start
   (e.g. specialists pinned to `dontAsk`), **teams break it**: a `dontAsk` lead does not
   pass that mode on. Use subagents.
3. **Split panes do not work in Windows Terminal**, VS Code's integrated terminal, or
   Ghostty. The docs name those three terminals specifically **[UNCONFIRMED whether this
   generalises to every Windows terminal — in practice, plan for in-process mode on
   Windows]**.

To turn off: set the variable to `"0"` in `~/.claude/settings.json`. No restart needed —
settings `env` values reapply to the running session on save. Project/local/managed
settings apply *after* user settings, so a `1` in any of those wins.

---

## Teams vs subagents

|  | Subagents | Agent teams |
|---|---|---|
| Coordination | Main agent manages all work | Self-coordination + shared task list |
| Communication | Return a result; named subagents can `SendMessage` each other | Teammates message each other directly |
| Nesting | **Can nest** (default depth 3) | **Cannot** — only the lead manages the team |
| Permission mode | Per-agent via frontmatter | Lead's mode at spawn (never `dontAsk`); changeable per teammate afterwards |
| Token cost | Lower — results summarised back | Higher — each teammate is a full session |
| Best for | Focused tasks where only the result matters | Work needing discussion and challenge |

"Workers cannot spawn workers" is true for **teams** and false for **subagents**. Do not
carry the rule across.

Prefer subagents. Reach for teams only when teammates genuinely need to challenge each
other — parallel review, competing debugging hypotheses, independent modules.

---

## Display modes

Set `teammateMode` in `~/.claude/settings.json`, or `claude --teammate-mode <mode>`
(experimental flag, absent from `--help`):

| Mode | Behaviour |
|---|---|
| `in-process` | **Default.** All teammates in one terminal, agent panel below the prompt. Works anywhere. |
| `auto` | Split panes when already inside tmux, or iTerm2 with `it2`; otherwise in-process. |
| `tmux` | Split panes, auto-detecting tmux vs iTerm2. |
| `iterm2` | iTerm2 native panes (v2.1.186+). Requires the `it2` CLI. |

Default was `auto` before v2.1.179 **[UNCONFIRMED — not on the current page]**. `teammateDefaultModel` was removed in v2.1.234.

**In-process panel**: up/down select, Enter opens a teammate's transcript, `x` stops it,
Ctrl+T toggles the task list, Escape clears/interrupts. Idle rows hide 30s after the
whole panel goes idle; beyond three idle teammates the surplus collapses into one
`N idle agents` row. Hidden is not stopped.

---

## Architecture

| Component | Role |
|---|---|
| Team lead | Main session; spawns teammates, coordinates, synthesises |
| Teammates | Separate full Claude Code instances |
| Task list | Shared work items teammates claim |
| Mailbox | Per-agent JSON message file |

Paths (session-derived name = `session-` + first 8 chars of session ID):

- Team config: `~/.claude/teams/{team-name}/config.json` — removed on session end.
  Holds runtime state; **never hand-edit or pre-author it.**
- Mailbox: `~/.claude/teams/{team-name}/inboxes/{agent-name}.json`
- Task list: `~/.claude/tasks/{team-name}/` — persists, so resumed sessions keep tasks.
  Retention follows `cleanupPeriodDays`.

There is no project-level team config. `.claude/teams/teams.json` is just an ordinary file.

---

## Shared task list

Tools available in sessions that have the Task tools: `TaskCreate`, `TaskGet`,
`TaskList`, `TaskUpdate`. Agents without them coordinate purely by messages.

States: pending, in progress, completed. Tasks may declare dependencies; a pending task
with unresolved dependencies cannot be claimed. Completing a task auto-unblocks its
dependents. Claiming uses file locking to prevent races.

Lead assigns explicitly, or a teammate self-claims the next unassigned unblocked task.

---

## Teammate messaging

`SendMessage`, addressed by the name the lead assigned at spawn. One message per
recipient — there is no broadcast. Delivery is automatic; the lead does not poll.
An idle teammate notifies the lead with its final answer; a teammate that dies on an
API error reports the error text.

**Security model:** a message from another agent is labelled as coming from another
Claude session, not from the user. A teammate cannot approve a permission prompt or
supply consent on the user's behalf, and a denied teammate cannot relay the action to
another teammate to get around the check. In `auto` mode the classifier treats relayed
approval claims as untrusted and screens every inter-agent message before delivery.

---

## Using subagent definitions as teammates

Name a subagent type when spawning: "Spawn a teammate using the security-reviewer agent
type." What carries over is partial:

| Field | In-process teammate | Split-pane teammate |
|---|---|---|
| `tools` | Applied, **plus** `SendMessage` (+ Task tools if present) | Applied **[UNCONFIRMED whether `SendMessage`/Task tools are added — the docs state it only for in-process]** |
| `model` | Applied when the prompt names none | Same |
| Body | **Appended to** the default system prompt | **Replaces** the system prompt |
| `skills` | **Ignored** — loads from project/user settings | **Ignored** |
| `mcpServers` | **Ignored** | Applied |

Model precedence: prompt, then the definition's `model`, then
`CLAUDE_CODE_SUBAGENT_MODEL`, then the lead's model. (Before v2.1.251 the env var came
first.) `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` applies to teammates too. Teammates inherit
the lead's effort level.

---

## Quality-gate hooks

- `TeammateIdle` — fires as a teammate goes idle; `exit 2` sends feedback and keeps it working
- `TaskCreated` — `exit 2` prevents creation
- `TaskCompleted` — `exit 2` prevents completion

---

## Limitations

- **No nested teams** — teammates cannot spawn teammates
- **No session resumption** for in-process teammates (`/resume`, `/rewind` do not restore them)
- **No background subagents from in-process teammates** — a teammate's subagents run
  foreground; `background: true` definitions error
- Task status can lag; teammates sometimes fail to mark completion, blocking dependents
- Shutdown is slow — teammates finish the current tool call first
- One team per session; lead is fixed for the session's lifetime
- Permissions set to the lead's mode at spawn (except `dontAsk`, never inherited); not settable per teammate at spawn, changeable individually afterwards

---

## DO / DO NOT

- DO: prefer subagents; reach for teams only when agents must challenge each other
- DO: start with 3-5 teammates; give each a distinct lens so they do not overlap
- DO: pass full context in the spawn prompt — teammates load CLAUDE.md, MCP and skills,
  but **not** the lead's conversation history
- DO: give each teammate its own files; two teammates editing one file overwrite
- DO: pre-approve common operations in permission settings before spawning
- DO NOT: enable teams if the design needs per-agent permission modes
- DO NOT: enable teams on Windows expecting split panes
- DO NOT: hand-edit the team config
- DO NOT: assume "workers cannot spawn workers" applies to subagents — it does not
