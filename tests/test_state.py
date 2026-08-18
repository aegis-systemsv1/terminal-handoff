"""Trigger lifecycle, generations, kill switch, cooldown, circuit breaker, retry.

Ported unchanged from the proven Terminal Handoff 1.0.0 suite.
"""
import json
import os
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import (  # noqa: E402
    PYTHON,
    TH_SCRIPT,
    THTestCase,
    json_file,
    run_th,
    text_file,
    wait_for,
)


class TestTriggerLifecycle(THTestCase):
    def test_22_same_session_triggers_once(self):
        payload = self.payload(percent=90.0)
        ok, _ = self.trigger_and_wait(payload)
        self.assertTrue(ok)
        first = json_file(self.launch_record(payload["_session_id"]))
        code, out, err = self.statusline(payload)
        self.assertEqual(code, 0)
        self.assertIn("handed off", out)
        time.sleep(1.0)
        second = json_file(self.launch_record(payload["_session_id"]))
        self.assertEqual(first["ts"], second["ts"], "a second launch overwrote the first")

    def test_23_different_sessions_trigger_independently(self):
        a = self.payload(percent=90.0)
        b = self.payload(percent=90.0)
        self.assertNotEqual(a["_session_id"], b["_session_id"])
        self.assertTrue(self.trigger_and_wait(a)[0])
        self.assertTrue(self.trigger_and_wait(b)[0])
        self.assertTrue(os.path.exists(self.launch_record(a["_session_id"])))
        self.assertTrue(os.path.exists(self.launch_record(b["_session_id"])))

    def test_24_51_successor_can_trigger_its_own_successor(self):
        parent = self.payload(percent=90.0)
        self.assertTrue(self.trigger_and_wait(parent)[0])
        parent_manifest = self.manifest(parent["_session_id"])
        manifest = json_file(parent_manifest)

        successor_env = self.env(
            CLAUDE_TERMINAL_HANDOFF_MANIFEST=parent_manifest,
            CLAUDE_TERMINAL_HANDOFF_CHAIN_ID=manifest["chain_id"],
            CLAUDE_TERMINAL_HANDOFF_GENERATION="2",
            CLAUDE_TERMINAL_HANDOFF_PARENT_SESSION=parent["_session_id"],
        )
        successor = self.payload(percent=90.0)
        # The parent's triggered marker must not block the successor.
        self.assertTrue(os.path.exists(os.path.join(self.home, "triggered", parent["_session_id"])))
        code, out, err = self.statusline(successor, successor_env)
        self.assertEqual(code, 0, err)
        self.assertTrue(wait_for(self.launch_record(successor["_session_id"])))
        successor_manifest = json_file(self.manifest(successor["_session_id"]))
        self.assertEqual(successor_manifest["chain_id"], manifest["chain_id"])
        self.assertEqual(successor_manifest["generation"], 2)
        self.assertEqual(successor_manifest["parent_handoff_manifest"], parent_manifest)

    def test_25_three_generations_simulate(self):
        chain = None
        parent_manifest = None
        parent_session = None
        sessions = []
        for generation in range(1, 4):
            payload = self.payload(percent=90.0)
            sessions.append(payload["_session_id"])
            overrides = {}
            if chain:
                overrides = {
                    "CLAUDE_TERMINAL_HANDOFF_CHAIN_ID": chain,
                    "CLAUDE_TERMINAL_HANDOFF_GENERATION": str(generation),
                    "CLAUDE_TERMINAL_HANDOFF_MANIFEST": parent_manifest,
                    "CLAUDE_TERMINAL_HANDOFF_PARENT_SESSION": parent_session,
                }
            env = self.env(**overrides)
            code, out, err = self.statusline(payload, env)
            self.assertEqual(code, 0, err)
            self.assertTrue(
                wait_for(self.launch_record(payload["_session_id"])),
                "generation %d did not launch" % generation,
            )
            manifest = json_file(self.manifest(payload["_session_id"]))
            self.assertEqual(manifest["generation"], generation)
            chain = chain or manifest["chain_id"]
            self.assertEqual(manifest["chain_id"], chain, "chain ID changed between generations")
            parent_manifest = self.manifest(payload["_session_id"])
            parent_session = payload["_session_id"]
        self.assertEqual(len(set(sessions)), 3, "sessions were not distinct")

    def test_26_unlimited_generations_has_no_artificial_stop(self):
        payload = self.payload(percent=90.0)
        env = self.env(
            CLAUDE_TERMINAL_HANDOFF_CHAIN_ID="abc123def456",
            CLAUDE_TERMINAL_HANDOFF_GENERATION="42",
        )
        decision = self.evaluate(payload, env)
        self.assertTrue(decision["trigger"], decision)
        self.assertEqual(decision["generation"], 42)

    def test_27_max_generations_stops_correctly(self):
        env = self.env(
            CLAUDE_TERMINAL_HANDOFF_MAX_GENERATIONS="3",
            CLAUDE_TERMINAL_HANDOFF_CHAIN_ID="abc123def456",
            CLAUDE_TERMINAL_HANDOFF_GENERATION="3",
        )
        decision = self.evaluate(self.payload(percent=90.0), env)
        self.assertFalse(decision["trigger"])
        self.assertEqual(decision["state"], "max_generations")
        env2 = self.env(
            CLAUDE_TERMINAL_HANDOFF_MAX_GENERATIONS="3",
            CLAUDE_TERMINAL_HANDOFF_CHAIN_ID="abc123def456",
            CLAUDE_TERMINAL_HANDOFF_GENERATION="2",
        )
        self.assertTrue(self.evaluate(self.payload(percent=90.0), env2)["trigger"])

    def test_28_70_concurrent_invocations_produce_one_launch(self):
        payload = self.payload(percent=90.0)
        text = json.dumps({k: v for k, v in payload.items() if k != "_session_id"})
        procs = []
        for _ in range(8):
            procs.append(
                subprocess.Popen(
                    [PYTHON, TH_SCRIPT, "statusline"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=self.env(),
                )
            )
        for proc in procs:
            proc.communicate(text.encode("utf-8"), timeout=60)
        self.assertTrue(wait_for(self.launch_record(payload["_session_id"])))
        time.sleep(2.0)
        launches = [
            line
            for line in text_file(os.path.join(self.home, "state", "launches.jsonl")).splitlines()
            if line.strip()
        ]
        self.assertEqual(len(launches), 1, "expected exactly one launch, got %d" % len(launches))
        triggered = os.listdir(os.path.join(self.home, "triggered"))
        self.assertEqual(len(triggered), 1)

    def test_50_52_successor_identity_is_fresh_and_independent(self):
        parent = self.payload(percent=95.0)
        self.assertTrue(self.trigger_and_wait(parent)[0])
        parent_manifest = self.manifest(parent["_session_id"])
        manifest = json_file(parent_manifest)
        successor_env = self.env(
            CLAUDE_TERMINAL_HANDOFF_MANIFEST=parent_manifest,
            CLAUDE_TERMINAL_HANDOFF_CHAIN_ID=manifest["chain_id"],
            CLAUDE_TERMINAL_HANDOFF_GENERATION="2",
            CLAUDE_TERMINAL_HANDOFF_PARENT_SESSION=parent["_session_id"],
        )
        # The successor reports its OWN low percentage; the parent's 95% must
        # not carry over.
        successor = self.payload(percent=4.0)
        self.assertNotEqual(successor["_session_id"], parent["_session_id"])
        decision = self.evaluate(successor, successor_env)
        self.assertFalse(decision["trigger"], "successor triggered on inherited percentage")
        self.assertEqual(decision["percent"], 4.0)
        self.assertEqual(decision["state"], "below")


# ---------------------------------------------------------------------------
# 6. Kill switch, test mode, storm protection, cooldown, retry
# ---------------------------------------------------------------------------


class TestSafety(THTestCase):
    def test_29_kill_switch_prevents_launching(self):
        env = self.env(CLAUDE_TERMINAL_HANDOFF_DISABLED="1")
        payload = self.payload(percent=99.0)
        decision = self.evaluate(payload, env)
        self.assertFalse(decision["trigger"])
        self.assertEqual(decision["state"], "disabled")
        code, out, err = self.statusline(payload, env)
        self.assertIn("TH disabled", out)
        time.sleep(1.0)
        self.assertFalse(os.path.exists(self.launch_record(payload["_session_id"])))

    def test_30_test_mode_suppresses_real_terminal_launch(self):
        payload = self.payload(percent=90.0)
        self.assertTrue(self.trigger_and_wait(payload)[0])
        record = json_file(self.launch_record(payload["_session_id"]))
        self.assertTrue(record["test_mode"])
        self.assertFalse(record["result"].get("launched"))
        self.assertTrue(record["result"].get("test_mode"))
        self.assertIn("tell application \"Terminal\"", record["applescript"])
        manifest = json_file(self.manifest(payload["_session_id"]))
        self.assertEqual(manifest["successor"]["launch_state"], "simulated_test_mode")

    def test_53_circuit_breaker_prevents_terminal_storm(self):
        env = self.env(
            CLAUDE_TERMINAL_HANDOFF_STORM_MAX="3",
            CLAUDE_TERMINAL_HANDOFF_STORM_WINDOW="600",
        )
        for index in range(3):
            payload = self.payload(percent=90.0)
            code, out, err = self.statusline(payload, env)
            self.assertEqual(code, 0, err)
            self.assertTrue(wait_for(self.launch_record(payload["_session_id"])), "launch %d" % index)
        fourth = self.payload(percent=90.0)
        decision = self.evaluate(fourth, env)
        self.assertFalse(decision["trigger"])
        self.assertEqual(decision["state"], "circuit_open")
        code, out, err = self.statusline(fourth, env)
        self.assertIn("TH circuit open", out, "circuit-breaker activation must be visible")
        self.assertTrue(os.path.exists(os.path.join(self.home, "state", "circuit-open.json")))
        log = text_file(os.path.join(self.home, "logs", "terminal-handoff.log"))
        self.assertIn("circuit_breaker_open", log, "circuit activation must be logged, never hidden")

    def test_53b_circuit_breaker_can_be_reset(self):
        env = self.env(CLAUDE_TERMINAL_HANDOFF_STORM_MAX="1")
        payload = self.payload(percent=90.0)
        self.statusline(payload, env)
        wait_for(self.launch_record(payload["_session_id"]))
        self.assertFalse(self.evaluate(self.payload(percent=90.0), env)["trigger"])
        code, out, err = run_th(["reset-circuit"], "", env)
        self.assertEqual(code, 0)
        self.assertTrue(self.evaluate(self.payload(percent=90.0), env)["trigger"])

    def test_54_cooldown_works(self):
        env = self.env(CLAUDE_TERMINAL_HANDOFF_COOLDOWN="300")
        first = self.payload(percent=90.0)
        code, out, err = self.statusline(first, env)
        self.assertTrue(wait_for(self.launch_record(first["_session_id"])))
        second = self.payload(percent=90.0)
        decision = self.evaluate(second, env)
        self.assertFalse(decision["trigger"])
        self.assertEqual(decision["state"], "cooldown")
        code, out, err = self.statusline(second, env)
        self.assertIn("TH retrying", out)

    def test_55_failed_launch_permits_bounded_retry(self):
        # No claude executable -> launch fails -> the one-shot claim is released.
        env = self.env(CLAUDE_TERMINAL_HANDOFF_CLAUDE_BIN="/nonexistent/claude")
        env["PATH"] = "/nonexistent"
        env["HOME"] = self.tmp  # no ~/.local/bin/claude to fall back to
        payload = self.payload(percent=90.0)
        code, out, err = self.statusline(payload, env)
        self.assertEqual(code, 0, err)
        failed = os.path.join(self.home, "failed", "%s.json" % payload["_session_id"])
        self.assertTrue(wait_for(failed), "no failure record written")
        record = json_file(failed)
        self.assertEqual(record["state"], "failed")
        self.assertFalse(
            os.path.exists(os.path.join(self.home, "triggered", payload["_session_id"])),
            "the one-shot claim was not released for retry",
        )
        self.assertFalse(os.path.exists(self.launch_record(payload["_session_id"])))

    def test_55b_retry_is_bounded(self):
        env = self.env(CLAUDE_TERMINAL_HANDOFF_CLAUDE_BIN="/nonexistent/claude")
        env["PATH"] = "/nonexistent"
        env["HOME"] = self.tmp  # no ~/.local/bin/claude to fall back to
        payload = self.payload(percent=90.0)
        failed = os.path.join(self.home, "failed", "%s.json" % payload["_session_id"])
        for attempt in range(4):
            if os.path.exists(failed):
                os.unlink(failed)
            self.statusline(payload, env)
            wait_for(failed, timeout=15)
        retries_file = os.path.join(self.home, "failed", "%s.retries" % payload["_session_id"])
        self.assertTrue(os.path.exists(retries_file))
        self.assertLessEqual(int(text_file(retries_file).strip()), 3)
        self.assertTrue(
            os.path.exists(os.path.join(self.home, "triggered", payload["_session_id"])),
            "retries were not bounded: the claim is still being released",
        )

    def test_56_successful_launch_prevents_retry(self):
        payload = self.payload(percent=90.0)
        self.assertTrue(self.trigger_and_wait(payload)[0])
        self.assertTrue(os.path.exists(os.path.join(self.home, "triggered", payload["_session_id"])))
        self.assertFalse(
            os.path.exists(os.path.join(self.home, "failed", "%s.retries" % payload["_session_id"]))
        )
        decision = self.evaluate(payload)
        self.assertFalse(decision["trigger"])
        self.assertEqual(decision["state"], "handed_off")

    def test_stability_gate_requires_own_observations(self):
        env = self.env(CLAUDE_TERMINAL_HANDOFF_MIN_OBSERVATIONS="2")
        payload = self.payload(percent=90.0)
        first = self.evaluate(payload, env)
        self.assertFalse(first["trigger"])
        self.assertEqual(first["state"], "unstable")
        second = self.evaluate(payload, env)
        self.assertTrue(second["trigger"])

    def test_67_detached_launch_survives_short_lived_statusline(self):
        env = self.env(CLAUDE_TERMINAL_HANDOFF_LAUNCH_DELAY="4")
        payload = self.payload(percent=90.0)
        started = time.time()
        code, out, err = self.statusline(payload, env)
        elapsed = time.time() - started
        self.assertEqual(code, 0, err)
        self.assertLess(elapsed, 3.5, "status line blocked on the launcher (%.1fs)" % elapsed)
        self.assertFalse(os.path.exists(self.launch_record(payload["_session_id"])))
        self.assertTrue(
            wait_for(self.launch_record(payload["_session_id"]), timeout=30),
            "the detached launcher did not survive the status-line process",
        )


# ---------------------------------------------------------------------------
# 7. Repository capture
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
