"""Claudit doc-watch classifier.

Pure functions only: no I/O, no network, no clock reads (the caller passes `today`).
Decides whether a live documentation page differs from its verified snapshot in a way
that needs a human (MAJOR), only cosmetically (MINOR), or not at all (CLEAN).

All thresholds live here, never in sources.json: a tampered sources.json must not be
able to weaken the classifier. Standard library only.
"""

from __future__ import annotations

import difflib
import hashlib
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date

# ---------------------------------------------------------------- constants

NORMALISER_VERSION = 2
MIN_PAGE_CHARS = 500
SHRINK_ANOMALY_RATIO = 0.30
HTML_SNIFF_BYTES = 512
SECTION_MAJOR_RATIO = 0.40
CUMULATIVE_MINOR_RATIO = 0.15
MINOR_MAX_AGE_DAYS = 30        # strictly > 30
UNREACHABLE_ALERT_DAYS = 3     # >= 3
FETCH_ATTEMPTS = 3
FETCH_RETRY_DELAY_S = 60
REBASELINE_STABLE_FETCHES = 3
DIGEST_HUNK_MAX_LINES = 40
DIGEST_PAGE_MAX_LINES = 120
DIGEST_BODY_MAX_CHARS = 60000
STATE_MAX_CHARS = 20000

# Spec: "last snapshot heading missing and live >10% shorter" (truncation heuristic).
TRUNCATED_SHORTER_RATIO = 0.10
# Spec: "keep the existing versions[:10] fallback" when the last reviewed version is gone.
CHANGELOG_FALLBACK_VERSIONS = 10

# ---------------------------------------------------------------- regexes

FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
FENCE_CLOSE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*$")
HEADING_RE = re.compile(r"^#{1,4} ")          # section boundary, outside fences only
TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$")
CODE_TOKEN_RE = re.compile(r"`([^`\n]+)`")
VERSION_RE = re.compile(r"\bv\d+\.\d+(?:\.\d+)*\b")
NUMBER_RE = re.compile(r"\d+(?:\.\d+)*")
MODAL_RE = re.compile(
    # Longest alternatives first so "can't"/"cannot" and "no longer" match whole, not as
    # their prefixes "can" / "no". Same word set as the approved spec.
    r"\b(must|never|required|deprecated|removed|default|only|no longer|cannot|can't|can|"
    r"won't|don't|doesn't|will|should|may|not|no|without|always)\b",
    re.IGNORECASE,
)
HTML_SNIFF_RE = re.compile(r"\A\s*(<!doctype html|<html)", re.IGNORECASE)

# Link targets. The spec requires a link-TARGET change to be MAJOR and a link-TEXT change
# MINOR but gives no regex, so these are defined here: inline `](target)`, reference
# definitions `[id]: target`, and bare http(s) URLs. Link text is deliberately not captured.
LINK_INLINE_RE = re.compile(r"\]\(\s*<?([^)\s>]+)")
LINK_REF_RE = re.compile(r"^\s*\[[^\]\n]+\]:\s*<?(\S+?)>?\s*$", re.MULTILINE)
BARE_URL_RE = re.compile(r"https?://[^\s)>\]\"']+")

_WS_RE = re.compile(r"\s+")
VERSION_HEADING = re.compile(r"^##\s+\[?v?(\d+\.\d+\.\d+)\]?", re.MULTILINE)

# ---------------------------------------------------------------- statuses and reasons

CLEAN = "CLEAN"
MINOR = "MINOR"
MAJOR = "MAJOR"
UNREACHABLE = "UNREACHABLE"
NO_BASELINE = "NO_BASELINE"

ANOM_EMPTY = "fetch anomaly: page empty or too short"
ANOM_HTML = "fetch anomaly: response looks like an HTML page"
ANOM_SHRANK = "fetch anomaly: page shrank sharply against the snapshot"
ANOM_TRUNCATED = "fetch anomaly: response looks truncated"
ANOM_GONE = "page moved or removed"

REASON_TABLE = "table row changed"
REASON_FENCE = "code fence changed"
REASON_CODE_TOKEN = "code token changed"
REASON_VERSION = "version changed"
REASON_NUMBER = "number changed"
REASON_MODAL = "modal changed"
REASON_LINK = "link target changed"
REASON_SECTION = "section rewritten"
REASON_HEADING_ADDED = "heading added"
REASON_HEADING_REMOVED = "heading removed"


# ---------------------------------------------------------------- hashing and slugs

def sha256(text: str) -> str:
    """SHA-256 hex digest of the UTF-8 encoding of `text`."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def slug(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".md")


def changelog_versions(text: str) -> list[str]:
    """Version headings in file order (newest first in the Claude Code changelog)."""
    return VERSION_HEADING.findall(text)


# ---------------------------------------------------------------- normalisers

def _lines(body: str) -> list[str]:
    """Split on "\\n" only (the normaliser's own line model); drop one trailing empty."""
    parts = body.split("\n")
    if parts and parts[-1] == "":
        parts.pop()
    return parts


def fence_flags(lines: list[str]) -> list[bool]:
    """True for lines inside a code fence or on its delimiter lines.

    An unclosed fence flags every line to EOF so changes there are treated as code (MAJOR).
    """
    flags: list[bool] = []
    marker: str | None = None
    for line in lines:
        if marker is None:
            m = FENCE_RE.match(line)
            if m:
                marker = m.group(1)
                flags.append(True)
            else:
                flags.append(False)
            continue
        flags.append(True)
        m = FENCE_CLOSE_RE.match(line)
        if m and m.group(1)[0] == marker[0] and len(m.group(1)) >= len(marker):
            marker = None
    return flags


def normalise(text: str) -> str:
    """Normaliser v2: remove formatting noise so only content changes alter the hash.

    CRLF/CR -> LF; NFC; NBSP -> space; U+200B and U+FEFF removed; outside code fences each
    line has whitespace runs collapsed to one space and is stripped, and blank-line runs are
    collapsed; inside fences lines are only right-stripped (indentation and blank runs are
    content); exactly one trailing newline. Idempotent.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = unicodedata.normalize("NFC", text)
    text = text.replace(" ", " ").replace("​", "").replace("﻿", "")
    lines = text.split("\n")
    flags = fence_flags(lines)
    out: list[str] = []
    prev_blank_outside = False
    for line, in_fence in zip(lines, flags):
        if in_fence:
            out.append(line.rstrip())
            prev_blank_outside = False
            continue
        stripped = _WS_RE.sub(" ", line).strip()
        if stripped and FENCE_RE.match(stripped) and not FENCE_RE.match(line):
            # Stripping must not turn indented code text into a fence (keeps normalise
            # idempotent): keep it visibly indented by 4 spaces, which is not a fence.
            stripped = "    " + stripped
        if not stripped:
            if prev_blank_outside:
                continue
            prev_blank_outside = True
        else:
            prev_blank_outside = False
        out.append(stripped)
    return "\n".join(out).strip("\n") + "\n"


def normalise_v1(text: str) -> str:
    """The pre-redesign normaliser, kept only so --migrate can reproduce the old hashes."""
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    out: list[str] = []
    for line in lines:
        if not line and out and not out[-1]:
            continue  # collapse runs of blank lines
        out.append(line)
    return "\n".join(out).strip() + "\n"


# ---------------------------------------------------------------- sections and diffs

@dataclass
class Section:
    title: str                 # the heading line, stripped; "" for the preamble
    occurrence: int            # 1-based count of sections with the same title
    lines: list[str] = field(default_factory=list)
    flags: list[bool] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, int]:
        return (self.title, self.occurrence)


def split_sections(lines: list[str]) -> list[Section]:
    """Split on HEADING_RE outside fences. The preamble section (title "") always exists."""
    flags = fence_flags(lines)
    seen: Counter[str] = Counter()
    seen[""] = 1
    sections = [Section("", 1)]
    for line, in_fence in zip(lines, flags):
        if not in_fence and HEADING_RE.match(line):
            title = line.strip()
            seen[title] += 1
            sections.append(Section(title, seen[title]))
        sections[-1].lines.append(line)
        sections[-1].flags.append(in_fence)
    return sections


@dataclass
class Hunk:
    section: tuple[str, int]
    tag: str                          # replace | delete | insert | heading added | heading removed
    old_lines: tuple[str, ...]
    new_lines: tuple[str, ...]
    old_flags: tuple[bool, ...]       # fence flags for old_lines
    new_flags: tuple[bool, ...]
    old_start: int                    # index within the section's lines
    new_start: int
    section_old_len: int
    section_new_len: int
    reasons: list[str] = field(default_factory=list)
    major: bool = False

    @property
    def changed(self) -> int:
        return max(len(self.old_lines), len(self.new_lines))


@dataclass
class PageDiff:
    hunks: list[Hunk]
    old_line_count: int
    new_line_count: int

    @property
    def major_hunks(self) -> list[Hunk]:
        return [h for h in self.hunks if h.major]

    @property
    def minor_hunks(self) -> list[Hunk]:
        return [h for h in self.hunks if not h.major]

    @property
    def minor_changed(self) -> int:
        return sum(h.changed for h in self.minor_hunks)

    @property
    def page_lines(self) -> int:
        return max(self.old_line_count, self.new_line_count, 1)


def _counter(pattern: re.Pattern[str], text: str) -> Counter[str]:
    return Counter(pattern.findall(text))


def _modals(text: str) -> Counter[str]:
    # Typographic apostrophes are folded to ASCII for matching only (normalise() and the
    # hashes are unchanged), so "can’t" and "can't" count as the same modal.
    text = text.replace("’", "'").replace("‘", "'")
    return Counter(m.lower() for m in MODAL_RE.findall(text))


def _link_targets(text: str) -> Counter[str]:
    targets: Counter[str] = Counter()
    targets.update(LINK_INLINE_RE.findall(text))
    targets.update(LINK_REF_RE.findall(text))
    targets.update(BARE_URL_RE.findall(text))
    return targets


def classify_hunk(hunk: Hunk) -> list[str]:
    """Return the MAJOR reasons for one hunk; an empty list means MINOR."""
    old_text = "\n".join(hunk.old_lines)
    new_text = "\n".join(hunk.new_lines)
    reasons: list[str] = []
    if any(TABLE_LINE_RE.match(line) for line in hunk.old_lines + hunk.new_lines):
        reasons.append(REASON_TABLE)
    if any(hunk.old_flags) or any(hunk.new_flags):
        reasons.append(REASON_FENCE)
    if _counter(CODE_TOKEN_RE, old_text) != _counter(CODE_TOKEN_RE, new_text):
        reasons.append(REASON_CODE_TOKEN)
    if _counter(VERSION_RE, old_text) != _counter(VERSION_RE, new_text):
        reasons.append(REASON_VERSION)
    if _counter(NUMBER_RE, old_text) != _counter(NUMBER_RE, new_text):
        reasons.append(REASON_NUMBER)
    if _modals(old_text) != _modals(new_text):
        reasons.append(REASON_MODAL)
    if _link_targets(old_text) != _link_targets(new_text):
        reasons.append(REASON_LINK)
    denom = max(hunk.section_old_len, hunk.section_new_len, 1)
    if hunk.changed / denom > SECTION_MAJOR_RATIO:
        reasons.append(REASON_SECTION)
    return reasons


def _unpaired(section: Section, removed: bool) -> Hunk:
    n = len(section.lines)
    lines = tuple(section.lines)
    flags = tuple(section.flags)
    return Hunk(
        section=section.key,
        tag=REASON_HEADING_REMOVED if removed else REASON_HEADING_ADDED,
        old_lines=lines if removed else (),
        new_lines=() if removed else lines,
        old_flags=flags if removed else (),
        new_flags=() if removed else flags,
        old_start=0, new_start=0,
        section_old_len=n if removed else 0,
        section_new_len=0 if removed else n,
        reasons=[REASON_HEADING_REMOVED if removed else REASON_HEADING_ADDED],
        major=True,
    )


def _diff_section(old: Section, new: Section) -> list[Hunk]:
    hunks: list[Hunk] = []
    matcher = difflib.SequenceMatcher(None, old.lines, new.lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        hunk = Hunk(
            section=old.key, tag=tag,
            old_lines=tuple(old.lines[i1:i2]), new_lines=tuple(new.lines[j1:j2]),
            old_flags=tuple(old.flags[i1:i2]), new_flags=tuple(new.flags[j1:j2]),
            old_start=i1, new_start=j1,
            section_old_len=len(old.lines), section_new_len=len(new.lines),
        )
        hunk.reasons = classify_hunk(hunk)
        hunk.major = bool(hunk.reasons)
        hunks.append(hunk)
    return hunks


def diff_page(snapshot_body: str, live_body: str) -> PageDiff:
    """Section-aware diff of two already-normalised bodies, with every hunk classified."""
    old_lines = _lines(snapshot_body)
    new_lines = _lines(live_body)
    old_secs = split_sections(old_lines)
    new_secs = split_sections(new_lines)
    old_keys = {s.key: s for s in old_secs}
    new_keys = {s.key: s for s in new_secs}
    hunks: list[Hunk] = []
    for sec in old_secs:
        other = new_keys.get(sec.key)
        if other is None:
            hunks.append(_unpaired(sec, removed=True))
        else:
            hunks.extend(_diff_section(sec, other))
    for sec in new_secs:
        if sec.key not in old_keys:
            hunks.append(_unpaired(sec, removed=False))
    return PageDiff(hunks, len(old_lines), len(new_lines))


# ---------------------------------------------------------------- fetch outcome and anomalies

@dataclass(frozen=True)
class FetchOutcome:
    """What the I/O layer observed for one URL (the classifier never fetches)."""
    text: str | None = None
    status: int = 200
    declared_length: int | None = None   # Content-Length, if sent
    received: int | None = None          # bytes actually read
    read_error: bool = False             # HTTPException (e.g. IncompleteRead) while reading
    unreachable: bool = False            # transport failure: no usable response at all
    error: str = ""


def is_empty(normalised: str) -> bool:
    return len(normalised) < MIN_PAGE_CHARS


def looks_like_html(normalised: str) -> bool:
    return HTML_SNIFF_RE.match(normalised[:HTML_SNIFF_BYTES]) is not None


def shrank(snapshot_body: str, live_normalised: str) -> bool:
    """Live is shorter than the snapshot by more than SHRINK_ANOMALY_RATIO."""
    if not snapshot_body:
        return False
    return (len(snapshot_body) - len(live_normalised)) / len(snapshot_body) > SHRINK_ANOMALY_RATIO


def length_truncated(declared_length: int | None, received: int | None, read_error: bool) -> bool:
    """Declared Content-Length exceeds what was received, or reading raised HTTPException."""
    if read_error:
        return True
    return declared_length is not None and received is not None and declared_length > received


def last_heading_missing(snapshot_body: str, live_body: str) -> bool:
    """The snapshot's last heading (outside fences) has no section in the live page."""
    old_titles = [s.title for s in split_sections(_lines(snapshot_body)) if s.title]
    if not old_titles:
        return False
    live_titles = {s.title for s in split_sections(_lines(live_body)) if s.title}
    return old_titles[-1] not in live_titles


def detect_anomalies(outcome: FetchOutcome, live_normalised: str,
                     snapshot_body: str | None) -> list[str]:
    """Anomaly reasons for a fetched page; any non-empty result means MAJOR."""
    if outcome.status in (404, 410):
        return [ANOM_GONE]
    found: list[str] = []
    if is_empty(live_normalised):
        found.append(ANOM_EMPTY)
    if looks_like_html(live_normalised):
        found.append(ANOM_HTML)
    truncated = length_truncated(outcome.declared_length, outcome.received, outcome.read_error)
    if snapshot_body:
        if shrank(snapshot_body, live_normalised):
            found.append(ANOM_SHRANK)
        if (last_heading_missing(snapshot_body, live_normalised)
                and len(live_normalised) < len(snapshot_body) * (1 - TRUNCATED_SHORTER_RATIO)):
            truncated = True
    if truncated:
        found.append(ANOM_TRUNCATED)
    return found


# ---------------------------------------------------------------- state and results

@dataclass(frozen=True)
class PageState:
    minor_first_seen: str | None = None     # ISO date
    unreachable_since: str | None = None    # ISO date


@dataclass(frozen=True)
class ChangelogState:
    new_versions: tuple[str, ...] = ()
    first_seen: str | None = None           # ISO date


@dataclass
class PageResult:
    status: str
    reasons: list[str] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)
    diff: PageDiff | None = None
    state: PageState = field(default_factory=PageState)
    detail: str = ""


@dataclass
class ChangelogResult:
    status: str
    reasons: list[str] = field(default_factory=list)
    new_versions: list[str] = field(default_factory=list)
    state: ChangelogState = field(default_factory=ChangelogState)


def _days_since(iso: str, today: date) -> int:
    return (today - date.fromisoformat(iso)).days


def _hunk_reason(hunk: Hunk) -> str:
    return f"{hunk.section[0] or '(preamble)'}: {', '.join(hunk.reasons)}"


def _evaluate_page(snapshot_body: str | None, covers: tuple[str, ...] | list[str],
                   outcome: FetchOutcome, state: PageState, today: date) -> PageResult:
    # (1) transport failure
    if outcome.unreachable:
        since = state.unreachable_since or today.isoformat()
        days = _days_since(since, today)
        new_state = replace(state, unreachable_since=since)
        if days >= UNREACHABLE_ALERT_DAYS:
            return PageResult(MAJOR, [f"unreachable {days} days"], state=new_state,
                              detail=outcome.error)
        return PageResult(UNREACHABLE, [f"unreachable {days} days"], state=new_state,
                          detail=outcome.error)
    state = replace(state, unreachable_since=None)

    # (2) anomalies
    live = normalise(outcome.text or "")
    anomalies = detect_anomalies(outcome, live, snapshot_body)
    if anomalies:
        return PageResult(MAJOR, list(anomalies), anomalies=anomalies, state=state)

    # (3) no snapshot
    if snapshot_body is None:
        if covers:
            return PageResult(MAJOR, ["no baseline"], state=state)
        return PageResult(NO_BASELINE, ["no baseline (rubric page)"], state=state)

    # (4) diff
    diff = diff_page(snapshot_body, live)
    if not diff.hunks:
        # (6) nothing changed
        return PageResult(CLEAN, state=replace(state, minor_first_seen=None), diff=diff)
    if diff.major_hunks:
        return PageResult(MAJOR, [_hunk_reason(h) for h in diff.major_hunks],
                          diff=diff, state=state)

    # (5) minor hunks only
    first_seen = state.minor_first_seen or today.isoformat()
    state = replace(state, minor_first_seen=first_seen)
    reasons: list[str] = []
    ratio = diff.minor_changed / diff.page_lines
    if ratio > CUMULATIVE_MINOR_RATIO:
        reasons.append(f"minor changes add up to {ratio:.0%} of the page")
    age = _days_since(first_seen, today)
    if age > MINOR_MAX_AGE_DAYS:
        reasons.append(f"minor changes unreviewed for {age} days")
    if reasons:
        return PageResult(MAJOR, reasons, diff=diff, state=state)
    return PageResult(MINOR, [f"{len(diff.minor_hunks)} minor change(s)"], diff=diff, state=state)


def evaluate_page(snapshot_body: str | None, covers: tuple[str, ...] | list[str],
                  outcome: FetchOutcome, state: PageState, today: date) -> PageResult:
    """Classify one page. Order: transport failure, anomalies, no baseline, diff, minors.

    `snapshot_body` is the already-normalised snapshot body or None; `covers` is the
    sources.json list (empty for rubric pages); `today` is supplied by the caller. Any
    exception becomes a MAJOR "classifier error: <Type>" for this page only.
    """
    try:
        return _evaluate_page(snapshot_body, covers, outcome, state, today)
    except Exception as exc:  # noqa: BLE001 - fail safe: surface, never hide, a page
        return PageResult(MAJOR, [f"classifier error: {type(exc).__name__}"], state=state)


def _evaluate_changelog(outcome: FetchOutcome, last_reviewed: str | None,
                        state: ChangelogState, today: date) -> ChangelogResult:
    if outcome.unreachable:
        # The state shape (spec) has no unreachable timer for the changelog.
        return ChangelogResult(UNREACHABLE, ["unreachable"], state=state)
    if outcome.status in (404, 410):
        return ChangelogResult(MAJOR, [ANOM_GONE], state=state)
    text = outcome.text or ""
    anomalies: list[str] = []
    if length_truncated(outcome.declared_length, outcome.received, outcome.read_error):
        anomalies.append(ANOM_TRUNCATED)
    if looks_like_html(normalise(text)):
        anomalies.append(ANOM_HTML)
    versions = changelog_versions(text)
    if not versions and not anomalies:
        anomalies.append("fetch anomaly: no versions found in the changelog")
    if anomalies:
        return ChangelogResult(MAJOR, anomalies, state=state)
    if last_reviewed is None:
        return ChangelogResult(MAJOR, ["no baseline"], state=state)

    reasons: list[str] = []
    if last_reviewed in versions:
        new = versions[: versions.index(last_reviewed)]
    else:
        new = versions[:CHANGELOG_FALLBACK_VERSIONS]
        reasons.append(f"last reviewed version {last_reviewed} not found in the feed")
    if not new:
        return ChangelogResult(CLEAN, state=ChangelogState())
    first_seen = state.first_seen if (state.new_versions and state.first_seen) else today.isoformat()
    reasons.insert(0, f"new Claude Code versions: {', '.join(new)}")
    return ChangelogResult(MAJOR, reasons, new_versions=list(new),
                           state=ChangelogState(tuple(new), first_seen))


def evaluate_changelog(outcome: FetchOutcome, last_reviewed: str | None,
                       state: ChangelogState, today: date) -> ChangelogResult:
    """Evaluate the Claude Code changelog pseudo-page (newest version first in the feed)."""
    try:
        return _evaluate_changelog(outcome, last_reviewed, state, today)
    except Exception as exc:  # noqa: BLE001
        return ChangelogResult(MAJOR, [f"classifier error: {type(exc).__name__}"], state=state)
