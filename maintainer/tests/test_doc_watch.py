"""Tests for maintainer/doc_watch.py (local half). No network, no real credentials.

urllib and the fetch layer are mocked; files live in temporary directories; sleeping is
injected. main() is called in-process; nothing here shells out to doc_watch.py.
"""

import contextlib
import copy
import http.client
import io
import json
import os
import re
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import doc_classify as dc  # noqa: E402
import doc_watch as dw  # noqa: E402

TODAY = date(2026, 10, 16)
URL_A = "https://code.claude.com/docs/en/alpha.md"
URL_B = "https://code.claude.com/docs/en/beta.md"
URL_RUBRIC = "https://platform.claude.com/docs/en/rubric.md"
URL_CL = "https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md"
FEED = "# Changelog\n" + "".join(
    f"## 2.1.{n}\n- some change description text here\n" for n in range(300, 270, -1))


def make_body(n=30):
    lines = ["# Sample page"] + [
        f"Paragraph {i} describes the widget behaviour in plain words." for i in range(n)]
    return dc.normalise("\n".join(lines) + "\n")


def fetched(text, **kw):
    data = text.encode("utf-8")
    kw.setdefault("declared_length", len(data))
    kw.setdefault("received", len(data))
    return dw.Fetched(text, kw.pop("status", 200), kw.pop("declared_length"),
                      kw.pop("received"), **kw)


def entry(body, verified="2026-10-15", covers=("skills/x",)):
    return {"sha256": dc.sha256(body), "verified": verified, "covers": list(covers)}


def rubric_entry():
    return {"sha256": None, "verified": None, "covers": []}


class FakeResp:
    def __init__(self, body=b"", url=URL_A, status=200, headers=None, read_exc=None):
        self.body, self.url, self.status = body, url, status
        self.headers = {"Content-Length": str(len(body))} if headers is None else headers
        self.read_exc = read_exc

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n=-1):
        if self.read_exc:
            raise self.read_exc
        return self.body if n < 0 else self.body[:n]


def run_main(argv, **kw):
    kw.setdefault("today", TODAY)
    kw.setdefault("sleep", lambda s: None)
    kw.setdefault("env", {})
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = dw.main(argv, **kw)
    return code, out.getvalue(), err.getvalue()


def fetch_map(mapping, calls=None):
    """A fake dw.fetch: mapping url -> Fetched | Exception instance | list (consumed in order)."""
    def fake(url):
        if calls is not None:
            calls.append(url)
        value = mapping[url]
        if isinstance(value, list):
            value = value.pop(0)
        if isinstance(value, Exception):
            raise value
        return value
    return fake


class TmpCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.snaps = self.dir / "snapshots"
        self.src = self.dir / "sources.json"

    def write_sources(self, data):
        self.src.write_text(dw.dump_json(data) + "\n", encoding="utf-8")

    def sources2(self, pages, changelog=None):
        data = {"schema": 2, "normaliser_version": 2, "_comment": "test comment", "pages": pages}
        if changelog:
            data["changelog"] = changelog
        return data

    def install(self, url, body, verified="2026-10-15"):
        dw.write_snapshot(dw.snapshot_path(self.snaps, url), url, verified, body)

    def argv(self, *extra):
        return [*extra, "--sources", str(self.src), "--snapshots-dir", str(self.snaps)]

    def read_sources(self):
        return json.loads(self.src.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- fetch

class FetchTests(unittest.TestCase):
    def patch_urlopen(self, **kw):
        return mock.patch("urllib.request.urlopen", **kw)

    def test_success(self):
        with self.patch_urlopen(return_value=FakeResp(b"hello")):
            f = dw.fetch(URL_A)
        self.assertEqual((f.text, f.status, f.declared_length, f.received, f.read_error),
                         ("hello", 200, 5, 5, False))

    def test_non_https_refused_without_request(self):
        with self.patch_urlopen() as m:
            for url in ("http://code.claude.com/x.md", "file:///etc/passwd", "ftp://code.claude.com/x"):
                with self.assertRaises(dw.FetchError):
                    dw.fetch(url)
        m.assert_not_called()

    def test_unlisted_host_refused_without_request(self):
        with self.patch_urlopen() as m:
            with self.assertRaises(dw.FetchError):
                dw.fetch("https://evil.example/x.md")
        m.assert_not_called()

    def test_redirect_to_unlisted_host_refused(self):
        with self.patch_urlopen(return_value=FakeResp(b"x", url="https://evil.example/x.md")):
            with self.assertRaises(dw.FetchError):
                dw.fetch(URL_A)

    def test_redirect_to_http_refused(self):
        with self.patch_urlopen(return_value=FakeResp(b"x", url="http://code.claude.com/x.md")):
            with self.assertRaises(dw.FetchError):
                dw.fetch(URL_A)

    def test_redirect_to_allowlisted_host_ok(self):
        resp = FakeResp(b"x", url="https://raw.githubusercontent.com/o/r/main/x.md")
        with self.patch_urlopen(return_value=resp):
            self.assertEqual(dw.fetch(URL_A).text, "x")

    def test_size_cap(self):
        with mock.patch.object(dw, "MAX_BYTES", 10), \
                self.patch_urlopen(return_value=FakeResp(b"x" * 11)):
            with self.assertRaises(dw.FetchError):
                dw.fetch(URL_A)
        with mock.patch.object(dw, "MAX_BYTES", 10), \
                self.patch_urlopen(return_value=FakeResp(b"x" * 10)):
            self.assertEqual(dw.fetch(URL_A).received, 10)

    def test_incomplete_read_reported_as_truncated(self):
        exc = http.client.IncompleteRead(b"abc", 100)
        resp = FakeResp(b"", headers={"Content-Length": "100"}, read_exc=exc)
        with self.patch_urlopen(return_value=resp):
            f = dw.fetch(URL_A)
        self.assertTrue(f.read_error)
        self.assertEqual((f.text, f.received, f.declared_length), ("", 3, 100))
        outcome = dw.to_outcome(f)
        self.assertTrue(dc.length_truncated(outcome.declared_length, outcome.received,
                                            outcome.read_error))

    def test_other_http_exception_on_read_reported_as_truncated(self):
        resp = FakeResp(b"", read_exc=http.client.BadStatusLine("x"))
        with self.patch_urlopen(return_value=resp):
            self.assertTrue(dw.fetch(URL_A).read_error)

    def test_declared_longer_than_received_is_visible(self):
        resp = FakeResp(b"abc", headers={"Content-Length": "50"})
        with self.patch_urlopen(return_value=resp):
            f = dw.fetch(URL_A)
        self.assertEqual((f.declared_length, f.received), (50, 3))

    def test_bad_content_length_header_ignored(self):
        resp = FakeResp(b"abc", headers={"Content-Length": "oops"})
        with self.patch_urlopen(return_value=resp):
            self.assertIsNone(dw.fetch(URL_A).declared_length)

    def http_error(self, code):
        return urllib.error.HTTPError(URL_A, code, "msg", {}, io.BytesIO(b""))

    def test_404_and_410_are_gone(self):
        for code in (404, 410):
            with self.patch_urlopen(side_effect=self.http_error(code)):
                with self.assertRaises(dw.FetchGone) as ctx:
                    dw.fetch(URL_A)
            self.assertEqual(ctx.exception.status, code)

    def test_other_http_error_is_plain_fetch_error(self):
        with self.patch_urlopen(side_effect=self.http_error(500)):
            with self.assertRaises(dw.FetchError) as ctx:
                dw.fetch(URL_A)
        self.assertNotIsInstance(ctx.exception, dw.FetchGone)

    def test_transport_errors_are_fetch_errors(self):
        for exc in (urllib.error.URLError("down"), TimeoutError("slow"), OSError("reset"),
                    http.client.RemoteDisconnected("gone")):
            with self.patch_urlopen(side_effect=exc):
                with self.assertRaises(dw.FetchError):
                    dw.fetch(URL_A)


class FetchCheckedTests(unittest.TestCase):
    GOOD = make_body()

    def test_clean_first_attempt_no_sleep(self):
        sleep = mock.Mock()
        with mock.patch.object(dw, "fetch", return_value=fetched(self.GOOD)) as m:
            outcome = dw.fetch_checked(URL_A, self.GOOD, sleep=sleep)
        self.assertEqual(m.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(outcome.text, self.GOOD)

    def test_anomaly_then_good_retries_with_delay(self):
        sleep = mock.Mock()
        seq = [fetched(""), fetched(self.GOOD)]
        with mock.patch.object(dw, "fetch", side_effect=seq) as m:
            outcome = dw.fetch_checked(URL_A, self.GOOD, sleep=sleep)
        self.assertEqual(m.call_count, 2)
        sleep.assert_called_once_with(dc.FETCH_RETRY_DELAY_S)
        self.assertEqual(outcome.text, self.GOOD)

    def test_persistent_anomaly_is_major_after_all_attempts(self):
        sleep = mock.Mock()
        with mock.patch.object(dw, "fetch", return_value=fetched("<html>oops</html>")) as m:
            outcome = dw.fetch_checked(URL_A, self.GOOD, sleep=sleep)
        self.assertEqual(m.call_count, dc.FETCH_ATTEMPTS)
        self.assertEqual(sleep.call_count, dc.FETCH_ATTEMPTS - 1)
        result = dc.evaluate_page(self.GOOD, ("x",), outcome, dc.PageState(), TODAY)
        self.assertEqual(result.status, dc.MAJOR)

    def test_truncated_response_is_retried(self):
        bad = fetched(self.GOOD, declared_length=len(self.GOOD) + 500)
        sleep = mock.Mock()
        with mock.patch.object(dw, "fetch", side_effect=[bad, fetched(self.GOOD)]):
            outcome = dw.fetch_checked(URL_A, self.GOOD, sleep=sleep)
        sleep.assert_called_once()
        self.assertEqual(outcome.declared_length, outcome.received)

    def test_gone_not_retried(self):
        sleep = mock.Mock()
        with mock.patch.object(dw, "fetch", side_effect=dw.FetchGone("gone", 410)) as m:
            outcome = dw.fetch_checked(URL_A, self.GOOD, sleep=sleep)
        self.assertEqual((m.call_count, outcome.status), (1, 410))
        sleep.assert_not_called()

    def test_transport_failure_not_retried_and_unreachable(self):
        with mock.patch.object(dw, "fetch", side_effect=dw.FetchError("down")) as m:
            outcome = dw.fetch_checked(URL_A, self.GOOD, sleep=mock.Mock())
        self.assertEqual(m.call_count, 1)
        self.assertTrue(outcome.unreachable)


class FetchStableTests(unittest.TestCase):
    def test_identical_bodies(self):
        body = make_body()
        with mock.patch.object(dw, "fetch", return_value=fetched(body)) as m:
            got, last = dw.fetch_stable(URL_A)
        self.assertEqual((got, m.call_count), (body, dc.REBASELINE_STABLE_FETCHES))
        self.assertEqual(last.text, body)

    def test_whitespace_only_differences_are_stable(self):
        body = make_body()
        seq = [fetched(body), fetched(body.replace("\n", "\r\n")), fetched(body + "\n\n")]
        with mock.patch.object(dw, "fetch", side_effect=seq):
            self.assertEqual(dw.fetch_stable(URL_A)[0], body)

    def test_disagreement_refuses(self):
        seq = [fetched(make_body(30)), fetched(make_body(31)), fetched(make_body(30))]
        with mock.patch.object(dw, "fetch", side_effect=seq):
            with self.assertRaises(dw.Unstable):
                dw.fetch_stable(URL_A)


# ---------------------------------------------------------------- snapshots and sources

class SnapshotTests(TmpCase):
    def test_round_trip_and_exact_header(self):
        body = make_body()
        path = self.snaps / "alpha.md"
        dw.write_snapshot(path, URL_A, "2026-10-15", body)
        raw = path.read_bytes()
        self.assertNotIn(b"\r", raw)
        header = raw.decode("utf-8").split("\n", 1)[0]
        self.assertEqual(
            header,
            f"<!-- claudit-snapshot v1 | source: {URL_A} | normaliser: 2 | verified: 2026-10-15 "
            "| written by doc_watch.py --rebaseline; do not edit -->")
        snap = dw.load_snapshot(path)
        self.assertEqual((snap.source, snap.normaliser, snap.verified, snap.body),
                         (URL_A, 2, "2026-10-15", body))

    def test_refuses_unnormalised_body(self):
        with self.assertRaises(dw.SnapshotError):
            dw.write_snapshot(self.snaps / "a.md", URL_A, "2026-10-15", "a  b\n")
        self.assertFalse((self.snaps / "a.md").exists())

    def test_refuses_bad_date_and_url(self):
        body = make_body()
        with self.assertRaises(dw.SnapshotError):
            dw.write_snapshot(self.snaps / "a.md", URL_A, "15/10/2026", body)
        with self.assertRaises(dw.SnapshotError):
            dw.write_snapshot(self.snaps / "a.md", URL_A + "-->x", "2026-10-15", body)

    def test_bad_headers_rejected(self):
        for text in ("no header\nbody\n", "", "<!-- claudit-snapshot v2 | x -->\nbody\n"):
            path = self.dir / "bad.md"
            path.write_bytes(text.encode("utf-8"))
            with self.assertRaises(dw.SnapshotError):
                dw.load_snapshot(path)

    def test_missing_file(self):
        with self.assertRaises(dw.SnapshotError):
            dw.load_snapshot(self.dir / "nope.md")

    def test_snapshot_path_uses_slug(self):
        self.assertEqual(dw.snapshot_path(self.snaps, URL_A), self.snaps / "alpha.md")


class SourcesTests(TmpCase):
    def good(self):
        return {"schema": 2, "normaliser_version": 2, "_comment": "c",
                "pages": {URL_A: {"sha256": "a" * 64, "verified": "2026-10-15", "covers": ["s/x"]},
                          URL_RUBRIC: rubric_entry()},
                "changelog": {"url": URL_CL, "last_reviewed_version": "2.1.300"}}

    def test_valid(self):
        dw.validate_sources(self.good())

    def assert_invalid(self, mutate):
        data = copy.deepcopy(self.good())
        mutate(data)
        with self.assertRaises(dw.ConfigError):
            dw.validate_sources(data)

    def test_invalid_variants(self):
        cases = {
            "schema 1": lambda d: d.pop("schema"),
            "normaliser": lambda d: d.update(normaliser_version=1),
            "threshold key": lambda d: d.update(SECTION_MAJOR_RATIO=0.9),
            "entry threshold key": lambda d: d["pages"][URL_A].update(ratio=1),
            "bad sha": lambda d: d["pages"][URL_A].update(sha256="xyz"),
            "upper sha": lambda d: d["pages"][URL_A].update(sha256="A" * 64),
            "sha without verified": lambda d: d["pages"][URL_A].update(verified=None),
            "verified without sha": lambda d: d["pages"][URL_RUBRIC].update(verified="2026-10-15"),
            "bad date": lambda d: d["pages"][URL_A].update(verified="2026-13-45"),
            "covers not list": lambda d: d["pages"][URL_A].update(covers="s/x"),
            "covers non string": lambda d: d["pages"][URL_A].update(covers=[1]),
            "host not allowed": lambda d: d["pages"].update({"https://evil.example/z.md": rubric_entry()}),
            "http page": lambda d: d["pages"].update({"http://code.claude.com/z.md": rubric_entry()}),
            "duplicate slug": lambda d: d["pages"].update(
                {"https://platform.claude.com/docs/en/alpha.md": rubric_entry()}),
            "changelog not object": lambda d: d.update(changelog="x"),
            "changelog extra key": lambda d: d["changelog"].update(x=1),
            "changelog bad host": lambda d: d["changelog"].update(url="https://evil.example/c"),
            "changelog last type": lambda d: d["changelog"].update(last_reviewed_version=3),
        }
        for name, mutate in cases.items():
            with self.subTest(name):
                self.assert_invalid(mutate)

    def test_load_sources_errors(self):
        with self.assertRaises(dw.ConfigError):
            dw.load_sources(self.dir / "missing.json")
        self.src.write_text("{not json", encoding="utf-8")
        with self.assertRaises(dw.ConfigError):
            dw.load_sources(self.src)
        self.src.write_text("[]", encoding="utf-8")
        with self.assertRaises(dw.ConfigError):
            dw.load_sources(self.src)

    def test_dump_json_preserves_order_and_style(self):
        data = self.good()
        data["pages"][URL_A]["covers"] = ["a/b", "c/d"]
        text = dw.dump_json(data)
        self.assertEqual(json.loads(text), data)
        self.assertEqual(list(json.loads(text)), list(data))
        self.assertIn('"covers": ["a/b", "c/d"]', text)
        self.assertIn('"covers": []', text)
        self.assertIn('  "pages": {\n    "' + URL_A + '": {\n      "sha256"', text)

    def test_main_exit_2_on_bad_config(self):
        self.src.write_text("{not json", encoding="utf-8")
        for argv in ([], ["--diff", "alpha"], ["--rebaseline"]):
            with mock.patch.object(dw, "fetch") as m:
                code, _, err = run_main(self.argv(*argv))
            self.assertEqual(code, 2, argv)
            m.assert_not_called()
        data = self.good()
        del data["schema"]
        self.write_sources(data)
        self.assertEqual(run_main(self.argv())[0], 2)


# ---------------------------------------------------------------- rebaseline

class RebaselineTests(TmpCase):
    def setUp(self):
        super().setUp()
        self.old = make_body(30)
        self.live = make_body(31)
        self.write_sources(self.sources2(
            {URL_A: entry(self.old), URL_RUBRIC: rubric_entry()},
            {"url": URL_CL, "last_reviewed_version": "2.1.300"}))

    def rebaseline(self, mapping, *extra, calls=None):
        with mock.patch.object(dw, "fetch", side_effect=fetch_map(mapping, calls)):
            return run_main(self.argv("--rebaseline", *extra))

    def test_writes_snapshot_hash_and_verified_preserving_order(self):
        before = self.read_sources()
        calls = []
        code, out, _ = self.rebaseline({URL_A: fetched(self.live)}, calls=calls)
        self.assertEqual(code, 0)
        self.assertEqual(calls, [URL_A] * dc.REBASELINE_STABLE_FETCHES)
        after = self.read_sources()
        self.assertEqual(list(after), list(before))
        self.assertEqual(after["_comment"], "test comment")
        self.assertEqual(after["pages"][URL_A],
                         {"sha256": dc.sha256(self.live), "verified": "2026-10-16",
                          "covers": ["skills/x"]})
        self.assertEqual(after["pages"][URL_RUBRIC], rubric_entry())
        self.assertEqual(after["changelog"], before["changelog"])
        snap = dw.load_snapshot(self.snaps / "alpha.md")
        self.assertEqual((snap.body, snap.verified), (self.live, "2026-10-16"))
        self.assertFalse((self.snaps / "rubric.md").exists())
        self.assertIn("BASELINED  alpha", out)

    def test_date_option(self):
        code, _, _ = self.rebaseline({URL_A: fetched(self.live)}, "--date", "2026-10-01")
        self.assertEqual(code, 0)
        self.assertEqual(self.read_sources()["pages"][URL_A]["verified"], "2026-10-01")

    def test_only_selects_pages_including_rubric(self):
        code, _, _ = self.rebaseline({URL_RUBRIC: fetched(self.live)}, "--only", "rubric")
        self.assertEqual(code, 0)
        data = self.read_sources()
        self.assertEqual(data["pages"][URL_RUBRIC]["sha256"], dc.sha256(self.live))
        self.assertEqual(data["pages"][URL_A]["sha256"], dc.sha256(self.old))

    def test_unknown_only_slug_exit_2(self):
        code, _, err = self.rebaseline({}, "--only", "nope")
        self.assertEqual(code, 2)
        self.assertIn("nope", err)

    def assert_refused_unchanged(self, code, snapshots=False):
        self.assertEqual(code, 3)
        self.assertEqual(self.before_bytes, self.src.read_bytes())
        if not snapshots:
            self.assertEqual(list(self.snaps.glob("*")), [])

    def test_refuses_anomaly_then_force_overrides(self):
        self.before_bytes = self.src.read_bytes()
        code, out, _ = self.rebaseline({URL_A: fetched("tiny")})
        self.assert_refused_unchanged(code)
        self.assertIn("REFUSED  alpha", out)
        code, _, _ = self.rebaseline({URL_A: fetched("tiny")}, "--force")
        self.assertEqual(code, 0)
        self.assertEqual(dw.load_snapshot(self.snaps / "alpha.md").body, "tiny\n")

    def test_refuses_truncated_fetch(self):
        self.before_bytes = self.src.read_bytes()
        bad = fetched(self.live, declared_length=len(self.live) + 100)
        code, _, _ = self.rebaseline({URL_A: bad})
        self.assert_refused_unchanged(code)

    def test_refuses_unstable(self):
        self.before_bytes = self.src.read_bytes()
        seq = [fetched(make_body(30)), fetched(make_body(31)), fetched(make_body(32))]
        code, out, _ = self.rebaseline({URL_A: seq})
        self.assert_refused_unchanged(code)
        self.assertIn("unstable", out)

    def test_refuses_unreachable_and_gone(self):
        self.before_bytes = self.src.read_bytes()
        for exc in (dw.FetchError("down"), dw.FetchGone("gone", 404)):
            code, out, _ = self.rebaseline({URL_A: exc})
            self.assert_refused_unchanged(code)
            self.assertIn("unreachable", out)

    def test_one_refusal_does_not_block_other_pages(self):
        self.write_sources(self.sources2({URL_A: entry(self.old), URL_B: entry(self.old)}))
        code, _, _ = self.rebaseline({URL_A: fetched("tiny"), URL_B: fetched(self.live)})
        self.assertEqual(code, 3)
        data = self.read_sources()
        self.assertEqual(data["pages"][URL_B]["sha256"], dc.sha256(self.live))
        self.assertEqual(data["pages"][URL_A]["sha256"], dc.sha256(self.old))

    def test_truncated_fetch_never_baselined_even_with_force(self):
        self.before_bytes = self.src.read_bytes()
        cut = dw.Fetched("", 200, 100, 3, read_error=True)
        for extra in ((), ("--force",)):
            code, out, _ = self.rebaseline({URL_A: cut}, *extra)
            self.assert_refused_unchanged(code)
            self.assertIn("truncated", out)
        short = fetched(self.live, declared_length=len(self.live) + 1)
        code, _, _ = self.rebaseline({URL_A: short}, "--force")
        self.assert_refused_unchanged(code)

    def test_incomplete_read_through_real_fetch_never_baselined(self):
        self.before_bytes = self.src.read_bytes()

        def resp(*a, **kw):
            return FakeResp(b"", headers={"Content-Length": "100"},
                            read_exc=http.client.IncompleteRead(self.live.encode()[:50], 100))

        with mock.patch("urllib.request.urlopen", side_effect=resp):
            code, out, _ = run_main(self.argv("--rebaseline", "--force"))
        self.assert_refused_unchanged(code)
        self.assertIn("truncated", out)

    def test_option_combinations_rejected(self):
        for argv in (["--migrate"], ["--only", "alpha"], ["--date", "2026-10-01"], ["--force"],
                     ["--rebaseline", "--date", "yesterday"],
                     ["--rebaseline", "--migrate", "--only", "alpha"],
                     ["--rebaseline", "--diff", "alpha"], ["--rebaseline", "--github"]):
            with mock.patch.object(dw, "fetch") as m:
                code, _, _ = run_main(self.argv(*argv))
            self.assertEqual(code, 2, argv)
            m.assert_not_called()


class MigrateTests(TmpCase):
    RAW = "# Sample page\n" + "".join(
        f"Paragraph {i}   describes  the widget behaviour.  \n" for i in range(30))

    def setUp(self):
        super().setUp()
        self.v1 = dc.sha256(dc.normalise_v1(self.RAW))
        self.v2_body = dc.normalise(self.RAW)
        self.assertNotEqual(dc.normalise_v1(self.RAW), self.v2_body)

    def schema1(self, pages):
        return {"_comment": "old comment", "pages": pages,
                "changelog": {"url": URL_CL, "last_reviewed_version": "2.1.300"}}

    def migrate(self, mapping, *extra):
        with mock.patch.object(dw, "fetch", side_effect=fetch_map(mapping)):
            return run_main(self.argv("--rebaseline", "--migrate", *extra))

    def test_happy_path_uses_v1_hash_and_keeps_verified(self):
        self.write_sources(self.schema1({
            URL_A: {"sha256": self.v1, "verified": "2026-10-15", "covers": ["skills/x"]},
            URL_RUBRIC: rubric_entry()}))
        code, _, _ = self.migrate({URL_A: fetched(self.RAW)})
        self.assertEqual(code, 0)
        data = self.read_sources()
        self.assertEqual(list(data)[:3], ["schema", "normaliser_version", "_comment"])
        self.assertEqual((data["schema"], data["normaliser_version"]), (2, 2))
        self.assertEqual(data["pages"][URL_A],
                         {"sha256": dc.sha256(self.v2_body), "verified": "2026-10-15",
                          "covers": ["skills/x"]})
        self.assertEqual(data["pages"][URL_RUBRIC], rubric_entry())
        snap = dw.load_snapshot(self.snaps / "alpha.md")
        self.assertEqual((snap.body, snap.verified), (self.v2_body, "2026-10-15"))
        dw.validate_sources(data)

    def changed_sources(self):
        self.write_sources(self.schema1({
            URL_A: {"sha256": self.v1, "verified": "2026-10-15", "covers": ["skills/x"]},
            URL_B: {"sha256": "b" * 64, "verified": "2026-10-15", "covers": ["skills/y"]}}))

    def test_mismatch_without_date_refused_and_nothing_written(self):
        self.changed_sources()
        before = self.src.read_bytes()
        code, out, _ = self.migrate({URL_A: fetched(self.RAW), URL_B: fetched(self.RAW)})
        self.assertEqual(code, 3)
        self.assertIn("CHANGED SINCE BASELINE  beta", out)
        self.assertEqual(self.src.read_bytes(), before)
        self.assertEqual(list(self.snaps.glob("*")), [])

    def test_mismatch_with_date_accepts_that_date(self):
        self.changed_sources()
        code, _, _ = self.migrate({URL_A: fetched(self.RAW), URL_B: fetched(self.RAW)},
                                  "--date", "2026-10-17")
        self.assertEqual(code, 0)
        pages = self.read_sources()["pages"]
        self.assertEqual(pages[URL_A]["verified"], "2026-10-15")
        self.assertEqual(pages[URL_B]["verified"], "2026-10-17")
        self.assertEqual(pages[URL_B]["sha256"], dc.sha256(self.v2_body))

    def test_unstable_page_refuses_migration(self):
        self.write_sources(self.schema1({
            URL_A: {"sha256": self.v1, "verified": "2026-10-15", "covers": ["skills/x"]}}))
        before = self.src.read_bytes()
        seq = [fetched(self.RAW), fetched(make_body(5)), fetched(self.RAW)]
        code, _, _ = self.migrate({URL_A: seq})
        self.assertEqual(code, 3)
        self.assertEqual(self.src.read_bytes(), before)

    def test_schema_2_input_rejected(self):
        self.write_sources({"schema": 2, "normaliser_version": 2, "pages": {}})
        self.assertEqual(self.migrate({})[0], 2)


# ---------------------------------------------------------------- dry run and run_checks

class DryRunTests(TmpCase):
    def setUp(self):
        super().setUp()
        self.body = make_body()
        self.install(URL_A, self.body)
        self.install(URL_B, self.body)
        self.pages = {URL_A: entry(self.body), URL_B: entry(self.body, covers=("agents/y",))}
        self.write_sources(self.sources2(self.pages))

    def dry(self, mapping, *extra, **kw):
        with mock.patch.object(dw, "fetch", side_effect=fetch_map(mapping)):
            return run_main(self.argv(*extra), **kw)

    def test_clean_run(self):
        code, out, _ = self.dry({URL_A: fetched(self.body), URL_B: fetched(self.body)})
        self.assertEqual(code, 0)
        self.assertIn("No page needs a check.", out)

    def test_major_change_listed_with_reasons_diff_and_dependents(self):
        live = self.body.replace("Paragraph 5 ", "Paragraph 55 ")
        code, out, _ = self.dry({URL_A: fetched(live), URL_B: fetched(self.body)})
        self.assertEqual(code, 0)
        self.assertIn("MAJOR  alpha", out)
        self.assertIn("number changed", out)
        self.assertIn("- Paragraph 5 describes", out)
        self.assertIn("+ Paragraph 55 describes", out)
        self.assertIn("plugins/claudit/skills/x", out)
        self.assertNotIn("MAJOR  beta", out)

    def test_minor_change_listed_exit_0(self):
        live = self.body.replace("behaviour", "behavoiur", 1)
        code, out, _ = self.dry({URL_A: fetched(live), URL_B: fetched(self.body)})
        self.assertEqual(code, 0)
        self.assertIn("MINOR  alpha", out)
        self.assertIn("No page needs a check.", out)

    def test_dry_run_makes_no_github_or_state_writes(self):
        before = sorted(p.name for p in self.dir.rglob("*"))
        with mock.patch.object(dw, "gh_request") as gh, \
                mock.patch.object(dw, "legacy_main") as legacy, \
                mock.patch("urllib.request.urlopen") as net:
            code, _, _ = self.dry({URL_A: fetched(self.body), URL_B: fetched(self.body)})
        self.assertEqual(code, 0)
        gh.assert_not_called()
        legacy.assert_not_called()
        net.assert_not_called()
        self.assertEqual(sorted(p.name for p in self.dir.rglob("*")), before)

    def test_missing_snapshot_is_major_no_baseline(self):
        (self.snaps / "alpha.md").unlink()
        code, out, _ = self.dry({URL_A: fetched(self.body), URL_B: fetched(self.body)})
        self.assertEqual(code, 0)
        self.assertIn("no baseline", out)

    def test_404_is_immediate_major(self):
        code, out, _ = self.dry({URL_A: dw.FetchGone("gone", 404), URL_B: fetched(self.body)})
        self.assertEqual(code, 0)
        self.assertIn(dc.ANOM_GONE, out)

    def test_anomalous_fetch_retries_via_injected_sleep(self):
        sleep = mock.Mock()
        code, out, _ = self.dry({URL_A: fetched(""), URL_B: fetched(self.body)}, sleep=sleep)
        self.assertEqual(code, 0)
        self.assertEqual(sleep.call_count, dc.FETCH_ATTEMPTS - 1)
        self.assertIn("MAJOR  alpha", out)

    def test_all_unreachable_exit_1(self):
        down = dw.FetchError("down")
        code, out, _ = self.dry({URL_A: down, URL_B: down})
        self.assertEqual(code, 1)
        self.assertIn("UNREACHABLE", out)

    def test_all_unreachable_includes_changelog(self):
        self.write_sources(self.sources2(self.pages, {"url": URL_CL, "last_reviewed_version": "2.1.300"}))
        down = dw.FetchError("down")
        code, _, _ = self.dry({URL_A: down, URL_B: down, URL_CL: fetched(FEED)})
        self.assertEqual(code, 0)
        code, _, _ = self.dry({URL_A: down, URL_B: down, URL_CL: down})
        self.assertEqual(code, 1)

    def test_partial_outage_is_exit_0(self):
        code, out, _ = self.dry({URL_A: dw.FetchError("down"), URL_B: fetched(self.body)})
        self.assertEqual(code, 0)
        self.assertIn("UNREACHABLE  alpha", out)

    def test_state_file_drives_unreachable_escalation(self):
        state = {"schema": 1, "updated": "2026-10-14", "major": [],
                 "pages": {"alpha": {"minor_first_seen": None, "unreachable_since": "2026-10-13"}},
                 "changelog": {"new_versions": [], "first_seen": None}}
        path = self.dir / "state.json"
        path.write_text(json.dumps(state), encoding="utf-8")
        before = path.read_bytes()
        code, out, _ = self.dry({URL_A: dw.FetchError("down"), URL_B: fetched(self.body)},
                                "--state-file", str(path))
        self.assertEqual(code, 0)
        self.assertIn("unreachable 3 days", out)
        self.assertEqual(path.read_bytes(), before)

    def test_corrupt_state_file_exit_1_before_any_fetch(self):
        path = self.dir / "state.json"
        path.write_text("{broken", encoding="utf-8")
        with mock.patch.object(dw, "fetch") as m:
            code, _, err = run_main(self.argv("--state-file", str(path)))
        self.assertEqual(code, 1)
        m.assert_not_called()
        self.assertIn("state", err)
        code, _, _ = run_main(self.argv("--state-file", str(self.dir / "absent.json")))
        self.assertEqual(code, 1)

    def test_changelog_new_versions_reported(self):
        self.write_sources(self.sources2(self.pages, {"url": URL_CL, "last_reviewed_version": "2.1.295"}))
        code, out, _ = self.dry({URL_A: fetched(self.body), URL_B: fetched(self.body),
                                 URL_CL: fetched(FEED)})
        self.assertEqual(code, 0)
        self.assertIn("new Claude Code versions: 2.1.300", out)

    def test_run_checks_isolates_a_raising_page(self):
        sources = self.sources2(self.pages)
        original = dw.fetch_checked

        def flaky(url, *a, **kw):
            if url == URL_A:
                raise RuntimeError("boom")
            return original(url, *a, **kw)

        with mock.patch.object(dw, "fetch", side_effect=fetch_map(
                {URL_A: fetched(self.body), URL_B: fetched(self.body)})), \
                mock.patch.object(dw, "fetch_checked", side_effect=flaky):
            report = dw.run_checks(sources, self.snaps, dw.DigestState(), TODAY)
            code, out, _ = run_main(self.argv())
        by_slug = {p.slug: p.result for p in report.pages}
        self.assertEqual(by_slug["alpha"].status, dc.MAJOR)
        self.assertEqual(by_slug["alpha"].reasons, ["classifier error: RuntimeError"])
        self.assertEqual(by_slug["beta"].status, dc.CLEAN)
        self.assertEqual(report.major_slugs, ["alpha"])
        self.assertEqual(code, 0)
        self.assertIn("classifier error: RuntimeError", out)

    def test_corrupt_snapshot_isolated_to_its_page(self):
        (self.snaps / "alpha.md").write_bytes(b"garbage\n")
        with mock.patch.object(dw, "fetch", side_effect=fetch_map(
                {URL_A: fetched(self.body), URL_B: fetched(self.body)})):
            report = dw.run_checks(self.sources2(self.pages), self.snaps, dw.DigestState(), TODAY)
        by_slug = {p.slug: p.result for p in report.pages}
        self.assertEqual(by_slug["alpha"].reasons, ["classifier error: SnapshotError"])
        self.assertEqual(by_slug["beta"].status, dc.CLEAN)

    def test_state_after_tracks_minor_and_unreachable_timers(self):
        live = self.body.replace("behaviour", "behavoiur", 1)
        with mock.patch.object(dw, "fetch", side_effect=fetch_map(
                {URL_A: fetched(live), URL_B: dw.FetchError("down")})):
            report = dw.run_checks(self.sources2(self.pages), self.snaps, dw.DigestState(), TODAY)
        after = report.state_after
        self.assertEqual(after.updated, "2026-10-16")
        self.assertEqual(after.pages["alpha"].minor_first_seen, "2026-10-16")
        self.assertEqual(after.pages["beta"].unreachable_since, "2026-10-16")
        self.assertEqual(after.major, ())


class DiffCommandTests(TmpCase):
    def setUp(self):
        super().setUp()
        self.body = make_body()
        self.install(URL_A, self.body)
        self.write_sources(self.sources2({URL_A: entry(self.body)}))

    def test_diff_shows_classified_hunks(self):
        live = self.body.replace("Paragraph 5 ", "Paragraph 55 ")
        with mock.patch.object(dw, "fetch", return_value=fetched(live)):
            code, out, _ = run_main(self.argv("--diff", "alpha"))
        self.assertEqual(code, 0)
        self.assertIn("[MAJOR: number changed]", out)
        self.assertIn("+ Paragraph 55 describes", out)

    def test_diff_identical(self):
        with mock.patch.object(dw, "fetch", return_value=fetched(self.body)):
            code, out, _ = run_main(self.argv("--diff", "alpha"))
        self.assertEqual(code, 0)
        self.assertIn("no differences", out)

    def test_diff_errors(self):
        with mock.patch.object(dw, "fetch", return_value=fetched(self.body)):
            self.assertEqual(run_main(self.argv("--diff", "nope"))[0], 2)
        (self.snaps / "alpha.md").unlink()
        with mock.patch.object(dw, "fetch", return_value=fetched(self.body)):
            self.assertEqual(run_main(self.argv("--diff", "alpha"))[0], 2)
        self.install(URL_A, self.body)
        with mock.patch.object(dw, "fetch", side_effect=dw.FetchError("down")):
            self.assertEqual(run_main(self.argv("--diff", "alpha"))[0], 1)


class NotImplementedFlagTests(TmpCase):
    def setUp(self):
        super().setUp()
        self.write_sources(self.sources2({URL_RUBRIC: rubric_entry()}))

    def run_guarded(self, argv, env):
        with mock.patch.object(dw, "gh_request") as gh, mock.patch("urllib.request.urlopen") as net, \
                mock.patch.object(dw, "fetch") as fetch:
            result = run_main(self.argv(*argv), env=env)
        gh.assert_not_called()
        net.assert_not_called()
        fetch.assert_not_called()
        return result

    def test_github_without_env_exits_1(self):
        for env in ({}, {"GITHUB_TOKEN": "t"}, {"GITHUB_REPOSITORY": "o/r"}):
            code, _, err = self.run_guarded(["--github"], env)
            self.assertEqual(code, 1)
            self.assertIn("GITHUB_TOKEN", err)

    def test_every_github_mode_without_credentials_exits_1_before_network(self):
        for argv in (["--github"], ["--shadow"], ["--close-legacy"]):
            code, _, err = self.run_guarded(argv, {})
            self.assertEqual(code, 1, argv)
            self.assertIn("GITHUB_TOKEN", err)

    def test_bad_repository_name_exits_1_before_network_in_every_mode(self):
        bad = ("../x", "o/..", "o/.", "o/r/", "o/r/extra", "/r", "o/", "o", "", " o/r", "o/r ",
               "o/r\n", "o /r", "o/r?x=1", "o/r#x", "o\\r", "o/%2e%2e", "-/..", "o/r;x")
        for repo in bad:
            for argv in (["--github"], ["--shadow"], ["--close-legacy"]):
                env = {"GITHUB_TOKEN": "dummy", "GITHUB_REPOSITORY": repo}
                self.assertEqual(self.run_guarded(argv, env)[0], 1, (repo, argv))

    def test_good_repository_names_pass_the_shape_check(self):
        for repo in ("o/r", "Owner-1/repo.name_x", "a/.github", "a/b..c"):
            self.assertIsNotNone(dw._REPO_RE.fullmatch(repo), repo)

    def test_github_combined_with_local_modes_is_usage_error(self):
        for argv in (["--github", "--shadow"], ["--github", "--diff", "x"],
                     ["--github", "--print-hashes"], ["--shadow", "--close-legacy"]):
            self.assertEqual(self.run_guarded(argv, {})[0], 2, argv)

    def test_state_file_only_for_plain_dry_run(self):
        self.assertEqual(self.run_guarded(["--shadow", "--state-file", "x"], {})[0], 2)


class PrintHashesTests(TmpCase):
    def test_alias_prints_json_and_deprecation_note(self):
        body = make_body()
        self.write_sources(self.sources2({URL_A: entry(body)},
                                         {"url": URL_CL, "last_reviewed_version": "2.1.300"}))
        mapping = {URL_A: fetched(body), URL_CL: fetched(FEED)}
        with mock.patch.object(dw, "fetch", side_effect=fetch_map(mapping)):
            code, out, err = run_main(self.argv("--print-hashes"))
        self.assertEqual(code, 0)
        self.assertIn("deprecated", err)
        printed = json.loads(out)
        self.assertEqual(printed[URL_A], dc.sha256(body))
        self.assertEqual(printed["changelog_latest"], "2.1.300")

    def test_alias_does_not_hash_truncated_pages(self):
        self.write_sources(self.sources2({URL_A: entry(make_body())}))
        cut = dw.Fetched("", 200, 100, 3, read_error=True)
        with mock.patch.object(dw, "fetch", side_effect=fetch_map({URL_A: cut})):
            code, out, _ = run_main(self.argv("--print-hashes"))
        self.assertEqual((code, json.loads(out)), (0, {}))

    def test_alias_skips_unreachable_and_works_on_schema_1(self):
        self.write_sources({"pages": {URL_A: rubric_entry()}})
        with mock.patch.object(dw, "fetch", side_effect=dw.FetchError("down")):
            code, out, _ = run_main(self.argv("--print-hashes"))
        self.assertEqual((code, json.loads(out)), (0, {}))


# ---------------------------------------------------------------- digest state

class StateTests(unittest.TestCase):
    def good(self):
        return {"schema": 1, "updated": "2026-10-16", "major": ["alpha"],
                "pages": {"alpha": {"minor_first_seen": "2026-10-03", "unreachable_since": None}},
                "changelog": {"new_versions": ["2.1.310"], "first_seen": "2026-10-14"}}

    def test_round_trip(self):
        state = dw.DigestState("2026-10-16", ("alpha",),
                               {"alpha": dc.PageState("2026-10-03", None)},
                               dc.ChangelogState(("2.1.310",), "2026-10-14"))
        text = dw.render_state(state)
        self.assertTrue(text.startswith("<!-- doc-watch-state {") and text.endswith("} -->"))
        self.assertEqual(dw.read_state("Digest body\n\n" + text + "\n"), state)

    def test_empty_state_round_trip(self):
        self.assertEqual(dw.read_state(dw.render_state(dw.DigestState())), dw.DigestState())

    def test_parse_good(self):
        state = dw.parse_state_json(json.dumps(self.good()))
        self.assertEqual(state.major, ("alpha",))
        self.assertEqual(state.pages["alpha"].minor_first_seen, "2026-10-03")

    def assert_bad(self, mutate):
        obj = self.good()
        mutate(obj)
        with self.assertRaises(dw.StateError):
            dw.parse_state_json(json.dumps(obj))

    def test_strict_validation(self):
        cases = {
            "extra top key": lambda o: o.update(extra=1),
            "missing key": lambda o: o.pop("updated"),
            "schema 2": lambda o: o.update(schema=2),
            "schema true": lambda o: o.update(schema=True),
            "bad updated": lambda o: o.update(updated="today"),
            "major not list": lambda o: o.update(major="alpha"),
            "major bad slug": lambda o: o.update(major=["../x"]),
            "major int": lambda o: o.update(major=[1]),
            "pages list": lambda o: o.update(pages=[]),
            "page bad slug": lambda o: o["pages"].update({"../x": {"minor_first_seen": None,
                                                                   "unreachable_since": None}}),
            "page extra key": lambda o: o["pages"]["alpha"].update(x=1),
            "page bad date": lambda o: o["pages"]["alpha"].update(minor_first_seen="2026-02-30"),
            "page date type": lambda o: o["pages"]["alpha"].update(unreachable_since=5),
            "changelog extra": lambda o: o["changelog"].update(x=1),
            "changelog bad version": lambda o: o["changelog"].update(new_versions=["v2"]),
            "changelog versions type": lambda o: o["changelog"].update(new_versions="2.1.3"),
            "changelog bad date": lambda o: o["changelog"].update(first_seen="soon"),
        }
        for name, mutate in cases.items():
            with self.subTest(name):
                self.assert_bad(mutate)

    def test_not_json_or_wrong_type(self):
        for text in ("", "{", "[]", "null", "[" * 100000):
            with self.assertRaises(dw.StateError):
                dw.parse_state_json(text)

    def test_size_cap(self):
        with self.assertRaises(dw.StateError):
            dw.parse_state_json(" " * (dc.STATE_MAX_CHARS + 1))

    def test_read_state_needs_marker_at_end(self):
        marker = dw.render_state(dw.DigestState())
        for body in ("no marker at all", marker + "\ntrailing text", marker.replace("{", "{{", 1)):
            with self.assertRaises(dw.StateError):
                dw.read_state(body)

    def test_render_state_refuses_oversize(self):
        pages = {f"slug-{i}": dc.PageState("2026-10-03", None) for i in range(2000)}
        with self.assertRaises(dw.StateError):
            dw.render_state(dw.DigestState("2026-10-16", (), pages))

    def test_comment_and_trim_diff(self):
        self.assertEqual(dw.render_comment(["a", "b"]),
                         "**New MAJOR on the drift digest:** a, b. 2 page(s) now need a check. "
                         "Details in the digest body.")
        old = make_body(30)
        live = old.replace("Paragraph 5 ", "Paragraph 55 ")
        lines = dw.trim_diff(dc.diff_page(old, live))
        self.assertIn("+ Paragraph 55 describes the widget behaviour in plain words.", lines)
        big_old = "# H\n" + "".join(f"old {i}\n" for i in range(100))
        big_new = "# H\n" + "".join(f"new {i}\n" for i in range(100))
        diff = dc.diff_page(big_old, big_new)
        self.assertLessEqual(len(dw.trim_diff(diff, hunk_max=10)), 11)
        self.assertTrue(any("use --diff" in ln for ln in dw.trim_diff(diff, hunk_max=10)))


# ---------------------------------------------------------------- legacy path

class LegacyTests(TmpCase):
    RAW = "# Page\nSome   spaced  text.  \n\n\nMore text.\n"

    def legacy(self, raw=None, **kw):
        with mock.patch.object(dw, "legacy_fetch", return_value=raw or self.RAW):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = dw.main(self.argv("--legacy-issues"), **kw)
        return code, out.getvalue()

    def page(self, sha):
        return {URL_A: {"sha256": sha, "verified": "2026-10-15", "covers": ["skills/x"]}}

    def test_schema_1_hashes_with_v1(self):
        self.write_sources({"pages": self.page(dc.sha256(dc.normalise_v1(self.RAW)))})
        code, out = self.legacy()
        self.assertEqual(code, 0)
        self.assertIn("No drift detected.", out)

    def test_schema_1_does_not_accept_v2_hash(self):
        self.write_sources({"pages": self.page(dc.sha256(dc.normalise(self.RAW)))})
        code, out = self.legacy()
        self.assertIn(f"CHANGED  {URL_A}", out)

    def test_schema_2_hashes_with_v2(self):
        self.write_sources(self.sources2(self.page(dc.sha256(dc.normalise(self.RAW)))))
        code, out = self.legacy()
        self.assertEqual(code, 0)
        self.assertIn("No drift detected.", out)

    def test_schema_2_does_not_accept_v1_hash(self):
        self.write_sources(self.sources2(self.page(dc.sha256(dc.normalise_v1(self.RAW)))))
        code, out = self.legacy()
        self.assertIn(f"CHANGED  {URL_A}", out)

    def test_legacy_reports_changelog_and_unreachable(self):
        data = {"pages": {URL_A: rubric_entry()},
                "changelog": {"url": URL_CL, "last_reviewed_version": "2.1.298"}}
        self.write_sources(data)

        def fake(url):
            if url == URL_CL:
                return FEED
            raise OSError("down")

        with mock.patch.object(dw, "legacy_fetch", side_effect=fake):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = dw.main(self.argv("--legacy-issues"))
        self.assertEqual(code, 0)
        self.assertIn("CHANGELOG  new Claude Code versions since last review: 2.1.300, 2.1.299",
                      out.getvalue())
        self.assertIn(f"UNREACHABLE  {URL_A}", out.getvalue())

    def test_legacy_all_unreachable_exit_1(self):
        self.write_sources({"pages": {URL_A: rubric_entry()}})
        with mock.patch.object(dw, "legacy_fetch", side_effect=OSError("down")):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(dw.main(self.argv("--legacy-issues")), 1)

    def test_legacy_github_without_env_exits_1_without_network(self):
        self.write_sources({"pages": self.page("c" * 64)})
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(dw, "gh_request") as gh, \
                mock.patch.object(dw, "legacy_fetch", return_value=self.RAW):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = dw.main(self.argv("--legacy-issues", "--github"))
        self.assertEqual(code, 1)
        gh.assert_not_called()
        self.assertIn("GITHUB_TOKEN", err.getvalue())

    def test_legacy_bad_file_exit_2(self):
        self.src.write_text("{nope", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(dw.main(self.argv("--legacy-issues")), 2)


# ---------------------------------------------------------------- digest rendering

def long_hunk(n_lines, major, marker, width=1000):
    old = tuple(f"{marker} old {i} " + "x" * width for i in range(n_lines))
    new = tuple(f"{marker} new {i} " + "y" * width for i in range(n_lines))
    return dc.Hunk(section=("## S", 1), tag="replace", old_lines=old, new_lines=new,
                   old_flags=(False,) * n_lines, new_flags=(False,) * n_lines, old_start=0,
                   new_start=0, section_old_len=400, section_new_len=400,
                   reasons=["code token changed"] if major else [], major=major)


def synthetic_page(slug, status, hunks, covers=("skills/x",)):
    diff = dc.PageDiff(hunks, 400, 400)
    reasons = ["code token changed"] if status == dc.MAJOR else ["1 minor change(s)"]
    return dw.PageReport(f"https://code.claude.com/docs/en/{slug}.md", slug, list(covers),
                         "2026-10-15", False,
                         dc.PageResult(status, reasons, diff=diff,
                                       state=dc.PageState("2026-10-01", None)))


def synthetic_report(pages, changelog=None):
    after = dw.DigestState("2026-10-16", tuple(p.slug for p in pages if p.result.status == dc.MAJOR),
                           {}, changelog.state if changelog else dc.ChangelogState())
    return dw.Report(pages, changelog, False, after)


class RenderHelperTests(unittest.TestCase):
    def test_fence_for(self):
        self.assertEqual(dw.fence_for("no ticks"), "```")
        self.assertEqual(dw.fence_for("a ``` b"), "````")
        self.assertEqual(dw.fence_for("a `````` b ` c"), "`" * 7)
        self.assertEqual(dw.fence_for("x", minimum=5), "`````")

    def test_inline_code(self):
        self.assertEqual(dw.inline_code("a\nb"), "`a b`")
        self.assertEqual(dw.inline_code("a ` b"), "``a ` b``")
        self.assertEqual(dw.inline_code("`edge`"), "`` `edge` ``")
        self.assertEqual(dw.inline_code(""), "`(empty)`")
        self.assertLessEqual(len(dw.inline_code("z" * 5000)), 310)

    def test_fenced_diff_fence_longer_than_content_runs(self):
        lines = ["+ ```", "+ ````", "- ``````x"]
        block = dw.fenced_diff(lines)
        self.assertEqual(block[0], "`" * 7 + "diff")
        self.assertEqual(block[-1], "`" * 7)
        self.assertEqual(block[1:-1], lines)


class DigestRenderTests(TmpCase):
    def setUp(self):
        super().setUp()
        self.body = make_body()
        self.install(URL_A, self.body)
        self.install(URL_B, self.body)
        self.pages = {URL_A: entry(self.body), URL_B: entry(self.body, covers=("agents/y",)),
                      URL_RUBRIC: rubric_entry()}
        self.sources = self.sources2(self.pages, {"url": URL_CL, "last_reviewed_version": "2.1.295"})

    def report(self, live_a, live_b, **extra):
        mapping = {URL_A: live_a, URL_B: live_b, URL_RUBRIC: fetched(self.body),
                   URL_CL: fetched(FEED), **extra}
        with mock.patch.object(dw, "fetch", side_effect=fetch_map(mapping)):
            return dw.run_checks(self.sources, self.snaps, dw.DigestState(), TODAY,
                                 sleep=lambda s: None)

    def test_full_digest_sections_and_wording(self):
        major = self.body.replace("Paragraph 5 ", "Paragraph 55 ")
        minor = self.body.replace("behaviour", "behavoiur", 1)
        report = self.report(fetched(major), fetched(minor))
        state = dw.digest_state_after(report)
        text = dw.render_digest(report, self.sources, TODAY, state)
        lines = text.split("\n")
        self.assertTrue(lines[0].startswith("<!-- claudit doc-watch digest"))
        self.assertIn("## Doc drift digest: 2 page(s) to check, 1 with minor changes", text)
        self.assertIn("Generated: 2026-10-16. Diffs are against each page's verified snapshot", text)
        self.assertIn("third-party content", text)
        self.assertIn("### MAJOR (1): check before the next release", text)
        self.assertIn("`plugins/claudit/skills/x`", text)
        self.assertIn("- [ ] Re-verify dependents against the live page. Then run "
                      "`--rebaseline --only alpha` in the release PR.", text)
        self.assertIn("```diff", text)
        self.assertIn("+ Paragraph 55 describes the widget behaviour in plain words.", text)
        start, end = text.index("<details>"), text.index("</details>")
        self.assertTrue(start < text.index("### MINOR (1): no action needed unless you disagree") < end)
        self.assertIn("### Claude Code changelog", text)
        self.assertIn("2.1.300", text)
        self.assertIn("### No-baseline rubric pages", text)
        self.assertNotIn("### Unreachable or anomalous fetches", text)
        self.assertTrue(lines[-1].startswith("<!-- doc-watch-state "))
        self.assertEqual(dw.read_state(text), state)
        self.assertEqual(state.major, ("alpha", "changelog"))

    def test_all_clear_body(self):
        report = self.report(fetched(self.body), fetched(self.body))
        report.changelog = None
        state = dw.digest_state_after(report)
        text = dw.render_digest(report, self.sources, TODAY, state)
        self.assertEqual(text, f"{dw.DIGEST_HEADER}\nAll clear: no page needs a check.\n\n"
                               f"{dw.render_state(state)}")

    def test_unreachable_section(self):
        report = self.report(dw.FetchError("down"), fetched(self.body))
        text = dw.render_digest(report, self.sources, TODAY, dw.digest_state_after(report))
        self.assertIn("### Unreachable or anomalous fetches", text)
        self.assertIn("`alpha` UNREACHABLE", text)

    def test_hostile_page_text_cannot_leave_the_fence(self):
        hostile = ("::error::pwned <script>alert(1)</script> @octocat [x](http://evil) "
                   "``` and ```` inside")
        live = self.body.replace("Paragraph 5 describes the widget behaviour in plain words.",
                                 hostile)
        # also a hostile heading, which ends up in the reasons
        live = live + "\n## @octocat <img src=x> ``` heading\nfiller text here.\n"
        report = self.report(fetched(live), fetched(self.body))
        text = dw.render_digest(report, self.sources, TODAY, dw.digest_state_after(report))
        in_fence, fence = False, ""
        for line in text.split("\n"):
            if not in_fence:
                m = re.match(r"^(`{3,})diff$", line)
                if m:
                    in_fence, fence = True, m.group(1)
                    self.assertGreaterEqual(len(fence), 5)
                    continue
                for danger in ("<script>", "@octocat", "<img", "evil"):
                    if danger in line:
                        self.assertTrue(line.startswith("  - `") or line.startswith("- `"), line)
                        self.assertGreaterEqual(line.count("`"), 2)
                self.assertFalse(line.startswith("::"))
            elif line == fence:
                in_fence = False
        self.assertFalse(in_fence, "fence never closed")
        self.assertIn(hostile, text)

    def test_no_output_line_starts_with_workflow_command(self):
        live = self.body.replace("Paragraph 5 describes the widget behaviour in plain words.",
                                 "::set-output name=x::y")
        report = self.report(fetched(live), fetched(self.body))
        for text in (dw.render_text(report),
                     dw.render_digest(report, self.sources, TODAY, dw.digest_state_after(report))):
            for line in text.split("\n"):
                self.assertFalse(line.lstrip().startswith("::"), line)


class DigestTrimTests(unittest.TestCase):
    STATE = dw.DigestState("2026-10-16")

    def build(self, pages, changelog=None):
        report = synthetic_report(pages, changelog)
        return dw.build_digest_body(report, {}, TODAY, self.STATE)

    def test_small_digest_keeps_everything(self):
        pages = [synthetic_page("maj", dc.MAJOR, [long_hunk(3, True, "MAJORMARK", 20)]),
                 synthetic_page("min", dc.MINOR, [long_hunk(3, False, "MINORMARK", 20)])]
        body = self.build(pages)
        self.assertIn("MAJORMARK", body)
        self.assertIn("MINORMARK", body)

    def test_over_limit_drops_minor_detail_first(self):
        pages = [synthetic_page("maj", dc.MAJOR, [long_hunk(10, True, "MAJORMARK")])]
        pages += [synthetic_page(f"min{i}", dc.MINOR, [long_hunk(30, False, "MINORMARK")])
                  for i in range(4)]
        self.assertGreater(len(dw.render_digest(synthetic_report(pages), {}, TODAY, self.STATE)),
                           dc.DIGEST_BODY_MAX_CHARS)
        body = self.build(pages)
        self.assertLessEqual(len(body), dc.DIGEST_BODY_MAX_CHARS)
        self.assertNotIn("MINORMARK", body)
        self.assertIn("### MINOR (4)", body)
        self.assertIn("MAJORMARK old 9", body)       # MAJOR hunk kept whole (20 lines)
        self.assertIn("diff omitted for size", body)

    def test_then_major_hunks_are_shortened(self):
        pages = [synthetic_page(f"maj{i}", dc.MAJOR, [long_hunk(30, True, "MAJORMARK")])
                 for i in range(8)]
        body = self.build(pages)
        self.assertLessEqual(len(body), dc.DIGEST_BODY_MAX_CHARS)
        self.assertIn("MAJORMARK old 0", body)
        self.assertNotIn("MAJORMARK old 29", body)
        self.assertIn("### MAJOR (8)", body)
        self.assertEqual(dw.read_state(body), self.STATE)

    def test_extreme_digest_is_truncated_but_keeps_state_last(self):
        pages = [synthetic_page(f"page-{i}", dc.MAJOR, [long_hunk(1, True, "M", 10)],
                                covers=[f"skills/{'c' * 40}{j}" for j in range(8)])
                 for i in range(200)]
        body = self.build(pages)
        self.assertLessEqual(len(body), dc.DIGEST_BODY_MAX_CHARS)
        self.assertIn("digest truncated", body)
        self.assertEqual(dw.read_state(body), self.STATE)
        self.assertEqual(body.count("<details>"), body.count("</details>"))


# ---------------------------------------------------------------- mocked GitHub layer

class FakeGitHub:
    """A stateful stand-in for gh_call (repo o/r). Records every call as (method, path, body)."""

    def __init__(self, page_size=100):
        self.issues, self.calls = [], []
        self.page_size, self.next_number = page_size, 1
        self.fail_comment, self.label_exists = False, True

    def add_issue(self, title, body="", state="open", labels=("doc-drift",), **extra):
        issue = {"number": self.next_number, "title": title, "body": body, "state": state,
                 "labels": [{"name": n} for n in labels], **extra}
        self.next_number += 1
        self.issues.append(issue)
        return issue

    def by_title(self, title):
        return [i for i in self.issues if i["title"] == title]

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]

    def kinds(self):
        out = []
        for method, path, _ in self.writes():
            if path.endswith("/comments"):
                out.append("COMMENT")
            elif method == "POST" and path.endswith("/issues"):
                out.append("CREATE")
            elif method == "POST":
                out.append("LABEL")
            else:
                out.append("PATCH")
        return out

    def _http_error(self, path, code):
        return urllib.error.HTTPError("https://api.github.com" + path, code, "err", {},
                                      io.BytesIO(b""))

    def call(self, method, path, token, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        parts = urllib.parse.urlsplit(path)
        query = urllib.parse.parse_qs(parts.query)
        route = parts.path
        if method == "GET" and route == "/repos/o/r/issues":
            state = query.get("state", ["open"])[0]
            label = query.get("labels", [None])[0]
            items = [i for i in self.issues
                     if (state == "all" or i["state"] == state)
                     and (label is None or label in [x["name"] for x in i["labels"]])]
            page, size = int(query.get("page", ["1"])[0]), self.page_size
            headers = {}
            if page * size < len(items):
                nxt = {k: v[0] for k, v in query.items()}
                nxt["page"] = str(page + 1)
                headers = {"Link": f'<https://api.github.com{route}?{urllib.parse.urlencode(nxt)}>; rel="next"'}
            return copy.deepcopy(items[(page - 1) * size: page * size]), headers
        if route == "/repos/o/r/labels/doc-drift" and method == "GET":
            if not self.label_exists:
                raise self._http_error(path, 404)
            return {}, {}
        if route == "/repos/o/r/labels" and method == "POST":
            self.label_exists = True
            return {}, {}
        if route == "/repos/o/r/issues" and method == "POST":
            issue = self.add_issue(body["title"], body["body"], labels=body.get("labels", ()))
            return copy.deepcopy(issue), {}
        m = re.fullmatch(r"/repos/o/r/issues/(\d+)", route)
        if m and method == "PATCH":
            issue = next(i for i in self.issues if i["number"] == int(m.group(1)))
            issue.update(body)
            return copy.deepcopy(issue), {}
        m = re.fullmatch(r"/repos/o/r/issues/(\d+)/comments", route)
        if m and method == "POST":
            if self.fail_comment:
                raise self._http_error(path, 500)
            return {"id": 1}, {}
        raise AssertionError(f"unexpected call {method} {path}")


class GithubBase(TmpCase):
    def setUp(self):
        super().setUp()
        self.body = make_body()
        self.install(URL_A, self.body)
        self.install(URL_B, self.body)
        self.write_sources(self.sources2({URL_A: entry(self.body),
                                          URL_B: entry(self.body, covers=("agents/y",))}))
        self.fake = FakeGitHub()
        self.env = {"GITHUB_TOKEN": "SECRET-TOKEN-123", "GITHUB_REPOSITORY": "o/r"}
        self.fetch_calls = []

    def live(self, a="clean", b="clean"):
        variants = {"clean": self.body,
                    "major": self.body.replace("Paragraph 5 ", "Paragraph 55 "),
                    "major2": self.body.replace("Paragraph 5 ", "Paragraph 56 "),
                    "minor": self.body.replace("behaviour", "behavoiur", 1)}
        return {URL_A: fetched(variants[a]), URL_B: fetched(variants[b])}

    def run_gh(self, mapping, *flags, env=None, today=TODAY, fake=None):
        fake = fake or self.fake
        with mock.patch.object(dw, "gh_call", side_effect=fake.call), \
                mock.patch.object(dw, "fetch", side_effect=fetch_map(mapping, self.fetch_calls)), \
                mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            return run_main(self.argv(*(flags or ("--github",))),
                            env=self.env if env is None else env, today=today)

    def digest(self):
        found = self.fake.by_title(dw.DIGEST_TITLE)
        return found[0] if found else None



class GithubDigestTests(GithubBase):
    def test_nothing_changed_and_no_digest_means_no_writes(self):
        code, out, _ = self.run_gh(self.live())
        self.assertEqual(code, 0)
        self.assertEqual(self.fake.writes(), [])
        self.assertIn("NOTICE", out)

    def test_minor_only_creates_then_closes_without_comment(self):
        code, _, _ = self.run_gh(self.live("minor"))
        self.assertEqual((code, self.fake.kinds()), (0, ["CREATE", "PATCH"]))
        create, close = self.fake.writes()
        self.assertEqual(create[2]["title"], "doc-drift: digest")
        self.assertEqual(close[2], {"state": "closed"})
        issue = self.digest()
        self.assertEqual(issue["state"], "closed")
        self.assertIn("<details>", issue["body"])
        self.assertEqual([x["name"] for x in issue["labels"]], ["doc-drift"])
        self.assertEqual(dw.read_state(issue["body"]).pages["alpha"].minor_first_seen,
                         "2026-10-16")

    def test_minor_only_rerun_writes_nothing_and_stays_closed(self):
        self.run_gh(self.live("minor"))
        self.fake.calls.clear()
        code, _, _ = self.run_gh(self.live("minor"), today=TODAY + timedelta(days=1))
        self.assertEqual((code, self.fake.writes()), (0, []))
        self.assertEqual(self.digest()["state"], "closed")

    def test_later_major_reopens_a_minor_only_digest_and_comments(self):
        self.run_gh(self.live("minor"))
        self.fake.calls.clear()
        code, _, _ = self.run_gh(self.live("major"))
        self.assertEqual((code, self.fake.kinds()), (0, ["PATCH", "COMMENT", "PATCH"]))
        first, comment, last = self.fake.writes()
        self.assertEqual(first[2]["state"], "open")
        self.assertEqual(dw.read_state(first[2]["body"]).major, ())
        self.assertEqual(dw.read_state(last[2]["body"]).major, ("alpha",))
        self.assertEqual(self.digest()["state"], "open")

    def test_major_create_stays_open(self):
        self.run_gh(self.live("major", "minor"))
        self.assertEqual(self.fake.kinds(), ["CREATE"])
        self.assertEqual(self.digest()["state"], "open")

    def test_missing_label_is_created_before_the_issue(self):
        self.fake.label_exists = False
        code, _, _ = self.run_gh(self.live("major"))
        self.assertEqual((code, self.fake.kinds()), (0, ["LABEL", "CREATE"]))

    def test_one_major_creates_without_comment(self):
        code, out, _ = self.run_gh(self.live("major"))
        self.assertEqual((code, self.fake.kinds()), (0, ["CREATE"]))
        state = dw.read_state(self.digest()["body"])
        self.assertEqual(state.major, ("alpha",))

    def test_identical_rerun_writes_nothing_even_on_a_new_day(self):
        self.run_gh(self.live("major"))
        self.fake.calls.clear()
        code, _, _ = self.run_gh(self.live("major"), today=TODAY + timedelta(days=1))
        self.assertEqual((code, self.fake.writes()), (0, []))

    def test_same_major_set_patches_body_only(self):
        self.run_gh(self.live("major"))
        self.fake.calls.clear()
        code, _, _ = self.run_gh(self.live("major2"))
        self.assertEqual((code, self.fake.kinds()), (0, ["PATCH"]))
        self.assertEqual(set(self.fake.writes()[0][2]), {"body"})

    def test_new_major_page_patches_then_comments_then_records_state(self):
        self.run_gh(self.live("major"))
        self.fake.calls.clear()
        code, _, _ = self.run_gh(self.live("major", "major"))
        self.assertEqual((code, self.fake.kinds()), (0, ["PATCH", "COMMENT", "PATCH"]))
        first, comment, last = self.fake.writes()
        self.assertEqual(dw.read_state(first[2]["body"]).major, ("alpha",))
        self.assertEqual(comment[2]["body"], dw.render_comment(["beta"]))
        self.assertEqual(dw.read_state(last[2]["body"]).major, ("alpha", "beta"))
        self.assertEqual(dw.read_state(self.digest()["body"]).major, ("alpha", "beta"))

    def test_major_set_shrinking_patches_only(self):
        self.run_gh(self.live("major", "major"))
        self.fake.calls.clear()
        code, _, _ = self.run_gh(self.live("major", "clean"))
        self.assertEqual((code, self.fake.kinds()), (0, ["PATCH"]))
        self.assertEqual(self.digest()["state"], "open")

    def test_empties_patch_and_close_without_comment(self):
        self.run_gh(self.live("major"))
        self.fake.calls.clear()
        code, _, _ = self.run_gh(self.live())
        self.assertEqual((code, self.fake.kinds()), (0, ["PATCH"]))
        payload = self.fake.writes()[0][2]
        self.assertEqual(payload["state"], "closed")
        self.assertIn("All clear: no page needs a check.", payload["body"])
        self.assertEqual(self.digest()["state"], "closed")
        self.assertEqual(dw.read_state(self.digest()["body"]).major, ())

    def test_closed_digest_with_new_major_reopens_patches_and_comments(self):
        self.run_gh(self.live("major"))
        self.run_gh(self.live())
        self.fake.calls.clear()
        code, _, _ = self.run_gh(self.live("major"))
        self.assertEqual((code, self.fake.kinds()), (0, ["PATCH", "COMMENT", "PATCH"]))
        self.assertEqual(self.fake.writes()[0][2]["state"], "open")
        self.assertEqual(self.digest()["state"], "open")

    def test_closed_digest_stays_closed_when_nothing_major(self):
        self.run_gh(self.live("major"))
        self.run_gh(self.live())
        self.fake.calls.clear()
        code, _, _ = self.run_gh(self.live("minor"))
        self.assertEqual(self.digest()["state"], "closed")
        self.assertNotIn("COMMENT", self.fake.kinds())
        self.assertNotIn("state", self.fake.writes()[0][2])

    def test_comment_failure_exits_1_and_next_run_retries(self):
        self.run_gh(self.live("major"))
        self.fake.calls.clear()
        self.fake.fail_comment = True
        code, _, err = self.run_gh(self.live("major", "major"))
        self.assertEqual(code, 1)
        self.assertEqual(self.fake.kinds(), ["PATCH", "COMMENT"])
        self.assertIn("GitHub step failed", err)
        self.assertEqual(dw.read_state(self.digest()["body"]).major, ("alpha",))
        self.fake.calls.clear()
        self.fake.fail_comment = False
        code, _, _ = self.run_gh(self.live("major", "major"))
        self.assertEqual((code, self.fake.kinds()), (0, ["COMMENT", "PATCH"]))
        self.assertEqual(dw.read_state(self.digest()["body"]).major, ("alpha", "beta"))
        self.fake.calls.clear()
        code, _, _ = self.run_gh(self.live("major", "major"))
        self.assertEqual((code, self.fake.writes()), (0, []))

    def test_corrupt_state_exits_1_with_zero_writes_and_no_fetching(self):
        for body in ("garbage with no marker",
                     '<!-- doc-watch-state {"schema":1,"extra":1} -->',
                     dw.render_state(dw.DigestState()).replace('"schema":1', '"schema":9')):
            fake = FakeGitHub()
            fake.add_issue(dw.DIGEST_TITLE, body=body)
            self.fetch_calls.clear()
            code, _, err = self.run_gh(self.live("major"), fake=fake)
            self.assertEqual(code, 1, body)
            self.assertEqual(fake.writes(), [])
            self.assertEqual(self.fetch_calls, [])
            self.assertIn("state unreadable", err)

    def test_missing_credentials_exit_1_before_any_call(self):
        for env in ({}, {"GITHUB_TOKEN": "x"}, {"GITHUB_REPOSITORY": "o/r"}):
            code, _, err = self.run_gh(self.live(), env=env)
            self.assertEqual(code, 1)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.fetch_calls, [])

    def test_pagination_cap_exits_1_with_zero_writes(self):
        fake = FakeGitHub(page_size=1)
        for i in range(12):
            fake.add_issue(f"other {i}")
        code, _, err = self.run_gh(self.live("major"), fake=fake)
        self.assertEqual(code, 1)
        self.assertEqual(fake.writes(), [])
        self.assertIn("pages", err)

    def test_digest_found_on_a_later_page(self):
        fake = FakeGitHub(page_size=2)
        for i in range(5):
            fake.add_issue(f"other {i}")
        fake.add_issue(dw.DIGEST_TITLE, body=dw.render_state(dw.DigestState()), state="closed")
        code, _, _ = self.run_gh(self.live("major"), fake=fake)
        self.assertEqual(code, 0)
        self.assertEqual(len(fake.by_title(dw.DIGEST_TITLE)), 1)
        self.assertEqual(fake.kinds(), ["PATCH", "COMMENT", "PATCH"])

    def test_token_never_echoed(self):
        secret = self.env["GITHUB_TOKEN"]
        boom = OSError(f"failed while using {secret}")
        with mock.patch.object(dw, "gh_call", side_effect=boom), \
                mock.patch.object(dw, "fetch", side_effect=AssertionError):
            code, out, err = run_main(self.argv("--github"), env=self.env, today=TODAY)
        self.assertEqual(code, 1)
        self.assertNotIn(secret, out + err)
        self.assertIn("***", err)

    def test_all_unreachable_exits_1_and_creates_nothing_yet(self):
        down = dw.FetchError("down")
        code, _, _ = self.run_gh({URL_A: down, URL_B: down})
        self.assertEqual(code, 1)
        self.assertEqual(self.fake.writes(), [])    # nothing MAJOR or MINOR yet: no digest

    def test_gh_call_sends_bearer_token_and_returns_headers(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["req"] = req
            return FakeResp(b'{"ok": true}', headers={"Link": "x"})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            data, headers = dw.gh_call("GET", "/repos/o/r/issues", "TOK")
        self.assertEqual((data, headers), ({"ok": True}, {"Link": "x"}))
        self.assertEqual(captured["req"].full_url, "https://api.github.com/repos/o/r/issues")
        self.assertEqual(captured["req"].get_header("Authorization"), "Bearer TOK")


class PaginationTests(unittest.TestCase):
    PATH = "/repos/o/r/issues?state=all&labels=doc-drift&per_page=100"

    def test_follows_link_rel_next(self):
        fake = FakeGitHub(page_size=2)
        for i in range(5):
            fake.add_issue(f"i{i}")
        with mock.patch.object(dw, "gh_call", side_effect=fake.call):
            items = dw.gh_paginate(self.PATH, "t")
        self.assertEqual([i["title"] for i in items], [f"i{n}" for n in range(5)])
        self.assertEqual(len(fake.calls), 3)

    def test_fails_loudly_over_the_cap(self):
        fake = FakeGitHub(page_size=1)
        for i in range(5):
            fake.add_issue(f"i{i}")
        with mock.patch.object(dw, "gh_call", side_effect=fake.call):
            with self.assertRaises(dw.GitHubError):
                dw.gh_paginate(self.PATH, "t", max_pages=3)
            self.assertEqual(len(dw.gh_paginate(self.PATH, "t", max_pages=5)), 5)

    def test_refuses_offsite_next_link(self):
        reply = ([], {"Link": '<https://evil.example/x?page=2>; rel="next"'})
        with mock.patch.object(dw, "gh_call", return_value=reply):
            with self.assertRaises(dw.GitHubError):
                dw.gh_paginate(self.PATH, "t")

    def test_non_list_response_rejected(self):
        with mock.patch.object(dw, "gh_call", return_value=({"message": "x"}, {})):
            with self.assertRaises(dw.GitHubError):
                dw.gh_paginate(self.PATH, "t")

    def test_find_digest_exact_title_oldest_and_not_prs(self):
        fake = FakeGitHub()
        fake.add_issue("doc-drift: digest (shadow)")
        fake.add_issue(dw.DIGEST_TITLE, pull_request={"url": "x"})
        second = fake.add_issue(dw.DIGEST_TITLE, state="closed")
        fake.add_issue(dw.DIGEST_TITLE)
        with mock.patch.object(dw, "gh_call", side_effect=fake.call):
            self.assertEqual(dw.find_digest("o/r", "t")["number"], second["number"])
            self.assertIsNone(dw.find_digest("o/r", "t", "doc-drift: nothing"))
        self.assertIn("state=all", fake.calls[0][1])


class ShadowTests(GithubBase):
    def setUp(self):
        super().setUp()
        self.summary = self.dir / "summary.md"
        self.env["GITHUB_STEP_SUMMARY"] = str(self.summary)

    def shadow(self, mapping, **kw):
        return self.run_gh(mapping, "--shadow", **kw)

    def test_shadow_creates_silent_issue_and_writes_summary(self):
        code, out, _ = self.shadow(self.live("major"))
        self.assertEqual((code, self.fake.kinds()), (0, ["CREATE"]))
        self.assertEqual(self.fake.by_title(dw.DIGEST_TITLE), [])
        shadow = self.fake.by_title(dw.SHADOW_TITLE)[0]
        self.assertEqual(dw.read_state(shadow["body"]).major, ("alpha",))
        text = self.summary.read_text(encoding="utf-8")
        self.assertIn("## Doc drift digest: 1 page(s) to check", text)
        self.assertNotIn("## Doc drift digest", out)

    def test_shadow_never_comments_closes_or_reopens(self):
        self.shadow(self.live("major"))
        for a, b in (("major", "major"), ("clean", "clean"), ("major", "clean"), ("minor", "minor")):
            self.fake.calls.clear()
            code, _, _ = self.shadow(self.live(a, b))
            self.assertEqual(code, 0)
            self.assertNotIn("COMMENT", self.fake.kinds())
            for _, _, payload in self.fake.writes():
                self.assertEqual(set(payload), {"body"})
            self.assertEqual(self.fake.by_title(dw.SHADOW_TITLE)[0]["state"], "open")

    def test_shadow_state_is_independent_of_the_real_digest(self):
        real = self.fake.add_issue(dw.DIGEST_TITLE, body="garbage that would fail to parse")
        code, _, _ = self.shadow(self.live("major"))
        self.assertEqual(code, 0)
        self.assertEqual(real["body"], "garbage that would fail to parse")
        for _, path, _ in self.fake.writes():
            self.assertNotIn(f"/issues/{real['number']}", path)

    def test_shadow_prints_digest_to_stdout_without_summary_env(self):
        env = dict(self.env)
        del env["GITHUB_STEP_SUMMARY"]
        code, out, _ = self.shadow(self.live("major"), env=env)
        self.assertEqual(code, 0)
        self.assertIn("## Doc drift digest: 1 page(s) to check", out)
        self.assertFalse(self.summary.exists())

    def test_shadow_corrupt_state_exits_1_with_zero_writes(self):
        self.fake.add_issue(dw.SHADOW_TITLE, body="garbage")
        code, _, _ = self.shadow(self.live("major"))
        self.assertEqual((code, self.fake.writes()), (1, []))

    def test_shadow_no_output_line_starts_with_workflow_command(self):
        hostile = self.body.replace("Paragraph 5 describes the widget behaviour in plain words.",
                                    "::error::pwned")
        env = dict(self.env)
        del env["GITHUB_STEP_SUMMARY"]
        code, out, err = self.shadow({URL_A: fetched(hostile), URL_B: fetched(self.body)}, env=env)
        for line in (out + err).split("\n"):
            self.assertFalse(line.lstrip().startswith("::"), line)


class CloseLegacyTests(GithubBase):
    def test_closes_only_per_page_issues_with_pointer_comment(self):
        f = self.fake
        alpha = f.add_issue("doc-drift: alpha")
        beta = f.add_issue("doc-drift: beta")
        clog = f.add_issue("doc-drift: claude-code changelog")
        digest = f.add_issue(dw.DIGEST_TITLE, body=dw.render_state(dw.DigestState()))
        shadow = f.add_issue(dw.SHADOW_TITLE, body="shadow")
        unrelated = f.add_issue("Something else entirely", labels=())
        labelled_other = f.add_issue("doc-drift: not-a-configured-page")
        pr = f.add_issue("doc-drift: alpha", pull_request={"url": "x"})
        already = f.add_issue("doc-drift: alpha", state="closed")
        code, out, _ = self.run_gh({}, "--close-legacy")
        self.assertEqual(code, 0)
        expected = [alpha["number"], beta["number"], clog["number"]]
        self.assertEqual(f.kinds(), ["COMMENT", "PATCH"] * 3)
        touched = [int(re.search(r"/issues/(\d+)", p).group(1)) for _, p, _ in f.writes()]
        self.assertEqual(touched, [n for n in expected for _ in (0, 1)])
        for method, path, payload in f.writes():
            if path.endswith("/comments"):
                self.assertEqual(payload["body"], dw.LEGACY_CLOSE_NOTE)
            else:
                self.assertEqual(payload, {"state": "closed"})
        for issue in (digest, shadow, unrelated, labelled_other, pr, already):
            self.assertNotIn(issue["number"], touched)
        self.assertEqual(self.fetch_calls, [])
        self.assertEqual(self.digest()["state"], "open")

    def test_note_text_matches_spec(self):
        self.assertEqual(dw.LEGACY_CLOSE_NOTE,
                         'Superseded by the rolling digest "doc-drift: digest". This per-page '
                         'issue is closed. Its page is tracked there.')

    def test_nothing_to_close(self):
        code, out, _ = self.run_gh({}, "--close-legacy")
        self.assertEqual((code, self.fake.writes()), (0, []))
        self.assertIn("no open legacy issues", out)

    def test_failure_exits_1(self):
        self.fake.add_issue("doc-drift: alpha")
        self.fake.fail_comment = True
        code, _, err = self.run_gh({}, "--close-legacy")
        self.assertEqual(code, 1)
        self.assertEqual(self.fake.kinds(), ["COMMENT"])
        self.assertNotIn("SECRET-TOKEN-123", err)


if __name__ == "__main__":
    unittest.main()
