"""공감, edits, notices, attachments and SafeBot, read from a synthetic database."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path

from kamado import cli, files, model
from kamado.errors import ErrorCode, KamadoError
from kamado.model import Attachment

from tests.extras_fixtures import (
    COVERED,
    COVERED_LINK,
    EDIT_FEED,
    EDITED,
    ERASED,
    FILE,
    FILE_EXPIRES,
    FILE_NAME,
    FILE_SIZE,
    FILE_URL,
    LINKS,
    NOTICE_AT,
    NOTICE_TEXT,
    PHOTO,
    REACTED,
    REPLY_TO_COVERED,
    build_extras_reader,
)
from tests.fixtures import DIRECT_ROOM, FRIEND_ID, GROUP_ROOM, PEER_ID, build_reader


class ReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reader = build_extras_reader()
        self.addCleanup(self.reader.close)

    def window(self, **kwargs) -> dict[int, model.Message]:
        return {one.id: one for one in self.reader.messages(GROUP_ROOM, limit=100, **kwargs)}

    def test_reactions_are_read_with_the_message_and_empty_ones_dropped(self) -> None:
        reacted = self.window()[REACTED]
        self.assertEqual(
            [(one.label, one.count) for one in reacted.reactions], [("사랑", 3), ("엄지척", 1)]
        )
        self.assertEqual(
            reacted.as_json()["reactions"][0], {"label": "사랑", "count": 3, "code": "1200509_021"}
        )

    def test_a_reaction_belongs_to_its_own_room(self) -> None:
        """The key is (logId, chatId, type): the same id elsewhere is not this message."""
        self.assertNotIn("reactions", self.window()[LINKS].as_json())

    def test_a_reaction_changes_the_row_version(self) -> None:
        before = self.reader.row_versions(GROUP_ROOM, REACTED, REACTED)
        self.reader.connection().execute(
            "UPDATE NTChatLogMeta SET content = ? WHERE logId = ?",
            (json.dumps({"rx": [{"k": 2, "c": 4, "a": {"ko": "사랑"}}]}), REACTED),
        )
        self.assertNotEqual(before, self.reader.row_versions(GROUP_ROOM, REACTED, REACTED))

    def test_an_edited_message_is_marked_and_its_edit_feed_is_hidden(self) -> None:
        shown = self.window()
        self.assertTrue(shown[EDITED].edited)
        self.assertIs(shown[EDITED].as_json()["edited"], True)
        self.assertNotIn("edited", shown[REACTED].as_json())
        self.assertNotIn(EDIT_FEED, shown)
        self.assertNotIn(EDIT_FEED, self.window(include_erased=True))

    def test_this_client_keeps_no_earlier_versions(self) -> None:
        self.assertEqual(self.reader.revisions(GROUP_ROOM, EDITED), [])

    def test_a_message_deleted_for_everyone_is_not_found_for_revisions(self) -> None:
        with self.assertRaises(KamadoError) as caught:
            self.reader.revisions(GROUP_ROOM, ERASED)
        self.assertIs(caught.exception.code, ErrorCode.MESSAGE_NOT_FOUND)

    def test_a_covered_message_keeps_its_text_back_until_revealed(self) -> None:
        covered = self.window()[COVERED]
        self.assertEqual(covered.text, "")
        self.assertEqual(covered.as_json()["covered"], "safebot")
        self.reader.reveal_covered = True
        self.assertEqual(self.window()[COVERED].text, "가려진 말")

    def test_quoting_a_covered_message_does_not_uncover_it(self) -> None:
        reply = self.window()[REPLY_TO_COVERED].reply
        assert reply is not None
        self.assertEqual(reply.text, "")
        self.reader.reveal_covered = True
        reply = self.window()[REPLY_TO_COVERED].reply
        assert reply is not None
        self.assertEqual(reply.text, "가려진 말")

    def test_search_does_not_point_at_what_a_cover_hides(self) -> None:
        self.assertEqual(list(self.reader.search("가려진")), [])
        self.reader.reveal_covered = True
        self.assertEqual([one.id for one in self.reader.search("가려진")], [COVERED])

    def test_the_notice_is_the_meta_row(self) -> None:
        pinned = self.reader.notice(GROUP_ROOM)
        assert pinned is not None
        self.assertEqual((pinned.text, pinned.source, pinned.kind), (NOTICE_TEXT, "legacy", "text"))
        self.assertEqual((pinned.author_id, pinned.posted_at), (PEER_ID, NOTICE_AT))
        self.assertEqual(pinned.author, "Peer Renamed")

    def test_a_room_without_one_has_no_notice(self) -> None:
        self.assertIsNone(self.reader.notice(DIRECT_ROOM))

    def test_a_database_without_the_newer_columns_still_reads(self) -> None:
        plain = build_reader()
        self.addCleanup(plain.close)
        self.assertIsNone(plain.notice(GROUP_ROOM))
        found = plain.messages(GROUP_ROOM, limit=100)
        self.assertTrue(found)
        self.assertTrue(all(not one.reactions and not one.edited for one in found))

    def test_attachments_gather_every_kind_oldest_first(self) -> None:
        found = [one for one in self.reader.attachments(GROUP_ROOM, limit=100) if one.message_id > 9000]
        self.assertEqual(
            [(one.message_id, one.kind) for one in found],
            [(LINKS, "link"), (LINKS, "link"), (FILE, "file"), (PHOTO, "photo")],
        )
        self.assertEqual(
            [one.url for one in found[:2]], ["https://example.com/a", "https://example.com/b"]
        )
        carried = found[2]
        self.assertEqual(
            (carried.name, carried.size, carried.expires_at), (FILE_NAME, FILE_SIZE, FILE_EXPIRES)
        )
        self.assertNotIn("localPath", carried.as_json())

    def test_one_kind_can_be_asked_for_and_an_unknown_one_is_refused(self) -> None:
        self.assertEqual(
            [one.kind for one in self.reader.attachments(GROUP_ROOM, kind="file")], ["file"]
        )
        with self.assertRaises(KamadoError) as caught:
            self.reader.attachments(GROUP_ROOM, kind="sticker")
        self.assertIs(caught.exception.code, ErrorCode.INVALID_REQUEST)

    def test_a_covered_message_offers_its_links_only_once_revealed(self) -> None:
        def linked() -> list[int]:
            return [one.message_id for one in self.reader.attachments(GROUP_ROOM, kind="link")]

        self.assertNotIn(COVERED_LINK, linked())
        self.reader.reveal_covered = True
        self.assertIn(COVERED_LINK, linked())

    def test_file_at_answers_only_for_a_file(self) -> None:
        self.assertEqual(self.reader.file_at(GROUP_ROOM, FILE).url, FILE_URL)
        with self.assertRaises(KamadoError) as caught:
            self.reader.file_at(GROUP_ROOM, PHOTO)
        self.assertIs(caught.exception.code, ErrorCode.MESSAGE_NOT_FOUND)

    def test_file_at_carries_where_the_client_saved_it(self) -> None:
        saved = build_extras_reader(local_path="/synthetic/Downloads/보고서.pdf")
        self.addCleanup(saved.close)
        self.assertEqual(saved.file_at(GROUP_ROOM, FILE).local_path, "/synthetic/Downloads/보고서.pdf")


class CommandTests(unittest.TestCase):
    def invoke(self, *argv: str) -> tuple[int, dict]:
        reader = build_extras_reader()
        self.addCleanup(reader.close)
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.run(cli.build_parser().parse_args(argv), reader)
        return code, json.loads(output.getvalue())

    def test_notice_attachments_and_revisions_answer_in_the_envelope(self) -> None:
        room = str(GROUP_ROOM)
        _code, notice = self.invoke("notice", "--room-id", room)
        self.assertEqual(notice["notice"]["text"], NOTICE_TEXT)
        self.assertEqual(notice["notice"]["authorId"], str(PEER_ID))
        _code, listed = self.invoke("attachments", "--room-id", room, "--kind", "file")
        self.assertEqual(listed["attachments"][0]["name"], FILE_NAME)
        _code, versions = self.invoke("revisions", "--room-id", room, "--message-id", str(EDITED))
        self.assertEqual((versions["ok"], versions["count"]), (True, 0))

    def test_show_covered_reveals_on_request_only(self) -> None:
        room = str(GROUP_ROOM)
        _code, hidden = self.invoke("messages", "--room-id", room, "--limit", "100")
        _code, shown = self.invoke("messages", "--room-id", room, "--limit", "100", "--show-covered")

        def text(answer: dict) -> str:
            return next(m["text"] for m in answer["messages"] if m["id"] == str(COVERED))

        self.assertEqual((text(hidden), text(shown)), ("", "가려진 말"))

    def test_only_the_commands_the_daemon_answers_are_forwarded(self) -> None:
        from kamado import daemon

        source = Path(daemon.__file__).read_text("utf-8")
        for command in cli.DAEMON_COMMANDS:
            self.assertIn(f'command == "{command}"', source)
        for command in ("notice", "attachments", "revisions", "digest", "search", "serve"):
            self.assertNotIn(command, cli.DAEMON_COMMANDS)


def _attachment(**overrides) -> Attachment:
    values = dict(
        message_id=FILE,
        kind="file",
        sender_id=FRIEND_ID,
        sender="friend",
        sent_at=None,
        url=FILE_URL,
        name=FILE_NAME,
        size=4,
        expires_at=FILE_EXPIRES,
    )
    values.update(overrides)
    return Attachment(**values)  # type: ignore[arg-type]


class _Response(io.BytesIO):
    headers = {"Content-Length": "5"}


class FileSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.fetched: list[str] = []

    def opener(self, request, timeout):
        self.fetched.append(request.full_url)
        return _Response(b"bytes")

    def refuse(self, status: int):
        def opener(request, timeout):
            raise urllib.error.HTTPError(request.full_url, status, "gone", {}, None)  # type: ignore[arg-type]

        return opener

    def open(self, attachment, **kwargs):
        download = files.open_file(attachment, now=lambda: 1_800_000_000, **kwargs)
        self.addCleanup(download.close)
        return download

    def test_the_client_s_saved_copy_is_preferred_and_needs_no_network(self) -> None:
        saved = self.folder / FILE_NAME
        saved.write_bytes(b"save")
        download = self.open(_attachment(local_path=str(saved)), opener=self.opener)
        self.assertEqual((download.source, download.stream.read()), ("local", b"save"))
        self.assertEqual(self.fetched, [])

    def test_a_saved_copy_of_another_size_is_not_this_file(self) -> None:
        saved = self.folder / FILE_NAME
        saved.write_bytes(b"other size")
        self.assertEqual(self.open(_attachment(local_path=str(saved)), opener=self.opener).source, "remote")

    def test_a_saved_copy_that_is_gone_or_a_link_is_not_used(self) -> None:
        self.assertIsNone(files.local_copy(_attachment(local_path=str(self.folder / "gone.pdf"))))
        target = self.folder / "elsewhere.pdf"
        target.write_bytes(b"save")
        link = self.folder / FILE_NAME
        os.symlink(target, link)
        self.assertIsNone(files.local_copy(_attachment(local_path=str(link))))
        self.assertIsNone(files.local_copy(_attachment(local_path="relative/보고서.pdf")))

    def test_a_name_is_reduced_to_its_last_part(self) -> None:
        self.assertEqual(files.safe_name("..\\up/보고서.pdf"), "보고서.pdf")
        self.assertIsNone(files.safe_name(".."))

    def test_without_a_copy_the_address_is_fetched(self) -> None:
        download = self.open(_attachment(), opener=self.opener)
        self.assertEqual(
            (download.source, download.size, download.stream.read()), ("remote", 5, b"bytes")
        )
        self.assertEqual(self.fetched, [FILE_URL])

    def test_an_expired_address_is_refused_without_asking(self) -> None:
        with self.assertRaises(KamadoError) as caught:
            self.open(_attachment(expires_at=1_000), opener=self.opener)
        self.assertIs(caught.exception.code, ErrorCode.FILE_EXPIRED)
        self.assertEqual(self.fetched, [])

    def test_an_address_that_stopped_answering_is_expired(self) -> None:
        for status in (403, 410):
            with self.subTest(status=status), self.assertRaises(KamadoError) as caught:
                self.open(_attachment(), opener=self.refuse(status))
            self.assertIs(caught.exception.code, ErrorCode.FILE_EXPIRED)

    def test_an_address_off_the_client_s_hosts_is_never_fetched(self) -> None:
        with self.assertRaises(KamadoError) as caught:
            self.open(_attachment(url="https://example.com/file.pdf"), opener=self.opener)
        self.assertIs(caught.exception.code, ErrorCode.READ_FAILED)
        self.assertEqual(self.fetched, [])


if __name__ == "__main__":
    unittest.main()
