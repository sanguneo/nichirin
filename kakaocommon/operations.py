"""Durable, opt-in at-most-once write receipts (Python 3.9+)."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Optional


class OperationError(Exception):
    """An operation refusal or the recorded failure of a previous attempt."""

    def __init__(self, envelope: dict[str, Any]) -> None:
        super().__init__(envelope["error"]["message"])
        self.envelope = envelope


def _failure(code: str, message: str, delivery: str, retry: str) -> OperationError:
    return OperationError({"ok": False, "error": {
        "code": code, "message": message,
        "disposition": {"delivery": delivery, "retry": retry},
    }})


def validate_id(value: Optional[str]) -> Optional[str]:
    """Bound the caller's identifier; its hash, never its spelling, is a path."""
    if value is not None and (
        not isinstance(value, str) or not 1 <= len(value) <= 128
        or not all(33 <= ord(char) <= 126 for char in value)
    ):
        raise _failure("INVALID_REQUEST", "operationId must be 1-128 printable ASCII characters without spaces",
                       "not_delivered", "safe")
    return value


def directory(build: str) -> Path:
    """Keep receipts in the current OS user's application data, per build."""
    if os.name == "nt":
        return Path(os.environ["LOCALAPPDATA"]) / f"{build}-cli" / "operations"
    return Path.home() / "Library" / "Application Support" / f"{build}-cli" / "operations"


def fingerprint(kind: str, room: str, body: bytes, **options: Any) -> str:
    """Bind the exact selector, payload hash, and behavior-affecting options."""
    encoded = json.dumps(
        [kind, room, hashlib.sha256(body).hexdigest(), options],
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _write(path: Path, record: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            # Also commit the rename and the claim directory before any input.
            with_dir = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(with_dir)
            finally:
                os.close(with_dir)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _recorded(path: Path, digest: str) -> dict[str, Any]:
    unknown = _failure("SEND_FAILED", "the recorded operation may have been submitted; read the room before retrying",
                       "unknown", "after_check")
    try:
        with path.open(encoding="utf-8") as stream:
            saved = json.load(stream)
    except (OSError, ValueError):
        raise unknown from None
    if saved["fingerprint"] != digest:
        raise _failure("INVALID_REQUEST", "operationId was already used for a different request",
                       "not_delivered", "safe")
    if saved["phase"] == "dispatching":
        raise unknown
    if "failure" in saved:
        raise OperationError(saved["failure"])
    return saved["result"]


def replay(operation_id: Optional[str], digest: str, *, root: Path) -> Optional[dict[str, Any]]:
    """Resolve existing receipts before a retry competes for a new-write slot."""
    validate_id(operation_id)
    if operation_id is None:
        return None
    claim = root / hashlib.sha256(operation_id.encode("ascii")).hexdigest()
    if not claim.exists():
        return None
    return _recorded(claim / "receipt.json", digest)


def run(
    operation_id: Optional[str],
    digest: str,
    action: Callable[[], Mapping[str, Any]],
    *,
    root: Path,
    failure: Callable[[Exception], dict[str, Any]],
    sensitive_text: str = "",
) -> dict[str, Any]:
    """Claim once before input; replay without calling *action*, even after a crash.

    The atomic directory creation is the cross-process claim. An empty or
    unreadable claim is uncertain, never permission to send again. Receipts
    have no expiry: deleting one explicitly forfeits its duplicate protection.
    """
    validate_id(operation_id)
    if operation_id is None:
        return dict(action())
    claim = root / hashlib.sha256(operation_id.encode("ascii")).hexdigest()
    path = claim / "receipt.json"
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            claim.mkdir(mode=0o700)
        except FileExistsError:
            return _recorded(path, digest)
        record: dict[str, Any] = {"fingerprint": digest, "phase": "dispatching"}
        _write(path, record)
        if os.name != "nt":
            parent = os.open(root, os.O_RDONLY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
    except OSError as error:
        raise _failure("SEND_FAILED", f"could not record operation before input: {error}",
                       "not_delivered", "safe") from error
    try:
        result = dict(action())
    except Exception as error:  # write boundary: preserve the recorded disposition
        envelope = failure(error)
        if sensitive_text:
            # Native/backend errors can quote what they were asked to type.
            envelope["error"]["message"] = envelope["error"]["message"].replace(
                sensitive_text, "[redacted]"
            )
        record.update(phase=envelope["error"]["disposition"].get("delivery", "unknown"),
                      failure=envelope)
        try:
            _write(path, record)
        except OSError:
            pass  # dispatching remains durable; never retry the action
        raise OperationError(envelope) from error
    record.update(phase="submitted" if result.get("submitted") else "not_delivered",
                  result=result)
    try:
        _write(path, record)
    except OSError:
        pass  # Known success stays success; replay conservatively remains unknown.
    return result
