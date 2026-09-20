# Changelog

All notable changes to Claudit. Versions follow [semantic versioning](https://semver.org/).
Guidance changes (a reference skill corrected against the live docs) are listed under
**Guidance**, with the doc page that changed.

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
