"""Tests for the snapshot checks in maintainer/check_repo.py. Temp trees only; no network."""

import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import check_repo as cr  # noqa: E402
import doc_classify as dc  # noqa: E402
import doc_watch as dw  # noqa: E402

URL_A = "https://code.claude.com/docs/en/alpha.md"
URL_B = "https://code.claude.com/docs/en/beta.md"
URL_RUBRIC = "https://platform.claude.com/docs/en/rubric.md"
VERIFIED = "2026-10-15"


def make_body(tag="alpha"):
    lines = ["# Sample page"] + [f"{tag} paragraph {i} about widgets." for i in range(30)]
    return dc.normalise("\n".join(lines) + "\n")


class TreeCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        skill = self.root / "plugins" / "claudit" / "skills" / "alpha"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\ndescription: x\n---\n", encoding="utf-8")
        agents = self.root / "plugins" / "claudit" / "agents"
        agents.mkdir(parents=True)
        (agents / "beta.md").write_text("---\nname: beta\ndescription: x\n---\n", encoding="utf-8")
        self.snaps = self.root / "maintainer" / "snapshots"
        self.body = make_body()
        self.pages = {}

    def add_page(self, url, body=None, *, covers=("skills/alpha",), verified=VERIFIED,
                 snapshot=True, snapshot_verified=None):
        body = self.body if body is None else body
        if snapshot:
            dw.write_snapshot(dw.snapshot_path(self.snaps, url), url,
                              snapshot_verified or verified, body)
        self.pages[url] = {"sha256": dc.sha256(body), "verified": verified,
                           "covers": list(covers)}

    def add_rubric(self):
        self.pages[URL_RUBRIC] = {"sha256": None, "verified": None, "covers": []}

    def sources(self):
        return {"schema": 2, "normaliser_version": 2, "_comment": "c",
                "pages": copy.deepcopy(self.pages),
                "changelog": {"url": "https://raw.githubusercontent.com/o/r/main/C.md",
                              "last_reviewed_version": "1.0.0"}}

    def problems(self, sources=None):
        return cr.snapshot_problems(sources or self.sources(), self.root)

    def assertProblem(self, fragment, sources=None):
        found = self.problems(sources)
        self.assertTrue(any(fragment in p for p in found), (fragment, found))
        return found


class GoodTreeTests(TreeCase):
    def test_good_tree_passes(self):
        self.add_page(URL_A, covers=("skills/alpha", "agents/beta"))
        self.add_page(URL_B, make_body("beta"), covers=("agents/beta",))
        self.add_rubric()
        self.assertEqual(self.problems(), [])

    def test_rubric_page_with_null_sha_and_no_covers_passes(self):
        self.add_rubric()
        self.assertEqual(self.problems(), [])

    def test_no_snapshot_directory_is_fine_without_baselined_pages(self):
        self.add_rubric()
        self.assertFalse(self.snaps.exists())
        self.assertEqual(self.problems(), [])

    def test_check_snapshots_reports_through_err(self):
        self.add_page(URL_A)
        (self.snaps / "alpha.md").unlink()
        cr.errors.clear()
        try:
            cr.check_snapshots(self.sources())
            self.assertTrue(any("missing snapshot" in e for e in cr.errors))
        finally:
            cr.errors.clear()


class FailureTests(TreeCase):
    def test_hash_mismatch_fails(self):
        self.add_page(URL_A)
        self.pages[URL_A]["sha256"] = dc.sha256("something else\n")
        self.assertProblem("does not match sources.json")

    def test_covers_with_null_sha_fails(self):
        self.pages[URL_A] = {"sha256": None, "verified": None, "covers": ["skills/alpha"]}
        self.assertProblem("no baseline")

    def test_orphan_snapshot_fails(self):
        self.add_page(URL_A)
        dw.write_snapshot(self.snaps / "orphan.md", URL_B, VERIFIED, self.body)
        self.assertProblem("orphan.md: orphan snapshot")

    def test_snapshot_of_page_without_baseline_is_orphan(self):
        self.add_rubric()
        dw.write_snapshot(self.snaps / "rubric.md", URL_RUBRIC, VERIFIED, self.body)
        self.assertProblem("rubric.md: orphan snapshot")

    def test_stray_non_markdown_file_in_snapshots_fails(self):
        self.add_page(URL_A)
        (self.snaps / "notes.txt").write_text("x", encoding="utf-8")
        self.assertProblem("notes.txt: orphan snapshot")

    def test_header_verified_mismatch_fails(self):
        self.add_page(URL_A, snapshot_verified="2026-10-14")
        self.assertProblem("differs from sources.json verified")

    def test_header_source_mismatch_fails(self):
        self.add_page(URL_A)
        dw.write_snapshot(self.snaps / "alpha.md", URL_B, VERIFIED, self.body)
        self.assertProblem("header source")

    def test_unnormalised_body_fails_even_when_hash_matches(self):
        bad = "# Sample page\nwords   with  extra   spaces " + "x" * 600 + "\n"
        self.assertNotEqual(dc.normalise(bad), bad)
        path = dw.snapshot_path(self.snaps, URL_A)
        path.parent.mkdir(parents=True)
        path.write_bytes((dw.snapshot_header(URL_A, VERIFIED) + "\n" + bad).encode("utf-8"))
        self.pages[URL_A] = {"sha256": dc.sha256(bad), "verified": VERIFIED,
                             "covers": ["skills/alpha"]}
        found = self.assertProblem("not normalised")
        self.assertFalse(any("does not match sources.json" in p for p in found))

    def test_slug_changelog_fails(self):
        url = "https://code.claude.com/docs/en/changelog.md"
        self.add_page(url)
        self.assertProblem("may not be 'changelog'")

    def test_unresolved_covers_fail(self):
        for cover in ("skills/missing", "agents/missing", "skills", "skills/../../x",
                      "/etc/passwd", "skills\\alpha", "skills/alpha/SKILL.md"):
            self.pages.clear()
            self.add_page(URL_A, covers=(cover,))
            with self.subTest(cover):
                self.assertProblem("does not resolve")

    def test_missing_snapshot_fails(self):
        self.add_page(URL_A)
        (self.snaps / "alpha.md").unlink()
        self.assertProblem("missing snapshot")

    def test_unparsable_header_fails(self):
        self.add_page(URL_A)
        (self.snaps / "alpha.md").write_bytes(b"no header\nbody\n")
        self.assertProblem("bad snapshot header")

    def test_schema_1_sources_fail_with_clear_message(self):
        self.add_page(URL_A)
        old = self.sources()
        del old["schema"], old["normaliser_version"]
        found = self.assertProblem("not schema 2", old)
        self.assertEqual(len(found), 1)

    def test_wrong_normaliser_version_fails(self):
        self.add_page(URL_A)
        data = self.sources()
        data["normaliser_version"] = 1
        self.assertProblem("normaliser_version", data)

    def test_duplicate_slugs_fail(self):
        self.add_page(URL_A)
        self.pages["https://platform.claude.com/docs/en/alpha.md"] = {
            "sha256": None, "verified": None, "covers": []}
        self.assertProblem("duplicate slug")

    def test_threshold_key_in_sources_fails(self):
        self.add_page(URL_A)
        data = self.sources()
        data["SECTION_MAJOR_RATIO"] = 0.99
        self.assertProblem("unknown top-level keys", data)


class CoverResolutionTests(TreeCase):
    def test_skill_and_agent_layouts_resolve(self):
        plugin = self.root / "plugins" / "claudit"
        self.assertTrue(cr.cover_resolves("skills/alpha", plugin))
        self.assertTrue(cr.cover_resolves("agents/beta", plugin))
        self.assertFalse(cr.cover_resolves("agents/beta.md", plugin))
        self.assertFalse(cr.cover_resolves("skills/nope", plugin))
        self.assertFalse(cr.cover_resolves(None, plugin))


if __name__ == "__main__":
    unittest.main()
