"""Very large prompts on the Grok path: the real gateway, the real bridge process and ACP client,
and the scriptable fake `grok agent stdio`. What the fake receives as `session/prompt` text is
compared byte for byte (SHA-256) with what was submitted."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import CORE  # noqa: E402
from test_grok_agent import GrokCase  # noqa: E402
from test_large_prompts import KB, MARKDOWN, PROSE, SHELLISH, UNICODE, sha, sized  # noqa: E402


class TestGrokLargePrompts(GrokCase):
    def submit(self, task):
        lsid = self.start(task=task)
        prompt = self.wait_for(lambda: self.prompts(), timeout=60, what="the bridge to send the task to Grok")
        return lsid, prompt[0]

    def test_a_250kb_unicode_markdown_task_reaches_grok_exactly(self):
        task = sized(UNICODE + MARKDOWN + SHELLISH, 250 * KB)
        lsid, received = self.submit(task)
        self.assertEqual(sha(received), sha(task))
        self.assertEqual(received, task)
        message = CORE.logical_read(lsid)["inbox"]["messages"][0]
        self.assertIsNotNone(message.get("task_ref"))  # it travelled by reference, not through the inbox record
        self.assertLess(len(message["text"]), 3000)
        self.assertEqual(CORE.logical_read(lsid)["task"]["sha256"], sha(task))

    def test_the_maximum_task_reaches_grok_exactly(self):
        task = sized(UNICODE + PROSE, CORE.MAX_TASK_BYTES)
        lsid, received = self.submit(task)
        self.assertEqual(len(received.encode("utf-8")), CORE.MAX_TASK_BYTES)
        self.assertEqual(sha(received), sha(task))

    def test_a_small_task_is_still_sent_inline_and_exact(self):
        task = "  say hello\n\tindented\n"
        lsid, received = self.submit(task)
        self.assertEqual(received, task)
        self.assertIsNone(CORE.logical_read(lsid)["inbox"]["messages"][0].get("task_ref"))

    def test_a_missing_or_altered_payload_is_never_sent_partly(self):
        # Start no bridge: the message waits, then the payload is altered before the bridge would deliver it.
        body = {"project": "nova", "task": sized(PROSE, 60 * KB), "agent": "grok", "request_id": "req-grok-tamper-1"}
        launched = []
        status, view = CORE.remote_create_session(
            body, {"device_id": "d_0123456789abcdef"}, terminal=self.fake_terminal, wait_seconds=0,
            isolation_check=self.isolation_check, grok_preflight=lambda binary: (True, None, None),
            grok_launcher=lambda lsid, mode: launched.append(lsid) or {"launched": True},
        )
        self.assertEqual(status, 202, view)
        lsid = view["logical_session_id"]
        with open(CORE.task_path(lsid), "ab") as handle:
            handle.write(b"tampered")
        message = CORE.logical_read(lsid)["inbox"]["messages"][0]
        bridge = CORE.GrokBridge.__new__(CORE.GrokBridge)
        bridge.lsid, bridge.sid = lsid, "grok-session-x"
        audits, lines, histories = [], [], []
        bridge.history = lambda *a, **k: histories.append(a)
        bridge.audit = lambda *a, **k: audits.append(a)
        bridge.add_line = lines.append

        def no_client():
            raise AssertionError("nothing may be sent to Grok")

        bridge.ensure_client = no_client
        bridge.deliver(message)
        self.assertIn("task_payload_unavailable", [a[0] for a in audits])
        self.assertEqual(histories, [("task_payload_unavailable",)])
        self.assertTrue(any("could not be loaded" in ln for ln in lines))
        self.assertEqual(self.prompts(), [])


if __name__ == "__main__":
    unittest.main()
