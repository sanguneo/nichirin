"""Asking a command outside this process, and what comes back.

No model is involved here: the configured command is a Python one-liner that
echoes, fails or says nothing on purpose, so what is under test is the handing
over and the taking back rather than anybody's answer. Nothing waits on a
clock — a started process is waited on directly, which is deterministic.
"""

from __future__ import annotations

import os
import shlex
import sys
import unittest
from unittest.mock import patch

from kanao import agent
from kanao.errors import ErrorCode, KanaoError
from kanao.tui.app import App, ChatState
from kanao.tui.console import Key
from kanao.tui.layout import strip_ansi
from kanao.tui.theme import Theme

from tests.fixtures import DIRECT_ROOM, GROUP_ROOM, build_reader

WIDTH = 80
HEIGHT = 24


def script(body: str) -> str:
    """A command line running *body*, quoted the way the variable would hold it.

    Single quotes around the body and double quotes inside it, never the other
    way round: the splitting here keeps backslashes rather than reading them
    as escapes, so it cannot undo the `'"'"'` that POSIX quoting would use for
    an inner apostrophe. Every body below is written to need none.
    """
    assert "'" not in body, "a body with an apostrophe cannot survive this quoting"
    return f"'{sys.executable}' -c '{body}'"


def writes(text: str, encoding: str = "utf-8", code: int = 0) -> str:
    """A command printing *text* in *encoding*, the way a real one would.

    Written as bytes on purpose. A child's own stdout encoding on Windows is
    the locale's, not UTF-8, so a test that let Python choose would be pinning
    this machine's code page rather than what the agent said.
    """
    tail = f"; sys.exit({code})" if code else ""
    # Spelled out as escapes in a double-quoted literal: repr() would quote it
    # with apostrophes, which the outer quoting here cannot carry.
    literal = 'b"' + "".join(f"\\x{byte:02x}" for byte in text.encode(encoding)) + '"'
    return script(f"import sys; sys.stdout.buffer.write({literal}){tail}")


ECHOES = script("import sys; sys.stdout.write(sys.stdin.read())")
ANSWERS = writes("답장 초안입니다")
ANSWERS_IN_THE_CODE_PAGE = writes("답장 초안입니다", encoding="cp949")
NARRATES_WHILE_WORKING = script(
    'import sys; sys.stderr.write("skipped { reason: not-continuable }\\n");'
    ' sys.stdout.buffer.write('
    + 'b"' + "".join(f"\\x{byte:02x}" for byte in "한 문장 요약입니다".encode()) + '"'
    + ")"
)
SAYS_NOTHING = script("pass")
FAILS = writes("무언가 잘못됨", code=3)
FAILS_NOISILY = writes("{\n  stack: 1,\n}\n로그인되지 않았습니다\n", code=1)


class AskCase(unittest.TestCase):
    def setUp(self) -> None:
        self.reader = build_reader()
        self.addCleanup(self.reader.close)
        self.app = App(self.reader, theme=Theme(truecolor=True, enabled=False))
        self.app.load_rooms()
        self.app.open_room(next(r for r in self.app.rooms if r.id == DIRECT_ROOM))

    @property
    def chat(self) -> ChatState:
        chat = self.app.chat
        assert chat is not None
        return chat

    def ask(self, command: str, question: str = "정리해줘") -> None:
        """Ask *question* of *command* and settle it, without waiting on a clock."""
        with patch.dict(os.environ, {agent.AGENT_VARIABLE: command}):
            self.app._run_command(f"/ask {question}")
        pending = self.chat.asking
        if pending is not None:
            pending.process.wait()
            self.app._collect_answer()

    def frame(self) -> list[str]:
        return [strip_ansi(line) for line in self.app.render(WIDTH, HEIGHT)]


class CommandResolutionTests(unittest.TestCase):
    def test_the_variable_is_read_as_a_command_line(self) -> None:
        with patch.dict(os.environ, {agent.AGENT_VARIABLE: "some agent --flag"}):
            self.assertEqual(agent.command(), ["some", "agent", "--flag"])

    def test_quotes_hold_a_word_with_a_space_together(self) -> None:
        with patch.dict(os.environ, {agent.AGENT_VARIABLE: "'a b' c"}):
            self.assertEqual(agent.command(), ["a b", "c"])

    def test_a_windows_path_keeps_its_backslashes(self) -> None:
        """Split as POSIX, `C:\\tools\\omo.exe` comes back `C:toolsomo.exe`.

        That is not a path, and the failure it produces says the program is
        missing rather than that its name was eaten — which is why the split
        is not the POSIX one here.
        """
        with patch.dict(os.environ, {agent.AGENT_VARIABLE: r"C:\tools\omo.exe -p"}):
            self.assertEqual(agent.command(), [r"C:\tools\omo.exe", "-p"])

    def test_a_quoted_windows_path_with_spaces_survives_both_ways(self) -> None:
        with patch.dict(
            os.environ, {agent.AGENT_VARIABLE: '"C:\\Program Files\\omo\\omo.exe" -p'}
        ):
            self.assertEqual(agent.command(), ["C:\\Program Files\\omo\\omo.exe", "-p"])

    def test_an_unclosed_quote_is_refused_rather_than_guessed(self) -> None:
        with patch.dict(os.environ, {agent.AGENT_VARIABLE: "broken 'quote"}):
            with self.assertRaises(KanaoError) as raised:
                agent.command()
        self.assertEqual(raised.exception.code, ErrorCode.INVALID_REQUEST)

    def test_omo_is_the_default_when_it_is_installed(self) -> None:
        with patch.dict(os.environ, {agent.AGENT_VARIABLE: ""}), patch.object(
            agent.shutil, "which", return_value=r"C:\somewhere\omo.exe"
        ):
            self.assertEqual(agent.command(), [r"C:\somewhere\omo.exe", "-p"])

    def test_nothing_configured_and_nothing_installed_is_nobody_to_ask(self) -> None:
        with patch.dict(os.environ, {agent.AGENT_VARIABLE: ""}), patch.object(
            agent.shutil, "which", return_value=None
        ):
            self.assertIsNone(agent.command())


class HandingOverTests(AskCase):
    def test_the_conversation_and_the_question_are_what_is_sent(self) -> None:
        """The command sees the room's own words and what was asked of them."""
        self.ask(ECHOES, "오늘 얘기 정리해줘")
        sent = self.chat.draft
        self.assertIn("first", sent, "the conversation did not reach the command")
        self.assertIn("오늘 얘기 정리해줘", sent)
        self.assertIn(self.chat.room.title, sent)

    def test_nothing_about_other_rooms_is_sent(self) -> None:
        self.app.open_room(
            next(r for r in self.app.rooms if r.id == GROUP_ROOM), beside=True
        )
        self.app.focus = 0
        self.ask(ECHOES)
        self.assertNotIn("group hello", self.chat.draft)

    def test_a_question_is_required(self) -> None:
        with patch.dict(os.environ, {agent.AGENT_VARIABLE: ECHOES}):
            self.app._run_command("/ask")
        self.assertIsNone(self.chat.asking)
        self.assertIn("물어볼 말", self.chat.notice)

    def test_with_nobody_to_ask_it_says_so(self) -> None:
        with patch.dict(os.environ, {agent.AGENT_VARIABLE: ""}), patch.object(
            agent.shutil, "which", return_value=None
        ):
            self.app._run_command("/ask 정리해줘")
        self.assertIsNone(self.chat.asking)
        self.assertIn(agent.AGENT_VARIABLE, self.chat.notice)

    def test_a_second_question_waits_for_the_first(self) -> None:
        with patch.dict(os.environ, {agent.AGENT_VARIABLE: ECHOES}):
            self.app._run_command("/ask 하나")
            first = self.chat.asking
            assert first is not None
            self.app._run_command("/ask 둘")
            self.assertIs(self.chat.asking, first)
            self.assertIn("이미", self.chat.notice)
            first.cancel()
            self.chat.asking = None


class AnswerTests(AskCase):
    def test_an_answer_becomes_a_draft_and_is_not_sent(self) -> None:
        self.ask(ANSWERS)
        self.assertEqual(self.chat.draft, "답장 초안입니다")
        self.assertEqual(self.chat.caret, len(self.chat.draft))
        self.assertIsNone(self.app.pending_send, "an answer was sent without being read")
        self.assertIsNone(self.chat.outbox)

    def test_what_the_command_narrates_is_not_part_of_the_answer(self) -> None:
        """An agent talks about its own work on stderr while it works.

        Run live against `omo -p`, it printed two lines about skipped
        continuations before the summary. Merged into stdout they arrive at
        the top of the draft — which is a message about to be sent.
        """
        self.ask(NARRATES_WHILE_WORKING)
        self.assertEqual(self.chat.draft, "한 문장 요약입니다")
        self.assertNotIn("not-continuable", self.chat.draft)

    def test_an_answer_in_this_machines_code_page_is_read_as_words(self) -> None:
        """A child on Windows writes the locale's encoding unless it says not to.

        Decoding that as UTF-8 raises nothing and returns mojibake, which the
        composer would then carry into the client as though it were the
        answer.
        """
        self.ask(ANSWERS_IN_THE_CODE_PAGE)
        self.assertEqual(self.chat.draft, "답장 초안입니다")

    def test_a_draft_already_being_written_is_not_written_over(self) -> None:
        with patch.dict(os.environ, {agent.AGENT_VARIABLE: ANSWERS}):
            self.app._run_command("/ask 정리해줘")
        pending = self.chat.asking
        assert pending is not None
        for character in "내가 쓰던 말":
            self.app.handle("char", character)
        pending.process.wait()
        self.app._collect_answer()

        self.assertEqual(self.chat.draft, "내가 쓰던 말")
        self.assertEqual(self.chat.answer, "답장 초안입니다")
        self.assertIn("/take", self.chat.notice)

    def test_take_puts_that_answer_in_the_composer(self) -> None:
        self.chat.answer = "기다리던 답변"
        self.app._run_command("/take")
        self.assertEqual(self.chat.draft, "기다리던 답변")
        self.assertIsNone(self.chat.answer)

    def test_take_with_nothing_waiting_says_so(self) -> None:
        self.app._run_command("/take")
        self.assertEqual(self.chat.draft, "")
        self.assertIn("없습니다", self.chat.notice)

    def test_a_command_that_fails_is_reported_with_what_it_printed(self) -> None:
        self.ask(FAILS)
        self.assertEqual(self.chat.draft, "")
        self.assertIn("무언가 잘못됨", self.chat.notice)

    def test_a_failure_is_quoted_by_its_last_line_not_its_last_bytes(self) -> None:
        """An agent that fails prints its own diagnostics above the reason.

        Cutting by length lands in the middle of one of them: against a real
        agent with no credentials that quoted half a JSON object, while the
        line below it said plainly that nobody was logged in.
        """
        self.ask(FAILS_NOISILY)
        self.assertIn("로그인되지 않았습니다", self.chat.notice)
        self.assertNotIn("stack", self.chat.notice)

    def test_a_command_that_says_nothing_is_not_an_empty_draft(self) -> None:
        self.ask(SAYS_NOTHING)
        self.assertEqual(self.chat.draft, "")
        self.assertIn("아무것도", self.chat.notice)

    def test_a_command_that_does_not_exist_is_reported(self) -> None:
        with patch.dict(os.environ, {agent.AGENT_VARIABLE: "kanao-no-such-command"}):
            self.app._run_command("/ask 정리해줘")
        self.assertIsNone(self.chat.asking)
        self.assertNotEqual(self.chat.notice, "")


class WhileWaitingTests(AskCase):
    def test_the_screen_names_the_program_the_words_went_to(self) -> None:
        """Which program was handed the conversation is never left to guess."""
        with patch.dict(os.environ, {agent.AGENT_VARIABLE: ECHOES}):
            self.app._run_command("/ask 정리해줘")
        pending = self.chat.asking
        assert pending is not None
        self.addCleanup(pending.cancel)
        above = next(line for line in self.frame() if "묻는 중" in line)
        self.assertIn(pending.label, above)

    def test_closing_the_pane_stops_the_command(self) -> None:
        with patch.dict(os.environ, {agent.AGENT_VARIABLE: ECHOES}):
            self.app._run_command("/ask 정리해줘")
        pending = self.chat.asking
        assert pending is not None
        self.app.handle(Key.ESCAPE, "")
        self.assertIsNotNone(pending.process.poll(), "the command outlived its pane")

    def test_an_answer_that_never_comes_is_given_up_on(self) -> None:
        with patch.dict(
            os.environ, {agent.AGENT_VARIABLE: script("import time; time.sleep(60)")}
        ):
            self.app._run_command("/ask 정리해줘")
        pending = self.chat.asking
        assert pending is not None
        self.addCleanup(pending.cancel)
        # The clock is read rather than waited on, so the deadline is reached
        # by moving it, not by sitting through it.
        self.app._clock = lambda: pending.started_at + agent.TIMEOUT_SECONDS + 1
        self.assertTrue(self.app._collect_answer())
        self.assertIsNone(self.chat.asking)
        self.assertIsNotNone(pending.process.poll())
        self.assertIn("답하지 않아", self.chat.notice)


if __name__ == "__main__":
    unittest.main()
