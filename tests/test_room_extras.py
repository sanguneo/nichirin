"""공감, edits, notices, attachments and SafeBot, read from a synthetic profile."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path

from kanao import cli, files, model
from kanao.errors import ErrorCode, KanaoError
from kanao.model import Attachment

from tests.extras_fixtures import (
    BOARD_NOTICE,
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
    LEGACY_NOTICE,
    LINKS,
    PHOTO,
    REACTED,
    REPLY_TO_COVERED,
    build_extras_reader,
)
from tests.fixtures import DIRECT_ROOM, FRIEND_ID, GROUP_ROOM, SELF_ROOM


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
        self.assertNotIn("reactions", self.window()[LINKS].as_json())

    def test_a_reaction_changes_the_row_version(self) -> None:
        before = self.reader.row_versions(GROUP_ROOM, REACTED, REACTED)
        self.reader._logs(GROUP_ROOM).execute(
            "UPDATE chatLogMeta SET content = ? WHERE logId = ?",
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

    def test_revisions_come_back_oldest_first(self) -> None:
        versions = self.reader.revisions(GROUP_ROOM, EDITED)
        self.assertEqual(
            [(one.revision, one.text) for one in versions],
            [(0, "처음 문장"), (1, "중간 문장"), (2, "고친 문장")],
        )

    def test_a_message_deleted_for_everyone_does_not_give_its_history_back(self) -> None:
        with self.assertRaises(KanaoError) as caught:
            self.reader.revisions(GROUP_ROOM, ERASED)
        self.assertIs(caught.exception.code, ErrorCode.MESSAGE_NOT_FOUND)

    def test_a_log_without_a_history_table_answers_with_none(self) -> None:
        from tests.fixtures import DIRECT_ROOM

        self.assertEqual(self.reader.revisions(DIRECT_ROOM, 5000), [])

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

    def test_the_board_notice_wins_over_the_older_row(self) -> None:
        pinned = self.reader.notice(GROUP_ROOM)
        assert pinned is not None
        self.assertEqual((pinned.text, pinned.source, pinned.kind), (BOARD_NOTICE, "board", "text"))
        self.assertEqual(pinned.author_id, FRIEND_ID)
        self.assertEqual(pinned.as_json()["postedAt"], "2026-01-04T03:00:00+00:00")

    def test_the_older_notice_row_answers_when_there_is_no_post(self) -> None:
        pinned = self.reader.notice(DIRECT_ROOM)
        assert pinned is not None
        self.assertEqual((pinned.text, pinned.source), (LEGACY_NOTICE, "legacy"))

    def test_a_post_that_is_not_a_notice_is_no_notice(self) -> None:
        self.assertIsNone(self.reader.notice(SELF_ROOM))

    def test_attachments_gather_every_kind_oldest_first(self) -> None:
        found = self.reader.attachments(GROUP_ROOM, limit=100)
        self.assertEqual(
            [(one.message_id, one.kind) for one in found],
            [(LINKS, "link"), (LINKS, "link"), (FILE, "file"), (PHOTO, "photo")],
        )
        self.assertEqual([one.url for one in found[:2]], ["https://example.com/a", "https://example.com/b"])
        carried = found[2]
        self.assertEqual((carried.name, carried.size, carried.expires_at), (FILE_NAME, FILE_SIZE, FILE_EXPIRES))

    def test_one_kind_can_be_asked_for_and_an_unknown_one_is_refused(self) -> None:
        self.assertEqual([one.kind for one in self.reader.attachments(GROUP_ROOM, kind="file")], ["file"])
        with self.assertRaises(KanaoError) as caught:
            self.reader.attachments(GROUP_ROOM, kind="sticker")
        self.assertIs(caught.exception.code, ErrorCode.INVALID_REQUEST)

    def test_a_covered_message_offers_its_links_only_once_revealed(self) -> None:
        links = lambda: [one.url for one in self.reader.attachments(GROUP_ROOM, kind="link")]  # noqa: E731
        self.assertNotIn("https://covered.example/x", links())
        self.reader.reveal_covered = True
        self.assertIn("https://covered.example/x", links())
        self.assertIn(COVERED_LINK, [one.message_id for one in self.reader.attachments(GROUP_ROOM, kind="link")])

    def test_file_at_answers_only_for_a_file(self) -> None:
        self.assertEqual(self.reader.file_at(GROUP_ROOM, FILE).url, FILE_URL)
        with self.assertRaises(KanaoError) as caught:
            self.reader.file_at(GROUP_ROOM, PHOTO)
        self.assertIs(caught.exception.code, ErrorCode.MESSAGE_NOT_FOUND)


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
        self.assertEqual(notice["notice"]["text"], BOARD_NOTICE)
        _code, listed = self.invoke("attachments", "--room-id", room, "--kind", "file")
        self.assertEqual(listed["attachments"][0]["name"], FILE_NAME)
        _code, versions = self.invoke("revisions", "--room-id", room, "--message-id", str(EDITED))
        self.assertEqual(versions["count"], 3)

    def test_show_covered_reveals_on_request_only(self) -> None:
        room = str(GROUP_ROOM)
        _code, hidden = self.invoke("messages", "--room-id", room, "--limit", "100")
        _code, shown = self.invoke("messages", "--room-id", room, "--limit", "100", "--show-covered")
        text = lambda answer: next(m["text"] for m in answer["messages"] if m["id"] == str(COVERED))  # noqa: E731
        self.assertEqual((text(hidden), text(shown)), ("", "가려진 말"))


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
        download = files.open_file(
            attachment, directory=self.folder, now=lambda: 1_800_000_000, **kwargs
        )
        self.addCleanup(download.close)
        return download

    def test_the_client_s_saved_copy_is_preferred_and_needs_no_network(self) -> None:
        (self.folder / FILE_NAME).write_bytes(b"save")
        download = self.open(_attachment(), opener=self.opener)
        self.assertEqual((download.source, download.stream.read()), ("local", b"save"))
        self.assertEqual(self.fetched, [])

    def test_a_numbered_copy_counts_but_a_different_size_does_not(self) -> None:
        (self.folder / FILE_NAME).write_bytes(b"other size")
        (self.folder / "보고서 (1).pdf").write_bytes(b"save")
        self.assertEqual(self.open(_attachment(), opener=self.opener).source, "local")

    def test_a_name_with_a_path_in_it_is_looked_for_only_inside_the_folder(self) -> None:
        inner = self.folder / "inner"
        inner.mkdir()
        (self.folder / "escape.pdf").write_bytes(b"save")
        self.assertIsNone(files.local_copy(_attachment(name="..\\escape.pdf"), inner))
        (inner / "escape.pdf").write_bytes(b"save")
        self.assertEqual(files.local_copy(_attachment(name="..\\escape.pdf"), inner), inner / "escape.pdf")
        self.assertIsNone(files.safe_name(".."))

    def test_without_a_copy_the_address_is_fetched(self) -> None:
        download = self.open(_attachment(), opener=self.opener)
        self.assertEqual((download.source, download.size, download.stream.read()), ("remote", 5, b"bytes"))
        self.assertEqual(self.fetched, [FILE_URL])

    def test_an_expired_address_is_refused_without_asking(self) -> None:
        with self.assertRaises(KanaoError) as caught:
            self.open(_attachment(expires_at=1_000), opener=self.opener)
        self.assertIs(caught.exception.code, ErrorCode.FILE_EXPIRED)
        self.assertEqual(self.fetched, [])

    def test_an_address_that_stopped_answering_is_expired(self) -> None:
        for status in (403, 410):
            with self.subTest(status=status), self.assertRaises(KanaoError) as caught:
                self.open(_attachment(), opener=self.refuse(status))
            self.assertIs(caught.exception.code, ErrorCode.FILE_EXPIRED)

    def test_an_address_off_the_client_s_hosts_is_never_fetched(self) -> None:
        with self.assertRaises(KanaoError) as caught:
            self.open(_attachment(url="https://example.com/file.pdf"), opener=self.opener)
        self.assertIs(caught.exception.code, ErrorCode.READ_FAILED)
        self.assertEqual(self.fetched, [])


if __name__ == "__main__":
    unittest.main()
