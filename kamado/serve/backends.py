"""What the routes are wired to on a host that is actually running KakaoTalk.

The HTTP layer knows nothing about a heap scan, a window handle, or a name
ledger; it knows it was handed callables. These are those callables, so the
platform-specific half of the server is one import away from the rest of it.
"""

from __future__ import annotations

from typing import Any, Callable

from ..errors import ErrorCode, KamadoError


def make_live_rooms_provider(reader_factory) -> Callable[[], list[Any]]:
    """Return the rooms a live server lists on each ``GET /rooms``."""

    def provider() -> list[Any]:
        with reader_factory() as reader:
            return list(reader.rooms())

    return provider


def make_live_context_provider(reader_factory) -> Callable[[int, int], dict[str, Any]]:
    """Return the context a live server reads on each ``GET /context``."""

    def provider(room_id: int, limit: int) -> dict[str, Any]:
        from ..context import read_context

        with reader_factory() as reader:
            answer = read_context(reader, room_id, limit=limit)
        return {key: value for key, value in answer.items() if key != "ok"}

    return provider


def make_live_send() -> Callable[..., dict[str, Any]]:
    """Build the send callable the server uses on a macOS host.

    Each call opens a fresh reader (so the catalog is never stale across
    requests), resolves the room exactly like the ``send`` command, refuses
    duplicate catalog titles, and hands the text to the real sender.
    """
    from ..names import NameLedger
    from ..reader import Reader

    def send(
        room: str,
        text: str,
        dry_run: bool = False,
        open_if_needed: bool = False,
        aliases=(),
        hint=None,
    ) -> dict[str, Any]:
        from ..context import _resolve
        from ..macauto import make_sender

        with Reader(ledger=NameLedger()) as reader:
            # cli._selected_room is this line plus an argparse namespace built
            # to carry two values; importing cli from here is a cycle, and the
            # cycle is the only thing that namespace was for.
            resolved = _resolve(reader, room)
            catalogued = reader.rooms()
            duplicates = [
                r for r in catalogued if r.title.casefold() == resolved.title.casefold()
            ]
            if len(duplicates) != 1:
                raise KamadoError(
                    ErrorCode.AMBIGUOUS_CHAT,
                    "multiple catalog rooms share this window title; "
                    "give the target a unique title",
                    candidates=[r.as_json() for r in duplicates],
                )
            stored = reader.stored_title(resolved.id)
            window_aliases = [stored] if stored and stored != resolved.title else []
            position = next(
                (index for index, r in enumerate(catalogued) if r.id == resolved.id),
                None,
            )
            try:
                result = make_sender().send(
                    resolved.title,
                    text,
                    dry_run=dry_run,
                    open_if_needed=open_if_needed,
                    aliases=window_aliases,
                    hint=position,
                )
            except OSError as error:
                raise KamadoError(ErrorCode.SEND_FAILED, str(error)) from error
            return {**result, "roomId": str(resolved.id)}

    return send


def make_live_digest_provider(reader_factory) -> Callable[[int, str, int, int], dict[str, Any]]:
    """Return the digest a live server builds on each ``GET /digest``.

    The digest is ``kamado.digest.build``, the same call the command makes, so
    a remote caller and a local one are reading one implementation rather than
    two. Reaching into ``cli`` for it used to mean assembling an
    ``argparse.Namespace`` here and pointing this package at the layer above
    it.
    """

    def provider(room_id: int, span: str, limit: int, highlights: int) -> dict[str, Any]:
        from ..digest import build

        with reader_factory() as reader:
            return build(
                reader, room_id, span=span, limit=limit, highlights=highlights
            )

    return provider


def make_live_watcher_factory(reader_factory, interval: float) -> Callable[[], Any]:
    """Return one ``watch.Watcher`` per SSE connection on a live server."""

    class _LiveWatcher:
        """Own the reader alongside the watcher so disconnect closes both."""

        def __init__(self) -> None:
            self._reader = reader_factory()
            from ..watch import Watcher

            self._watcher = Watcher(self._reader, interval=interval)

        def prime(self) -> None:
            self._watcher.prime()

        def resume(self, cursor) -> None:
            self._watcher.resume(cursor)

        def poll(self):
            return self._watcher.poll()

        def close(self) -> None:
            try:
                self._reader.close()
            except Exception:
                pass

    def factory() -> Any:
        return _LiveWatcher()

    return factory
