---
name: permission-rules
description: >-
  Reference for Claude Code permission rules in settings.json: allow/ask/deny
  precedence, specifier syntax and wildcards, Bash and PowerShell command
  matching including the AST and compound-command rules, Read/Edit gitignore
  patterns, WebFetch domains, Agent rules, scope precedence, and when edits take
  effect. Use when writing, auditing, or debugging a permission rule, or when a
  tool call is denied and the reason is unclear.
user-invocable: false
---

# Permission Rules Reference

Verified on 2026-09-25 against code.claude.com/docs/en/permissions, /permission-modes,
/sub-agents, /settings and /output-styles — 5 open claims, each marked **[UNCONFIRMED]**
inline: the PowerShell `&` call-operator behaviour (local probe only), "no per-agent allow
scoping of other tools", the exact `dontAsk` denial-message wording, what concretely happens
to an `allow` entry that uses the `Tool(param:value)` form, and whether a UNC path overrides
an explicit `allow` rule on another command.

A `dontAsk` denial often does not say *why*: some denials do name the rule (the docs say, in the WebFetch section on artifact reads, "When a rule blocks a read, the denial names the rule"), but the `dontAsk` wording is not documented. The failures observed under `dontAsk` read along the lines of
"Permission to use X has been denied because Claude Code is running in don't ask mode"
**[UNCONFIRMED wording]**, regardless of whether the rule is missing, malformed, or shadowed by a deny.
**Never diagnose from the message text — diagnose from the rule.**

---

## Precedence

`deny` → `ask` → `allow`. Evaluated in that order, across every scope.

- A broad deny like `Bash(aws *)` blocks a call **even when a narrower allow matches it**.
  Deny rules cannot carry allowlist exceptions, except that a `!` negation inside a same-source `Read`/`Edit` deny or ask list carves paths out of earlier rules there (see Read and Edit).
- An `ask` rule prompts even when a more specific `allow` also matches.
- Across scopes too: a user-level deny blocks a project-level allow.

Scope precedence (highest first): managed → `--settings` → `.claude/settings.local.json` →
`.claude/settings.json` → `~/.claude/settings.json`.

**List keys merge across scopes** — `permissions.allow` from several files combine rather
than one replacing another.

---

## Rule shape

`Tool` or `Tool(specifier)`. Parentheses inside a specifier are literal and need no escaping.

`Bash(*)` is equivalent to bare `Bash`. As a **deny** rule, both forms remove the tool from
Claude's context entirely.

### You cannot match a tool's primary content field by parameter

`command` (Bash/PowerShell), `file_path` (Read/Edit/Write), `path` (Grep/Glob),
`notebook_path`, and `url` (WebFetch) are **not** matchable as `Tool(param:value)`.
`Bash(command:rm *)` is ignored with a startup warning, because a compound command would
bypass it. Use the specifier form instead: `Bash(rm *)`, `Read(./path)`,
`WebFetch(domain:host)`.

Other scalar parameters *are* matchable — but **only in `deny` and `ask` rules**:
`Bash(run_in_background:true)`, `Agent(model:opus)`, `Agent(isolation:worktree)`. Allow
rules "continue to use each tool's own specifier syntax instead," since matching one
parameter value wouldn't establish that the whole call is safe. **[UNCONFIRMED]** What
concretely happens to an allow entry written in this form — ignored, warned, or a
never-matching literal — isn't documented; don't rely on it.
One parameter per rule. A parameter the model omits never matches, so `Agent(model:*)`
misses a call that leaves `model` unset.

---

## Command matching (Bash and PowerShell)

PowerShell rules use the same shape as Bash rules. Matching is case-insensitive for
PowerShell, and common aliases are canonicalized first — `PowerShell(Get-ChildItem *)` also
matches `gci`, `ls`, and `dir`.

### The trailing-space rule — the one people get wrong

| Rule | Matches | Does not match |
|---|---|---|
| `Bash(npm run build)` | exactly `npm run build` | `npm run build --watch` |
| `Bash(npm run *)` | `npm run build`, `npm run` | `npm install` |
| `Bash(ls *)` | `ls -la`, **and bare `ls`** | `lsof` |
| `Bash(ls*)` | `ls -la`, `ls`, **and `lsof`** | |
| `Bash(* --version)` | `node --version`, any program | `node -v` |

- **The space before a trailing `*` is part of the rule.** `ls *` requires a space after
  `ls`, so `lsof` fails. `ls*` has no space, so `lsof` matches too — the no-space form is
  **looser, not tighter**. Prefer the space form.
- **A trailing ` *` also matches the bare command.** `Bash(ls *)` matches `ls` with no
  arguments.
- That only holds when the trailing `*` is the rule's **only** wildcard:
  `Bash(* --help *)` matches `npm --help x` but not `npm --help`.
- `:*` is exactly equivalent to a trailing ` *`. `Bash(ls:*)` ≡ `Bash(ls *)`. It is
  **only recognized at the end** — in `Bash(git:* push)` the colon is a literal.
- A `*` stands in for whatever is in its place, including dangerous things.
  `Bash(git * main)` matches `git -c core.fsmonitor=<script> diff main`, which runs a
  program you name. `Bash(* --version)` lets any program match.

### PowerShell and the `&` call operator **[UNCONFIRMED — local probe, not in published docs]**

**Never write `&` in a PowerShell rule or invocation.**

```
BAD   "PowerShell(& C:\\path\\script.ps1 *)"     observed never to match; silently denied
GOOD  "PowerShell(C:\\path\\script.ps1 *)"
```

Claude Code parses the PowerShell **AST** and matches each command in it. `& C:\x.ps1` is
the call operator applied to a command whose name is the bare path — the `&` is an
operator, not part of the command text, so a rule containing `& ` does not line up.

Observed 2026-09-07 on a real Windows setup: a call of the form
`& C:\...\script.ps1 -Action x` was denied against a rule that was an exact textual match
for it; rewriting the rule and the invocation in bare-path form fixed it. Use a path with
no spaces so `&` is never needed.

### Compound commands

Every subcommand must match a rule independently, or the whole call is denied.

- Bash separators: `&&`, `||`, `;`, `|`, `|&`, `&`, and newlines.
- PowerShell: `|`, `;`, and on PS7+ `&&` and `||`. Claude Code walks the AST.
- So a rule like `Bash(safe-cmd *)` does **not** authorise `safe-cmd && other-cmd`.
- Appending a second statement (e.g. an `$LASTEXITCODE` echo) to an otherwise-allowed
  script call gets the whole thing denied. **One statement per call.**
- `deny`/`ask` rules match a subcommand **anywhere**, including inside a subshell, a command
  substitution, or a `for` body. `Bash(git clean *)` in `ask` still prompts for
  `echo "$(git clean -f)"`.
- A `deny`/`ask` Bash rule matches the command text as written, not the program: `Bash(curl *)` does not stop `/usr/bin/curl …` or `sh -c 'curl …'`, and `Bash(git push *)` does not stop `git -C . push`. It is not a security boundary; the sandbox is.
- When `&&` has nothing after it (`npm test &&`), the command is unparseable and is not
  split, so `Bash(npm *)` will not approve it.

### Wrappers

Stripped before matching, so `Bash(npm test *)` also matches `timeout 30 npm test`:
`timeout`, `time`, `nice`, `nohup`, `stdbuf`, shell builtins `command` and `builtin`, zsh
`noglob`, and bare `xargs` (only with no flags — `xargs -n1 grep x` is matched as `xargs`).
Not stripped: `command -v`, zsh `nocorrect`.

Leading assignments of known-safe env vars are stripped for allow rules, so
`Bash(npm test *)` matches `NODE_ENV=test npm test`. Any other variable blocks an allow
match. Deny/ask match past **any** assignment.

**Exec wrappers cannot be prefix-approved**: `watch`, `setsid`, `ionice`, `flock`, and
`find` with `-exec`/`-delete` always prompt **in Manual mode** under a rule like
`Bash(watch *)`. Write an exact-match rule for the full command string to approve one
invocation.

### Windows UNC paths

In Manual mode, a command from the built-in read-only set still prompts when its arguments
include a network (UNC) path such as `\\server\share\file`, because accessing one can send
your Windows credentials to the host it names — the same check applies to PowerShell tool
commands. **[UNCONFIRMED]** Whether this also overrides an explicit `allow` rule on some
other command isn't documented; write the rule you need and test it.

Most network paths also can't be added as working directories at all, because looking
one up can contact the host it names — map the share to a drive letter and pass that with `--add-dir`.

### Argument-constraining rules are fragile

`Bash(curl http://github.com/ *)` looks like it restricts curl to GitHub but does not
survive variations. Prefer allowing a wrapper script you control over trying to constrain
arguments.

### Output redirects

For `> file`, `>> file`, `2> file`, the target is checked against your `Edit` allow/deny
rules, **protected paths**, and the working directories. `Bash(git commit *)` allows the
command, not the target. A target starting with `~` or containing a glob needs approval.

Claude Code also checks the files a `tee` command writes, including in a pipeline such as `make | tee build.log`. The check covers your `Edit` allow and deny rules, protected paths, and the working directories, so an allow rule such as `Bash(tee *)` doesn't cover a destination outside the working directories. Claude Code checks `tee` targets in v2.1.269 and later.

---

## Read and Edit

Both use **gitignore** pattern syntax. `*` matches within one path segment; `**` crosses
directories. Bare filenames match at any depth, so `Read(.env)` ≡ `Read(**/.env)`.

Anchors: `//` absolute (filesystem root), `~/` home, and `/` **relative to the settings
source that defines the rule** — not uniformly the working directory. A `/path` rule
resolves to the primary working directory when it comes from project settings, local
settings, a CLI flag or a session rule; to `~/.claude/path` when it comes from user
settings; and to the file's own directory when passed via `--settings <file>`. The same
rule text therefore matches different locations depending on which file holds it.

- A deny/ask rule with an unusable pattern still guards that exact path.
- An **allow** rule with an unusable pattern approves nothing.
- A path rule on `Write`, `NotebookEdit`, `Glob` or `MultiEdit` is accepted but never consulted (startup warning, v2.1.210+): write `Edit(path)` or `Read(path)`. A bare `Write` with no path still matches at tool level.
- Paths approved via "don't ask again" are escaped (`[`, `]`, `*`); rules you write are not.
- A deny or ask pattern that starts with `!` is a gitignore negation. It carves the paths it matches out of the `path` or `./path` rules listed before it. In one settings file's `deny` list, `Read(*.env)` followed by `Read(!sample.env)` blocks every file whose name ends in `.env` at any depth, except files named `sample.env`. A `!` rule listed first carves nothing out. The carve-out reaches only rules from the same source: a `Read(!.env)` in project settings or in `--disallowedTools` doesn't cancel a `Read(./.env)` deny from managed settings or any other settings file. Two limits: `!` is read relative to the current directory, so `Read(!~/notes/public/**)` carves nothing out of `Read(~/notes/**)`, and a carve-out cannot reopen a file inside a directory that a rule blocks whole.
- When Claude accesses a symlink, permission rules check two paths: the symlink itself and the file it resolves to. Allow rules apply only when both the symlink path and its target match, so a symlink inside an allowed directory that points outside it still prompts you. Deny rules apply when either the symlink path or its target matches.

---

## Cd (the `/cd` command)

`Cd` rules control which directories the `/cd` command can move the session to.
**`Cd` is not a model-invocable tool** — Claude cannot call it, so these rules apply only
when a person runs `/cd` themselves.

- A bare `Cd` deny rule disables `/cd` entirely.
- A `Cd(<path-pattern>)` deny rule blocks matching targets. Deny rules check every spelling
  of the target, including each symlink hop it resolves through, so a rule written for one
  path also blocks targets that resolve to it.
- Adding **any** `Cd` allow rule switches `/cd` into allowlist mode: the resolved target
  directory must match an allow rule, or `/cd` refuses.
- With no `Cd` rules configured, `/cd` keeps its default behaviour and prompts to trust an
  unfamiliar directory.

Path patterns share the `//`, `~/` and `/` anchors from Read and Edit, but matching is
anchored to the whole directory path rather than gitignore-style: `*` matches exactly one
segment, `**` crosses segments, and a trailing `/**` also matches its own root.

---

## WebFetch

`WebFetch(domain:example.com)` matches the hostname, case-insensitively, `*` supported,
trailing dot stripped. `domain:*.example.com` matches subdomains at any depth but **not**
`example.com` itself. A bare `WebFetch` rule and `WebFetch(domain:*)` both cover every URL
but behave differently: only the `domain:` form feeds the sandbox's allowed-domain list.

---

## Agent (subagents)

`Agent(Explore)`, `Agent(my-custom-agent)`. **Use these in `deny`** to disable a subagent —
there is no per-agent *allow* scoping of other tools **[UNCONFIRMED]**.

**This is the key architectural constraint:** permission rules are **session-wide**, and
subagents inherit the parent's rules. Adding `Edit(~/.claude/settings.json)` to `allow`
grants it to *every* agent holding `Edit`, not just the one you meant.

To give exactly one agent write access without widening the session:

- set `permissionMode: acceptEdits` in that agent's frontmatter, which auto-accepts its file
  edits in the working directory (not available to plugin-shipped agents), **and**
- keep the path out of the session-wide `allow` list, **and**
- constrain the agent by its `tools` list instead.

A child's `permissionMode` applies when the parent session is in `default`, `dontAsk`, or
`plan`, **except** a child that declares `permissionMode: bypassPermissions`, which keeps
the parent's mode instead (v2.1.267+). A parent in `auto`, `acceptEdits`, or
`bypassPermissions` **overrides the child's frontmatter** entirely, which means an
auto-mode session can make a broken allowlist look like it works.

---

## MCP

Allow rules accept tool-name globs only after a literal `mcp__<server>__` prefix; the server
segment must be glob-free. `mcp__puppeteer__*` is valid. Unanchored globs (`"*"`, `"B*"`,
`"mcp__*"`) are skipped with a warning and approve nothing.

---

## When edits take effect

**Permission edits hot-reload into the running session.** Claude Code watches settings files
and applies `permissions`, `hooks`, and credential-helper changes without a restart.
**Never tell the user to restart to pick up a permission change.**

Read once at session start, so a settings-file edit does not reach the running session:
`model`, `effortLevel`, `modelSettings` (change them live with `/model` and `/effort`
instead). A switched `outputStyle` applies from the next message (v2.1.251+).

**Agent `.md` files also hot-reload — no restart needed.** Claude Code watches
`~/.claude/agents/` and `.claude/agents/`; an added or edited file is detected within a few
seconds. A `skills:` preload change takes effect the same way. Three cases still need a
restart: a scope's first agent file in an `agents` directory that did not exist at session
start; agents under a directory added via `--add-dir`/`/add-dir` (not watched); and
sessions started with `--disable-slash-commands`. As of **v2.1.198** the `/agents` command
no longer opens an interactive wizard — it prints a reminder to edit the files directly.

`permissions.defaultMode` values `auto` and `bypassPermissions` do not take effect from
project or local settings — set them in user or managed settings.

Malformed individual entries are skipped with a Settings Warning at startup while the rest
of the file still loads, so a broken rule simply never applies. The warning is easy to
miss; nothing fails at the moment the rule should have matched.

---

## Workspace trust

`permissions.allow`, `permissions.additionalDirectories`, and most `env` values from a
*project* settings file apply only after the folder is trusted. `deny` and `ask` apply
immediately. An untracked `settings.local.json` does not wait for trust; a tracked one does.

---

## Checklist before writing a rule

1. Is the tool's content field being matched via `param:` by mistake? Use the specifier.
2. Trailing wildcard: space form ` *` unless you deliberately want prefix-glue.
3. PowerShell: no `&`, bare path, one statement.
4. Will every subcommand of the realistic invocation match?
5. Is a `deny` or `ask` rule shadowing this allow?
6. Is the rule as narrow as the job needs — no bare `Bash`, no `Tool(*)`, no leading `*`?
7. Does this belong session-wide, or should it be one agent's `acceptEdits`?
8. Verify by **executing the real command**, not by re-reading the rule.
