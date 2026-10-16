"""Tests for maintainer/doc_classify.py. Synthetic fixtures only; no network, no files."""

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import doc_classify as dc  # noqa: E402

TODAY = date(2026, 10, 16)

NAMES5 = ["alpha", "bravo", "charlie", "delta", "echo"]
NAMES20 = NAMES5 + [
    "foxtrot", "golf", "hotel", "india", "juliet", "kilo", "lima", "mike",
    "november", "oscar", "papa", "quebec", "romeo", "sierra", "tango",
]
ORDINALS = ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth"]
FILLER = "explains widget behaviour in plain words."


def build(names=NAMES5, overrides=None, extras=None):
    """A normalised page: per name a heading plus 9 digit-free, modal-free body lines.

    overrides: {(name, k): line} replaces body line k (1..9); extras: {name: [lines]} appends.
    """
    overrides = overrides or {}
    extras = extras or {}
    out = []
    for name in names:
        out.append(f"## Section {name}")
        for k, word in enumerate(ORDINALS, start=1):
            out.append(overrides.get((name, k), f"{name.capitalize()} {word} {FILLER}"))
        out.extend(extras.get(name, []))
    page = "\n".join(out) + "\n"
    assert dc.normalise(page) == page, "fixture must already be normalised"
    return page


def outcome_for(text, **kw):
    data = text.encode("utf-8")
    kw.setdefault("declared_length", len(data))
    kw.setdefault("received", len(data))
    return dc.FetchOutcome(text=text, **kw)


def reasons_of(old, new):
    diff = dc.diff_page(old, new)
    flat = []
    for hunk in diff.hunks:
        flat.extend(hunk.reasons)
    return diff, flat


def evaluate(snapshot, live, state=None, covers=("skills/x",), **kw):
    return dc.evaluate_page(snapshot, covers, outcome_for(live, **kw),
                            state or dc.PageState(), TODAY)


class NormaliserTests(unittest.TestCase):
    def test_variants_hash_equal(self):
        base = "# Title\n\nSome text here.\n\nMore text.\n"
        variants = [
            base.replace("\n", "\r\n"),
            base.replace("\n", "\r"),
            base.replace(" ", " "),
            base.replace("text", "te​xt").replace("More", "﻿More"),
            "# Title   \n\n\n\n\nSome   text \t here.  \n\nMore text.\n\n\n",
            "\n\n" + base,
        ]
        want = dc.sha256(dc.normalise(base))
        for variant in variants:
            self.assertEqual(dc.sha256(dc.normalise(variant)), want, repr(variant))

    def test_nfc(self):
        self.assertEqual(dc.normalise("café\n"), dc.normalise("café\n"))

    def test_single_trailing_newline(self):
        self.assertEqual(dc.normalise("a\n\n\n"), "a\n")
        self.assertEqual(dc.normalise("a"), "a\n")

    def test_fenced_indentation_changes_hash(self):
        a = "```\ncode\n```\n"
        b = "```\n  code\n```\n"
        self.assertNotEqual(dc.sha256(dc.normalise(a)), dc.sha256(dc.normalise(b)))
        self.assertIn("  code", dc.normalise(b))

    def test_fence_trailing_space_stripped_but_blank_runs_kept(self):
        out = dc.normalise("```\na   \n\n\nb\n```\n")
        self.assertEqual(out, "```\na\n\n\nb\n```\n")

    def test_outside_fence_whitespace_collapsed(self):
        self.assertEqual(dc.normalise("a    b\n\n\n\nc\n"), "a b\n\nc\n")

    def test_idempotent(self):
        samples = [
            "a  b\r\n\r\n\r\n```\n  x  \n\n\n  y\n```\n\ttext\n",
            "    ```\nnot a fence once stripped\n",
            " ```\ncode\n",
            "```\nunclosed\n\n\n",
            "",
        ]
        for sample in samples:
            once = dc.normalise(sample)
            self.assertEqual(dc.normalise(once), once, repr(sample))

    def test_normalise_v1_matches_old_behaviour(self):
        self.assertEqual(dc.normalise_v1("x \r\n\n\ny\n\n"), "x\n\ny\n")
        # v1 does not touch inline whitespace or NBSP, v2 does.
        self.assertEqual(dc.normalise_v1("a  b c\n"), "a  b c\n")
        self.assertEqual(dc.normalise("a  b c\n"), "a b c\n")

    def test_sha256_is_utf8(self):
        self.assertEqual(dc.sha256("abc"),
                         "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        self.assertEqual(dc.sha256("é"), dc.sha256("é".encode("utf-8").decode("utf-8")))

    def test_slug(self):
        self.assertEqual(dc.slug("https://code.claude.com/docs/en/sub-agents.md"), "sub-agents")


class FenceAndSectionTests(unittest.TestCase):
    def test_fence_flags(self):
        self.assertEqual(dc.fence_flags(["a", "```py", "b", "```", "c"]),
                         [False, True, True, True, False])

    def test_unclosed_fence_flags_to_eof(self):
        self.assertEqual(dc.fence_flags(["a", "```", "b", "c"]), [False, True, True, True])

    def test_longer_fence_needs_matching_close(self):
        self.assertEqual(dc.fence_flags(["````", "```", "x", "````", "y"]),
                         [True, True, True, True, False])
        self.assertEqual(dc.fence_flags(["~~~", "```", "~~~", "y"]),
                         [True, True, True, False])

    def test_comment_inside_fence_does_not_split_section(self):
        lines = ["# Title", "```bash", "# comment", "echo hi", "```", "tail"]
        sections = dc.split_sections(lines)
        self.assertEqual([s.title for s in sections], ["", "# Title"])
        self.assertEqual(sections[1].lines, lines)

    def test_heading_after_unclosed_fence_does_not_split(self):
        sections = dc.split_sections(["# A", "```", "## B", "text"])
        self.assertEqual([s.title for s in sections], ["", "# A"])

    def test_repeated_titles_get_occurrence_numbers(self):
        sections = dc.split_sections(["intro", "## A", "x", "## B", "## A", "y"])
        self.assertEqual([s.key for s in sections],
                         [("", 1), ("## A", 1), ("## B", 1), ("## A", 2)])
        self.assertEqual(sections[0].lines, ["intro"])

    def test_five_hashes_is_not_a_boundary(self):
        self.assertEqual(len(dc.split_sections(["# A", "##### deep", "x"])), 2)


class ClassifierTests(unittest.TestCase):
    def assert_minor(self, old, new):
        diff, flat = reasons_of(old, new)
        self.assertTrue(diff.hunks, "expected at least one hunk")
        self.assertEqual(flat, [])
        self.assertEqual(diff.major_hunks, [])

    def assert_major(self, old, new, reason):
        diff, flat = reasons_of(old, new)
        self.assertTrue(diff.major_hunks)
        self.assertIn(reason, flat)

    def one_line(self, text, k=3, name="bravo"):
        return build(overrides={(name, k): text})

    def test_identical_pages_have_no_hunks(self):
        page = build()
        self.assertEqual(dc.diff_page(page, page).hunks, [])

    def test_whitespace_variants_make_no_hunk(self):
        page = build()
        variant = page.replace("\n", "\r\n").replace(" widget", "  widget  ")
        variant = variant.replace("plain", "pl​ain")
        live = dc.normalise(variant)
        self.assertEqual(live, page)
        self.assertEqual(dc.diff_page(page, live).hunks, [])
        self.assertEqual(dc.sha256(live), dc.sha256(page))

    def test_typo_is_minor(self):
        old = build()
        new = self.one_line(f"Bravo third {FILLER}".replace("behaviour", "behavoiur"))
        self.assert_minor(old, new)

    def test_rewording_is_minor(self):
        old = build()
        new = self.one_line("Bravo third describes widget conduct in simple terms.")
        self.assert_minor(old, new)

    def test_link_text_change_is_minor(self):
        old = self.one_line("See [the guide](https://example.com/a) for details.")
        new = self.one_line("See [the manual](https://example.com/a) for details.")
        self.assert_minor(old, new)

    def test_link_target_change_is_major(self):
        old = self.one_line("See [the guide](https://example.com/a) for details.")
        new = self.one_line("See [the guide](https://example.com/b) for details.")
        self.assert_major(old, new, dc.REASON_LINK)

    def test_relative_link_target_change_is_major(self):
        old = self.one_line("See [the guide](./a.md) for details.")
        new = self.one_line("See [the guide](./b.md) for details.")
        self.assert_major(old, new, dc.REASON_LINK)

    def test_reference_link_target_change_is_major(self):
        old = self.one_line("[guide]: https://example.com/a")
        new = self.one_line("[guide]: https://example.com/b")
        self.assert_major(old, new, dc.REASON_LINK)

    def test_heading_removed_is_major(self):
        old = build()
        new = build(names=NAMES5[:-1])
        self.assert_major(old, new, dc.REASON_HEADING_REMOVED)

    def test_heading_added_is_major(self):
        old = build(names=NAMES5[:-1])
        new = build()
        self.assert_major(old, new, dc.REASON_HEADING_ADDED)

    def test_heading_typo_is_major(self):
        old = build()
        new = old.replace("## Section charlie", "## Section charlei")
        diff, flat = reasons_of(old, new)
        self.assertIn(dc.REASON_HEADING_REMOVED, flat)
        self.assertIn(dc.REASON_HEADING_ADDED, flat)

    def test_table_row_is_major(self):
        old = self.one_line("| name | Display name of the thing |")
        new = self.one_line("| name | Lowercase identifier of the thing |")
        self.assert_major(old, new, dc.REASON_TABLE)

    def test_code_fence_edit_is_major(self):
        old = build(extras={"delta": ["```bash", "run tool", "```"]})
        new = build(extras={"delta": ["```bash", "run tool now", "```"]})
        self.assert_major(old, new, dc.REASON_FENCE)

    def test_code_fence_indentation_edit_is_major(self):
        old = build(extras={"delta": ["```bash", "run tool", "```"]})
        new = build(extras={"delta": ["```bash", "  run tool", "```"]})
        self.assert_major(old, new, dc.REASON_FENCE)

    def test_modal_can_to_cannot_is_major(self):
        old = self.one_line("The agent can read files.")
        new = self.one_line("The agent cannot read files.")
        self.assert_major(old, new, dc.REASON_MODAL)

    def test_modal_can_to_cant_is_major(self):
        old = self.one_line("The agent can read files.")
        new = self.one_line("The agent can't read files.")
        self.assert_major(old, new, dc.REASON_MODAL)

    def test_modal_cant_to_cannot_is_major(self):
        old = self.one_line("The agent can't read files.")
        new = self.one_line("The agent cannot read files.")
        self.assert_major(old, new, dc.REASON_MODAL)

    def test_modal_typographic_apostrophe_equals_ascii(self):
        old = self.one_line("The agent can't read files.")
        new = self.one_line("The agent can’t read files.")
        self.assertEqual(dc.diff_page(old, new).major_hunks, [])

    def test_modal_does_not_to_doesnt_is_major(self):
        # Chosen: counted as a change ("not" vs "doesn't"), so MAJOR. Conservative.
        old = self.one_line("The tool does not read files.")
        new = self.one_line("The tool doesn't read files.")
        self.assert_major(old, new, dc.REASON_MODAL)

    def test_modal_no_longer_matches_whole(self):
        self.assertEqual(dc.MODAL_RE.findall("It is no longer set; cannot, can't, can."),
                         ["no longer", "cannot", "can't", "can"])

    def test_unchanged_cant_text_stays_clean(self):
        page = self.one_line("The agent can't read files.")
        self.assertEqual(dc.diff_page(page, page).hunks, [])
        self.assertEqual(evaluate(page, page).status, dc.CLEAN)

    def test_code_token_added_is_major(self):
        old = self.one_line("Fields are `name` and `description`.")
        new = self.one_line("Fields are `name` and `description` and `extra`.")
        self.assert_major(old, new, dc.REASON_CODE_TOKEN)

    def test_exit_code_number_is_major(self):
        old = self.one_line("Exit 2 blocks the call.")
        new = self.one_line("Exit 1 blocks the call.")
        self.assert_major(old, new, dc.REASON_NUMBER)

    def test_version_is_major(self):
        old = self.one_line("Needs v2.1.294+ to run.")
        new = self.one_line("Needs v2.1.295+ to run.")
        self.assert_major(old, new, dc.REASON_VERSION)

    def test_section_over_forty_percent_rewritten_is_major(self):
        # 5 changed of a 10-line section (heading + 9) is 50% > 40%; words only, so no
        # other rule fires.
        repl = {("alpha", k): f"Completely different prose {w} here." for k, w in
                [(1, "aa"), (2, "bb"), (3, "cc"), (4, "dd"), (5, "ee")]}
        old = build()
        new = build(overrides=repl)
        diff, flat = reasons_of(old, new)
        self.assertIn(dc.REASON_SECTION, flat)
        self.assertEqual(set(flat), {dc.REASON_SECTION})

    def test_section_at_exactly_forty_percent_is_not_rewritten(self):
        # 4 of 10 body lines in a 10-line section (heading + 9): 4/10 = 0.40, not > 0.40.
        repl = {("alpha", k): f"Completely different prose {w} here." for k, w in
                [(1, "aa"), (2, "bb"), (3, "cc"), (4, "dd")]}
        diff, flat = reasons_of(build(), build(overrides=repl))
        self.assertNotIn(dc.REASON_SECTION, flat)

    # ---- the six synthetic v0.2.2 corrections

    def test_v022_permission_rules_gains_not(self):
        old = self.one_line("Network paths are blocked by the rule.")
        new = self.one_line("Network paths are not blocked by the rule.")
        self.assert_major(old, new, dc.REASON_MODAL)

    def test_v022_skill_builder_name_table_row(self):
        old = self.one_line("| `name` | Optional display name |")
        new = self.one_line("| `name` | Lowercase letters, numbers and hyphens |")
        self.assert_major(old, new, dc.REASON_TABLE)

    def test_v022_plugin_builder_gains_autocompactwindow(self):
        old = self.one_line("Settings fields are `model` and `theme`.")
        new = self.one_line("Settings fields are `model`, `theme` and `autoCompactWindow`.")
        self.assert_major(old, new, dc.REASON_CODE_TOKEN)

    def test_v022_plugin_builder_distribution_link_target(self):
        old = self.one_line("Distribute via the [marketplace guide](https://example.com/old).")
        new = self.one_line("Distribute via the [marketplace guide](https://example.com/new).")
        self.assert_major(old, new, dc.REASON_LINK)

    def test_v022_config_reviewer_exit_2_added(self):
        old = self.one_line("Hooks report failures.")
        new = self.one_line("Hooks report failures. Exit 2 blocks the call.")
        self.assert_major(old, new, dc.REASON_NUMBER)

    def test_v022_onfailure_block_added(self):
        old = self.one_line("Hooks report failures.")
        new = self.one_line('Hooks report failures. `onFailure: "block"` (v2.1.295+) is set.')
        diff, flat = reasons_of(old, new)
        self.assertIn(dc.REASON_CODE_TOKEN, flat)
        self.assertIn(dc.REASON_VERSION, flat)


class EvaluatePageTests(unittest.TestCase):
    def typo_pages(self, count, lines_each):
        """Snapshot and live: `count` of 20 ten-line sections get `lines_each` typo'd lines."""
        old = build(names=NAMES20)
        overrides = {}
        for name in NAMES20[:count]:
            for k in range(2, 2 + lines_each):
                overrides[(name, k)] = (
                    f"{name.capitalize()} {ORDINALS[k - 1]} {FILLER}".replace("behaviour", "behavoiur"))
        return old, build(names=NAMES20, overrides=overrides)

    def test_clean(self):
        page = build()
        result = evaluate(page, page, dc.PageState(minor_first_seen="2026-09-01"))
        self.assertEqual(result.status, dc.CLEAN)
        self.assertIsNone(result.state.minor_first_seen)

    def test_twenty_minors_of_two_lines_escalates(self):
        old, new = self.typo_pages(20, 2)
        self.assertEqual(len(old.splitlines()), 200)
        result = evaluate(old, new)
        self.assertEqual(result.status, dc.MAJOR)
        self.assertEqual(result.diff.major_hunks, [])
        self.assertEqual(result.diff.minor_changed, 40)

    def test_twenty_minors_of_one_line_stays_minor(self):
        old, new = self.typo_pages(20, 1)
        result = evaluate(old, new)
        self.assertEqual(result.status, dc.MINOR)
        self.assertEqual(result.diff.minor_changed, 20)
        self.assertEqual(result.state.minor_first_seen, TODAY.isoformat())

    def test_minor_age_thirty_days_is_minor(self):
        old, new = self.typo_pages(1, 1)
        state = dc.PageState(minor_first_seen=(TODAY - timedelta(days=30)).isoformat())
        self.assertEqual(evaluate(old, new, state).status, dc.MINOR)

    def test_minor_age_thirty_one_days_is_major(self):
        old, new = self.typo_pages(1, 1)
        state = dc.PageState(minor_first_seen=(TODAY - timedelta(days=31)).isoformat())
        result = evaluate(old, new, state)
        self.assertEqual(result.status, dc.MAJOR)
        self.assertTrue(any("31 days" in r for r in result.reasons))

    def test_major_hunk_makes_page_major(self):
        old = build()
        new = build(overrides={("bravo", 2): "The agent cannot read files."})
        self.assertEqual(evaluate(old, new).status, dc.MAJOR)

    def test_no_baseline_with_covers_is_major(self):
        result = evaluate(None, build(), covers=("skills/x",))
        self.assertEqual((result.status, result.reasons), (dc.MAJOR, ["no baseline"]))

    def test_rubric_page_without_baseline_is_no_baseline(self):
        self.assertEqual(evaluate(None, build(), covers=()).status, dc.NO_BASELINE)

    # ---- fail-safe

    def test_empty_body_is_major(self):
        for text in ("", "   \n\n"):
            result = evaluate(build(), text)
            self.assertEqual(result.status, dc.MAJOR)
            self.assertIn(dc.ANOM_EMPTY, result.anomalies)

    def test_html_error_page_is_major(self):
        text = "<!DOCTYPE html><html><body>" + "Service unavailable. " * 60 + "</body></html>"
        result = evaluate(build(), text)
        self.assertEqual(result.status, dc.MAJOR)
        self.assertIn(dc.ANOM_HTML, result.anomalies)

    def test_html_sniff_only_looks_at_start(self):
        text = build() + "<html> appears later\n"
        self.assertFalse(dc.looks_like_html(dc.normalise(text)))

    def test_truncated_by_declared_length(self):
        page = build()
        result = dc.evaluate_page(page, ("x",), dc.FetchOutcome(
            text=page, declared_length=len(page) + 1000, received=len(page)),
            dc.PageState(), TODAY)
        self.assertEqual(result.status, dc.MAJOR)
        self.assertIn(dc.ANOM_TRUNCATED, result.anomalies)

    def test_truncated_by_read_error(self):
        page = build()
        result = dc.evaluate_page(page, ("x",), dc.FetchOutcome(text=page, read_error=True),
                                  dc.PageState(), TODAY)
        self.assertEqual(result.status, dc.MAJOR)
        self.assertIn(dc.ANOM_TRUNCATED, result.anomalies)

    def test_complete_download_is_not_truncated(self):
        page = build()
        self.assertEqual(dc.detect_anomalies(outcome_for(page), page, page), [])

    def test_last_heading_missing_and_shorter_is_truncated(self):
        old = build(names=NAMES20)
        live = build(names=NAMES20[:17])          # 15% shorter, last heading gone
        self.assertIn(dc.ANOM_TRUNCATED, dc.detect_anomalies(outcome_for(live), live, old))

    def test_last_heading_missing_but_barely_shorter_is_not_truncated_anomaly(self):
        old = build(names=NAMES20)
        live = build(names=NAMES20[:19])          # 5% shorter
        self.assertNotIn(dc.ANOM_TRUNCATED, dc.detect_anomalies(outcome_for(live), live, old))

    def test_shrink_to_sixty_percent_is_anomaly(self):
        old = build(names=NAMES5)
        live = build(names=NAMES5[:3])
        self.assertTrue(0.5 < len(live) / len(old) < 0.7)
        result = evaluate(old, live)
        self.assertEqual(result.status, dc.MAJOR)
        self.assertIn(dc.ANOM_SHRANK, result.anomalies)

    def test_shrink_within_threshold_is_not_anomaly(self):
        old = build(names=NAMES20)
        live = build(names=NAMES20[:15])          # 25% shorter
        self.assertNotIn(dc.ANOM_SHRANK, dc.detect_anomalies(outcome_for(live), live, old))

    def test_404_and_410_are_immediate_major(self):
        for status in (404, 410):
            result = dc.evaluate_page(build(), ("x",), dc.FetchOutcome(text=None, status=status),
                                      dc.PageState(), TODAY)
            self.assertEqual(result.status, dc.MAJOR)
            self.assertEqual(result.reasons, [dc.ANOM_GONE])

    def test_classifier_exception_is_major_for_that_page_only(self):
        page = build()
        with mock.patch.object(dc, "diff_page", side_effect=RuntimeError("boom")):
            broken = evaluate(page, build(overrides={("bravo", 2): "changed words here."}))
        self.assertEqual(broken.status, dc.MAJOR)
        self.assertEqual(broken.reasons, ["classifier error: RuntimeError"])
        self.assertEqual(evaluate(page, page).status, dc.CLEAN)

    def test_bad_state_date_is_major_not_raised(self):
        result = evaluate(build(), build(), dc.PageState(unreachable_since="garbage"))
        self.assertIsInstance(result, dc.PageResult)  # CLEAN path ignores it; no exception
        result = dc.evaluate_page(build(), ("x",), dc.FetchOutcome(unreachable=True),
                                  dc.PageState(unreachable_since="garbage"), TODAY)
        self.assertEqual(result.status, dc.MAJOR)
        self.assertEqual(result.reasons, ["classifier error: ValueError"])

    # ---- unreachable

    def unreachable(self, since):
        return dc.evaluate_page(build(), ("x",), dc.FetchOutcome(unreachable=True, error="boom"),
                                dc.PageState(unreachable_since=since), TODAY)

    def test_first_unreachable_sets_since_today(self):
        result = self.unreachable(None)
        self.assertEqual(result.status, dc.UNREACHABLE)
        self.assertEqual(result.state.unreachable_since, TODAY.isoformat())

    def test_unreachable_two_days_is_not_major(self):
        result = self.unreachable((TODAY - timedelta(days=2)).isoformat())
        self.assertEqual(result.status, dc.UNREACHABLE)

    def test_unreachable_three_days_is_major(self):
        result = self.unreachable((TODAY - timedelta(days=3)).isoformat())
        self.assertEqual(result.status, dc.MAJOR)
        self.assertEqual(result.reasons, ["unreachable 3 days"])
        self.assertEqual(result.state.unreachable_since, (TODAY - timedelta(days=3)).isoformat())

    def test_success_clears_unreachable(self):
        page = build()
        result = evaluate(page, page, dc.PageState(unreachable_since="2026-10-01"))
        self.assertIsNone(result.state.unreachable_since)

    def test_unreachable_does_not_touch_minor_timer(self):
        result = dc.evaluate_page(build(), ("x",), dc.FetchOutcome(unreachable=True),
                                  dc.PageState(minor_first_seen="2026-10-01"), TODAY)
        self.assertEqual(result.state.minor_first_seen, "2026-10-01")


class ChangelogTests(unittest.TestCase):
    FEED = "# Changelog\n" + "".join(f"## 2.1.{n}\n- change\n" for n in range(311, 295, -1))

    def run_cl(self, text, last="2.1.309", state=None, **kw):
        return dc.evaluate_changelog(dc.FetchOutcome(text=text, **kw), last,
                                     state or dc.ChangelogState(), TODAY)

    def test_new_versions_are_major(self):
        result = self.run_cl(self.FEED)
        self.assertEqual(result.status, dc.MAJOR)
        self.assertEqual(result.new_versions, ["2.1.311", "2.1.310"])
        self.assertEqual(result.reasons[0], "new Claude Code versions: 2.1.311, 2.1.310")
        self.assertEqual(result.state.first_seen, TODAY.isoformat())

    def test_first_seen_is_kept_while_pending(self):
        state = dc.ChangelogState(("2.1.310",), "2026-10-14")
        self.assertEqual(self.run_cl(self.FEED, state=state).state.first_seen, "2026-10-14")

    def test_up_to_date_is_clean_and_clears_state(self):
        state = dc.ChangelogState(("2.1.310",), "2026-10-14")
        result = self.run_cl(self.FEED, last="2.1.311", state=state)
        self.assertEqual(result.status, dc.CLEAN)
        self.assertEqual(result.state, dc.ChangelogState())

    def test_last_reviewed_missing_from_feed_uses_first_ten(self):
        result = self.run_cl(self.FEED, last="2.0.1")
        self.assertEqual(result.status, dc.MAJOR)
        self.assertEqual(len(result.new_versions), 10)
        self.assertEqual(result.new_versions[0], "2.1.311")

    def test_missing_last_reviewed_is_major_no_baseline(self):
        result = self.run_cl(self.FEED, last=None)
        self.assertEqual((result.status, result.reasons), (dc.MAJOR, ["no baseline"]))

    def test_unreachable_changelog_is_unreachable(self):
        result = dc.evaluate_changelog(dc.FetchOutcome(unreachable=True), "2.1.309",
                                       dc.ChangelogState(), TODAY)
        self.assertEqual(result.status, dc.UNREACHABLE)

    def test_empty_feed_is_major(self):
        self.assertEqual(self.run_cl("").status, dc.MAJOR)

    def test_truncated_feed_is_major(self):
        result = self.run_cl(self.FEED, read_error=True)
        self.assertEqual(result.status, dc.MAJOR)
        self.assertIn(dc.ANOM_TRUNCATED, result.reasons)

    def test_changelog_404_is_major(self):
        self.assertEqual(self.run_cl(None, status=404).reasons, [dc.ANOM_GONE])

    def test_changelog_versions_parsing(self):
        self.assertEqual(dc.changelog_versions("## [v1.2.3]\n## 4.5.6\n### 7.8.9\n"),
                         ["1.2.3", "4.5.6"])


if __name__ == "__main__":
    unittest.main()
