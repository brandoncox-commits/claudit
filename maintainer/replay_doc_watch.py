#!/usr/bin/env python3
"""Claudit doc-watch replay: a read-only what-if tool for the classifier.

Replays doc_classify.py over a baseline commit's snapshots versus the working-tree snapshots
(the "live" side) and reports how many pages the classifier calls MAJOR, which reasons drive
that, and how the count would change if a reason were removed or the section-rewrite
threshold loosened. It never fetches, never writes inside the repo and never commits: git is
only asked to resolve a revision and to read objects, and the only files written are the
recovered baseline copies under --scratch. Standard library only; no LLM.

Usage:
  python maintainer/replay_doc_watch.py --scratch DIR [--repo DIR] [--baseline REV]
        [--current working-tree] [--ablate REASON ...] [--section-ratio T ...]
        [--ground-truth FILE] [--expected-major FILE] [--target-pct P]
        [--format text|json] [--show-lines N] [--page SLUG]
  --scratch must be a new or empty directory outside the repo. REASON is a classifier reason
  such as "number changed" (quote it). By default no snapshot text is printed.
Exit codes: 0 = ran; 1 = every page ERROR, parity mismatch, git object-id mismatch, git
timeout or broken stdout; 2 = usage, repo, ref, scratch or schema problem.
"""

from __future__ import annotations

import sys
sys.dont_write_bytecode = True   # must precede the doc_classify import: the repo stays byte-clean

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import doc_classify as dc  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = "33fd75b"
GIT_TIMEOUT_S = 30
GIT_ALLOWED = frozenset({"rev-parse", "cat-file", "ls-tree", "log", "hash-object"})
DEFAULT_SECTION_RATIOS = (0.5, 0.6, 0.75)
DEFAULT_TARGET_PCT = "25"
FIXED_DATE = date(2000, 1, 1)     # evaluate_page needs a date; with an empty PageState it has
                                  # no effect (minor age is 0 days, no unreachable timer)
STATUS_ERROR = "ERROR"
SOURCES_PATH = "maintainer/sources.json"
SNAPSHOTS_DIR = "maintainer/snapshots"
HEADING_TAGS = frozenset({dc.REASON_HEADING_ADDED, dc.REASON_HEADING_REMOVED})
ABLATABLE = (dc.REASON_TABLE, dc.REASON_FENCE, dc.REASON_CODE_TOKEN, dc.REASON_VERSION,
             dc.REASON_NUMBER, dc.REASON_MODAL, dc.REASON_LINK, dc.REASON_SECTION,
             dc.REASON_HEADING_ADDED, dc.REASON_HEADING_REMOVED)

# Copied from doc_watch._HEADER_RE (this tool must not import doc_watch: it pulls in urllib).
# A test asserts the two patterns stay identical.
HEADER_RE = re.compile(
    r"^<!-- claudit-snapshot v1 \| source: (\S+) \| normaliser: (\d+) \| "
    r"verified: (\d{4}-\d{2}-\d{2}) \| written by doc_watch\.py --rebaseline; do not edit -->$")
_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
_OID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SNAPSHOT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.md\Z")
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


# ---------------------------------------------------------------- errors

class ReplayError(Exception):
    """Base class; `exit_code` is what main() returns for it."""
    exit_code = 2


class UsageError(ReplayError):
    """Bad flag value, repo, ref or scratch (exit 2)."""


class SchemaError(ReplayError):
    """Baseline sources.json or git output is not what this tool expects (exit 2)."""


class GitFailure(ReplayError):
    """A permitted git command returned non-zero (exit 2)."""


class GitTimeout(ReplayError):
    exit_code = 1


class IntegrityError(ReplayError):
    """A recovered blob does not hash to its object id (exit 1)."""
    exit_code = 1


class ParityError(ReplayError):
    """The tool's own verdict disagrees with doc_classify (exit 1)."""
    exit_code = 1


class PageError(Exception):
    """One page cannot be analysed; it becomes an ERROR page and the run continues."""


# ---------------------------------------------------------------- git (read-only)

def _git(repo: Path, *args: str) -> bytes:
    """Run one allow-listed git command, list argv, no shell. Returns stdout bytes."""
    if not args or args[0] not in GIT_ALLOWED or "-w" in args:
        raise UsageError(f"git command not allowed: {args[:1]}")
    argv = ["git", "-C", str(repo), *args]
    env = dict(os.environ)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(name, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL, shell=False,
                              timeout=GIT_TIMEOUT_S, env=env, check=False)
    except FileNotFoundError as exc:
        raise UsageError("git is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitTimeout(f"git {args[0]} timed out after {GIT_TIMEOUT_S} s") from exc
    except OSError as exc:
        raise UsageError(f"cannot run git: {type(exc).__name__}") from exc
    if proc.returncode != 0:
        raise GitFailure(f"git {args[0]} failed (exit {proc.returncode})")
    return proc.stdout


def git_object_id(data: bytes, hex_length: int = 40) -> str:
    """Git blob object id of `data` (sha1 for 40-hex ids, sha256 for 64-hex ids)."""
    algo = "sha1" if hex_length == 40 else "sha256"
    return hashlib.new(algo, b"blob %d\0" % len(data) + data).hexdigest()


def resolve_baseline(repo: Path, rev: str) -> tuple[str, str]:
    """(commit sha, commit date) of `rev`; raises UsageError if it is not a commit."""
    if not rev or rev.startswith("-"):
        raise UsageError("baseline revision must not be empty or start with '-'")
    try:
        sha = _git(repo, "rev-parse", "--verify", f"{rev}^{{commit}}").decode("ascii", "replace").strip()
    except GitFailure as exc:
        raise UsageError(f"baseline revision {rev!r} does not resolve to a commit") from exc
    if not _OID_RE.match(sha):
        raise UsageError("git rev-parse returned an unexpected value")
    stamp = _git(repo, "log", "-1", "--format=%cs", sha).decode("ascii", "replace").strip()
    return sha, stamp if _ISO_DATE_RE.match(stamp) else "unknown"


def _wanted_path(path: str) -> bool:
    if path == SOURCES_PATH:
        return True
    prefix = SNAPSHOTS_DIR + "/"
    return path.startswith(prefix) and bool(_SNAPSHOT_NAME_RE.match(path[len(prefix):]))


def recover_baseline(repo: Path, sha: str, scratch: Path) -> dict[str, bytes]:
    """Read the baseline's sources.json and snapshots from git objects and write them, byte for
    byte, under SCRATCH/baseline/. Every blob is re-hashed and compared with its object id."""
    listing = _git(repo, "ls-tree", "-r", "-z", sha, "--", SNAPSHOTS_DIR, SOURCES_PATH)
    wanted: dict[str, str] = {}
    for record in listing.split(b"\0"):
        if not record:
            continue
        meta, tab, raw_path = record.partition(b"\t")
        parts = meta.split(b" ")
        if not tab or len(parts) != 3:
            raise SchemaError("unexpected git ls-tree output")
        mode, kind, oid = (p.decode("ascii", "replace") for p in parts)
        path = raw_path.decode("utf-8", "replace")
        if kind != "blob" or mode not in ("100644", "100755") or not _wanted_path(path):
            continue
        if not _OID_RE.match(oid):
            raise SchemaError("unexpected object id in git ls-tree output")
        wanted[path] = oid
    if SOURCES_PATH not in wanted:
        raise SchemaError("the baseline commit has no maintainer/sources.json")
    out: dict[str, bytes] = {}
    for path in sorted(wanted):
        oid = wanted[path]
        data = _git(repo, "cat-file", "blob", oid)
        if git_object_id(data, len(oid)) != oid:
            raise IntegrityError(f"object id mismatch for {path}")
        dest = scratch / "baseline" / Path(*path.split("/"))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        out[path] = data
    return out


# ---------------------------------------------------------------- scratch and inputs

def _same_or_inside(path: Path, root: Path) -> bool:
    p = Path(os.path.normcase(str(path)))
    r = Path(os.path.normcase(str(root)))
    return p == r or r in p.parents


def check_scratch(scratch: Path, repo: Path) -> Path:
    """Resolved scratch path; refuses a path in or equal to the repo and a non-empty directory."""
    try:
        resolved = Path(scratch).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise UsageError("cannot resolve the scratch path") from exc
    if _same_or_inside(resolved, repo):
        raise UsageError("scratch must be outside the repo")
    if resolved.exists():
        if not resolved.is_dir():
            raise UsageError("scratch exists and is not a directory")
        try:
            non_empty = any(resolved.iterdir())
        except OSError as exc:
            raise UsageError("cannot list the scratch directory") from exc
        if non_empty:
            raise UsageError("scratch directory is not empty")
    return resolved


def read_slug_set(path: Path, what: str) -> set[str]:
    """One page slug per line; blank lines and '#' comments are ignored."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise UsageError(f"cannot read the {what} file") from exc
    found: set[str] = set()
    for number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not _SLUG_RE.match(line):
            raise UsageError(f"{what} file line {number} is not a page slug")
        found.add(line)
    return found


def resolve_ablation(names: list[str] | None) -> list[str]:
    """Canonical reasons to ablate, in ABLATABLE order; every reason when none are given."""
    if not names:
        return list(ABLATABLE)
    lookup = {r.lower(): r for r in ABLATABLE}
    chosen: set[str] = set()
    for name in names:
        reason = lookup.get(name.strip().lower())
        if reason is None:
            raise UsageError(f"unknown reason {name!r}; choose from: " + "; ".join(ABLATABLE))
        chosen.add(reason)
    return [r for r in ABLATABLE if r in chosen]


def parse_sources(raw: bytes) -> dict[str, dict]:
    """Schema-2 sources.json -> {url: {"sha256": str | None, "covers": [...]}}."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SchemaError("baseline sources.json is not valid JSON") from exc
    if not isinstance(data, dict) or data.get("schema") != 2:
        raise SchemaError("baseline sources.json is not schema 2")
    if data.get("normaliser_version") != dc.NORMALISER_VERSION:
        raise SchemaError(f"baseline normaliser_version is not {dc.NORMALISER_VERSION}")
    pages = data.get("pages")
    if not isinstance(pages, dict):
        raise SchemaError("baseline sources.json has no pages object")
    out: dict[str, dict] = {}
    seen: set[str] = set()
    for url, entry in pages.items():
        if not isinstance(url, str) or not isinstance(entry, dict):
            raise SchemaError("baseline sources.json has a malformed page entry")
        sha, covers = entry.get("sha256"), entry.get("covers")
        if sha is not None and not (isinstance(sha, str) and _SHA_RE.match(sha)):
            raise SchemaError("baseline sources.json has a malformed sha256")
        if not isinstance(covers, list) or not all(isinstance(c, str) for c in covers):
            raise SchemaError("baseline sources.json has a malformed covers list")
        page_slug = dc.slug(url)
        if page_slug in seen:
            raise SchemaError("baseline sources.json has duplicate page slugs")
        seen.add(page_slug)
        out[url] = {"sha256": sha, "covers": list(covers)}
    return out


def parse_snapshot(raw: bytes, url: str, side: str) -> str:
    """Body of a snapshot file; PageError (static message, no page text) if it is unusable."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PageError(f"{side} snapshot is not valid UTF-8") from exc
    header, sep, body = text.partition("\n")
    match = HEADER_RE.match(header)
    if not sep or not match:
        raise PageError(f"{side} snapshot has a bad header")
    try:
        date.fromisoformat(match.group(3))
    except ValueError as exc:
        raise PageError(f"{side} snapshot has a bad header date") from exc
    if match.group(1) != url:
        raise PageError(f"{side} snapshot source does not match the page URL")
    if int(match.group(2)) != dc.NORMALISER_VERSION:
        raise PageError(f"{side} snapshot normaliser is not {dc.NORMALISER_VERSION}")
    if dc.normalise(body) != body:
        raise PageError(f"{side} snapshot body is not normalised")
    return body


# ---------------------------------------------------------------- analysis

@dataclass
class HunkView:
    hunk: dc.Hunk
    label: str            # "(preamble)", "section N" or "new section N"; never page text


@dataclass
class PageAnalysis:
    slug: str
    url: str
    status: str                                   # MAJOR | MINOR | CLEAN | ERROR
    error: str = ""
    anomalies: list[str] = field(default_factory=list)
    hunks: list[HunkView] = field(default_factory=list)
    page_lines: int = 1


def _split_lines(body: str) -> list[str]:
    lines = body.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _section_label(key: tuple[str, int], old_keys: list, new_keys: list) -> str:
    if key == ("", 1):
        return "(preamble)"
    if key in old_keys:
        return f"section {old_keys.index(key)}"
    return f"new section {new_keys.index(key)}"


def effective_reasons(hunk: dc.Hunk, removed: frozenset[str] = frozenset(),
                      section_ratio: float | None = None) -> list[str]:
    """R'(h): the hunk's reasons minus `removed`, with "section rewritten" re-decided at
    `section_ratio` (strict >) for paired hunks. Heading added/removed hunks are never run
    through classify_hunk by doc_classify, so the section rule does not apply to them."""
    reasons = list(hunk.reasons)
    if section_ratio is not None and hunk.tag not in HEADING_TAGS:
        reasons = [r for r in reasons if r != dc.REASON_SECTION]
        denom = max(hunk.section_old_len, hunk.section_new_len, 1)
        if hunk.changed / denom > section_ratio:
            reasons.append(dc.REASON_SECTION)
    return [r for r in reasons if r not in removed]


def page_status(pa: PageAnalysis, removed: frozenset[str] = frozenset(),
                section_ratio: float | None = None) -> str:
    """Page verdict under a what-if, mirroring doc_classify._evaluate_page for a page with a
    snapshot, a 200 response and a fresh PageState (so the 30-day age rule never fires)."""
    if pa.anomalies:
        return dc.MAJOR
    if not pa.hunks:
        return dc.CLEAN
    minor_changed = 0
    for view in pa.hunks:
        if effective_reasons(view.hunk, removed, section_ratio):
            return dc.MAJOR
        minor_changed += view.hunk.changed
    if minor_changed / pa.page_lines > dc.CUMULATIVE_MINOR_RATIO:
        return dc.MAJOR
    return dc.MINOR


def analyse_page(page_slug: str, url: str, covers: list[str], old: str, new: str,
                 today: date = FIXED_DATE) -> PageAnalysis:
    """Classify one page (baseline body vs current body) and cross-check it against
    dc.evaluate_page and dc.classify_hunk; ParityError on any disagreement."""
    outcome = dc.FetchOutcome(text=new, status=200)
    result = dc.evaluate_page(old, covers, outcome, dc.PageState(), today)
    if result.status == dc.MAJOR and result.diff is None and not result.anomalies:
        return PageAnalysis(page_slug, url, STATUS_ERROR, error="the classifier raised an exception")
    live = dc.normalise(new)
    anomalies = dc.detect_anomalies(outcome, live, old)
    try:
        diff = dc.diff_page(old, live)
    except Exception:  # noqa: BLE001 - one page must not stop the run
        return PageAnalysis(page_slug, url, STATUS_ERROR, error="the diff raised an exception")
    old_keys = [s.key for s in dc.split_sections(_split_lines(old))]
    new_keys = [s.key for s in dc.split_sections(_split_lines(live))]
    views = [HunkView(h, _section_label(h.section, old_keys, new_keys)) for h in diff.hunks]
    pa = PageAnalysis(page_slug, url, dc.CLEAN, anomalies=list(anomalies), hunks=views,
                      page_lines=diff.page_lines)
    pa.status = page_status(pa)
    if pa.status != result.status or list(result.anomalies) != pa.anomalies:
        raise ParityError(f"{page_slug}: replay verdict {pa.status} differs from "
                          f"evaluate_page {result.status}")
    for view in views:
        h = view.hunk
        expected = [h.tag] if h.tag in HEADING_TAGS else dc.classify_hunk(h)
        if h.reasons != expected or h.major != bool(expected):
            raise ParityError(f"{page_slug}: hunk reasons differ from classify_hunk")
    return pa


def _analyse_url(repo: Path, baseline: dict[str, bytes], url: str, entry: dict) -> PageAnalysis:
    page_slug = dc.slug(url)
    if not _SLUG_RE.match(page_slug):
        return PageAnalysis(page_slug, url, STATUS_ERROR, error="unusable page slug")
    try:
        raw_old = baseline.get(f"{SNAPSHOTS_DIR}/{page_slug}.md")
        if raw_old is None:
            raise PageError("baseline snapshot is missing")
        old = parse_snapshot(raw_old, url, "baseline")
        try:
            raw_new = (repo / SNAPSHOTS_DIR / f"{page_slug}.md").read_bytes()
        except OSError as exc:
            raise PageError("current snapshot is missing or unreadable") from exc
        new = parse_snapshot(raw_new, url, "current")
    except PageError as exc:
        return PageAnalysis(page_slug, url, STATUS_ERROR, error=str(exc))
    return analyse_page(page_slug, url, entry["covers"], old, new)


def find_no_baseline(repo: Path, baselined: set[str]) -> list[str]:
    """Current snapshot files whose page has no baselined snapshot (reported, never errors)."""
    try:
        names = sorted(p.name for p in (repo / SNAPSHOTS_DIR).iterdir() if p.is_file())
    except OSError:
        return []
    return [n[:-3] for n in names if _SNAPSHOT_NAME_RE.match(n) and n[:-3] not in baselined]


# ---------------------------------------------------------------- what-ifs

def scenario(analyses: list[PageAnalysis], removed: frozenset[str] = frozenset(),
             section_ratio: float | None = None) -> dict:
    """MAJOR count under a what-if, and the pages that flip: baseline MAJOR, not anomaly-driven,
    not MAJOR any more. ERROR pages are ignored."""
    major_after = 0
    flips: list[str] = []
    for pa in analyses:
        if pa.status == STATUS_ERROR:
            continue
        after = page_status(pa, removed, section_ratio)
        if after == dc.MAJOR:
            major_after += 1
        if pa.status == dc.MAJOR and not pa.anomalies and after != dc.MAJOR:
            flips.append(pa.slug)
    return {"major_after": major_after, "flips": sorted(flips)}


# Held-MAJOR report and feasibility.
#
# For a set R of removed reasons the page verdict is exactly (page_status with removed=R):
#     MAJOR(R) = B  union  {p : mask(p) not subset of R}
# where B = pages held MAJOR whatever is removed (any anomaly, or all-hunk changed lines /
# page_lines > CUMULATIVE_MINOR_RATIO) and mask(p) = union of the reasons carried by p's hunks.
# Both terms are independent of R except the second. With R = all ten reasons every mask is a
# subset, so MAJOR(all) == B; build_report asserts that identity (ParityError, exit 1).

def _changed_total(pa: PageAnalysis, skip_headings: bool = False) -> int:
    return sum(v.hunk.changed for v in pa.hunks if not (skip_headings and v.hunk.tag in HEADING_TAGS))


def _pct(changed: int, page_lines: int) -> float:
    return round(float(Fraction(100 * changed, page_lines)), 1)


def blocked_label(pa: PageAnalysis) -> str | None:
    """None, "anomaly: <label>", or "cumulative minor <x>% > 15%"; same tests as page_status."""
    if pa.anomalies:
        return "anomaly: " + "; ".join(pa.anomalies)
    if _changed_total(pa) / pa.page_lines > dc.CUMULATIVE_MINOR_RATIO:
        return (f"cumulative minor {_pct(_changed_total(pa), pa.page_lines)}% > "
                f"{dc.CUMULATIVE_MINOR_RATIO:.0%}")
    return None


def page_hold(pa: PageAnalysis) -> dict:
    """Per-page held-MAJOR fields (slugs and reason names only; never snapshot text)."""
    blocked = blocked_label(pa)
    mask = sorted({r for v in pa.hunks for r in v.hunk.reasons})
    keep = None
    if mask and blocked is None:
        counts = [(sum(1 for v in pa.hunks if r in v.hunk.reasons), r)
                  for r in ABLATABLE if r in mask]
        fewest = min(counts, key=lambda t: t[0])      # first minimum = ABLATABLE order
        keep = {"reason": fewest[1], "hunks": fewest[0]}
    return {"slug": pa.slug, "status": pa.status, "blocked": blocked,
            "cum_pct": _pct(_changed_total(pa), pa.page_lines),
            "cum_pct_excluding_heading_hunks": _pct(_changed_total(pa, True), pa.page_lines),
            "mask": mask, "minimal_keep": keep}


def feasibility(ok: list[PageAnalysis], target: Fraction) -> dict:
    """Can the target be reached at all, and only by removing every reason?"""
    analysed = len(ok)
    blocked = sum(1 for pa in ok if blocked_label(pa) is not None)
    limit = math.floor(target * analysed / 100)
    everything = frozenset(ABLATABLE)
    if scenario(ok, everything)["major_after"] != blocked:
        raise ParityError("MAJOR with every reason removed differs from the blocked page count")
    # Removal only lowers MAJOR, so a proper subset meets the target iff some 9-reason set does.
    degenerate = blocked <= limit and all(
        scenario(ok, everything - {r})["major_after"] > limit for r in ABLATABLE)
    return {"blocked_count": blocked, "target_count": limit,
            "reachable_without_recall": blocked <= limit, "degenerate_floor_flag": degenerate}


def target_met(major: int, analysed: int, target: Fraction) -> bool:
    """MAJOR share <= target percent (equal counts as met)."""
    return analysed > 0 and Fraction(100 * major, analysed) <= target


def reason_table(analyses: list[PageAnalysis]) -> list[tuple[str, int, int]]:
    """(reason, pages with it, hunks with it), pages descending then reason name."""
    pages: Counter[str] = Counter()
    hunks: Counter[str] = Counter()
    for pa in analyses:
        if pa.status == STATUS_ERROR:
            continue
        seen: set[str] = set()
        for view in pa.hunks:
            for reason in view.hunk.reasons:
                hunks[reason] += 1
                seen.add(reason)
        pages.update(seen)
    return sorted(((r, pages[r], hunks[r]) for r in pages), key=lambda t: (-t[1], t[0]))


def _clean(line: str, limit: int = 200) -> str:
    return "".join(ch if ch.isprintable() else "?" for ch in line)[:limit]


def _hunk_dict(view: HunkView, show_lines: int) -> dict:
    h = view.hunk
    out = {"section": view.label, "tag": h.tag, "old_lines": len(h.old_lines),
           "new_lines": len(h.new_lines), "reasons": list(h.reasons), "major": h.major}
    if show_lines > 0:
        out["section_title"] = _clean(h.section[0] or "(preamble)")
        out["old"] = [_clean(x) for x in h.old_lines[:show_lines]]
        out["new"] = [_clean(x) for x in h.new_lines[:show_lines]]
    return out


def _page_dict(pa: PageAnalysis, show_lines: int) -> dict:
    if pa.status == STATUS_ERROR:
        return {"slug": pa.slug, "status": pa.status, "error": pa.error}
    notes = list(pa.anomalies)
    if pa.status == dc.MAJOR and not pa.anomalies and not any(v.hunk.reasons for v in pa.hunks):
        notes.append(f"minor changes add up to more than {dc.CUMULATIVE_MINOR_RATIO:.0%} of the page")
    out = {"slug": pa.slug, "status": pa.status, "anomalies": list(pa.anomalies), "notes": notes,
           "hunks": [_hunk_dict(v, show_lines) for v in pa.hunks]}
    out.update({k: v for k, v in page_hold(pa).items() if k not in ("slug", "status")})
    return out


def _row(label: str, scen: dict, analysed: int, target: Fraction, truth: set[str] | None) -> dict:
    major = scen["major_after"]
    return {"label": label, "major_after": major,
            "pct": round(float(Fraction(100 * major, analysed)), 1) if analysed else 0.0,
            "target_met": target_met(major, analysed, target),
            "flips": list(scen["flips"]),
            "flipped_ground_truth": None if truth is None else sorted(set(scen["flips"]) & truth)}


def build_report(*, rev: str, sha: str, commit_date: str, analyses: list[PageAnalysis],
                 rubric_skipped: int, no_baseline: list[str], ablate: list[str],
                 ratios: list[float], target: Fraction, truth: set[str] | None,
                 expected: set[str] | None, show_lines: int = 0) -> dict:
    ok = [pa for pa in analyses if pa.status != STATUS_ERROR]
    analysed = len(ok)
    major = sorted(pa.slug for pa in ok if pa.status == dc.MAJOR)
    removal = [_row("(none)", scenario(ok), analysed, target, truth)]
    for reason in ablate:
        row = _row(reason, scenario(ok, frozenset({reason})), analysed, target, truth)
        row["reason"] = reason
        removal.append(row)
    removal[0]["reason"] = None
    sweep = []
    for t in ratios:
        row = _row(f"section ratio > {t:g}", scenario(ok, section_ratio=t), analysed, target, truth)
        row["threshold"] = t
        sweep.append(row)
    return {
        "baseline": {"rev": rev, "sha": sha, "date": commit_date},
        "classifier": {
            "file_sha256": hashlib.sha256(Path(dc.__file__).read_bytes()).hexdigest(),
            "normaliser": dc.NORMALISER_VERSION,
            "section_major_ratio": dc.SECTION_MAJOR_RATIO,
            "cumulative_minor_ratio": dc.CUMULATIVE_MINOR_RATIO},
        "current": "working-tree",
        "counts": {
            "baselined": len(analyses), "analysed": analysed,
            "errors": len(analyses) - analysed, "rubric_skipped": rubric_skipped,
            "no_baseline": len(no_baseline),
            "anomaly_driven_major": sum(1 for pa in ok if pa.status == dc.MAJOR and pa.anomalies),
            "changed_pages": sum(1 for pa in ok if pa.hunks)},
        "no_baseline": list(no_baseline),
        "pages": [_page_dict(pa, show_lines) for pa in analyses],
        "summary": {
            "major": len(major), "analysed": analysed,
            "pct": round(float(Fraction(100 * len(major), analysed)), 1) if analysed else 0.0,
            "target_pct": float(target), "target_met": target_met(len(major), analysed, target),
            "major_slugs": major},
        "reasons": [{"reason": r, "pages": p, "hunks": h} for r, p, h in reason_table(analyses)],
        "feasibility": feasibility(ok, target),
        "removal": removal,
        "section_sweep": sweep,
        "expected_major": None if expected is None else {
            "missing": sorted(expected - set(major)), "unexpected": sorted(set(major) - expected)},
    }


# ---------------------------------------------------------------- rendering

def render_json(rep: dict) -> str:
    return json.dumps(rep, indent=2, sort_keys=True)


def _row_line(row: dict) -> str:
    gt = "-" if row["flipped_ground_truth"] is None else str(len(row["flipped_ground_truth"]))
    met = "yes" if row["target_met"] else "no"
    return (f"  {row['label']:<28} {row['major_after']:>5}  {row['pct']:>5.1f}%  {met:<3}  "
            f"{len(row['flips']):>5}  {gt:>6}")


def _row_details(rows: list[dict]) -> list[str]:
    lines = []
    for row in rows:
        if row["flips"]:
            lines.append(f"  flips under {row['label']}: {', '.join(row['flips'])}")
        if row["flipped_ground_truth"]:
            lines.append(f"  flipped ground truth under {row['label']}: "
                         f"{', '.join(row['flipped_ground_truth'])}")
    return lines


def render_text(rep: dict) -> str:
    b, c, k, s = rep["baseline"], rep["classifier"], rep["counts"], rep["summary"]
    out = ["Doc-watch replay (read-only)",
           f"baseline: {b['sha']} ({b['date']})  rev: {b['rev']}",
           f"current:  {rep['current']}",
           f"classifier: doc_classify.py sha256:{c['file_sha256'][:12]}  normaliser {c['normaliser']}"
           f"  section ratio {c['section_major_ratio']}  cumulative minor {c['cumulative_minor_ratio']}",
           f"pages: {k['baselined']} baselined, {k['analysed']} analysed, {k['errors']} error(s), "
           f"{k['rubric_skipped']} rubric skipped, {k['no_baseline']} without a baseline",
           f"anomaly-driven MAJOR: {k['anomaly_driven_major']}",
           f"changed pages (any hunk): {k['changed_pages']}"]
    if rep["no_baseline"]:
        out.append("without a baseline: " + ", ".join(rep["no_baseline"]))
    out += ["", "Pages"]
    for page in rep["pages"]:
        if page["status"] == STATUS_ERROR:
            out.append(f"[ERROR] {page['slug']}: {page['error']}")
            continue
        out.append(f"[{page['status']}] {page['slug']}")
        out += [f"    note: {n}" for n in page["notes"]]
        for h in page["hunks"]:
            why = ", ".join(h["reasons"]) or "minor"
            out.append(f"    {h['section']} [{h['tag']}] -{h['old_lines']} +{h['new_lines']}: {why}")
            if "section_title" in h:
                out.append(f"      @@ {h['section_title']} @@")
                out += [f"      - {x}" for x in h["old"]] + [f"      + {x}" for x in h["new"]]
    met = "met" if s["target_met"] else "NOT met"
    out += ["", "Summary",
            f"  MAJOR: {s['major']} of {s['analysed']} analysed pages ({s['pct']:.1f}%); "
            f"target <= {s['target_pct']:g}%: {met}",
            "", "Reasons (pages with at least one hunk carrying the reason)",
            f"  {'pages':>5} {'hunks':>5}  reason"]
    out += [f"  {r['pages']:>5} {r['hunks']:>5}  {r['reason']}" for r in rep["reasons"]] or ["  (none)"]
    f = rep["feasibility"]
    out += ["", "Held MAJOR (per analysed page; cum% = all hunks, cum% no-heading = without "
            "unpaired heading hunks)"]
    for page in rep["pages"]:
        if page["status"] == STATUS_ERROR:
            continue
        keep = page["minimal_keep"]
        out.append(f"  {page['slug']} [{page['status']}] blocked: {page['blocked'] or 'no'}; "
                   f"cum {page['cum_pct']:.1f}%; cum no-heading "
                   f"{page['cum_pct_excluding_heading_hunks']:.1f}%; mask: "
                   f"{', '.join(page['mask']) or '(none)'}; minimal keep: "
                   f"{'none' if keep is None else keep['reason'] + ' (' + str(keep['hunks']) + ' hunk(s))'}")
    out += ["", "Feasibility",
            f"  blocked pages |B|: {f['blocked_count']}; target count T: {f['target_count']}; "
            f"reachable without recall: {'yes' if f['reachable_without_recall'] else 'no'}; "
            f"degenerate floor (only all ten reasons removed meets target): "
            f"{'yes' if f['degenerate_floor_flag'] else 'no'}"]
    head = f"  {'':<28} {'MAJOR':>5}  {'pct':>6}  met  {'flips':>5}  {'gt':>6}"
    out += ["", "Removal ablation (MAJOR count after removing one reason)", head]
    out += [_row_line(r) for r in rep["removal"]] + _row_details(rep["removal"])
    out += ["", "Section-rewrite sweep (loosened threshold; classifier uses "
            f"{c['section_major_ratio']})", head]
    out += [_row_line(r) for r in rep["section_sweep"]] + _row_details(rep["section_sweep"])
    if rep["expected_major"] is not None:
        em = rep["expected_major"]
        out += ["", "Expected-major mismatches",
                "  expected but not MAJOR: " + (", ".join(em["missing"]) or "none"),
                "  MAJOR but not expected: " + (", ".join(em["unexpected"]) or "none")]
    return "\n".join(out)


# ---------------------------------------------------------------- main

def _fraction(value: str) -> Fraction:
    try:
        parsed = Fraction(value)
    except (ValueError, ZeroDivisionError) as exc:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from exc
    if not 0 <= parsed <= 100:
        raise argparse.ArgumentTypeError("must be between 0 and 100")
    return parsed


def _non_negative(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="replay_doc_watch.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 allow_abbrev=False)
    ap.add_argument("--scratch", type=Path, required=True,
                    help="new or empty directory outside the repo for the recovered baseline")
    ap.add_argument("--repo", type=Path, help="repo root (default: this script's repo)")
    ap.add_argument("--baseline", default=DEFAULT_BASELINE, help="baseline revision")
    ap.add_argument("--current", choices=["working-tree"], default="working-tree",
                    help="the live side: working-tree snapshots")
    ap.add_argument("--ablate", nargs="+", metavar="REASON",
                    help="reasons to remove one at a time (default: all; quote multi-word names)")
    ap.add_argument("--section-ratio", nargs="+", type=float, metavar="T",
                    help="section-rewrite thresholds to sweep (default: 0.5 0.6 0.75)")
    ap.add_argument("--ground-truth", type=Path, help="slugs that truly need a check, one per line")
    ap.add_argument("--expected-major", type=Path, help="slugs expected to be MAJOR, one per line")
    ap.add_argument("--target-pct", type=_fraction, default=Fraction(DEFAULT_TARGET_PCT),
                    help="target MAJOR share in percent (default 25)")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    ap.add_argument("--show-lines", type=_non_negative, default=0, metavar="N",
                    help="also print up to N old and N new snapshot lines per hunk (default 0)")
    ap.add_argument("--page", metavar="SLUG", help="analyse only this page")
    return ap


def run(args: argparse.Namespace) -> dict:
    ablate = resolve_ablation(args.ablate)
    ratios = list(args.section_ratio) if args.section_ratio else list(DEFAULT_SECTION_RATIOS)
    if any(not math.isfinite(t) or t < 0 for t in ratios):
        raise UsageError("--section-ratio values must be finite and not negative")
    truth = read_slug_set(args.ground_truth, "ground-truth") if args.ground_truth else None
    expected = read_slug_set(args.expected_major, "expected-major") if args.expected_major else None

    repo = Path(args.repo).resolve() if args.repo else ROOT
    if not (repo / ".git").exists():
        raise UsageError("the repo has no .git")
    scratch = check_scratch(args.scratch, repo)
    sha, commit_date = resolve_baseline(repo, args.baseline)
    try:
        scratch.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UsageError("cannot create the scratch directory") from exc
    baseline = recover_baseline(repo, sha, scratch)

    sources = parse_sources(baseline[SOURCES_PATH])
    entries = sorted(sources.items(), key=lambda kv: dc.slug(kv[0]))
    rubric_skipped = sum(1 for _, e in entries if e["sha256"] is None)
    baselined = [(u, e) for u, e in entries if e["sha256"] is not None]
    baselined_slugs = {dc.slug(u) for u, _ in baselined}
    if args.page:
        if args.page not in baselined_slugs:
            raise UsageError("--page is not a baselined page")
        baselined = [(u, e) for u, e in baselined if dc.slug(u) == args.page]
    if not baselined:
        raise SchemaError("the baseline has no baselined pages")
    analyses = [_analyse_url(repo, baseline, u, e) for u, e in baselined]
    return build_report(
        rev=args.baseline, sha=sha, commit_date=commit_date, analyses=analyses,
        rubric_skipped=rubric_skipped, no_baseline=find_no_baseline(repo, baselined_slugs),
        ablate=ablate, ratios=ratios, target=args.target_pct, truth=truth, expected=expected,
        show_lines=args.show_lines)


def main(argv: list[str] | None = None, *, out=None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    try:
        report = run(args)
        text = render_json(report) if args.format == "json" else render_text(report)
    except ReplayError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return exc.exit_code
    stream = out if out is not None else sys.stdout
    try:
        stream.write(text + "\n")
        stream.flush()
    except (BrokenPipeError, OSError, ValueError):
        return 1
    if report["counts"]["analysed"] == 0:
        print("ERROR: every page failed to analyse", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
