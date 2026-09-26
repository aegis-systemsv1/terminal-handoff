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
        status, view = self.create(request_id="req-task-limit-1", task="x" * CORE.MAX_TASK_BYTES)
        self.assertEqual(status, 201, view)
        self.assertEqual(view["task"]["bytes"], CORE.MAX_TASK_BYTES)

    def test_a_task_between_the_old_and_new_limits_now_starts(self):
        status, view = self.create(request_id="req-task-mid-1", task="y" * 200000)
        self.assertEqual(status, 201, view)

    def test_follow_up_instructions_keep_their_own_limit(self):
        self.assertEqual(CORE.MAX_INSTRUCTION_CHARS, 8000)
        self.assertEqual(CORE.MAX_TASK_BYTES, 524288)

    def test_over_long_task_is_reported_as_too_large_not_empty_or_truncated(self):
        status, body = self.create(request_id="req-task-long-1", name="ShipSure", task="A" * (CORE.MAX_TASK_BYTES + 123))
        self.assertEqual(status, 413)
        self.assertEqual(body["error"], "task_too_large")
        self.assertEqual(body["reason"], "Task is too large: 524,411 bytes (524,411 characters); the maximum is 524,288 bytes")
        self.assertEqual((body["bytes"], body["chars"], body["limit_bytes"]), (524411, 524411, 524288))
        self.assertNotIn("non-empty", body["reason"])
        self.assertEqual(self.launched, [])  # validation is not weakened: nothing starts

    def test_the_limit_is_in_bytes_not_characters(self):
        status, body = self.create(request_id="req-task-uni-1", task="\u00e9" * (CORE.MAX_TASK_BYTES // 2 + 1))  # 2 bytes each
        self.assertEqual(status, 413, body)
        self.assertEqual(body["chars"], CORE.MAX_TASK_BYTES // 2 + 1)
        self.assertEqual(body["bytes"], 2 * (CORE.MAX_TASK_BYTES // 2 + 1))

    def test_empty_and_missing_are_still_refused_with_their_own_reasons(self):
        for i, (project, task, needle) in enumerate((("nova", "   \n ", "non-empty task"), ("nova", None, "non-empty task"), ("nova", "abc\x00def", "NUL character"))):
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
