---
name: output-style-builder
description: >-
  Reference for creating and managing Claude Code output styles: file location,
  frontmatter fields, keep-coding-instructions, built-in styles, and selecting a
  style via /output-style, /config or the outputStyle setting. Use when defining, auditing or
  debugging a custom communication style or persona.
user-invocable: false
---

# Output Style Builder Reference

Verified against code.claude.com/docs/en/output-styles on 2026-09-19 — 0 open claims.

Common stale beliefs this file corrects: output styles are NOT skills and do not live in
`~/.claude/skills/`; there are only four frontmatter fields; and switching styles no longer
needs `/clear` or a restart (v2.1.251+).

---

## What an Output Style Is

An output style **modifies Claude Code's system prompt** to change role, tone, and default
response format. It changes *how Claude responds, not what Claude knows*.

Reach for one when you keep re-prompting for the same voice or format every turn, or when
Claude should act as something other than a software engineer.

For project conventions and codebase context, use `CLAUDE.md` instead — not a style.

### Output styles do not reach subagents

Styles apply to the **main conversation only**. A subagent runs its own system prompt, so a
style never changes how a subagent responds. The one exception is a **fork**, which
inherits the parent's full system prompt.

This matters for any orchestrator/specialist setup: you cannot use an output style to shape
specialist behaviour. Put that in the agent file.

---

## File Location and Format

An output style is a **plain Markdown file** — NOT a `SKILL.md`, and NOT in the skills
directory.

```
~/.claude/output-styles/my-style.md        ← User (all projects)
.claude/output-styles/my-style.md          ← Project
.claude/output-styles/my-style.md          ← Managed policy (inside managed settings dir)
```

- The **file name becomes the style name** unless you set `name` in the frontmatter.
- Project styles load from every `.claude/output-styles/` between the working directory and
  the repository root. If nested directories define the same style name, the one **closest
  to the working directory** wins.
- Plugins can also ship styles in an `output-styles/` directory.

---

## Frontmatter

Exactly four fields are supported. There are no others.

| Field | Purpose | Default |
|-------|---------|---------|
| `name` | Name of the style, if not the file name | Inherits from file name |
| `description` | Shown in the `/config` picker | None |
| `keep-coding-instructions` | Keep Claude Code's built-in software-engineering instructions | `false` |
| `force-for-plugin` | Plugin styles only: apply automatically whenever the plugin is enabled, without the user selecting it. Overrides the user's `outputStyle`. If several enabled plugins set it, the first loaded wins | `false` |

**`keep-coding-instructions` is the field to get right.** Custom styles *leave out* Claude
Code's built-in software-engineering instructions — how to scope changes, write comments,
verify work — unless it is set to `true`.

- Set `true` when changing how Claude communicates but it is still coding.
- Leave it out when Claude is not doing software engineering at all (writing assistant,
  data analyst).

---

## Selecting a Style

Run **`/output-style <style>`** to switch (e.g. `/output-style concise`); with no argument
it lists the styles you can pick and marks the current one. It also works in
non-interactive mode, the Agent SDK and Remote Control (v2.1.269+).

| Where | How |
|-------|-----|
| Terminal | Run `/output-style <style>`, or `/config` and select **Output style**. Saves to `.claude/settings.local.json` |
| VS Code | Open the command menu with `/`, select **Output styles** (v2.1.257+). Same file |
| Desktop | Set the `outputStyle` field in a settings file directly |

To set it without the menu:

```json
{
  "outputStyle": "Explanatory"
}
```

**Only one style is active at a time** — it is a single `outputStyle` field, so there is no
multi-style conflict to resolve.

**Styles don't auto-activate.** The docs describe no description-matching or
trigger-phrase mechanism; `description` is shown to a human in the picker. The one
documented exception is a plugin style using `force-for-plugin: true`.

### When it takes effect

A style you switch to mid-session applies from your **next message** (v2.1.251+; earlier
versions needed `/clear` or a new session). Editing or creating a style **file** during a
running terminal session still needs a restart: style files are read at startup.

---

## Built-in Styles

| Style | Behaviour |
|-------|-----------|
| **Default** | The existing system prompt, tuned for software engineering |
| **Proactive** | Executes immediately, makes reasonable assumptions, prefers action over planning. Stronger autonomous-execution guidance than auto mode, and works without changing permission mode — your permission mode still decides what runs without asking |
| **Concise** | Leads with the result, skips preamble, keeps responses short, while doing the engineering just as thoroughly. Answers in full when you ask for detail. Always keeps complete error reports, security warnings, and destructive-action confirmations. Requires v2.1.237+ |
| **Explanatory** | Adds educational "Insights" between tasks, explaining implementation choices and codebase patterns |
| **Learning** | Learn-by-doing: shares Insights and asks you to write small strategic pieces, adding `TODO(human)` markers in your code |

---

## Example

```markdown
---
name: Diagrams first
description: Lead every explanation with a diagram
keep-coding-instructions: true
---

When explaining code, architecture, or data flow, start with a Mermaid diagram showing
the structure, then explain in prose.

## Diagram conventions

Use `flowchart TD` for control flow and `sequenceDiagram` for request paths. Keep
diagrams under 15 nodes.
```

---

## Choosing Between Related Features

| Feature | How it works | Use when |
|---------|-------------|----------|
| **Output styles** | Modifies the system prompt | You want a different role, tone, or default format every turn |
| **CLAUDE.md** | Adds a user message after the system prompt | Claude should always know your project conventions |
| **`--append-system-prompt`** | Appends to the system prompt without removing anything | A one-off addition for a single invocation |
| **Agents** | Runs a subagent with its own system prompt, model, tools | You want a separately scoped helper for a focused task |
| **Skills** | Loads task-specific instructions when invoked or relevant | You have a reusable workflow |

---

## Token Cost

Adding instructions to the system prompt increases input tokens, though prompt caching
reduces this after the first request in a session. Explanatory and Learning produce longer
responses by design (more output tokens); Concise does the opposite.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Style doesn't appear in the `/config` picker | Missing `description`, or file not under an `output-styles/` directory | Add a `description`; confirm the file is at `~/.claude/output-styles/` or `.claude/output-styles/` |
| Style selected but nothing changed | Before v2.1.251 a switch applied only after `/clear`; or you edited the style file, which is read at startup | Update Claude Code, or restart after editing a style file |
| Coding behaviour got worse | `keep-coding-instructions` not set | Add `keep-coding-instructions: true` |
| Style has no effect on a subagent | Working as designed | Styles apply to the main conversation only; put it in the agent file instead |
| Wrong style keeps applying | A plugin style sets `force-for-plugin: true` | It overrides your `outputStyle`; disable that plugin |

---

## DO / DO NOT

- **DO** set `keep-coding-instructions: true` for any style used while still writing code.
- **DO** write a clear `description` — a human reads it in the picker.
- **DO** keep instructions short; system prompt space is valuable.
- **DO NOT** put output styles in `~/.claude/skills/` — they will never load.
- **DO NOT** use a style to add knowledge; that is what skills and `CLAUDE.md` are for.
- **DO NOT** expect a style to influence subagents.
- **DO NOT** write styles that contradict the user's `CLAUDE.md`.
- **DO NOT** create personas that claim to be a different AI or misstate capabilities.
