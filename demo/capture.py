"""Turn a mockup scene into a standalone HTML page, ready to be photographed.

The Windows original drove Windows Terminal from PowerShell and photographed
the window it opened, which is why the shots it produced carry Windows chrome
and Windows fonts.  This does the opposite: the scene is rendered to a page
that *is* the picture, so the capture owns its own frame and nothing from the
host desktop can wander into it.

Alignment is the whole difficulty.  A terminal puts every character in a cell
and gives a Korean syllable two of them; a browser gives it whatever the font
says.  So each character here is placed in a box of its own, sized by
``layout.char_width`` — the same function the interface lays itself out with —
and the grid holds no matter which font ends up drawing the glyph.

    python3 demo/capture.py --all           every scene, into demo/.render
    python3 demo/capture.py rooms           one scene
    python3 demo/capture.py rooms --out /tmp/rooms.html

Only the HTML is written; turning it into a PNG is a screenshot, by whatever
headless browser is at hand.  Nothing here touches the signed-in account: the
scenes come from ``mockup.py``, which invents every name it shows.
"""

from __future__ import annotations

import argparse
import re
import sys
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mockup  # noqa: E402
from kamado.tui.layout import char_width  # noqa: E402

SCENES = ("rooms", "chat", "digest", "friends", "filter", "sent", "unseen", "real")

WIDTH, HEIGHT = 110, 30
"""The grid the interface is meant to be seen in."""

CELL, LINE, FONT = 9, 21, 15
"""Cell box, line box, and type size, in CSS pixels."""

PAD = 24
"""Room around the window, so its shadow is inside the picture."""

BACKGROUND = "#16161d"
FOREGROUND = "#d8d8d8"

_SGR = re.compile(r"\x1b\[([0-9;]*)m")


class Pen:
    """The colours in force, as the escape codes leave them."""

    __slots__ = ("fg", "bg", "bold")

    def __init__(self) -> None:
        self.fg: str | None = None
        self.bg: str | None = None
        self.bold = False

    def apply(self, parameters: str) -> None:
        codes = [int(one or 0) for one in parameters.split(";")] if parameters else [0]
        index = 0
        while index < len(codes):
            code = codes[index]
            if code == 0:
                self.fg = self.bg = None
                self.bold = False
            elif code == 1:
                self.bold = True
            elif code == 22:
                self.bold = False
            elif code == 39:
                self.fg = None
            elif code == 49:
                self.bg = None
            elif code in (38, 48) and codes[index + 1 : index + 2] == [2]:
                red, green, blue = codes[index + 2 : index + 5]
                colour = f"#{red:02x}{green:02x}{blue:02x}"
                if code == 38:
                    self.fg = colour
                else:
                    self.bg = colour
                index += 4
            index += 1

    def style(self) -> str:
        parts = []
        if self.fg:
            parts.append(f"color:{self.fg}")
        if self.bg:
            parts.append(f"background:{self.bg}")
        if self.bold:
            parts.append("font-weight:600")
        return ";".join(parts)


def row(line: str) -> str:
    """Render one line of escaped text as a run of fixed-width cells."""
    pen = Pen()
    out: list[str] = []
    position = 0
    for piece in _SGR.split(line):
        if position % 2:
            pen.apply(piece)
        else:
            for character in piece:
                width = char_width(character)
                if width == 0:
                    continue
                style = pen.style()
                box = f"width:{width * CELL}px"
                attribute = f"{box};{style}" if style else box
                # A wide glyph is drawn at the size of the two cells it owns.
                # Left at the Latin size it would sit in its box with a gap
                # beside it, which is the spaced-out look the old shots had.
                wide = ' class="x"' if width == 2 else ""
                out.append(f'<i{wide} style="{attribute}">{escape(character)}</i>')
        position += 1
    return f'<div class="r">{"".join(out)}</div>'


PAGE = """<!doctype html>
<meta charset="utf-8">
<title>{title}</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ background:transparent; display:flex; padding:{pad}px; }}
  .w {{
    width:{frame}px; border-radius:10px; overflow:hidden;
    background:{background}; font-kerning:none; font-variant-ligatures:none;
    box-shadow:0 18px 50px rgba(0,0,0,.45);
  }}
  .bar {{
    height:28px; display:flex; align-items:center; gap:8px; padding:0 12px;
    background:#23232c; border-bottom:1px solid #2e2e38;
  }}
  .d {{ width:12px; height:12px; border-radius:50%; }}
  .t {{
    flex:1; text-align:center; margin-left:-56px; color:#9a9aa8;
    font:12px/1 -apple-system, "Helvetica Neue", sans-serif;
  }}
  .s {{ padding:14px 16px; }}
  .r {{ height:{line}px; white-space:nowrap; }}
  i {{
    display:inline-block; height:{line}px; line-height:{line}px;
    font-style:normal; text-align:center; overflow:hidden;
  }}
  i.x {{ font-size:{wide}px; }}
  .s {{
    color:{foreground};
    font:{font}px/{line}px "MesloLGS NF","Menlo","SF Mono",monospace;
  }}
</style>
<div class="w">
  <div class="bar">
    <span class="d" style="background:#ff5f57"></span>
    <span class="d" style="background:#febc2e"></span>
    <span class="d" style="background:#28c840"></span>
    <span class="t">{title}</span>
  </div>
  <div class="s">{body}</div>
</div>
"""


def page(lines: list[str], columns: int, title: str) -> str:
    """Wrap rendered rows in a window that looks like the platform it runs on."""
    return PAGE.format(
        title=escape(title),
        frame=columns * CELL + 32,
        pad=PAD,
        background=BACKGROUND,
        foreground=FOREGROUND,
        line=LINE,
        font=FONT,
        wide=CELL * 2,
        body="".join(row(one) for one in lines),
    )


def scene_lines(name: str) -> tuple[list[str], int]:
    """Return the scene's rendered lines and the columns they need."""
    if name == "cli":
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with mockup.build_reader() as reader, redirect_stdout(buffer):
            mockup.show_cli(reader)
        lines = buffer.getvalue().splitlines()
        return lines, max(len(one) for one in lines) + 2

    from kamado.tui.app import App
    from kamado.tui.theme import Theme

    with mockup.build_reader() as reader:
        # Forced rather than detected: a screenshot should show the palette the
        # interface was designed in, whatever the host terminal advertises.
        app = App(reader, theme=Theme(truecolor=True, enabled=True))
        mockup.arrange(app, name)
        return app.render(WIDTH, HEIGHT), WIDTH


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scene", nargs="?", choices=(*SCENES, "cli"))
    parser.add_argument("--all", action="store_true", help="Render every scene")
    parser.add_argument("--out", type=Path, help="Write one scene here")
    arguments = parser.parse_args(argv)

    if not arguments.all and not arguments.scene:
        parser.error("name a scene, or pass --all")

    wanted = (*SCENES, "cli") if arguments.all else (arguments.scene,)
    destination = Path(__file__).resolve().parent / ".render"
    destination.mkdir(exist_ok=True)

    for name in wanted:
        lines, columns = scene_lines(name)
        target = arguments.out if arguments.out and not arguments.all else destination / f"{name}.html"
        target.write_text(page(lines, columns, f"kamado — {name}"), encoding="utf-8")
        print(f"{name}: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
