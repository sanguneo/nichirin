"""The HTTP half of ``POST /upload``: the form, the name, and the staged copy.

Nothing here touches the client. A multipart body is taken apart strictly —
exactly the parts the contract names, each once — the file's name is reduced
to one plain file name, and the bytes are written where the client can read
them after the request has answered.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import time
import urllib.parse
from pathlib import Path
from typing import Callable, NamedTuple, Optional

from ..errors import ErrorCode, KamadoError
from .config import UPLOAD_KEEP_SECONDS

MAX_NAME_LENGTH = 120

_FORBIDDEN = re.compile(r'[<>:"|?*\x00-\x1f\x7f/\\]')
_DEVICE_NAMES = frozenset(
    ["con", "prn", "aux", "nul"]
    + [f"com{n}" for n in range(1, 10)]
    + [f"lpt{n}" for n in range(1, 10)]
)
_PARTS = ("room", "file", "openRoom")
_BOUNDARY = re.compile(r'boundary=(?:"([^"]+)"|([^\s;]+))', re.IGNORECASE)
_PARAM = re.compile(r';\s*([A-Za-z*]+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^;\s]*))')


def _refuse(message: str) -> KamadoError:
    return KamadoError(ErrorCode.INVALID_REQUEST, message)


def sanitize_name(raw: str) -> str:
    """Reduce *raw* to one file name the room can be shown.

    Folders are dropped (either separator), then the characters a Windows
    file name cannot hold and every control character; a device name gets a
    leading ``_``; trailing dots and spaces go; and a name past 120 characters
    is cut from the stem so its extension survives. The same rules as the
    Windows build, so a name the room sees does not depend on the host.
    """
    name = re.split(r"[/\\]", raw)[-1]
    name = _FORBIDDEN.sub("", name).strip().rstrip(". ")
    if not name or set(name) <= {"."}:
        raise _refuse("the file needs a name")
    stem, dot, extension = name.rpartition(".")
    if not dot:
        stem, extension = name, ""
    if stem.casefold() in _DEVICE_NAMES or (not dot and name.casefold() in _DEVICE_NAMES):
        name = "_" + name
        stem = "_" + stem
    if len(name) > MAX_NAME_LENGTH:
        suffix = f".{extension}" if dot and len(extension) < MAX_NAME_LENGTH // 2 else ""
        name = stem[: MAX_NAME_LENGTH - len(suffix)].rstrip(". ") + suffix
    return name


class UploadForm(NamedTuple):
    room: str
    name: str
    data: bytes
    open_room: bool


def _params(header: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for key, quoted, bare in _PARAM.findall(header):
        value = re.sub(r"\\(.)", r"\1", quoted) if quoted else bare
        found[key.lower()] = value
    return found


def _part_name_and_file(head: bytes) -> tuple[str, Optional[str]]:
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        raise _refuse("a part's headers are not UTF-8")
    disposition = None
    for line in text.split("\r\n"):
        key, _, value = line.partition(":")
        if key.strip().lower() == "content-disposition":
            disposition = value.strip()
    if disposition is None or not disposition.lower().startswith("form-data"):
        raise _refuse("every part needs a form-data Content-Disposition")
    params = _params(disposition)
    if "name" not in params:
        raise _refuse("a part has no name")
    filename = params.get("filename")
    extended = params.get("filename*")
    if extended:
        charset, _, rest = extended.partition("''")
        if charset.lower() == "utf-8":
            filename = urllib.parse.unquote(rest, encoding="utf-8", errors="strict")
    return params["name"], filename


def parse_form(content_type: Optional[str], body: bytes, max_file_bytes: int) -> UploadForm:
    """Return the one upload *body* describes, or refuse it whole.

    Only ``room``, ``file`` and ``openRoom`` are taken, each at most once;
    anything else is a ``400`` rather than something quietly ignored.
    """
    if not content_type or not content_type.lower().startswith("multipart/form-data"):
        raise _refuse("an upload is a multipart/form-data body")
    match = _BOUNDARY.search(content_type)
    if match is None:
        raise _refuse("the multipart body names no boundary")
    delimiter = b"--" + (match.group(1) or match.group(2)).encode("latin-1")
    if not body.startswith(delimiter):
        raise _refuse("the multipart body does not start with its boundary")
    parts: dict[str, tuple[Optional[str], bytes]] = {}
    position = len(delimiter)
    while True:
        marker = body[position : position + 2]
        if marker == b"--":
            break
        if marker != b"\r\n":
            raise _refuse("the multipart body is malformed")
        end = body.find(b"\r\n" + delimiter, position + 2)
        if end < 0:
            raise _refuse("the multipart body ends before its closing boundary")
        head, separator, content = body[position + 2 : end].partition(b"\r\n\r\n")
        if not separator:
            raise _refuse("a part has no header block")
        name, filename = _part_name_and_file(head)
        if name not in _PARTS:
            raise _refuse(f"unexpected part {name!r}")
        if name in parts:
            raise _refuse(f"part {name!r} was sent twice")
        parts[name] = (filename, content)
        position = end + 2 + len(delimiter)

    if "room" not in parts or "file" not in parts:
        raise _refuse("an upload needs a room and a file")
    try:
        room = parts["room"][1].decode("utf-8")
    except UnicodeDecodeError:
        raise _refuse("room is not UTF-8")
    if not room.strip():
        raise _refuse("a nonempty room is required")
    filename, data = parts["file"]
    if filename is None:
        raise _refuse("the file part needs a file name")
    if not data:
        raise _refuse("the file is empty")
    if len(data) > max_file_bytes:
        raise _refuse(f"the file is larger than {max_file_bytes} bytes")
    flag = parts.get("openRoom", (None, b"false"))[1]
    if flag not in (b"true", b"false"):
        raise _refuse('openRoom must be "true" or "false"')
    return UploadForm(room, sanitize_name(filename), data, flag == b"true")


class UploadStage:
    """Where uploaded bytes wait for the client to read them.

    Each upload gets a directory of its own under *root*, so two files of one
    name never meet. The client reads the path after its confirmation closes,
    which is after the request answered, so a copy is kept for
    ``UPLOAD_KEEP_SECONDS`` and swept by a later upload rather than deleted
    on the way out.
    """

    def __init__(self, root: Path, clock: Callable[[], float] = time.time) -> None:
        self._root = root
        self._clock = clock

    def stage(self, name: str, data: bytes) -> Path:
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        folder = self._root / secrets.token_hex(8)
        folder.mkdir(mode=0o700)
        path = folder / name
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        return path

    def sweep(self) -> None:
        if not self._root.is_dir():
            return
        cutoff = self._clock() - UPLOAD_KEEP_SECONDS
        for folder in self._root.iterdir():
            try:
                if folder.is_dir() and not folder.is_symlink() and folder.stat().st_mtime < cutoff:
                    shutil.rmtree(folder)
            except OSError:
                # A copy that cannot be removed now is tried again next time.
                continue
