# Claudit

**Audit, fix and build your Claude Code setup against Anthropic's documented guidance.**

Claudit is a [Claude Code](https://code.claude.com) plugin. It reviews your agents,
skills, hooks, MCP servers, permission rules and settings; explains each problem in plain
English; and applies only the fixes you approve, one by one. It can also build new agents
and skills to the same standard.

> Claudit is an independent community project. It is not made, endorsed or supported by
> Anthropic.

## What it does

| Command | What happens |
|---|---|
| `/claudit:audit` | Reviews your setup and shows each problem as a change card: what happens now, the exact change, what it means in plain English, where the guidance comes from (Anthropic's docs, Claudit's summary of them, or a Claudit rule, labelled as such), and how to undo it. You choose which to apply. |
| `/claudit:audit permissions` | Just one area. Also: `config`, `agents`, `supply-chain`. |
| `/claudit:build <what you want>` | Designs a new agent or skill, has a separate reviewer check it, and shows you a card like *"Build an agent that reviews SQL migrations — it can read files, it cannot edit them or run commands."* Nothing is written until you say yes. |
| `/claudit:about` | Your version, when each piece of guidance was last checked against the docs, and whether an update is available. |

What the audit looks for, for example:

- **Permission rules that can never match** — and so silently deny, or silently fail to
  protect — plus rules much broader than they look.
- **Hooks** with the wrong event name, a matcher that fires on everything, or an exit code
  that means they never actually block.
- **Agents and skills** with invalid settings, an orchestrator that can't ask you
  anything, or tools far wider than the job needs. Style suggestions are labelled
  optional and never reported as errors.
- **Third-party components** that can run commands, add MCP servers or skip permission
  prompts — reported in plain English so you can decide what to keep.

## Install

In Claude Code:

```
/plugin marketplace add brandoncox-commits/claudit
/plugin install claudit@claudit
```

Choose **user scope** to use it in every project.

### Turn on automatic updates

Claudit's guidance is kept current with Anthropic's documentation, but **Claude Code turns
automatic updates off by default for marketplaces not run by Anthropic.** To get updates
without thinking about it:

```
/plugin  →  Marketplaces  →  claudit  →  Enable auto-update
```

Or update by hand at any time with `/plugin marketplace update claudit`, then
`/reload-plugins`. `/claudit:about` and the top of every audit tell you when a newer
version exists.

## What it reads, what it changes, what it sends

- **Reads** your Claude Code configuration: `~/.claude/` (settings, agents, skills, output
  styles, and installed plugins including their hooks, MCP config and bundled scripts),
  the MCP sections of `~/.claude.json`, and the current project's `.claude/`, `.mcp.json`
  and `CLAUDE.md`. It does not read `.env` files or private keys.
- **Changes** nothing unless you approve that specific change. Claude Code's own
  permission prompts still apply on top of Claudit's approval step.
- **Sends** what it reads to the model, as part of your normal Claude Code session — the
  same as any file Claude Code reads. That includes a token or key if one is stored as a
  literal value in a settings or MCP file. Claudit's findings never repeat such a value
  (it is shown as `<redacted>`), but the model still processes it for that session. To
  keep secrets out of every Claude Code session, not just Claudit's, use `${VAR}`
  environment-variable expansion instead of literal values.
- **Fetches** one thing from the web: Claudit's own version number from
  `raw.githubusercontent.com`, to tell you when an update exists. Nothing else.

The Claudit agents are read-only and offline: they have no tool to edit files, run
commands or reach the internet. Edits are made in your main conversation, after you
approve them.

The supply-chain review reads installed third-party files so it can tell you what they
do. It is not a quarantine for untrusted downloads — screen those before installing.

## How the guidance stays current

Each reference Claudit relies on records the documentation page it was checked against,
and when. A daily job compares those pages with their verified versions and flags any that
have changed; the maintainer re-checks the affected guidance, and a new version ships. The
[CHANGELOG](CHANGELOG.md) lists each guidance change and the doc page behind it.

Guidance marked **partially verified** or **not yet audited** in `/claudit:about` is
used, but its findings are labelled with lower confidence.

## Limits

- Claudit checks configuration against documented behaviour. It is not a security audit
  of your code (use `/security-review`) or a code review (use `/code-review`).
- Documentation changes faster than any snapshot. Findings show their source and a
  confidence level; if one looks wrong, check the linked page and
  [open an issue](https://github.com/brandoncox-commits/claudit/issues).
- An audit of a large setup spawns several reviewer agents and uses a meaningful number of
  tokens. Narrow it with `/claudit:audit <area>`.

## Contributing

Issues and pull requests are welcome. A finding that disagrees with the docs is the most
useful report there is — include the doc link.

## Licence

[MIT](LICENSE)
