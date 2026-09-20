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

Verified on 2026-09-19 against code.claude.com/docs/en/plugins, /plugins-reference,
/plugin-marketplaces, /discover-plugins and /sub-agents — 2 open claims, each marked
**[UNCONFIRMED]** inline. The `/plugin` command list was re-checked against /discover-plugins
on 2026-09-20.

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

Inside plugin skill content, `${CLAUDE_PLUGIN_ROOT}` (install directory) and
`${CLAUDE_PLUGIN_DATA}` (persistent data directory that survives updates) are substituted,
as is `${CLAUDE_SKILL_DIR}` (the skill's own subdirectory).
---

## Agents in Plugins
Plugin agents support only: `name`, `description`, `model`, `effort`, `maxTurns`,
`tools`, `disallowedTools`, `skills`, `memory`, `background`, `omitClaudeMd`, and
`isolation` (the only valid value is `"worktree"`).

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
  "database-tools": {
    "command": "${CLAUDE_PLUGIN_ROOT}/servers/db-server",
    "args": ["--config", "${CLAUDE_PLUGIN_ROOT}/config.json"],
    "env": {
      "DB_URL": "${DB_URL}"
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
claude --plugin-dir ./my-plugin             # load without installing
claude --plugin-dir ./plugin-one --plugin-dir ./plugin-two
claude plugin init my-tool                  # scaffold a skills-directory plugin
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

```bash
/plugin                                     # open the plugin manager UI (menu — ignores arguments)
/plugin marketplace add owner/repo
/plugin install my-plugin@my-marketplace
/plugin uninstall my-plugin@my-marketplace
/plugin disable my-plugin@my-marketplace
/plugin enable my-plugin@my-marketplace
/plugin marketplace update my-marketplace
/plugin marketplace list
/plugin marketplace remove my-marketplace   # also uninstalls its plugins
```

`/plugin` on its own opens an interactive, tabbed panel in the terminal CLI. Cycle the tabs
with **Tab**, or **Shift+Tab** to go backward. Settings that live only in that panel — such as
enabling auto-update for a marketplace — have to be changed by navigating it.
**[UNCONFIRMED]** The docs describe `/plugin` only as interactive, and do not say what a bare
`/plugin` does with trailing arguments; observed behaviour is that they are ignored, so a
setting cannot be changed by typing it as an argument.

Install scopes: **User** (all your projects), **Project** (all collaborators — writes
`.claude/settings.json`), **Local** (you, this repo only).

---

## Versioning and Updates
- If `version` is set in `plugin.json`, users **only receive an update when you bump it**,
  except for a plugin with a `command` source or one loaded in place. If both `plugin.json` and the marketplace entry set it, `plugin.json` wins.
- If no version is set, git-sourced plugins are versioned by commit SHA.
- **Auto-update:** `claude-plugins-official` and most other official Anthropic marketplaces
  have it enabled by default. **Third-party and local marketplaces have it disabled by
  default** — users enable it in `/plugin` → Marketplaces, or run
  `/plugin marketplace update`.
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
   claude.ai/admin-settings/directory/submissions/plugins/new (Team/Enterprise). Submissions
   pass `claude plugin validate` plus automated safety screening; approved plugins are
   pinned to a commit SHA and CI bumps the pin as you push.
3. **The official marketplace** (`claude-plugins-official`) is curated by Anthropic at its
   discretion. There is no application process; the submission forms do NOT add to it.

Inclusion in a directory does not grant rights to Anthropic's names or marks **[UNCONFIRMED]**.

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

After migrating, remove the original **agents/** files from `.claude/` — project and user
agent definitions OVERRIDE same-named plugin agents. Original **skills** do not need
removing: plugin skills are namespaced, so both remain available.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `/plugin` command not found | Update Claude Code: `npm install -g @anthropic-ai/claude-code@latest`, or re-run the native installer |
| Plugin not loading | Check structure: all dirs at plugin root, not inside `.claude-plugin/`. Check the `/plugin` Errors tab |
| Skills not appearing after install | Run `/reload-plugins` or restart |
| Users not receiving an update | `version` not bumped, or their marketplace has auto-update off |
| Plugin agent ignores `permissionMode`/`hooks`/`mcpServers` | Not supported for plugin agents — use `tools`/`disallowedTools` |
| LSP "executable not found" | Install the language server binary |

---

## DO / DO NOT

- DO: put all directories at plugin root (not inside `.claude-plugin/`)
- DO: use `${CLAUDE_PLUGIN_ROOT}` for plugin-relative paths
- DO: run `claude plugin validate` and test with `--plugin-dir` before publishing
- DO: bump `version` on every release you want users to receive
- DO NOT: include components inside `.claude-plugin/`
- DO NOT: rely on `permissionMode`, `hooks` or `mcpServers` in plugin agents
- DO NOT: trust unverified plugins — they can execute arbitrary code
