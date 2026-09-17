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

# -- OmO light theme tokens (optimized for light/white terminals e.g. Catppuccin Latte) -

TOKENS_LIGHT: Final[dict[str, str]] = {
    "accent": "#1e66f5",
    "border": "#1e66f5",
    "borderAccent": "#04a5e5",
    "borderMuted": "#9ca0b0",
    "success": "#40a02b",
    "error": "#d20f39",
    "warning": "#df8e1d",
    "muted": "#5c5f77",
    "dim": "#7c7f93",
    "text": "#3c3e4f",
    "selectedBg": "#ccd0da",
    "userMessageBg": "#dce0e8",
    "customMessageBg": "#e6e9ef",
    "customMessageLabel": "#8839ef",
}

TOKENS = TOKENS_DARK

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


def _detect_dark_mode() -> bool:
    env = os.environ.get("KAKAOREAD_THEME", "").lower()
    if env in ("light", "latte", "white"):
        return False
    if env in ("dark", "mocha", "black"):
        return True

    colorfgbg = os.environ.get("COLORFGBG", "")
    if colorfgbg and ";" in colorfgbg:
        try:
            parts = colorfgbg.split(";")
            bg = int(parts[-1])
            if bg in (7, 15):
                return False
            if bg in (0, 8):
                return True
        except ValueError:
            pass

    if sys.platform == "darwin":
        try:
            import subprocess
            res = subprocess.run(
                ["/usr/bin/defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True, text=True, timeout=0.5
            )
            return bool(res.returncode == 0 and "Dark" in res.stdout)
        except Exception:
            pass
    return True


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
        self.tokens = TOKENS_DARK if self.dark else TOKENS_LIGHT

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
    if os.environ.get("KAKAOREAD_FORCE_COLOR"):
        return True
    return sys.stdout.isatty()


def _truecolor_supported() -> bool:
    if os.environ.get("COLORTERM", "").lower() in {"truecolor", "24bit"}:
        return True
    # iTerm2 and Apple Terminal both render 24-bit colour; iTerm advertises
    # itself, and Terminal.app is detected by its program name.
    return (
        os.environ.get("TERM_PROGRAM") in {"iTerm.app", "Apple_Terminal", "vscode"}
        or "ITERM_SESSION_ID" in os.environ
    )


# -- Speaker tints -----------------------------------------------------------

_DARK_BASE_LIGHTNESS: Final = 0.831
_DARK_NAME_LIGHTNESS: Final = 0.72
_DARK_NAME_SATURATION: Final = 0.75
_DARK_BODY_SATURATION: Final = 0.10
_DARK_LIGHTNESS_STEPS: Final = (0.0, -0.04, 0.04)

_LIGHT_BASE_LIGHTNESS: Final = 0.35
_LIGHT_NAME_LIGHTNESS: Final = 0.36
_LIGHT_NAME_SATURATION: Final = 0.85
_LIGHT_BODY_SATURATION: Final = 0.15
_LIGHT_LIGHTNESS_STEPS: Final = (0.0, -0.04, 0.04)

_GOLDEN_ANGLE: Final = 137.507764  # Spreads consecutive hues as far as possible.
"""Hue alone stops separating once a room has many speakers, because a small
saturation over a 360-degree circle rounds neighbours to the same byte. A
three-step lightness cycle multiplies the usable slots without making any one
speaker louder than another."""


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

    def __init__(self, dark: bool = True, own_name: str | None = None) -> None:
        self.dark = dark
        self._assigned: dict[str, int] = {}
        self._own_name = own_name

    def _slot(self, speaker: str) -> int:
        if speaker not in self._assigned:
            self._assigned[speaker] = len(self._assigned)
        return self._assigned[speaker]

    def hue(self, speaker: str) -> float:
        """A stable hue, placed as far as possible from the hues already used."""
        return (self._slot(speaker) * _GOLDEN_ANGLE + _hue_for(speaker) * 0.02) % 360

    def _name_lightness(self, speaker: str) -> float:
        slot = self._slot(speaker)
        hue = self.hue(speaker)
        if self.dark:
            return _DARK_NAME_LIGHTNESS + _DARK_LIGHTNESS_STEPS[slot % len(_DARK_LIGHTNESS_STEPS)]
        # For light backgrounds, lower lightness for naturally bright hues (yellow/green/cyan)
        extra_dark = 0.06 if 40 <= hue <= 130 else 0.0
        return _LIGHT_NAME_LIGHTNESS - extra_dark + _LIGHT_LIGHTNESS_STEPS[slot % len(_LIGHT_LIGHTNESS_STEPS)]

    def name_colour(self, speaker: str) -> str:
        """A distinct colour for the speaker's name: clearly separable across people."""
        sat = _DARK_NAME_SATURATION if self.dark else _LIGHT_NAME_SATURATION
        return _hsl_to_hex(self.hue(speaker), sat, self._name_lightness(speaker))

    def body_colour(self, speaker: str) -> str:
        """A far weaker tint for the message itself, so the text stays the text."""
        sat = _DARK_BODY_SATURATION if self.dark else _LIGHT_BODY_SATURATION
        lit = _DARK_BASE_LIGHTNESS if self.dark else _LIGHT_BASE_LIGHTNESS
        return _hsl_to_hex(self.hue(speaker), sat, lit)

    def distinct_names(self, speakers: list[str]) -> int:
        """How many of *speakers* end up visually separable; used by tests."""
        return len({self.name_colour(speaker) for speaker in speakers})
