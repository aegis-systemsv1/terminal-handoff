"""Regression: New Session on the phone reported "project and a non-empty task are required" for
a visibly non-empty task. The task was over the length limit; the server called it empty."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import CORE  # noqa: E402
from test_session_names import NameCase  # noqa: E402


class TestNewSessionTask(NameCase):
    def test_multiline_task_with_project_and_name_starts(self):
        status, view = self.create(request_id="req-task-ok-1", name="ShipSure", task="Line one of the task.\n\nLine two.\n  - indented point\nLine four.")
        self.assertEqual(status, 201, view)
        self.assertEqual(view["name"], "ShipSure")
        self.assertEqual(len(self.launched), 1)

    def test_task_at_the_limit_starts(self):
        status, view = self.create(request_id="req-task-limit-1", task="x" * CORE.MAX_TASK_CHARS)
        self.assertEqual(status, 201, view)

    def test_a_task_between_the_old_and_new_limits_now_starts(self):
        status, view = self.create(request_id="req-task-mid-1", task="y" * 20000)
        self.assertEqual(status, 201, view)

    def test_follow_up_instructions_keep_their_own_limit(self):
        self.assertEqual(CORE.MAX_INSTRUCTION_CHARS, 8000)
        self.assertEqual(CORE.MAX_TASK_CHARS, 24000)

    def test_over_long_task_is_reported_as_too_long_not_empty(self):
        status, body = self.create(request_id="req-task-long-1", name="ShipSure", task="A" * 25123)
        self.assertEqual(status, 400)
        self.assertEqual(body["reason"], "Task is too long: 25,123 characters; maximum is 24,000")
        self.assertNotIn("non-empty", body["reason"])
        self.assertEqual(self.launched, [])  # validation is not weakened: nothing starts

    def test_empty_and_missing_are_still_refused_with_their_own_reasons(self):
        for i, (project, task, needle) in enumerate((("nova", "   \n ", "non-empty task"), ("nova", None, "non-empty task"), ("nova", "\x00\x01", "non-empty task"))):
            ctx = {"device_id": "d_0123456789abcdef"}
            body = {"project": project, "task": task, "request_id": "req-task-empty-%d" % i}
            status, payload = CORE.remote_create_session(body, ctx, terminal=self.fake_terminal, wait_seconds=0, isolation_check=self.isolation_check)
            self.assertEqual(status, 400)
            self.assertIn(needle, payload["reason"])
        status, payload = CORE.remote_create_session({"task": "do it", "request_id": "req-task-noproj"}, {"device_id": "d_0123456789abcdef"}, terminal=self.fake_terminal, wait_seconds=0, isolation_check=self.isolation_check)
        self.assertEqual((status, payload["reason"]), (400, "a project is required"))
        self.assertEqual(self.launched, [])


if __name__ == "__main__":
    unittest.main()
