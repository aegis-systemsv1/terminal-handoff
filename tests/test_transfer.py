"""The heartbeat validation gate and the transfer-of-ownership boundary.

Before a verified successor heartbeat the parent owns continuation. Every
failure mode below must leave the parent running and the transfer visibly
failed - never silently complete.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import (  # noqa: E402
    CORE,
    REAL_MODEL_ID,
    THTestCase,
    json_file,
    process_alive,
    run_th,
    text_file,
)


class TransferTestCase(THTestCase):
    """A completed handoff whose transfer is bound to a real stand-in process."""

    def handoff(self, **payload_kwargs):
        payload_kwargs.setdefault("percent", 90.0)
        payload_kwargs.setdefault("session_name", "Ranger")
        payload = self.payload(**payload_kwargs)
        ok, _ = self.trigger_and_wait(payload)
        self.assertTrue(ok, "no launch record produced")
        return payload

    def bind_transfer_to(self, session_id, pid):
        """Attach a known stand-in process to an existing transfer record."""
        path = self.transfer(session_id)
        record = json_file(path)
        record["parent_process"] = self.binding_for(pid, session_id=session_id)
        record["parent_process"]["chain_id"] = record["chain_id"]
        record["parent_process"]["generation"] = record["parent_generation"]
        record["parent_process_bound"] = True
        with open(path, "w") as handle:
            json.dump(record, handle, indent=2, sort_keys=True)
        return path

    def successor_env(self, parent, chain_id=None, generation=2, **extra):
        manifest = json_file(self.manifest(parent["_session_id"]))
        values = dict(
            CLAUDE_TERMINAL_HANDOFF_MANIFEST=self.manifest(parent["_session_id"]),
            CLAUDE_TERMINAL_HANDOFF_CHAIN_ID=chain_id or manifest["chain_id"],
            CLAUDE_TERMINAL_HANDOFF_GENERATION=str(generation),
            CLAUDE_TERMINAL_HANDOFF_PARENT_SESSION=parent["_session_id"],
            CLAUDE_TERMINAL_HANDOFF_TRANSFER=self.transfer(parent["_session_id"]),
        )
        values.update(extra)
        return self.env(**values)

    def beat(self, successor, env, times=2):
        for _ in range(times):
            code, out, err = self.statusline(successor, env)
            self.assertEqual(code, 0, err)

    def supervise(self, session_id, timeout=60):
        env = self.env(
            CLAUDE_TERMINAL_HANDOFF_TEST_MODE=None,
            CLAUDE_TERMINAL_HANDOFF_HEARTBEAT_TIMEOUT="3",
            CLAUDE_TERMINAL_HANDOFF_TRANSFER_POLL="0.2",
            CLAUDE_TERMINAL_HANDOFF_STOP_GRACE="6",
        )
        return run_th(["supervise", "--transfer", self.transfer(session_id)], env=env, timeout=timeout)


class TestTransferIsCreatedAtLaunch(TransferTestCase):
    def test_a_launch_creates_a_transfer_in_launching(self):
        parent = self.handoff()
        record = json_file(self.transfer(parent["_session_id"]))
        self.assertEqual(record["state"], "LAUNCHING")
        self.assertEqual(record["owner"], "parent")
        self.assertEqual(record["successor_display_name"], "Ranger 2")
        self.assertEqual(record["expected_successor"]["model_id"], REAL_MODEL_ID)
        self.assertEqual(record["expected_successor"]["effort_level"], "high")
        self.assertEqual(record["expected_successor"]["current_dir"], self.workdir)
        self.assertEqual(record["expected_successor"]["generation"], 2)
        self.assertEqual(record["stop"]["signal"], "SIGTERM")
        self.assertFalse(record["stop"]["escalates"])

    def test_test_mode_does_not_start_a_supervisor(self):
        parent = self.handoff()
        record = json_file(self.transfer(parent["_session_id"]))
        self.assertFalse(record["supervisor_spawned"])
        self.assertTrue(record["test_mode"])

    def test_the_launch_script_carries_the_transfer_and_base_name(self):
        parent = self.handoff()
        record = json_file(self.launch_record(parent["_session_id"]))
        script = text_file(record["script_file"])
        self.assertIn("CLAUDE_TERMINAL_HANDOFF_TRANSFER=", script)
        self.assertIn("CLAUDE_TERMINAL_HANDOFF_BASE_NAME=", script)
        self.assertIn("CLAUDE_TERMINAL_HANDOFF_DISPLAY_NAME=", script)
        self.assertIn(self.transfer(parent["_session_id"]), script)


class TestHeartbeatGate(TransferTestCase):
    """Only a fully valid heartbeat may authorise a shutdown."""

    def _successor(self, **kwargs):
        kwargs.setdefault("percent", 4.0)
        kwargs.setdefault("session_name", "Ranger 2")
        return self.payload(**kwargs)

    def test_a_valid_heartbeat_verifies_the_transfer(self):
        parent = self.handoff()
        successor = self._successor()
        self.beat(successor, self.successor_env(parent))
        record = json_file(self.transfer(parent["_session_id"]))
        self.assertEqual(record["state"], "SUCCESSOR_VERIFIED")
        self.assertEqual(record["successor"]["session_id"], successor["_session_id"])
        self.assertTrue(all(record["successor"]["checks"].values()))
        manifest = json_file(self.manifest(parent["_session_id"]))
        self.assertEqual(manifest["successor"]["launch_state"], "completed")

    def test_one_heartbeat_is_not_enough(self):
        parent = self.handoff()
        self.beat(self._successor(), self.successor_env(parent), times=1)
        self.assertEqual(json_file(self.transfer(parent["_session_id"]))["state"], "LAUNCHING")

    def _assert_rejected(self, parent, check_name):
        record = json_file(self.transfer(parent["_session_id"]))
        self.assertEqual(record["state"], "LAUNCHING", "the transfer was verified anyway")
        self.assertIn("successor_rejected", record)
        self.assertIn(check_name, record["successor_rejected"]["failed_checks"])
        manifest = json_file(self.manifest(parent["_session_id"]))
        self.assertEqual(manifest["successor"]["launch_state"], "successor_mismatch")
        self.assertIsNone(manifest["successor"]["confirmed_utc"])

    def test_a_wrong_model_is_rejected(self):
        parent = self.handoff()
        self.beat(self._successor(model_id="claude-sonnet-5"), self.successor_env(parent))
        self._assert_rejected(parent, "model_matches")

    def test_a_wrong_effort_is_rejected(self):
        parent = self.handoff()
        self.beat(self._successor(effort="low"), self.successor_env(parent))
        self._assert_rejected(parent, "effort_matches")

    def test_a_missing_effort_is_rejected_when_one_was_required(self):
        parent = self.handoff()
        self.beat(self._successor(include_effort=False), self.successor_env(parent))
        self._assert_rejected(parent, "effort_matches")

    def test_a_wrong_working_directory_is_rejected(self):
        parent = self.handoff()
        other = os.path.join(self.tmp, "other-work")
        os.makedirs(other, exist_ok=True)
        self.beat(self._successor(workdir=other), self.successor_env(parent))
        self._assert_rejected(parent, "cwd_matches")

    def test_a_wrong_chain_is_rejected(self):
        parent = self.handoff()
        self.beat(self._successor(), self.successor_env(parent, chain_id="ffffff999999"))
        self._assert_rejected(parent, "chain_matches")

    def test_a_wrong_generation_is_rejected(self):
        parent = self.handoff()
        self.beat(self._successor(), self.successor_env(parent, generation=7))
        self._assert_rejected(parent, "generation_matches")

    def test_a_reused_session_id_is_rejected(self):
        """A session already recorded at another generation is not a successor."""
        parent = self.handoff()
        successor = self._successor()
        manifest = json_file(self.manifest(parent["_session_id"]))
        os.environ["CLAUDE_TERMINAL_HANDOFF_HOME"] = self.home
        try:
            CORE.record_chain_generation(
                manifest["chain_id"], 1, session_id=successor["_session_id"],
                display_name="Ranger",
            )
        finally:
            os.environ.pop("CLAUDE_TERMINAL_HANDOFF_HOME", None)
        self.beat(successor, self.successor_env(parent))
        self._assert_rejected(parent, "session_id_unused")

    def test_the_parent_can_never_be_its_own_successor(self):
        parent = self.handoff()
        self.beat(parent, self.successor_env(parent))
        record = json_file(self.transfer(parent["_session_id"]))
        self.assertEqual(record["state"], "LAUNCHING")
        manifest = json_file(self.manifest(parent["_session_id"]))
        self.assertIsNone(manifest["successor"]["session_id"])

    def test_a_missing_context_percentage_is_rejected(self):
        parent = self.handoff()
        self.beat(self._successor(include_context=False), self.successor_env(parent))
        self._assert_rejected(parent, "context_percentage_live")


class TestFailedHeartbeatsNeverStopTheParent(TransferTestCase):
    """The end-to-end guarantee, proved against a real process each time."""

    def _run(self, successor_kwargs=None, env_kwargs=None, beats=2):
        parent = self.handoff()
        standin = self.standin_claude("parent")
        self.bind_transfer_to(parent["_session_id"], standin.pid)
        if successor_kwargs is not None:
            successor = self.payload(percent=4.0, session_name="Ranger 2", **successor_kwargs)
            self.beat(successor, self.successor_env(parent, **(env_kwargs or {})), times=beats)
        self.supervise(parent["_session_id"])
        return parent, standin, json_file(self.transfer(parent["_session_id"]))

    def test_no_heartbeat_at_all_leaves_the_parent_running(self):
        parent, standin, record = self._run()
        self.assertTrue(process_alive(standin.pid))
        self.assertEqual(record["state"], "TRANSFER_FAILED")
        self.assertFalse(record.get("parent_stopped"))

    def test_a_wrong_model_leaves_the_parent_running(self):
        parent, standin, record = self._run({"model_id": "claude-sonnet-5"})
        self.assertTrue(process_alive(standin.pid))
        self.assertEqual(record["state"], "TRANSFER_FAILED")

    def test_a_wrong_effort_leaves_the_parent_running(self):
        parent, standin, record = self._run({"effort": "max"})
        self.assertTrue(process_alive(standin.pid))
        self.assertEqual(record["state"], "TRANSFER_FAILED")

    def test_a_wrong_generation_leaves_the_parent_running(self):
        parent, standin, record = self._run({}, {"generation": 9})
        self.assertTrue(process_alive(standin.pid))
        self.assertEqual(record["state"], "TRANSFER_FAILED")

    def test_a_wrong_chain_leaves_the_parent_running(self):
        parent, standin, record = self._run({}, {"chain_id": "ffffff999999"})
        self.assertTrue(process_alive(standin.pid))
        self.assertEqual(record["state"], "TRANSFER_FAILED")

    def test_a_single_heartbeat_leaves_the_parent_running(self):
        parent, standin, record = self._run({}, None, beats=1)
        self.assertTrue(process_alive(standin.pid))
        self.assertEqual(record["state"], "TRANSFER_FAILED")

    def test_a_valid_heartbeat_does_stop_the_parent(self):
        """The positive control for every test above."""
        parent = self.handoff()
        standin = self.standin_claude("parent")
        unrelated = self.standin_claude("unrelated")
        self.bind_transfer_to(parent["_session_id"], standin.pid)
        self.beat(self.payload(percent=4.0, session_name="Ranger 2"), self.successor_env(parent))
        self.assertEqual(json_file(self.transfer(parent["_session_id"]))["state"],
                         "SUCCESSOR_VERIFIED")
        code, out, err = self.supervise(parent["_session_id"])
        self.assertEqual(code, 0, err)
        record = json_file(self.transfer(parent["_session_id"]))
        self.assertEqual(record["state"], "TRANSFER_COMPLETE")
        self.assertTrue(record["parent_stopped"])
        self.assertFalse(process_alive(standin.pid), "the bound parent was not stopped")
        self.assertTrue(process_alive(unrelated.pid), "an unrelated session was stopped")


class TestSuccessorPromptOwnership(THTestCase):
    def test_fallback_prompt_has_the_same_exclusive_boundary(self):
        fallback = CORE.FALLBACK_PROMPT_TEMPLATE
        self.assertIn("transfer state reads TRANSFER_COMPLETE", fallback)
        self.assertIn("PARENT_STOP_REQUESTED is read-only", fallback)
        self.assertNotIn("PARENT_STOP_REQUESTED or", fallback)

    def test_the_prompt_states_the_ownership_boundary(self):
        payload = self.payload(percent=90.0, session_name="Ranger")
        ok, _ = self.trigger_and_wait(payload)
        self.assertTrue(ok)
        record = json_file(self.launch_record(payload["_session_id"]))
        prompt = text_file(record["prompt_file"])
        self.assertIn("TRANSFER OF OWNERSHIP", prompt)
        self.assertIn("heartbeat", prompt)
        self.assertIn("PARENT_STOP_REQUESTED", prompt)
        self.assertIn("TRANSFER_COMPLETE", prompt)
        self.assertIn('Only `"state": "TRANSFER_COMPLETE"` means you own', prompt)
        self.assertIn("neither session may mutate", prompt.replace("\n     ", " "))
        self.assertNotIn(
            '`"state": "PARENT_STOP_REQUESTED"` or `"TRANSFER_COMPLETE"` means you own',
            prompt,
        )
        self.assertIn("repeat pwd", prompt)
        self.assertIn(self.transfer(payload["_session_id"]), prompt)
        self.assertIn("Ranger 2", prompt)
        bootstrap = record["argv"][-1]
        self.assertIn("Mutate nothing", bootstrap)


if __name__ == "__main__":
    unittest.main(verbosity=2)
