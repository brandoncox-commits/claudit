---
name: skill-builder
description: >-
  Reference for creating Claude Code skills: frontmatter fields, progressive
  disclosure, description formula, invocation control, workflow patterns,
  troubleshooting. Use when building, auditing, or writing a SKILL.md, or
  diagnosing why a skill does or does not trigger.
user-invocable: false
---

# Skill Builder Reference

Patterns and standards for creating Claude Code skills (Agent Skills open standard).

Verified against code.claude.com/docs/en/skills on 2026-09-19 — 5 open claims, each
marked **[UNCONFIRMED]** or **[UNVERIFIED]** inline. Items marked *Convention* are good
practice, not requirements — report gaps against them as suggestions, never as errors.

---

## Core Concepts

| Concept | What it means |
|---------|---------------|
| Progressive Disclosure | Level 1 = frontmatter (always loaded), Level 2 = SKILL.md body (loaded on trigger), Level 3 = `references/` (on demand) |
| Composability | Skills work alongside others; don't assume you're the only one |
| Portability | The open standard works across Claude.ai, Claude Code, and the API — but see Portability below for which fields survive |

---

## File Structure

```
skill-name/              ← kebab-case folder name = skill ID
├── SKILL.md             ← Required. Exact name (case-sensitivity [UNCONFIRMED]).
├── scripts/             ← Executable code (Python, Bash)
├── references/          ← Detailed docs loaded on demand (convention)
└── assets/              ← Templates, fonts, icons (convention)
```

The docs' own example uses `scripts/` plus loose files such as `reference.md` and
`examples.md`; the `references/` and `assets/` folders are a convention, not a
requirement.

Rules:
- *Convention:* folder name in kebab-case (no spaces, no capitals, no underscores). The
  docs require kebab-case for **plugin** names; they state no such rule for skill folders
- File MUST be named exactly `SKILL.md`
- Keep `SKILL.md` under 500 lines (official guidance); move heavy content to `references/`
- *Convention:* no `README.md` inside the skill folder — put docs in `SKILL.md` or `references/`

---

## Minimal Frontmatter

```yaml
---
name: skill-name        # optional; defaults to folder name
description: >-         # the trigger. Combined description + when_to_use capped at 1536 chars in the listing
  What it does. Use when user asks to [trigger phrases].
---
```

## Full Frontmatter Reference

| Field | Required | Notes |
|-------|----------|-------|
| `name` | No | Display label; defaults to folder name. The command name comes from the directory, not this field (except a plugin-root SKILL.md). |
| `description` | Recommended | WHAT + WHEN + triggers. Combined `description` + `when_to_use` capped at 1536 chars in the listing (configurable via `skillListingMaxDescChars`). If omitted, the first non-empty line of the body is used. |
| `when_to_use` | No | Extra trigger context / example requests; appended to `description`, counts toward the cap. |
| `disable-model-invocation` | No | `true` = only the user can invoke (manual `/skill-name`); also blocks preload into subagents. |
| `user-invocable` | No | `false` = hide from the `/` menu (background knowledge only). Controls menu visibility only, not Skill-tool access. |
| `allowed-tools` | No | Tools Claude can use without prompting while the skill is active. Space/comma string or YAML list. |
| `disallowed-tools` | No | Tools removed from the pool while the skill is active. Clears on next message. |
| `model` | No | Same values as `/model`, or `inherit`. Applies for the rest of the current turn only. |
| `effort` | No | `low` / `medium` / `high` / `xhigh` / `max` (depends on model). Overrides session effort. |
| `context` | No | `fork` = run in a forked subagent. |
| `agent` | No | Subagent type for `context: fork`: `Explore`, `Plan`, `general-purpose`, or custom. Defaults to `general-purpose`. |
| `paths` | No | Glob patterns limiting when the skill auto-activates. |
| `arguments` | No | Named positional args for `$name` substitution. |
| `shell` | No | Shell for dynamic-injection commands: `bash` (default) or `powershell`. |
| `background` | No | With `context: fork`, `false` waits for the result in the invoking turn. Default `true`. Requires v2.1.218+. |
| `hooks` | No | Hooks scoped to this skill's active lifetime. |
| `argument-hint` | No | Shown in autocomplete: `[issue-number]` or `[file] [format]`. |
| `license` / `compatibility` / `metadata` | No | Open-standard (agentskills.io) fields. |

### Unknown frontmatter keys

The table above is Claude Code's complete recognised set: "Claude Code accepts every field
in the table above" (/docs/en/skills). **What Claude Code does with a key outside that set —
silently ignore it, warn, or reject the file — is not documented.** [UNVERIFIED — do not
assume "silently ignored" merely because no failure has been observed; that has not been
confirmed either.]

That is a different claim from portability, which the docs do state precisely: uploading to
claude.ai or the Skills API rejects any field outside the six spec fields with a hard error.
That error belongs to the **upload and packaging validator**, not to Claude Code loading a
local `SKILL.md`. Never conflate the two.

**Audit rule.** An unrecognised key in a local `SKILL.md` (say `skills-version: 2`) is not a
confirmed Claude Code defect — its local behaviour is undocumented — so raise it as a
SUGGESTION worded as "unrecognised frontmatter key; Claude Code's handling of it is
undocumented", never as an ERROR. If the skill is or will be published to claude.ai or the
Skills API, the same key **is** a documented hard failure: raise that at real severity and
cite the six-field spec.

Avoid angle brackets (`<` `>`) in `description` — they display badly in claude.ai-synced
skills. For skills uploaded to claude.ai or the Skills API, `name` may not contain the
reserved words `claude` or `anthropic` **[UNCONFIRMED — not on the Claude Code skills page]**.

---

## Description Formula

`[What it does] + [When to use it] + [Key capabilities]`

### Good descriptions

```yaml
description: >-
  Creates REST endpoint handlers following this project's conventions.
  Use when user asks to create, add, or write a new API route.
  Contains naming rules, error handling patterns, template code.
```

```yaml
description: >-
  Analyzes Figma design files and generates developer handoff docs.
  Use when user uploads .fig files, asks for "design specs",
  "component documentation", or "design-to-code handoff".
```

### Bad descriptions

```yaml
description: Helps with projects.            # too vague, no triggers
description: Creates documentation systems.  # missing trigger phrases
```

Add negative triggers when needed:
```yaml
description: >-
  Advanced statistical analysis for CSV data. Use for regression and clustering.
  Do NOT use for simple data viewing (use data-viz skill instead).
```

---

## Invocation Control

| Frontmatter | You can invoke | Claude can invoke | When loaded into context |
|-------------|-----------|-------------------|-----------|
| (default) | Yes | Yes | Description always in context, full skill loads when invoked |
| `disable-model-invocation: true` | Yes | No | Description not in context, full skill loads when you invoke |
| `user-invocable: false` | No | Yes | Description always in context, full skill loads when invoked |

Use `disable-model-invocation: true` for side-effecting workflows: `/deploy`, `/commit`,
`/send-message`. Use `user-invocable: false` for background knowledge and style guides.

The docs say that if Claude tries to invoke a `disable-model-invocation: true` skill anyway,
"Claude Code blocks the call". [UNVERIFIED — observed 2026-09-11 on v2.1.268: when a
user's `/command` text arrived as plain text (leading spaces), Claude's Skill-tool call to
such a skill succeeded. Whether the block holds when Claude invokes one unprompted is
untested.]

---

## Three Skill Roles

| Role | Pattern | Use for |
|------|---------|---------|
| **Reference knowledge** | Default frontmatter (often `user-invocable: false`) | API patterns, conventions, domain expertise |
| **Slash command** | `disable-model-invocation: true` | Workflows you trigger manually |
| **Subagent task** | `context: fork` + `agent:` | Isolated research or execution |

---

## Arguments & String Substitutions

```
/skill-name some text here    → $ARGUMENTS = "some text here"
/skill-name arg1 arg2         → $0 = "arg1", $1 = "arg2"
```

If no placeholder receives the arguments, they are appended as `ARGUMENTS: <input>`.

| Variable | Meaning |
|---|---|
| `$ARGUMENTS` | All arguments |
| `$ARGUMENTS[N]` / `$N` | Argument by 0-based index |
| `$name` | Named argument declared in `arguments:` frontmatter |
| `${CLAUDE_SKILL_DIR}` | Directory containing this SKILL.md (for plugin skills, the skill's own subdirectory) |
| `${CLAUDE_PROJECT_DIR}` | Project root |
| `${CLAUDE_SESSION_ID}` | Current session ID |
| `${CLAUDE_EFFORT}` | Current effort level |
| `${CLAUDE_PLUGIN_ROOT}` / `${CLAUDE_PLUGIN_DATA}` | Plugin skills only |

Escape a literal `$` with a backslash: `\$1.00`.

---

## Discovery & Nested Skills

- Project skills load from `.claude/skills/` in the starting directory **and every parent
  directory** up to the repo root
- Nested `.claude/skills/` **below** the starting point load when Claude first reads or
  edits a file in that subdirectory. `/add-dir <path>` preloads them (v2.1.257+)
- A nested skill sharing a root skill's name stays available under a directory-qualified
  name (`/apps/web:deploy`); the root keeps the bare `/deploy`
- Folder name `synced` is **reserved** (skills pulled from a claude.ai account)
- Symlinks are followed; the same target reachable twice loads once
- Plugin skills are namespaced (`/plugin-name:skill-name`) and never collide with
  standalone skills

Budget: the listing costs ~1% of the context window, tunable via
`skillListingBudgetFraction` or the `SLASH_COMMAND_TOOL_CHAR_BUDGET` env var. When over
budget, descriptions are dropped starting with the **least-used** skills. `/doctor` shows
listing cost; `/skill-doctor` finds unused skills.

---

## Portability: what survives outside Claude Code

The Agent Skills spec (Skills API, claude.ai upload) accepts **only**: `name`,
`description`, `license`, `compatibility`, `metadata`, `allowed-tools`. This is a rule about
*external distribution*, separate from what Claude Code tolerates locally — see "Unknown
frontmatter keys" above.

Every other field — including `disable-model-invocation`, `user-invocable`, `context`,
`paths` — is rejected on upload:

```
Unexpected key(s) in SKILL.md frontmatter: disable-model-invocation.
```

So a Claude-Code-tuned skill is not directly uploadable, and a downloaded spec-compliant
skill will lack invocation control.

---

## Content Lifecycle

- Invoked SKILL.md content enters the conversation as one message and **persists across
  later turns**
- Re-invoking with identical rendered content adds a short note, not a full copy
- `allowed-tools` / `disallowed-tools` grants **clear on the next user message**
- After auto-compaction the most recent invocation of each skill is re-attached
  (first 5,000 tokens per skill, 25,000 combined)

---

## Dynamic Context Injection

Run shell commands before skill content reaches Claude: a bang immediately followed by a
backtick-quoted command; the command's output replaces the placeholder.

**In the example below, `<bang>` stands for a literal `!`.** It is written that way on
purpose: the syntax is recognised anywhere the bang starts a line or follows whitespace,
**including inside a fenced code block** **[UNCONFIRMED — observed, not in the docs]**, so a reference file that reproduced it literally
would execute the command every time it was loaded — including when preloaded into a
subagent that has no shell. There is no escape character; making the bang follow a
non-whitespace character renders it inert.

**Audit implication:** any skill containing the live syntax runs that command whenever it
loads. Treat it like a hook.

```yaml
---
name: pr-summary
context: fork
agent: Explore
allowed-tools: Bash(gh *)
---

PR diff: <bang>`gh pr diff`
Changed files: <bang>`gh pr diff --name-only`

Summarize this PR...
```

A multi-line form also exists: a fenced block whose info string is a single bang runs each
line as a command.

---

## Skill Body Template

```markdown
# Skill Title

One-line summary.

## Core Principles
High-level rules. Keep short and opinionated.

## [Topic Area]
| Column A | Column B |
|----------|----------|

## Common Scenarios
| Scenario | What to do |
|----------|------------|

## Do / Do Not
- DO: follow X
- DO NOT: do Y

## Troubleshooting
Error / Cause / Solution
```

---

## Troubleshooting Skills

| Symptom | Cause | Fix |
|---------|-------|-----|
| Never auto-triggers | Description too vague | Add specific trigger phrases; test by asking Claude "when would you use X skill?" |
| Triggers too often | Description too broad | Add negative triggers; be more specific |
| Wrong file name | `skill.md` or `SKILL.MD` | Must be exactly `SKILL.md` |
| Folder name awkward to invoke | Has spaces or capitals | Use kebab-case (a convention; the docs state no skill-folder naming rule) |
| Description dropped from listing | Over the listing budget, and the skill is rarely used | Shorten descriptions; remove unused skills |
| Skill conflicts with another | Same name at different scopes | Higher-priority wins: enterprise > user > project |
| Upload to claude.ai fails | Claude-Code-only frontmatter fields | Strip to the six spec fields |

---

## DO / DO NOT

- DO: include both WHAT and WHEN in the description
- DO: use tables for lookup content and code blocks for patterns
- DO: move heavy docs to `references/`
- DO: use `disable-model-invocation: true` for anything with side effects
- DO NOT: put multi-step workflows in a reference skill — make a user-invoked skill
- DO NOT: write the live bang-backtick syntax in examples
