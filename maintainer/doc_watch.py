#!/usr/bin/env python3
"""Claudit doc watcher.

Detects when a Claude Code documentation page that Claudit's guidance depends on has
changed since that guidance was last verified, and when Claude Code has released new
versions since the changelog was last reviewed. Standard library only; no LLM.

It never edits the repo. It reports, and (with --github) opens or updates one
`doc-drift` issue per changed source so the maintainer can re-verify the dependent files.

Usage:
  python maintainer/doc_watch.py                 # report only (dry run)
  python maintainer/doc_watch.py --print-hashes  # print current hashes, to record a baseline
  python maintainer/doc_watch.py --github        # report + open/update issues
                                                 #   (needs GITHUB_TOKEN, GITHUB_REPOSITORY)
  python maintainer/doc_watch.py --sources PATH  # use a different sources file (for tests)
Exit codes: 0 = ran (changes or not); 1 = every source was unreachable; 2 = bad sources.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

SOURCES = Path(__file__).with_name("sources.json")
USER_AGENT = "claudit-doc-watch/1.0 (+https://github.com/brandoncox-commits/claudit)"
LABEL = "doc-drift"
TIMEOUT = 30
MAX_BYTES = 8 * 1024 * 1024


def fetch(url: str) -> str:
    # urllib honours file:// and ftp:// as well as http(s). Every legitimate source here is
    # a public https doc page, so anything else is refused rather than fetched — a bad or
    # tampered sources.json must not be able to make this read local files.
    if not url.lower().startswith("https://"):
        raise ValueError(f"refusing non-https source: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        if resp.url.lower().split("?", 1)[0].startswith("http://"):
            raise ValueError(f"refusing redirect to plain http: {resp.url}")
        raw = resp.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError(f"source exceeds {MAX_BYTES} bytes: {url}")
        return raw.decode("utf-8", errors="replace")


def normalise(text: str) -> str:
    """Remove formatting noise so only content changes alter the hash."""
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    out: list[str] = []
    for line in lines:
        if not line and out and not out[-1]:
            continue  # collapse runs of blank lines
        out.append(line)
    return "\n".join(out).strip() + "\n"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


VERSION_HEADING = re.compile(r"^##\s+\[?v?(\d+\.\d+\.\d+)\]?", re.MULTILINE)


def changelog_versions(text: str) -> list[str]:
    """Version headings in file order (newest first in the Claude Code changelog)."""
    return VERSION_HEADING.findall(text)


def slug(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".md")


# ---------------------------------------------------------------- GitHub (optional)

def gh_request(method: str, path: str, token: str, body: dict | None = None) -> object:
    url = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def ensure_label(repo: str, token: str) -> None:
    try:
        gh_request("GET", f"/repos/{repo}/labels/{LABEL}", token)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        gh_request("POST", f"/repos/{repo}/labels", token, {
            "name": LABEL, "color": "d93f0b",
            "description": "A doc page Claudit depends on changed since last verification",
        })


def open_issues(repo: str, token: str) -> dict[str, dict]:
    issues = gh_request("GET", f"/repos/{repo}/issues?state=open&labels={LABEL}&per_page=100", token)
    return {i["title"]: i for i in issues or [] if "pull_request" not in i}


def upsert_issue(repo: str, token: str, existing: dict[str, dict], title: str, body: str) -> str:
    if title in existing:
        num = existing[title]["number"]
        if existing[title].get("body") != body:
            gh_request("PATCH", f"/repos/{repo}/issues/{num}", token, {"body": body})
            return f"updated #{num}"
        return f"unchanged #{num}"
    created = gh_request("POST", f"/repos/{repo}/issues", token,
                         {"title": title, "body": body, "labels": [LABEL]})
    return f"opened #{created['number']}"


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--github", action="store_true", help="open/update doc-drift issues")
    ap.add_argument("--print-hashes", action="store_true", help="print current hashes as JSON")
    ap.add_argument("--sources", type=Path, default=SOURCES,
                    help="path to a sources.json (default: the one beside this script). "
                         "Lets the watcher be tested without editing the real file.")
    args = ap.parse_args()

    sources_path: Path = args.sources
    try:
        sources = json.loads(sources_path.read_text(encoding="utf-8"))
        pages: dict = sources["pages"]
    except (OSError, ValueError, KeyError) as exc:
        print(f"ERROR: cannot read {sources_path}: {exc}", file=sys.stderr)
        return 2

    changed: list[tuple[str, dict, str]] = []   # (url, entry, new_hash)
    no_baseline: list[tuple[str, str]] = []
    unreachable: list[tuple[str, str]] = []
    current: dict[str, str] = {}

    for url, entry in pages.items():
        try:
            digest = sha256(normalise(fetch(url)))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            unreachable.append((url, str(exc)))
            continue
        current[url] = digest
        recorded = entry.get("sha256")
        if recorded is None:
            no_baseline.append((url, digest))
        elif recorded != digest:
            changed.append((url, entry, digest))

    new_versions: list[str] = []
    cl = sources.get("changelog") or {}
    if cl.get("url"):
        try:
            versions = changelog_versions(fetch(cl["url"]))
            last = cl.get("last_reviewed_version")
            if versions:
                current["changelog_latest"] = versions[0]
                if last and last in versions:
                    new_versions = versions[: versions.index(last)]
                elif last is None:
                    no_baseline.append((cl["url"], versions[0]))
                else:
                    new_versions = versions[:10]
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            unreachable.append((cl["url"], str(exc)))

    if args.print_hashes:
        print(json.dumps(current, indent=2))
        return 0

    # ---- report
    print(f"Checked {len(pages)} doc pages + changelog.")
    for url, entry, digest in changed:
        print(f"CHANGED  {url}  (verified {entry.get('verified')}) -> re-check: {', '.join(entry.get('covers', []))}")
    if new_versions:
        print(f"CHANGELOG  new Claude Code versions since last review: {', '.join(new_versions)}")
    for url, digest in no_baseline:
        print(f"NO BASELINE  {url}  current={digest}")
    for url, err in unreachable:
        print(f"UNREACHABLE  {url}  ({err})")
    if not changed and not new_versions:
        print("No drift detected.")

    if args.github and (changed or new_versions):
        token = os.environ.get("GITHUB_TOKEN")
        repo = os.environ.get("GITHUB_REPOSITORY")
        if not token or not repo:
            print("ERROR: --github needs GITHUB_TOKEN and GITHUB_REPOSITORY", file=sys.stderr)
            return 1
        ensure_label(repo, token)
        existing = open_issues(repo, token)
        for url, entry, digest in changed:
            title = f"doc-drift: {slug(url)}"
            body = (
                f"The page **{url}** no longer matches the version Claudit's guidance was "
                f"verified against (verified: {entry.get('verified')}).\n\n"
                f"**Re-check these files:**\n" +
                "".join(f"- `plugins/claudit/{c}`\n" for c in entry.get("covers", [])) +
                f"\nRecorded hash: `{entry.get('sha256')}`\nCurrent hash: `{digest}`\n\n"
                "When the dependent files are re-verified, update this page's `sha256` and "
                "`verified` in `maintainer/sources.json` in the same release PR."
            )
            print(f"  {title}: {upsert_issue(repo, token, existing, title, body)}")
        if new_versions:
            title = "doc-drift: claude-code changelog"
            body = (
                "Claude Code has released versions since the changelog was last reviewed:\n\n" +
                "".join(f"- {v}\n" for v in new_versions) +
                f"\nSource: {cl.get('url')}\n\nReview for new features or behaviour changes "
                "Claudit's guidance should cover, then set `changelog.last_reviewed_version` "
                "in `maintainer/sources.json`."
            )
            print(f"  {title}: {upsert_issue(repo, token, existing, title, body)}")

    reachable = len(pages) + (1 if cl.get("url") else 0) - len(unreachable)
    return 1 if reachable == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
