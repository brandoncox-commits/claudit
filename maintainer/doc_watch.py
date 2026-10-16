#!/usr/bin/env python3
"""Claudit doc watcher.

Detects when a Claude Code documentation page that Claudit's guidance depends on has
changed since that guidance was last verified, and when Claude Code has released new
versions since the changelog was last reviewed. Standard library only; no LLM.

It compares each page with a verified snapshot (section-aware diff, classified MAJOR /
MINOR by doc_classify.py) and never edits the repo, except --rebaseline, which writes
snapshots and sources.json locally (never commits, never touches GitHub).

Usage:
  python maintainer/doc_watch.py                   # dry run: classify, print digest
  python maintainer/doc_watch.py --diff SLUG       # full local diff vs the snapshot
  python maintainer/doc_watch.py --rebaseline [--only SLUG ...] [--date YYYY-MM-DD]
                                 [--migrate] [--force]
  python maintainer/doc_watch.py --print-hashes    # DEPRECATED alias, one release
  python maintainer/doc_watch.py --legacy-issues [--github]   # old per-page behaviour, frozen
  options: --sources PATH, --snapshots-dir PATH, --state-file PATH (dry run only)
  python maintainer/doc_watch.py --github          # create/update the rolling digest issue
                                                   #   (needs GITHUB_TOKEN, GITHUB_REPOSITORY)
  python maintainer/doc_watch.py --shadow          # new logic only; silent state issue
  python maintainer/doc_watch.py --close-legacy    # one-time cutover step
Exit codes: 0 = ran; 1 = every source unreachable / digest state unreadable / GitHub step
failed; 2 = bad sources.json, snapshot config or usage; 3 = rebaseline refused.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import doc_classify as dc  # noqa: E402
from doc_classify import changelog_versions, slug  # noqa: E402,F401

SOURCES = Path(__file__).with_name("sources.json")
SNAPSHOTS = Path(__file__).with_name("snapshots")
USER_AGENT = "claudit-doc-watch/1.0 (+https://github.com/brandoncox-commits/claudit)"
LABEL = "doc-drift"
TIMEOUT = 30
MAX_BYTES = 8 * 1024 * 1024
ALLOWED_HOSTS = frozenset({"code.claude.com", "platform.claude.com", "raw.githubusercontent.com"})


def legacy_fetch(url: str) -> str:
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


sha256 = dc.sha256


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


def ensure_label(repo: str, token: str, request: Callable[..., object] | None = None) -> None:
    # `request` defaults to the frozen legacy gh_request; the digest code passes its own.
    request = request or gh_request
    try:
        request("GET", f"/repos/{repo}/labels/{LABEL}", token)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        request("POST", f"/repos/{repo}/labels", token, {
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


# ---------------------------------------------------------------- legacy per-page issues
# Frozen for the shadow period (--legacy-issues); removed at cutover.

def legacy_main(args: argparse.Namespace) -> int:
    sources_path: Path = args.sources
    try:
        sources = json.loads(sources_path.read_text(encoding="utf-8"))
        pages: dict = sources["pages"]
    except (OSError, ValueError, KeyError) as exc:
        print(f"ERROR: cannot read {sources_path}: {exc}", file=sys.stderr)
        return 2

    # The one deliberate change to the frozen legacy path (maintainer decision, 2026-10-16): under
    # schema 2, sources.json `sha256` is the v2 hash of the snapshot body, so the live page
    # is hashed with normaliser v2. Schema 1 (no "schema" key) keeps the original v1 hashing.
    normalise = dc.normalise if sources.get("schema") == 2 else dc.normalise_v1

    changed: list[tuple[str, dict, str]] = []   # (url, entry, new_hash)
    no_baseline: list[tuple[str, str]] = []
    unreachable: list[tuple[str, str]] = []
    current: dict[str, str] = {}

    for url, entry in pages.items():
        try:
            digest = sha256(normalise(legacy_fetch(url)))
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
            versions = changelog_versions(legacy_fetch(cl["url"]))
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


# ---------------------------------------------------------------- errors and fetching

class FetchError(Exception):
    """No usable response (transport failure, refused URL, oversize body)."""


class FetchGone(FetchError):
    """HTTP 404 or 410: the page moved or was removed."""

    def __init__(self, message: str, status: int = 404):
        super().__init__(message)
        self.status = status


class Unstable(FetchError):
    """Repeated fetches of the same URL disagreed."""


class SnapshotError(ValueError):
    """A snapshot file is missing its header, unparsable, or cannot be written safely."""


class ConfigError(Exception):
    """sources.json is unreadable or fails schema validation (exit 2)."""


class StateError(ValueError):
    """Digest state is malformed or oversized (exit 1; leave the digest unchanged)."""


@dataclass(frozen=True)
class Fetched:
    text: str
    status: int
    declared_length: int | None
    received: int
    read_error: bool = False      # HTTPException (e.g. IncompleteRead) while reading the body


def _declared_length(headers: object) -> int | None:
    raw = headers.get("Content-Length") if headers is not None else None
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _check_https_allowed(url: str, what: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() != "https":
        raise FetchError(f"refusing non-https {what}: {url}")
    if (parts.hostname or "").lower() not in ALLOWED_HOSTS:
        raise FetchError(f"refusing {what} host not on the allowlist: {parts.hostname}")


def fetch(url: str) -> Fetched:
    """GET `url` (https and allowlisted hosts only, also after redirects)."""
    _check_https_allowed(url, "source")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            _check_https_allowed(getattr(resp, "url", url), "redirect")
            status = getattr(resp, "status", None) or 200
            declared = _declared_length(getattr(resp, "headers", None))
            read_error = False
            try:
                raw = resp.read(MAX_BYTES + 1)
            except http.client.IncompleteRead as exc:
                raw, read_error = bytes(exc.partial or b""), True
            except http.client.HTTPException:
                raw, read_error = b"", True
    except FetchError:
        raise
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 410):
            raise FetchGone(f"HTTP {exc.code}: {url}", exc.code) from exc
        raise FetchError(f"HTTP {exc.code}: {url}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        raise FetchError(f"{type(exc).__name__}: {exc}") from exc
    if len(raw) > MAX_BYTES:
        raise FetchError(f"source exceeds {MAX_BYTES} bytes: {url}")
    # A read that failed part-way never exposes its partial text: it could otherwise be
    # hashed or baselined as if it were the whole page. `received` still records the length.
    text = "" if read_error else raw.decode("utf-8", errors="replace")
    return Fetched(text, status, declared, len(raw), read_error)


def to_outcome(fetched: Fetched) -> dc.FetchOutcome:
    return dc.FetchOutcome(text=fetched.text, status=fetched.status,
                           declared_length=fetched.declared_length,
                           received=fetched.received, read_error=fetched.read_error)


def fetch_checked(url: str, snapshot_body: str | None = None, *,
                  sleep: Callable[[float], None] = time.sleep,
                  attempts: int = dc.FETCH_ATTEMPTS,
                  delay: float = dc.FETCH_RETRY_DELAY_S) -> dc.FetchOutcome:
    """Fetch, retrying anomalous responses; the last outcome is returned (and is MAJOR if
    still anomalous). 404/410 and transport failures are not retried here."""
    outcome = dc.FetchOutcome(unreachable=True, error="no attempt made")
    for attempt in range(attempts):
        if attempt:
            sleep(delay)
        try:
            fetched = fetch(url)
        except FetchGone as exc:
            return dc.FetchOutcome(text=None, status=exc.status, error=str(exc))
        except FetchError as exc:
            return dc.FetchOutcome(unreachable=True, error=str(exc))
        outcome = to_outcome(fetched)
        if not dc.detect_anomalies(outcome, dc.normalise(fetched.text), snapshot_body):
            return outcome
    return outcome


def fetch_stable(url: str, attempts: int = dc.REBASELINE_STABLE_FETCHES) -> tuple[str, Fetched]:
    """Fetch `attempts` times; return (normalised body, last Fetched) if all bodies agree."""
    bodies: list[str] = []
    last: Fetched | None = None
    for _ in range(attempts):
        last = fetch(url)
        bodies.append(dc.normalise(last.text))
    if last is None or len(set(bodies)) != 1:
        raise Unstable(f"fetches of {url} disagreed")
    return bodies[0], last


# ---------------------------------------------------------------- snapshots

_HEADER_RE = re.compile(
    r"^<!-- claudit-snapshot v1 \| source: (\S+) \| normaliser: (\d+) \| "
    r"verified: (\d{4}-\d{2}-\d{2}) \| written by doc_watch\.py --rebaseline; do not edit -->$")
_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def is_iso_date(value: object) -> bool:
    if not isinstance(value, str) or not _ISO_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class Snapshot:
    source: str
    normaliser: int
    verified: str
    body: str


def snapshot_header(url: str, verified: str) -> str:
    return (f"<!-- claudit-snapshot v1 | source: {url} | normaliser: {dc.NORMALISER_VERSION} | "
            f"verified: {verified} | written by doc_watch.py --rebaseline; do not edit -->")


def load_snapshot(path: Path) -> Snapshot:
    try:
        text = Path(path).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SnapshotError(f"cannot read snapshot {path}: {exc}") from exc
    header, sep, body = text.partition("\n")
    m = _HEADER_RE.match(header)
    if not sep or not m or not is_iso_date(m.group(3)):
        raise SnapshotError(f"bad snapshot header in {path}")
    return Snapshot(m.group(1), int(m.group(2)), m.group(3), body)


def write_snapshot(path: Path, url: str, verified: str, body: str) -> None:
    if dc.normalise(body) != body:
        raise SnapshotError("refusing to write a body that is not normalised")
    if not is_iso_date(verified):
        raise SnapshotError(f"bad verified date: {verified!r}")
    if not re.fullmatch(r"\S+", url) or "-->" in url:
        raise SnapshotError(f"url cannot go in a snapshot header: {url!r}")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((snapshot_header(url, verified) + "\n" + body).encode("utf-8"))


def snapshot_path(snapshots_dir: Path, url: str) -> Path:
    return Path(snapshots_dir) / f"{slug(url)}.md"


def _load_snapshot_for(snapshots_dir: Path, url: str) -> str | None:
    """Body of the page's snapshot, None if the file is absent; raises if it is corrupt."""
    path = snapshot_path(snapshots_dir, url)
    if not path.exists():
        return None
    snap = load_snapshot(path)
    if snap.source != url or snap.normaliser != dc.NORMALISER_VERSION:
        raise SnapshotError(f"snapshot {path.name} does not match its source or normaliser")
    return snap.body


# ---------------------------------------------------------------- sources.json

def load_json_sources(path: Path) -> dict:
    """Read sources.json of any schema (no validation beyond a `pages` object)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("pages"), dict):
        raise ConfigError(f"{path}: expected an object with a `pages` object")
    return data


def validate_sources(data: dict) -> None:
    """Strict schema-2 validation; unknown keys are rejected (no threshold keys)."""
    if data.get("schema") != 2:
        raise ConfigError("sources.json is not schema 2 (run --rebaseline --migrate first)")
    if data.get("normaliser_version") != dc.NORMALISER_VERSION:
        raise ConfigError(f"normaliser_version must be {dc.NORMALISER_VERSION}")
    extra = set(data) - {"schema", "normaliser_version", "_comment", "pages", "changelog"}
    if extra:
        raise ConfigError(f"unknown top-level keys: {sorted(extra)}")
    seen: dict[str, str] = {}
    for url, entry in data["pages"].items():
        if not isinstance(url, str):
            raise ConfigError("page keys must be URLs")
        try:
            _check_https_allowed(url, "page")
        except FetchError as exc:
            raise ConfigError(str(exc)) from exc
        s = slug(url)
        if not _SLUG_RE.match(s):
            raise ConfigError(f"unusable slug {s!r} for {url}")
        if s in seen:
            raise ConfigError(f"duplicate slug {s!r}: {seen[s]} and {url}")
        seen[s] = url
        if not isinstance(entry, dict) or set(entry) != {"sha256", "verified", "covers"}:
            raise ConfigError(f"{url}: entry must have exactly sha256, verified, covers")
        sha, verified, covers = entry["sha256"], entry["verified"], entry["covers"]
        if sha is not None and not (isinstance(sha, str) and _SHA_RE.match(sha)):
            raise ConfigError(f"{url}: sha256 must be 64 lowercase hex characters or null")
        if verified is not None and not is_iso_date(verified):
            raise ConfigError(f"{url}: verified must be YYYY-MM-DD or null")
        if (sha is None) != (verified is None):
            raise ConfigError(f"{url}: sha256 and verified must both be set or both null")
        if not isinstance(covers, list) or not all(isinstance(c, str) and c for c in covers):
            raise ConfigError(f"{url}: covers must be a list of strings")
    cl = data.get("changelog")
    if cl is not None:
        if not isinstance(cl, dict) or set(cl) - {"url", "last_reviewed_version"}:
            raise ConfigError("changelog must be an object with url and last_reviewed_version")
        if not isinstance(cl.get("url"), str):
            raise ConfigError("changelog.url must be a string")
        try:
            _check_https_allowed(cl["url"], "changelog")
        except FetchError as exc:
            raise ConfigError(str(exc)) from exc
        last = cl.get("last_reviewed_version")
        if last is not None and not isinstance(last, str):
            raise ConfigError("changelog.last_reviewed_version must be a string or null")


def load_sources(path: Path) -> dict:
    data = load_json_sources(path)
    validate_sources(data)
    return data


def dump_json(obj: object, level: int = 0) -> str:
    """Pretty JSON in the repo's sources.json style: objects expanded, lists on one line."""
    if isinstance(obj, dict) and obj:
        pad = "  " * (level + 1)
        items = [f"{pad}{json.dumps(k, ensure_ascii=False)}: {dump_json(v, level + 1)}"
                 for k, v in obj.items()]
        return "{\n" + ",\n".join(items) + "\n" + "  " * level + "}"
    return json.dumps(obj, ensure_ascii=False)


def save_sources(path: Path, data: dict) -> None:
    Path(path).write_bytes((dump_json(data) + "\n").encode("utf-8"))


# ---------------------------------------------------------------- digest state

_STATE_RE = re.compile(r"<!-- doc-watch-state (\{[^\n]*\}) -->\s*\Z")
_VERSION_STR_RE = re.compile(r"\d+\.\d+\.\d+\Z")


@dataclass
class DigestState:
    updated: str | None = None
    major: tuple[str, ...] = ()
    pages: dict[str, dc.PageState] = field(default_factory=dict)
    changelog: dc.ChangelogState = field(default_factory=dc.ChangelogState)


def _opt_date(value: object, what: str) -> str | None:
    if value is None:
        return None
    if not is_iso_date(value):
        raise StateError(f"{what} must be YYYY-MM-DD or null")
    return value  # type: ignore[return-value]


def parse_state_json(text: str) -> DigestState:
    """Strictly parse the state JSON object; raises StateError on any deviation."""
    if len(text) > dc.STATE_MAX_CHARS:
        raise StateError("state exceeds the size cap")
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise StateError(f"state is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict) or set(obj) != {"schema", "updated", "major", "pages", "changelog"}:
        raise StateError("state has the wrong top-level keys")
    if obj["schema"] != 1 or isinstance(obj["schema"], bool):
        raise StateError("unsupported state schema")
    updated = _opt_date(obj["updated"], "updated")
    major = obj["major"]
    if not isinstance(major, list) or not all(isinstance(s, str) and _SLUG_RE.match(s) for s in major):
        raise StateError("major must be a list of slugs")
    pages_obj = obj["pages"]
    if not isinstance(pages_obj, dict):
        raise StateError("pages must be an object")
    pages: dict[str, dc.PageState] = {}
    for s, st in pages_obj.items():
        if not _SLUG_RE.match(s) or not isinstance(st, dict) \
                or set(st) != {"minor_first_seen", "unreachable_since"}:
            raise StateError(f"bad page state for {s!r}")
        pages[s] = dc.PageState(_opt_date(st["minor_first_seen"], "minor_first_seen"),
                                _opt_date(st["unreachable_since"], "unreachable_since"))
    cl = obj["changelog"]
    if not isinstance(cl, dict) or set(cl) != {"new_versions", "first_seen"}:
        raise StateError("bad changelog state")
    nv = cl["new_versions"]
    if not isinstance(nv, list) or not all(isinstance(v, str) and _VERSION_STR_RE.match(v) for v in nv):
        raise StateError("changelog.new_versions must be a list of versions")
    return DigestState(updated, tuple(major), pages,
                       dc.ChangelogState(tuple(nv), _opt_date(cl["first_seen"], "first_seen")))


def read_state(issue_body: str) -> DigestState:
    """Extract and parse the state comment at the end of a digest issue body."""
    m = _STATE_RE.search(issue_body)
    if not m:
        raise StateError("digest body has no state comment at its end")
    return parse_state_json(m.group(1))


def render_state(state: DigestState) -> str:
    obj = {
        "schema": 1,
        "updated": state.updated,
        "major": list(state.major),
        "pages": {s: {"minor_first_seen": p.minor_first_seen,
                      "unreachable_since": p.unreachable_since}
                  for s, p in sorted(state.pages.items())},
        "changelog": {"new_versions": list(state.changelog.new_versions),
                      "first_seen": state.changelog.first_seen},
    }
    payload = json.dumps(obj, separators=(",", ":"))
    if len(payload) > dc.STATE_MAX_CHARS:
        raise StateError("state exceeds the size cap")
    return f"<!-- doc-watch-state {payload} -->"


# ---------------------------------------------------------------- run_checks

@dataclass
class PageReport:
    url: str
    slug: str
    covers: list[str]
    verified: str | None
    unreachable: bool
    result: dc.PageResult


@dataclass
class Report:
    pages: list[PageReport]
    changelog: dc.ChangelogResult | None
    changelog_unreachable: bool
    state_after: DigestState

    @property
    def major_slugs(self) -> list[str]:
        return [p.slug for p in self.pages if p.result.status == dc.MAJOR]

    @property
    def all_unreachable(self) -> bool:
        flags = [p.unreachable for p in self.pages]
        if self.changelog is not None:
            flags.append(self.changelog_unreachable)
        return bool(flags) and all(flags)


def run_checks(sources: dict, snapshots_dir: Path, state: DigestState, today: date, *,
               sleep: Callable[[float], None] = time.sleep) -> Report:
    """Classify every page and the changelog; one page raising never affects the others."""
    reports: list[PageReport] = []
    for url, entry in sources["pages"].items():
        s = slug(url)
        prev = state.pages.get(s, dc.PageState())
        unreachable = False
        try:
            snap = _load_snapshot_for(snapshots_dir, url) if entry.get("sha256") is not None else None
            outcome = fetch_checked(url, snap, sleep=sleep)
            unreachable = outcome.unreachable
            result = dc.evaluate_page(snap, entry.get("covers", []), outcome, prev, today)
        except Exception as exc:  # noqa: BLE001 - isolate: this page becomes MAJOR, others run
            result = dc.PageResult(dc.MAJOR, [f"classifier error: {type(exc).__name__}"], state=prev)
        reports.append(PageReport(url, s, list(entry.get("covers", [])), entry.get("verified"),
                                  unreachable, result))

    changelog: dc.ChangelogResult | None = None
    cl_unreachable = False
    cl = sources.get("changelog")
    if cl and cl.get("url"):
        try:
            outcome = fetch_checked(cl["url"], None, sleep=sleep)
            cl_unreachable = outcome.unreachable
            changelog = dc.evaluate_changelog(outcome, cl.get("last_reviewed_version"),
                                              state.changelog, today)
        except Exception as exc:  # noqa: BLE001
            changelog = dc.ChangelogResult(dc.MAJOR, [f"classifier error: {type(exc).__name__}"],
                                           state=state.changelog)

    pages_after = {r.slug: r.result.state for r in reports
                   if r.result.state.minor_first_seen or r.result.state.unreachable_since}
    after = DigestState(today.isoformat(), tuple(r.slug for r in reports if r.result.status == dc.MAJOR),
                        pages_after, changelog.state if changelog else dc.ChangelogState())
    return Report(reports, changelog, cl_unreachable, after)


# ---------------------------------------------------------------- rendering

def _hunk_lines(hunk: dc.Hunk) -> list[str]:
    return ([f"@@ {hunk.section[0] or '(preamble)'} @@ {hunk.tag}"]
            + [f"- {line}" for line in hunk.old_lines]
            + [f"+ {line}" for line in hunk.new_lines])


def trim_diff(diff: dc.PageDiff, hunk_max: int = dc.DIGEST_HUNK_MAX_LINES,
              page_max: int = dc.DIGEST_PAGE_MAX_LINES) -> list[str]:
    """Diff lines for MAJOR hunks, capped per hunk and per page (full: --diff SLUG)."""
    return trim_hunks(diff.major_hunks, hunk_max, page_max)


def trim_hunks(hunks: list[dc.Hunk], hunk_max: int = dc.DIGEST_HUNK_MAX_LINES,
               page_max: int = dc.DIGEST_PAGE_MAX_LINES) -> list[str]:
    """Diff lines for the given hunks, capped per hunk and per page; [] if hunk_max <= 0."""
    out: list[str] = []
    if hunk_max <= 0:
        return out
    for hunk in hunks:
        lines = _hunk_lines(hunk)
        if len(lines) > hunk_max:
            lines = lines[:hunk_max] + [f"... ({len(lines) - hunk_max} more lines; use --diff)"]
        if len(out) + len(lines) > page_max:
            out.extend(lines[: max(page_max - len(out), 0)])
            out.append("... (diff truncated; use --diff)")
            break
        out.extend(lines)
    return out


def render_comment(slugs: list[str]) -> str:
    return (f"**New MAJOR on the drift digest:** {', '.join(slugs)}. {len(slugs)} page(s) now "
            "need a check. Details in the digest body.")


def render_text(report: Report) -> str:
    """Plain-text digest for the dry run."""
    lines = [f"Checked {len(report.pages)} doc pages" + (" + changelog." if report.changelog else ".")]
    majors = [p for p in report.pages if p.result.status == dc.MAJOR]
    for p in majors:
        lines.append(f"MAJOR  {p.slug}  {p.url}  (verified {p.verified})")
        if p.covers:
            lines.append("  re-check: " + ", ".join(f"plugins/claudit/{c}" for c in p.covers))
        lines.extend(f"  - {r}" for r in p.result.reasons)
        if p.result.diff is not None:
            lines.extend("    " + ln for ln in trim_diff(p.result.diff))
    for p in report.pages:
        if p.result.status == dc.UNREACHABLE:
            lines.append(f"UNREACHABLE  {p.slug}  ({p.result.detail}; {p.result.reasons[0]})")
        elif p.result.status == dc.MINOR:
            lines.append(f"MINOR  {p.slug}  ({'; '.join(p.result.reasons)})")
        elif p.result.status == dc.NO_BASELINE:
            lines.append(f"NO BASELINE  {p.slug}  {p.url}")
    cl = report.changelog
    if cl is not None and cl.status != dc.CLEAN:
        lines.append(f"CHANGELOG {cl.status}  " + "; ".join(cl.reasons))
    if not majors and not (cl and cl.status == dc.MAJOR):
        lines.append("No page needs a check.")
    return "\n".join(lines)


# ---------------------------------------------------------------- rebaseline

def _is_baselined(entry: dict) -> bool:
    return entry.get("sha256") is not None or bool(entry.get("covers"))


def rebaseline(args: argparse.Namespace, sources: dict, sources_path: Path,
               snapshots_dir: Path, today: date) -> int:
    """Write snapshots and update sources.json. With --migrate, `sources` is schema 1 and is
    converted to schema 2 all-or-nothing. Exit 0, 2 (unknown --only slug) or 3 (refused)."""
    pages: dict = sources["pages"]
    by_slug = {slug(u): u for u in pages}
    only = list(args.only or [])
    unknown = [s for s in only if s not in by_slug]
    if unknown:
        print(f"ERROR: unknown slug(s): {', '.join(unknown)}", file=sys.stderr)
        return 2
    if args.migrate:
        targets = [u for u, e in pages.items() if e.get("sha256") is not None]
    elif only:
        targets = [by_slug[s] for s in only]
    else:
        targets = [u for u, e in pages.items() if _is_baselined(e)]
    stamp = args.date or today.isoformat()

    writes: list[tuple[str, str, str]] = []   # (url, body, verified)
    refused: list[str] = []
    for url in targets:
        s = slug(url)
        entry = pages[url]
        try:
            body, last = fetch_stable(url)
        except Unstable:
            print(f"REFUSED  {s}: unstable fetch (bodies disagreed)")
            refused.append(s)
            continue
        except FetchError as exc:
            print(f"REFUSED  {s}: unreachable ({exc})")
            refused.append(s)
            continue
        if dc.length_truncated(last.declared_length, last.received, last.read_error):
            # Never overridable: a truncated page must not become a snapshot, even with --force.
            print(f"REFUSED  {s}: truncated fetch (cannot be baselined, even with --force)")
            refused.append(s)
            continue
        existing = None
        if not args.migrate:
            try:
                existing = _load_snapshot_for(snapshots_dir, url)
            except SnapshotError:
                existing = None
        anomalies = dc.detect_anomalies(to_outcome(last), body, existing)
        if anomalies and not args.force:
            print(f"REFUSED  {s}: {'; '.join(anomalies)} (use --force to override)")
            refused.append(s)
            continue
        verified = stamp
        if args.migrate:
            if dc.sha256(dc.normalise_v1(last.text)) == entry.get("sha256"):
                verified = entry["verified"]
            elif not args.date:
                print(f"CHANGED SINCE BASELINE  {s}: re-verify its dependents, then "
                      "re-run with --date YYYY-MM-DD")
                refused.append(s)
                continue
        writes.append((url, body, verified))

    if args.migrate and refused:
        print("Migration refused: nothing was written.")
        return 3
    for url, body, verified in writes:
        write_snapshot(snapshot_path(snapshots_dir, url), url, verified, body)
        pages[url]["sha256"] = dc.sha256(body)
        pages[url]["verified"] = verified
        print(f"BASELINED  {slug(url)}  verified {verified}")
    if args.migrate:
        sources = {"schema": 2, "normaliser_version": dc.NORMALISER_VERSION,
                   **{k: v for k, v in sources.items() if k not in ("schema", "normaliser_version")}}
    if writes or args.migrate:
        save_sources(sources_path, sources)
    return 3 if refused else 0


# ---------------------------------------------------------------- other commands

def show_diff(slug_arg: str, sources: dict, snapshots_dir: Path) -> int:
    by_slug = {slug(u): u for u in sources["pages"]}
    url = by_slug.get(slug_arg)
    if url is None:
        print(f"ERROR: unknown slug {slug_arg!r}", file=sys.stderr)
        return 2
    try:
        body = _load_snapshot_for(snapshots_dir, url)
    except SnapshotError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if body is None:
        print(f"ERROR: no snapshot for {slug_arg}", file=sys.stderr)
        return 2
    try:
        live = dc.normalise(fetch(url).text)
    except FetchError as exc:
        print(f"ERROR: cannot fetch {url}: {exc}", file=sys.stderr)
        return 1
    diff = dc.diff_page(body, live)
    if not diff.hunks:
        print(f"{slug_arg}: no differences from the snapshot.")
    for hunk in diff.hunks:
        verdict = "MAJOR" if hunk.major else "MINOR"
        print(f"[{verdict}{': ' + ', '.join(hunk.reasons) if hunk.reasons else ''}]")
        print("\n".join(_hunk_lines(hunk)))
    return 0


def print_hashes(sources_path: Path) -> int:
    print("NOTE: --print-hashes is deprecated and will be removed in the next release; "
          "use --rebaseline.", file=sys.stderr)
    try:
        data = load_json_sources(sources_path)
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    current: dict[str, str] = {}
    for url in data["pages"]:
        try:
            got = fetch(url)
        except FetchError:
            continue
        if not dc.length_truncated(got.declared_length, got.received, got.read_error):
            current[url] = dc.sha256(dc.normalise(got.text))
    cl = data.get("changelog") or {}
    if cl.get("url"):
        try:
            versions = changelog_versions(fetch(cl["url"]).text)
            if versions:
                current["changelog_latest"] = versions[0]
        except FetchError:
            pass
    print(json.dumps(current, indent=2))
    return 0


# ---------------------------------------------------------------- digest rendering

DIGEST_TITLE = "doc-drift: digest"
SHADOW_TITLE = "doc-drift: digest (shadow)"
LEGACY_CHANGELOG_TITLE = "doc-drift: claude-code changelog"
CHANGELOG_TOKEN = "changelog"     # marks a MAJOR changelog in DigestState.major
ALL_CLEAR = "All clear: no page needs a check."
DIGEST_HEADER = ("<!-- claudit doc-watch digest: generated by maintainer/doc_watch.py; "
                 "edits to this body are overwritten -->")
LEGACY_CLOSE_NOTE = ('Superseded by the rolling digest "doc-drift: digest". This per-page issue '
                     'is closed. Its page is tracked there.')
# (keep MINOR diffs, per-hunk lines, per-page lines): MINOR detail is dropped first, then
# MAJOR hunks are shortened step by step.
_TRIM_STEPS = ((True, dc.DIGEST_HUNK_MAX_LINES, dc.DIGEST_PAGE_MAX_LINES),
               (False, dc.DIGEST_HUNK_MAX_LINES, dc.DIGEST_PAGE_MAX_LINES),
               (False, 20, 60), (False, 10, 30), (False, 5, 15), (False, 2, 6), (False, 0, 0))


def _longest_backtick_run(text: str) -> int:
    return max((len(m) for m in re.findall(r"`+", text)), default=0)


def fence_for(text: str, minimum: int = 3) -> str:
    """A backtick fence strictly longer than any backtick run in `text`."""
    return "`" * max(minimum, _longest_backtick_run(text) + 1)


def inline_code(text: object, limit: int = 300) -> str:
    """Untrusted text as a single-line code span (mentions, HTML and links stay inert)."""
    flat = " ".join(str(text).split())[:limit] or "(empty)"
    ticks = "`" * (_longest_backtick_run(flat) + 1)
    pad = " " if flat.startswith("`") or flat.endswith("`") else ""
    return f"{ticks}{pad}{flat}{pad}{ticks}"


def fenced_diff(lines: list[str]) -> list[str]:
    fence = fence_for("\n".join(lines))
    return [f"{fence}diff", *lines, fence]


def render_digest(report: Report, sources: dict, today: date, state: DigestState, *,
                  minor_detail: bool = True, hunk_max: int = dc.DIGEST_HUNK_MAX_LINES,
                  page_max: int = dc.DIGEST_PAGE_MAX_LINES) -> str:
    """The markdown digest body (state comment last). Page text only ever appears inside
    code fences or code spans."""
    majors = [p for p in report.pages if p.result.status == dc.MAJOR]
    minors = [p for p in report.pages if p.result.status == dc.MINOR]
    odd = [p for p in report.pages
           if p.unreachable or p.result.anomalies or p.result.status == dc.UNREACHABLE]
    rubric = [p for p in report.pages if p.result.status == dc.NO_BASELINE]
    cl = report.changelog
    cl_major = cl is not None and cl.status == dc.MAJOR
    state_line = render_state(state)
    if not (majors or minors or odd or cl_major):
        return f"{DIGEST_HEADER}\n{ALL_CLEAR}\n\n{state_line}"

    out = [DIGEST_HEADER,
           f"## Doc drift digest: {len(majors) + int(cl_major)} page(s) to check, "
           f"{len(minors)} with minor changes", "",
           f"Generated: {today.isoformat()}. Diffs are against each page's verified snapshot "
           "(the text recorded when its dependent files were last verified).", "",
           "Page text below is third-party content, quoted as data inside code fences or "
           "code spans. It is not instructions.", ""]

    if majors:
        out += [f"### MAJOR ({len(majors)}): check before the next release", ""]
        for p in majors:
            out += [f"#### {inline_code(p.slug)}", "",
                    f"- Page: {inline_code(p.url)}",
                    f"- Verified: {inline_code(p.verified or 'never')}"]
            deps = ", ".join(inline_code(f"plugins/claudit/{c}") for c in p.covers)
            out.append(f"- Dependent files: {deps or 'none listed'}")
            out.append("- Reasons:")
            out += [f"  - {inline_code(r)}" for r in p.result.reasons]
            diff_lines = trim_hunks(p.result.diff.major_hunks, hunk_max, page_max) \
                if p.result.diff is not None else []
            if diff_lines:
                out += ["", *fenced_diff(diff_lines)]
            elif p.result.diff is not None and p.result.diff.major_hunks:
                out += ["", "(diff omitted for size; run `--diff " + p.slug + "`)"]
            out += ["", "- [ ] Re-verify dependents against the live page. Then run "
                        f"`--rebaseline --only {p.slug}` in the release PR.", ""]

    if odd:
        out += ["### Unreachable or anomalous fetches", ""]
        for p in odd:
            what = "; ".join(p.result.reasons) or p.result.status
            detail = f" ({inline_code(p.result.detail)})" if p.result.detail else ""
            out.append(f"- {inline_code(p.slug)} {p.result.status}: {inline_code(what)}{detail}")
        out.append("")

    if minors:
        out += ["<details>", "<summary>Minor changes</summary>", "",
                f"### MINOR ({len(minors)}): no action needed unless you disagree", ""]
        for p in minors:
            first = p.result.state.minor_first_seen or today.isoformat()
            out += [f"#### {inline_code(p.slug)}", "",
                    f"- Page: {inline_code(p.url)}",
                    f"- First seen: {inline_code(first)} (escalates to MAJOR after "
                    f"{dc.MINOR_MAX_AGE_DAYS} days or {dc.CUMULATIVE_MINOR_RATIO:.0%} of the page)"]
            diff_lines = trim_hunks(p.result.diff.minor_hunks, hunk_max, page_max) \
                if (minor_detail and p.result.diff is not None) else []
            if diff_lines:
                out += ["", *fenced_diff(diff_lines)]
            elif not minor_detail:
                out += ["", "(diff omitted for size; run `--diff " + p.slug + "`)"]
            out.append("")
        out += ["</details>", ""]

    if cl is not None and cl.status != dc.CLEAN:
        out += ["### Claude Code changelog", ""]
        out += [f"- {inline_code(r)}" for r in cl.reasons]
        url = (sources.get("changelog") or {}).get("url")
        if url:
            out.append(f"- Source: {inline_code(url)}")
        if cl_major:
            out.append("- Review for new features or behaviour changes Claudit's guidance should "
                       "cover, then set `changelog.last_reviewed_version` in "
                       "`maintainer/sources.json` in the release PR.")
        out.append("")

    if rubric:
        out += ["### No-baseline rubric pages", ""]
        out += [f"- {inline_code(p.slug)} ({inline_code(p.url)})" for p in rubric]
        out.append("")

    out.append(state_line)
    return "\n".join(out)


def build_digest_body(report: Report, sources: dict, today: date, state: DigestState) -> str:
    """render_digest, trimmed to DIGEST_BODY_MAX_CHARS: MINOR detail first, then MAJOR hunks."""
    body = ""
    for minor_detail, hunk_max, page_max in _TRIM_STEPS:
        body = render_digest(report, sources, today, state, minor_detail=minor_detail,
                             hunk_max=hunk_max, page_max=page_max)
        if len(body) <= dc.DIGEST_BODY_MAX_CHARS:
            return body
    cut = body.rfind("<!-- doc-watch-state ")
    head, tail = body[:cut], body[cut:]
    note = "\n\n... (digest truncated to fit the size limit; run `--diff SLUG` locally)\n\n"
    closers = "</details>\n\n"
    room = max(dc.DIGEST_BODY_MAX_CHARS - len(tail) - len(note) - len(closers), 0)
    head = head[:room].rsplit("\n", 1)[0]
    unclosed = head.count("<details>") - head.count("</details>")
    return head + note + closers * max(unclosed, 0) + tail


# ---------------------------------------------------------------- digest planning

def digest_state_after(report: Report) -> DigestState:
    """state_after plus the changelog marker in `major` when the changelog is MAJOR."""
    st = report.state_after
    major = tuple(st.major)
    if report.changelog is not None and report.changelog.status == dc.MAJOR \
            and CHANGELOG_TOKEN not in major:
        major += (CHANGELOG_TOKEN,)
    return DigestState(st.updated, major, dict(st.pages), st.changelog)


def newly_major(report: Report, before: DigestState) -> list[str]:
    """Pages (and the changelog marker) that are MAJOR now but were not in the stored state."""
    out = [s for s in report.state_after.major if s not in before.major]
    cl = report.changelog
    if cl is not None and cl.status == dc.MAJOR:
        added = set(cl.new_versions) - set(before.changelog.new_versions)
        if CHANGELOG_TOKEN not in before.major or added:
            out.append(CHANGELOG_TOKEN)
    return out


@dataclass
class DigestPlan:
    final: str            # body with the complete new state
    pending: str          # body whose state does not yet record the newly-MAJOR items
    newly: list[str]
    has_major: bool
    create_ok: bool       # create the issue if none exists
    comment: str


def plan_digest(report: Report, sources: dict, today: date, before: DigestState, *,
                shadow: bool = False) -> DigestPlan:
    after = digest_state_after(report)
    newly = newly_major(report, before)
    final = build_digest_body(report, sources, today, after)
    pending = final
    if newly:
        # Until the comment succeeds the stored state must not claim these are announced,
        # or a failed comment would never be retried.
        pend_cl = before.changelog if CHANGELOG_TOKEN in newly else after.changelog
        pending = build_digest_body(
            report, sources, today,
            DigestState(after.updated, tuple(s for s in after.major if s not in newly),
                        after.pages, pend_cl))
    has_minor = any(p.result.status == dc.MINOR for p in report.pages)
    create_ok = bool(after.major) or has_minor or (shadow and bool(after.pages))
    return DigestPlan(final, pending, newly, bool(after.major), create_ok, render_comment(newly))


def _signature(body: str) -> str:
    """Body with the volatile 'Generated' line and state date removed (no churn PATCHes)."""
    body = body.replace("\r\n", "\n")
    body = re.sub(r"^Generated: \d{4}-\d{2}-\d{2}\.", "Generated: -.", body, flags=re.MULTILINE)
    return re.sub(r'"updated":(?:"[^"]*"|null)', '"updated":null', body)


# ---------------------------------------------------------------- GitHub (digest)

GITHUB_API = "https://api.github.com"
# owner/name: owner is alphanumerics and hyphens; name may also hold "." and "_" but is never
# "." or ".." (the earlier pattern let "../x" through into the API path).
_REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/(?!\.{1,2}\Z)[A-Za-z0-9._-]+")


class GitHubError(Exception):
    """A GitHub response the digest code will not act on."""


def gh_call(method: str, path: str, token: str, body: dict | None = None) -> tuple[object, object]:
    """One GitHub API request; returns (parsed JSON or None, response headers)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(GITHUB_API + path, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read()
        return (json.loads(raw) if raw else None), resp.headers


def _gh_data(method: str, path: str, token: str, body: dict | None = None) -> object:
    return gh_call(method, path, token, body)[0]


def _next_path(headers: object) -> str | None:
    link = headers.get("Link") if headers is not None else None
    for part in (link or "").split(","):
        m = re.match(r'\s*<([^>]+)>\s*;\s*rel="next"', part)
        if m:
            url = m.group(1)
            if not url.startswith(GITHUB_API + "/"):
                raise GitHubError("refusing to follow a pagination link off api.github.com")
            return url[len(GITHUB_API):]
    return None


def gh_paginate(path: str, token: str, max_pages: int = 10) -> list:
    """All items of a list endpoint, following Link rel=next; raises past `max_pages`."""
    items: list = []
    nxt: str | None = path
    for _ in range(max_pages):
        data, headers = gh_call("GET", nxt, token)
        if not isinstance(data, list):
            raise GitHubError("unexpected GitHub response (expected a list)")
        items.extend(data)
        nxt = _next_path(headers)
        if nxt is None:
            return items
    raise GitHubError(f"more than {max_pages} pages of results; refusing to continue")


def _real_issues(items: list) -> list[dict]:
    return [i for i in items if isinstance(i, dict) and "pull_request" not in i
            and isinstance(i.get("number"), int) and isinstance(i.get("title"), str)]


def find_digest(repo: str, token: str, title: str = DIGEST_TITLE) -> dict | None:
    """The oldest issue (open or closed) whose title is exactly `title`, or None."""
    issues = _real_issues(gh_paginate(
        f"/repos/{repo}/issues?state=all&labels={LABEL}&per_page=100", token))
    matches = [i for i in issues if i["title"] == title]
    return min(matches, key=lambda i: i["number"]) if matches else None


def write_digest(repo: str, token: str, existing: dict | None, plan: DigestPlan, title: str,
                 *, shadow: bool = False) -> list[str]:
    """Apply the digest algorithm; returns human-readable actions.

    No digest: create only if there is something to report (a MINOR-only digest is created
    and then closed in a second call, without a comment). Existing digest: PATCH the body
    if it changed (combined with reopen/close, since one call is atomic), reopen when MAJOR
    and closed, close when no MAJOR and open (no close comment). A comment announcing newly
    MAJOR items is posted AFTER the body PATCH; the first PATCH stores a state that does not
    yet list them, and a final PATCH records them once the comment succeeded. A failed comment
    therefore leaves them "new" for the next run (at-least-once). The shadow issue never gets
    comments and is never reopened or closed.
    """
    if existing is None:
        if not plan.create_ok:
            return ["no digest needed"]
        ensure_label(repo, token, request=_gh_data)
        created = _gh_data("POST", f"/repos/{repo}/issues", token,
                           {"title": title, "body": plan.final, "labels": [LABEL]})
        if not isinstance(created, dict) or not isinstance(created.get("number"), int):
            raise GitHubError("unexpected response creating the digest issue")
        actions = [f"created #{created['number']}"]
        if not plan.has_major and not shadow:
            # MINOR-only content: the issue API cannot create a closed issue, so create it and
            # close it straight away (no comment). A later MAJOR reopens it.
            _gh_data("PATCH", f"/repos/{repo}/issues/{created['number']}", token,
                     {"state": "closed"})
            actions.append(f"closed #{created['number']} (minor changes only)")
        return actions

    num = existing["number"]
    announce = bool(plan.newly) and not shadow
    first = plan.pending if announce else plan.final
    payload: dict = {}
    if _signature(first) != _signature(existing.get("body") or ""):
        payload["body"] = first
    is_open = existing.get("state") == "open"
    if not shadow:
        if plan.has_major and not is_open:
            payload["state"] = "open"
        elif not plan.has_major and is_open:
            payload["state"] = "closed"
    actions: list[str] = []
    if payload:
        _gh_data("PATCH", f"/repos/{repo}/issues/{num}", token, payload)
        actions.append(f"updated #{num}" + (f" ({payload['state']})" if "state" in payload else ""))
    if announce:
        _gh_data("POST", f"/repos/{repo}/issues/{num}/comments", token, {"body": plan.comment})
        actions.append(f"commented on #{num}")
        if _signature(plan.final) != _signature(first):
            _gh_data("PATCH", f"/repos/{repo}/issues/{num}", token, {"body": plan.final})
            actions.append(f"recorded state on #{num}")
    return actions or [f"unchanged #{num}"]


def close_legacy(repo: str, token: str, sources: dict) -> list[str]:
    """Close the open per-page `doc-drift: <slug>` issues (and the old changelog issue) with a
    pointer comment. Matches exact titles built from sources.json only, so the digest, the
    shadow issue, pull requests and any other issue are never touched."""
    wanted = {f"doc-drift: {slug(u)}" for u in sources["pages"]} | {LEGACY_CHANGELOG_TITLE}
    wanted -= {DIGEST_TITLE, SHADOW_TITLE}
    issues = _real_issues(gh_paginate(
        f"/repos/{repo}/issues?state=open&labels={LABEL}&per_page=100", token))
    actions: list[str] = []
    for issue in sorted(issues, key=lambda i: i["number"]):
        if issue["title"] not in wanted or issue.get("state", "open") != "open":
            continue
        n = issue["number"]
        _gh_data("POST", f"/repos/{repo}/issues/{n}/comments", token, {"body": LEGACY_CLOSE_NOTE})
        _gh_data("PATCH", f"/repos/{repo}/issues/{n}", token, {"state": "closed"})
        actions.append(f"closed #{n} {issue['title']}")
    return actions or ["no open legacy issues"]


def _scrub(message: str, token: str) -> str:
    return message.replace(token, "***") if token else message


_GH_FAILURES = (GitHubError, urllib.error.URLError, OSError, http.client.HTTPException,
                ValueError, KeyError, TypeError)


def run_github_mode(args: argparse.Namespace, env: dict[str, str], today: date,
                    sleep: Callable[[float], None]) -> int:
    """--github, --shadow and --close-legacy. Exit 1 before any network without credentials."""
    flag = "--close-legacy" if args.close_legacy else "--shadow" if args.shadow else "--github"
    token, repo = env.get("GITHUB_TOKEN"), env.get("GITHUB_REPOSITORY")
    if not token or not repo:
        print(f"ERROR: {flag} needs GITHUB_TOKEN and GITHUB_REPOSITORY", file=sys.stderr)
        return 1
    if not _REPO_RE.fullmatch(repo):
        print("ERROR: GITHUB_REPOSITORY must look like owner/name", file=sys.stderr)
        return 1
    try:
        sources = load_sources(args.sources)
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.close_legacy:
        try:
            for line in close_legacy(repo, token, sources):
                print(line)
        except _GH_FAILURES as exc:
            print(f"ERROR: GitHub step failed: {_scrub(f'{type(exc).__name__}: {exc}', token)}",
                  file=sys.stderr)
            return 1
        return 0

    shadow = bool(args.shadow)
    title = SHADOW_TITLE if shadow else DIGEST_TITLE
    try:
        existing = find_digest(repo, token, title)
        if existing is None:
            print("NOTICE: no digest issue found; starting with an empty state")
            before = DigestState()
        else:
            before = read_state(existing.get("body") or "")
    except StateError as exc:
        print(f"ERROR: digest state unreadable, leaving the digest unchanged: {exc}",
              file=sys.stderr)
        return 1
    except _GH_FAILURES as exc:
        print(f"ERROR: GitHub step failed: {_scrub(f'{type(exc).__name__}: {exc}', token)}",
              file=sys.stderr)
        return 1

    report = run_checks(sources, args.snapshots_dir, before, today, sleep=sleep)
    print(render_text(report))
    try:
        plan = plan_digest(report, sources, today, before, shadow=shadow)
    except StateError as exc:
        print(f"ERROR: cannot build the digest state: {exc}", file=sys.stderr)
        return 1
    rc = 1 if report.all_unreachable else 0

    if shadow:
        summary = env.get("GITHUB_STEP_SUMMARY")
        if summary:
            try:
                with open(summary, "a", encoding="utf-8") as fh:
                    fh.write(plan.final + "\n")
            except OSError as exc:
                print(f"ERROR: cannot write the step summary: {exc}", file=sys.stderr)
                rc = 1
        else:
            print(plan.final)
    try:
        for line in write_digest(repo, token, existing, plan, title, shadow=shadow):
            print(f"  {title}: {line}")
    except _GH_FAILURES as exc:
        print(f"ERROR: GitHub step failed: {_scrub(f'{type(exc).__name__}: {exc}', token)}",
              file=sys.stderr)
        return 1
    return rc


# ---------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="doc_watch.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--github", action="store_true",
                    help="create/update the rolling digest issue (GITHUB_TOKEN, GITHUB_REPOSITORY)")
    ap.add_argument("--shadow", action="store_true",
                    help="new logic only: digest to $GITHUB_STEP_SUMMARY/stdout, state in a silent issue")
    ap.add_argument("--legacy-issues", action="store_true", help="old per-page behaviour, frozen")
    ap.add_argument("--close-legacy", action="store_true",
                    help="one-time: close open per-page doc-drift issues with a pointer comment")
    ap.add_argument("--rebaseline", action="store_true", help="write snapshots and hashes")
    ap.add_argument("--only", nargs="+", metavar="SLUG", help="with --rebaseline: these pages only")
    ap.add_argument("--date", metavar="YYYY-MM-DD", help="with --rebaseline: verified date")
    ap.add_argument("--migrate", action="store_true", help="with --rebaseline: schema 1 -> 2")
    ap.add_argument("--force", action="store_true", help="with --rebaseline: ignore anomalies")
    ap.add_argument("--diff", metavar="SLUG", help="full local diff against the snapshot")
    ap.add_argument("--print-hashes", action="store_true", help="DEPRECATED alias")
    ap.add_argument("--sources", type=Path, default=SOURCES, help="sources.json path")
    ap.add_argument("--snapshots-dir", type=Path, default=SNAPSHOTS, help="snapshots directory")
    ap.add_argument("--state-file", type=Path, help="dry run only: read digest state JSON")
    return ap


def _usage_error(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None, *, today: date | None = None,
         env: dict[str, str] | None = None,
         sleep: Callable[[float], None] = time.sleep) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    today = today or date.today()
    env = os.environ if env is None else env

    modes = [args.rebaseline, args.diff is not None, args.print_hashes,
             args.close_legacy, args.shadow, args.legacy_issues]
    if sum(bool(m) for m in modes) > 1:
        return _usage_error("choose only one of --rebaseline, --diff, --print-hashes, "
                            "--close-legacy, --shadow, --legacy-issues")
    if args.github and (args.rebaseline or args.diff is not None or args.print_hashes
                        or args.close_legacy or args.shadow):
        return _usage_error("--github only combines with --legacy-issues")
    if (args.only or args.date or args.migrate or args.force) and not args.rebaseline:
        return _usage_error("--only/--date/--migrate/--force need --rebaseline")
    if args.migrate and args.only:
        return _usage_error("--migrate converts the whole file; do not combine with --only")
    if args.state_file and (args.github or any(modes)):
        return _usage_error("--state-file is for the plain dry run only")
    if args.date and not is_iso_date(args.date):
        return _usage_error("--date must be YYYY-MM-DD")

    if args.legacy_issues:
        return legacy_main(args)
    if args.print_hashes:
        return print_hashes(args.sources)
    if args.close_legacy or args.shadow or args.github:
        return run_github_mode(args, env, today, sleep)

    try:
        if args.rebaseline and args.migrate:
            sources = load_json_sources(args.sources)
            if sources.get("schema", 1) != 1:
                raise ConfigError("--migrate expects a schema 1 sources.json")
        else:
            sources = load_sources(args.sources)
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.rebaseline:
        return rebaseline(args, sources, args.sources, args.snapshots_dir, today)
    if args.diff is not None:
        return show_diff(args.diff, sources, args.snapshots_dir)

    state = DigestState()
    if args.state_file:
        try:
            state = parse_state_json(Path(args.state_file).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, StateError) as exc:
            print(f"ERROR: cannot use state file: {exc}", file=sys.stderr)
            return 1
    report = run_checks(sources, args.snapshots_dir, state, today, sleep=sleep)
    print(render_text(report))
    return 1 if report.all_unreachable else 0


if __name__ == "__main__":
    sys.exit(main())
