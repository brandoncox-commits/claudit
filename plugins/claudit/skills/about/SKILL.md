---
name: about
description: >-
  Show the installed Claudit version, when each guidance reference was last checked
  against the Claude Code docs, whether a newer version is available, and how to
  turn on automatic updates. Use when the user runs /claudit:about or asks which
  version of Claudit they have or how to update it.
disable-model-invocation: true
allowed-tools: Read, Grep, WebFetch(domain:raw.githubusercontent.com)
---

# Claudit — about

1. Read `${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json` — report `version`.
2. WebFetch
   `https://raw.githubusercontent.com/brandoncox-commits/claudit/main/plugins/claudit/.claude-plugin/plugin.json`
   asking only for the `version` field. If it fails, say so and continue.
3. Grep `${CLAUDE_PLUGIN_ROOT}/skills/*/SKILL.md` for lines containing `erified` to get
   each reference's verification status. Show a small table: reference · status
   (verified / partially verified / not yet audited) · date.
4. Report:

```
Claudit <installed version>   (latest: <latest version, or "couldn't check">)

Guidance references
  <table from step 3>

Updating
  Claudit comes from a third-party marketplace, and Claude Code turns automatic
  updates OFF for those by default. To turn them on:
    /plugin  →  Marketplaces  →  claudit  →  Enable auto-update
  To update right now:
    /plugin marketplace update claudit
  then run /reload-plugins (or start a new session).

What changed:  https://github.com/brandoncox-commits/claudit/blob/main/CHANGELOG.md
Report a problem:  https://github.com/brandoncox-commits/claudit/issues
```

If the installed version is older than the latest, lead with that and the update command.
