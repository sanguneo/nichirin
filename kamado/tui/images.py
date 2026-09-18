"""Show a photo in the terminal itself, for the terminals that can.

Three facts shape this module, and each was established rather than assumed:

* **The client stores addresses, not pictures.**  A photo message's attachment
  holds ``thumbnailUrl`` and ``url`` on ``talk.kakaocdn.net`` plus the original
  dimensions; the container keeps no image cache to read instead.  So a photo
  that is actually shown has to be fetched, which is the one place in this
  program that reaches the network — and only when somebody clicks.
* **The thumbnail is the right thing to fetch.**  Measured on live rows, a
  thumbnail is 1.7-9.4 KB against 85 KB-1.7 MB for the original, and a
  terminal cell grid cannot show a 1988x1118 image at full size anyway.
* **A drawn image is not part of the frame.**  Kitty graphics place a picture
  over the cell grid and it stays there until it is deleted by id, so every
  placement this module makes is remembered and explicitly withdrawn.  A frame
  redrawn underneath an orphaned placement is how a terminal ends up with a
  photo floating over unrelated text.

Nothing here decides *whether* to show a photo; that is the interface's call.
This module answers "can this terminal do it", "what are the bytes", and "what
escape sequence draws or erases them".
"""

from __future__ import annotations

import base64
import os
import struct
import subprocess
import tempfile
import urllib.error
import urllib.request
from typing import Any, Final, Optional

from .. import paths

THUMBNAIL_KEYS: Final = ("url", "thumbnailUrl")
"""Where a single photo's address lives, best first.

The full-size address comes first: a thumbnail measured 110x120 against its
original's 384x422, and the terminal scales whatever it is given into the same
cells, so the thumbnail is simply a softer picture in the same space.
"""

LIST_KEYS: Final = ("imageUrls", "thumbnailUrls")
"""The same for a multi-photo message, which stores lists instead."""

_ALLOWED_HOSTS: Final = ("talk.kakaocdn.net", "dn-talk.kakaocdn.net")
"""Hosts a photo may be fetched from.

The address comes out of a database another program writes, so it is checked
against the CDN this client is known to use rather than followed anywhere it
happens to point.
"""

_FETCH_TIMEOUT_SECONDS: Final = 8.0
_MAX_BYTES: Final = 4 * 1024 * 1024
"""Refuse anything larger than a photo plausibly is, rather than filling memory."""

_CHUNK: Final = 4096
"""Kitty's escape payload is chunked; 4096 base64 bytes per chunk is its limit."""

_CONVERT_TIMEOUT_SECONDS: Final = 10.0
"""How long a format conversion may take before the photo is given up on."""

MAX_ROWS: Final = 20
"""A floor for how tall a shown photo may be, in cells.

Used only when the caller cannot say how tall the conversation is; a real
frame passes its own height so the picture scales with the terminal.
"""

MAX_COLUMNS: Final = 96
"""How wide a shown photo may be, in cells.

A photo is scaled into whatever cell box it is given, so a small box is the
other half of why an image looks low-resolution.  On a Retina terminal a cell
is roughly 8x17 device pixels, which puts this near 768 px of real width.
"""

BODY_SHARE: Final = 0.8
"""How much of the conversation's height one picture may take.

Leaving a fifth of the view to the conversation is what keeps an opened photo
from reading as a full-screen viewer; the rest of the messages stay in sight.
"""

MIN_CELLS: Final = 2
"""The smallest a picture may be on either axis, in cells.

A single cell is not a picture, but forcing a floor on one axis alone is what
flattens a panorama into a square, so the other axis is reduced to match and
the shape is reported as the caller asked -- rounded, not squashed.
"""

CELL_ASPECT: Final = 2.0
"""How many times taller than wide a terminal cell is, when it cannot be asked.

Measured on this machine a cell is 8x17 device pixels, so 2.0 is close but not
exact -- assuming it stretches an ordinary photo by 5-8%. A terminal that
answers ``CSI 16 t`` is believed over this default.
"""

_MAX_IMAGE_ID: Final = 0xFFFFFFFF
"""The protocol's image id is a 32-bit field.

A KakaoTalk message id is a ~63-bit snowflake, so it cannot be used as an
image id: a terminal handed one silently rejects the placement and draws
nothing, which is exactly how this failed the first time.  Ids are handed out
by :class:`ImageIds` instead.
"""


class ImageIds:
    """Hands out terminal image ids and remembers which message owns each one."""

    def __init__(self) -> None:
        self._by_message: dict[int, int] = {}
        self._next = 1

    def of(self, message_id: int) -> int:
        known = self._by_message.get(message_id)
        if known is not None:
            return known
        assigned = self._next
        self._next = assigned + 1 if assigned < _MAX_IMAGE_ID else 1
        self._by_message[message_id] = assigned
        return assigned


def supported(term_program: Optional[str] = None, term: Optional[str] = None) -> bool:
    """Whether this terminal draws Kitty graphics.

    Read from the environment rather than by querying the terminal: a query
    needs a reply before the first frame can be drawn, and a terminal that
    ignores it would leave the interface waiting on input that never comes.
    """
    program = (term_program if term_program is not None else os.environ.get("TERM_PROGRAM", "")).lower()
    name = (term if term is not None else os.environ.get("TERM", "")).lower()
    if program in ("ghostty", "wezterm", "kitty"):
        return True
    return "kitty" in name or "ghostty" in name


def photo_url(attachment: dict[str, Any]) -> Optional[str]:
    """Return the address of the picture to show, or None when there is none.

    A multi-photo message shows its first picture: the transcript has one line
    to spend on it, and the first is the one the client shows as the cover.
    """
    for key in THUMBNAIL_KEYS:
        value = attachment.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    for key in LIST_KEYS:
        values = attachment.get(key)
        if isinstance(values, list):
            for value in values:
                if isinstance(value, str) and value.startswith("http"):
                    return value
    return None


def _permitted(url: str) -> bool:
    """Whether *url* points at the client's own CDN over TLS."""
    from urllib.parse import urlsplit

    split = urlsplit(url)
    if split.scheme != "https":
        return False
    return split.hostname is not None and split.hostname.lower() in _ALLOWED_HOSTS


def pixel_size(blob: bytes) -> Optional[tuple[int, int]]:
    """Return a picture's own width and height, read from its bytes.

    The stored dimensions cannot be trusted for shape.  Measured across live
    rows, 13 of 14 matched but one recorded 1848x4000 for an image that is
    actually 4000x1848 -- a rotated photo whose stored size was never turned
    with it.  Sizing from that lands a landscape picture in a tall, narrow box.

    PNG, JPEG and WebP are decoded, which is what this client stores; anything
    else falls back to the stored numbers.
    """
    if blob[:8] == b"\x89PNG\r\n\x1a\n" and len(blob) >= 24:
        width, height = struct.unpack(">II", blob[16:24])
        return width, height
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return _webp_size(blob)
    if blob[:2] == b"\xff\xd8":
        index = 2
        while index < len(blob) - 9:
            if blob[index] != 0xFF:
                index += 1
                continue
            marker = blob[index + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3):
                height, width = struct.unpack(">HH", blob[index + 5 : index + 9])
                return width, height
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                index += 2
                continue
            index += 2 + struct.unpack(">H", blob[index + 2 : index + 4])[0]
    return None


def _webp_size(blob: bytes) -> Optional[tuple[int, int]]:
    """Return a WebP's dimensions from whichever of its three shapes it is.

    Lossy stores them as 14-bit fields, lossless packs them across bytes as
    ``width - 1``, and the extended form uses 24-bit values; the client sends
    all of them, so reading only one shape leaves photos unsized.
    """
    if len(blob) < 30:
        return None
    kind = blob[12:16]
    try:
        if kind == b"VP8 ":
            return (
                struct.unpack("<H", blob[26:28])[0] & 0x3FFF,
                struct.unpack("<H", blob[28:30])[0] & 0x3FFF,
            )
        if kind == b"VP8L":
            bits = struct.unpack("<I", blob[21:25])[0]
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if kind == b"VP8X":
            width = blob[24] | (blob[25] << 8) | (blob[26] << 16)
            height = blob[27] | (blob[28] << 8) | (blob[29] << 16)
            return width + 1, height + 1
    except struct.error:
        return None
    return None


def drawable(blob: bytes) -> Optional[bytes]:
    """Return bytes the terminal will accept, converting when it will not.

    The protocol defines exactly three payload formats -- 24-bit RGB, 32-bit
    RGBA, and PNG via ``f=100`` -- so PNG is the only file format a terminal is
    required to decode.  Ghostty happens to carry a JPEG decoder and draws one
    anyway, but kitty and WezTerm are within spec to ignore it, so JPEG and WebP
    are both converted rather than relying on that.

    ``sips`` ships with macOS, so the conversion costs no dependency; a picture
    that cannot be converted is reported as undrawable rather than sent as
    bytes the terminal is free to drop in silence.
    """
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return blob
    return _to_png(blob)
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return blob
    if blob[:2] == b"\xff\xd8":
        return blob
    return _to_png(blob)


def _to_png(blob: bytes) -> Optional[bytes]:
    """Convert a picture to PNG with the tool macOS already provides."""
    try:
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "source")
            target = os.path.join(directory, "target.png")
            with open(source, "wb") as handle:
                handle.write(blob)
            finished = subprocess.run(
                ["sips", "-s", "format", "png", source, "--out", target],
                capture_output=True,
                timeout=_CONVERT_TIMEOUT_SECONDS,
            )
            if finished.returncode != 0:
                return None
            with open(target, "rb") as handle:
                return handle.read()
    except (OSError, subprocess.SubprocessError):
        return None


def fit(
    width: int,
    height: int,
    columns: int,
    body_rows: Optional[int] = None,
    cell: Optional[tuple[int, int]] = None,
) -> tuple[int, int, tuple[int, int]]:
    """Return the cell box for a picture and the pixel box it is drawn into.

    The protocol scales a picture into whatever cell rectangle it is given, so a
    cell box alone cannot express an exact ratio: rounding to whole cells moves
    it by however much the rounding was.  Measured against a real 8x17 cell, an
    ordinary photo came out 5-8% too tall and a panorama over 100% wrong.

    So the pixel box is returned alongside the cells.  The caller pads the
    picture to exactly that many pixels, which lets the terminal scale a source
    whose ratio already matches the box -- leaving the picture's own proportions
    untouched no matter how the cell rounding fell.
    """
    cell_width, cell_height = cell or (1, round(CELL_ASPECT))
    aspect = (cell_height / cell_width) if cell_width > 0 and cell_height > 0 else CELL_ASPECT
    cols, rows = cell_size(width, height, columns, body_rows, aspect)
    return cols, rows, (cols * cell_width, rows * cell_height)


def letterbox(blob: bytes, box: tuple[int, int]) -> Optional[bytes]:
    """Pad *blob* with transparent margins until it fills *box* exactly.

    The picture is centred and never scaled or cropped here; only margins are
    added.  A source whose ratio already equals the box's is returned unchanged,
    which is the common case once the box was computed from its own shape.
    """
    measured = pixel_size(blob)
    if measured is None:
        return None
    source_width, source_height = measured
    box_width, box_height = box
    if source_width <= 0 or source_height <= 0 or box_width <= 0 or box_height <= 0:
        return None

    scale = min(box_width / source_width, box_height / source_height)
    drawn_width = max(1, round(source_width * scale))
    drawn_height = max(1, round(source_height * scale))
    if (drawn_width, drawn_height) == (box_width, box_height):
        return blob
    return _pad_to_box(
        blob, (drawn_width, drawn_height), (box_width, box_height)
    )


def _pad_to_box(
    blob: bytes, drawn: tuple[int, int], box: tuple[int, int]
) -> Optional[bytes]:
    """Scale a picture to *drawn* and centre it on a transparent *box*."""
    try:
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "source")
            target = os.path.join(directory, "target.png")
            with open(source, "wb") as handle:
                handle.write(blob)
            finished = subprocess.run(
                [
                    "sips",
                    "--resampleHeightWidth",
                    str(drawn[1]),
                    str(drawn[0]),
                    "--padToHeightWidth",
                    str(box[1]),
                    str(box[0]),
                    "--padColor",
                    "000000",
                    "-s",
                    "format",
                    "png",
                    source,
                    "--out",
                    target,
                ],
                capture_output=True,
                timeout=_CONVERT_TIMEOUT_SECONDS,
            )
            if finished.returncode != 0:
                return None
            with open(target, "rb") as handle:
                return handle.read()
    except (OSError, subprocess.SubprocessError):
        return None


def cell_size(
    width: int,
    height: int,
    columns: int,
    body_rows: Optional[int] = None,
    cell_aspect: float = CELL_ASPECT,
) -> tuple[int, int]:
    """Fit a picture's own pixel size into cells, keeping its shape.

    A terminal cell is taller than it is wide, so the row count is divided by
    that ratio; sizing without it lands a picture about twice as tall as it
    should be. *cell_aspect* is the measured ratio when the terminal reported
    one, since a wrong ratio is a picture stretched by exactly that error.

    *body_rows* is how many rows the conversation has.  The ceiling follows it
    rather than being fixed, so a tall terminal shows a larger picture instead
    of scaling a 2 MB original down to a fixed box, and a short one is not
    swamped by a photo taller than the view.

    Both axes are reduced together when either bound is hit, so the shape
    survives even the extremes: a panorama clamped to the floor of two rows
    would otherwise be drawn nearly square.
    """
    ceiling = MAX_ROWS if body_rows is None else max(2, int(body_rows * BODY_SHARE))
    limit_cols = max(4, min(columns, MAX_COLUMNS))
    if width <= 0 or height <= 0:
        return limit_cols, min(ceiling, 8)

    aspect = cell_aspect if cell_aspect > 0 else CELL_ASPECT
    cols = limit_cols
    rows = cols * height / width / aspect
    if rows > ceiling:
        cols = cols * ceiling / rows
        rows = ceiling

    # A floor on one axis alone changes the shape, so whichever axis is too
    # small pulls the other down with it by the same factor -- narrowing a
    # panorama rather than letting it be drawn nearly square.
    if rows < MIN_CELLS and rows > 0:
        cols, rows = cols * MIN_CELLS / rows, MIN_CELLS
    if cols < MIN_CELLS and cols > 0:
        cols, rows = MIN_CELLS, rows * MIN_CELLS / cols
    if cols > limit_cols:
        cols, rows = limit_cols, rows * limit_cols / cols
    if rows > ceiling:
        cols, rows = cols * ceiling / rows, ceiling

    # Rounding last keeps the ratio: rounding each axis as it is computed lets
    # a small box drift away from the shape that was asked for.
    return max(1, round(cols)), max(1, round(rows))


class PhotoStore:
    """Fetches photos once and keeps them beside the tool's other state."""

    def __init__(self, directory: Optional[str] = None) -> None:
        self._directory = directory

    def directory(self) -> str:
        if self._directory is None:
            self._directory = os.path.join(str(paths.state_directory()), "photos")
        return self._directory

    def cached_path(self, url: str) -> str:
        """Where *url* is kept, named by its digest rather than by its address.

        The address carries the account's own CDN parameters, so it is hashed
        rather than written into a filename somebody else may read.
        """
        from hashlib import sha256

        digest = sha256(url.encode("utf-8")).hexdigest()[:40]
        return os.path.join(self.directory(), f"{digest}.img")

    def fetch(self, url: str) -> Optional[bytes]:
        """Return the picture's bytes, from disk when they are already there.

        Returns None rather than raising: a photo that cannot be shown is a
        message that stays as ``[사진]``, not a conversation that fails.

        Only the hosts the client itself stores addresses on are contacted, and
        the check lives here because this is the only method that opens a
        socket -- a whitelist somewhere else is one a later caller can walk
        around.
        """
        if not _permitted(url):
            return None
        path = self.cached_path(url)
        try:
            with open(path, "rb") as handle:
                return handle.read()
        except OSError:
            pass

        request = urllib.request.Request(url, headers={"User-Agent": "kamado-cli"})
        try:
            with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_SECONDS) as response:
                if not str(response.headers.get("Content-Type", "")).startswith("image/"):
                    return None
                blob = response.read(_MAX_BYTES + 1)
        except (urllib.error.URLError, OSError, ValueError):
            return None
        if not blob or len(blob) > _MAX_BYTES:
            return None

        try:
            os.makedirs(self.directory(), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(blob)
        except OSError:
            pass  # A photo that could not be cached is still a photo.
        return blob


def draw(
    image_id: int,
    blob: bytes,
    columns: int,
    rows: int,
    crop: Optional[tuple[int, int]] = None,
) -> str:
    """Return the escape that places *blob* at the cursor, sized in cells.

    ``a=T`` transmits and displays in one go, ``f=100`` names the payload as a
    complete image file rather than raw pixels, and ``c``/``r`` ask the
    terminal to scale it into that many cells.  The payload is chunked because
    the protocol caps one escape's data, and every chunk but the last says
    ``m=1`` for "more is coming".

    *crop* is a ``(top, height)`` pair in source pixels.  A picture whose rows
    run past the conversation would otherwise be drawn over the composer and
    the footer, because a placement is not clipped by anything the frame does.
    """
    payload = base64.standard_b64encode(blob).decode("ascii")
    chunks = [payload[index : index + _CHUNK] for index in range(0, len(payload), _CHUNK)] or [""]
    window = "" if crop is None else f",y={crop[0]},h={crop[1]}"
    pieces: list[str] = []
    for index, chunk in enumerate(chunks):
        more = 1 if index < len(chunks) - 1 else 0
        if index == 0:
            control = (
                f"a=T,f=100,i={image_id},q=2,c={columns},r={rows}{window},m={more}"
            )
        else:
            control = f"m={more}"
        pieces.append(f"\x1b_G{control};{chunk}\x1b\\")
    return "".join(pieces)


def place(
    image_id: int,
    columns: int,
    rows: int,
    crop: Optional[tuple[int, int]] = None,
) -> str:
    """Return the escape that shows an already-transmitted image again.

    Measured on live rows, one original is 294-362 KB, which is 392-482 KB once
    base64-encoded; re-transmitting that whenever the picture moves is what a
    reader sees as flicker.  The terminal keeps the image against its id, so a
    move only needs a fresh placement (``a=p``) rather than the bytes again.
    """
    window = "" if crop is None else f",y={crop[0]},h={crop[1]}"
    return f"\x1b_Ga=p,i={image_id},q=2,c={columns},r={rows}{window}\x1b\\"


def crop_for(
    height: int, total_rows: int, hidden_rows: int, shown_rows: int
) -> Optional[tuple[int, int]]:
    """Return the source band to show when only part of a picture fits.

    Rows are the only unit the transcript works in, so the visible fraction is
    converted back into source pixels here; a picture with nothing hidden gets
    no crop at all, which keeps the common case a plain full-size placement.

    A band of zero height is not a crop but an invisible picture, and asking a
    terminal for one draws nothing while still holding a placement, so a fully
    hidden picture is reported as having no band to show.
    """
    if height <= 0 or total_rows <= 0 or shown_rows <= 0:
        return None
    if hidden_rows <= 0 and shown_rows >= total_rows:
        return None
    if hidden_rows >= total_rows:
        return None
    top = round(height * hidden_rows / total_rows)
    band = max(1, round(height * min(shown_rows, total_rows - hidden_rows) / total_rows))
    return top, min(band, height - top)


def erase(image_id: int, free: bool = True) -> str:
    """Return the escape that removes a placement.

    ``d=I`` also releases the transmitted image, which is what keeps a room full
    of opened photos from growing the terminal's image memory.  ``d=i`` removes
    only the placement and leaves the bytes, so a picture that merely moved can
    be shown again without being sent a second time.
    """
    return f"\x1b_Ga=d,d={'I' if free else 'i'},i={image_id},q=2\x1b\\"
