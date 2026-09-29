"""Where a file message's bytes come from: the client's own copy, or its address.

KakaoTalk stores an address for a file, not the file. When somebody saves one
in this client it writes where it put it into ``NTChatMessage.localFilePath``
— an absolute path the user chose, ``~/Downloads`` and ``~/Desktop`` for 31 of
the 36 rows measured — and that copy is preferred: it costs no network and
outlives the address. The path is only a hint. 35 of those 36 files had since
been moved or deleted, so the copy is used only when it is still a regular
file of exactly the size the message recorded.

Otherwise the address is fetched, and only on the client's own hosts — the
same whitelist the interface's photos go through. The Windows build measured
a live ``talk.kakaocdn.net`` address answering 206, an expired one 410 and an
expired ``dn.talk.kakao.com`` one 403, so an expired address is
``FILE_EXPIRED`` whatever the host says, and it is decided from the
attachment's own ``expire`` before anything is sent.
"""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Final, Optional

from .errors import ErrorCode, KamadoError
from .model import Attachment

ALLOWED_HOSTS: Final = ("talk.kakaocdn.net", "dn-talk.kakaocdn.net", "dn.talk.kakao.com")
FETCH_TIMEOUT_SECONDS: Final = 30.0
_GONE_STATUSES: Final = (403, 404, 410)
"""What an address that stopped answering says; measured 410 and 403."""


def safe_name(name: Optional[str]) -> Optional[str]:
    """The file name alone, or None when there is nothing safe to call it.

    The name is whatever the sender's client wrote, so a separator in it is a
    path, and it reaches a download header rather than this disk.
    """
    if not name:
        return None
    base = name.replace("\\", "/").split("/")[-1].strip()
    if not base or base in (".", ".."):
        return None
    return base


def local_copy(attachment: Attachment) -> Optional[Path]:
    """The client's saved copy of this file, when it is still what was sent.

    The path is the client's record of a save, not a promise that the file is
    still there or still the same one: a regular file of the recorded size is
    required, and a symlink is not followed to somewhere else.
    """
    if not attachment.local_path or attachment.size is None:
        return None
    candidate = Path(attachment.local_path)
    if not candidate.is_absolute():
        return None
    try:
        if candidate.is_symlink() or not candidate.is_file():
            return None
        if candidate.stat().st_size != attachment.size:
            return None
    except OSError:
        return None
    return candidate


@dataclass
class Download:
    """An open file, whichever place it came from; the caller closes it."""

    name: str
    size: Optional[int]
    stream: BinaryIO
    source: str
    """``local`` for the client's saved copy, ``remote`` for the address."""

    def close(self) -> None:
        try:
            self.stream.close()
        except OSError:
            pass


def allowed(url: str) -> bool:
    split = urllib.parse.urlsplit(url)
    return (
        split.scheme == "https"
        and split.hostname is not None
        and split.hostname.lower() in ALLOWED_HOSTS
    )


def open_file(
    attachment: Attachment,
    now: Callable[[], float] = time.time,
    opener: Callable[..., BinaryIO] = urllib.request.urlopen,  # type: ignore[assignment]
) -> Download:
    """Open a file message's bytes, preferring the copy already on this disk."""
    name = safe_name(attachment.name) or "file"
    found = local_copy(attachment)
    if found is not None:
        try:
            # O_NOFOLLOW: the check above and the open below are two looks at
            # a path somebody else controls, and the second must not be led
            # through a link the first did not see.
            descriptor = os.open(found, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            descriptor = -1
        if descriptor >= 0:
            return Download(
                name=name,
                size=attachment.size,
                stream=os.fdopen(descriptor, "rb"),
                source="local",
            )
    if not allowed(attachment.url):
        raise KamadoError(
            ErrorCode.READ_FAILED, "the file's address is not on the client's own hosts"
        )
    if attachment.expires_at is not None and attachment.expires_at < now() * 1000:
        raise KamadoError(
            ErrorCode.FILE_EXPIRED,
            "the file's address has expired and no saved copy was found; "
            "save it in the client to keep it",
        )
    request = urllib.request.Request(attachment.url, headers={"User-Agent": "kamado-cli"})
    try:
        response = opener(request, timeout=FETCH_TIMEOUT_SECONDS)
    except urllib.error.HTTPError as error:
        if error.code in _GONE_STATUSES:
            raise KamadoError(
                ErrorCode.FILE_EXPIRED, f"the file's address no longer answers ({error.code})"
            ) from error
        raise KamadoError(
            ErrorCode.READ_FAILED, f"fetching the file failed ({error.code})"
        ) from error
    except (urllib.error.URLError, OSError) as error:
        raise KamadoError(ErrorCode.READ_FAILED, f"fetching the file failed: {error}") from error
    declared = getattr(response, "headers", None)
    length = declared.get("Content-Length") if declared is not None else None
    size = int(length) if length is not None and str(length).isdigit() else attachment.size
    return Download(name=name, size=size, stream=response, source="remote")
