"""Sixel graphics for the terminals that draw them but not Kitty's protocol.

Sixel is not a file format: it carries raw pixels, so a picture has to be
decoded before it can be drawn.  That is the whole reason this module exists
beside :mod:`images` -- Kitty and iTerm2 accept a PNG as-is, and only sixel
needs the bytes taken apart.

Two functions, both pure standard library:

* :func:`png_to_rgb` decodes an 8-bit PNG (which is what ``sips`` produces for
  everything this client stores) into opaque 24-bit RGB, compositing any
  transparency over white because sixel has no alpha channel.
* :func:`encode_sixel` quantises that RGB to at most 256 colours and writes the
  escape sequence.

Both are deterministic: the same picture encodes to the same bytes every time,
which is what lets the interface compare frames instead of re-sending one that
has not changed.
"""

from __future__ import annotations

import struct
import zlib
from typing import Final

_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _paeth(a: int, b: int, c: int) -> int:
    # Choose the neighbour nearest a+b-c, with PNG's specified tie order.
    p = a + b - c
    distances = (abs(p - a), abs(p - b), abs(p - c))
    return (a, b, c)[distances.index(min(distances))]


def png_to_rgb(blob: bytes) -> tuple[int, int, bytes]:
    """Decode a non-interlaced 8-bit PNG, compositing transparency over white.

    Raises :class:`ValueError` for anything it cannot read -- an interlaced or
    16-bit PNG, a bad signature, a corrupt chunk.  The caller treats that as
    "cannot draw this one" and leaves the reserved rows blank rather than
    failing the frame.
    """
    return _decode_png(blob)


def _decode_png(
    blob: bytes, background: tuple[int, int, int] = (255, 255, 255)
) -> tuple[int, int, bytes]:
    if not blob.startswith(_SIGNATURE):
        raise ValueError("Not a PNG")
    pos = 8
    header = None
    palette = b""
    transparency = b""
    compressed: list[bytes] = []
    ended = False
    while pos < len(blob):
        if pos + 12 > len(blob):
            raise ValueError("Truncated PNG chunk")
        size = struct.unpack_from(">I", blob, pos)[0]
        kind = blob[pos + 4 : pos + 8]
        end = pos + 8 + size
        if end + 4 > len(blob):
            raise ValueError("Truncated PNG chunk")
        data = blob[pos + 8 : end]
        crc = struct.unpack_from(">I", blob, end)[0]
        if zlib.crc32(kind + data) & 0xFFFFFFFF != crc:
            raise ValueError("Invalid PNG CRC")
        pos = end + 4
        if header is None and kind != b"IHDR":
            raise ValueError("Missing PNG header")
        if kind == b"IHDR":
            if header is not None or size != 13:
                raise ValueError("Invalid PNG header")
            header = struct.unpack(">IIBBBBB", data)
        elif kind == b"PLTE":
            if not size or size % 3 or size > 768:
                raise ValueError("Invalid PNG palette")
            palette = data
        elif kind == b"tRNS":
            transparency = data
        elif kind == b"IDAT":
            compressed.append(data)
        elif kind == b"IEND":
            if size:
                raise ValueError("Invalid PNG end chunk")
            ended = True
            break
        elif not kind[0] & 32:
            raise ValueError("Unsupported critical PNG chunk")
        # Other ancillary chunks (metadata, profiles, etc.) are ignored.
    if header is None or not ended or not compressed:
        raise ValueError("Incomplete PNG")
    width, height, depth, color_type, compression, filtering, interlace = header
    if depth != 8:
        raise ValueError("Only 8-bit PNG is supported")
    if interlace:
        raise ValueError("Interlaced PNG is not supported")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
    if not width or not height or channels is None or compression or filtering:
        raise ValueError("Unsupported PNG header")
    if color_type == 3 and not palette:
        raise ValueError("Indexed PNG has no palette")
    if transparency:
        if (
            color_type == 0
            and len(transparency) != 2
            or color_type == 2
            and len(transparency) != 6
            or color_type == 3
            and len(transparency) > len(palette) // 3
            or color_type in (4, 6)
        ):
            raise ValueError("Invalid PNG transparency")
    transparent_sample = None
    if transparency and color_type in (0, 2):
        transparent_sample = struct.unpack(
            ">H" if color_type == 0 else ">HHH", transparency
        )
    try:
        raw = zlib.decompress(b"".join(compressed))
    except zlib.error as exc:
        raise ValueError("Invalid PNG compressed data") from exc
    stride = width * channels
    if len(raw) != height * (stride + 1):
        raise ValueError("Invalid PNG scanline size")
    previous = bytearray(stride)
    rgb = bytearray()
    for y in range(height):
        start = y * (stride + 1)
        filter_type = raw[start]
        if filter_type > 4:
            raise ValueError("Invalid PNG filter")
        row = bytearray(raw[start + 1 : start + 1 + stride])
        for x in range(stride):
            a = row[x - channels] if x >= channels else 0
            b = previous[x]
            c = previous[x - channels] if x >= channels else 0
            if filter_type == 1:
                row[x] = (row[x] + a) & 255
            elif filter_type == 2:
                row[x] = (row[x] + b) & 255
            elif filter_type == 3:
                row[x] = (row[x] + (a + b) // 2) & 255
            elif filter_type == 4:
                row[x] = (row[x] + _paeth(a, b, c)) & 255
        for x in range(0, stride, channels):
            sample = tuple(row[x : x + channels])
            alpha = 255
            if color_type == 3:
                index = sample[0]
                if index >= len(palette) // 3:
                    raise ValueError("PNG palette index out of range")
                color = tuple(palette[index * 3 : index * 3 + 3])
                if index < len(transparency):
                    alpha = transparency[index]
            elif color_type in (0, 4):
                color = (sample[0],) * 3
                if color_type == 4:
                    alpha = sample[1]
            else:
                color = sample[:3]
                if color_type == 6:
                    alpha = sample[3]
            if sample == transparent_sample:
                alpha = 0
            rgb.extend(
                (value * alpha + bg * (255 - alpha) + 127) // 255
                for value, bg in zip(color, background)
            )
        previous = row
    return width, height, bytes(rgb)


_EXACT_COLOURS: Final = 4096
"""Above this many distinct colours, the space is collapsed before cutting.

Median-cut is superlinear in the number of distinct colours and a photograph
carries tens of thousands of them -- 57,505 in a measured 240x240 -- which cost
seconds of pure Python and froze the interface on the first picture. Rounding
each channel to four bits bounds the input at 4,096 buckets, and since the
result is cut down to 256 colours anyway the loss is not visible at the size a
terminal draws. Below the threshold the colours are kept exactly, so a picture
with a handful of them encodes to precisely those.
"""

_CHANNEL_MASK: Final = 0xF0


def _quantize(rgb: bytes, max_colors: int) -> tuple[list[tuple[int, ...]], list[int]]:
    """Median-cut quantisation, weighted by how often each colour appears."""
    counts: dict[tuple[int, int, int], int] = {}
    for i in range(0, len(rgb), 3):
        color = (rgb[i], rgb[i + 1], rgb[i + 2])
        counts[color] = counts.get(color, 0) + 1
    coarse = len(counts) > _EXACT_COLOURS
    if coarse:
        merged: dict[tuple[int, int, int], int] = {}
        for color, weight in counts.items():
            bucket = (
                color[0] & _CHANNEL_MASK,
                color[1] & _CHANNEL_MASK,
                color[2] & _CHANNEL_MASK,
            )
            merged[bucket] = merged.get(bucket, 0) + weight
        counts = merged
    colors = sorted(counts)
    palette: list[tuple[int, ...]] = []
    if len(colors) <= max_colors:
        palette.extend(colors)
        mapping = {color: i for i, color in enumerate(palette)}
    else:
        boxes = [colors]
        while len(boxes) < max_colors:
            candidates: list[tuple[int, int, int, int]] = []
            for i, box in enumerate(boxes):
                if len(box) > 1:
                    spans = [
                        max(c[k] for c in box) - min(c[k] for c in box)
                        for k in range(3)
                    ]
                    axis = max(range(3), key=lambda k: spans[k])
                    population = sum(counts[c] for c in box)
                    candidates.append((spans[axis], population, -i, axis))
            if not candidates:
                break
            _, population, negative_index, axis = max(candidates)
            index = -negative_index
            box = sorted(boxes[index], key=lambda c: (c[axis], c))
            # Split at the frequency-weighted median, keeping both halves nonempty.
            cumulative = 0
            cut = 1
            for cut in range(1, len(box)):
                cumulative += counts[box[cut - 1]]
                if cumulative * 2 >= population:
                    break
            boxes[index : index + 1] = [box[:cut], box[cut:]]
        palette = []
        for box in boxes:
            population = sum(counts[c] for c in box)
            palette.append(
                tuple(
                    (sum(c[k] * counts[c] for c in box) + population // 2) // population
                    for k in range(3)
                )
            )
        # Median-cut has already decided which box every colour fell into, and
        # the palette is those boxes in order -- so the mapping *is* that
        # membership. Searching the palette for a nearest match instead measured
        # 19.0 s on one 240x240 photo (57,505 distinct colours against 256
        # entries is 44 million distance computations), which froze the whole
        # interface on the first picture drawn.
        mapping = {}
        for i, box in enumerate(boxes):
            for c in box:
                mapping[c] = i
    if coarse:
        indices = [
            mapping[
                (
                    rgb[i] & _CHANNEL_MASK,
                    rgb[i + 1] & _CHANNEL_MASK,
                    rgb[i + 2] & _CHANNEL_MASK,
                )
            ]
            for i in range(0, len(rgb), 3)
        ]
    else:
        indices = [
            mapping[(rgb[i], rgb[i + 1], rgb[i + 2])] for i in range(0, len(rgb), 3)
        ]
    return palette, indices


def _rle(values: bytearray) -> str:
    """Run-length encode one colour's sixel bytes; ``!n`` pays off from four."""
    parts: list[str] = []
    start = 0
    while start < len(values):
        end = start + 1
        while end < len(values) and values[end] == values[start]:
            end += 1
        count = end - start
        char = chr(values[start] + 63)
        parts.append(f"!{count}{char}" if count >= 4 else char * count)
        start = end
    return "".join(parts)


def encode_sixel(
    rgb: bytes,
    width: int,
    height: int,
    *,
    background: tuple[int, int, int] = (255, 255, 255),
    max_colors: int = 256,
) -> str:
    """Return the sixel escape that draws *rgb* at the cursor.

    *background* is accepted and validated for symmetry with
    :func:`png_to_rgb`, but unused: the RGB handed in is already opaque.
    """
    if width < 1 or height < 1:
        raise ValueError("Dimensions must be positive integers")
    if len(rgb) != width * height * 3:
        raise ValueError("RGB length does not match dimensions")
    if max_colors < 1:
        raise ValueError("max_colors must be a positive integer")
    if len(background) != 3 or any(v < 0 or v > 255 for v in background):
        raise ValueError("background must contain three 8-bit components")
    palette, indices = _quantize(rgb, max_colors)
    # Raster attributes preserve the exact height of the final partial band.
    parts = [f'\x1bPq"1;1;{width};{height}']
    for index, color in enumerate(palette):
        # Sixel colour registers use percentages, not 8-bit components.
        r, g, b = (round(value * 100 / 255) for value in color)
        parts.append(f"#{index};2;{r};{g};{b}")
    for top in range(0, height, 6):
        planes: dict[int, bytearray] = {}
        for y in range(top, min(top + 6, height)):
            for x in range(width):
                index = indices[y * width + x]
                if index not in planes:
                    planes[index] = bytearray(width)
                # Bit zero is the top row; bit five is the bottom of the band.
                planes[index][x] |= 1 << (y - top)
        for n, index in enumerate(sorted(planes)):
            if n:
                parts.append("$")
            parts.append(f"#{index}" + _rle(planes[index]))
        if top + 6 < height:
            parts.append("-")
    parts.append("\x1b\\")
    return "".join(parts)
