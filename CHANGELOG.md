# Changelog

All notable changes to Claudit. Versions follow [semantic versioning](https://semver.org/).
Guidance changes (a reference skill corrected against the live docs) are listed under
**Guidance**, with the doc page that changed.

## [0.2.1] — 2026-09-25

Guidance-only release. No agent, command or hook behaviour changed. All 16 documentation pages
that Claudit's doc-watch tracks were re-verified claim by claim against the live docs on
2026-09-25. Their recorded hashes and verification dates in `maintainer/sources.json` were
re-baselined, the verification stamps at the top of nine reference skills were re-dated and
their open-claim counts corrected, and the Claude Code changelog is reviewed through 2.1.282.

### Guidance
- skill-builder — corrected the audit note on skills that run shell commands when loaded. Such
  commands never prompt: a matching deny rule, or any command whose permission check is not
  allow outside auto mode, aborts the skill unless its `allowed-tools` pre-approves it. In auto
  mode the skill loads with an instruction for Claude to run it, and it still aborts in a
  forked skill that sets an agent or in a session with no shell tool. Also documented
  claude.ai skill sync in terminal sessions (v2.1.273+), its opt-out setting
  `syncClaudeAiSkills`, the `/anthropic-skills:<name>` full name, and a fuller note on the
  reserved `synced` folder name.
- permission-rules — documented `!` negation patterns in Read/Edit deny and ask rules
  (same-source only, and ignored if listed first), the write-check that now applies to `tee`
  targets (v2.1.269+), and how allow and deny rules treat symlinks. The line saying deny rules
  cannot carry exceptions is now qualified, and the reason network paths cannot be added as
  working directories is corrected.
- plugin-builder — documented synced plugin IDs (`<name>@synced`) and the `syncClaudeAiPlugins`
  opt-out. Corrected the migration note: plugin skills and agents carry a plugin prefix so the
  originals do not collide, but hooks left in both places run twice. Updated the `/plugin`
  panel description to the now-documented per-command behaviour, trimmed the community and
  official marketplace notes to what the docs state, and corrected the "/plugin not found"
  troubleshooting row.
- mcp-config — project `.mcp.json` servers need approval only in interactive sessions.
  Corrected the http transport description.
- agent-builder — corrected the dontAsk and `--agent` main-session Tools rows, and marked one
  unverified AskUserQuestion claim.
- output-style-builder — corrected the plugin force-for-plugin conflict note and a
  troubleshooting row.
- troubleshooting — corrected the folder-naming note and three rows, and removed three markers
  the docs now settle.
- audit — removed an undocumented user-scope settings file from the inventory.
- Reviewer agents — marked or softened unverified claims in the agents, permissions and
  supply-chain reviewers.

### Repository
- `maintainer/sources.json` — baselines updated for all 16 watched pages.

## [0.2.0] — 2026-09-24

Every agent and skill was re-checked against the current Claude Code docs, which have
changed since 0.1.1. The verification stamps at the top of each reference skill keep their
existing dates, and the daily doc-watch drift issues remain open, to be handled by the normal
`/doc-check` flow.

### Added
- The config reviewer now warns when a gate-style hook would fail open (missing script, crash
  or timeout), flags `enableAllProjectMcpServers: true` and MCP servers that download and run
  code, and checks the settings keys that run a command (`statusLine`, `apiKeyHelper`,
  `awsAuthRefresh`, `otelHeadersHelper`).
- `/claudit:audit` now also sends agent and skill files that declare hooks or MCP servers to
  the config reviewer.
- `/claudit:build` now flags `hooks`, `mcpServers` or `permissionMode` fields in a drafted file
  on the build card.
- hooks-builder — guidance that a blocking hook fails open when its script cannot run.

### Fixed
- `/claudit:about` — the reference-status search matched unrelated lines and could not show
  "not yet audited". It now matches only the verification stamp lines.
- `/claudit:build` — the restart note now covers the first skill in a new skills folder.
- claudit-permissions-reviewer — stopped overclaiming what an allow rule with a
  `Tool(param:value)` specifier does, and about `cd … &&` compounds.
- claudit-config-reviewer — a hook that exits 0 with a JSON deny decision does block; the
  reviewer no longer flags it.
- claudit-supply-chain-reviewer — can no longer load other skills (the Skill tool was removed),
  and its output can now label convention findings.
- claudit-agents-reviewer — unrecognised frontmatter keys are reported as ignored by Claude
  Code, and TaskOutput is no longer treated as a stripped tool.

### Guidance
Reference skills corrected against the current Claude Code docs:
- agent-builder — the docs no longer list TaskOutput among the tools stripped from subagents;
  `LSP` added to the background subagent tool list; `subagent_type` is optional (it falls back
  to general-purpose, and only errors when the session has no general-purpose subagent);
  frontmatter hooks need workspace trust only for project-level agents, not user-level or
  `--agents` ones.
- skill-builder — unrecognised frontmatter keys are ignored without an error (the docs now say
  so); the angle-bracket escaping note now applies to claude.ai-synced skills only; placeholder
  tokens are written so they are not rewritten when the skill loads.
- hooks-builder — HTTP webhook example fixed (no `method` field; `allowedEnvVars` is needed for
  header variables); the exit-code table now notes the WorktreeCreate/WorktreeRemove
  exceptions; plugin hooks live in `hooks/hooks.json` (or inline in `plugin.json`); the
  block-dangerous-command examples now match `git push --force`; shell examples quote file
  paths so filenames with spaces work; event-name casing is no longer asserted.
- plugin-builder — the plugin-agent supported-fields list corrected (adds `color`,
  `experimental`; `initialPrompt` is unsupported); the `.mcp.json` example uses the
  `mcpServers` wrapper; admins can set `autoUpdate` in managed settings.
- mcp-config — the `.mcp.json` example uses the `mcpServers` wrapper; placeholder tokens are
  written so they are not rewritten on load.
- permission-rules and troubleshooting — denial-message wording no longer over-claimed; new
  notes that a Bash deny/ask rule matches command text, not the program, and that path rules
  on Write, NotebookEdit, Glob and MultiEdit are never consulted.

### Repository
- Added `.gitattributes` so line endings are stored as LF.
- `maintainer/sources.json` — two rubric pages added with no baseline yet (they back no
  shipped file).

## [0.1.1] — 2026-09-20

### Fixed
- `/claudit:about` — step 3's reference-file search used a Grep glob that silently matched
  nothing, so the skill reported every reference as unaudited. It now also treats zero
  matches as a search failure rather than a clean result.

### Guidance
- `/setup` — troubleshooting's Diagnostic Commands gained `claude doctor` and `claude update`,
  plus a new Updating note: native installs auto-update, Homebrew/WinGet/apt/dnf/apk do not,
  and npm installs should run `npm install -g @anthropic-ai/claude-code@latest` rather than
  `npm update -g`, which respects the semver range from the original install. Verified against
  code.claude.com/docs/en/setup on 2026-09-20.
- `/discover-plugins` — plugin-builder's Marketplaces command block gained bare `/plugin`,
  `uninstall`, `disable` and `enable`, plus a note that `/plugin` opens an interactive tabbed
  panel cycled with Tab / Shift+Tab. One new claim is marked **[UNCONFIRMED]** inline; the
  stamp line now counts 2 open claims (was 1).
- The maintainer watch list (`maintainer/sources.json`) added code.claude.com/docs/en/setup.md
  as a 16th watched page, since the troubleshooting update above depends on it and nothing was
  watching it before.

## [0.1.0] — 2026-09-19

### Added
- `/claudit:audit` — reviews permission rules, hooks/MCP/settings, agents and skills, and
  installed third-party components; proposes fixes as plain-English change cards.
- `/claudit:build` — designs and writes a new agent or skill, with approval before any file
  is written.
- `/claudit:about` — installed version, guidance verification dates, update check.

### Guidance
- Every reference skill verified claim by claim against the Claude Code docs (15 pages) on
  2026-09-19. Claims the docs don't settle are marked **[UNCONFIRMED]** inline.
