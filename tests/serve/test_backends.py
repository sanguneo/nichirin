"""What the live send backend reuses between requests, and what it does not.

Every other test in this package replaces this backend with a stub, so nothing
watched it build its reader. What it builds there decides what a send costs:
a reader given neither a cache nor an account decrypts the whole database and
rediscovers the account on every request, which measured 2.56 s before a single
character was typed.
"""

from __future__ import annotations

import unittest

from kamado.serve import backends


class _Room:
    """A catalog room, as much of one as resolving and the census need."""

    def __init__(self, room_id: int, title: str) -> None:
        self.id = room_id
        self.title = title

    def as_json(self) -> dict:
        return {"id": str(self.id), "title": self.title}


class _StubSender:
    def send(self, room, text, dry_run=False, open_if_needed=False, aliases=(), hint=None):
        return {"submitted": not dry_run, "room": room, "length": len(text)}


class LiveSendTests(unittest.TestCase):
    """The reader is rebuilt per send; the cache and the account are not."""

    def setUp(self) -> None:
        # No platform guard on purpose: the reader, the account and the sender
        # are all replaced here, so this runs wherever the author happens to be.
        import kamado.keyring as keyring_module
        import kamado.macauto as macauto_module
        import kamado.reader as reader_module

        # Bind the originals before patching, or the restore puts back a wrapper.
        original_reader = reader_module.Reader
        original_discover = keyring_module.Keyring.discover
        original_sender = macauto_module.make_sender

        self.built: list = []
        built = self.built
        room = _Room(7, "Synthetic")

        class _RecordingReader:
            def __init__(self, cache=None, keyring=None, ledger=None, connection=None):
                built.append({"cache": cache, "keyring": keyring})

            def __enter__(self):
                return self

            def __exit__(self, *_exception):
                return None

            def resolve(self, target):
                return [room] if target == room.title else []

            def rooms(self):
                return [room]

            def stored_title(self, _room_id):
                return None

            def close(self) -> None:
                return None

        self.discovered: list = []
        discovered = self.discovered

        def _discover():
            discovered.append(object())
            return discovered[-1]

        reader_module.Reader = _RecordingReader
        keyring_module.Keyring.discover = staticmethod(  # type: ignore[attr-defined]
            _discover
        )
        macauto_module.make_sender = lambda: _StubSender()
        self.addCleanup(setattr, reader_module, "Reader", original_reader)
        self.addCleanup(setattr, keyring_module.Keyring, "discover", original_discover)
        self.addCleanup(setattr, macauto_module, "make_sender", original_sender)

    def test_the_cache_and_the_account_are_shared_across_sends(self) -> None:
        send = backends.make_live_send()
        for text in ("one", "two", "three"):
            send("Synthetic", text)

        self.assertEqual(len(self.built), 3, "a reader is still built per send")
        caches = {id(call["cache"]) for call in self.built}
        self.assertEqual(len(caches), 1, "a fresh cache would decrypt the database again")
        keyrings = {id(call["keyring"]) for call in self.built}
        self.assertEqual(len(keyrings), 1)
        self.assertEqual(
            len(self.discovered), 1, "the account was rediscovered on a later send"
        )

    def test_no_cache_is_built_and_no_account_resolved_until_the_first_send(self) -> None:
        """Building the server must not decide whether the install works.

        `status` explains a broken install; a server that refused to start for
        the same reason would take that explanation away.
        """
        backends.make_live_send()
        self.assertEqual(self.discovered, [])
        self.assertEqual(self.built, [])

    def test_each_send_still_gets_its_own_reader(self) -> None:
        """Only the expensive halves are shared.

        A reader remembers names and column sets on itself, and those are the
        things a rename should not survive.
        """
        send = backends.make_live_send()
        send("Synthetic", "one")
        send("Synthetic", "two")
        self.assertEqual(len(self.built), 2)


if __name__ == "__main__":
    unittest.main()
