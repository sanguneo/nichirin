"""Layout and theme tests.

Korean text is double-width and wrapped in zero-width escapes, so every
assertion here is about columns rather than characters. Getting this wrong
does not crash; it shifts every column on screen by a few cells, which is
exactly the kind of bug a test has to catch instead of an eye.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from kanao.tui import theme as theme_module
from kanao.tui.layout import (
    ELLIPSIS,
    Segment,
    display_width,
    elide_head,
    pad_to_width,
    plan_footer,
    strip_ansi,
    truncate_to_width,
    wrap_to_width,
    wrap_with_links,
)
from kanao.tui.theme import (
    LIGHT_BACKGROUND,
    TEXT,
    THEME_VARIABLE,
    TOKENS_DARK,
    TOKENS_LIGHT,
    SpeakerPalette,
    Theme,
    detect_dark_mode,
    to_256,
)

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


class LinkTests(unittest.TestCase):
    """A URL in a message is clickable, and costs the layout nothing.

    The escapes that carry the target are zero-width. If anything measures
    them, every column after a link moves by the length of the URL, which is
    invisible in a screenshot and shows up only as text landing late.
    """

    URL = "https://example.com/products/5035494?ref=chat"

    def targets(self, lines: list[str]) -> list[str]:
        """Every target bound anywhere in *lines*, in order."""
        found = []
        for line in lines:
            for piece in line.split("\x1b]8;;")[1:]:
                target = piece.split("\x1b\\")[0]
                if target:
                    found.append(target)
        return found

    def test_a_url_is_bound_to_its_own_text(self) -> None:
        lines = wrap_with_links(f"봐봐 {self.URL} 이거", 200)
        self.assertEqual(self.targets(lines), [self.URL])
        self.assertIn(self.URL, strip_ansi(lines[0]))

    def test_a_split_url_carries_the_whole_target_on_every_line(self) -> None:
        lines = wrap_with_links(f"봐봐 {self.URL} 이거", 20)
        self.assertGreater(len(self.targets(lines)), 1, "the URL was not split")
        self.assertEqual(set(self.targets(lines)), {self.URL})

    def test_a_link_costs_no_columns(self) -> None:
        for width in (20, 32, 60):
            with self.subTest(width=width):
                for line in wrap_with_links(f"봐봐 {self.URL} 이거", width):
                    self.assertLessEqual(display_width(line), width)

    def test_the_text_is_the_text_that_would_have_been_wrapped(self) -> None:
        """Binding a target must not move, drop or duplicate a character."""
        text = f"앞 {self.URL} 뒤  두칸 {self.URL}/b 끝"
        for width in (16, 24, 40, 100):
            with self.subTest(width=width):
                linked = [strip_ansi(line) for line in wrap_with_links(text, width)]
                self.assertEqual(linked, wrap_to_width(text, width))

    def test_text_without_a_url_is_left_alone(self) -> None:
        text = "링크 없는 평범한 줄입니다"
        self.assertEqual(wrap_with_links(text, 12), wrap_to_width(text, 12))

    def test_a_trailing_bracket_is_not_part_of_the_address(self) -> None:
        lines = wrap_with_links(f"(보기 {self.URL})", 200)
        self.assertEqual(self.targets(lines), [self.URL])

    def test_strip_ansi_removes_the_link_escapes(self) -> None:
        self.assertEqual(strip_ansi(wrap_with_links(self.URL, 200)[0]), self.URL)


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
        self.assertIsNotNone(plan.anchor_plain)
        self.assertTrue(plan.anchor_plain.startswith(ELLIPSIS))

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
                    self.assertLessEqual(self._spread(colour), 60, "a tint, never a colour")


class LightModeTests(unittest.TestCase):
    """A light terminal is a different problem, not the dark one inverted.

    Subtlety is what the dark palette is for. On a white field the same tints
    are simply missing, so the light palette trades some of that calm for
    contrast — and the number is checked rather than asserted in a comment.
    """

    BACKGROUNDS = (LIGHT_BACKGROUND, "#ffffff")
    FLOOR = 3.8

    @staticmethod
    def _relative_luminance(colour: str) -> float:
        channels = []
        for index in (1, 3, 5):
            srgb = int(colour[index : index + 2], 16) / 255
            channels.append(
                srgb / 12.92 if srgb <= 0.04045 else ((srgb + 0.055) / 1.055) ** 2.4
            )
        red, green, blue = channels
        return 0.2126 * red + 0.7152 * green + 0.0722 * blue

    @classmethod
    def _contrast(cls, colour: str, background: str) -> float:
        first = cls._relative_luminance(colour)
        second = cls._relative_luminance(background)
        return (max(first, second) + 0.05) / (min(first, second) + 0.05)

    def test_every_speaker_is_legible_on_a_light_terminal(self) -> None:
        palette = SpeakerPalette(dark=False)
        for index in range(40):
            speaker = f"speaker{index}"
            for background in self.BACKGROUNDS:
                for colour in (palette.name_colour(speaker), palette.body_colour(speaker)):
                    with self.subTest(colour=colour, background=background):
                        self.assertGreaterEqual(
                            self._contrast(colour, background), self.FLOOR
                        )

    def test_the_marks_that_carry_meaning_are_legible(self) -> None:
        """✓, ? and @ say whether a message landed and whether it named you."""
        for token in ("success", "warning", "error", "text", "accent"):
            for background in self.BACKGROUNDS:
                with self.subTest(token=token, background=background):
                    self.assertGreaterEqual(
                        self._contrast(TOKENS_LIGHT[token], background), self.FLOOR
                    )

    def test_a_light_room_still_separates_its_speakers(self) -> None:
        palette = SpeakerPalette(dark=False)
        speakers = [f"speaker{index}" for index in range(32)]
        self.assertGreaterEqual(palette.distinct_names(speakers), 30)

    def test_the_dark_palette_is_not_changed_by_any_of_this(self) -> None:
        palette = SpeakerPalette()
        self.assertTrue(palette.dark)
        self.assertEqual(palette.name_colour("speaker0"), "#e8c0c0")
        self.assertEqual(Theme(truecolor=True, enabled=True).tokens, TOKENS_DARK)

    def test_the_environment_decides_before_anything_else_is_asked(self) -> None:
        for value, dark in (("light", False), ("dark", True), ("Latte", False)):
            with self.subTest(value=value):
                with patch.dict(os.environ, {THEME_VARIABLE: value}):
                    self.assertIs(detect_dark_mode(), dark)

    def test_a_terminal_that_reports_its_background_is_believed(self) -> None:
        for background, dark in (("15;15", False), ("15;0", True)):
            with self.subTest(background=background):
                with patch.dict(os.environ, {THEME_VARIABLE: "", "COLORFGBG": background}):
                    self.assertIs(detect_dark_mode(), dark)

    def test_an_unreadable_environment_leaves_the_theme_dark(self) -> None:
        with patch.dict(os.environ, {THEME_VARIABLE: "chartreuse", "COLORFGBG": "nonsense"}):
            with patch.object(theme_module, "_apps_use_light_theme", lambda: False):
                self.assertTrue(detect_dark_mode())

    def test_the_windows_app_theme_is_the_last_word(self) -> None:
        with patch.dict(os.environ, {THEME_VARIABLE: "", "COLORFGBG": ""}):
            with patch.object(theme_module, "_apps_use_light_theme", lambda: True):
                self.assertFalse(detect_dark_mode())


if __name__ == "__main__":
    unittest.main()
