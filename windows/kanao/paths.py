"""Locate the signed-in KakaoTalk profile and its databases on this computer.

Everything is resolved from the running machine's environment, so an installed
copy works for whoever runs it rather than for whoever built it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final, Iterator

from .errors import ErrorCode, KanaoError

CHAT_DATA_DIRECTORY: Final = "chat_data"
USER_DATABASE: Final = "TalkUserDB.edb"
CATALOG_DATABASE: Final = f"{CHAT_DATA_DIRECTORY}/chatListInfo.edb"
OPEN_LINK_DATABASE: Final = f"{CHAT_DATA_DIRECTORY}/openLinkListInfo.edb"
"""Open chat links: ``openLinkList(linkId, linkImageUrl, …)``, the open room's own picture."""
DATABASE_SUFFIX: Final = ".edb"

_STATE_FILE: Final = "appstate.dat"


def user_directory(environment: os._Environ[str] | dict[str, str] | None = None) -> Path:
    """Return the active profile directory under ``%LOCALAPPDATA%``.

    A profile counts as usable only when it has both ``appstate.dat`` and a
    ``chat_data`` directory.  When a machine carries leftover profiles from
    previous sign-ins, the most recently touched one is the active account.
    """
    env = os.environ if environment is None else environment
    local_app_data = env.get("LOCALAPPDATA")
    if not local_app_data:
        raise KanaoError(
            ErrorCode.NO_USER_DATA,
            "LOCALAPPDATA is not set, so the KakaoTalk profile directory cannot be located",
        )

    profiles_root = Path(local_app_data) / "Kakao" / "KakaoTalk" / "users"
    if not profiles_root.is_dir():
        raise KanaoError(
            ErrorCode.NO_USER_DATA,
            f"No KakaoTalk profile directory exists at {profiles_root}",
        )

    candidates = [
        profile
        for profile in profiles_root.iterdir()
        if profile.is_dir()
        and (profile / _STATE_FILE).is_file()
        and (profile / CHAT_DATA_DIRECTORY).is_dir()
    ]
    if not candidates:
        raise KanaoError(
            ErrorCode.NO_USER_DATA,
            f"No signed-in KakaoTalk profile was found under {profiles_root}; "
            "sign in to the KakaoTalk desktop client first",
        )
    return max(candidates, key=lambda profile: (profile / _STATE_FILE).stat().st_mtime_ns)


def database_paths(user_directory: Path) -> Iterator[Path]:
    """Yield every encrypted database below a profile, in a stable order.

    Walked rather than globbed: ``rglob("*")`` stats every entry in a profile
    that also holds media and caches, which measured 1.36 s against 298
    databases. ``os.walk`` already knows which entries are files, so the same
    answer costs a directory read instead.

    The order folds case here rather than leaving it to ``Path`` comparison,
    which folds on Windows and does not anywhere else. It is the same order on
    Windows either way — this only stops the answer from depending on the host
    a developer happens to be running the suite on.
    """
    found: list[Path] = []
    for directory, _subdirectories, names in os.walk(user_directory):
        for name in names:
            if os.path.splitext(name)[1].casefold() == DATABASE_SUFFIX:
                found.append(Path(directory) / name)
    yield from sorted(found, key=lambda path: str(path).casefold())


def chat_log_name(chat_id: int) -> str:
    """Return the profile-relative path of one room's message database."""
    return f"{CHAT_DATA_DIRECTORY}/chatLogs_{chat_id}.edb"


def wal_path(database: Path) -> Path:
    """Return the write-ahead log that belongs to *database*."""
    return database.with_name(database.name + "-wal")
