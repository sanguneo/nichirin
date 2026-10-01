from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .. import paths
from .console import mouse_default

_STATE_FILE = "tui_state.json"


@dataclass
class TuiSessionState:
    show_timestamps: bool = True
    mouse_clicks: bool = field(default_factory=mouse_default)
    reveal_covered: bool = False
    open_room_ids: list[int] = field(default_factory=list)
    focused_room_id: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "show_timestamps": self.show_timestamps,
            "mouse_clicks": self.mouse_clicks,
            "reveal_covered": self.reveal_covered,
            "open_room_ids": self.open_room_ids,
            "focused_room_id": self.focused_room_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TuiSessionState":
        show_timestamps = bool(data.get("show_timestamps", True))
        mouse_clicks = bool(data.get("mouse_clicks", mouse_default()))
        reveal_covered = bool(data.get("reveal_covered", False))

        raw_room_ids = data.get("open_room_ids", [])
        open_room_ids: list[int] = []
        if isinstance(raw_room_ids, list):
            for item in raw_room_ids:
                if isinstance(item, int) and item > 0:
                    open_room_ids.append(item)
                elif isinstance(item, str) and item.isdigit():
                    open_room_ids.append(int(item))

        raw_focused = data.get("focused_room_id")
        focused_room_id: Optional[int] = None
        if isinstance(raw_focused, int) and raw_focused > 0:
            focused_room_id = raw_focused
        elif isinstance(raw_focused, str) and raw_focused.isdigit():
            focused_room_id = int(raw_focused)

        return cls(
            show_timestamps=show_timestamps,
            mouse_clicks=mouse_clicks,
            reveal_covered=reveal_covered,
            open_room_ids=open_room_ids,
            focused_room_id=focused_room_id,
        )


def state_file_path(
    environment: os._Environ[str] | dict[str, str] | None = None,
) -> Path:
    return paths.state_directory(environment) / _STATE_FILE


def current_user_id(
    environment: os._Environ[str] | dict[str, str] | None = None,
) -> Optional[int]:
    """The account id ``status`` last resolved, or ``None`` when none is cached.

    Only the cached id is read: recovering it from the preferences is a
    multi-core search that has no place in starting the interface. Without one
    the session is filed under ``"default"``.
    """
    from .. import derive, keyring
    from ..errors import KamadoError

    try:
        return keyring._remembered_id(environment, derive.platform_uuid())
    except KamadoError:
        return None


def load_session(
    user_id: Optional[int] = None,
    environment: os._Environ[str] | dict[str, str] | None = None,
) -> TuiSessionState:
    path = state_file_path(environment)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return TuiSessionState()

    if not isinstance(raw, dict):
        return TuiSessionState()

    account_key = str(user_id) if user_id is not None else "default"
    accounts = raw.get("accounts")
    if isinstance(accounts, dict):
        account_data = accounts.get(account_key)
        if isinstance(account_data, dict):
            return TuiSessionState.from_dict(account_data)
        if user_id is None and len(accounts) == 1:
            single = next(iter(accounts.values()))
            if isinstance(single, dict):
                return TuiSessionState.from_dict(single)

    return TuiSessionState()


def save_session(
    state: TuiSessionState,
    user_id: Optional[int] = None,
    environment: os._Environ[str] | dict[str, str] | None = None,
) -> None:
    path = state_file_path(environment)
    account_key = str(user_id) if user_id is not None else "default"

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}

    if not isinstance(raw, dict):
        raw = {}

    accounts = raw.get("accounts")
    if not isinstance(accounts, dict):
        accounts = {}

    accounts[account_key] = state.to_dict()
    payload = {
        "version": 1,
        "accounts": accounts,
    }

    # An OSError is the caller's to report: a session that silently stops
    # being saved is a restore that silently stops working.
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    temp_path.replace(path)
