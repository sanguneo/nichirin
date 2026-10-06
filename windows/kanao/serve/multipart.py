"""``multipart/form-data`` for ``POST /upload``, strictly and in memory.

Python 3.13 removed ``cgi``, and ``email`` would hold the body several times
over. The one request that needs this carries two named fields and one file,
and its length is bounded before a byte of it is read, so a strict split of
the body that was read is the whole job. Anything this does not understand is
refused rather than guessed at.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from typing import Optional

from ..errors import ErrorCode, KanaoError

MAX_PARTS = 8
"""More parts than any form this server reads, so a body of tiny parts is refused early."""

_BOUNDARY = re.compile(r'boundary=(?:"([^"]{1,70})"|([^\s;,"]{1,70}))', re.IGNORECASE)
_PARAMETER = re.compile(r';\s*([A-Za-z0-9_*.-]+)\s*=\s*("((?:[^"\\]|\\.)*)"|[^;]*)')


@dataclass(frozen=True)
class Part:
    """One part of a form: its field name, file name if it is a file, and bytes."""

    name: str
    filename: Optional[str]
    content_type: Optional[str]
    data: bytes


def _refuse(message: str) -> KanaoError:
    return KanaoError(ErrorCode.INVALID_REQUEST, message)


def boundary_of(content_type: Optional[str]) -> str:
    """The boundary a ``multipart/form-data`` content type names."""
    kind, _, rest = (content_type or "").partition(";")
    if kind.strip().lower() != "multipart/form-data":
        raise _refuse("the body must be multipart/form-data")
    match = _BOUNDARY.search(rest)
    if match is None:
        raise _refuse("multipart/form-data needs a boundary")
    return match.group(1) or match.group(2)


def _disposition(value: str) -> tuple[str, Optional[str]]:
    kind, _, _rest = value.partition(";")
    if kind.strip().lower() != "form-data":
        raise _refuse("every part must be form-data")
    parameters: dict[str, str] = {}
    for match in _PARAMETER.finditer(value[len(kind):]):
        key = match.group(1).lower()
        if match.group(3) is not None:
            text = re.sub(r"\\(.)", r"\1", match.group(3))
        else:
            text = match.group(2).strip()
        if key in parameters:
            raise _refuse(f"a part names {key} twice")
        parameters[key] = text
    name = parameters.get("name")
    if not name:
        raise _refuse("every part needs a name")
    filename = parameters.get("filename")
    extended = parameters.get("filename*")
    if extended is not None:
        charset, _, encoded = extended.partition("''")
        if charset.lower() != "utf-8":
            raise _refuse("filename* must be UTF-8")
        filename = urllib.parse.unquote(encoded, encoding="utf-8", errors="strict")
    return name, filename


def _part(head: bytes, data: bytes) -> Part:
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        raise _refuse("part headers must be UTF-8")
    disposition: Optional[str] = None
    content_type: Optional[str] = None
    for line in text.split("\r\n"):
        key, colon, value = line.partition(":")
        if not colon:
            raise _refuse("a part header has no colon")
        key = key.strip().lower()
        if key == "content-disposition":
            if disposition is not None:
                raise _refuse("a part has two Content-Disposition headers")
            disposition = value.strip()
        elif key == "content-type":
            content_type = value.strip()
    if disposition is None:
        raise _refuse("every part needs Content-Disposition")
    name, filename = _disposition(disposition)
    return Part(name=name, filename=filename, content_type=content_type, data=data)


def parse(body: bytes, boundary: str) -> list[Part]:
    """Split *body* at *boundary* into its parts, or refuse it whole."""
    delimiter = b"--" + boundary.encode("ascii", errors="strict")
    if not body.startswith(delimiter):
        raise _refuse("the body does not start with its boundary")
    position = len(delimiter)
    parts: list[Part] = []
    while True:
        if body.startswith(b"--", position):
            return parts
        if not body.startswith(b"\r\n", position):
            raise _refuse("a boundary is not followed by a line break")
        start = position + 2
        head_end = body.find(b"\r\n\r\n", start)
        if head_end < 0:
            raise _refuse("a part's headers never end")
        content_start = head_end + 4
        content_end = body.find(b"\r\n" + delimiter, content_start)
        if content_end < 0:
            raise _refuse("the body ends before its closing boundary")
        parts.append(_part(body[start:head_end], body[content_start:content_end]))
        if len(parts) > MAX_PARTS:
            raise _refuse("the form has too many parts")
        position = content_end + 2 + len(delimiter)
