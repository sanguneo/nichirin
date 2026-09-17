from __future__ import annotations

import json
import os
import signal
import socket
import sys
import time
from pathlib import Path
from typing import Any, Optional

from . import paths
from .context import DEFAULT_LIMIT, read_context
from .errors import ErrorCode, KamadoError, as_envelope, success
from .names import NameLedger
from .reader import Reader

SOCKET_FILE = "daemon.sock"
PID_FILE = "daemon.pid"


def socket_path(environment: Any = None) -> Path:
    return paths.state_directory(environment) / SOCKET_FILE


def pid_path(environment: Any = None) -> Path:
    return paths.state_directory(environment) / PID_FILE


def is_daemon_running(environment: Any = None) -> bool:
    sock_path = socket_path(environment)
    if not sock_path.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            sock.connect(str(sock_path))
            sock.sendall(json.dumps({"command": "ping"}).encode("utf-8") + b"\n")
            response = sock.recv(4096)
            data = json.loads(response.decode("utf-8"))
            return bool(data.get("ok"))
    except (OSError, ValueError):
        return False


def send_to_daemon(
    command: str, args: dict[str, Any], environment: Any = None, timeout: float = 5.0
) -> Optional[dict[str, Any]]:
    sock_path = socket_path(environment)
    if not sock_path.exists():
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(sock_path))
            payload = json.dumps({"command": command, "args": args}, ensure_ascii=False)
            sock.sendall(payload.encode("utf-8") + b"\n")

            buffer = bytearray()
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buffer.extend(chunk)
                if b"\n" in chunk:
                    break
            if not buffer:
                return None
            return json.loads(buffer.decode("utf-8"))
    except (OSError, ValueError):
        return None


class DaemonServer:
    def __init__(self, environment: Any = None) -> None:
        self.environment = environment
        self.sock_path = socket_path(environment)
        self.p_path = pid_path(environment)
        self.running = False
        self._reader: Optional[Reader] = None

    def start(self, foreground: bool = False) -> int:
        if is_daemon_running(self.environment):
            print(
                json.dumps(
                    {"ok": True, "status": "already_running", "socket": str(self.sock_path)},
                    ensure_ascii=False,
                )
            )
            return 0

        self.sock_path.parent.mkdir(parents=True, exist_ok=True)
        if self.sock_path.exists():
            self.sock_path.unlink()

        if not foreground:
            pid = os.fork()
            if pid > 0:
                deadline = time.time() + 5.0
                while time.time() < deadline:
                    if is_daemon_running(self.environment):
                        print(
                            json.dumps(
                                {"ok": True, "status": "started", "pid": pid, "socket": str(self.sock_path)},
                                ensure_ascii=False,
                            )
                        )
                        return 0
                    time.sleep(0.05)
                print(
                    json.dumps(
                        {"ok": False, "error": {"code": "START_TIMEOUT", "message": "daemon startup timed out"}},
                        ensure_ascii=False,
                    )
                )
                return 1
            os.setsid()
            devnull = os.open(os.devnull, os.O_RDWR)
            os.dup2(devnull, 0)
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
            os.close(devnull)

        self.p_path.write_text(str(os.getpid()), encoding="utf-8")

        def handle_signal(_signum, _frame):
            self.running = False

        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)

        self.running = True
        try:
            with Reader(ledger=NameLedger()) as reader:
                self._reader = reader
                try:
                    reader.status()
                except Exception:
                    pass
                self._serve()
        finally:
            self._cleanup()
        return 0

    def stop(self) -> int:
        res = send_to_daemon("stop", {}, environment=self.environment, timeout=2.0)
        if res and res.get("ok"):
            print(json.dumps({"ok": True, "status": "stopped"}, ensure_ascii=False))
            return 0

        if self.p_path.exists():
            try:
                pid = int(self.p_path.read_text("utf-8").strip())
                os.kill(pid, signal.SIGTERM)
                print(json.dumps({"ok": True, "status": "stopped", "pid": pid}, ensure_ascii=False))
                return 0
            except (OSError, ValueError):
                pass
        print(json.dumps({"ok": False, "error": {"code": "NOT_RUNNING", "message": "daemon is not running"}}, ensure_ascii=False))
        return 1

    def status(self) -> int:
        running = is_daemon_running(self.environment)
        info = {
            "ok": True,
            "running": running,
            "socket": str(self.sock_path),
        }
        if self.p_path.exists():
            try:
                info["pid"] = int(self.p_path.read_text("utf-8").strip())
            except (ValueError, OSError):
                pass
        print(json.dumps(info, ensure_ascii=False))
        return 0

    def _cleanup(self) -> None:
        if self.sock_path.exists():
            try:
                self.sock_path.unlink()
            except OSError:
                pass
        if self.p_path.exists():
            try:
                self.p_path.unlink()
            except OSError:
                pass

    def _serve(self) -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server_sock:
            server_sock.bind(str(self.sock_path))
            server_sock.listen(16)
            server_sock.settimeout(0.5)

            while self.running:
                try:
                    conn, _ = server_sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break

                with conn:
                    conn.settimeout(10.0)
                    try:
                        raw = conn.recv(65536)
                        if not raw:
                            continue
                        req = json.loads(raw.decode("utf-8"))
                        command = req.get("command")
                        args = req.get("args", {})

                        if command == "ping":
                            resp = {"ok": True, "status": "pong"}
                        elif command == "stop":
                            self.running = False
                            resp = {"ok": True, "status": "stopping"}
                        else:
                            resp = self._dispatch(command, args)
                    except Exception as err:
                        resp = as_envelope(err)

                    try:
                        out = json.dumps(resp, ensure_ascii=False).encode("utf-8") + b"\n"
                        conn.sendall(out)
                    except OSError:
                        pass

    def _dispatch(self, command: str, args: dict[str, Any]) -> dict[str, Any]:
        reader = self._reader
        if reader is None:
            raise KamadoError(ErrorCode.KEY_UNAVAILABLE, "daemon reader is not initialized")

        if command == "status":
            return success(reader.status())

        if command == "verify":
            return success(reader.verify())

        if command == "rooms":
            limit = args.get("limit")
            readable_only = args.get("readable_only", False)
            rooms = reader.rooms(limit=None if readable_only else limit)
            if readable_only:
                rooms = [room for room in rooms if room.readable]
                if limit is not None:
                    rooms = rooms[:limit]
            return success({"rooms": [room.as_json() for room in rooms], "count": len(rooms)})

        if command == "context":
            target = args.get("room_id") if args.get("room_id") is not None else args.get("room")
            limit = args.get("limit", DEFAULT_LIMIT)
            return read_context(reader, target, limit)

        if command == "messages":
            room_id = args.get("room_id")
            if room_id is None and args.get("room") is not None:
                rooms = reader.rooms(name=args.get("room"))
                if not rooms:
                    raise KamadoError(ErrorCode.WINDOW_NOT_FOUND, f"No room matches {args.get('room')!r}")
                room_id = rooms[0].id
            found = reader.messages(room_id, limit=args.get("limit", DEFAULT_LIMIT), before_id=args.get("before_id"))
            return success({"messages": [msg.as_json() for msg in found], "count": len(found)})

        if command == "friends":
            friends = reader.friends(favorites_only=args.get("favorites", False))
            return success(
                {
                    "friends": [friend.as_json(include_phone=args.get("with_phone", False)) for friend in friends],
                    "count": len(friends),
                }
            )

        if command == "query":
            rows = reader.query(args["sql"], room_id=args.get("room_id"))
            return success({"rows": rows, "count": len(rows)})

        raise KamadoError(ErrorCode.INVALID_REQUEST, f"unknown daemon command: {command}")
