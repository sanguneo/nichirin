"""Colour tokens and ANSI helpers, matching the OmO interactive theme.

The palette is OmO's ``dark`` theme so the two tools sit beside each other
without clashing. Values are duplicated rather than imported because this
project ships no dependencies and must not read another program's install.

Speaker colouring deserves its own note. Conversations are read for a long
time, and a transcript where every participant shouts in a saturated colour is
tiring and hides the text itself. So every speaker shares one base grey and
differs only by a slight tint: identical lightness, a small amount of
saturation, and a hue picked far from its neighbours. The result reads as one
calm block where the eye can still follow who is speaking.
"""

from __future__ import annotations

import os
import sys
from typing import Final

# -- OmO dark theme tokens ---------------------------------------------------

CYAN: Final = "#00d7ff"
BLUE: Final = "#5f87ff"
GREEN: Final = "#b5bd68"
RED: Final = "#cc6666"
YELLOW: Final = "#ffff00"
TEXT: Final = "#d4d4d4"
GRAY: Final = "#808080"
DIM_GRAY: Final = "#666666"
DARK_GRAY: Final = "#505050"
ACCENT: Final = "#8abeb7"
SELECTED_BG: Final = "#3a3a4a"
USER_MESSAGE_BG: Final = "#343541"
CUSTOM_MESSAGE_BG: Final = "#2d2838"
CUSTOM_MESSAGE_LABEL: Final = "#9575cd"

TOKENS_DARK: Final[dict[str, str]] = {
    "accent": ACCENT,
    "border": BLUE,
    "borderAccent": CYAN,
    "borderMuted": DARK_GRAY,
    "success": GREEN,
    "error": RED,
    "warning": YELLOW,
    "muted": GRAY,
    "dim": DIM_GRAY,
    "text": TEXT,
    "selectedBg": SELECTED_BG,
    "userMessageBg": USER_MESSAGE_BG,
    "customMessageBg": CUSTOM_MESSAGE_BG,
    "customMessageLabel": CUSTOM_MESSAGE_LABEL,
}

# -- The same tokens for a light terminal ------------------------------------
#
# Every colour above is chosen to sit on a dark background: on a white one the
# text greys out and the muted tokens disappear entirely. These are the same
# roles at the other end of the scale.

TOKENS_LIGHT: Final[dict[str, str]] = {
    "accent": "#1e66f5",
    "border": "#1e66f5",
    # Darker than the palette these came from, because `success` and
    # `warning` are not decoration here: they are the ✓, the ? and the @
    # that say whether a message was delivered and whether it named you.
    # At the original #40a02b and #df8e1d those measured 2.96:1 and 2.31:1
    # against a light terminal, which is a mark you have to look for.
    "borderAccent": "#00728f",
    "borderMuted": "#9ca0b0",
    "success": "#2f7d20",
    "error": "#d20f39",
    "warning": "#8f6200",
    "muted": "#5c5f77",
    "dim": "#7c7f93",
    "text": "#3c3e4f",
    "selectedBg": "#ccd0da",
    "userMessageBg": "#dce0e8",
    "customMessageBg": "#e6e9ef",
    "customMessageLabel": "#8839ef",
}

TOKENS: Final = TOKENS_DARK
"""The dark tokens under their old name; a `Theme` reads `self.tokens`."""

THEME_VARIABLE: Final = "KANAO_THEME"
LIGHT_BACKGROUND: Final = "#eff1f5"
"""What a light palette is checked against: a shade off white, because white
is the easiest background for dark text and proves the least."""

RESET: Final = "\x1b[0m"
BOLD: Final = "\x1b[1m"
NOT_BOLD: Final = "\x1b[22m"
RESET_FG: Final = "\x1b[39m"
RESET_BG: Final = "\x1b[49m"

_CUBE = (0, 95, 135, 175, 215, 255)
_GRAYS = tuple(8 + index * 10 for index in range(24))


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    cleaned = hex_colour.lstrip("#")
    if len(cleaned) != 6:
        raise ValueError(f"invalid hex colour: {hex_colour}")
    return int(cleaned[0:2], 16), int(cleaned[2:4], 16), int(cleaned[4:6], 16)


def _distance(first: tuple[int, int, int], second: tuple[int, int, int]) -> float:
    """Weighted so the comparison follows the eye's sensitivity to green."""
    red, green, blue = (a - b for a, b in zip(first, second))
    return red * red * 0.299 + green * green * 0.587 + blue * blue * 0.114


def to_256(hex_colour: str) -> int:
    """Approximate a colour in the 256-colour palette, keeping any tint."""
    source = _rgb(hex_colour)
    indices = [min(range(6), key=lambda i: abs(value - _CUBE[i])) for value in source]
    cube = (_CUBE[indices[0]], _CUBE[indices[1]], _CUBE[indices[2]])
    cube_index = 16 + 36 * indices[0] + 6 * indices[1] + indices[2]

    red, green, blue = source
    gray = round(0.299 * red + 0.587 * green + 0.114 * blue)
    gray_slot = min(range(24), key=lambda i: abs(gray - _GRAYS[i]))
    gray_value = _GRAYS[gray_slot]

    # Only fall back to the grey ramp for colours with no tint worth keeping.
    neutral = max(source) - min(source) < 10
    if neutral and _distance(source, (gray_value, gray_value, gray_value)) < _distance(source, cube):
        return 232 + gray_slot
    return cube_index


class Theme:
    """Renders colour for one terminal's capabilities."""

    def __init__(
        self,
        truecolor: bool | None = None,
        enabled: bool | None = None,
        dark: bool = True,
    ) -> None:
        self.enabled = _colour_supported() if enabled is None else enabled
        self.truecolor = _truecolor_supported() if truecolor is None else truecolor
        self.dark = dark
        """Dark unless a caller says otherwise. `detect_dark_mode` is asked
        once, by `cli`, for the same reason the name ledger is: a default that
        reads the machine makes every test depend on the machine."""
        self.tokens = TOKENS_DARK if dark else TOKENS_LIGHT

    def _sequence(self, hex_colour: str, background: bool) -> str:
        layer = 48 if background else 38
        if self.truecolor:
            red, green, blue = _rgb(hex_colour)
            return f"\x1b[{layer};2;{red};{green};{blue}m"
        return f"\x1b[{layer};5;{to_256(hex_colour)}m"

    def fg(self, token_or_hex: str, text: str) -> str:
        """Paint *text* in a theme token or a literal hex colour."""
        if not self.enabled or not text:
            return text
        colour = self.tokens.get(token_or_hex, token_or_hex)
        return f"{self._sequence(colour, False)}{text}{RESET_FG}"

    def bg(self, token_or_hex: str, text: str) -> str:
        """Fill the background of *text*, leaving any foreground runs intact."""
        if not self.enabled:
            return text
        colour = self.tokens.get(token_or_hex, token_or_hex)
        # Existing foreground resets inside the run must not clear the fill.
        body = text.replace(RESET_BG, self._sequence(colour, True))
        return f"{self._sequence(colour, True)}{body}{RESET_BG}"

    def bold(self, text: str) -> str:
        return f"{BOLD}{text}{NOT_BOLD}" if self.enabled and text else text


def _colour_supported() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("KANAO_FORCE_COLOR"):
        return True
    return sys.stdout.isatty()


def detect_dark_mode() -> bool:
    """Whether the terminal is most likely showing light text on a dark field.

    Nothing here can read the terminal's actual background: Windows Terminal
    keeps its colour scheme to itself and reports the legacy attribute table
    through the console API whatever the scheme says. So this is three
    guesses in order of how much they know, and the answer is dark when none
    of them do — which is both the common case and the one the palette was
    built for.
    """
    wanted = os.environ.get(THEME_VARIABLE, "").strip().lower()
    if wanted in ("light", "latte", "white"):
        return False
    if wanted in ("dark", "mocha", "black"):
        return True

    # Set by some terminals as "foreground;background" in palette indices.
    foreground_background = os.environ.get("COLORFGBG", "")
    if ";" in foreground_background:
        try:
            background = int(foreground_background.rsplit(";", 1)[1])
        except ValueError:
            background = -1
        if background in (7, 15):
            return False
        if background in (0, 8):
            return True

    return not _apps_use_light_theme()


def _apps_use_light_theme() -> bool:
    """The Windows app theme, which a terminal following the OS will match."""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return bool(value)
    except (ImportError, OSError, ValueError):
        return False


def _truecolor_supported() -> bool:
    if os.environ.get("COLORTERM", "").lower() in {"truecolor", "24bit"}:
        return True
    # Windows Terminal and modern conhost both render 24-bit colour.
    return os.environ.get("WT_SESSION") is not None or sys.platform == "win32"


# -- Speaker tints -----------------------------------------------------------

_BASE_LIGHTNESS: Final = 0.831  # #d4d4d4 expressed as HSL lightness.
_NAME_SATURATION: Final = 0.46
_BODY_SATURATION: Final = 0.10
_LIGHTNESS_STEPS: Final = (0.0, -0.035, 0.035)
"""Hue alone stops separating once a room has many speakers, because a small
saturation over a 360-degree circle rounds neighbours to the same byte. A
three-step lightness cycle multiplies the usable slots without making any one
speaker louder than another."""

# The light end is not the dark end inverted. A tint that reads as subtle on
# a dark field is nearly invisible on a white one, so the names carry more
# saturation and far less lightness, and the yellow-to-cyan arc is darkened
# again because those hues are naturally bright and lose contrast first.
_LIGHT_BASE_LIGHTNESS: Final = 0.35
_LIGHT_NAME_LIGHTNESS: Final = 0.28
_LIGHT_NAME_SATURATION: Final = 0.85
_LIGHT_BODY_SATURATION: Final = 0.15
_LIGHT_LIGHTNESS_STEPS: Final = (0.0, -0.03, 0.03)
_LIGHT_BRIGHT_HUES: Final = (40.0, 185.0)
_LIGHT_BRIGHT_RELIEF: Final = 0.04

_GOLDEN_ANGLE: Final = 137.507764  # Spreads consecutive hues as far as possible.


def _hsl_to_hex(hue: float, saturation: float, lightness: float) -> str:
    chroma = (1 - abs(2 * lightness - 1)) * saturation
    section = (hue % 360) / 60
    second = chroma * (1 - abs(section % 2 - 1))
    if section < 1:
        red, green, blue = chroma, second, 0.0
    elif section < 2:
        red, green, blue = second, chroma, 0.0
    elif section < 3:
        red, green, blue = 0.0, chroma, second
    elif section < 4:
        red, green, blue = 0.0, second, chroma
    elif section < 5:
        red, green, blue = second, 0.0, chroma
    else:
        red, green, blue = chroma, 0.0, second
    offset = lightness - chroma / 2
    values = [round((channel + offset) * 255) for channel in (red, green, blue)]
    return "#" + "".join(f"{max(0, min(255, value)):02x}" for value in values)


def _hue_for(name: str) -> float:
    """Derive a stable hue from a name, so a tint survives a restart."""
    seed = 0
    for character in name:
        seed = (seed * 31 + ord(character)) & 0xFFFFFFFF
    return (seed * _GOLDEN_ANGLE) % 360


class SpeakerPalette:
    """Assigns each speaker a tint of the base text colour, never a new colour.

    Hues are handed out by walking the golden angle in first-appearance order,
    so the speakers visible together are the ones furthest apart in hue, while
    a speaker keeps the same tint for as long as the palette lives.
    """

    def __init__(self, own_name: str | None = None, dark: bool = True) -> None:
        self._assigned: dict[str, int] = {}
        self._own_name = own_name
        self.dark = dark

    def _slot(self, speaker: str) -> int:
        if speaker not in self._assigned:
            self._assigned[speaker] = len(self._assigned)
        return self._assigned[speaker]

    def hue(self, speaker: str) -> float:
        """A stable hue, placed as far as possible from the hues already used."""
        return (self._slot(speaker) * _GOLDEN_ANGLE + _hue_for(speaker) * 0.02) % 360

    def _lightness(self, speaker: str) -> float:
        slot = self._slot(speaker)
        if self.dark:
            return _BASE_LIGHTNESS + _LIGHTNESS_STEPS[slot % len(_LIGHTNESS_STEPS)]
        low, high = _LIGHT_BRIGHT_HUES
        relief = _LIGHT_BRIGHT_RELIEF if low <= self.hue(speaker) <= high else 0.0
        return (
            _LIGHT_NAME_LIGHTNESS
            - relief
            + _LIGHT_LIGHTNESS_STEPS[slot % len(_LIGHT_LIGHTNESS_STEPS)]
        )

    def name_colour(self, speaker: str) -> str:
        """A tint for the speaker's name: short enough to carry real colour."""
        saturation = _NAME_SATURATION if self.dark else _LIGHT_NAME_SATURATION
        return _hsl_to_hex(self.hue(speaker), saturation, self._lightness(speaker))

    def body_colour(self, speaker: str) -> str:
        """A far weaker tint for the message itself, so the text stays the text."""
        saturation = _BODY_SATURATION if self.dark else _LIGHT_BODY_SATURATION
        base = _BASE_LIGHTNESS if self.dark else _LIGHT_BASE_LIGHTNESS
        return _hsl_to_hex(self.hue(speaker), saturation, base)

    def distinct_names(self, speakers: list[str]) -> int:
        """How many of *speakers* end up visually separable; used by tests."""
        return len({self.name_colour(speaker) for speaker in speakers})
