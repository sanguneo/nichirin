"""What the interface remembers between launches: two switches and the open tabs.

Kept in ``%LOCALAPPDATA%\\kanao-cli\\tui_state.json`` beside the context and
row caches, and filed per account, so a second account signed in on this
machine gets its own tabs back rather than somebody else's. It holds room ids
and two booleans. Never a title, a name or a draft: a half-written message
would otherwise sit on disk in plain text after the screen that held it closed.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

VERSION = 1
DEFAULT_ACCOUNT = "default"
"""Where a session is filed when the catalog could not say whose it is."""


def session_path() -> Path:
    """Where the interface keeps its session, outside the repository."""
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or "."
    return Path(root) / "kanao-cli" / "tui_state.json"


def _room_id(value: Any) -> Optional[int]:
    """Read one id back, written as a string or, by the macOS build, a number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


@dataclass
class SessionState:
    show_timestamps: bool = True
    reveal_covered: bool = False
    open_room_ids: list[int] = field(default_factory=list)
    """Left to right, the way the tab strip showed them."""
    focused_room_id: Optional[int] = None

    def as_json(self) -> dict[str, Any]:
        return {
            "show_timestamps": self.show_timestamps,
            "reveal_covered": self.reveal_covered,
            # Strings, like every id this tool hands out: they exceed 2**53.
            "open_room_ids": [str(room_id) for room_id in self.open_room_ids],
            "focused_room_id": None
            if self.focused_room_id is None
            else str(self.focused_room_id),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "SessionState":
        shown = data.get("show_timestamps")
        revealed = data.get("reveal_covered")
        listed = data.get("open_room_ids")
        open_ids: list[int] = []
        for value in listed if isinstance(listed, list) else []:
            room_id = _room_id(value)
            if room_id is not None and room_id not in open_ids:
                open_ids.append(room_id)
        return cls(
            show_timestamps=shown if isinstance(shown, bool) else True,
            reveal_covered=revealed if isinstance(revealed, bool) else False,
            open_room_ids=open_ids,
            focused_room_id=_room_id(data.get("focused_room_id")),
        )


def _accounts(path: Path) -> dict[str, Any]:
    """Every account's session in the file, or none when it cannot be read.

    A missing or broken file is a first launch, not an error: what it held
    was a convenience, and refusing to start over it would be worse.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    accounts = raw.get("accounts") if isinstance(raw, dict) else None
    return dict(accounts) if isinstance(accounts, dict) else {}


def load_session(path: Path, account: str) -> SessionState:
    saved = _accounts(path).get(account)
    return SessionState.from_json(saved) if isinstance(saved, dict) else SessionState()


def save_session(path: Path, account: str, state: SessionState) -> None:
    """Write *account*'s session, keeping every other account's.

    Written beside the file and moved over it, so a reader never sees half a
    file. The temporary name carries the process id: two interfaces open at
    once would otherwise write into each other's. An ``OSError`` is the
    caller's to report — on Windows the move is refused while another process
    has the file open — because a session that silently stops being saved is
    a restore that silently stops working.
    """
    accounts = _accounts(path)
    accounts[account] = state.as_json()
    payload = json.dumps({"version": VERSION, "accounts": accounts}, ensure_ascii=False, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    try:
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
