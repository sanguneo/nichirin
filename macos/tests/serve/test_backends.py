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


class RoomIdTests(unittest.TestCase):
    """`room` is "the room's title, or its id as a string", as the contract says.

    A JSON body carries an id as a string like every other id, and the id path
    was reached only by an int, so a client handing back the id `/rooms` gave
    it was answered CHAT_NOT_FOUND.
    """

    ROOMS = [
        _Room(7, "Synthetic"),
        _Room(42, "Answer"),
        _Room(8, "42"),
        _Room(9, "123"),
        _Room(10, "Twin"),
        _Room(11, "Twin"),
    ]

    def setUp(self) -> None:
        import kamado.keyring as keyring_module
        import kamado.macauto as macauto_module
        import kamado.reader as reader_module

        original_reader = reader_module.Reader
        original_discover = keyring_module.Keyring.discover
        original_sender = macauto_module.make_sender
        rooms = self.ROOMS
        self.sent: list = []
        sent = self.sent

        class _CatalogReader:
            def __init__(self, cache=None, keyring=None, ledger=None, connection=None):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_exception):
                return None

            def room(self, room_id):
                return next((room for room in rooms if room.id == room_id), None)

            def resolve(self, target):
                return [room for room in rooms if room.title.casefold() == target.casefold()]

            def rooms(self):
                return list(rooms)

            def stored_title(self, _room_id):
                return None

        class _Sender:
            def send(self, room, text, **_keywords):
                sent.append(room)
                return {"submitted": True, "room": room}

        reader_module.Reader = _CatalogReader
        keyring_module.Keyring.discover = staticmethod(lambda: object())  # type: ignore[attr-defined]
        macauto_module.make_sender = lambda: _Sender()
        self.addCleanup(setattr, reader_module, "Reader", original_reader)
        self.addCleanup(setattr, keyring_module.Keyring, "discover", original_discover)
        self.addCleanup(setattr, macauto_module, "make_sender", original_sender)
        self.send = backends.make_live_send()

    def test_an_id_from_rooms_resolves_to_that_room(self) -> None:
        answer = self.send("7", "hello")
        self.assertEqual(answer["roomId"], "7")
        self.assertEqual(self.sent, ["Synthetic"])

    def test_an_id_is_tried_before_a_title_that_happens_to_be_that_number(self) -> None:
        self.assertEqual(self.send("42", "hello")["roomId"], "42")
        self.assertEqual(self.sent, ["Answer"])

    def test_a_number_no_room_has_as_its_id_still_resolves_by_title(self) -> None:
        self.assertEqual(self.send("123", "hello")["roomId"], "9")

    def test_a_number_that_names_nothing_is_not_found(self) -> None:
        from kamado.errors import ErrorCode, KamadoError

        with self.assertRaises(KamadoError) as raised:
            self.send("999", "hello")
        self.assertEqual(raised.exception.code, ErrorCode.CHAT_NOT_FOUND)
        self.assertEqual(self.sent, [])

    def test_an_id_whose_window_title_is_shared_is_still_refused(self) -> None:
        from kamado.errors import ErrorCode, KamadoError

        with self.assertRaises(KamadoError) as raised:
            self.send("10", "hello")
        self.assertEqual(raised.exception.code, ErrorCode.AMBIGUOUS_CHAT)
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
