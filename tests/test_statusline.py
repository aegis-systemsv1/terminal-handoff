"""Status-line rendering and wrapping a pre-existing status line.

Ported unchanged from the proven Terminal Handoff 1.0.0 suite.
"""
import json
import io
import os
import shutil
import subprocess
import sys
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import (  # noqa: E402
    CORE,
    THTestCase,
    json_file,
    run_th,
    text_file,
    wait_for,
)


VENDOR_STATUSLINE_SCRIPT = r"""#!/usr/bin/env node
// Stand-in for a real-world project status line: reads the Claude
// status JSON from fd 0 and prints one decorated line.
const fs = require('fs');
let raw = '';
try { raw = fs.readFileSync(0, 'utf-8'); } catch (e) { raw = ''; }
let data = null;
try { data = JSON.parse(raw); } catch (e) { data = null; }
const model = data && data.model ? data.model.display_name : 'unknown';
const pct = data && data.context_window && data.context_window.used_percentage != null
  ? Math.floor(data.context_window.used_percentage) : '--';
console.log(`\x1b[0;36mVENDOR\x1b[0m | ${model} | ctx ${pct}% | agents 3/15`);
"""


class TestStatusLineWrapping(THTestCase):
    def setUp(self):
        super(TestStatusLineWrapping, self).setUp()
        self.inner = os.path.join(self.tmp, "statusline.cjs")
        with open(self.inner, "w") as handle:
            handle.write(VENDOR_STATUSLINE_SCRIPT)
        os.chmod(self.inner, 0o755)
        self.wrap_command = 'node "%s"' % self.inner
        self.node = shutil.which("node")

    def inner_output(self, payload):
        proc = subprocess.Popen(
            ["/bin/sh", "-c", self.wrap_command],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out, _ = proc.communicate(json.dumps(payload).encode("utf-8"))
        return out

    def test_43_61_existing_status_line_output_remains_intact(self):
        if not self.node:
            self.skipTest("node is not installed")
        payload = self.payload(percent=42.0)
        clean = {k: v for k, v in payload.items() if k != "_session_id"}
        expected = self.inner_output(clean).rstrip(b"\r\n")
        code, out, err = self.statusline(payload, wrap=self.wrap_command)
        self.assertEqual(code, 0, err)
        produced = out.rstrip("\n").encode("utf-8")
        self.assertTrue(
            produced.startswith(expected),
            "existing output was not preserved byte-for-byte.\n expected prefix: %r\n produced: %r"
            % (expected, produced),
        )
        self.assertIn(b"VENDOR", produced)
        self.assertIn(b"agents 3/15", produced)
        self.assertIn(b"\x1b[0;36m", produced, "ANSI colours were stripped")
        self.assertIn("TH 42%", out)

    def test_62_wrapped_command_receives_identical_status_json(self):
        if not self.node:
            self.skipTest("node is not installed")
        capture = os.path.join(self.tmp, "captured.json")
        sink = os.path.join(self.tmp, "sink.sh")
        with open(sink, "w") as handle:
            handle.write("#!/bin/sh\ncat > %s\necho 'INNER OK'\n" % capture)
        os.chmod(sink, 0o700)
        payload = self.payload(percent=50.0)
        clean = {k: v for k, v in payload.items() if k != "_session_id"}
        sent = json.dumps(clean)
        code, out, err = run_th(["statusline", "--wrap", sink], sent, self.env())
        self.assertEqual(code, 0, err)
        received = text_file(capture)
        self.assertEqual(json.loads(received), json.loads(sent), "inner command got different JSON")
        self.assertEqual(received, sent, "inner command did not receive the identical bytes")
        self.assertIn("INNER OK", out)

    def test_63_wrapped_seventy_nine_does_not_trigger(self):
        payload = self.payload(percent=79.0)
        code, out, err = self.statusline(payload, wrap=self.wrap_command)
        self.assertEqual(code, 0)
        self.assertIn("TH 79%", out)
        time.sleep(1.0)
        self.assertFalse(os.path.exists(self.launch_record(payload["_session_id"])))

    def test_64_wrapped_eighty_triggers_in_simulation(self):
        payload = self.payload(percent=80.0)
        code, out, err = self.statusline(payload, wrap=self.wrap_command)
        self.assertEqual(code, 0, err)
        self.assertTrue(wait_for(self.launch_record(payload["_session_id"])))
        self.assertIn("TH launching", out)
        record = json_file(self.launch_record(payload["_session_id"]))
        self.assertTrue(record["test_mode"])

    def test_65_wrapped_malformed_and_null_input_fails_safely(self):
        if not self.node:
            self.skipTest("node is not installed")
        code, out, err = run_th(["statusline", "--wrap", self.wrap_command], "{broken", self.env())
        self.assertEqual(code, 0)
        self.assertIn("VENDOR", out, "inner output lost on malformed input")
        self.assertIn("TH", out)
        payload = self.payload(percent=None)
        code, out, err = self.statusline(payload, wrap=self.wrap_command)
        self.assertEqual(code, 0)
        self.assertIn("VENDOR", out)
        self.assertIn("TH ready", out)
        time.sleep(0.5)
        self.assertFalse(os.path.exists(self.launch_record(payload["_session_id"])))

    def test_wrapped_command_failure_does_not_break_terminal_handoff(self):
        payload = self.payload(percent=80.0)
        code, out, err = self.statusline(payload, wrap="/nonexistent/command --flag")
        self.assertEqual(code, 0)
        self.assertIn("TH", out)
        self.assertTrue(wait_for(self.launch_record(payload["_session_id"])))

    def test_wrapped_command_timeout_does_not_hang_the_status_line(self):
        payload = self.payload(percent=10.0)
        started = time.time()
        code, out, err = self.statusline(payload, wrap="sleep 30")
        elapsed = time.time() - started
        self.assertEqual(code, 0)
        self.assertLess(elapsed, 10, "status line hung on a slow wrapped command")

    def test_detached_launcher_spawn_failure_is_visible_and_retryable(self):
        payload = self.payload(percent=90.0)
        session_id = payload.pop("_session_id")
        raw = json.dumps(payload).encode("utf-8")

        class Input(object):
            buffer = io.BytesIO(raw)

        output = io.StringIO()
        env = self.env(CLAUDE_TERMINAL_HANDOFF_STOP_PARENT="0")
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            CORE, "spawn_launcher", return_value=False
        ), mock.patch.object(CORE.sys, "stdin", Input()), mock.patch.object(
            CORE.sys, "stdout", output
        ):
            code = CORE.cmd_statusline(SimpleNamespace(wrap=None))

        self.assertEqual(code, 0)
        self.assertIn("TH failed", output.getvalue())
        failure = json_file(os.path.join(self.home, "failed", "%s.json" % session_id))
        self.assertIn("detached launcher", failure["reason"])
        pending = os.path.join(self.home, "outbox", "pending")
        events = [name for name in os.listdir(pending) if name.endswith(".json")]
        self.assertEqual(len(events), 1)
        event = json_file(os.path.join(pending, events[0]))["event"]
        self.assertEqual(event["event_type"], "terminal_handoff.failed")
        self.assertEqual(event["owner"], "parent")
        self.assertFalse(
            os.path.exists(os.path.join(self.home, "triggered", session_id)),
            "the failed claim was not released for a bounded retry",
        )


# ---------------------------------------------------------------------------
# 10. Install / uninstall / rollback (simulated against copies)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
