#!/usr/bin/env python3
"""Claudit repo checks — run in CI on every pull request. Standard library only.

Fails (exit 1) on:
  - invalid JSON in marketplace.json / plugin.json / sources.json
  - plugin.json version that is not semver
  - a plugin agent declaring permissionMode, hooks or mcpServers (unsupported in plugins)
  - an agent or skill missing name/description frontmatter
  - a `skills:` preload that names a skill not shipped in the plugin
  - live bang-backtick syntax (executes when a skill loads)
  - a personal-data marker, if LEAK_PATTERNS is set (comma-separated, from a repo secret)

Usage: python maintainer/check_repo.py [--base-version X.Y.Z]
  --base-version  fail unless plugin.json's version is greater (used on release PRs)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "claudit"
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
# A bang immediately followed by a backtick, at line start or after whitespace.
LIVE_BANG = re.compile(r"(^|\s)!`", re.MULTILINE)
FORBIDDEN_AGENT_FIELDS = ("permissionMode", "hooks", "mcpServers")
TEXT_SUFFIXES = {".md", ".json", ".py", ".yml", ".yaml", ".txt"}

errors: list[str] = []


def err(msg: str) -> None:
    errors.append(msg)


def frontmatter(text: str) -> dict[str, str]:
    """Top-level keys of a YAML frontmatter block (enough for presence checks)."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    keys: dict[str, str] = {}
    block = text[3:end].split("\n")
    for i, line in enumerate(block):
        m = re.match(r"^([A-Za-z][\w-]*):\s*(.*)$", line)
        if m:
            key, value = m.group(1), m.group(2).strip()
            if value in ("", ">-", "|", ">"):
                # collect indented continuation / list lines
                cont = []
                for nxt in block[i + 1:]:
                    if nxt.startswith((" ", "\t")):
                        cont.append(nxt.strip())
                    else:
                        break
                value = "\n".join(cont)
            keys[key] = value
    return keys


def load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        err(f"{path.relative_to(ROOT)}: invalid JSON ({exc})")
        return None


def version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-version")
    args = ap.parse_args()

    load_json(ROOT / ".claude-plugin" / "marketplace.json")
    load_json(ROOT / "maintainer" / "sources.json")
    manifest = load_json(PLUGIN / ".claude-plugin" / "plugin.json") or {}
    version = str(manifest.get("version", ""))
    if not SEMVER.match(version):
        err(f"plugin.json: version {version!r} is not X.Y.Z")
    elif args.base_version and SEMVER.match(args.base_version):
        if version_tuple(version) <= version_tuple(args.base_version):
            err(f"plugin.json: version {version} must be greater than {args.base_version} "
                "or users will not receive this release")

    skill_names = {p.parent.name for p in (PLUGIN / "skills").glob("*/SKILL.md")}

    for agent in sorted((PLUGIN / "agents").glob("**/*.md")):
        rel = agent.relative_to(ROOT)
        fm = frontmatter(agent.read_text(encoding="utf-8"))
        for field in ("name", "description"):
            if not fm.get(field):
                err(f"{rel}: missing frontmatter '{field}'")
        for field in FORBIDDEN_AGENT_FIELDS:
            if field in fm:
                err(f"{rel}: '{field}' is not supported in plugin agents")
        for line in fm.get("skills", "").split("\n"):
            name = line.lstrip("- ").strip()
            if not name:
                continue
            bare = name.split(":", 1)[1] if name.startswith("claudit:") else name
            if bare not in skill_names:
                err(f"{rel}: skills preload '{name}' is not a skill in this plugin")

    for skill in sorted((PLUGIN / "skills").glob("*/SKILL.md")):
        rel = skill.relative_to(ROOT)
        fm = frontmatter(skill.read_text(encoding="utf-8"))
        if not fm.get("description"):
            err(f"{rel}: missing frontmatter 'description'")
        if len(fm.get("description", "")) > 1536:
            err(f"{rel}: description exceeds the 1536-character listing cap")

    patterns = [p.strip().lower() for p in os.environ.get("LEAK_PATTERNS", "").split(",") if p.strip()]
    if not patterns:
        print("note: LEAK_PATTERNS not set — personal-data scan skipped")

    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or ".git" in path.parts or path.suffix not in TEXT_SUFFIXES:
            continue
        rel = path.relative_to(ROOT)
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix == ".md" and PLUGIN in path.parents and LIVE_BANG.search(text):
            err(f"{rel}: live bang-backtick syntax — it would execute when loaded")
        lowered = text.lower()
        for pat in patterns:
            if pat in lowered:
                line_no = lowered[: lowered.index(pat)].count("\n") + 1
                err(f"{rel}:{line_no}: personal-data marker found (pattern #{patterns.index(pat) + 1})")

    for e in errors:
        print(f"FAIL  {e}")
    print(f"{len(errors)} problem(s)." if errors else "All checks passed.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
