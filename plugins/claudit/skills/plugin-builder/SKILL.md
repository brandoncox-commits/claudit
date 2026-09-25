---
name: plugin-builder
description: >-
  Reference for creating Claude Code plugins: manifest format, directory
  structure, components (skills/agents/hooks/MCP), what plugin agents may declare,
  versioning and updates, distribution, marketplaces. Use when building,
  packaging, auditing, or installing a plugin.
user-invocable: false
---

# Plugin Builder Reference

Patterns and standards for creating and distributing Claude Code plugins.

Verified on 2026-09-25 against code.claude.com/docs/en/plugins, /plugins-reference,
/plugin-marketplaces, /discover-plugins and /sub-agents — 3 open claims, marked
**[UNCONFIRMED]** inline.

---

## Plugin vs Standalone Configuration

| | Standalone `.claude/` | Plugin |
|-|----------------------|--------|
| Skill names | `/hello` | `/plugin-name:hello` |
| Sharing | Manual copy | Install via marketplace |
| Best for | Personal, single-project | Team, community, versioned |
| Namespaced | No | Yes (prevents conflicts) |

**Use standalone** when: personal workflow, single project, quick iteration.
**Use plugin** when: sharing with team/community, multiple projects, versioned releases.

---

## Plugin Structure
```
my-plugin/
├── .claude-plugin/
│   └── plugin.json         ← Manifest (optional if components use default locations)
├── skills/                 ← Skills as <name>/SKILL.md (at plugin ROOT)
├── commands/               ← Skills as flat .md files (use skills/ for new plugins)
├── agents/                 ← Subagent definitions
├── output-styles/          ← Output style definitions
├── workflows/              ← Workflow script files
├── themes/                 ← Color theme definitions
├── monitors/monitors.json  ← Background monitors (experimental)
├── hooks/hooks.json        ← Hook configuration
├── bin/                    ← Executables added to the Bash tool's PATH while enabled
├── settings.json           ← Default settings (only `agent` and `subagentStatusLine`)
├── .mcp.json               ← MCP server definitions
├── .lsp.json               ← LSP server configurations
└── scripts/                ← Hook and utility scripts
```

WARNING: `commands/`, `agents/`, `skills/`, `workflows/`, `output-styles/`, `themes/`,
`monitors/` and `hooks/` MUST be at plugin ROOT.
Never put them inside `.claude-plugin/`. Only `plugin.json` goes there.

A plugin that ships exactly one skill can place `SKILL.md` directly at the plugin root.

Paths cannot reach outside the plugin directory with `../` — installed plugins are copied
into a cache, and files outside the plugin root are not copied.

---

## Plugin Manifest (.claude-plugin/plugin.json)
If you include a manifest, `name` is the ONLY required field:
```json
{
  "name": "my-plugin"
}
```

Common fields:
```json
{
  "name": "my-plugin",              // Required. Skill namespace prefix. kebab-case.
  "displayName": "My Plugin",       // Human-readable name for UI.
  "description": "...",             // Shown in plugin manager.
  "version": "1.0.0",               // Semver. Pins the version — see Versioning below.
  "author": { "name": "Your Name", "email": "you@example.com" },
  "homepage": "https://...",
  "repository": "https://github.com/your/repo",
  "license": "MIT",
  "keywords": ["..."]
}
```

The `name` field becomes the namespace prefix: `/my-plugin:skill-name`.

---

## Skills in Plugins

Same as regular skills but namespaced. `skills/greet/SKILL.md` → `/my-plugin:greet`.
Arguments work the same: `/my-plugin:greet Alex` → `$ARGUMENTS = "Alex"`.

Inside plugin skill content, `<dollar>{CLAUDE_PLUGIN_ROOT}` (install directory) and
`<dollar>{CLAUDE_PLUGIN_DATA}` (persistent data directory that survives updates) are substituted,
as is `<dollar>{CLAUDE_SKILL_DIR}` (the skill's own subdirectory). In this file `<dollar>` stands for a literal `$`, written that way so this skill's own text is not rewritten when it loads. When you write config for a user, type a real `$`; copying `<dollar>` verbatim produces a server that fails to start.

---

## Agents in Plugins
Plugin agents support only: `name`, `description`, `model`, `effort`, `maxTurns`,
`tools`, `disallowedTools`, `skills`, `memory`, `background`, `omitClaudeMd`, `color`,
`experimental`, and `isolation` (the only valid value is `"worktree"`). `initialPrompt` is
also not supported.

**For security reasons, `hooks`, `mcpServers`, and `permissionMode` are not supported
for plugin-shipped agents.** A plugin agent that declares them does not get them. Enforce
read-only / propose-only behaviour through `tools` and `disallowedTools` instead.

This restriction applies to plugins only. A plain agent `.md` file copied into
`~/.claude/agents/` or `.claude/agents/` DOES honour `hooks`, `mcpServers` and
`permissionMode` — which is why downloaded agent files need screening. (One exception:
from v2.1.267 a subagent that declares `permissionMode: bypassPermissions` keeps the
parent's mode instead.)

Plugin agents are listed under a scoped name such as `my-plugin:code-reviewer`. Project
and user `.claude/agents/` definitions override same-named plugin agents.

---

## Hooks in Plugins

Create `hooks/hooks.json` at plugin root (same format as settings.json hooks):

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [{
          "type": "command",
          "command": "jq -r '.tool_input.file_path' | xargs npm run lint:fix"
        }]
      }
    ]
  }
}
```

Hook commands receive JSON via stdin. Exit code 2 blocks the action; stderr becomes
Claude's feedback. A plugin hook runs shell commands on the user's machine — it is the
highest-trust component a plugin can ship.

---

## MCP Servers in Plugins

In `.mcp.json` at plugin root:
```json
{
  "mcpServers": {
    "database-tools": {
      "command": "<dollar>{CLAUDE_PLUGIN_ROOT}/servers/db-server",
      "args": ["--config", "<dollar>{CLAUDE_PLUGIN_ROOT}/config.json"],
      "env": {
        "DB_URL": "${DB_URL}"
      }
    }
  }
}
```

Supported transports: `stdio`, `http`, `ws`, `sse` (deprecated — use `http` instead
where available).

---

## Default Settings (settings.json)
Only the `agent` and `subagentStatusLine` keys are supported; unknown keys are silently
ignored. `agent` activates one of the plugin's agents as the **main thread**, replacing
the user's normal system prompt, tools and model while the plugin is enabled — use it only
when that is the plugin's whole purpose.

---

## LSP Servers (.lsp.json)

```json
{
  "go": {
    "command": "gopls",
    "args": ["serve"],
    "extensionToLanguage": { ".go": "go" }
  }
}
```

Users must have the language server binary installed. Prefer the official marketplace LSP
plugins (typescript-lsp, pyright-lsp, gopls-lsp, rust-analyzer-lsp, etc.).

---

## Testing a Plugin Locally
```bash
claude plugin validate ./my-plugin          # same check the community review runs
claude --plugin-dir ./my-plugin             # load without installing, for one session
claude --plugin-dir ./plugin-one --plugin-dir ./plugin-two  # load several at once
claude --plugin-dir ./plugins               # folder of plugins: loads every subfolder
                                             # with a manifest. Requires v2.1.265+
claude --plugin-url https://…/my-plugin.zip # zip archive hosted at a URL; repeat the
                                             # flag to load more than one
claude plugin init my-tool                  # scaffold a skills-directory plugin
claude plugin eval ./my-plugin              # run the plugin's eval suite. Requires v2.1.269+
```

A `--plugin-dir` plugin takes precedence over an installed plugin of the same name for that
session. Run `/reload-plugins` to pick up edits without restarting.

---

## Marketplaces
A marketplace is a git repo (or hosted JSON) with `.claude-plugin/marketplace.json`:

```json
{
  "name": "my-marketplace",
  "owner": { "name": "Your Name" },
  "plugins": [
    { "name": "my-plugin", "source": "./plugins/my-plugin", "description": "..." }
  ]
}
```

Plugin `source` can be a relative path, `github` (`repo`, optional `ref`/`sha`), git `url`,
`git-subdir`, `npm`, a zip `archive`, or a `command`. Relative paths only resolve when the
marketplace is added from git or a local directory, not from a direct JSON URL.

A `command` source shows the user the exact command before it runs. In a non-interactive
shell, `claude plugin install` and `claude plugin update` take `--yes` to accept whatever
command is currently printed, or `--accept-command <sha256>` to accept only the exact
command a prior `--json` run reported in its `shownCommand` object — if the command, plugin,
or marketplace catalog changed since that run, the digest doesn't match and Claude Code
shows the command again. `--accept-command` requires v2.1.271+; prefer it in automation,
since `--yes` accepts whatever is printed at the moment it runs, not necessarily what you
reviewed earlier.

```bash
/plugin                                     # open the interactive plugin manager panel
/plugin list                                # list installed plugins without opening the panel
/plugin list --enabled                      # also --disabled
/plugin validate ./my-plugin                # run the validation checks inline
/plugin marketplace add owner/repo
/plugin install my-plugin@my-marketplace
/plugin install my-plugin --marketplace owner/repo  # adds the marketplace first; v2.1.275+
/plugin uninstall my-plugin@my-marketplace
/plugin disable my-plugin@my-marketplace
/plugin enable my-plugin@my-marketplace
/plugin marketplace update my-marketplace
/plugin marketplace list
/plugin marketplace remove my-marketplace   # also uninstalls its plugins
```

Shortcuts: `/plugin market` works in place of `/plugin marketplace`, and `rm` in place of
`remove`.

`/plugin` with no recognised subcommand opens an interactive, tabbed panel in the terminal
CLI. Press **Tab** to move between tabs. `/plugin list` runs without opening it.
`/plugin install` opens only that plugin's details view, to pick an install scope — not the
full manager. `/plugin disable`, `/plugin enable` and `/plugin uninstall` open the panel on
the **Installed** tab at that plugin and make the change there. **[UNCONFIRMED]** Whether
**Shift+Tab** cycles the tabs backward, and whether the panel then stays open until **Esc**
closes it: neither is stated in the docs. `/plugin validate` prints its report inline, and
`/plugin marketplace add <source>` reports its result and `/plugin marketplace list` prints
inline; `/plugin marketplace update` and `/plugin marketplace remove` open the
**Marketplaces** tab. Settings that exist only in the panel — enabling auto-update for a
marketplace, for instance — have no documented CLI equivalent; the panel is the per-user
way to change them (administrators can instead set `"autoUpdate": true` on an
`extraKnownMarketplaces` entry in managed settings).

Install scopes: **User** (all your projects), **Project** (all collaborators — writes
`.claude/settings.json`), **Local** (you, this repo only).

Synced plugins have IDs of the form `<name>@synced`, and no marketplace can be named `inline`, `skills-dir`, or `synced`. To turn off every synced plugin on a machine, set `syncClaudeAiPlugins` to `false` in your user settings; an organization can set it in managed settings. (`<name>` is a plain word in angle brackets, not a substitution.)

---

## Versioning and Updates
- If `version` is set in `plugin.json`, users **only receive an update when you bump it**,
  except for a plugin with a `command` source or one loaded in place. If both `plugin.json` and the marketplace entry set it, `plugin.json` wins.
- If no version is set, git-sourced plugins are versioned by commit SHA.
- **Auto-update:** `claude-plugins-official` and most other official Anthropic marketplaces
  have it enabled by default; third-party and local marketplaces have it disabled by
  default. Toggle it per marketplace via `/plugin` → Marketplaces → *Enable/Disable
  auto-update*. **[UNCONFIRMED]** Whether `/plugin marketplace update` — a one-off
  catalogue refresh — leaves this setting untouched: the docs describe the two as separate
  mechanisms but never state that refreshing doesn't change the toggle.
- Updates are checked in the background after session start; the running session keeps the
  version it loaded and is told to `/reload-plugins`.
- Rename or retire a plugin with the marketplace `renames` map (`"old": "new"` or
  `"old": null`) — installs migrate automatically.

---

## Distribution
1. **Your own marketplace** — a public (or private) GitHub repo containing
   `marketplace.json`. You control releases completely.
2. **The community marketplace** (`anthropics/claude-plugins-community`, installed as
   `@claude-community`) — submit via platform.claude.com/plugins/submit (individuals) or
   claude.ai/admin-settings/directory/submissions/plugins/new (Team/Enterprise). Run
   `claude plugin validate` locally before you submit; listed plugins are, in nearly every
   case, pinned to a specific commit SHA. **[UNCONFIRMED]** Whether submissions also get
   automated safety screening, and whether CI bumps the pin as you push: the docs say neither.
3. **The official marketplace** (`claude-plugins-official`) does not take submissions through
   these forms — they do NOT add to it. If you work with an Anthropic partner contact, ask them about a listing.

Position by outcomes, not features:
```
"Sets up a complete project workspace in seconds instead of 30 minutes manual setup."
not "A folder containing YAML frontmatter that calls our MCP server tools."
```

---

## Converting Standalone Config to Plugin

```bash
mkdir -p my-plugin/.claude-plugin     # then create plugin.json
cp -r .claude/skills my-plugin/
cp -r .claude/agents my-plugin/
# Migrate hooks from settings.json → my-plugin/hooks/hooks.json
claude --plugin-dir ./my-plugin
```

After migrating and confirming the plugin works, delete the originals from `.claude/`.
Skills and agents do not collide while both exist — plugin skills and agents carry the
`my-plugin:` prefix, so `reviewer` and `my-plugin:reviewer` are two subagents — but hooks
have no prefix, so a hook left in both `settings.json` and `hooks/hooks.json` runs twice.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `/plugin` fails at a shell prompt, or replies `/plugin isn't available in this environment` | `/plugin` runs only inside an interactive Claude Code terminal session, not at a shell prompt or in `claude -p` or the Agent SDK. Run `claude` and type it there, or install from your shell with `claude plugin install <plugin>@<marketplace>` |
| Plugin not loading | Check structure: all dirs at plugin root, not inside `.claude-plugin/`. Check the `/plugin` Errors tab |
| Skills not appearing after install | Run `/reload-plugins` or restart |
| Users not receiving an update | `version` not bumped, or their marketplace has auto-update off |
| Plugin agent ignores `permissionMode`/`hooks`/`mcpServers` | Not supported for plugin agents — use `tools`/`disallowedTools` |
| LSP "executable not found" | Install the language server binary |

---

## DO / DO NOT

- DO: put all directories at plugin root (not inside `.claude-plugin/`)
- DO: use `<dollar>{CLAUDE_PLUGIN_ROOT}` for plugin-relative paths
- DO: run `claude plugin validate` and test with `--plugin-dir` before publishing
- DO: bump `version` on every release you want users to receive
- DO NOT: include components inside `.claude-plugin/`
- DO NOT: rely on `permissionMode`, `hooks` or `mcpServers` in plugin agents
- DO NOT: trust unverified plugins — they can execute arbitrary code
