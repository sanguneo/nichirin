"""Decode an image to pixels through the Windows Imaging Component.

Sixel carries raw pixels, not a file, so a photo has to be taken apart before
it can be drawn -- and what the client stores is JPEG and WebP, which the
standard library cannot read. The macOS sibling shells out to ``sips``; Windows
ships no such command, so the decoders already in the operating system are
called directly, the same shape as the CNG binding in :mod:`kanao.cng`: loaded
lazily so importing this module off Windows still works, and every handle
released on the way out.

Unlike CNG, WIC is COM, so the calls go through each interface's vtable rather
than through flat exports. The vtable indices below are the interface layouts,
counting the three :class:`IUnknown` slots every interface starts with; getting
one wrong is a wild call rather than an error, which is why each is named.

Nothing here is KakaoTalk-specific. It answers one question: what are the
pixels of these bytes.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Final, Optional

_CLSID_WIC_IMAGING_FACTORY: Final = "cacaf262-9370-4615-a13b-9f5539da4c0a"
_IID_WIC_IMAGING_FACTORY: Final = "ec5ec8a9-c395-4314-9c77-54d7a935ff70"

_GUID_24BPP_RGB: Final = "6fddc324-4e03-4bfe-b185-3d77768dc90d"
"""``GUID_WICPixelFormat24bppRGB`` -- three bytes a pixel, in the order sixel
and the kitty protocol both want, so no channel swap is needed afterwards."""

_CLSCTX_INPROC_SERVER: Final = 1
_COINIT_APARTMENTTHREADED: Final = 0x2
_RPC_E_CHANGED_MODE: Final = -2147417850  # 0x80010106

# IWICImagingFactory, past IUnknown's QueryInterface/AddRef/Release.
_FACTORY_CREATE_DECODER_FROM_STREAM: Final = 4
_FACTORY_CREATE_FORMAT_CONVERTER: Final = 10
_FACTORY_CREATE_STREAM: Final = 14

# IWICStream inherits IStream, which inherits ISequentialStream.
_STREAM_INITIALIZE_FROM_MEMORY: Final = 16

# IWICBitmapDecoder.
_DECODER_GET_FRAME: Final = 13

# IWICBitmapSource, shared by frames and by the format converter.
_SOURCE_GET_SIZE: Final = 3
_SOURCE_COPY_PIXELS: Final = 7
# IWICFormatConverter adds its own past that.
_CONVERTER_INITIALIZE: Final = 8

_RELEASE: Final = 2

_WIC_BITMAP_DITHER_TYPE_NONE: Final = 0
_WIC_BITMAP_PALETTE_TYPE_CUSTOM: Final = 0

_PUBYTE = ctypes.POINTER(ctypes.c_ubyte)


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __init__(self, text: str) -> None:
        super().__init__()
        parts = text.strip("{}").split("-")
        self.Data1 = int(parts[0], 16)
        self.Data2 = int(parts[1], 16)
        self.Data3 = int(parts[2], 16)
        for index, byte in enumerate(bytes.fromhex(parts[3] + parts[4])):
            self.Data4[index] = byte


_ole32: Optional[ctypes.CDLL] = None


def _com() -> ctypes.CDLL:
    """Load ole32 and join the apartment once, lazily.

    ``RPC_E_CHANGED_MODE`` means this thread is already in an apartment of the
    other kind, which is somebody else's decision and not an error here: the
    calls below work either way.
    """
    global _ole32
    if _ole32 is not None:
        return _ole32
    library = ctypes.WinDLL("ole32")
    library.CoInitializeEx.argtypes = (ctypes.c_void_p, wintypes.DWORD)
    library.CoInitializeEx.restype = ctypes.c_long
    library.CoCreateInstance.argtypes = (
        ctypes.POINTER(_GUID),
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_GUID),
        ctypes.POINTER(ctypes.c_void_p),
    )
    library.CoCreateInstance.restype = ctypes.c_long
    status = library.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
    if status < 0 and status != _RPC_E_CHANGED_MODE:
        raise ValueError(f"COM could not be started: 0x{status & 0xFFFFFFFF:08x}")
    _ole32 = library
    return library


def _call(
    interface: ctypes.c_void_p, index: int, *arguments: object, types: tuple
) -> int:
    """Call one vtable slot on a COM interface pointer."""
    table = ctypes.cast(interface, ctypes.POINTER(ctypes.c_void_p))[0]
    function = ctypes.cast(table, ctypes.POINTER(ctypes.c_void_p))[index]
    prototype = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *types)
    return prototype(function)(interface, *arguments)


def _check(status: int, what: str) -> None:
    if status < 0:
        raise ValueError(f"{what} failed: 0x{status & 0xFFFFFFFF:08x}")


def available() -> bool:
    """Whether this machine can decode at all, without raising if it cannot."""
    try:
        _com()
    except (OSError, AttributeError, ValueError):
        return False
    return True


def decode(blob: bytes) -> tuple[int, int, bytes]:
    """Return ``(width, height, rgb)`` for *blob*, three bytes a pixel.

    Reads whatever WIC has a decoder for, which covers the JPEG, PNG, GIF and
    (on current Windows) WebP this client stores. Raises :class:`ValueError`
    for bytes it cannot read, which is what the caller treats as "cannot draw
    this one" -- the same contract as :func:`kanao.tui.sixel.png_to_rgb`.
    """
    if not blob:
        raise ValueError("no image bytes")
    library = _com()
    factory = ctypes.c_void_p()
    _check(
        library.CoCreateInstance(
            ctypes.byref(_GUID(_CLSID_WIC_IMAGING_FACTORY)),
            None,
            _CLSCTX_INPROC_SERVER,
            ctypes.byref(_GUID(_IID_WIC_IMAGING_FACTORY)),
            ctypes.byref(factory),
        ),
        "creating the imaging factory",
    )
    opened: list[ctypes.c_void_p] = [factory]
    try:
        stream = ctypes.c_void_p()
        _check(
            _call(
                factory,
                _FACTORY_CREATE_STREAM,
                ctypes.byref(stream),
                types=(ctypes.POINTER(ctypes.c_void_p),),
            ),
            "creating a stream",
        )
        opened.append(stream)

        # The buffer has to outlive the stream, which reads from it lazily.
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        _check(
            _call(
                stream,
                _STREAM_INITIALIZE_FROM_MEMORY,
                buffer,
                len(blob),
                types=(_PUBYTE, ctypes.c_uint),
            ),
            "reading the image from memory",
        )

        decoder = ctypes.c_void_p()
        _check(
            _call(
                factory,
                _FACTORY_CREATE_DECODER_FROM_STREAM,
                stream,
                None,
                0,
                ctypes.byref(decoder),
                types=(
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                    ctypes.c_uint,
                    ctypes.POINTER(ctypes.c_void_p),
                ),
            ),
            "finding a decoder for these bytes",
        )
        opened.append(decoder)

        frame = ctypes.c_void_p()
        _check(
            _call(
                decoder,
                _DECODER_GET_FRAME,
                0,
                ctypes.byref(frame),
                types=(ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)),
            ),
            "reading the first frame",
        )
        opened.append(frame)

        width = ctypes.c_uint()
        height = ctypes.c_uint()
        _check(
            _call(
                frame,
                _SOURCE_GET_SIZE,
                ctypes.byref(width),
                ctypes.byref(height),
                types=(ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)),
            ),
            "measuring the frame",
        )
        if width.value <= 0 or height.value <= 0:
            raise ValueError("the image has no pixels")

        converter = ctypes.c_void_p()
        _check(
            _call(
                factory,
                _FACTORY_CREATE_FORMAT_CONVERTER,
                ctypes.byref(converter),
                types=(ctypes.POINTER(ctypes.c_void_p),),
            ),
            "creating a format converter",
        )
        opened.append(converter)
        _check(
            _call(
                converter,
                _CONVERTER_INITIALIZE,
                frame,
                ctypes.byref(_GUID(_GUID_24BPP_RGB)),
                _WIC_BITMAP_DITHER_TYPE_NONE,
                None,
                0.0,
                _WIC_BITMAP_PALETTE_TYPE_CUSTOM,
                types=(
                    ctypes.c_void_p,
                    ctypes.POINTER(_GUID),
                    ctypes.c_int,
                    ctypes.c_void_p,
                    ctypes.c_double,
                    ctypes.c_int,
                ),
            ),
            "converting to 24-bit RGB",
        )

        stride = width.value * 3
        size = stride * height.value
        pixels = (ctypes.c_ubyte * size)()
        _check(
            _call(
                converter,
                _SOURCE_COPY_PIXELS,
                None,
                stride,
                size,
                pixels,
                types=(ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, _PUBYTE),
            ),
            "copying the pixels out",
        )
        return width.value, height.value, bytes(pixels)
    finally:
        for interface in reversed(opened):
            if interface:
                _call(interface, _RELEASE, types=())
