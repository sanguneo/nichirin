"""Layout and theme tests.

Korean text is double-width and wrapped in zero-width escapes, so every
assertion here is about columns rather than characters. Getting this wrong
does not crash; it shifts every column on screen by a few cells, which is
exactly the kind of bug a test has to catch instead of an eye.
"""

from __future__ import annotations

import unittest

from kamado.tui.layout import (
    ELLIPSIS,
    Segment,
    display_width,
    elide_head,
    pad_to_width,
    plan_footer,
    strip_ansi,
    truncate_to_width,
    wrap_to_width,
)
from kamado.tui.theme import TEXT, SpeakerPalette, Theme, to_256

RED = "\x1b[38;2;204;102;102m"
OFF = "\x1b[39m"


class WidthTests(unittest.TestCase):
    def test_hangul_counts_as_two_columns(self) -> None:
        self.assertEqual(display_width("가나다"), 6)

    def test_latin_counts_as_one_column(self) -> None:
        self.assertEqual(display_width("abc"), 3)

    def test_mixed_text_adds_up(self) -> None:
        self.assertEqual(display_width("가a나b"), 6)

    def test_escape_sequences_occupy_nothing(self) -> None:
        self.assertEqual(display_width(f"{RED}가나{OFF}"), 4)

    def test_osc8_hyperlinks_occupy_no_columns(self) -> None:
        link = "\x1b]8;;https://example.com\x1b\\링크\x1b]8;;\x1b\\"
        self.assertEqual(display_width(link), 4)
        self.assertEqual(strip_ansi(link), "링크")

    def test_stripping_escapes_leaves_the_text(self) -> None:
        self.assertEqual(strip_ansi(f"{RED}hello{OFF}"), "hello")


class TruncateTests(unittest.TestCase):
    def test_text_that_fits_is_untouched(self) -> None:
        self.assertEqual(truncate_to_width("가나다", 6), "가나다")

    def test_a_wide_character_is_never_split_across_the_boundary(self) -> None:
        cut = truncate_to_width("가나다", 5)
        self.assertLessEqual(display_width(cut), 5)
        self.assertEqual(cut, "가나")

    def test_the_marker_is_counted_inside_the_budget(self) -> None:
        cut = truncate_to_width("가나다라", 6, ELLIPSIS)
        self.assertLessEqual(display_width(cut), 6)
        self.assertTrue(cut.endswith(ELLIPSIS))

    def test_colour_survives_truncation(self) -> None:
        cut = truncate_to_width(f"{RED}가나다라{OFF}", 4)
        self.assertIn(RED, cut)
        self.assertEqual(display_width(cut), 4)

    def test_zero_width_returns_nothing(self) -> None:
        self.assertEqual(truncate_to_width("가나", 0), "")


class PaddingTests(unittest.TestCase):
    def test_padding_measures_in_columns_not_characters(self) -> None:
        self.assertEqual(display_width(pad_to_width("가나", 10)), 10)

    def test_padding_never_shortens(self) -> None:
        self.assertEqual(pad_to_width("가나다", 2), "가나다")


class ElisionTests(unittest.TestCase):
    def test_the_tail_is_what_survives(self) -> None:
        self.assertEqual(elide_head("abcdefgh", 5), f"{ELLIPSIS}efgh")

    def test_text_that_fits_is_untouched(self) -> None:
        self.assertEqual(elide_head("abc", 5), "abc")

    def test_a_single_column_becomes_the_marker(self) -> None:
        self.assertEqual(elide_head("abcdef", 1), ELLIPSIS)


class WrapTests(unittest.TestCase):
    def test_every_wrapped_line_fits(self) -> None:
        text = "가나다라마바사아자차카타파하 " * 4
        for line in wrap_to_width(text, 20):
            self.assertLessEqual(display_width(line), 20)

    def test_a_word_longer_than_the_line_is_broken_rather_than_overflowing(self) -> None:
        for line in wrap_to_width("가" * 40, 10):
            self.assertLessEqual(display_width(line), 10)

    def test_newlines_start_new_lines(self) -> None:
        self.assertEqual(len(wrap_to_width("a\nb\nc", 40)), 3)

    def test_wrapping_keeps_every_character(self) -> None:
        text = "가나다 라마바 사아자"
        joined = "".join(wrap_to_width(text, 8)).replace(" ", "")
        self.assertEqual(joined, text.replace(" ", ""))


class FooterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.anchor = [Segment("room-title", "room-title")]
        self.middle = [Segment("400 loaded", "400 loaded"), Segment("12 lines", "12 lines")]
        self.tail = Segment("transcript", "transcript")
        self.right = Segment("v0.1.0", "v0.1.0")

    def plan(self, width: int):
        return plan_footer(self.anchor, self.middle, self.tail, self.right, width)

    def test_a_wide_footer_keeps_every_segment(self) -> None:
        plan = self.plan(120)
        self.assertEqual(plan.kept_middle, len(self.middle))
        self.assertFalse(plan.show_marker)

    def test_a_narrower_footer_drops_middle_segments_from_the_right(self) -> None:
        plan = self.plan(48)
        self.assertLess(plan.kept_middle, len(self.middle))
        self.assertTrue(plan.show_marker)

    def test_a_narrow_footer_drops_the_middle_entirely(self) -> None:
        plan = self.plan(34)
        self.assertEqual(plan.kept_middle, 0)

    def test_a_very_narrow_footer_shortens_the_anchor_head_last(self) -> None:
        plan = self.plan(26)
        anchor = plan.anchor_plain
        assert anchor is not None, "the anchor was dropped entirely"
        self.assertTrue(anchor.startswith(ELLIPSIS))

    def test_the_right_label_is_never_dropped_while_anything_fits(self) -> None:
        for width in (120, 60, 40, 30):
            with self.subTest(width=width):
                self.assertIsNone(self.plan(width).right_plain)

    def test_an_anchor_is_required(self) -> None:
        with self.assertRaises(ValueError):
            plan_footer([], self.middle, self.tail, self.right, 80)


class ThemeTests(unittest.TestCase):
    def test_disabled_theme_emits_no_escapes(self) -> None:
        theme = Theme(truecolor=True, enabled=False)
        self.assertEqual(theme.fg("accent", "text"), "text")
        self.assertEqual(theme.bg("selectedBg", "text"), "text")
        self.assertEqual(theme.bold("text"), "text")

    def test_truecolor_uses_24_bit_sequences(self) -> None:
        theme = Theme(truecolor=True, enabled=True)
        self.assertIn("38;2;", theme.fg("accent", "x"))

    def test_without_truecolor_it_falls_back_to_the_256_palette(self) -> None:
        theme = Theme(truecolor=False, enabled=True)
        self.assertIn("38;5;", theme.fg("accent", "x"))

    def test_colouring_never_changes_the_visible_width(self) -> None:
        theme = Theme(truecolor=True, enabled=True)
        self.assertEqual(display_width(theme.fg("accent", "가나다")), 6)

    def test_a_background_survives_coloured_runs_inside_it(self) -> None:
        """The selected row is a filled bar containing its own foreground runs.

        A foreground reset must not punch a hole in the fill, or the highlight
        breaks apart exactly where the row has content.
        """
        theme = Theme(truecolor=True, enabled=True)
        inner = theme.fg("accent", "› ") + theme.fg("warning", "room") + "tail"
        filled = theme.bg("selectedBg", inner)

        self.assertTrue(filled.startswith("\x1b[48;2;58;58;74m"))
        self.assertTrue(filled.endswith("\x1b[49m"))
        self.assertEqual(filled.count("\x1b[49m"), 1, "the fill must close exactly once")
        self.assertEqual(strip_ansi(filled), "› roomtail")

    def test_a_background_reopens_after_an_inner_background_reset(self) -> None:
        theme = Theme(truecolor=True, enabled=True)
        nested = theme.bg("customMessageBg", "a")
        filled = theme.bg("selectedBg", nested)
        self.assertEqual(strip_ansi(filled), "a")
        self.assertTrue(filled.endswith("\x1b[49m"))
        self.assertNotIn("\x1b[49ma", filled, "the fill must not end before its content")

    def test_a_neutral_grey_maps_onto_the_grey_ramp(self) -> None:
        self.assertGreaterEqual(to_256("#808080"), 232)

    def test_a_tinted_colour_keeps_its_tint_in_256_colour(self) -> None:
        self.assertLess(to_256("#00d7ff"), 232)


class SpeakerPaletteTests(unittest.TestCase):
    """Subtle was the requirement; indistinguishable would be a failure."""

    @staticmethod
    def _channels(colour: str) -> list[int]:
        return [int(colour[index : index + 2], 16) for index in (1, 3, 5)]

    @classmethod
    def _spread(cls, colour: str) -> int:
        channels = cls._channels(colour)
        return max(channels) - min(channels)

    @classmethod
    def _luminance(cls, colour: str) -> int:
        red, green, blue = cls._channels(colour)
        return round(0.299 * red + 0.587 * green + 0.114 * blue)

    def test_a_speaker_keeps_the_same_tint_across_lookups(self) -> None:
        palette = SpeakerPalette()
        self.assertEqual(palette.name_colour("나"), palette.name_colour("나"))

    def test_message_bodies_stay_within_a_whisper_of_the_base_colour(self) -> None:
        palette = SpeakerPalette()
        base = self._luminance(TEXT)
        for index in range(40):
            colour = palette.body_colour(f"speaker{index}")
            with self.subTest(speaker=index):
                self.assertLessEqual(self._spread(colour), 12, "body tint must stay subtle")
                self.assertLessEqual(abs(self._luminance(colour) - base), 6)

    def test_small_rooms_give_every_speaker_a_distinct_name_tint(self) -> None:
        palette = SpeakerPalette()
        speakers = [f"speaker{index}" for index in range(12)]
        self.assertEqual(palette.distinct_names(speakers), len(speakers))

    def test_a_crowded_room_still_separates_almost_every_speaker(self) -> None:
        palette = SpeakerPalette()
        speakers = [f"speaker{index}" for index in range(32)]
        self.assertGreaterEqual(palette.distinct_names(speakers), 30)

    def test_name_tints_are_stronger_than_body_tints(self) -> None:
        palette = SpeakerPalette()
        for index in range(8):
            speaker = f"speaker{index}"
            with self.subTest(speaker=speaker):
                self.assertGreater(
                    self._spread(palette.name_colour(speaker)),
                    self._spread(palette.body_colour(speaker)),
                )

    def test_no_speaker_is_ever_given_a_saturated_colour(self) -> None:
        palette = SpeakerPalette()
        for index in range(40):
            for colour in (
                palette.name_colour(f"speaker{index}"),
                palette.body_colour(f"speaker{index}"),
            ):
                with self.subTest(colour=colour):
                    self.assertLessEqual(self._spread(colour), 125, "a distinct palette tint, never pure neon")

    def test_light_mode_palette_generates_distinct_readable_colours(self) -> None:
        palette = SpeakerPalette(dark=False)
        speakers = [f"speaker{index}" for index in range(20)]
        self.assertEqual(palette.distinct_names(speakers), 20)
        for speaker in speakers:
            col = palette.name_colour(speaker)
            # Light mode colours should be deeply saturated and not washed out to white
            channels = self._channels(col)
            self.assertGreaterEqual(self._spread(col), 70, "should have rich distinct tint")
            self.assertLessEqual(max(channels), 225, "should not be washed out to near-white")


if __name__ == "__main__":
    unittest.main()
