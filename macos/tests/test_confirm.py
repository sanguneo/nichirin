"""Shared local-log observation, with synthetic rows and virtual time only."""

from dataclasses import dataclass
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kakaocommon import confirm, trace


@dataclass(frozen=True)
class Row:
    id: int
    text: str = "synthetic"
    from_me: bool = True


class Clock:
    def __init__(self):
        self.now = 0.0
        self.on_advance = lambda: None

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.on_advance()


class Log:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.failure = False

    def newest_log_id(self, room_id):
        if self.failure:
            raise OSError("synthetic read failure")
        return max((row.id for row in self.rows), default=None)

    def messages(self, room_id, limit=40, *, after_id=None):
        if self.failure:
            raise OSError("synthetic read failure")
        return [row for row in self.rows if row.id > after_id][:limit]


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.timer = patch.object(confirm, "time", self.clock)
        self.timer.start()
        self.addCleanup(self.timer.stop)

    def observation(self, log):
        return confirm.Confirmation(log, 7, "synthetic", 0.25, (OSError,))

    def test_only_new_exact_from_me_matches_even_across_full_pages(self):
        log = Log([Row(1)])
        observation = self.observation(log)
        observation.capture()
        log.rows.extend([Row(i, from_me=False) for i in range(2, 104)])
        log.rows.extend([Row(104, "synthetic "), Row(105)])

        result = observation.check()

        self.assertEqual(result, {"logged": True, "messageId": "105"})

    def test_repeated_identical_text_never_matches_previous_submission(self):
        log = Log([Row(1)])
        first = self.observation(log)
        first.capture()
        log.rows.append(Row(2))
        self.assertEqual(first.check(), {"logged": True, "messageId": "2"})
        second = self.observation(log)
        second.capture()

        result = second.check()

        self.assertEqual(result, {"logged": False})
        self.assertEqual(self.clock.now, 0.25)

    def test_delayed_row_is_observed_before_deadline(self):
        log = Log()
        observation = self.observation(log)
        observation.capture()
        self.clock.on_advance = lambda: log.rows.append(Row(3))

        self.assertEqual(observation.check(), {"logged": True, "messageId": "3"})

    def test_deadline_does_not_accept_a_late_matching_row(self):
        log = Log()
        observation = self.observation(log)
        observation.capture()
        self.clock.on_advance = lambda: (
            log.rows.append(Row(3)) if self.clock.now >= 0.25 else None
        )

        self.assertEqual(observation.check(), {"logged": False})

    def test_failed_baseline_and_failed_post_submit_read_are_unknown(self):
        for baseline_failure in (True, False):
            with self.subTest(baseline_failure=baseline_failure):
                log = Log([Row(1)])
                log.failure = baseline_failure
                observation = self.observation(log)
                observation.capture()
                log.failure = not baseline_failure

                self.assertEqual(observation.check(), {"logged": False})

    def test_trace_reports_local_observation_without_changing_send_outcome(self):
        for found in (True, False):
            with self.subTest(found=found), tempfile.TemporaryDirectory() as directory:
                log = Log()
                observation = self.observation(log)
                observation.capture()
                if found:
                    log.rows.append(Row(4))
                with trace.attempt(directory, trace.Identity("macos", "kamado", "test"), "cli"):
                    observation.check()
                rows = [json.loads(line) for line in next(Path(directory).glob("*.jsonl")).read_text().splitlines()]
                self.assertEqual(rows[-1]["conditions"]["L"], "pass" if found else "unknown")
                self.assertEqual(rows[-1]["outcome"], "pass")

    def test_seconds_are_finite_positive_and_bounded(self):
        for value in ("nan", "inf", "0", "-1", "30.01", True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                confirm.parse_seconds(value)
        self.assertEqual(confirm.parse_seconds("30"), 30)
