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
3. Grep for the verification stamp lines — pattern `^Verified` — to get each reference's
   verification status. Search the directory `${CLAUDE_PLUGIN_ROOT}/skills` and do **not**
   pass a `*/SKILL.md` glob alongside it. Keep only hits in a `SKILL.md`.
   If the search returns no matches at all, treat that as a search failure, not as evidence
   that the references are unverified: say the check could not be completed and why. Every
   shipped reference carries a stamp, so zero matches always means the search was wrong.
   Show a small table: reference · status (verified / partially verified / not yet audited) ·
   date. Read the DATE from each stamp rather than assuming it matches the plugin version.
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
    claude plugin update claudit@claudit   (run in your terminal)
  then run /reload-plugins (or start a new session).

What changed:  https://github.com/brandoncox-commits/claudit/blob/main/CHANGELOG.md
Report a problem:  https://github.com/brandoncox-commits/claudit/issues
```

If the installed version is older than the latest, lead with that and the update command.
