---
name: build
description: >-
  Build a new Claude Code agent or skill from a plain-English request, following
  Anthropic's documented guidance and the user's own conventions. Shows a
  plain-English build card ("build an agent that does X — it can read files, it
  cannot edit them") and writes nothing until the user approves. Use when the user
  runs /claudit:build or asks Claudit to create, make or design an agent or skill.
argument-hint: "<what you want it to do>"
disable-model-invocation: true
allowed-tools: Read, Glob, Grep
---

# Claudit — build

Build a new agent and/or skill for: **$ARGUMENTS**

You orchestrate in the main conversation. `claudit-designer` drafts; a different agent
reviews the draft; the user approves a plain-English card; only then do you write files.

If `$ARGUMENTS` is empty, ask the user what they want built and stop until they answer.

If the `AskUserQuestion` tool is unavailable (e.g. `dontAsk` mode), show the build card
and drafted files but **write nothing**, and say why.

## Step 1 — scope

Ask with `AskUserQuestion` where it should live:

- **This project** — `<cwd>/.claude/` — only available when working in this folder
- **Everywhere (user)** — `~/.claude/` — available in every project

Put the option that fits the request first and mark it (Recommended): project-specific
work → project; general-purpose helpers → user.

## Step 2 — design

Every agent in this skill gets only the fields listed for it, and nothing else. Add no
hints, examples or expectations of your own; the reviewers must judge the draft
independently.

Spawn `claudit:claudit-designer` (retry once with the bare name `claudit-designer` if the
scoped name is rejected) with:

```
request: <the user's request, verbatim>
scope: user | project
target_dir: <absolute path>
platform: <win32 | darwin | linux>
answers: <replies to earlier questions, or empty>
```

If it returns `NEEDS_REVIEW` with `questions`, ask them with `AskUserQuestion` (its
recommended option first), then send the answers back to the designer.

## Step 3 — independent review

Spawn `claudit:claudit-agents-reviewer` with:

```
mode: build-review
platform: <win32 | darwin | linux>
draft:
  - path: <path>
    content: <complete drafted contents>
previous_findings: <the last round's findings, or empty>
```

The designer does not review its own work.

- `PASS` → continue.
- `CHANGES_NEEDED` → send the findings to the designer (continuing the same designer is
  fine), then review the new draft with a **fresh** reviewer: a new agent, not a
  continued one, given the new draft and the previous findings. At most two rounds; after
  that, continue but show the outstanding findings on the card.
- Before sending any reviewer-proposed edit to the designer, check it would leave the file
  valid (no duplicate frontmatter key). If it wouldn't, describe the problem to the
  designer instead of passing the broken edit.

## Step 4 — permissions (only if `permissions_needed` is non-empty)

Spawn `claudit:claudit-permissions-reviewer` with:

```
mode: design
platform: <win32 | darwin | linux>
commands:
  - <exact command form>
scope: user | project
settings_file: <absolute path>
```

It returns rule text and a plain-English effect statement, including who else would gain
the capability. Before showing it, `Read` the settings file and check by eye that its edit
would leave the JSON valid: a comma between entries, none after the last, brackets and
quotes balanced. **Never run a shell command or script to check it.**

## Step 5 — the build card and approval

Show:

1. The designer's build card, unchanged.
2. The files that will be created (paths), and a collapsed view of their contents if the
   user wants to read them.
3. Any permission rules, each with its plain-English effect statement.
4. Any outstanding review findings.

Ask with `AskUserQuestion`:
- **Build it (Recommended)** — write the files and add the permission rules shown
- **Build it without the permission rules** — Claude Code will ask each time the new
  component runs a command (only offer when there are rules)
- **Change something** — ask what, then return to Step 2 with the change
- **Cancel** — write nothing

## Step 6 — write

1. For every path: check it does **not** already exist. If it does, stop and ask — never
   overwrite.
2. Write each file with the drafted contents, exactly, with one correction: agent results
   can arrive with `<`, `>` and `&` encoded as `&lt;`, `&gt;` and `&amp;`. The designer
   never writes those on purpose, so decode them before writing (and before sending a
   draft to the reviewer in Step 3). Then `Read` each written file and confirm that no
   `&lt;`, `&gt;` or `&amp;` remains and the frontmatter opens and closes with `---`. If
   either check fails, delete nothing, show the user the problem and stop.
3. If approved, add each permission rule to the named settings file with `Edit`, then
   re-read it with `Read` and check it by eye: a comma between entries, none after the
   last, brackets and quotes balanced. **Never run a shell command or script to check it.**
   Claudit needs no shell, and a command prompt in the middle of writing files is one users
   should be able to decline. If the file is not valid, restore the original text
   immediately and report it.

## Step 7 — hand-off

Tell the user, briefly:
- what was created and where;
- how to try it (the card's "Try it" line);
- that agent and skill files are picked up automatically within seconds — except the very
  first agent in an `agents/` folder that did not exist when the session started, which
  needs a restart;
- how to remove it (delete the file(s), and the named rules).
