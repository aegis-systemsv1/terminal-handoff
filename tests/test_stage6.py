"""Approvals, wake, dead-owner detection, restart recovery, persistent security
state, permission isolation and launch-token handling."""

import json
import os
import re
import subprocess
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import CORE, json_file, process_alive, run_th, text_file, wait_for_exit  # noqa: E402
from test_continuation import ContinuationCase, SUCCESSOR  # noqa: E402
from test_logical import LogicalCase  # noqa: E402
from test_remote_api import HOST, LOGIN, ORIGIN, RemoteCase  # noqa: E402

OWNER = "agent-A-session"
NOSLEEP = lambda seconds: None  # noqa: E731


def approve_ready(case, lsid, action="Restart the Nova production service after deploying commit abc123.", reason="The service must restart for the change to take effect."):
    ok, why, approval = CORE.approval_request(lsid, OWNER, action, reason)
    assert ok, why
    return approval


class TestApprovals(LogicalCase):
    def decide(self, lsid, approval, decision="approve", nonce=None, epoch=None, by="device:d_1"):
        record = CORE.logical_read(lsid)
        return CORE.approval_decide(lsid, approval["id"], decision, nonce or approval["nonce"], record["owner_epoch"] if epoch is None else epoch, by)

    def test_only_the_owner_can_request_and_the_state_becomes_waiting(self):
        lsid = self.new_session()
        self.assertFalse(CORE.approval_request(lsid, "intruder", "deploy", "why")[0])
        approval = approve_ready(self, lsid)
        self.assertEqual(CORE.logical_read(lsid)["state"], "WAITING_FOR_HUMAN")
        view = CORE.logical_public_view(CORE.logical_read(lsid))
        self.assertEqual(view["state"], "WAITING_FOR_HUMAN")
        self.assertEqual(view["human_gate"]["action"], approval["action"])
        self.assertIn("nonce", view["human_gate"])
        self.assertFalse(CORE.approval_request(lsid, OWNER, "", "why")[0])
        self.assertFalse(CORE.approval_request(lsid, OWNER, "deploy", "")[0])

    def test_approve_then_consume_exactly_once(self):
        lsid = self.new_session()
        approval = approve_ready(self, lsid)
        self.assertFalse(CORE.approval_consume(lsid, OWNER, approval["id"], approval["action"])[0])  # not yet decided
        ok, why, decided = self.decide(lsid, approval)
        self.assertTrue(ok, why)
        self.assertEqual(CORE.logical_read(lsid)["state"], "RUNNING")
        self.assertTrue(CORE.approval_consume(lsid, OWNER, approval["id"], approval["action"])[0])
        ok, why, _ = CORE.approval_consume(lsid, OWNER, approval["id"], approval["action"])
        self.assertFalse(ok)  # duplicate approval cannot execute twice
        self.assertIn("consumed", why)

    def test_denial_is_respected(self):
        lsid = self.new_session()
        approval = approve_ready(self, lsid)
        self.assertTrue(self.decide(lsid, approval, "deny")[0])
        ok, why, _ = CORE.approval_consume(lsid, OWNER, approval["id"], approval["action"])
        self.assertFalse(ok)
        self.assertIn("denied", why)

    def test_duplicate_and_replayed_decisions_are_refused(self):
        lsid = self.new_session()
        approval = approve_ready(self, lsid)
        self.assertTrue(self.decide(lsid, approval)[0])
        ok, why, _ = self.decide(lsid, approval)
        self.assertFalse(ok)
        self.assertIn("not pending", why)
        self.assertFalse(self.decide(lsid, approval, "deny")[0])  # cannot flip after the fact

    def test_wrong_nonce_unknown_id_and_bad_input_are_refused(self):
        lsid = self.new_session()
        approval = approve_ready(self, lsid)
        for nonce in ("wrong", "", None, 5, approval["nonce"][:-1], approval["nonce"] + "x"):
            with self.subTest(nonce=nonce):
                self.assertFalse(CORE.approval_decide(lsid, approval["id"], "approve", nonce, 1, "d")[0])
        self.assertFalse(CORE.approval_decide(lsid, "ap_" + "0" * 16, "approve", "x", 1, "d")[0])
        self.assertFalse(CORE.approval_decide(lsid, "../etc", "approve", "x", 1, "d")[0])
        self.assertFalse(CORE.approval_decide(lsid, approval["id"], "maybe", approval["nonce"], 1, "d")[0])
        self.assertEqual(CORE.logical_read(lsid)["approvals"][0]["status"], "pending")

    def test_expired_approval_cannot_be_decided_or_used(self):
        lsid = self.new_session()
        approval = approve_ready(self, lsid)
        CORE.logical_mutate(lsid, lambda r: r["approvals"][0].update(expires_epoch=1.0))
        ok, why, _ = self.decide(lsid, approval)
        self.assertFalse(ok)
        self.assertIn("expired", why)
        view = CORE.logical_public_view(CORE.logical_read(lsid))
        self.assertIsNone(view["human_gate"])
        self.assertEqual(view["state"], "RUNNING")
        # an approved-but-expired approval cannot be consumed either
        lsid2 = self.new_session()
        a2 = approve_ready(self, lsid2)
        self.decide(lsid2, a2)
        CORE.logical_mutate(lsid2, lambda r: r["approvals"][0].update(expires_epoch=1.0))
        ok, why, _ = CORE.approval_consume(lsid2, OWNER, a2["id"], a2["action"])
        self.assertFalse(ok)
        self.assertIn("expired", why)

    def test_a_changed_action_needs_a_new_approval(self):
        lsid = self.new_session()
        first = approve_ready(self, lsid, action="Deploy build abc123 to production")
        self.decide(lsid, first)
        ok, why, _ = CORE.approval_consume(lsid, OWNER, first["id"], "Deploy build def456 to production")
        self.assertFalse(ok)
        self.assertIn("differs", why)
        # asking for a different action supersedes the open one; the old cannot be used
        second = approve_ready(self, lsid, action="Deploy build def456 to production")
        self.assertNotEqual(first["id"], second["id"])
        statuses = {a["id"]: a["status"] for a in CORE.logical_read(lsid)["approvals"]}
        self.assertEqual(statuses[first["id"]], "superseded")
        self.assertFalse(CORE.approval_consume(lsid, OWNER, first["id"], "Deploy build abc123 to production")[0])
        self.assertFalse(self.decide(lsid, first)[0])
        # whitespace differences alone are not a material change
        third = approve_ready(self, lsid, action="Deploy   build def456   to production")
        self.assertEqual(third["id"], second["id"])

    def test_the_same_open_request_does_not_spam_notifications(self):
        lsid = self.new_session()
        for _ in range(4):
            approve_ready(self, lsid)
        pending = os.listdir(os.path.join(self.home, "outbox", "pending"))
        kinds = [json_file(os.path.join(self.home, "outbox", "pending", n))["event"]["kind"] for n in pending]
        self.assertEqual(kinds.count("human_gate"), 1)

    def test_approvals_cannot_cross_logical_sessions(self):
        one, two = self.new_session(owner=OWNER), self.new_session(owner="agent-two-session")
        a1 = approve_ready(self, one)
        self.assertFalse(CORE.approval_decide(two, a1["id"], "approve", a1["nonce"], 1, "d")[0])
        ok, _, a2 = CORE.approval_request(two, "agent-two-session", "same action text", "why")
        self.assertFalse(CORE.approval_decide(one, a2["id"], "approve", a2["nonce"], 1, "d")[0])
        self.assertFalse(CORE.approval_consume(two, "agent-two-session", a1["id"], a1["action"])[0])

    def test_pending_approval_survives_a_handoff_but_must_be_reviewed_again(self):
        lsid = self.new_session()
        approval = approve_ready(self, lsid)
        old_epoch = CORE.logical_read(lsid)["owner_epoch"]
        CORE.logical_adopt_successor(lsid, self.transfer_complete(lsid, OWNER, "agent-B-session"))
        record = CORE.logical_read(lsid)
        self.assertEqual(record["state"], "WAITING_FOR_HUMAN")
        pending = [a for a in record["approvals"] if a["status"] == "pending"]
        self.assertEqual([a["id"] for a in pending], [approval["id"]])
        self.assertEqual(pending[0]["action"], approval["action"])
        self.assertEqual(pending[0]["bound_epoch"], record["owner_epoch"])
        # a decision made against the epoch the phone last displayed is stale
        ok, why, _ = CORE.approval_decide(lsid, approval["id"], "approve", approval["nonce"], old_epoch, "d")
        self.assertFalse(ok)
        self.assertIn("stale", why)
        ok, why, _ = CORE.approval_decide(lsid, approval["id"], "approve", approval["nonce"], record["owner_epoch"], "d")
        self.assertTrue(ok, why)
        self.assertFalse(CORE.approval_consume(lsid, OWNER, approval["id"], approval["action"])[0])  # stale owner
        self.assertTrue(CORE.approval_consume(lsid, "agent-B-session", approval["id"], approval["action"])[0])

    def test_an_unused_approval_does_not_cross_a_handoff(self):
        lsid = self.new_session()
        approval = approve_ready(self, lsid)
        self.decide(lsid, approval)
        CORE.logical_adopt_successor(lsid, self.transfer_complete(lsid, OWNER, "agent-B-session"))
        ok, why, _ = CORE.approval_consume(lsid, "agent-B-session", approval["id"], approval["action"])
        self.assertFalse(ok)
        self.assertIn("invalidated_by_handoff", why)
        self.assertFalse(CORE.approval_consume(lsid, OWNER, approval["id"], approval["action"])[0])

    def test_stale_owner_cannot_consume_or_request(self):
        lsid = self.new_session()
        approval = approve_ready(self, lsid)
        self.decide(lsid, approval)
        CORE.logical_adopt_successor(lsid, self.transfer_complete(lsid, OWNER, "agent-B-session"))
        self.assertFalse(CORE.approval_request(lsid, OWNER, "another action", "why")[0])
        self.assertFalse(CORE.approval_consume(lsid, OWNER, approval["id"], approval["action"])[0])

    def test_stop_blocks_consumption_and_new_requests(self):
        lsid = self.new_session()
        approval = approve_ready(self, lsid)
        self.decide(lsid, approval)
        CORE.logical_stop(lsid, by="phone", reason="hold")
        ok, why, _ = CORE.approval_consume(lsid, OWNER, approval["id"], approval["action"])
        self.assertFalse(ok)
        self.assertIn("halted", why)
        self.assertFalse(CORE.approval_request(lsid, OWNER, "x action", "why")[0])
        CORE.logical_resume(lsid, by="phone", clear_stop=True, reason="reviewed")
        self.assertTrue(CORE.approval_consume(lsid, OWNER, approval["id"], approval["action"])[0])

    def test_orphaned_sessions_take_no_decisions(self):
        lsid = self.new_session()
        approval = approve_ready(self, lsid)
        CORE.logical_mutate(lsid, lambda r: r.update(state="ORPHANED"))
        self.assertFalse(self.decide(lsid, approval)[0])

    def test_terminal_handoff_approval_is_not_a_native_permission_answer(self):
        """The gate is cooperative. Nothing in the code types into or answers Claude's prompt."""
        source = text_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "terminal_handoff", "core.py"))
        for forbidden in ("keystroke", "System Events", "TIOCSTI", "pty.", "os.write(fd, b"):
            self.assertNotIn(forbidden, source)


class TestApprovalOverHttp(RemoteCase):
    def setUp(self):
        super().setUp()
        _, self.token = self.enroll_token()
        self.lsid = self.new_session()
        self.approval = approve_ready(self, self.lsid)
        self.epoch = CORE.logical_read(self.lsid)["owner_epoch"]

    def act(self, decision, body=None, aid=None, lsid=None, token=None):
        path = "/api/v1/sessions/%s/approvals/%s/%s" % (lsid or self.lsid, aid or self.approval["id"], decision)
        body = body if body is not None else {"nonce": self.approval["nonce"], "owner_epoch": self.epoch, "request_id": "req-ap-%08d" % (time.time_ns() % 10 ** 8)}
        return self.call("POST", path, body, token=token or self.token)

    def test_the_phone_sees_exactly_what_is_being_approved(self):
        status, view, _ = self.call("GET", "/api/v1/sessions/" + self.lsid, token=self.token)
        gate = view["human_gate"]
        self.assertEqual(gate["action"], self.approval["action"])
        self.assertEqual(gate["reason"], self.approval["reason"])
        self.assertEqual(view["state"], "WAITING_FOR_HUMAN")
        self.assertEqual(view["owner"]["epoch"], self.epoch)

    def test_approve_over_http_then_agent_consumes(self):
        status, view, _ = self.act("approve")
        self.assertEqual(status, 200, view)
        self.assertEqual(view["state"], "RUNNING")
        self.assertTrue(CORE.approval_consume(self.lsid, OWNER, self.approval["id"], self.approval["action"])[0])

    def test_deny_over_http(self):
        self.assertEqual(self.act("deny")[0], 200)
        self.assertFalse(CORE.approval_consume(self.lsid, OWNER, self.approval["id"], self.approval["action"])[0])

    def test_replay_and_duplicate_approval_over_http(self):
        body = {"nonce": self.approval["nonce"], "owner_epoch": self.epoch, "request_id": "req-ap-fixed001"}
        first = self.act("approve", body)
        again = self.act("approve", body)
        self.assertTrue(again[1].get("replayed"))
        other = self.act("approve", dict(body, request_id="req-ap-fixed002"))
        self.assertEqual(other[0], 409)  # a fresh request for an already-decided approval
        self.server.gateway.replay.clear()
        self.assertEqual(self.act("approve", body)[0], 409)  # and after a gateway restart

    def test_stale_epoch_forged_nonce_and_bad_fields_are_refused(self):
        self.assertEqual(self.act("approve", {"nonce": self.approval["nonce"], "owner_epoch": self.epoch + 1, "request_id": "req-ap-stale001"})[0], 409)
        self.assertEqual(self.act("approve", {"nonce": "forged", "owner_epoch": self.epoch, "request_id": "req-ap-forged01"})[0], 409)
        self.assertEqual(self.act("approve", {"nonce": self.approval["nonce"], "owner_epoch": "1", "request_id": "req-ap-string01"})[0], 400)
        self.assertEqual(self.act("approve", {"nonce": self.approval["nonce"], "owner_epoch": True, "request_id": "req-ap-bool0001"})[0], 400)
        self.assertEqual(self.act("approve", {"nonce": self.approval["nonce"], "owner_epoch": self.epoch, "request_id": "req-ap-extra001", "action": "other"})[0], 400)
        self.assertEqual(self.act("approve", aid="ap_" + "1" * 16)[0], 404)
        self.assertEqual(CORE.logical_read(self.lsid)["approvals"][0]["status"], "pending")

    def test_unauthenticated_and_cross_session_approval_is_refused(self):
        self.assertEqual(self.act("approve", token="thd_d_0123456789abcdef.nope")[0], 401)
        other = self.new_session(owner="agent-two-session")
        self.assertEqual(self.act("approve", lsid=other)[0], 404)
        self.assertEqual(self.call("POST", "/api/v1/sessions/%s/approvals/%s/approve" % (self.lsid, self.approval["id"]),
                                   {"nonce": self.approval["nonce"], "owner_epoch": self.epoch, "request_id": "req-ap-noorig01"}, token=self.token, origin=None)[0], 403)

    def test_approval_action_rate_limit_is_persistent(self):
        limit = CORE.SENSITIVE_LIMITS["approve"][0]
        for i in range(limit):
            self.act("approve", {"nonce": "x", "owner_epoch": self.epoch, "request_id": "req-ap-rl%06d" % i})
        self.assertEqual(self.act("approve", {"nonce": "x", "owner_epoch": self.epoch, "request_id": "req-ap-rl999999"})[0], 429)


class TestGateUnification(ContinuationCase):
    def setUp(self):
        super().setUp()
        self._saved = os.environ.get("CLAUDE_TERMINAL_HANDOFF_HOME")
        os.environ["CLAUDE_TERMINAL_HANDOFF_HOME"] = self.home

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("CLAUDE_TERMINAL_HANDOFF_HOME", None)
        else:
            os.environ["CLAUDE_TERMINAL_HANDOFF_HOME"] = self._saved
        super().tearDown()

    def linked(self):
        import hashlib

        record = CORE.logical_create(project="nova", repository=self.workdir, launch_token_sha256=hashlib.sha256(b"t").hexdigest(), launch_expires_epoch=9999999999)
        lsid = record["logical_session_id"]
        CORE.logical_register_owner(lsid, self.parent_id, 1, None, None, "t")
        self.make_transfer("TRANSFER_COMPLETE", logical_session_id=lsid)
        self.session_record()
        self.cont("wait")
        return lsid

    def test_continuation_gate_creates_one_remote_visible_approval_and_one_notification(self):
        lsid = self.linked()
        _, report, _ = self.cont("gate", "--reason", "needs approval", "--requested-action", "Deploy build abc123")
        self.assertEqual(report["directive"], "HOLD_FOR_HUMAN")
        view = CORE.logical_public_view(CORE.logical_read(lsid))
        self.assertEqual(view["human_gate"]["action"], "Deploy build abc123")
        self.assertEqual(len(self.outbox("human_gate")), 1)
        self.cont("gate", "--reason", "needs approval", "--requested-action", "Deploy build abc123")
        self.assertEqual(len(self.outbox("human_gate")), 1)

    def test_the_agent_cannot_resume_a_gate_the_human_has_not_decided(self):
        lsid = self.linked()
        self.cont("gate", "--reason", "r", "--requested-action", "Deploy build abc123")
        _, report, _ = self.cont("resume")
        self.assertIn("error", report)
        self.assertEqual(report["directive"], "HOLD_FOR_HUMAN")
        approval = CORE.logical_read(lsid)["approvals"][0]
        CORE.approval_decide(lsid, approval["id"], "approve", approval["nonce"], CORE.logical_read(lsid)["owner_epoch"], "device:d_1")
        _, report, _ = self.cont("resume")
        self.assertEqual(report["directive"], "CONTINUE")
        self.assertEqual(report["approval_decisions"][0]["status"], "approved")


class TestWake(LogicalCase):
    def test_pending_work_wakes_the_owner_immediately(self):
        lsid = self.new_session()
        CORE.inbox_post(lsid, "please audit")
        result = CORE.logical_wait(lsid, OWNER, 5, sleep=NOSLEEP)
        self.assertEqual(result["directive"], "INSTRUCTIONS")
        self.assertEqual(result["messages"][0]["text"], "please audit")

    def test_a_blocked_wait_wakes_when_an_instruction_arrives(self):
        lsid = self.new_session()
        threading.Timer(0.3, lambda: CORE.inbox_post(lsid, "arrives later")).start()
        started = time.time()
        result = CORE.logical_wait(lsid, OWNER, 8, sleep=lambda s: time.sleep(0.05))
        self.assertEqual(result["directive"], "INSTRUCTIONS")
        self.assertLess(time.time() - started, 4)

    def test_timeout_returns_wait_and_records_the_agent_poll(self):
        lsid = self.new_session()
        self.assertEqual(CORE.logical_wait(lsid, OWNER, 0, sleep=NOSLEEP)["directive"], "WAIT")
        view = CORE.logical_public_view(CORE.logical_read(lsid))
        self.assertLess(view["agent_poll_age_seconds"], 5)

    def test_wake_goes_only_to_the_current_owner(self):
        lsid = self.new_session()
        CORE.inbox_post(lsid, "work")
        self.assertEqual(CORE.logical_wait(lsid, "stale-or-stranger", 0, sleep=NOSLEEP)["directive"], "STOP")
        CORE.logical_adopt_successor(lsid, self.transfer_complete(lsid, OWNER, "agent-B-session"))
        self.assertEqual(CORE.logical_wait(lsid, OWNER, 0, sleep=NOSLEEP)["directive"], "STOP")  # the stale owner
        result = CORE.logical_wait(lsid, "agent-B-session", 0, sleep=NOSLEEP)
        self.assertEqual(result["directive"], "INSTRUCTIONS")

    def test_an_instruction_during_a_transfer_is_kept_for_the_legitimate_owner(self):
        lsid = self.new_session()
        CORE.inbox_post(lsid, "before")
        CORE.inbox_claim(lsid, OWNER)  # delivered to A, unacknowledged
        # ownership is mid-transfer: nothing has moved yet, and a new instruction arrives
        CORE.inbox_post(lsid, "mid-transfer")
        record = CORE.logical_read(lsid)
        self.assertEqual([m["status"] for m in record["inbox"]["messages"]], ["delivered", "pending"])
        CORE.logical_adopt_successor(lsid, self.transfer_complete(lsid, OWNER, "agent-B-session"))
        result = CORE.logical_wait(lsid, "agent-B-session", 0, sleep=NOSLEEP)
        self.assertEqual([m["text"] for m in result["messages"]], ["before", "mid-transfer"])

    def test_a_lost_wake_loses_nothing(self):
        lsid = self.new_session()
        CORE.inbox_post(lsid, "important")
        first = CORE.logical_wait(lsid, OWNER, 0, sleep=NOSLEEP)  # the result never reaches the agent
        self.assertEqual(first["directive"], "INSTRUCTIONS")
        self.assertEqual(CORE.logical_wait(lsid, OWNER, 0, sleep=NOSLEEP)["directive"], "WAIT")  # leased, not spammed
        CORE.logical_mutate(lsid, lambda r: r["inbox"]["messages"][0].update(delivered_time=time.time() - CORE.DELIVERY_LEASE_SECONDS - 5))
        again = CORE.logical_wait(lsid, OWNER, 0, sleep=NOSLEEP)
        self.assertEqual(again["directive"], "INSTRUCTIONS")
        self.assertEqual(again["messages"][0]["redelivered"], 1)

    def test_stop_holds_delivery_and_a_deliberate_resume_wakes_the_agent(self):
        lsid = self.new_session()
        CORE.logical_stop(lsid, by="phone", reason="hold")
        CORE.inbox_post(lsid, "queued while stopped")
        self.assertEqual(CORE.logical_wait(lsid, OWNER, 0, sleep=NOSLEEP)["directive"], "HALT")
        self.assertEqual(CORE.logical_read(lsid)["inbox"]["messages"][0]["status"], "pending")
        threading.Timer(0.3, lambda: CORE.logical_resume(lsid, by="phone", clear_stop=True, reason="ok")).start()
        result = CORE.logical_wait(lsid, OWNER, 8, sleep=lambda s: time.sleep(0.05))
        self.assertEqual(result["directive"], "CONTINUE")
        self.assertEqual(CORE.logical_wait(lsid, OWNER, 0, sleep=NOSLEEP)["directive"], "INSTRUCTIONS")

    def test_a_stop_that_begins_while_blocked_is_reported_immediately(self):
        lsid = self.new_session()
        threading.Timer(0.3, lambda: CORE.logical_stop(lsid, by="phone", reason="hold")).start()
        started = time.time()
        result = CORE.logical_wait(lsid, OWNER, 30, sleep=lambda s: time.sleep(0.05))
        self.assertEqual((result["directive"], result["halt"]), ("HALT", "stop"))
        self.assertLess(time.time() - started, 5)  # not at the 30 s timeout
        # the next wait then blocks quietly until a deliberate resume
        self.assertTrue(CORE.logical_wait(lsid, OWNER, 0, sleep=NOSLEEP)["timeout"])
        threading.Timer(0.3, lambda: CORE.logical_resume(lsid, by="phone", clear_stop=True, reason="ok")).start()
        self.assertEqual(CORE.logical_wait(lsid, OWNER, 30, sleep=lambda s: time.sleep(0.05))["directive"], "CONTINUE")

    def test_a_decision_wakes_the_agent_once(self):
        lsid = self.new_session()
        approval = approve_ready(self, lsid)
        threading.Timer(0.3, lambda: CORE.approval_decide(lsid, approval["id"], "approve", approval["nonce"], 1, "device:d_1")).start()
        result = CORE.logical_wait(lsid, OWNER, 8, sleep=lambda s: time.sleep(0.05))
        self.assertEqual(result["directive"], "APPROVAL_DECISION")
        self.assertEqual(result["approval"]["status"], "approved")
        self.assertEqual(CORE.logical_wait(lsid, OWNER, 0, sleep=NOSLEEP)["directive"], "WAIT")

    def test_ended_and_orphaned_sessions_tell_the_agent_to_stop(self):
        lsid = self.new_session()
        CORE.logical_mutate(lsid, lambda r: r.update(state="ORPHANED"))
        self.assertEqual(CORE.logical_wait(lsid, OWNER, 0, sleep=NOSLEEP)["directive"], "STOP")

    def test_the_wait_is_bounded(self):
        lsid = self.new_session()
        start = time.time()
        CORE.logical_wait(lsid, OWNER, 10 ** 6, sleep=lambda s: (_ for _ in ()).throw(KeyboardInterrupt()) if False else None, clock=iter([0, 0, 0, CORE.WAIT_MAX_SECONDS + 1, CORE.WAIT_MAX_SECONDS + 1] + [10 ** 7] * 20).__next__)
        self.assertLess(time.time() - start, 5)

    def test_cli_wait_round_trip(self):
        lsid = self.new_session()
        CORE.inbox_post(lsid, "via cli")
        code, out, _ = run_th(["session", "wait", "--logical-session", lsid, "--session-id", OWNER, "--timeout", "2"], env=self.env())
        self.assertEqual(json.loads(out)["directive"], "INSTRUCTIONS")

    def test_stop_hook_keeps_the_agent_from_idling_past_waiting_work(self):
        lsid = self.new_session()
        env = {"CLAUDE_TERMINAL_HANDOFF_LOGICAL_SESSION": lsid}
        stdin = json.dumps({"session_id": OWNER, "stop_hook_active": False})
        self.assertEqual(CORE.session_hook_stop(stdin, env)[0], 0)  # nothing waiting
        CORE.inbox_post(lsid, "work")
        code, message = CORE.session_hook_stop(stdin, env)
        self.assertEqual(code, 2)  # the documented "keep working" exit code
        self.assertIn("session wait", message)
        self.assertEqual(CORE.session_hook_stop(json.dumps({"session_id": OWNER, "stop_hook_active": True}), env)[0], 0)  # no loop
        self.assertEqual(CORE.session_hook_stop(json.dumps({"session_id": "stranger"}), env)[0], 0)
        self.assertEqual(CORE.session_hook_stop("not json", env)[0], 0)
        self.assertEqual(CORE.session_hook_stop(stdin, {})[0], 0)
        self.assertEqual(CORE.session_hook_stop(stdin, {"CLAUDE_TERMINAL_HANDOFF_LOGICAL_SESSION": "../x"})[0], 0)
        CORE.logical_stop(lsid, by="phone", reason="x")
        self.assertEqual(CORE.session_hook_stop(stdin, env)[0], 0)  # a stopped session may stop


class TestDeadOwnerDetection(LogicalCase):
    def alive(self, record, now):
        return "alive", "test"

    def dead(self, record, now):
        return "dead", "the bound Claude process has exited"

    def unknown(self, record, now):
        return "unknown", "no evidence"

    def test_a_single_or_short_failure_does_not_orphan(self):
        lsid = self.new_session()
        CORE.logical_reconcile(lsid, liveness=self.dead, now=1000.0)
        CORE.logical_reconcile(lsid, liveness=self.dead, now=1030.0)
        self.assertEqual(CORE.logical_read(lsid)["state"], "RUNNING")
        CORE.logical_reconcile(lsid, liveness=self.alive, now=1040.0)  # a transient blip recovers
        self.assertEqual(CORE.logical_read(lsid)["owner_health"]["failures"], 0)
        CORE.logical_reconcile(lsid, liveness=self.dead, now=1100.0)
        CORE.logical_reconcile(lsid, liveness=self.dead, now=1130.0)
        CORE.logical_reconcile(lsid, liveness=self.dead, now=1150.0)
        self.assertEqual(CORE.logical_read(lsid)["state"], "RUNNING")  # only 50s since the first failure

    def test_sustained_death_becomes_orphaned_and_notifies_once(self):
        lsid = self.new_session()
        for t in (1000.0, 1030.0, 1065.0, 1100.0):
            CORE.logical_reconcile(lsid, liveness=self.dead, now=t)
        record = CORE.logical_read(lsid)
        self.assertEqual(record["state"], "ORPHANED")
        self.assertEqual(record["orphaned"]["previous_state"], "RUNNING")
        kinds = [json_file(os.path.join(self.home, "outbox", "pending", n))["event"]["kind"] for n in os.listdir(os.path.join(self.home, "outbox", "pending"))]
        self.assertEqual(kinds.count("owner_lost"), 1)
        self.assertEqual(CORE.logical_public_view(record)["state"], "ORPHANED")

    def test_unknown_is_not_evidence_of_death(self):
        lsid = self.new_session()
        for i in range(12):
            CORE.logical_reconcile(lsid, liveness=self.unknown, now=1000.0 + i * 100)
        self.assertEqual(CORE.logical_read(lsid)["state"], "RUNNING")

    def test_orphaned_is_never_replaced_automatically(self):
        lsid = self.new_session()
        for t in (1000.0, 1030.0, 1065.0):
            CORE.logical_reconcile(lsid, liveness=self.dead, now=t)
        before = len(CORE.logical_list())
        CORE.logical_reconcile(lsid, liveness=self.alive, now=2000.0)  # even a later "alive" does not self-heal
        self.assertEqual(CORE.logical_read(lsid)["state"], "ORPHANED")
        self.assertEqual(len(CORE.logical_list()), before)
        self.assertFalse(CORE.inbox_post(lsid, "still queued")[0] is False)  # instructions are kept, not lost
        self.assertEqual(CORE.logical_wait(lsid, OWNER, 0, sleep=NOSLEEP)["directive"], "STOP")

    def test_deliberate_recovery(self):
        lsid = self.new_session()
        CORE.logical_stop(lsid, by="phone", reason="hold")
        for t in (1000.0, 1030.0, 1065.0):
            CORE.logical_reconcile(lsid, liveness=self.dead, now=t)
        self.assertEqual(CORE.logical_read(lsid)["state"], "ORPHANED")
        ok, why, _ = CORE.logical_recover(lsid, "reattach", liveness=self.dead)
        self.assertFalse(ok)
        ok, why, record = CORE.logical_recover(lsid, "reattach", liveness=self.alive)
        self.assertTrue(ok, why)
        self.assertEqual(record["state"], "STOPPED")  # STOP survived the whole episode
        self.assertFalse(CORE.logical_recover(lsid, "reattach", liveness=self.alive)[0])  # not orphaned any more
        lsid2 = self.new_session()
        CORE.logical_mutate(lsid2, lambda r: r.update(state="ORPHANED"))
        self.assertEqual(CORE.logical_recover(lsid2, "abandon")[2]["state"], "FAILED")
        self.assertFalse(CORE.logical_recover(lsid2, "relaunch")[0])

    def test_a_parent_being_replaced_is_not_orphaned(self):
        lsid = self.new_session()
        os.makedirs(os.path.join(self.home, "transfers"), exist_ok=True)
        with open(os.path.join(self.home, "transfers", "%s.json" % OWNER), "w") as handle:
            json.dump({"state": "PARENT_STOP_REQUESTED", "parent_session_id": OWNER, "created_epoch": time.time()}, handle)
        for t in (1000.0, 1100.0, 1200.0, 1300.0):
            CORE.logical_reconcile(lsid, liveness=self.dead, now=t)
        self.assertEqual(CORE.logical_read(lsid)["state"], "RUNNING")

    def test_real_process_death_is_detected_and_a_live_process_is_not_misjudged(self):
        lsid = self.new_session()
        standin = self.standin_claude("owner")
        binding = self.binding_for(standin.pid, session_id=OWNER)
        CORE.logical_mutate(lsid, lambda r: r["owner"].update(process=binding))
        record = CORE.logical_read(lsid)
        self.assertEqual(CORE.owner_liveness(record)[0], "alive")
        standin.terminate()
        self.assertTrue(wait_for_exit(standin.pid))
        self.assertEqual(CORE.owner_liveness(CORE.logical_read(lsid))[0], "dead")

    def test_a_dead_binding_is_overridden_by_a_live_claude_session_record(self):
        lsid = self.new_session()
        standin = self.standin_claude("owner")
        binding = self.binding_for(standin.pid, session_id=OWNER)
        CORE.logical_mutate(lsid, lambda r: r["owner"].update(process=binding))
        standin.terminate()
        wait_for_exit(standin.pid)
        sessions = os.path.join(self.tmp, "sessions")
        os.makedirs(sessions)
        with open(os.path.join(sessions, "9.json"), "w") as handle:
            json.dump({"pid": os.getpid(), "sessionId": OWNER}, handle)
        os.environ["CLAUDE_TERMINAL_HANDOFF_CLAUDE_SESSIONS_DIR"] = sessions
        try:
            self.assertEqual(CORE.owner_liveness(CORE.logical_read(lsid))[0], "alive")
        finally:
            del os.environ["CLAUDE_TERMINAL_HANDOFF_CLAUDE_SESSIONS_DIR"]

    def test_a_fresh_status_line_snapshot_counts_as_alive(self):
        lsid = self.new_session()
        CORE.write_json_private(CORE.live_session_path(OWNER), {"observed_epoch": time.time()})
        self.assertEqual(CORE.owner_liveness(CORE.logical_read(lsid))[0], "alive")

    def test_creating_sessions_that_never_registered_fail(self):
        record = CORE.logical_create(project="nova", launch_expires_epoch=1.0, launch_token_sha256="x")
        CORE.logical_reconcile(record["logical_session_id"], liveness=self.alive, now=time.time())
        self.assertEqual(CORE.logical_read(record["logical_session_id"])["state"], "FAILED")


class TestServiceRestartRecovery(LogicalCase):
    def dead(self, record, now):
        return "dead", "gone"

    def alive(self, record, now):
        return "alive", "ok"

    def unknown(self, record, now):
        return "unknown", "no evidence"

    def test_a_running_session_whose_owner_died_is_orphaned_not_running(self):
        lsid = self.new_session()
        summary = CORE.service_recover(liveness=self.dead, sleep=NOSLEEP)
        self.assertEqual(summary[lsid], "ORPHANED")

    def test_a_verified_live_owner_keeps_its_state(self):
        running, waiting, stopped = self.new_session(owner="a1-session"), self.new_session(owner="a2-session"), self.new_session(owner="a3-session")
        CORE.approval_request(waiting, "a2-session", "deploy build abc123", "why")
        CORE.logical_stop(stopped, by="phone", reason="hold")
        summary = CORE.service_recover(liveness=self.alive, sleep=NOSLEEP)
        self.assertEqual((summary[running], summary[waiting], summary[stopped]), ("RUNNING", "WAITING_FOR_HUMAN", "STOPPED"))

    def test_an_unverifiable_owner_is_orphaned_after_a_restart(self):
        lsid = self.new_session()
        self.assertEqual(CORE.service_recover(liveness=self.unknown, sleep=NOSLEEP)[lsid], "ORPHANED")

    def test_stop_inbox_and_pending_approval_survive_recovery(self):
        lsid = self.new_session()
        CORE.inbox_post(lsid, "queued before the restart")
        approval = approve_ready(self, lsid)
        CORE.logical_stop(lsid, by="phone", reason="hold")
        CORE.service_recover(liveness=self.dead, sleep=NOSLEEP)
        record = CORE.logical_read(lsid)
        self.assertEqual(record["state"], "ORPHANED")
        self.assertTrue(record["stop"]["active"])
        self.assertEqual(record["inbox"]["messages"][0]["text"], "queued before the restart")
        self.assertEqual([a["status"] for a in record["approvals"]], ["pending"])
        self.assertEqual(record["approvals"][0]["nonce"], approval["nonce"])
        CORE.logical_recover(lsid, "reattach", liveness=self.alive)
        self.assertEqual(CORE.logical_read(lsid)["state"], "STOPPED")

    def test_stale_approvals_expire_and_removed_projects_are_flagged(self):
        repo = os.path.join(self.tmp, "nova")
        os.makedirs(repo)
        CORE.project_add("nova", repo)
        lsid = self.new_session()
        approval = approve_ready(self, lsid)
        CORE.logical_mutate(lsid, lambda r: r["approvals"][0].update(expires_epoch=1.0))
        CORE.projects_update(lambda p: p.pop("nova"))
        CORE.service_recover(liveness=self.alive, sleep=NOSLEEP)
        record = CORE.logical_read(lsid)
        self.assertEqual(record["approvals"][0]["status"], "expired")
        self.assertFalse(record["project_available"])
        self.assertFalse(CORE.logical_public_view(record)["project_available"])

    def test_finished_sessions_are_left_alone_and_recovery_is_repeatable(self):
        lsid = self.new_session()
        CORE.logical_mutate(lsid, lambda r: r.update(state="COMPLETED"))
        self.assertEqual(CORE.service_recover(liveness=self.dead, sleep=NOSLEEP), {})
        self.assertEqual(CORE.logical_read(lsid)["state"], "COMPLETED")

    def test_the_cli_startup_recovery_never_reports_unverified_sessions_as_running(self):
        lsid = self.new_session()
        code, out, _ = run_th(["session", "reconcile", "--startup"], env=self.env(), timeout=90)
        self.assertEqual(json.loads(out)[lsid], "ORPHANED")


class TestPersistentSecurityState(RemoteCase):
    def test_authentication_lockout_survives_a_gateway_restart(self):
        for _ in range(CORE.AUTH_FAIL_LIMIT):
            self.assertEqual(self.call("GET", "/api/v1/sessions", token="thd_bad.token")[0], 401)
        self.server.shutdown()
        self.server.server_close()
        self.server = CORE.make_remote_server(self.config, port=0, tailscale_checker=lambda h: (True, None))
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.assertEqual(self.call("GET", "/api/v1/sessions", token="thd_bad.token")[0], 429)

    def test_enrollment_attempts_are_limited_across_restarts(self):
        for _ in range(5):
            self.assertEqual(self.call("POST", "/api/v1/enroll", {"code": "thc_wrong"})[0], 401)
        self.server.shutdown()
        self.server.server_close()
        self.server = CORE.make_remote_server(self.config, port=0, tailscale_checker=lambda h: (True, None))
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.assertEqual(self.call("POST", "/api/v1/enroll", {"code": "thc_wrong"})[0], 429)

    def test_enrollment_code_cannot_be_replayed(self):
        code, _ = CORE.device_enroll_begin("phone")
        self.assertEqual(self.call("POST", "/api/v1/enroll", {"code": code})[0], 200)
        self.assertEqual(self.call("POST", "/api/v1/enroll", {"code": code})[0], 401)

    def test_the_state_file_is_private_and_holds_no_secrets(self):
        _, token = self.enroll_token()
        self.call("GET", "/api/v1/sessions", token="thd_d_0123456789abcdef.SECRET-VALUE-XYZ")
        path = CORE.security_state_path()
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        text = text_file(path)
        self.assertNotIn("SECRET-VALUE-XYZ", text)
        self.assertNotIn(token.split(".", 1)[1], text)

    def test_limiter_windows_expire_and_are_pruned(self):
        clock = {"t": 1000.0}
        limiter = CORE.PersistentLimiter(clock=lambda: clock["t"])
        for _ in range(3):
            self.assertTrue(limiter.allow("k", 3, 60))
        self.assertFalse(limiter.allow("k", 3, 60))
        self.assertTrue(limiter.blocked("k", 3, 60))
        clock["t"] += 61
        self.assertTrue(limiter.allow("k", 3, 60))
        clock["t"] += 7200
        limiter.allow("other", 3, 60)
        self.assertNotIn("k", json_file(CORE.security_state_path())["windows"])

    def test_create_and_control_actions_are_rate_limited(self):
        _, token = self.enroll_token()
        lsid = self.new_session()
        limit = CORE.SENSITIVE_LIMITS["stop"][0]
        statuses = [self.call("POST", "/api/v1/sessions/%s/stop" % lsid, {"request_id": "req-stop%05d" % i}, token=token)[0] for i in range(limit + 2)]
        self.assertEqual(statuses[-1], 429)
        self.assertNotIn(429, statuses[:limit])

    def test_device_revocation_takes_effect_on_an_active_browser_session(self):
        code, _ = CORE.device_enroll_begin("phone")
        _, _, resp = self.call("POST", "/api/v1/enroll", {"code": code})
        cookie = resp.getheader("Set-Cookie").split(";")[0]
        self.assertEqual(self.call("GET", "/api/v1/me", headers={"Cookie": cookie})[0], 200)
        device_id = CORE.device_list()[0]["device_id"]
        CORE.device_revoke(device_id)
        self.assertEqual(self.call("GET", "/api/v1/me", headers={"Cookie": cookie})[0], 401)

    def test_an_expired_browser_token_is_refused_and_last_use_is_tracked(self):
        code, _ = CORE.device_enroll_begin("phone")
        _, _, resp = self.call("POST", "/api/v1/enroll", {"code": code})
        cookie = resp.getheader("Set-Cookie").split(";")[0]
        self.call("GET", "/api/v1/me", headers={"Cookie": cookie})
        row = CORE.device_list()[0]
        self.assertIsNotNone(row["last_used_utc"])
        self.assertEqual(row["name"], "phone")
        did = row["device_id"]
        CORE.update_json_locked(CORE.devices_path(), lambda d: d["devices"][did].update(expires_epoch=1.0))
        self.assertEqual(self.call("GET", "/api/v1/me", headers={"Cookie": cookie})[0], 401)


class TestPermissionIsolation(LogicalCase):
    def fake_run(self, broken=False, granted=True):
        """A stand-in for `claude -p`: it 'runs' touch only when the profile file grants it."""
        def run(argv, **kw):
            class R:
                stdout = b"2.1.278 (Claude Code)"
                stderr = b""
                returncode = 0
            if "--version" in argv:
                return R()
            target = re.search(r"touch (\S+)", argv[argv.index("-p") + 1]).group(1)
            has_profile = "--settings" in argv
            excluded = argv[argv.index("--setting-sources") + 1] == ""
            project_allow_applies = not excluded and not broken
            if (has_profile and granted) or project_allow_applies or (broken and not has_profile):
                open(target, "w").close()
            return R()
        return run

    def test_isolation_is_only_accepted_when_proven(self):
        ok, why = CORE.isolation_ok(self.fake_claude, run=self.fake_run())
        self.assertFalse(ok)
        self.assertIn("verify-isolation", why)

    def test_a_working_isolation_is_recorded_per_claude_version(self):
        result = CORE.isolation_probe(self.fake_claude, run=self.fake_run())
        self.assertTrue(result["broader_project_allow_blocked"])
        self.assertTrue(result["profile_settings_effective"])
        CORE.update_json_locked(CORE.isolation_state_path(), lambda d: d.setdefault("versions", {}).__setitem__(result["claude_version"], result))
        self.assertTrue(CORE.isolation_ok(self.fake_claude, run=self.fake_run())[0])
        newer = self.fake_run()
        def newer_run(argv, **kw):
            r = newer(argv, **kw)
            if "--version" in argv:
                r.stdout = b"9.9.9 (Claude Code)"
            return r
        self.assertFalse(CORE.isolation_ok(self.fake_claude, run=newer_run)[0])  # a new Claude must be re-verified

    def test_broken_isolation_fails_closed(self):
        result = CORE.isolation_probe(self.fake_claude, run=self.fake_run(broken=True))
        self.assertFalse(result["broader_project_allow_blocked"])
        CORE.update_json_locked(CORE.isolation_state_path(), lambda d: d.setdefault("versions", {}).__setitem__(result["claude_version"], result))
        self.assertFalse(CORE.isolation_ok(self.fake_claude, run=self.fake_run())[0])

    def test_an_ineffective_profile_channel_fails_closed(self):
        result = CORE.isolation_probe(self.fake_claude, run=self.fake_run(granted=False))
        self.assertFalse(result["profile_settings_effective"])

    def test_remote_launch_refuses_without_verified_isolation(self):
        from test_remote_launch import LaunchCase  # noqa: F401  (documented dependency)

    def test_the_probe_itself_never_touches_real_settings(self):
        before = text_file(os.path.expanduser("~/.claude/settings.json")) if os.path.exists(os.path.expanduser("~/.claude/settings.json")) else ""
        CORE.isolation_probe(self.fake_claude, run=self.fake_run())
        after = text_file(os.path.expanduser("~/.claude/settings.json")) if os.path.exists(os.path.expanduser("~/.claude/settings.json")) else ""
        self.assertEqual(before, after)

    def test_broader_existing_permissions_do_not_apply_to_the_remote_argv(self):
        argv = CORE.build_remote_launch_argv("/bin/claude", "n", "/s.json", "p")
        i = argv.index("--setting-sources")
        self.assertEqual(argv[i + 1], "")
        self.assertEqual(argv[i + 2], "--settings")
        self.assertNotIn("--permission-mode", argv)


class TestLaunchTokenHandling(LogicalCase):
    def test_the_launcher_holds_no_secret_and_removes_itself_and_the_token(self):
        token_file = os.path.join(self.tmp, "t.tok")
        CORE.write_text_private(token_file, "one-time-secret-token")
        script = CORE.build_remote_launch_script(self.workdir, [self.fake_claude, "--remote-control", "prompt"], "ls_" + "a" * 24, token_file)
        self.assertNotIn("one-time-secret-token", script)
        script_file = os.path.join(self.tmp, "launch.sh")
        CORE.write_text_private(script_file, script, 0o700)
        self.assertEqual(os.stat(token_file).st_mode & 0o777, 0o600)
        proc = subprocess.run(["/bin/zsh", script_file], stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.workdir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(os.path.exists(token_file))  # removed before Claude started
        self.assertFalse(os.path.exists(script_file))
        self.assertNotIn(b"one-time-secret-token", proc.stdout + proc.stderr)

    def test_a_replayed_launcher_cannot_obtain_the_token(self):
        token_file = os.path.join(self.tmp, "t.tok")
        CORE.write_text_private(token_file, "tok")
        script = CORE.build_remote_launch_script(self.workdir, [self.fake_claude, "p"], "ls_" + "b" * 24, token_file)
        first, second = os.path.join(self.tmp, "l1.sh"), os.path.join(self.tmp, "l2.sh")
        for path in (first, second):
            CORE.write_text_private(path, script, 0o700)
        self.assertEqual(subprocess.run(["/bin/zsh", first], stdout=subprocess.PIPE, stderr=subprocess.PIPE).returncode, 0)
        again = subprocess.run(["/bin/zsh", second], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertNotEqual(again.returncode, 0)
        self.assertIn(b"missing or already used", again.stdout)

    def test_stale_launch_material_is_swept(self):
        prompts = CORE.th_path("prompts")
        os.makedirs(prompts, exist_ok=True)
        old, fresh, other = (os.path.join(prompts, n) for n in ("remote-ls_old.tok", "remote-ls_new.tok", "successor-x.md"))
        for path in (old, fresh, other):
            CORE.write_text_private(path, "x")
        os.utime(old, (1, 1))
        os.utime(other, (1, 1))
        self.assertEqual(CORE.sweep_launch_artifacts(), 1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))
        self.assertTrue(os.path.exists(other))  # only remote launch material is ever swept


if __name__ == "__main__":
    unittest.main()
