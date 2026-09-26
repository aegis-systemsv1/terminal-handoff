"""The New Session screen for very large prompts: it shows the size as you paste, refuses to send what cannot
fit (never truncating), keeps the task when a launch is rejected and retries with a fresh request id,
and says plainly that the whole task was accepted and when a stale session was recovered for it."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_ui import ME, LSID, NODE, run_ui, view  # noqa: E402

PROJECTS = ("GET /api/v1/projects", [200, {"projects": ["nova"], "agents": {"nova": ["claude"]}, "limits": {"task_max_bytes": 524288}}])
TASK = "héllo \U0001f680\nsecond line"  # 19 code points, 23 UTF-8 bytes, 2 lines


def routes(post=None):
    r = {ME[0]: ME[1], PROJECTS[0]: PROJECTS[1]}
    if post is not None:
        r["POST /api/v1/sessions"] = post
    return r


def posts(out):
    return [c for c in out["calls"] if c["method"] == "POST"]


@unittest.skipIf(NODE is None, "node is required for the UI tests")
class TestNewSessionLargePrompt(unittest.TestCase):
    def test_the_size_is_shown_as_the_mac_will_count_it(self):
        out = run_ui("#/new", routes(), [{"type": "textarea", "value": TASK}, {"poll": True}])
        self.assertIn("19 characters · 23 bytes · 2 lines (limit 524,288 bytes)", out["all"])

    def test_a_trailing_newline_is_not_an_extra_line_and_an_empty_box_says_how_to_start(self):
        out = run_ui("#/new", routes(), [{"type": "textarea", "value": "a\nb\n"}, {"poll": True}])
        self.assertIn("4 characters · 4 bytes · 2 lines", out["all"])
        empty = run_ui("#/new", routes(), [{"poll": True}])
        self.assertIn("kept exactly as written", empty["all"])

    def test_an_over_limit_task_is_flagged_and_never_sent(self):
        big = "x" * 524289
        out = run_ui("#/new", routes([201, view()]), [{"type": "textarea", "value": big}, {"poll": True}, {"click": "Start Session"}])
        self.assertIn("Too large: 524,289 bytes (524,289 characters); the maximum is 524,288 bytes. Nothing will be truncated", out["all"])
        self.assertEqual(posts(out), [])

    def test_a_task_at_the_limit_is_sent_whole(self):
        exact = "y" * 524288
        out = run_ui("#/new", routes([201, view()]), [{"type": "textarea", "value": exact}, {"poll": True}, {"click": "Start Session"}])
        self.assertEqual(len(posts(out)), 1)
        self.assertEqual(posts(out)[0]["body"]["task"], exact)  # the whole thing, untouched

    def test_the_task_is_sent_exactly_as_typed_including_whitespace(self):
        text = "  indented\n\n\ttab \r\nlast  \n\n"
        out = run_ui("#/new", routes([201, view()]), [{"type": "textarea", "value": text}, {"click": "Start Session"}])
        self.assertEqual(posts(out)[0]["body"]["task"], text)

    def test_a_rejected_launch_keeps_the_task_and_the_retry_is_a_new_request(self):
        refused = [409, {"error": "project_in_use", "reason": "Project 'nova' is in use by session Nova Health (RUNNING, Claude Code). Its owner is alive."}]
        out = run_ui("#/new", routes(refused), [
            {"type": "textarea", "value": TASK}, {"click": "Start Session"}, {"snap": "after_reject"},
            {"routes": {"POST /api/v1/sessions": [201, view()]}}, {"click": "Start Session"},
        ])
        self.assertIn("Not started: Project 'nova' is in use by session Nova Health (RUNNING, Claude Code).", out["snaps"]["after_reject"]["text"])
        self.assertIn("Your task is still here.", out["snaps"]["after_reject"]["text"])
        kept = [b for b in out["snaps"]["after_reject"]["boxes"] if b["tag"] == "textarea"]
        self.assertEqual(kept[0]["value"], TASK)
        first, second = posts(out)
        self.assertEqual(first["body"]["task"], second["body"]["task"])
        self.assertNotEqual(first["body"]["request_id"], second["body"]["request_id"])  # a definitive answer is never replayed

    def test_a_too_large_refusal_from_the_mac_is_shown_with_its_numbers(self):
        reason = "Task is too large: 524,289 bytes (524,289 characters); the maximum is 524,288 bytes"
        out = run_ui("#/new", routes([413, {"error": "task_too_large", "reason": reason}]), [{"type": "textarea", "value": "z"}, {"click": "Start Session"}])
        self.assertIn("Not started: " + reason, out["all"])

    def test_acceptance_says_the_whole_task_arrived(self):
        meta = {"chars": 19, "bytes": 23, "lines": 2, "parts": 1, "parts_read": 0, "fingerprint": "abc123def456"}
        out = run_ui("#/new", routes([201, view(task=meta)]), [{"type": "textarea", "value": TASK}, {"click": "Start Session"}])
        self.assertIn("Accepted the whole task: 19 characters, 23 bytes, 2 lines (fingerprint abc123def456).", out["all"])

    def test_a_recovered_stale_session_is_reported_and_the_same_launch_continues(self):
        meta = {"chars": 19, "bytes": 23, "lines": 2, "parts": 1, "parts_read": 0, "fingerprint": "abc123def456"}
        recovered = [{"logical_session_id": "ls_" + "b" * 24, "name": "Brag Skill", "previous_state": "ORPHANED", "reason": "gone"}]
        out = run_ui("#/new", routes([201, view(task=meta, auto_recovered=recovered)]), [{"type": "textarea", "value": TASK}, {"click": "Start Session"}])
        self.assertEqual(len(posts(out)), 1)  # one launch: no second paste, no second tap
        self.assertIn("A stale session (Brag Skill) was recovered and your launch continued.", out["all"])

    def test_the_task_box_is_built_for_long_text(self):
        out = run_ui("#/new", routes())
        attrs = {a[1]: a[2] for a in out["attrs"] if a[0] == "textarea" and a[2] is not None}
        self.assertEqual((attrs["autocapitalize"], attrs["autocorrect"], attrs["spellcheck"]), ("off", "off", "false"))
        self.assertGreaterEqual(int(attrs["rows"]), 10)
        self.assertIn("mono", attrs["class"] if "class" in attrs else "mono")

    def test_the_session_page_shows_how_much_of_the_stored_task_the_agent_has_read(self):
        meta = {"chars": 250000, "bytes": 260000, "lines": 4000, "parts": 16, "parts_read": 3, "fingerprint": "f" * 12, "inline": False}
        out = run_ui("#/s/" + LSID, {ME[0]: ME[1], "GET /api/v1/sessions/" + LSID: [200, view(task=meta)]})
        self.assertIn("Full task stored: 250000 characters, 260000 bytes, 4000 lines", out["all"])
        self.assertIn("read by the agent so far: 3 of 16 parts", out["all"])

    def test_an_inline_task_is_not_presented_as_a_partly_read_stored_task(self):
        meta = {"chars": 12, "bytes": 12, "lines": 1, "parts": 1, "parts_read": 0, "fingerprint": "a" * 12, "inline": True}
        out = run_ui("#/s/" + LSID, {ME[0]: ME[1], "GET /api/v1/sessions/" + LSID: [200, view(task=meta)]})
        self.assertNotIn("Full task stored", out["all"])
        self.assertNotIn("read by the agent", out["all"])


if __name__ == "__main__":
    unittest.main()
