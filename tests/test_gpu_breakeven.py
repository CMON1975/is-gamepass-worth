"""Characterization + regression tests for gpu_breakeven.

Pins behavior of pure helpers. Network-touching code (ITAD, HLTB) is not
covered here — we rely on smoke-running the script for that.
"""

import argparse
import datetime as dt
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import gpu_breakeven as gb


class ResolveRegionTests(unittest.TestCase):
    def _ns(self, **kw):
        return argparse.Namespace(
            region=kw.get("region", "CA"),
            currency=kw.get("currency"),
            gpu_price=kw.get("gpu_price"),
        )

    def test_ca_default_uses_preset_and_warns_unverified(self):
        currency, price, unverified = gb.resolve_region(self._ns(region="CA"))
        self.assertEqual(currency, "CAD")
        self.assertEqual(price, 26.99)
        self.assertTrue(unverified)

    def test_us_default_does_not_warn(self):
        currency, price, unverified = gb.resolve_region(self._ns(region="US"))
        self.assertEqual(currency, "USD")
        self.assertEqual(price, 22.99)
        self.assertFalse(unverified)

    def test_explicit_gpu_price_suppresses_unverified_warning(self):
        _, _, unverified = gb.resolve_region(self._ns(region="GB", gpu_price=15.0))
        self.assertFalse(unverified)

    def test_unknown_region_without_overrides_exits(self):
        with self.assertRaises(SystemExit):
            gb.resolve_region(self._ns(region="JP"))

    def test_unknown_region_with_overrides_works(self):
        currency, price, unverified = gb.resolve_region(
            self._ns(region="JP", currency="JPY", gpu_price=1100.0)
        )
        self.assertEqual(currency, "JPY")
        self.assertEqual(price, 1100.0)
        self.assertFalse(unverified)

    def test_lowercase_region_is_normalized(self):
        currency, _, _ = gb.resolve_region(self._ns(region="ca"))
        self.assertEqual(currency, "CAD")


class ReadTitlesTests(unittest.TestCase):
    def test_strips_blanks_and_comments(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("# header\n\nAvowed\n  Hi-Fi Rush  \n# trailing\nPentiment\n")
            path = f.name
        try:
            self.assertEqual(
                gb.read_titles(path),
                ["Avowed", "Hi-Fi Rush", "Pentiment"],
            )
        finally:
            pathlib.Path(path).unlink()


class CacheFreshTests(unittest.TestCase):
    def test_none_is_not_fresh(self):
        self.assertFalse(gb.cache_fresh(None))

    def test_garbage_is_not_fresh(self):
        self.assertFalse(gb.cache_fresh("not-a-timestamp"))

    def test_recent_is_fresh(self):
        ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        self.assertTrue(gb.cache_fresh(ts))

    def test_old_is_not_fresh(self):
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30))
        self.assertFalse(gb.cache_fresh(old.isoformat(timespec="seconds")))


class RenderMarkdownTests(unittest.TestCase):
    def _rows(self):
        return [
            {"title": "Avowed", "price": 79.99, "hours": 30.0},
            {"title": "MysteryGame", "price": None, "hours": None},
        ]

    def test_table_header_and_rows(self):
        out = gb.render_markdown(
            self._rows(), "CAD", total_cost=79.99, total_hours=30.0,
            gpu_price=26.99, hours_per_week=8.0, unresolved=[],
        )
        self.assertIn("| Title | Hist. Low (CAD) | Hours | $/hr |", out)
        self.assertIn("| Avowed | 79.99 | 30.0 | 2.67 |", out)
        # Missing data renders as em-dash.
        self.assertIn("| MysteryGame | — | — | — |", out)

    def test_summary_includes_verdict(self):
        out = gb.render_markdown(
            self._rows(), "CAD", total_cost=79.99, total_hours=30.0,
            gpu_price=26.99, hours_per_week=8.0, unresolved=[],
        )
        self.assertIn("Total buyout: **79.99 CAD**", out)
        self.assertRegex(out, r"Verdict: \*\*(subscribe|buy)\*\*")

    def test_unresolved_section_appears_only_when_needed(self):
        without = gb.render_markdown(
            self._rows(), "CAD", 0, 0, 26.99, 8.0, unresolved=[]
        )
        with_misses = gb.render_markdown(
            self._rows(), "CAD", 0, 0, 26.99, 8.0,
            unresolved=["Bad Title"],
        )
        self.assertNotIn("Unresolved", without)
        self.assertIn("## Unresolved titles", with_misses)
        self.assertIn("- Bad Title", with_misses)


class HltbMissingSectionTests(unittest.TestCase):
    """New behavior: report should call out HLTB misses separately from ITAD misses."""

    def test_hltb_missing_section_when_provided(self):
        rows = [{"title": "Foo", "price": 10.0, "hours": None}]
        out = gb.render_markdown(
            rows, "CAD", total_cost=10.0, total_hours=0.0,
            gpu_price=26.99, hours_per_week=8.0, unresolved=[],
            hltb_missing=["Foo"],
        )
        self.assertIn("## Hours not found (HLTB)", out)
        self.assertIn("- Foo", out)

    def test_no_hltb_section_when_empty(self):
        rows = [{"title": "Foo", "price": 10.0, "hours": 5.0}]
        out = gb.render_markdown(
            rows, "CAD", total_cost=10.0, total_hours=5.0,
            gpu_price=26.99, hours_per_week=8.0, unresolved=[],
            hltb_missing=[],
        )
        self.assertNotIn("Hours not found", out)

    def test_hltb_missing_defaults_to_empty(self):
        # Backwards-compat: callers that don't pass hltb_missing still work.
        rows = [{"title": "Foo", "price": 10.0, "hours": 5.0}]
        out = gb.render_markdown(
            rows, "CAD", total_cost=10.0, total_hours=5.0,
            gpu_price=26.99, hours_per_week=8.0, unresolved=[],
        )
        self.assertNotIn("Hours not found", out)


class ExtractEffectivePriceTests(unittest.TestCase):
    """Waterfall: history low → lowest current deal price → lowest regular/MSRP."""

    def test_uses_history_low_when_present(self):
        entry = {
            "historyLow": {"all": {"amount": 45.99, "currency": "CAD"}},
            "deals": [{"price": {"amount": 99.99, "currency": "CAD"}}],
        }
        self.assertEqual(gb._extract_effective_price(entry),
                         (45.99, "CAD", "hist_low"))

    def test_falls_back_to_lowest_current_deal_when_no_history(self):
        entry = {
            "historyLow": {"all": None},
            "deals": [
                {"price": {"amount": 69.99, "currency": "CAD"},
                 "regular": {"amount": 79.99, "currency": "CAD"}},
                {"price": {"amount": 59.99, "currency": "CAD"},
                 "regular": {"amount": 79.99, "currency": "CAD"}},
            ],
        }
        self.assertEqual(gb._extract_effective_price(entry),
                         (59.99, "CAD", "current"))

    def test_falls_back_to_msrp_when_no_deal_prices(self):
        entry = {
            "historyLow": {"all": None},
            "deals": [
                {"regular": {"amount": 79.99, "currency": "CAD"}},
                {"regular": {"amount": 89.99, "currency": "CAD"}},
            ],
        }
        self.assertEqual(gb._extract_effective_price(entry),
                         (79.99, "CAD", "msrp"))

    def test_returns_none_when_nothing_available(self):
        entry = {"historyLow": {"all": None}, "deals": []}
        self.assertIsNone(gb._extract_effective_price(entry))

    def test_handles_missing_history_low_key(self):
        entry = {"deals": [{"price": {"amount": 50.0, "currency": "CAD"}}]}
        self.assertEqual(gb._extract_effective_price(entry),
                         (50.0, "CAD", "current"))


class FallbackAnnotationTests(unittest.TestCase):
    """Renderers must mark fallback prices with * and add a footnote when used."""

    def _row(self, source):
        return {"title": "Foo", "price": 49.99, "hours": 10.0, "price_source": source}

    def test_terminal_marks_fallback_price(self):
        out = gb.render_terminal(
            rows=[self._row("current")], currency="CAD",
            total_cost=49.99, total_hours=10.0, gpu_price=26.99,
            hours_per_week=8.0, unresolved=[], hltb_missing=[],
            substitutions=[], width=100,
        )
        self.assertRegex(out, r"49\.99\s*\*")
        self.assertIn("current", out.lower())  # footnote mentions current

    def test_terminal_no_footnote_when_all_hist_low(self):
        out = gb.render_terminal(
            rows=[self._row("hist_low")], currency="CAD",
            total_cost=49.99, total_hours=10.0, gpu_price=26.99,
            hours_per_week=8.0, unresolved=[], hltb_missing=[],
            substitutions=[], width=100,
        )
        self.assertNotRegex(out, r"49\.99\s*\*")

    def test_markdown_marks_fallback_price(self):
        out = gb.render_markdown(
            [self._row("msrp")], "CAD",
            total_cost=49.99, total_hours=10.0, gpu_price=26.99,
            hours_per_week=8.0, unresolved=[],
        )
        self.assertRegex(out, r"49\.99\s*\*")

    def test_markdown_no_marker_when_all_hist_low(self):
        out = gb.render_markdown(
            [self._row("hist_low")], "CAD",
            total_cost=49.99, total_hours=10.0, gpu_price=26.99,
            hours_per_week=8.0, unresolved=[],
        )
        self.assertNotRegex(out, r"49\.99\s*\*")


class SubstitutionsSectionTests(unittest.TestCase):
    def test_section_present_when_provided(self):
        out = gb.render_markdown(
            [], "CAD", 0, 0, 26.99, 8.0, unresolved=[],
            substitutions=[("avowed", "Unavowed"),
                           ("Little nightmares ii", "Little Nightmares III")],
        )
        self.assertIn("## Title substitutions", out)
        self.assertIn("- avowed → Unavowed", out)
        self.assertIn("- Little nightmares ii → Little Nightmares III", out)

    def test_section_absent_when_empty(self):
        out = gb.render_markdown(
            [], "CAD", 0, 0, 26.99, 8.0, unresolved=[], substitutions=[],
        )
        self.assertNotIn("## Title substitutions", out)

    def test_section_absent_when_omitted(self):
        out = gb.render_markdown([], "CAD", 0, 0, 26.99, 8.0, unresolved=[])
        self.assertNotIn("## Title substitutions", out)


class RenderTerminalTests(unittest.TestCase):
    def _rows(self):
        return [
            {"title": "Avowed", "price": 79.99, "hours": 30.0},
            {"title": "Hi-Fi Rush", "price": 39.99, "hours": 14.1},
        ]

    def _call(self, **overrides):
        kw = dict(
            rows=self._rows(),
            currency="CAD",
            total_cost=119.98,
            total_hours=44.1,
            gpu_price=26.99,
            hours_per_week=8.0,
            unresolved=[],
            hltb_missing=[],
            substitutions=[],
            width=100,
        )
        kw.update(overrides)
        return gb.render_terminal(**kw)

    def test_table_shows_titles_and_prices(self):
        out = self._call()
        self.assertIn("Avowed", out)
        self.assertIn("79.99", out)
        self.assertIn("Hi-Fi Rush", out)
        self.assertIn("39.99", out)

    def test_table_has_column_headers(self):
        out = self._call()
        self.assertIn("Title", out)
        self.assertIn("Price", out)
        self.assertIn("Hours", out)
        # $/hr column appears
        self.assertRegex(out, r"\$/hr|\$ ?/ ?hr")

    def test_total_row_present(self):
        out = self._call()
        self.assertIn("Total", out)
        self.assertIn("119.98", out)

    def test_summary_block_includes_verdict(self):
        out = self._call()
        self.assertIn("Summary", out)
        self.assertIn("Verdict", out)
        self.assertRegex(out, r"\b(SUBSCRIBE|BUY)\b")

    def test_issues_block_hidden_when_clean(self):
        out = self._call()
        self.assertNotIn("Issues", out)
        self.assertNotIn("Unresolved", out)

    def test_issues_block_shown_when_unresolved(self):
        out = self._call(unresolved=["bad title"])
        self.assertIn("Issues", out)
        self.assertIn("bad title", out)

    def test_issues_block_shown_when_substitutions(self):
        out = self._call(substitutions=[("avowed", "Unavowed")])
        self.assertIn("Issues", out)
        self.assertIn("avowed", out)
        self.assertIn("Unavowed", out)
        self.assertIn("→", out)

    def test_issues_block_shown_when_hltb_missing(self):
        out = self._call(hltb_missing=["Mystery Game"])
        self.assertIn("Issues", out)
        self.assertIn("Mystery Game", out)

    def test_long_titles_get_truncated_to_fit_width(self):
        long_rows = [
            {"title": "X" * 200, "price": 1.0, "hours": 1.0},
        ]
        out = gb.render_terminal(
            rows=long_rows, currency="CAD", total_cost=1.0, total_hours=1.0,
            gpu_price=26.99, hours_per_week=8.0,
            unresolved=[], hltb_missing=[], substitutions=[],
            width=80,
        )
        # Table data lines (those starting with the indent) shouldn't exceed width.
        for line in out.splitlines():
            if line.lstrip().startswith("X"):
                self.assertLessEqual(len(line), 80)

    def test_missing_data_renders_as_dash(self):
        rows = [{"title": "Mystery", "price": None, "hours": None}]
        out = gb.render_terminal(
            rows=rows, currency="CAD", total_cost=0.0, total_hours=0.0,
            gpu_price=26.99, hours_per_week=8.0,
            unresolved=[], hltb_missing=["Mystery"], substitutions=[],
            width=80,
        )
        self.assertIn("Mystery", out)
        self.assertIn("—", out)


class HltbLookupTests(unittest.TestCase):
    """hltb_lookup must return both hours and canonical name; falls back to main_story."""

    def _result(self, **kw):
        r = mock.Mock()
        r.similarity = kw.get("similarity", 0.9)
        r.main_extra = kw.get("main_extra", 0)
        r.main_story = kw.get("main_story", 0)
        r.game_name = kw.get("game_name", "")
        return r

    def test_returns_hours_and_canonical_name(self):
        fake = self._result(main_extra=15.5, game_name="Avowed")
        with mock.patch.object(gb, "HowLongToBeat") as cls:
            cls.return_value.search.return_value = [fake]
            out = gb.hltb_lookup("avowed")
        self.assertEqual(out, {"hours": 15.5, "name": "Avowed"})

    def test_falls_back_to_main_story(self):
        fake = self._result(main_extra=0, main_story=10.0, game_name="Pentiment")
        with mock.patch.object(gb, "HowLongToBeat") as cls:
            cls.return_value.search.return_value = [fake]
            out = gb.hltb_lookup("Pentiment")
        self.assertEqual(out["hours"], 10.0)

    def test_picks_highest_similarity_match(self):
        worse = self._result(similarity=0.4, main_extra=5.0, game_name="Wrong")
        better = self._result(similarity=0.95, main_extra=20.0, game_name="Correct")
        with mock.patch.object(gb, "HowLongToBeat") as cls:
            cls.return_value.search.return_value = [worse, better]
            out = gb.hltb_lookup("anything")
        self.assertEqual(out["name"], "Correct")

    def test_empty_results_returns_none(self):
        with mock.patch.object(gb, "HowLongToBeat") as cls:
            cls.return_value.search.return_value = []
            self.assertIsNone(gb.hltb_lookup("nonsense title"))

    def test_zero_hours_returns_none(self):
        fake = self._result(main_extra=0, main_story=0, game_name="X")
        with mock.patch.object(gb, "HowLongToBeat") as cls:
            cls.return_value.search.return_value = [fake]
            self.assertIsNone(gb.hltb_lookup("X"))

    def test_exception_returns_none(self):
        with mock.patch.object(gb, "HowLongToBeat") as cls:
            cls.return_value.search.side_effect = RuntimeError("boom")
            self.assertIsNone(gb.hltb_lookup("X"))


class RenderBarTests(unittest.TestCase):
    def test_zero_progress(self):
        bar = gb.render_bar(0, 10, width=10)
        self.assertEqual(bar.count("█"), 0)
        self.assertEqual(bar.count("░"), 10)

    def test_full_progress(self):
        bar = gb.render_bar(10, 10, width=10)
        self.assertEqual(bar.count("█"), 10)
        self.assertEqual(bar.count("░"), 0)

    def test_partial_progress(self):
        bar = gb.render_bar(3, 10, width=10)
        self.assertEqual(bar.count("█"), 3)
        self.assertEqual(bar.count("░"), 7)

    def test_zero_total_safe(self):
        bar = gb.render_bar(0, 0, width=10)
        self.assertIn("░", bar)


if __name__ == "__main__":
    unittest.main()
