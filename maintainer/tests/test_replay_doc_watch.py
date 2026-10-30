"""Tests for maintainer/replay_doc_watch.py. No network, no real credentials.

Git-backed tests build throwaway repositories in temporary directories (git is read through the
tool's own allow-listed wrapper; test setup uses git directly). The scratch directory is always a
sibling of the repo. Nothing here shells out to doc_watch.py. The one test against the real repo
is gated on CLAUDIT_REPLAY_INTEGRATION=1.
"""

import ast
import contextlib
import hashlib
import io
import json
import os
import shutil
import socket  # noqa: F401 - imported before it is patched (ssl subclasses socket.socket)
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from datetime import date
from fractions import Fraction
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import doc_classify as dc  # noqa: E402
import doc_watch as dw  # noqa: E402
import replay_doc_watch as rdw  # noqa: E402

TODAY = date(2026, 10, 30)
VERIFIED = "2026-10-15"
RUBRIC_URL = "https://platform.claude.com/docs/en/rubric.md"
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
}

ORDINALS = ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth",
            "ninth", "tenth", "eleventh", "twelfth"]
FILLER = "widget behaviour in plain words."
NAMES5 = ["alpha", "bravo", "charlie", "delta", "echo"]
FIVE = [(n, 9) for n in NAMES5]


def url_for(slug):
    return f"https://code.claude.com/docs/en/{slug}.md"


def line(name, k, verb="explains"):
    return f"{name.capitalize()} {ORDINALS[k - 1]} {verb} {FILLER}"


def build(spec, tweak=None):
    """A normalised page: per (name, count) a heading and `count` digit-free, modal-free lines.
    tweak: {(name, k): replacement line} (k from 1)."""
    tweak = tweak or {}
    out = []
    for name, count in spec:
        out.append(f"## Section {name}")
        for k in range(1, count + 1):
            out.append(tweak.get((name, k), line(name, k)))
    page = "\n".join(out) + "\n"
    assert dc.normalise(page) == page, "fixture must already be normalised"
    return page


def describes(name, ks):
    return {(name, k): line(name, k, "describes") for k in ks}


OLD5 = build(FIVE)
NUMBER_ONLY = build(FIVE, {("alpha", 1): "Alpha first explains widget behaviour in plain 5 words."})
NUMBER_MODAL = build(FIVE, {("alpha", 1): "Alpha first must explain 5 widget behaviour in plain words."})
VERSION_ONLY = build(FIVE, {("alpha", 1): "Alpha first explains widget behaviour in plain words v1.2.3."})
CUMULATIVE = build(FIVE, {**describes("alpha", (1, 3, 5, 7)), **describes("bravo", (1, 3, 5, 7))})
BOUNDARY = build(FIVE, describes("alpha", (1, 2, 3, 4, 5)))
MINOR_ONE = build(FIVE, describes("alpha", (1,)))
ANOMALY_NEW = build([("alpha", 9)])
HEAD_SPEC = [("alpha", 12), ("tiny", 4), ("bravo", 12), ("charlie", 12), ("delta", 12)]
HEAD_OLD = build(HEAD_SPEC)
HEAD_NEW = build([s for s in HEAD_SPEC if s[0] != "tiny"])


def analyse(old, new, slug="p"):
    return rdw.analyse_page(slug, url_for(slug), ["skills/x"], old, new, TODAY)


def make_report(analyses, **kw):
    args = dict(rev="r", sha="0" * 40, commit_date="2026-01-01", analyses=analyses,
                rubric_skipped=0, no_baseline=[], ablate=list(rdw.ABLATABLE),
                ratios=[0.5, 0.6, 0.75], target=Fraction(25), truth=None, expected=None)
    args.update(kw)
    return rdw.build_report(**args)


def manifest(root):
    """{relative path: sha256 or ''} of every file and directory under root, minus .git and
    the scratch directories."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git" and not d.startswith("scratch")]
        for d in dirnames:
            out[(Path(dirpath) / d).relative_to(root).as_posix() + "/"] = ""
        for name in filenames:
            p = Path(dirpath) / name
            out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def git(repo, *args, data=None):
    proc = subprocess.run(["git", "-C", str(repo), *args], input=data, capture_output=True,
                          check=True)
    return proc.stdout


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class RepoCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, GIT_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "core.autocrlf", "false")
        self.scratch = self.base / "scratch"
        self.counter = 0
        self.committed = {}
        self.committed_sources = b""

    def new_scratch(self):
        self.counter += 1
        return self.base / f"scratch{self.counter}"

    def snapshot_bytes(self, slug, body):
        return (dw.snapshot_header(url_for(slug), VERIFIED) + "\n" + body).encode("utf-8")

    def put(self, slug, raw):
        path = self.repo / "maintainer" / "snapshots" / f"{slug}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)

    def set_current(self, slug, body):
        self.put(slug, self.snapshot_bytes(slug, body))

    def make_baseline(self, pages, rubric=False, raw=None, schema=2):
        """Write snapshots + sources.json for {slug: body} and commit them."""
        raw = raw or {}
        entries = {url_for(s): {"sha256": dc.sha256(b), "verified": VERIFIED,
                                "covers": ["skills/x"]} for s, b in pages.items()}
        if rubric:
            entries[RUBRIC_URL] = {"sha256": None, "verified": None, "covers": []}
        data = {"schema": 2, "normaliser_version": 2, "pages": entries}
        if schema == 1:
            data = {"pages": entries}
        for slug, body in pages.items():
            self.committed[slug] = raw.get(slug) or self.snapshot_bytes(slug, body)
            self.put(slug, self.committed[slug])
        self.committed_sources = (json.dumps(data, indent=2) + "\n").encode("utf-8")
        (self.repo / "maintainer").mkdir(parents=True, exist_ok=True)
        (self.repo / "maintainer" / "sources.json").write_bytes(self.committed_sources)
        git(self.repo, "add", "-A")
        git(self.repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "base")

    def run_tool(self, *extra, scratch=None, repo=None, baseline="HEAD"):
        argv = ["--repo", str(repo or self.repo), "--scratch", str(scratch or self.new_scratch()),
                "--baseline", baseline, *extra]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = rdw.main(argv)
        return code, out.getvalue(), err.getvalue()

    def run_json(self, *extra, **kw):
        code, out, err = self.run_tool("--format", "json", *extra, **kw)
        return code, (json.loads(out) if out.strip() else None), err

    @staticmethod
    def statuses(report):
        return {p["slug"]: p["status"] for p in report["pages"]}


# ---------------------------------------------------------------- A. refusals and usage

class RefusalTests(RepoCase):
    def setUp(self):
        super().setUp()
        self.make_baseline({"pagea": OLD5})

    def main_quiet(self, argv):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return rdw.main(argv)

    def test_unknown_flags_rejected(self):
        for flag in ("--bogus", "--github", "--write", "--fetch", "--commit", "--rebaseline"):
            with self.subTest(flag):
                self.assertEqual(self.main_quiet(
                    ["--repo", str(self.repo), "--scratch", str(self.scratch), flag]), 2)
        self.assertFalse(self.scratch.exists())

    def test_abbreviated_flag_rejected(self):
        self.assertEqual(self.main_quiet(["--repo", str(self.repo), "--scr", str(self.scratch)]), 2)

    def test_scratch_is_required(self):
        self.assertEqual(self.main_quiet(["--repo", str(self.repo)]), 2)

    def test_scratch_inside_repo_refused(self):
        inside = self.repo / "out"
        code, _, err = self.run_tool(scratch=inside)
        self.assertEqual(code, 2)
        self.assertIn("outside the repo", err)
        self.assertFalse(inside.exists())

    def test_scratch_equal_to_repo_refused(self):
        code, _, _ = self.run_tool(scratch=self.repo)
        self.assertEqual(code, 2)

    def test_non_empty_scratch_refused_and_left_alone(self):
        self.scratch.mkdir()
        (self.scratch / "keep.txt").write_text("x", encoding="utf-8")
        code, _, err = self.run_tool(scratch=self.scratch)
        self.assertEqual(code, 2)
        self.assertIn("not empty", err)
        self.assertEqual([p.name for p in self.scratch.iterdir()], ["keep.txt"])

    def test_empty_existing_scratch_is_accepted(self):
        self.scratch.mkdir()
        code, _, _ = self.run_tool(scratch=self.scratch)
        self.assertEqual(code, 0)

    def test_bad_ref_exit_2_and_nothing_written(self):
        code, _, err = self.run_tool(scratch=self.scratch, baseline="no-such-ref")
        self.assertEqual(code, 2)
        self.assertIn("does not resolve", err)
        self.assertFalse(self.scratch.exists())

    def test_option_like_ref_refused(self):
        argv = ["--repo", str(self.repo), "--scratch", str(self.scratch), "--baseline=-x"]
        self.assertEqual(self.main_quiet(argv), 2)

    def test_no_git_dir_exit_2(self):
        plain = self.base / "plain"
        plain.mkdir()
        code, _, err = self.run_tool(repo=plain)
        self.assertEqual(code, 2)
        self.assertIn(".git", err)

    def test_git_missing_exit_2(self):
        with mock.patch.object(rdw.subprocess, "run", side_effect=FileNotFoundError("git")):
            code, _, err = self.run_tool()
        self.assertEqual(code, 2)
        self.assertIn("git", err)

    def test_bad_option_values_exit_2(self):
        for extra in (["--ablate", "no such reason"], ["--section-ratio", "-1"],
                      ["--target-pct", "101"], ["--target-pct", "abc"], ["--show-lines", "-1"],
                      ["--page", "nosuchpage"], ["--format", "xml"], ["--current", "head"]):
            with self.subTest(extra):
                code, _, _ = self.run_tool(*extra)
                self.assertEqual(code, 2)


# ---------------------------------------------------------------- B. recovery

class RecoveryTests(RepoCase):
    def test_recovered_files_byte_identical(self):
        self.make_baseline({"pagea": OLD5, "pageb": NUMBER_ONLY})
        scratch = self.new_scratch()
        code, _, _ = self.run_tool(scratch=scratch)
        self.assertEqual(code, 0)
        root = scratch / "baseline" / "maintainer"
        for slug, raw in self.committed.items():
            self.assertEqual((root / "snapshots" / f"{slug}.md").read_bytes(), raw)
        self.assertEqual((root / "sources.json").read_bytes(), self.committed_sources)

    def test_crlf_preserved(self):
        crlf = (dw.snapshot_header(url_for("pageb"), VERIFIED) + "\r\n"
                + OLD5.replace("\n", "\r\n")).encode("utf-8")
        self.make_baseline({"pagea": OLD5, "pageb": OLD5}, raw={"pageb": crlf})
        scratch = self.new_scratch()
        code, report, _ = self.run_json(scratch=scratch)
        got = (scratch / "baseline" / "maintainer" / "snapshots" / "pageb.md").read_bytes()
        self.assertEqual(got, crlf)
        self.assertIn(b"\r\n", got)
        self.assertEqual(code, 0)
        self.assertEqual(self.statuses(report), {"pagea": "CLEAN", "pageb": "ERROR"})

    def test_object_id_function(self):
        self.assertEqual(rdw.git_object_id(b""), "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391")
        self.assertEqual(len(rdw.git_object_id(b"x", 64)), 64)

    def test_object_id_matches_git_hash_object(self):
        self.make_baseline({"pagea": OLD5})
        data = b"hello\nworld\r\n"
        want = git(self.repo, "hash-object", "--no-filters", "--stdin", data=data).decode().strip()
        self.assertEqual(rdw.git_object_id(data), want)

    def test_tampered_blob_detected_exit_1(self):
        self.make_baseline({"pagea": OLD5})
        real = rdw._git

        def tamper(repo, *args):
            data = real(repo, *args)
            return data + b"x" if args[0] == "cat-file" else data

        with mock.patch.object(rdw, "_git", side_effect=tamper):
            code, out, err = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn("object id mismatch", err)
        self.assertEqual(out, "")

    def test_working_tree_unchanged(self):
        self.make_baseline({"pagea": OLD5, "pageb": OLD5})
        self.set_current("pagea", NUMBER_ONLY)
        before = manifest(self.base)
        status = git(self.repo, "status", "--porcelain")
        code, _, _ = self.run_tool()
        self.assertEqual(code, 0)
        self.assertEqual(manifest(self.base), before)
        self.assertEqual(git(self.repo, "status", "--porcelain"), status)

    def test_schema_1_sources_exit_2(self):
        self.make_baseline({"pagea": OLD5}, schema=1)
        code, _, err = self.run_tool()
        self.assertEqual(code, 2)
        self.assertIn("schema 2", err)

    def test_parse_sources_rejections(self):
        good = {"schema": 2, "normaliser_version": 2, "pages": {url_for("a"): {
            "sha256": None, "verified": None, "covers": []}}}
        self.assertEqual(list(rdw.parse_sources(json.dumps(good).encode())), [url_for("a")])
        bad = [b"not json", b"[]", json.dumps({**good, "normaliser_version": 1}).encode(),
               json.dumps({**good, "pages": []}).encode(),
               json.dumps({**good, "pages": {url_for("a"): {"sha256": "zz", "covers": []}}}).encode(),
               json.dumps({**good, "pages": {url_for("a"): {"sha256": None, "covers": "x"}}}).encode()]
        for raw in bad:
            with self.subTest(raw[:20]):
                with self.assertRaises(rdw.SchemaError):
                    rdw.parse_sources(raw)


# ---------------------------------------------------------------- C. parity and page errors

class ParityTests(RepoCase):
    def test_header_regex_matches_doc_watch(self):
        self.assertEqual(rdw.HEADER_RE.pattern, dw._HEADER_RE.pattern)
        self.assertEqual(rdw.HEADER_RE.flags, dw._HEADER_RE.flags)

    def test_parse_snapshot_rejections(self):
        url = url_for("p")
        hdr = dw.snapshot_header(url, VERIFIED)
        bad = {
            "bad header": b"nope\n" + OLD5.encode(),
            "no newline": hdr.encode(),
            "normaliser 1": (hdr.replace("normaliser: 2", "normaliser: 1") + "\n" + OLD5).encode(),
            "not normalised": (hdr + "\n# T\nwords   with  spaces\n").encode(),
            "not utf-8": hdr.encode() + b"\n\xff\xfe\n",
            "other source": (dw.snapshot_header(url_for("q"), VERIFIED) + "\n" + OLD5).encode(),
            "bad date": (hdr.replace(VERIFIED, "2026-13-45") + "\n" + OLD5).encode(),
        }
        for name, raw in bad.items():
            with self.subTest(name):
                with self.assertRaises(rdw.PageError):
                    rdw.parse_snapshot(raw, url, "current")
        self.assertEqual(rdw.parse_snapshot((hdr + "\n" + OLD5).encode(), url, "current"), OLD5)

    def test_each_defect_gives_one_error_page(self):
        self.make_baseline({"pagea": OLD5, "pageb": OLD5})
        hdr = dw.snapshot_header(url_for("pagea"), VERIFIED)
        cases = {
            "bad header": b"not a header\n" + OLD5.encode(),
            "normaliser 1": (hdr.replace("normaliser: 2", "normaliser: 1") + "\n" + OLD5).encode(),
            "non-normalised body": (hdr + "\n# T\nwords   with  spaces\n").encode(),
            "non-utf-8": hdr.encode() + b"\n\xff\xfe\n",
            "other source": (dw.snapshot_header(url_for("zzz"), VERIFIED) + "\n" + OLD5).encode(),
            "missing file": None,
        }
        for name, raw in cases.items():
            with self.subTest(name):
                if raw is None:
                    (self.repo / "maintainer" / "snapshots" / "pagea.md").unlink()
                else:
                    self.put("pagea", raw)
                code, report, _ = self.run_json()
                self.assertEqual(code, 0)
                self.assertEqual(self.statuses(report), {"pagea": "ERROR", "pageb": "CLEAN"})
                self.assertEqual(report["counts"]["errors"], 1)
                self.put("pagea", self.committed["pagea"])

    def test_rubric_page_skipped_and_counted(self):
        self.make_baseline({"pagea": OLD5}, rubric=True)
        code, report, _ = self.run_json()
        self.assertEqual(code, 0)
        self.assertEqual(report["counts"]["rubric_skipped"], 1)
        self.assertEqual(report["counts"]["baselined"], 1)
        self.assertEqual(list(self.statuses(report)), ["pagea"])

    def test_page_without_baseline_is_reported_not_an_error(self):
        self.make_baseline({"pagea": OLD5})
        self.set_current("extra", OLD5)
        code, report, _ = self.run_json()
        self.assertEqual(code, 0)
        self.assertEqual(report["no_baseline"], ["extra"])
        self.assertEqual(report["counts"]["errors"], 0)

    def test_tool_status_equals_evaluate_page(self):
        pairs = [(OLD5, OLD5), (OLD5, NUMBER_ONLY), (OLD5, NUMBER_MODAL), (OLD5, VERSION_ONLY),
                 (OLD5, CUMULATIVE), (OLD5, BOUNDARY), (OLD5, MINOR_ONE), (OLD5, ANOMALY_NEW),
                 (HEAD_OLD, HEAD_NEW)]
        for old, new in pairs:
            with self.subTest(new[:40]):
                pa = analyse(old, new)
                want = dc.evaluate_page(old, ["skills/x"], dc.FetchOutcome(text=new, status=200),
                                        dc.PageState(), TODAY)
                self.assertEqual(pa.status, want.status)

    def test_hunk_reasons_equal_classify_hunk(self):
        for old, new in [(OLD5, NUMBER_MODAL), (OLD5, VERSION_ONLY), (OLD5, BOUNDARY),
                         (OLD5, CUMULATIVE), (HEAD_OLD, HEAD_NEW)]:
            pa = analyse(old, new)
            self.assertTrue(pa.hunks)
            for view in pa.hunks:
                h = view.hunk
                want = [h.tag] if h.tag in rdw.HEADING_TAGS else dc.classify_hunk(h)
                self.assertEqual(h.reasons, want)


# ---------------------------------------------------------------- D. what-if semantics

class WhatIfTests(unittest.TestCase):
    NUMBER = frozenset({dc.REASON_NUMBER})

    def test_number_only_hunk_flips(self):
        pa = analyse(OLD5, NUMBER_ONLY)
        self.assertEqual([v.hunk.reasons for v in pa.hunks], [[dc.REASON_NUMBER]])
        self.assertEqual(pa.status, dc.MAJOR)
        self.assertEqual(rdw.scenario([pa], self.NUMBER), {"major_after": 0, "flips": ["p"]})

    def test_number_plus_modal_does_not_flip(self):
        pa = analyse(OLD5, NUMBER_MODAL)
        self.assertEqual(set(pa.hunks[0].hunk.reasons), {dc.REASON_NUMBER, dc.REASON_MODAL})
        self.assertEqual(rdw.scenario([pa], self.NUMBER), {"major_after": 1, "flips": []})
        self.assertEqual(rdw.scenario([pa], frozenset({dc.REASON_MODAL}))["flips"], [])

    def test_version_only_hunk_stays_major_after_removing_number(self):
        pa = analyse(OLD5, VERSION_ONLY)
        self.assertEqual(set(pa.hunks[0].hunk.reasons), {dc.REASON_VERSION, dc.REASON_NUMBER})
        self.assertEqual(rdw.scenario([pa], self.NUMBER)["flips"], [])
        both = frozenset({dc.REASON_NUMBER, dc.REASON_VERSION})
        self.assertEqual(rdw.scenario([pa], both)["flips"], ["p"])

    def test_cumulative_minor_rule_keeps_page_major(self):
        pa = analyse(OLD5, CUMULATIVE)
        self.assertEqual(pa.status, dc.MAJOR)
        self.assertTrue(all(not v.hunk.reasons for v in pa.hunks))
        self.assertEqual(sum(v.hunk.changed for v in pa.hunks), 8)
        every = frozenset(rdw.ABLATABLE)
        self.assertEqual(rdw.scenario([pa], every), {"major_after": 1, "flips": []})
        self.assertEqual(rdw.scenario([pa], section_ratio=0.99)["flips"], [])

    def test_anomaly_page_never_flips(self):
        pa = analyse(OLD5, ANOMALY_NEW)
        self.assertEqual(pa.status, dc.MAJOR)
        self.assertTrue(pa.anomalies)
        self.assertTrue(all(v.hunk.tag == dc.REASON_HEADING_REMOVED for v in pa.hunks))
        for removed in (frozenset({dc.REASON_HEADING_REMOVED}), frozenset(rdw.ABLATABLE)):
            self.assertEqual(rdw.scenario([pa], removed), {"major_after": 1, "flips": []})
        report = make_report([pa])
        self.assertEqual(report["counts"]["anomaly_driven_major"], 1)

    def test_section_boundary_is_strict(self):
        pa = analyse(OLD5, BOUNDARY)
        self.assertEqual([v.hunk.reasons for v in pa.hunks], [[dc.REASON_SECTION]])
        h = pa.hunks[0].hunk
        self.assertEqual((h.changed, h.section_old_len, h.section_new_len), (5, 10, 10))
        self.assertEqual(rdw.scenario([pa], section_ratio=0.5)["flips"], ["p"])    # 0.5 > 0.5 is false
        self.assertEqual(rdw.scenario([pa], section_ratio=0.49)["flips"], [])      # 0.5 > 0.49
        self.assertEqual(rdw.scenario([pa], section_ratio=0.6)["flips"], ["p"])

    def test_classifier_threshold_reproduces_baseline(self):
        pas = [analyse(OLD5, new, slug=f"p{i}") for i, new in enumerate(
            [NUMBER_ONLY, NUMBER_MODAL, BOUNDARY, CUMULATIVE, MINOR_ONE, OLD5])]
        base = rdw.scenario(pas)
        again = rdw.scenario(pas, section_ratio=dc.SECTION_MAJOR_RATIO)
        self.assertEqual(again, base)
        self.assertEqual(base["flips"], [])

    def test_heading_only_page_flips_on_heading_removal(self):
        pa = analyse(HEAD_OLD, HEAD_NEW)
        self.assertEqual([v.hunk.reasons for v in pa.hunks], [[dc.REASON_HEADING_REMOVED]])
        self.assertFalse(pa.anomalies)
        self.assertEqual(rdw.scenario([pa], frozenset({dc.REASON_HEADING_REMOVED}))["flips"], ["p"])
        self.assertEqual(rdw.scenario([pa], frozenset({dc.REASON_HEADING_ADDED}))["flips"], [])
        self.assertEqual(rdw.scenario([pa], section_ratio=0.99)["flips"], [])   # unpaired hunk

    def test_empty_diff_is_clean(self):
        pa = analyse(OLD5, OLD5)
        self.assertEqual((pa.status, pa.hunks, pa.anomalies), (dc.CLEAN, [], []))
        report = make_report([pa])
        self.assertEqual(report["counts"]["changed_pages"], 0)
        self.assertEqual(report["summary"]["major"], 0)

    def test_flips_only_come_from_baseline_major(self):
        pas = [analyse(OLD5, NUMBER_ONLY, "a"), analyse(OLD5, MINOR_ONE, "b"),
               analyse(OLD5, OLD5, "c"), analyse(OLD5, NUMBER_MODAL, "d")]
        self.assertEqual([p.status for p in pas], [dc.MAJOR, dc.MINOR, dc.CLEAN, dc.MAJOR])
        for reason in rdw.ABLATABLE:
            flips = rdw.scenario(pas, frozenset({reason}))["flips"]
            self.assertLessEqual(set(flips), {"a", "d"}, reason)
        report = make_report(pas)
        for row in report["removal"]:
            self.assertLessEqual(row["major_after"], report["summary"]["major"])

    def test_no_ablation_row_equals_baseline(self):
        pas = [analyse(OLD5, NUMBER_ONLY, "a"), analyse(OLD5, CUMULATIVE, "b"),
               analyse(OLD5, OLD5, "c")]
        report = make_report(pas)
        first = report["removal"][0]
        self.assertEqual(first["label"], "(none)")
        self.assertEqual(first["major_after"], report["summary"]["major"])
        self.assertEqual(first["flips"], [])
        self.assertEqual(first["pct"], report["summary"]["pct"])

    def test_error_pages_are_ignored_by_what_ifs(self):
        err = rdw.PageAnalysis("e", url_for("e"), rdw.STATUS_ERROR, error="x")
        ok = analyse(OLD5, NUMBER_ONLY, "a")
        report = make_report([err, ok])
        self.assertEqual(report["summary"]["analysed"], 1)
        self.assertEqual(report["counts"]["errors"], 1)
        self.assertEqual(rdw.scenario([err, ok], self.NUMBER)["flips"], ["a"])


# ---------------------------------------------------------------- E. reporting

class ReportingTests(RepoCase):
    def test_reason_counts_are_page_level_and_sorted(self):
        pas = [analyse(OLD5, NUMBER_ONLY, "p1"), analyse(OLD5, NUMBER_MODAL, "p2"),
               analyse(OLD5, OLD5, "p3"), analyse(OLD5, VERSION_ONLY, "p4")]
        self.assertEqual(rdw.reason_table(pas), [
            (dc.REASON_NUMBER, 3, 3), (dc.REASON_MODAL, 1, 1), (dc.REASON_VERSION, 1, 1)])

    def test_reason_counts_count_a_page_once(self):
        two = build(FIVE, {("alpha", 1): "Alpha first explains widget behaviour in plain 5 words.",
                           ("bravo", 1): "Bravo first explains widget behaviour in plain 7 words."})
        pa = analyse(OLD5, two)
        self.assertEqual(len(pa.hunks), 2)
        self.assertEqual(rdw.reason_table([pa]), [(dc.REASON_NUMBER, 1, 2)])

    def test_default_sweep_and_target(self):
        self.assertEqual(rdw.DEFAULT_SECTION_RATIOS, (0.5, 0.6, 0.75))
        self.assertEqual(rdw.DEFAULT_BASELINE, "33fd75b")
        self.make_baseline({"pagea": OLD5})
        _, report, _ = self.run_json()
        self.assertEqual([r["threshold"] for r in report["section_sweep"]], [0.5, 0.6, 0.75])
        self.assertEqual(report["summary"]["target_pct"], 25.0)
        self.assertEqual([r["label"] for r in report["removal"]], ["(none)", *rdw.ABLATABLE])

    def test_ablate_restricts_rows(self):
        self.make_baseline({"pagea": OLD5})
        _, report, _ = self.run_json("--ablate", "NUMBER changed", "modal changed",
                                     "--section-ratio", "0.9")
        self.assertEqual([r["label"] for r in report["removal"]],
                         ["(none)", dc.REASON_NUMBER, dc.REASON_MODAL])
        self.assertEqual([r["threshold"] for r in report["section_sweep"]], [0.9])

    def test_deterministic_ordering_and_identical_reruns(self):
        self.make_baseline({"pagez": OLD5, "pagea": OLD5, "pagem": OLD5})
        self.set_current("pagez", NUMBER_ONLY)
        self.set_current("pagea", NUMBER_MODAL)
        self.set_current("pagem", CUMULATIVE)
        first = self.run_tool()
        second = self.run_tool()
        self.assertEqual(first[0], 0)
        self.assertEqual(first, second)
        self.assertLess(first[1].index("pagea"), first[1].index("pagem"))
        self.assertLess(first[1].index("pagem"), first[1].index("pagez"))
        j1, j2 = self.run_tool("--format", "json"), self.run_tool("--format", "json")
        self.assertEqual(j1, j2)

    def test_json_parses_with_required_keys(self):
        self.make_baseline({"pagea": OLD5, "pageb": OLD5})
        self.set_current("pageb", NUMBER_ONLY)
        code, report, _ = self.run_json()
        self.assertEqual(code, 0)
        self.assertLessEqual({"baseline", "classifier", "current", "counts", "no_baseline", "pages",
                              "summary", "reasons", "removal", "section_sweep", "expected_major"},
                             set(report))
        self.assertEqual(len(report["baseline"]["sha"]), 40)
        self.assertEqual(set(report["counts"]), {
            "baselined", "analysed", "errors", "rubric_skipped", "no_baseline",
            "anomaly_driven_major", "changed_pages"})
        self.assertEqual(report["counts"]["changed_pages"], 1)
        self.assertEqual(report["summary"]["major_slugs"], ["pageb"])
        self.assertEqual(report["current"], "working-tree")

    def test_text_output_shape(self):
        self.make_baseline({"pagea": OLD5, "pageb": OLD5, "pagec": OLD5}, rubric=True)
        self.set_current("pageb", NUMBER_ONLY)
        self.set_current("pagec", MINOR_ONE)
        code, out, _ = self.run_tool()
        self.assertEqual(code, 0)
        for text in ("[CLEAN] pagea", "[MAJOR] pageb", "[MINOR] pagec", "number changed",
                     "1 rubric skipped", "anomaly-driven MAJOR: 0", "changed pages (any hunk): 2",
                     "target <= 25%: NOT met", "Removal ablation", "Section-rewrite sweep"):
            self.assertIn(text, out)
        self.assertIn("baseline: ", out)

    def test_no_snapshot_text_in_default_output(self):
        secret = "zephyrquartz"
        spec = [("quillfeather", 9), ("bravo", 9)]
        old = build(spec)
        new = build(spec, {("quillfeather", 1): f"Quillfeather first {secret} 5 widget words."})
        self.make_baseline({"pagea": old})
        self.set_current("pagea", new)
        for extra in ((), ("--format", "json")):
            code, out, err = self.run_tool(*extra)
            self.assertEqual(code, 0)
            for blob in (out, err):
                self.assertNotIn(secret, blob)
                self.assertNotIn("quillfeather", blob.lower())
        code, out, _ = self.run_tool("--show-lines", "2")
        self.assertIn(secret, out)
        self.assertIn("quillfeather", out)

    def test_pct_equal_to_target_counts_as_met(self):
        self.assertTrue(rdw.target_met(1, 4, Fraction(25)))
        self.assertFalse(rdw.target_met(1, 4, Fraction("24.9")))
        self.assertFalse(rdw.target_met(2, 7, Fraction(25)))
        self.assertFalse(rdw.target_met(0, 0, Fraction(25)))
        self.make_baseline({f"page{c}": OLD5 for c in "abcd"})
        self.set_current("pagea", NUMBER_ONLY)
        _, report, _ = self.run_json("--target-pct", "25")
        self.assertEqual((report["summary"]["pct"], report["summary"]["target_met"]), (25.0, True))
        _, report, _ = self.run_json("--target-pct", "24.9")
        self.assertFalse(report["summary"]["target_met"])
        flipped = next(r for r in report["removal"] if r["label"] == dc.REASON_NUMBER)
        self.assertEqual((flipped["major_after"], flipped["pct"], flipped["target_met"]),
                         (0, 0.0, True))

    def test_ground_truth_and_expected_major_arithmetic(self):
        pas = [analyse(OLD5, NUMBER_ONLY, "p1"), analyse(OLD5, NUMBER_MODAL, "p2"),
               analyse(OLD5, OLD5, "p3")]
        report = make_report(pas, truth={"p1", "p3", "unknown"}, expected={"p1", "p3"})
        row = {r["label"]: r for r in report["removal"]}
        self.assertEqual(row[dc.REASON_NUMBER]["flips"], ["p1"])
        self.assertEqual(row[dc.REASON_NUMBER]["flipped_ground_truth"], ["p1"])
        self.assertEqual(row[dc.REASON_MODAL]["flipped_ground_truth"], [])
        self.assertEqual(report["expected_major"], {"missing": ["p3"], "unexpected": ["p2"]})
        plain = make_report(pas)
        self.assertIsNone(plain["removal"][0]["flipped_ground_truth"])
        self.assertIsNone(plain["expected_major"])
        json.dumps(report)

    def test_slug_files_are_read_with_comments(self):
        path = self.base / "gt.txt"
        path.write_text("# comment\n\npagea\n  pageb  \n", encoding="utf-8")
        self.assertEqual(rdw.read_slug_set(path, "ground-truth"), {"pagea", "pageb"})
        path.write_text("not a slug!\n", encoding="utf-8")
        with self.assertRaises(rdw.UsageError):
            rdw.read_slug_set(path, "ground-truth")
        with self.assertRaises(rdw.UsageError):
            rdw.read_slug_set(self.base / "missing.txt", "ground-truth")

    def test_ground_truth_files_through_main(self):
        self.make_baseline({"pagea": OLD5, "pageb": OLD5})
        self.set_current("pagea", NUMBER_ONLY)
        gt, em = self.base / "gt.txt", self.base / "em.txt"
        gt.write_text("pagea\n", encoding="utf-8")
        em.write_text("pageb\n", encoding="utf-8")
        code, report, _ = self.run_json("--ground-truth", str(gt), "--expected-major", str(em))
        self.assertEqual(code, 0)
        flipped = next(r for r in report["removal"] if r["label"] == dc.REASON_NUMBER)
        self.assertEqual(flipped["flipped_ground_truth"], ["pagea"])
        self.assertEqual(report["expected_major"], {"missing": ["pageb"], "unexpected": ["pagea"]})

    def test_page_filter(self):
        self.make_baseline({"pagea": OLD5, "pageb": OLD5})
        _, report, _ = self.run_json("--page", "pageb")
        self.assertEqual(list(self.statuses(report)), ["pageb"])
        self.assertEqual(report["summary"]["analysed"], 1)


# ---------------------------------------------------------------- E2. held-MAJOR report

def fake_page(slug, hunk_reasons, page_lines=100):
    """A PageAnalysis whose hunks carry the given reason lists (one changed line each)."""
    views = []
    for reasons in hunk_reasons:
        h = dc.Hunk(section=("", 1), tag="replace", old_lines=("a",), new_lines=("b",),
                    old_flags=(False,), new_flags=(False,), old_start=0, new_start=0,
                    section_old_len=1, section_new_len=1, reasons=list(reasons),
                    major=bool(reasons))
        views.append(rdw.HunkView(h, "section 0"))
    pa = rdw.PageAnalysis(slug, url_for(slug), dc.CLEAN, hunks=views, page_lines=page_lines)
    pa.status = rdw.page_status(pa)
    return pa


class HeldMajorTests(RepoCase):
    def test_mask_is_sorted_union_of_hunk_reasons(self):
        two = build(FIVE, {("alpha", 1): "Alpha first must explain widget behaviour in plain words.",
                           ("bravo", 1): "Bravo first explains widget behaviour in plain 7 words."})
        pa = analyse(OLD5, two)
        want = sorted({r for v in pa.hunks for r in v.hunk.reasons})
        self.assertEqual(want, sorted([dc.REASON_MODAL, dc.REASON_NUMBER]))
        self.assertEqual(rdw.page_hold(pa)["mask"], want)
        self.assertEqual(rdw.page_hold(analyse(OLD5, OLD5))["mask"], [])

    def test_blocked_labels(self):
        anomaly = rdw.page_hold(analyse(OLD5, ANOMALY_NEW))
        self.assertTrue(anomaly["blocked"].startswith("anomaly: "))
        self.assertIsNone(anomaly["minimal_keep"])
        cum = rdw.page_hold(analyse(OLD5, CUMULATIVE))
        self.assertEqual(cum["blocked"], "cumulative minor 16.0% > 15%")
        self.assertEqual((cum["mask"], cum["minimal_keep"]), ([], None))
        free = rdw.page_hold(analyse(OLD5, NUMBER_ONLY))
        self.assertIsNone(free["blocked"])

    def test_cum_pct_arithmetic(self):
        self.assertEqual(rdw.page_hold(analyse(OLD5, CUMULATIVE))["cum_pct"], 16.0)     # 8 / 50
        self.assertEqual(rdw.page_hold(analyse(OLD5, MINOR_ONE))["cum_pct"], 2.0)       # 1 / 50
        self.assertEqual(rdw.page_hold(analyse(OLD5, OLD5))["cum_pct"], 0.0)
        pa = analyse(HEAD_OLD, HEAD_NEW)
        self.assertEqual(rdw.page_hold(pa)["cum_pct"],
                         round(100 * sum(v.hunk.changed for v in pa.hunks) / pa.page_lines, 1))

    def test_heading_exclusion_variant_differs(self):
        hold = rdw.page_hold(analyse(HEAD_OLD, HEAD_NEW))
        self.assertGreater(hold["cum_pct"], 0.0)
        self.assertEqual(hold["cum_pct_excluding_heading_hunks"], 0.0)
        self.assertNotEqual(hold["cum_pct"], hold["cum_pct_excluding_heading_hunks"])
        plain = rdw.page_hold(analyse(OLD5, CUMULATIVE))
        self.assertEqual(plain["cum_pct"], plain["cum_pct_excluding_heading_hunks"])

    def test_minimal_keep_fewest_hunks_and_tie_break(self):
        tie = rdw.page_hold(analyse(OLD5, NUMBER_MODAL))     # one hunk with both reasons
        self.assertEqual(tie["minimal_keep"], {"reason": dc.REASON_NUMBER, "hunks": 1})
        self.assertLess(rdw.ABLATABLE.index(dc.REASON_NUMBER), rdw.ABLATABLE.index(dc.REASON_MODAL))
        pa = fake_page("f", [[dc.REASON_MODAL], [dc.REASON_MODAL], [dc.REASON_LINK],
                             [dc.REASON_TABLE], [dc.REASON_TABLE]])
        self.assertEqual(rdw.page_hold(pa)["minimal_keep"], {"reason": dc.REASON_LINK, "hunks": 1})
        pa = fake_page("g", [[dc.REASON_MODAL], [dc.REASON_MODAL], [dc.REASON_TABLE],
                             [dc.REASON_TABLE], [dc.REASON_LINK], [dc.REASON_LINK]])
        self.assertEqual(rdw.page_hold(pa)["minimal_keep"], {"reason": dc.REASON_TABLE, "hunks": 2})

    def test_all_reasons_removed_equals_blocked_count(self):
        pas = [analyse(OLD5, new, slug=f"p{i}") for i, new in enumerate(
            [NUMBER_ONLY, NUMBER_MODAL, VERSION_ONLY, CUMULATIVE, BOUNDARY, MINOR_ONE, OLD5,
             ANOMALY_NEW])]
        everything = frozenset(rdw.ABLATABLE)
        blocked = sum(1 for p in pas if rdw.page_hold(p)["blocked"])
        self.assertEqual(blocked, 2)
        self.assertEqual(rdw.scenario(pas, everything)["major_after"], blocked)
        self.assertEqual(make_report(pas)["feasibility"]["blocked_count"], blocked)

    def test_identity_mismatch_is_a_parity_error(self):
        pas = [analyse(OLD5, NUMBER_ONLY, "a")]
        with mock.patch.object(rdw, "scenario", return_value={"major_after": 99, "flips": []}):
            with self.assertRaises(rdw.ParityError):
                make_report(pas)
        self.make_baseline({"pagea": OLD5})
        with mock.patch.object(rdw, "scenario", return_value={"major_after": 99, "flips": []}):
            code, out, err = self.run_tool()
        self.assertEqual((code, out), (1, ""))

    def test_feasibility_on_sixteen_pages(self):
        four = [analyse(OLD5, CUMULATIVE, f"b{i:02d}") for i in range(4)]
        rest = [analyse(OLD5, OLD5, f"c{i:02d}") for i in range(12)]
        fz = make_report(four + rest)["feasibility"]
        self.assertEqual(fz, {"blocked_count": 4, "target_count": 4,
                              "reachable_without_recall": True, "degenerate_floor_flag": False})
        five = [analyse(OLD5, CUMULATIVE, f"b{i:02d}") for i in range(5)]
        fz = make_report(five + rest[:11])["feasibility"]
        self.assertEqual((fz["blocked_count"], fz["target_count"], fz["reachable_without_recall"],
                          fz["degenerate_floor_flag"]), (5, 4, False, False))

    def test_degenerate_floor_flag(self):
        full = [[r] for r in rdw.ABLATABLE]
        pas = [fake_page("a", full), fake_page("b", full),
               fake_page("c", []), fake_page("d", [])]
        fz = rdw.feasibility(pas, Fraction(25))
        self.assertEqual((fz["blocked_count"], fz["target_count"]), (0, 1))
        self.assertTrue(fz["degenerate_floor_flag"])
        easy = [fake_page("a", [[dc.REASON_NUMBER]]), fake_page("b", []), fake_page("c", []),
                fake_page("d", [])]
        self.assertFalse(rdw.feasibility(easy, Fraction(25))["degenerate_floor_flag"])

    def test_json_has_new_fields_and_reruns_identical(self):
        self.make_baseline({"pagez": OLD5, "pagea": OLD5, "pagem": OLD5})
        self.set_current("pagez", NUMBER_ONLY)
        self.set_current("pagea", CUMULATIVE)
        self.set_current("pagem", NUMBER_MODAL)
        first = self.run_tool("--format", "json")
        self.assertEqual(first, self.run_tool("--format", "json"))
        self.assertEqual(self.run_tool(), self.run_tool())
        report = json.loads(first[1])
        page = next(p for p in report["pages"] if p["slug"] == "pagea")
        self.assertLessEqual({"blocked", "cum_pct", "cum_pct_excluding_heading_hunks", "mask",
                              "minimal_keep"}, set(page))
        self.assertEqual(report["feasibility"]["blocked_count"], 1)
        self.assertEqual(report["feasibility"]["target_count"], 0)

    def test_text_section_lists_names_only(self):
        self.make_baseline({"pagea": OLD5})
        self.set_current("pagea", NUMBER_MODAL)
        _, out, _ = self.run_tool()
        for text in ("Held MAJOR", "Feasibility", "blocked: no", "minimal keep: number changed (1 hunk(s))"):
            self.assertIn(text, out)
        self.assertNotIn("widget", out)


# ---------------------------------------------------------------- F. read-only guarantees

class ReadOnlyTests(RepoCase):
    def setUp(self):
        super().setUp()
        self.make_baseline({"pagea": OLD5, "pageb": OLD5})
        self.set_current("pagea", NUMBER_ONLY)

    def test_repo_manifest_unchanged_and_no_bytecode(self):
        before = manifest(self.base)
        status = git(self.repo, "status", "--porcelain")
        code, _, _ = self.run_tool()
        self.assertEqual(code, 0)
        after = manifest(self.base)
        self.assertEqual(after, before)
        self.assertEqual(git(self.repo, "status", "--porcelain"), status)
        self.assertFalse([p for p in after if p.endswith(".pyc") or "__pycache__" in p])

    def test_git_argv_is_allow_listed(self):
        real = subprocess.run
        calls = []

        def record(argv, *args, **kw):
            calls.append((list(argv), kw))
            return real(argv, *args, **kw)

        with mock.patch.object(rdw.subprocess, "run", side_effect=record):
            code, _, _ = self.run_tool()
        self.assertEqual(code, 0)
        self.assertTrue(calls)
        for argv, kw in calls:
            self.assertEqual(argv[:2], ["git", "-C"])
            self.assertIn(argv[3], rdw.GIT_ALLOWED)
            self.assertNotIn("-w", argv)
            self.assertIs(kw.get("shell"), False)
            self.assertEqual(kw.get("timeout"), 30)
            self.assertEqual(kw["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertLessEqual({"rev-parse", "ls-tree", "cat-file"}, {argv[3] for argv, _ in calls})

    def test_disallowed_git_commands_refused_without_running(self):
        with mock.patch.object(rdw.subprocess, "run") as run:
            for args in (("commit", "-m", "x"), ("checkout", "HEAD"), ("hash-object", "-w", "f"),
                         ("push",), ("config", "x", "y")):
                with self.subTest(args):
                    with self.assertRaises(rdw.UsageError):
                        rdw._git(self.repo, *args)
        run.assert_not_called()

    def test_network_is_never_touched(self):
        boom = OSError("network blocked")
        with mock.patch("socket.socket", side_effect=boom), \
                mock.patch("socket.create_connection", side_effect=boom), \
                mock.patch("urllib.request.urlopen", side_effect=boom):
            code, out, _ = self.run_tool()
        self.assertEqual(code, 0)
        self.assertIn("[MAJOR] pagea", out)


class SourceStructureTests(unittest.TestCase):
    ALLOWED_IMPORTS = {"__future__", "sys", "argparse", "hashlib", "json", "math", "os", "re",
                       "subprocess", "collections", "dataclasses", "datetime", "fractions",
                       "pathlib", "doc_classify"}

    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(Path(rdw.__file__).read_text(encoding="utf-8"))

    def test_imports_are_stdlib_only_and_offline(self):
        names = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").split(".")[0])
        self.assertLessEqual(names, self.ALLOWED_IMPORTS)
        self.assertTrue(names.isdisjoint({"urllib", "http", "socket", "ssl", "doc_watch"}))

    def test_dont_write_bytecode_set_before_doc_classify_import(self):
        flag = imp = None
        for index, node in enumerate(self.tree.body):
            if (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Attribute)
                    and node.targets[0].attr == "dont_write_bytecode"
                    and isinstance(node.value, ast.Constant) and node.value.value is True):
                flag = index if flag is None else flag
            if isinstance(node, ast.Import) and any(a.name == "doc_classify" for a in node.names):
                imp = index
        self.assertIsNotNone(flag)
        self.assertIsNotNone(imp)
        self.assertLess(flag, imp)
        self.assertTrue(sys.dont_write_bytecode)


# ---------------------------------------------------------------- G. failure isolation

class FailureTests(RepoCase):
    def test_one_error_page_does_not_stop_the_others(self):
        self.make_baseline({"pagea": OLD5, "pageb": OLD5, "pagec": OLD5})
        self.set_current("pagea", NUMBER_ONLY)
        (self.repo / "maintainer" / "snapshots" / "pagec.md").unlink()
        code, report, _ = self.run_json()
        self.assertEqual(code, 0)
        self.assertEqual(self.statuses(report),
                         {"pagea": "MAJOR", "pageb": "CLEAN", "pagec": "ERROR"})

    def test_all_pages_error_exit_1(self):
        self.make_baseline({"pagea": OLD5, "pageb": OLD5})
        for slug in ("pagea", "pageb"):
            (self.repo / "maintainer" / "snapshots" / f"{slug}.md").unlink()
        code, out, _ = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn("[ERROR] pagea", out)

    def test_git_timeout_exit_1(self):
        self.make_baseline({"pagea": OLD5})
        timeout = subprocess.TimeoutExpired(cmd="git", timeout=30)
        with mock.patch.object(rdw.subprocess, "run", side_effect=timeout):
            code, _, err = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn("timed out", err)

    def test_injected_status_mismatch_exit_1(self):
        self.make_baseline({"pagea": OLD5})
        self.set_current("pagea", NUMBER_ONLY)
        wrong = dc.PageResult(dc.CLEAN)
        with mock.patch.object(dc, "evaluate_page", return_value=wrong):
            code, out, err = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn("differs from", err)
        self.assertEqual(out, "")

    def test_injected_hunk_reason_mismatch_exit_1(self):
        self.make_baseline({"pagea": OLD5})
        self.set_current("pagea", NUMBER_ONLY)
        real = dc.diff_page

        def tampered(old, new):
            diff = real(old, new)
            for hunk in diff.hunks:
                hunk.reasons = ["bogus"]
            return diff

        with mock.patch.object(dc, "diff_page", side_effect=tampered):
            with self.assertRaises(rdw.ParityError):
                analyse(OLD5, NUMBER_ONLY)
            code, _, err = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn("classify_hunk", err)

    def test_broken_stdout_exit_1(self):
        self.make_baseline({"pagea": OLD5})

        class Broken:
            def write(self, text):
                raise BrokenPipeError()

            def flush(self):
                pass

        argv = ["--repo", str(self.repo), "--scratch", str(self.new_scratch()),
                "--baseline", "HEAD"]
        self.assertEqual(rdw.main(argv, out=Broken()), 1)


# ---------------------------------------------------------------- H. integration (env-gated)

@unittest.skipUnless(os.environ.get("CLAUDIT_REPLAY_INTEGRATION") == "1",
                     "set CLAUDIT_REPLAY_INTEGRATION=1 to replay against the real repo")
class IntegrationTests(unittest.TestCase):
    def test_real_repo_replay(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            scratch = Path(tmp).resolve() / "scratch"
            self.assertFalse(rdw._same_or_inside(scratch, rdw.ROOT))
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = rdw.main(["--scratch", str(scratch), "--format", "json"])
        self.assertEqual(code, 0, err.getvalue())
        report = json.loads(out.getvalue())
        self.assertEqual(report["counts"]["baselined"], 16)
        self.assertTrue(report["baseline"]["sha"].startswith("33fd75b"))


if __name__ == "__main__":
    unittest.main()
