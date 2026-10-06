"""Where a file message's bytes come from: the client's own copy, or its address.

KakaoTalk stores an address for a file, not the file. When somebody saves one
in the client it lands in ``Documents\\카카오톡 받은 파일``, and that copy is
preferred: it costs no network and outlives the address. Otherwise the address
is fetched, and only on the client's own hosts — the same whitelist the
interface's photos go through.

Measured on this account: a live ``talk.kakaocdn.net`` address answered a
ranged ``GET`` with 206, an expired one 410, and an expired
``dn.talk.kakao.com`` one 403. An expired address is therefore
``FILE_EXPIRED`` whatever the host says, and it is decided from the
attachment's own ``expire`` before anything is sent.
"""

from __future__ import annotations

import glob
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Final, Optional

from .errors import ErrorCode, KanaoError
from .model import Attachment

RECEIVED_DIRECTORY_NAME: Final = "카카오톡 받은 파일"
ALLOWED_HOSTS: Final = ("talk.kakaocdn.net", "dn-talk.kakaocdn.net", "dn.talk.kakao.com")
FETCH_TIMEOUT_SECONDS: Final = 30.0
_GONE_STATUSES: Final = (403, 404, 410)
"""What an address that stopped answering says; measured 410 and 403."""


def received_directory() -> Path:
    """Where the client saves a file somebody chose to keep."""
    return Path.home() / "Documents" / RECEIVED_DIRECTORY_NAME


def safe_name(name: Optional[str]) -> Optional[str]:
    """The file name alone, or None when there is nothing safe to look for.

    The name is whatever the sender's client wrote, so a separator in it is a
    path this process must not follow out of the directory it searches.
    """
    if not name:
        return None
    base = name.replace("\\", "/").split("/")[-1].strip()
    if not base or base in (".", ".."):
        return None
    return base


def local_copy(attachment: Attachment, directory: Optional[Path] = None) -> Optional[Path]:
    """The client's saved copy of this file, matched by name and size.

    The client names a second copy ``name (1).ext``, so those are tried after
    the plain name. Size has to agree exactly: a name alone is the easiest
    thing for two different files to share. A link is never a copy: followed,
    it would hand a remote ``/file`` caller whatever it points at.
    """
    base = safe_name(attachment.name)
    if base is None or attachment.size is None:
        return None
    folder = directory if directory is not None else received_directory()
    stem, dot, suffix = base.rpartition(".")
    if not dot:
        stem, suffix = base, ""
    else:
        suffix = "." + suffix
    candidates = [folder / base] + sorted(
        Path(found)
        for found in glob.glob(str(folder / f"{glob.escape(stem)} (*){glob.escape(suffix)}"))
    )
    for candidate in candidates:
        try:
            if (
                candidate.parent == folder
                and not candidate.is_symlink()
                and candidate.is_file()
                and candidate.stat().st_size == attachment.size
            ):
                return candidate
        except OSError:
            continue
    return None


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
    directory: Optional[Path] = None,
    now: Callable[[], float] = time.time,
    opener: Callable[..., BinaryIO] = urllib.request.urlopen,  # type: ignore[assignment]
) -> Download:
    """Open a file message's bytes, preferring the copy already on this disk."""
    name = safe_name(attachment.name) or "file"
    found = local_copy(attachment, directory)
    if found is not None:
        return Download(name=name, size=attachment.size, stream=open(found, "rb"), source="local")
    if not allowed(attachment.url):
        raise KanaoError(
            ErrorCode.READ_FAILED, "the file's address is not on the client's own hosts"
        )
    if attachment.expires_at is not None and attachment.expires_at < now() * 1000:
        raise KanaoError(
            ErrorCode.FILE_EXPIRED,
            "the file's address has expired and no saved copy was found; "
            "save it in the client to keep it",
        )
    request = urllib.request.Request(attachment.url, headers={"User-Agent": "kanao-cli"})
    try:
        response = opener(request, timeout=FETCH_TIMEOUT_SECONDS)
    except urllib.error.HTTPError as error:
        if error.code in _GONE_STATUSES:
            raise KanaoError(
                ErrorCode.FILE_EXPIRED, f"the file's address no longer answers ({error.code})"
            ) from error
        raise KanaoError(ErrorCode.READ_FAILED, f"fetching the file failed ({error.code})") from error
    except (urllib.error.URLError, OSError) as error:
        raise KanaoError(ErrorCode.READ_FAILED, f"fetching the file failed: {error}") from error
    declared = getattr(response, "headers", None)
    length = declared.get("Content-Length") if declared is not None else None
    size = int(length) if length is not None and str(length).isdigit() else attachment.size
    return Download(name=name, size=size, stream=response, source="remote")
