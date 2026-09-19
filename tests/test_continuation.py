"""Automatic continuation, human gates and Remote Control readiness.

Ownership is decided only by the transfer state machine. These tests prove that
continuation begins strictly after TRANSFER_COMPLETE, only for the verified
successor, that Remote Control health is actually probed, that a human gate is
machine-readable and never bypassed, and that no failure creates a second owner.
"""

import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import (  # noqa: E402
    CORE,
    TH_SCRIPT,
    THTestCase,
    json_file,
    process_alive,
    run_th,
    text_file,
)
from test_transfer import TransferTestCase  # noqa: E402

SUCCESSOR = "successor-session-0001"
OTHER = "another-session-0002"
BRIDGE_SECRET = "session_BRIDGE_SECRET_MUST_NEVER_LEAK"


class ContinuationCase(THTestCase):
    """A transfer record in a chosen state plus a fake Claude session directory."""

    def setUp(self):
        super().setUp()
        self.sessions_dir = os.path.join(self.tmp, "claude-sessions")
        os.makedirs(self.sessions_dir)
        self.parent_id = "parent-session-0001"

    def th_env(self, **extra):
        values = dict(
            CLAUDE_TERMINAL_HANDOFF_CLAUDE_SESSIONS_DIR=self.sessions_dir,
            CLAUDE_TERMINAL_HANDOFF_REMOTE_VERIFY_SECONDS="0",
            CLAUDE_TERMINAL_HANDOFF_TRANSFER=self.transfer(self.parent_id),
            CLAUDE_TERMINAL_HANDOFF_PARENT_SESSION=self.parent_id,
            CLAUDE_TERMINAL_HANDOFF_TRANSFER_POLL="0.1",
        )
        values.update(extra)
        return self.env(**values)

    def make_transfer(self, state, successor_session=SUCCESSOR, **overrides):
        successor = {"session_id": successor_session} if successor_session else {}
        return self.write_transfer(
            self.parent_id,
            None,
            state=state,
            successor=successor,
            attempt_id="attempt-1",
            **overrides
        )

    def session_record(self, session_id=SUCCESSOR, pid=None, bridge=BRIDGE_SECRET):
        record = {"pid": pid if pid is not None else os.getpid(), "sessionId": session_id}
        if bridge is not None:
            record["bridgeSessionId"] = bridge
        with open(os.path.join(self.sessions_dir, "%s.json" % record["pid"]), "w") as handle:
            json.dump(record, handle)

    def cont(self, action, *extra, session=SUCCESSOR, env=None):
        args = ["continuation", action, "--session-id", session] + list(extra)
        if action == "wait" and "--timeout" not in extra:
            args += ["--timeout", "0"]
        code, out, err = run_th(args, env=env or self.th_env())
        try:
            return code, json.loads(out), err
        except ValueError:
            self.fail("not JSON: %r %r" % (out, err))

    def record(self):
        return json_file(self.transfer(self.parent_id))

    def outbox(self, kind):
        directory = os.path.join(self.home, "outbox", "pending")
        found = []
        if os.path.isdir(directory):
            for name in os.listdir(directory):
                data = json_file(os.path.join(directory, name))
                if data["event"]["kind"] == kind:
                    found.append(data["event"])
        return found

    def all_text_under_home(self):
        chunks = []
        for root, _, files in os.walk(self.home):
            for name in files:
                try:
                    chunks.append(text_file(os.path.join(root, name)))
                except Exception:
                    pass
        return "\n".join(chunks)


class TestAutomaticContinuation(ContinuationCase):
    def test_complete_transfer_with_healthy_remote_directs_continue(self):
        self.make_transfer("TRANSFER_COMPLETE")
        self.session_record()
        code, report, _ = self.cont("wait")
        self.assertEqual(code, 0)
        self.assertEqual(report["directive"], "CONTINUE")
        self.assertEqual(report["phase"], "RUNNING")
        self.assertEqual(report["owner"], "successor")
        self.assertEqual(report["remote_control"]["state"], "healthy")
        self.assertFalse(report.get("human_gate"))

    def test_continuation_needs_no_permission_or_confirmation(self):
        """Already-authorised work is never held behind a question."""
        self.make_transfer("TRANSFER_COMPLETE")
        self.session_record()
        _, report, _ = self.cont("wait")
        self.assertEqual(report["directive"], "CONTINUE")
        record = self.record()
        self.assertFalse(record["continuation"]["human_gate"]["waiting_for_human"])
        self.assertIn("automatic_continuation_started_utc", record["continuation"])

    def test_advance_is_idempotent(self):
        self.make_transfer("TRANSFER_COMPLETE")
        self.session_record()
        self.cont("wait")
        first = self.record()["continuation"]["automatic_continuation_started_utc"]
        self.cont("wait")
        self.assertEqual(self.record()["continuation"]["automatic_continuation_started_utc"], first)


class TestPromptAndManifest(TransferTestCase):
    def test_the_prompt_instructs_automatic_continuation(self):
        payload = self.handoff()
        manifest = json_file(self.manifest(payload["_session_id"]))
        prompt = CORE.render_successor_prompt(manifest)
        self.assertNotIn("{{", prompt)
        self.assertIn("continuation wait", prompt)
        self.assertIn("CONTINUE THE EXISTING TASK", " ".join(prompt.split()))
        self.assertIn("Automatic continuation is never automatic approval", prompt)
        self.assertIn("continuation gate", prompt)
        self.assertIn("continuation resume", prompt)
        self.assertNotIn("Stop and ask the user if", prompt)
        self.assertIn("continuation wait", CORE.FALLBACK_PROMPT_TEMPLATE)
        cont = manifest["continuation"]
        self.assertEqual(cont["ownership"]["parent_session_id"], payload["_session_id"])
        self.assertIsNone(cont["pending_human_gate"])
        self.assertIn("approvals_not_granted", cont["required_brief_fields"])
        self.assertIn("never blanket approval", cont["authority"]["rule"])


class TestHumanGate(ContinuationCase):
    def setUp(self):
        super().setUp()
        self.make_transfer("TRANSFER_COMPLETE")
        self.session_record()
        self.cont("wait")

    def raise_gate(self, **kw):
        return self.cont(
            "gate",
            "--reason", kw.get("reason", "production deployment approval required"),
            "--requested-action", kw.get("action", "approve deployment"),
            env=kw.get("env"),
        )

    def test_gate_is_machine_readable(self):
        code, report, _ = self.raise_gate()
        self.assertEqual(code, 0)
        self.assertEqual(report["directive"], "HOLD_FOR_HUMAN")
        self.assertEqual(report["phase"], "WAITING_FOR_HUMAN")
        gate = report["human_gate"]
        self.assertTrue(gate["waiting_for_human"])
        self.assertEqual(gate["reason"], "production deployment approval required")
        self.assertEqual(gate["requested_action"], "approve deployment")
        self.assertTrue(gate["resume_capable"])
        self.assertEqual(self.record()["state"], "TRANSFER_COMPLETE")

    def test_gated_action_is_never_released_without_resume(self):
        self.raise_gate()
        for _ in range(3):
            _, report, _ = self.cont("wait")
            self.assertEqual(report["directive"], "HOLD_FOR_HUMAN")
            self.assertNotEqual(report["phase"], "RUNNING")
        _, report, _ = self.cont("remote-check")
        self.assertEqual(report["directive"], "HOLD_FOR_HUMAN")

    def test_resume_continues_from_the_protected_boundary(self):
        self.raise_gate()
        code, report, _ = self.cont("resume")
        self.assertEqual(report["directive"], "CONTINUE")
        self.assertEqual(report["phase"], "RUNNING")
        record = self.record()
        self.assertEqual(len(record["continuation"]["gates_history"]), 1)
        self.assertIn("resolved_utc", record["continuation"]["gates_history"][0])

    def test_a_gate_requires_reason_and_action(self):
        code, report, _ = self.cont("gate", "--reason", "x", "--requested-action", "")
        self.assertEqual(report["directive"], "CONTINUE")
        self.assertIn("error", report)

    def test_the_same_gate_notifies_once(self):
        self.raise_gate()
        self.raise_gate()
        self.raise_gate()
        events = self.outbox("human_gate")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["urgency"], "critical")
        self.assertIn("approve deployment", events[0]["message"])

    def test_a_new_gate_after_resume_notifies_again(self):
        self.raise_gate()
        self.cont("resume")
        self.raise_gate(action="approve rollback", reason="rollback needs approval")
        self.assertEqual(len(self.outbox("human_gate")), 2)

    def test_away_gate_notification_reaches_the_messages_channel(self):
        env = self.th_env(TERMINAL_HANDOFF_PRESENCE="away")
        self.raise_gate(env=env)
        event = self.outbox("human_gate")[0]
        config = {"enabled": True, "messages": {"enabled": True, "on": ["complete", "failed"], "when": "away_or_critical"}}
        self.assertIn("messages", CORE.selected_notification_channels(config, event, "away"))
        self.assertIn("messages", CORE.selected_notification_channels(config, event, "unknown"))
        self.assertNotIn("messages", CORE.selected_notification_channels(config, event, "home"))

    def test_notification_failure_does_not_block_or_corrupt(self):
        original = CORE.enqueue_notification

        def boom(*args, **kwargs):
            raise RuntimeError("outbox unavailable")

        os.environ["CLAUDE_TERMINAL_HANDOFF_HOME"] = self.home
        CORE.enqueue_notification = boom
        try:
            ok, why, record = CORE.continuation_raise_gate(
                self.transfer(self.parent_id), SUCCESSOR, "needs approval", "approve"
            )
        finally:
            CORE.enqueue_notification = original
        self.assertTrue(ok, why)
        self.assertEqual(record["state"], "TRANSFER_COMPLETE")
        self.assertEqual(record["continuation"]["phase"], "WAITING_FOR_HUMAN")


class TestRemoteControl(ContinuationCase):
    def test_probe_is_a_real_check_of_the_live_session_record(self):
        os.environ["CLAUDE_TERMINAL_HANDOFF_HOME"] = self.home
        probe = lambda: CORE.probe_remote_control(SUCCESSOR, self.sessions_dir)  # noqa: E731
        self.assertFalse(probe()[0])  # no record at all
        self.session_record(bridge=None)
        self.assertFalse(probe()[0])  # live, but no bridge registered
        self.session_record(pid=2 ** 22 + 12345)  # not a live process
        os.remove(os.path.join(self.sessions_dir, "%d.json" % os.getpid()))
        self.assertFalse(probe()[0])
        os.remove(os.path.join(self.sessions_dir, "%d.json" % (2 ** 22 + 12345)))
        self.session_record()  # live process and a registered bridge
        self.assertTrue(probe()[0])
        self.assertFalse(CORE.probe_remote_control(OTHER, self.sessions_dir)[0])

    def test_healthy_remote_is_recorded_and_secret_never_persisted(self):
        self.make_transfer("TRANSFER_COMPLETE")
        self.session_record()
        self.cont("wait")
        remote = self.record()["continuation"]["remote_control"]
        self.assertEqual(remote["state"], "healthy")
        self.assertNotIn(BRIDGE_SECRET, self.all_text_under_home())

    def test_failed_remote_records_degraded_but_keeps_running(self):
        self.make_transfer("TRANSFER_COMPLETE")
        self.session_record(bridge=None)
        code, report, _ = self.cont("wait")
        self.assertEqual(report["directive"], "CONTINUE")
        self.assertEqual(report["phase"], "DEGRADED_REMOTE")
        self.assertEqual(report["remote_control"]["state"], "degraded")
        self.assertIn("no Remote Control bridge", report["remote_control"]["detail"])
        record = self.record()
        self.assertEqual(record["state"], "TRANSFER_COMPLETE")
        self.assertEqual(record["owner"], "successor")
        self.assertEqual(len(self.outbox("remote_degraded")), 1)

    def test_degraded_remote_never_creates_split_brain(self):
        self.make_transfer("TRANSFER_COMPLETE")
        self.session_record(bridge=None)
        self.cont("wait")
        _, report, _ = self.cont("wait", session=OTHER)
        self.assertEqual(report["directive"], "STOP")
        self.assertEqual(self.record()["successor"]["session_id"], SUCCESSOR)

    def test_remote_recovers_when_it_later_becomes_healthy(self):
        self.make_transfer("TRANSFER_COMPLETE")
        self.session_record(bridge=None)
        self.cont("wait")
        self.session_record()
        _, report, _ = self.cont("remote-check")
        self.assertEqual(report["phase"], "RUNNING")
        self.assertEqual(report["remote_control"]["state"], "healthy")

    def test_remote_disabled_by_configuration_is_reported_and_not_degraded(self):
        self.make_transfer("TRANSFER_COMPLETE")
        env = self.th_env(CLAUDE_TERMINAL_HANDOFF_REMOTE_CONTROL="0")
        _, report, _ = self.cont("wait", env=env)
        self.assertEqual(report["remote_control"]["state"], "disabled")
        self.assertEqual(report["directive"], "CONTINUE")
        self.assertEqual(self.outbox("remote_degraded"), [])

    def test_away_handoff_continues_automatically(self):
        self.make_transfer("TRANSFER_COMPLETE")
        self.session_record()
        _, report, _ = self.cont("wait", env=self.th_env(TERMINAL_HANDOFF_PRESENCE="away"))
        self.assertEqual(report["presence"], "away")
        self.assertEqual(report["directive"], "CONTINUE")

    def test_remote_flag_is_in_the_successor_argv_and_grants_nothing(self):
        manifest = {
            "model": {"id": "claude-opus-5"},
            "effort": {"level": "high", "available": True},
            "chain_id": "abcdef012345",
            "generation": 1,
            "display": {"successor_display_name": "Ranger 2"},
        }
        os.environ.pop("CLAUDE_TERMINAL_HANDOFF_REMOTE_CONTROL", None)
        argv = CORE.build_launch_argv(manifest, "/bin/claude", "PROMPT")
        self.assertIn("--remote-control", argv)
        self.assertNotEqual(argv[argv.index("--remote-control") + 1], "PROMPT")
        self.assertEqual(CORE.assert_launch_argv_safe(argv), [])
        os.environ["CLAUDE_TERMINAL_HANDOFF_REMOTE_CONTROL"] = "0"
        try:
            self.assertNotIn("--remote-control", CORE.build_launch_argv(manifest, "/bin/claude", "P"))
        finally:
            del os.environ["CLAUDE_TERMINAL_HANDOFF_REMOTE_CONTROL"]


class TestOwnershipSafety(ContinuationCase):
    def test_parent_still_owns_before_verification(self):
        for state, phase in (("LAUNCHING", "PREPARING_SUCCESSOR"), ("SUCCESSOR_VERIFIED", "SUCCESSOR_READY")):
            with self.subTest(state=state):
                path = self.make_transfer(state)
                before = json_file(path)
                _, report, _ = self.cont("wait")
                self.assertEqual(report["directive"], "WAIT")
                self.assertEqual(report["phase"], phase)
                self.assertEqual(report["owner"], "parent")
                self.assertEqual(json_file(path), before)

    def test_a_stop_in_flight_is_quiescent_for_both_sessions(self):
        """Parent dying mid-transfer: the successor must not assume ownership."""
        path = self.make_transfer("PARENT_STOP_REQUESTED")
        before = json_file(path)
        _, report, _ = self.cont("wait")
        self.assertEqual(report["directive"], "WAIT")
        self.assertEqual(report["phase"], "OWNERSHIP_TRANSFERRING")
        self.assertEqual(report["owner"], "none")
        _, gate, _ = self.cont("gate", "--reason", "r", "--requested-action", "a")
        self.assertEqual(gate["directive"], "WAIT")
        self.assertEqual(json_file(path), before)

    def test_failed_transfer_stops_the_successor_and_parent_keeps_the_work(self):
        self.make_transfer("TRANSFER_FAILED")
        code, report, _ = self.cont("wait")
        self.assertEqual(code, 3)
        self.assertEqual(report["directive"], "STOP")
        self.assertEqual(report["phase"], "FAILED")
        self.assertEqual(self.record()["owner"], "parent")

    def test_two_successors_only_one_owns(self):
        self.make_transfer("TRANSFER_COMPLETE")
        self.session_record()
        self.cont("wait")
        before = self.record()
        code, report, _ = self.cont("wait", session=OTHER)
        self.assertEqual(report["directive"], "STOP")
        _, gate, _ = self.cont("gate", "--reason", "r", "--requested-action", "a", session=OTHER)
        self.assertEqual(gate["directive"], "STOP")
        _, resumed, _ = self.cont("resume", session=OTHER)
        self.assertEqual(resumed["directive"], "STOP")
        after = self.record()
        self.assertEqual(after["successor"], before["successor"])
        self.assertEqual(after["continuation"]["phase"], before["continuation"]["phase"])
        self.assertFalse(after["continuation"]["human_gate"]["waiting_for_human"])

    def test_second_successor_is_refused_before_ownership_too(self):
        self.make_transfer("SUCCESSOR_VERIFIED")
        _, report, _ = self.cont("wait", session=OTHER)
        self.assertEqual(report["directive"], "STOP")

    def test_an_unbound_session_cannot_continue(self):
        self.make_transfer("TRANSFER_COMPLETE")
        code, out, _ = run_th(
            ["continuation", "wait", "--timeout", "0"], env=self.th_env(CLAUDE_CODE_SESSION_ID="")
        )
        self.assertNotEqual(json.loads(out)["directive"], "CONTINUE")

    def test_duplicate_transitions_cannot_make_two_owners(self):
        os.environ["CLAUDE_TERMINAL_HANDOFF_HOME"] = self.home
        path = self.make_transfer("SUCCESSOR_VERIFIED")
        ok1, _ = CORE.transfer_transition(path, "PARENT_STOP_REQUESTED", reason="one")
        ok2, _ = CORE.transfer_transition(path, "PARENT_STOP_REQUESTED", reason="duplicate")
        ok3, _ = CORE.transfer_transition(path, "TRANSFER_COMPLETE", reason="stopped")
        ok4, _ = CORE.transfer_transition(path, "TRANSFER_COMPLETE", reason="duplicate")
        ok5, _ = CORE.transfer_transition(path, "SUCCESSOR_VERIFIED", reason="regress")
        self.assertEqual([ok1, ok2, ok3, ok4, ok5], [True, False, True, False, False])
        self.assertEqual(self.record()["owner"], "successor")

    def test_successor_death_after_ownership_leaves_a_recoverable_record(self):
        self.make_transfer("TRANSFER_COMPLETE")
        self.session_record()
        self.cont("wait")
        self.cont("gate", "--reason", "needs approval", "--requested-action", "approve")
        os.remove(os.path.join(self.sessions_dir, "%d.json" % os.getpid()))  # the session vanished
        record = self.record()
        self.assertEqual(record["state"], "TRANSFER_COMPLETE")
        self.assertEqual(record["continuation"]["phase"], "WAITING_FOR_HUMAN")
        _, report, _ = self.cont("status")
        self.assertEqual(report["directive"], "HOLD_FOR_HUMAN")  # readable for recovery
        _, stranger, _ = self.cont("wait", session=OTHER)
        self.assertEqual(stranger["directive"], "STOP")  # nobody else takes over silently

    def test_successor_death_before_ownership_does_not_corrupt_ownership(self):
        path = self.make_transfer("LAUNCHING", successor_session=None)
        before = json_file(path)
        _, report, _ = self.cont("wait")
        self.assertEqual(report["owner"], "parent")
        self.assertEqual(json_file(path), before)


class TestFullTransitionIntegration(TransferTestCase):
    """Real handoff → heartbeat → supervisor stops parent → successor continues."""

    def test_end_to_end(self):
        parent = self.handoff()
        pid = parent["_session_id"]
        standin = self.standin_claude("parent")
        self.bind_transfer_to(pid, standin.pid)
        successor = self.payload(percent=4.0, session_name="Ranger 2")
        env = self.successor_env(parent)
        sessions = os.path.join(self.tmp, "claude-sessions")
        os.makedirs(sessions)
        env["CLAUDE_TERMINAL_HANDOFF_CLAUDE_SESSIONS_DIR"] = sessions
        env["CLAUDE_TERMINAL_HANDOFF_REMOTE_VERIFY_SECONDS"] = "0"
        env["CLAUDE_TERMINAL_HANDOFF_TRANSFER_POLL"] = "0.1"

        # Before the heartbeat the successor is told to keep waiting, read-only.
        code, out, _ = run_th(
            ["continuation", "wait", "--timeout", "0", "--session-id", successor["_session_id"]], env=env
        )
        self.assertEqual(json.loads(out)["directive"], "WAIT")

        self.beat(successor, env)
        code, out, _ = run_th(
            ["continuation", "wait", "--timeout", "0", "--session-id", successor["_session_id"]], env=env
        )
        self.assertEqual(json.loads(out)["directive"], "WAIT")  # verified, parent still owns
        self.supervise(pid)
        self.assertFalse(process_alive(standin.pid))
        record = json_file(self.transfer(pid))
        self.assertEqual(record["state"], "TRANSFER_COMPLETE")

        # Remote Control registered for the successor's own live session.
        with open(os.path.join(sessions, "%d.json" % os.getpid()), "w") as handle:
            json.dump({"pid": os.getpid(), "sessionId": successor["_session_id"], "bridgeSessionId": "b"}, handle)
        code, out, _ = run_th(
            ["continuation", "wait", "--timeout", "5", "--session-id", successor["_session_id"]], env=env
        )
        report = json.loads(out)
        self.assertEqual(report["directive"], "CONTINUE")
        self.assertEqual(report["remote_control"]["state"], "healthy")

        # A human gate is raised and held; then resolved.
        run_th(
            ["continuation", "gate", "--session-id", successor["_session_id"], "--reason", "deploy",
             "--requested-action", "approve deploy"], env=env)
        code, out, _ = run_th(["continuation", "status", "--session-id", successor["_session_id"]], env=env)
        self.assertEqual(json.loads(out)["directive"], "HOLD_FOR_HUMAN")
        code, out, _ = run_th(["continuation", "resume", "--session-id", successor["_session_id"]], env=env)
        self.assertEqual(json.loads(out)["directive"], "CONTINUE")

        # The audit log names every milestone and never a secret.
        log = text_file(os.path.join(self.home, "logs", "terminal-handoff.log"))
        for event in (
            "handoff_requested", "successor_spawned", "successor_ready",
            "ownership_transfer_started", "parent_ownership_released",
            "successor_ownership_acquired", "remote_control_activation_attempted",
            "remote_control_verified", "automatic_continuation_started",
            "human_gate_reached", "human_gate_resolved",
        ):
            self.assertIn('"event": "%s"' % event, log, event)

    def test_launch_argv_requests_remote_control_without_any_bypass(self):
        parent = self.handoff()
        argv = json_file(self.launch_record(parent["_session_id"]))["argv"]
        self.assertIn("--remote-control", argv)
        for token in CORE.FORBIDDEN_LAUNCH_TOKENS:
            self.assertNotIn(token, argv[:-1])


class TestNoPermissionBypass(unittest.TestCase):
    def test_bypass_flags_remain_forbidden(self):
        for token in ("--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "--permission-mode"):
            self.assertIn(token, CORE.FORBIDDEN_LAUNCH_TOKENS)
            argv = ["claude", "--model", "m", token, "prompt"]
            self.assertTrue(CORE.assert_launch_argv_safe(argv))

    def test_no_code_path_answers_prompts_or_types_into_a_session(self):
        source = text_file(TH_SCRIPT)
        self.assertNotIn("keystroke", source)
        self.assertNotIn("System Events", source)
        for hit in re.finditer(r"--dangerously-skip-permissions|--allow-dangerously-skip-permissions", source):
            line_start = source.rfind("\n", 0, hit.start()) + 1
            line = source[line_start: source.find("\n", hit.end())]
            self.assertTrue(line.strip().startswith(('"', "#")) or "FORBIDDEN" in line, line)
        template = text_file(os.path.join(os.path.dirname(TH_SCRIPT), "templates", "successor-prompt.md"))
        self.assertNotIn("dangerously", template.replace("never use any permission bypass", ""))


if __name__ == "__main__":
    unittest.main()
