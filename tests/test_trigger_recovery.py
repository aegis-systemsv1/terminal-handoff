"""An automatic trigger whose status-line process dies after claiming but before launching
must not strand the session forever (seen live: the 5% trigger claimed, then never launched)."""

import json
import os
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import CORE, THTestCase, json_file, run_th, text_file, wait_for  # noqa: E402


class RecoveryCase(THTestCase):
    def setUp(self):
        super().setUp()
        self._home = os.environ.get("CLAUDE_TERMINAL_HANDOFF_HOME")
        os.environ["CLAUDE_TERMINAL_HANDOFF_HOME"] = self.home
        CORE.ensure_dirs()

    def tearDown(self):
        if self._home is None:
            os.environ.pop("CLAUDE_TERMINAL_HANDOFF_HOME", None)
        else:
            os.environ["CLAUDE_TERMINAL_HANDOFF_HOME"] = self._home
        super().tearDown()

    def dead_pid(self):
        proc = subprocess.Popen(["/usr/bin/true"])
        proc.wait()
        return proc.pid

    def claim(self, sid, age=400.0, pid=None, mode="automatic"):
        CORE.write_json_private(
            CORE.th_path("triggered", sid),
            {"claimed_epoch": time.time() - age, "claimed_utc": "x", "mode": mode, "pid": self.dead_pid() if pid is None else pid},
        )

    def marker(self, sid):
        return os.path.exists(CORE.th_path("triggered", sid))

    def log(self):
        return text_file(os.path.join(self.home, "logs", "terminal-handoff.log"))


class TestStaleClaimRecovery(RecoveryCase):
    def test_an_old_orphaned_claim_with_no_launch_trace_is_released(self):
        self.claim("s-orphan-0001")
        self.assertTrue(CORE.recover_stale_trigger_claim("s-orphan-0001"))
        self.assertFalse(self.marker("s-orphan-0001"))
        self.assertIn("stale_trigger_claim_released", self.log())

    def test_a_fresh_claim_is_left_alone(self):
        self.claim("s-fresh-00001", age=5.0)
        self.assertFalse(CORE.recover_stale_trigger_claim("s-fresh-00001"))
        self.assertTrue(self.marker("s-fresh-00001"))

    def test_a_claim_whose_process_is_still_running_is_left_alone(self):
        self.claim("s-alive-00001", age=120.0, pid=os.getpid())
        self.assertFalse(CORE.recover_stale_trigger_claim("s-alive-00001"))
        self.assertTrue(self.marker("s-alive-00001"))

    def test_a_very_old_claim_is_released_even_if_its_pid_was_reused(self):
        self.claim("s-reused-0001", age=CORE.STALE_CLAIM_HARD_SECONDS + 10, pid=os.getpid())
        self.assertTrue(CORE.recover_stale_trigger_claim("s-reused-0001"))

    def test_any_launch_trace_means_the_launch_progressed_and_the_claim_stands(self):
        for name, path in (
            ("transfer", CORE.transfer_path("s-trace-00001")),
            ("manifest", CORE.manifest_path("s-trace-00001")),
            ("script", CORE.th_path("launching", "s-trace-00001.launch.sh")),
            ("completed", CORE.th_path("completed", "s-trace-00001.launch.json")),
            ("failed", CORE.th_path("failed", "s-trace-00001.json")),
        ):
            with self.subTest(name):
                sid = "s-trace-%s" % name
                self.claim(sid)
                real = path.replace("s-trace-00001", sid)
                os.makedirs(os.path.dirname(real), exist_ok=True)
                CORE.write_json_private(real, {})
                self.assertFalse(CORE.recover_stale_trigger_claim(sid))
                self.assertTrue(self.marker(sid))

    def test_manual_claims_and_garbage_markers_are_never_touched(self):
        self.claim("s-manual-0001", mode="manual")
        self.assertFalse(CORE.recover_stale_trigger_claim("s-manual-0001"))
        with open(CORE.th_path("triggered", "s-garbage-001"), "w") as handle:
            handle.write("not json")
        self.assertFalse(CORE.recover_stale_trigger_claim("s-garbage-001"))
        self.assertFalse(CORE.recover_stale_trigger_claim("s-nothing-0001"))

    def test_recovery_is_bounded(self):
        results = []
        for _ in range(CORE.MAX_STALE_RECLAIMS + 2):
            self.claim("s-bounded-001")
            results.append(CORE.recover_stale_trigger_claim("s-bounded-001"))
        self.assertEqual(results, [True] * CORE.MAX_STALE_RECLAIMS + [False, False])
        self.assertTrue(self.marker("s-bounded-001"))  # after the budget the claim finally stands


class TestThroughTheDecisionEngine(RecoveryCase):
    def test_an_orphaned_claim_lets_the_next_status_line_retry(self):
        payload = self.payload(percent=90.0)
        sid = payload["_session_id"]
        self.claim(sid)
        decision = self.evaluate(payload)
        self.assertTrue(decision["trigger"], decision)
        self.assertFalse(self.marker(sid))

    def test_evaluate_without_recording_never_releases_a_claim(self):
        payload = self.payload(percent=90.0)
        sid = payload["_session_id"]
        self.claim(sid)
        sid_key = payload.pop("_session_id")
        code, out, err = run_th(["evaluate", "--no-record"], json.dumps(payload), self.env())
        payload["_session_id"] = sid_key
        decision = json.loads(out)
        self.assertFalse(decision["trigger"])
        self.assertEqual(decision["state"], "handed_off")
        self.assertTrue(self.marker(sid))

    def test_a_young_or_progressed_claim_still_reports_handed_off(self):
        payload = self.payload(percent=90.0)
        self.claim(payload["_session_id"], age=5.0)
        decision = self.evaluate(payload)
        self.assertEqual((decision["trigger"], decision["state"]), (False, "handed_off"))

    def test_end_to_end_the_status_line_relaunches_after_an_orphaned_claim(self):
        payload = self.payload(percent=90.0)
        sid = payload["_session_id"]
        self.claim(sid)
        code, out, err = self.statusline(payload)
        self.assertEqual(code, 0, err)
        self.assertTrue(wait_for(self.launch_record(sid), 30), "the retry never launched")
        self.assertIn("stale_trigger_claim_released", self.log())
        self.assertIn('"trigger_claimed"', self.log())
        claim = json_file(CORE.th_path("triggered", sid))
        self.assertGreater(claim["claimed_epoch"], time.time() - 60)  # a fresh claim, not the orphan

    def test_a_normal_first_trigger_is_unchanged(self):
        payload = self.payload(percent=90.0)
        code, out, err = self.statusline(payload)
        self.assertEqual(code, 0, err)
        self.assertTrue(wait_for(self.launch_record(payload["_session_id"]), 30))
        self.assertNotIn("stale_trigger_claim_released", self.log())

    def test_the_parent_is_bound_before_the_claim_is_taken(self):
        source = text_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "terminal_handoff", "core.py"))
        block = source[source.index("binding, bind_reason = None, \"parent shutdown disabled by configuration\""):]
        self.assertLess(block.index("bind_parent_claude_process("), block.index("claim_trigger(session_id)"))


if __name__ == "__main__":
    unittest.main()
