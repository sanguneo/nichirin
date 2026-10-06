"""Announce a running ``serve`` to saya (saya ``docs/register.md``, version 1).

saya holds one register token and no core address. A ``serve`` that is given
saya's register address and token tells saya where it is and which bearer
token to present, repeats that every interval saya answers with, and says
goodbye on a clean stop. Registration is opt-in and never a condition of
serving: a refusal or an unreachable saya is logged once, without either token,
and tried again on the next interval.

Standard library only, Python 3.9+, the same file for both builds.
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

DEFAULT_INTERVAL_SECONDS = 30.0
REQUEST_TIMEOUT_SECONDS = 10.0
GOODBYE_TIMEOUT_SECONDS = 3.0

Send = Callable[[str, str, str, Dict[str, Any], float], Tuple[int, Any]]
_UNSAID = object()


@dataclass(frozen=True)
class Registration:
    register_url: str
    register_token: str
    kind: str
    id: str
    url: str
    token: str


def registration_from_env(
    env: Mapping[str, str], *, kind: str, id: str, port: int, token: str
) -> Optional[Registration]:
    """``SAYA_REGISTER_URL`` and ``SAYA_REGISTER_TOKEN`` turn it on.

    ``SAYA_ADVERTISE_URL`` is where saya reaches this service; without it, this
    machine's loopback, which is right only when saya runs here too.
    """
    register_url = env.get("SAYA_REGISTER_URL", "")
    register_token = env.get("SAYA_REGISTER_TOKEN", "")
    if not register_url or not register_token:
        return None
    return Registration(
        register_url=register_url.rstrip("/"),
        register_token=register_token,
        kind=kind,
        id=id,
        url=env.get("SAYA_ADVERTISE_URL") or f"http://127.0.0.1:{port}",
        token=token,
    )


def _send(method: str, url: str, token: str, body: Dict[str, Any], timeout: float) -> Tuple[int, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:  # noqa: S310 - configured URL
            return answer.status, _json(answer.read())
    except urllib.error.HTTPError as refused:
        return refused.code, _json(refused.read())


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        return {}


def _stderr(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class Registrar:
    """Keeps one registration alive on a daemon thread until :meth:`stop`."""

    def __init__(
        self,
        registration: Registration,
        *,
        send: Send = _send,
        log: Callable[[str], None] = _stderr,
    ) -> None:
        self._registration = registration
        self._send = send
        self._log = log
        self._stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._reported: object = _UNSAID

    @property
    def _endpoint(self) -> str:
        return f"{self._registration.register_url}/register"

    def beat(self) -> float:
        """Register once; return the seconds until the next heartbeat."""
        r = self._registration
        interval = DEFAULT_INTERVAL_SECONDS
        problem: Optional[str] = None
        try:
            status, body = self._send(
                "POST",
                self._endpoint,
                r.register_token,
                {"kind": r.kind, "id": r.id, "url": r.url, "token": r.token},
                REQUEST_TIMEOUT_SECONDS,
            )
            if 200 <= status < 300:
                answered = body.get("interval") if isinstance(body, dict) else None
                if isinstance(answered, (int, float)) and answered > 0:
                    interval = float(answered)
            else:
                code = body.get("error", {}).get("code", "") if isinstance(body, dict) else ""
                problem = f"refused with {status} {code}".strip()
        except (OSError, ValueError) as error:
            problem = f"unreachable ({type(error).__name__})"
        # Say it once when it changes, not every interval.
        if problem != self._reported:
            self._log(
                f"saya registration of {r.id} {problem}; retrying every {interval:g}s"
                if problem
                else f"registered {r.id} with saya as {r.url}"
            )
            self._reported = problem
        return interval

    def _loop(self) -> None:
        interval = self.beat()
        while not self._stopping.wait(interval):
            interval = self.beat()

    def start(self) -> "Registrar":
        self._thread = threading.Thread(target=self._loop, name="saya-register", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """Stop the heartbeat and take the registration back, best effort."""
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(REQUEST_TIMEOUT_SECONDS)
        r = self._registration
        try:
            self._send(
                "DELETE",
                self._endpoint,
                r.register_token,
                {"kind": r.kind, "id": r.id},
                GOODBYE_TIMEOUT_SECONDS,
            )
        except (OSError, ValueError):
            pass  # saya forgets it after its ttl anyway


def start_from_env(
    env: Mapping[str, str], *, kind: str, id: str, port: int, token: str
) -> Optional[Registrar]:
    """Start registering when the environment asks for it; otherwise ``None``."""
    registration = registration_from_env(env, kind=kind, id=id, port=port, token=token)
    return Registrar(registration).start() if registration else None
