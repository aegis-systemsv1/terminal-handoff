"""Logical sessions: registry, owner fencing, durable inbox and STOP.

The logical session is the durable control object. These tests prove that the
STOP flag and the instruction inbox belong to it, survive A -> B -> C handoffs
and restarts, and can be touched only by the current fenced owner.
"""

import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import CORE, TH_SCRIPT, THTestCase, json_file, process_alive, run_th  # noqa: E402
from test_continuation import ContinuationCase, OTHER, SUCCESSOR  # noqa: E402

TOKEN = "launch-token-for-tests"


class LogicalCase(THTestCase):
    def setUp(self):
        super().setUp()
        self._saved = {k: os.environ.get(k) for k in ("CLAUDE_TERMINAL_HANDOFF_HOME", "CLAUDE_TERMINAL_HANDOFF_TEST_MODE")}
        os.environ["CLAUDE_TERMINAL_HANDOFF_HOME"] = self.home
        # Test mode stays on: it keeps notification workers and Terminal windows
        # from ever starting for real. Only the real-signal tests turn it off.
        os.environ["CLAUDE_TERMINAL_HANDOFF_TEST_MODE"] = "1"

    def real_signals(self):
        os.environ.pop("CLAUDE_TERMINAL_HANDOFF_TEST_MODE", None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        super().tearDown()

    def new_session(self, owner="agent-A-session", generation=1, **kw):
        import hashlib

        record = CORE.logical_create(
            project="nova",
            repository=self.workdir,
            launch_token_sha256=hashlib.sha256(TOKEN.encode()).hexdigest(),
            launch_expires_epoch=9999999999,
            **kw
        )
        lsid = record["logical_session_id"]
        if owner:
            ok, why, _ = CORE.logical_register_owner(lsid, owner, generation, "chain1", None, TOKEN)
            self.assertTrue(ok, why)
        return lsid

    def transfer_complete(self, lsid, parent, successor, gen=2):
        return {
            "state": "TRANSFER_COMPLETE",
            "parent_session_id": parent,
            "successor": {"session_id": successor},
            "successor_generation": gen,
            "chain_id": "chain1",
        }


class TestRegistry(LogicalCase):
    def test_ids_are_validated_and_cannot_traverse(self):
        for bad in ("../../etc/passwd", "ls_ZZZ", "", None, "ls_" + "a" * 23, "ls_" + "a" * 25, "ls_" + "A" * 24, 5):
            with self.subTest(bad=bad):
                self.assertFalse(CORE.logical_session_valid(bad))
                self.assertIsNone(CORE.logical_read(bad))
                ok, why, _, _ = CORE.logical_mutate(bad, lambda r: None)
                self.assertFalse(ok)
        with self.assertRaises(ValueError):
            CORE.logical_path("../x")

    def test_unknown_but_wellformed_id_is_refused(self):
        ok, why, _, _ = CORE.logical_mutate("ls_" + "0" * 24, lambda r: None)
        self.assertFalse(ok)
        self.assertEqual(why, "unknown logical session")

    def test_owner_registration_needs_the_one_time_token(self):
        lsid = self.new_session(owner=None)
        ok, why, _ = CORE.logical_register_owner(lsid, "agent-A", 1, "c", None, "wrong")
        self.assertFalse(ok)
        self.assertIsNone(CORE.logical_read(lsid)["owner"])
        ok, why, _ = CORE.logical_register_owner(lsid, "agent-A", 1, "c", None, TOKEN)
        self.assertTrue(ok, why)
        ok, why, _ = CORE.logical_register_owner(lsid, "agent-EVIL", 1, "c", None, TOKEN)
        self.assertFalse(ok)
        self.assertEqual(CORE.logical_read(lsid)["owner"]["agent_session_id"], "agent-A")
        self.assertEqual(CORE.logical_read(lsid)["state"], "RUNNING")

    def test_record_is_private_and_public_view_hides_process_details(self):
        lsid = self.new_session()
        path = CORE.logical_path(lsid)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        view = CORE.logical_public_view(CORE.logical_read(lsid))
        text = json.dumps(view)
        for forbidden in ("pid", "process", "token", "sha256", "transfer_file", "repository", self.workdir):
            self.assertNotIn(forbidden, text)

    def test_the_cli_has_no_pid_or_shell_arguments(self):
        source = open(TH_SCRIPT).read()
        block = source[source.index('"session",\n        help="Logical sessions'):]
        block = block[: block.index("p.set_defaults(func=cmd_session)")]
        for flag in ("--pid", "--command", "--shell", "--exec"):
            self.assertNotIn(flag, block)


class TestInbox(LogicalCase):
    def test_instructions_are_ordered_and_owner_only(self):
        lsid = self.new_session()
        for text in ("first", "second", "third"):
            ok, _, m = CORE.inbox_post(lsid, text)
            self.assertTrue(ok)
        ok, why, claimed = CORE.inbox_claim(lsid, "agent-A-session")
        self.assertTrue(ok)
        self.assertEqual([m["text"] for m in claimed], ["first", "second", "third"])
        self.assertEqual([m["seq"] for m in claimed], [1, 2, 3])
        ok, why, _ = CORE.inbox_claim(lsid, "someone-else")
        self.assertFalse(ok)
        # delivered messages are not delivered twice
        ok, _, again = CORE.inbox_claim(lsid, "agent-A-session")
        self.assertEqual(again, [])

    def test_ack_is_exactly_once_and_owner_only(self):
        lsid = self.new_session()
        _, _, m = CORE.inbox_post(lsid, "do it")
        CORE.inbox_claim(lsid, "agent-A-session")
        self.assertFalse(CORE.inbox_ack(lsid, "someone-else", m["id"])[0])
        self.assertEqual(CORE.inbox_ack(lsid, "agent-A-session", m["id"])[2], "acked")
        self.assertEqual(CORE.inbox_ack(lsid, "agent-A-session", m["id"])[2], "already_acked")
        self.assertFalse(CORE.inbox_ack(lsid, "agent-A-session", "im_unknown")[0])

    def test_ack_requires_prior_delivery(self):
        lsid = self.new_session()
        _, _, m = CORE.inbox_post(lsid, "do it")
        ok, why, _ = CORE.inbox_ack(lsid, "agent-A-session", m["id"])
        self.assertFalse(ok)

    def test_replay_of_the_same_instruction_is_deduplicated(self):
        lsid = self.new_session()
        key = "phone-request-0001"
        _, _, first = CORE.inbox_post(lsid, "deploy nothing", idempotency_key=key)
        _, _, second = CORE.inbox_post(lsid, "deploy nothing", idempotency_key=key)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(CORE.logical_read(lsid)["inbox"]["messages"]), 1)

    def test_input_is_validated_and_never_interpreted(self):
        lsid = self.new_session()
        for bad in ("", "   ", None, 5, "x" * (CORE.MAX_INSTRUCTION_CHARS + 1), b"bytes"):
            self.assertFalse(CORE.inbox_post(lsid, bad)[0], repr(bad)[:20])
        self.assertFalse(CORE.inbox_post(lsid, "ok text", idempotency_key="bad key!")[0])
        payload = "$(touch /tmp/pwned); `id` ; rm -rf / \x00\x07 done"
        ok, _, m = CORE.inbox_post(lsid, payload)
        self.assertTrue(ok)
        self.assertNotIn("\x00", m["text"])
        self.assertIn("$(touch /tmp/pwned)", m["text"])  # stored inert, as data

    def test_instruction_during_ownership_transfer_reaches_the_successor(self):
        lsid = self.new_session()
        CORE.inbox_post(lsid, "before")
        CORE.inbox_claim(lsid, "agent-A-session")  # delivered to A, never acked
        CORE.inbox_post(lsid, "arrives mid-transfer")
        ok, why, _ = CORE.logical_adopt_successor(
            lsid, self.transfer_complete(lsid, "agent-A-session", "agent-B-session")
        )
        self.assertTrue(ok, why)
        ok, _, claimed = CORE.inbox_claim(lsid, "agent-B-session")
        self.assertEqual([m["text"] for m in claimed], ["before", "arrives mid-transfer"])
        self.assertEqual(claimed[0]["redelivered"], 1)

    def test_stale_owner_cannot_consume_after_handoff(self):
        lsid = self.new_session()
        _, _, m = CORE.inbox_post(lsid, "work")
        CORE.inbox_claim(lsid, "agent-A-session")
        CORE.logical_adopt_successor(lsid, self.transfer_complete(lsid, "agent-A-session", "agent-B-session"))
        self.assertFalse(CORE.inbox_claim(lsid, "agent-A-session")[0])
        self.assertFalse(CORE.inbox_ack(lsid, "agent-A-session", m["id"])[0])
        CORE.inbox_claim(lsid, "agent-B-session")
        self.assertTrue(CORE.inbox_ack(lsid, "agent-B-session", m["id"])[0])

    def test_instruction_survives_successor_failure_before_ownership(self):
        lsid = self.new_session()
        CORE.inbox_post(lsid, "keep me")
        # the successor never becomes owner; the parent stays owner and still gets it
        ok, _, claimed = CORE.inbox_claim(lsid, "agent-A-session")
        self.assertEqual([m["text"] for m in claimed], ["keep me"])
        self.assertFalse(CORE.inbox_claim(lsid, "agent-B-session")[0])

    def test_output_is_redacted_and_owner_only(self):
        lsid = self.new_session()
        secret = "Bearer abcdefghijklmnop1234 and sk-abcdefghijklmnopqrstuv and password=hunter22"
        self.assertTrue(CORE.logical_append_output(lsid, "agent-A-session", secret)[0])
        self.assertFalse(CORE.logical_append_output(lsid, "intruder", "x")[0])
        line = CORE.logical_read(lsid)["output"][0]["text"]
        for leaked in ("abcdefghijklmnop1234", "sk-abcdefghijklmnopqrstuv", "hunter22"):
            self.assertNotIn(leaked, line)


class TestOwnerFencing(LogicalCase):
    def test_adoption_requires_the_registered_owner_to_be_the_transfer_parent(self):
        lsid = self.new_session()
        ok, why, _ = CORE.logical_adopt_successor(
            lsid, self.transfer_complete(lsid, "forged-parent", "agent-EVIL")
        )
        self.assertFalse(ok)
        self.assertEqual(CORE.logical_read(lsid)["owner"]["agent_session_id"], "agent-A-session")

    def test_adoption_needs_a_complete_transfer(self):
        lsid = self.new_session()
        for state in ("LAUNCHING", "SUCCESSOR_VERIFIED", "PARENT_STOP_REQUESTED", "TRANSFER_FAILED"):
            record = self.transfer_complete(lsid, "agent-A-session", "agent-B-session")
            record["state"] = state
            self.assertFalse(CORE.logical_adopt_successor(lsid, record)[0], state)
        self.assertEqual(CORE.logical_read(lsid)["owner"]["agent_session_id"], "agent-A-session")

    def test_a_chain_of_owners_bumps_the_epoch_each_time(self):
        lsid = self.new_session()
        CORE.logical_adopt_successor(lsid, self.transfer_complete(lsid, "agent-A-session", "agent-B-session", 2))
        CORE.logical_adopt_successor(lsid, self.transfer_complete(lsid, "agent-B-session", "agent-C-session", 3))
        record = CORE.logical_read(lsid)
        self.assertEqual(record["owner"]["agent_session_id"], "agent-C-session")
        self.assertEqual(record["owner_epoch"], 3)
        # the earlier successor cannot be re-adopted after its own handoff
        self.assertFalse(
            CORE.logical_adopt_successor(lsid, self.transfer_complete(lsid, "agent-A-session", "agent-EVIL"))[0]
        )

    def test_duplicate_adoption_is_idempotent_and_does_not_bump_the_epoch(self):
        lsid = self.new_session()
        tr = self.transfer_complete(lsid, "agent-A-session", "agent-B-session")
        CORE.logical_adopt_successor(lsid, tr)
        epoch = CORE.logical_read(lsid)["owner_epoch"]
        self.assertTrue(CORE.logical_adopt_successor(lsid, tr)[0])
        self.assertEqual(CORE.logical_read(lsid)["owner_epoch"], epoch)

    def test_remote_control_is_re_verified_for_the_new_owner(self):
        lsid = self.new_session()
        CORE.logical_mutate(lsid, lambda r: r.update(remote_control={"state": "healthy"}))
        CORE.logical_adopt_successor(lsid, self.transfer_complete(lsid, "agent-A-session", "agent-B-session"))
        self.assertEqual(CORE.logical_read(lsid)["remote_control"]["state"], "unknown")


class TestStop(LogicalCase):
    def test_stop_halts_delivery_and_is_visible(self):
        lsid = self.new_session()
        CORE.inbox_post(lsid, "work")
        ok, _, record = CORE.logical_stop(lsid, by="phone", reason="emergency")
        self.assertTrue(ok)
        self.assertEqual(record["state"], "STOPPED")
        ok, why, _ = CORE.inbox_claim(lsid, "agent-A-session")
        self.assertFalse(ok)
        self.assertEqual(why, "halted:stop")
        self.assertEqual(CORE.logical_public_view(CORE.logical_read(lsid))["stop"]["active"], True)
        # the instruction is not lost while stopped
        self.assertEqual(CORE.logical_public_view(CORE.logical_read(lsid))["inbox"]["pending"], 1)

    def test_stop_survives_handoff_and_cannot_be_cleared_by_it(self):
        lsid = self.new_session()
        CORE.logical_stop(lsid, by="phone", reason="hold")
        CORE.logical_adopt_successor(lsid, self.transfer_complete(lsid, "agent-A-session", "agent-B-session"))
        record = CORE.logical_read(lsid)
        self.assertTrue(record["stop"]["active"])
        self.assertEqual(record["state"], "STOPPED")
        self.assertEqual(record["owner"]["agent_session_id"], "agent-B-session")

    def test_stop_survives_a_service_restart(self):
        lsid = self.new_session()
        CORE.logical_stop(lsid, by="phone", reason="hold")
        code, out, _ = run_th(["session", "show", "--logical-session", lsid], env=self.env())
        self.assertEqual(json.loads(out)["stop"]["active"], True)
        self.assertEqual(json.loads(out)["state"], "STOPPED")

    def test_resume_from_stop_must_be_deliberate(self):
        lsid = self.new_session()
        CORE.logical_stop(lsid, by="phone", reason="hold")
        self.assertFalse(CORE.logical_resume(lsid, by="phone")[0])
        self.assertFalse(CORE.logical_resume(lsid, by="phone", clear_stop=True)[0])  # no reason
        ok, why, record = CORE.logical_resume(lsid, by="phone", clear_stop=True, reason="reviewed, safe")
        self.assertTrue(ok, why)
        self.assertEqual(record["state"], "RUNNING")
        self.assertFalse(record["stop"]["active"])
        self.assertEqual(len(record["stop_history"]), 1)
        self.assertTrue(CORE.inbox_claim(lsid, "agent-A-session")[0])

    def test_pause_is_distinct_from_stop(self):
        lsid = self.new_session()
        CORE.logical_pause(lsid, by="phone")
        self.assertEqual(CORE.logical_read(lsid)["state"], "PAUSED")
        self.assertFalse(CORE.inbox_claim(lsid, "agent-A-session")[0])
        self.assertTrue(CORE.logical_resume(lsid, by="phone")[0])
        self.assertEqual(CORE.logical_read(lsid)["state"], "RUNNING")
        CORE.logical_stop(lsid, by="phone", reason="x")
        self.assertFalse(CORE.logical_pause(lsid, by="phone")[0])  # a stop cannot be downgraded

    def test_hard_stop_signals_only_a_reproved_claude_process(self):
        lsid = self.new_session()
        standin = self.standin_claude("owner")
        binding = self.binding_for(standin.pid, session_id="agent-A-session")
        binding["chain_id"], binding["generation"] = "chain1", 1
        CORE.logical_mutate(lsid, lambda r: r["owner"].update(process=binding))
        self.assertEqual(CORE.logical_hard_stop(lsid)["outcome"], "simulated")  # test mode never signals
        self.assertTrue(process_alive(standin.pid))
        self.real_signals()
        result = CORE.logical_hard_stop(lsid)
        self.assertEqual(result["outcome"], "signalled")
        from _harness import wait_for_exit

        self.assertTrue(wait_for_exit(standin.pid))

    def test_hard_stop_refuses_a_forged_or_mismatched_binding(self):
        lsid = self.new_session()
        bystander = self.standin_claude("bystander")
        binding = self.binding_for(bystander.pid, session_id="some-other-session")
        binding["chain_id"], binding["generation"] = "chain1", 1
        CORE.logical_mutate(lsid, lambda r: r["owner"].update(process=binding))
        result = CORE.logical_hard_stop(lsid)
        self.assertEqual(result["outcome"], "not_signalled")
        self.assertTrue(process_alive(bystander.pid))
        # a non-Claude process is never signalled either
        other = self.standin_claude("sleeper", name="sleep")
        binding2 = self.binding_for(other.pid, session_id="agent-A-session", name="claude")
        binding2["chain_id"], binding2["generation"] = "chain1", 1
        CORE.logical_mutate(lsid, lambda r: r["owner"].update(process=binding2))
        self.assertEqual(CORE.logical_hard_stop(lsid)["outcome"], "not_signalled")
        self.assertTrue(process_alive(other.pid))

    def test_stop_without_a_process_binding_is_still_cooperatively_effective(self):
        lsid = self.new_session()
        ok, _, record = CORE.logical_stop(lsid, by="phone", reason="x", hard=True)
        self.assertTrue(ok)
        self.assertEqual(record["hard_stop"]["outcome"], "not_signalled")
        self.assertEqual(record["state"], "STOPPED")


class TestContinuationIntegration(ContinuationCase):
    """The transfer record links a handoff to its logical session."""

    def make_logical(self, owner):
        import hashlib

        os.environ["CLAUDE_TERMINAL_HANDOFF_HOME"] = self.home
        record = CORE.logical_create(
            project="nova", launch_token_sha256=hashlib.sha256(b"t").hexdigest(), launch_expires_epoch=9999999999
        )
        CORE.logical_register_owner(record["logical_session_id"], owner, 1, "abcdef012345", None, "t")
        return record["logical_session_id"]

    def setUp(self):
        super().setUp()
        self._old = os.environ.get("CLAUDE_TERMINAL_HANDOFF_HOME")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("CLAUDE_TERMINAL_HANDOFF_HOME", None)
        else:
            os.environ["CLAUDE_TERMINAL_HANDOFF_HOME"] = self._old
        super().tearDown()

    def test_successor_adopts_the_logical_session_then_continues(self):
        lsid = self.make_logical(self.parent_id)
        self.make_transfer("TRANSFER_COMPLETE", logical_session_id=lsid)
        self.session_record()
        _, report, _ = self.cont("wait")
        self.assertEqual(report["directive"], "CONTINUE")
        self.assertEqual(report["logical_session_id"], lsid)
        self.assertEqual(CORE.logical_read(lsid)["owner"]["agent_session_id"], SUCCESSOR)

    def test_stop_before_handoff_halts_the_successor(self):
        lsid = self.make_logical(self.parent_id)
        CORE.logical_stop(lsid, by="phone", reason="stop")
        self.make_transfer("TRANSFER_COMPLETE", logical_session_id=lsid)
        self.session_record()
        _, report, _ = self.cont("wait")
        self.assertEqual(report["directive"], "HALT")
        self.assertEqual(report["halt"], "stop")
        _, gate, _ = self.cont("resume")
        self.assertEqual(gate["directive"], "HALT")  # a gate resume cannot clear a logical STOP

    def test_stop_applied_after_the_handoff_halts_the_running_successor(self):
        lsid = self.make_logical(self.parent_id)
        self.make_transfer("TRANSFER_COMPLETE", logical_session_id=lsid)
        self.session_record()
        self.assertEqual(self.cont("wait")[1]["directive"], "CONTINUE")
        CORE.logical_stop(lsid, by="phone", reason="stop")
        self.assertEqual(self.cont("wait")[1]["directive"], "HALT")

    def test_a_forged_link_cannot_take_over_a_logical_session(self):
        lsid = self.make_logical("some-other-owner")
        self.make_transfer("TRANSFER_COMPLETE", logical_session_id=lsid)
        self.session_record()
        _, report, _ = self.cont("wait")
        self.assertEqual(report["directive"], "STOP")
        self.assertEqual(CORE.logical_read(lsid)["owner"]["agent_session_id"], "some-other-owner")

    def test_second_successor_cannot_adopt(self):
        lsid = self.make_logical(self.parent_id)
        self.make_transfer("TRANSFER_COMPLETE", logical_session_id=lsid)
        self.session_record()
        self.cont("wait")
        _, report, _ = self.cont("wait", session=OTHER)
        self.assertEqual(report["directive"], "STOP")
        self.assertEqual(CORE.logical_read(lsid)["owner"]["agent_session_id"], SUCCESSOR)

    def test_manifest_and_launch_env_carry_the_logical_id(self):
        os.environ["CLAUDE_TERMINAL_HANDOFF_LOGICAL_SESSION"] = "ls_" + "a" * 24
        try:
            self.assertEqual(CORE.logical_id_from_env(), "ls_" + "a" * 24)
            os.environ["CLAUDE_TERMINAL_HANDOFF_LOGICAL_SESSION"] = "../bad"
            self.assertIsNone(CORE.logical_id_from_env())
        finally:
            os.environ.pop("CLAUDE_TERMINAL_HANDOFF_LOGICAL_SESSION", None)
        script = CORE.build_launch_script(
            {"chain_id": "c", "generation": 1, "logical_session_id": "ls_" + "b" * 24,
             "outgoing": {"session_id": "p"}, "display": {}},
            ["claude", "--model", "m", "p"], self.workdir, "/m.json", "/t.json",
        )
        self.assertIn("export CLAUDE_TERMINAL_HANDOFF_LOGICAL_SESSION=ls_" + "b" * 24, script)


if __name__ == "__main__":
    unittest.main()
