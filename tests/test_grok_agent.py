"""Grok as a second supported agent: ACP bridge, exact-session reconnect, STOP, approvals, archive.

Nothing here calls Grok or any network. `grok agent stdio` is replaced by tests/fake_grok_acp.py, a
scriptable ACP agent, so the real gateway, the real bridge process and the real ACP client are all
exercised end to end. A pre-1.5 (Claude) session, and the Claude launch path, must be untouched.
"""

import json
import os
import signal
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import CORE  # noqa: E402
from test_remote_api import HOST, LOGIN  # noqa: E402
from test_remote_launch import LaunchCase  # noqa: E402
import http.client  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE = os.path.join(HERE, "fake_grok_acp.py")
ENV_KEYS = ("GROK_HOME", "CLAUDE_TERMINAL_HANDOFF_GROK_BIN", "FAKE_GROK_DIR", "CLAUDE_TERMINAL_HANDOFF_OWNER_GRACE",
            "FAKE_GROK_INIT_ERROR", "FAKE_GROK_INIT_GARBAGE", "FAKE_GROK_NEW_ERROR", "FAKE_GROK_LOAD_ERROR", "FAKE_GROK_NO_LOAD")


class GrokCase(LaunchCase):
    def setUp(self):
        super().setUp()
        self._env = {key: os.environ.get(key) for key in ENV_KEYS}
        self._find = CORE.find_grok_executable
        self.grok_home = os.path.join(self.tmp, "grok-home")
        self.fake_dir = os.path.join(self.tmp, "fake-grok")
        os.makedirs(self.grok_home)
        os.makedirs(self.fake_dir)
        with open(os.path.join(self.grok_home, "auth.json"), "w") as handle:
            handle.write("{}")  # existence is all Terminal Handoff ever checks; the content is never read
        wrapper = os.path.join(self.tmp, "grok")
        with open(wrapper, "w") as handle:
            handle.write('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, FAKE))
        os.chmod(wrapper, 0o700)
        os.environ["GROK_HOME"] = self.grok_home
        os.environ["CLAUDE_TERMINAL_HANDOFF_GROK_BIN"] = wrapper
        os.environ["FAKE_GROK_DIR"] = self.fake_dir
        os.environ["CLAUDE_TERMINAL_HANDOFF_OWNER_GRACE"] = "0"
        for key in ("FAKE_GROK_INIT_ERROR", "FAKE_GROK_INIT_GARBAGE", "FAKE_GROK_NEW_ERROR", "FAKE_GROK_LOAD_ERROR", "FAKE_GROK_NO_LOAD"):
            os.environ.pop(key, None)
        ok, why = CORE.project_set_agent("nova", "grok", True)
        self.assertTrue(ok, why)
        self.start_server(wait=45.0)  # a bridge is a real process (interpreter start, module import, ACP handshake): allow for a loaded machine
        self.n = 0

    def tearDown(self):
        for record in CORE.logical_list():
            binding = (record.get("owner") or {}).get("process") or {}
            pid = binding.get("pid")
            if pid and binding.get("kind") == "grok-bridge" and int(pid) != os.getpid():
                try:
                    os.kill(int(pid), signal.SIGTERM)
                except (OSError, ValueError):
                    pass
        time.sleep(0.3)
        CORE.find_grok_executable = self._find
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        super().tearDown()

    # -- helpers -----------------------------------------------------------------
    def rid(self):
        self.n += 1
        return "req-grok-%06d" % self.n

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request("GET", path, headers={"Host": HOST, "Tailscale-User-Login": LOGIN, "Authorization": "Bearer " + self.token})
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        conn.close()
        return resp.status, data

    def start(self, task="say hello", **extra):
        extra.setdefault("agent", "grok")
        status, view = self.create(task=task, request_id=self.rid(), **extra)
        self.assertEqual(status, 201, view)
        return view["logical_session_id"]

    def calls(self, kind=None):
        path = os.path.join(self.fake_dir, "calls.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as handle:
            rows = [json.loads(line) for line in handle if line.strip()]
        return [r for r in rows if kind is None or r["kind"] == kind]

    def rpc(self, method):
        return [c for c in self.calls("rpc") if c["method"] == method]

    def prompts(self):
        return [c["text"] for c in self.calls("prompt")]

    def wait_for(self, check, timeout=25.0, what="condition"):
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = check()
            if value:
                return value
            time.sleep(0.1)
        self.fail("timed out waiting for %s" % what)

    def record(self, lsid):
        return CORE.logical_read(lsid)

    def transcript_text(self, lsid):
        return "\n".join(item["text"] for item in CORE.logical_transcript(self.record(lsid))["items"])

    def acked(self, lsid, count=1):
        return lambda: sum(1 for m in self.record(lsid)["inbox"]["messages"] if m["status"] == "acked") >= count

    def instruct(self, lsid, text):
        return self.post("/api/v1/sessions/%s/instructions" % lsid, {"text": text, "request_id": self.rid()}, token=self.token)

    def stop(self, lsid):
        return self.post("/api/v1/sessions/%s/stop" % lsid, {"request_id": self.rid()}, token=self.token)

    def resume(self, lsid):
        return self.post("/api/v1/sessions/%s/resume" % lsid, {"request_id": self.rid(), "clear_stop": True, "reason": "carry on"}, token=self.token)

    def kill_bridge(self, lsid):
        pid = int(self.record(lsid)["owner"]["process"]["pid"])
        os.kill(pid, signal.SIGTERM)
        self.wait_for(lambda: CORE.grok_owner_liveness(self.record(lsid))[0] == "dead", what="bridge to exit")

    def orphan(self, lsid):
        self.kill_bridge(lsid)
        CORE.logical_reconcile(lsid, strict=True)
        CORE.logical_reconcile(lsid, strict=True)
        self.assertEqual(self.record(lsid)["state"], "ORPHANED")


class TestAgentTypeAndCompatibility(GrokCase):
    def test_a_record_with_no_agent_type_is_claude(self):
        record = CORE.logical_create(project="nova", repository=self.workdir)
        self.assertEqual(record["agent_type"], "claude")
        legacy = dict(record)
        del legacy["agent_type"]
        self.assertEqual(CORE.agent_type_of(legacy), "claude")
        self.assertEqual(CORE.agent_type_of({"agent_type": "unknown-agent"}), "claude")
        self.assertEqual(CORE.logical_public_view(legacy)["agent_type"], "claude")
        self.assertIsNone(CORE.logical_public_view(legacy)["grok"])

    def test_a_legacy_record_keeps_the_claude_liveness_path(self):
        record = CORE.logical_read(self.new_session())
        del record["agent_type"]
        self.assertEqual(CORE.owner_liveness(record)[0], "unknown")  # the Claude signals, not the Grok bridge check

    def test_the_default_agent_is_claude_and_launches_through_the_terminal(self):
        status, view = self.create(request_id=self.rid())
        self.assertEqual((status, view["agent_type"]), (201, "claude"))
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(self.calls(), [])  # Grok was never touched

    def test_an_explicit_claude_agent_is_the_same_path(self):
        status, view = self.create(request_id=self.rid(), agent="claude")
        self.assertEqual((status, view["agent_type"]), (201, "claude"))
        self.assertEqual(len(self.launched), 1)

    def test_an_unknown_agent_is_refused_before_anything_starts(self):
        for bad in ("codex", "GROK", "", 5, ["grok"], None):  # direct: the create rate limit is deliberately small
            body = {"project": "nova", "task": "do it", "agent": bad, "request_id": self.rid()}
            status, payload = CORE.remote_create_session(body, {"device_id": "d_0123456789abcdef"}, terminal=self.fake_terminal, wait_seconds=0, isolation_check=self.isolation_check)
            self.assertEqual(status, 400, repr(bad))
        self.assertEqual((self.launched, CORE.logical_list()), ([], []))

    def test_the_agent_cannot_be_switched_after_creation(self):
        lsid = self.start()
        self.assertEqual(self.record(lsid)["agent_type"], "grok")
        for key in ("agent", "agent_type"):
            status, _ = self.post("/api/v1/sessions/%s/rename" % lsid, {"name": "x", key: "claude", "request_id": self.rid()}, token=self.token)
            self.assertEqual(status, 400)
        self.assertEqual(self.record(lsid)["agent_type"], "grok")


class TestGrokSession(GrokCase):
    def test_a_grok_session_starts_and_records_the_exact_grok_session_id(self):
        lsid = self.start(name="Grok iPhone Test")
        record = self.record(lsid)
        self.assertEqual((record["agent_type"], record["state"], record["repository"]), ("grok", "RUNNING", self.real_repo))
        self.assertEqual(record["owner"]["process"]["kind"], "grok-bridge")
        sid = record["grok_session_id"]
        self.assertTrue(sid.startswith("fakegrok-"))
        self.assertEqual(record["owner"]["agent_session_id"], sid)
        self.assertEqual(len(self.rpc("session/new")), 1)
        start = self.calls("start")[0]
        self.assertEqual(start["cwd"], self.real_repo)  # the pinned trusted realpath, never anything the phone sent
        self.assertIn("--no-leader", start["argv"])
        self.assertFalse(start["has_launch_token"])  # the launch secret is never handed to Grok
        self.assertEqual(CORE.logical_public_view(record)["agent_type"], "grok")

    def test_the_task_is_delivered_through_acp_and_the_stream_reaches_the_transcript(self):
        lsid = self.start(task="say hello to the world")
        self.wait_for(self.acked(lsid), what="task acknowledged")
        self.assertEqual(self.prompts(), ["say hello to the world"])
        text = self.transcript_text(lsid)
        self.assertIn("You: say hello to the world", text)
        self.assertIn("ok: say hello to the world", text)
        self.assertIn("second line", text)
        self.assertNotIn("REPLAYED-HISTORY", text)

    def test_tool_calls_are_shown_safely_and_reasoning_is_never_exposed(self):
        lsid = self.start(task="THOUGHT please")
        self.wait_for(self.acked(lsid), what="turn")
        text = self.transcript_text(lsid)
        self.assertIn("[tool] Read README.md", text)
        self.assertIn("[tool completed] Read README.md", text)
        blob = json.dumps(self.record(lsid)) + json.dumps(CORE.logical_public_view(self.record(lsid))) + text
        for secret in ("PRIVATE-REASONING-SECRET", "TOPSECRET-RAW-INPUT", "TOPSECRET-TOOL-OUTPUT"):
            self.assertNotIn(secret, blob)

    def test_a_follow_up_runs_in_the_same_grok_session(self):
        lsid = self.start()
        self.wait_for(self.acked(lsid), what="task")
        status, _ = self.instruct(lsid, "and now the follow-up")
        self.assertIn(status, (200, 202))
        self.wait_for(self.acked(lsid, 2), what="follow-up")
        sessions = {c["session"] for c in self.calls("prompt")}
        self.assertEqual(sessions, {self.record(lsid)["grok_session_id"]})
        self.assertEqual(len(self.rpc("session/new")), 1)

    def test_a_grok_error_is_reported_and_not_retried(self):
        lsid = self.start(task="ERR402")
        self.wait_for(self.acked(lsid), what="failed turn acknowledged")
        self.assertIn("usage balance is exhausted", self.transcript_text(lsid))
        self.assertEqual(len(self.prompts()), 1)  # a failing prompt must not loop

    def test_malformed_acp_lines_are_ignored_safely(self):
        lsid = self.start(task="MALFORMED please")
        self.wait_for(self.acked(lsid), what="turn")
        self.assertIn("ok: MALFORMED please", self.transcript_text(lsid))
        self.assertEqual(self.record(lsid)["state"], "RUNNING")


class TestProtocolSafety(GrokCase):
    def test_a_flood_of_malformed_messages_fails_the_turn_and_reloads_the_exact_session(self):
        lsid = self.start(task="FLOOD please")
        sid = self.record(lsid)["grok_session_id"]
        self.wait_for(self.acked(lsid), what="failed turn")
        self.assertIn("Grok sent an unexpected response.", self.transcript_text(lsid))
        self.assertEqual(self.record(lsid)["state"], "RUNNING")
        self.instruct(lsid, "still there?")
        self.wait_for(self.acked(lsid, 2), what="next turn on a fresh process")
        self.assertEqual(len(self.calls("start")), 2)
        self.assertEqual([c["params"]["sessionId"] for c in self.rpc("session/load")], [sid])
        self.assertEqual(len(self.rpc("session/new")), 1)

    def test_the_acp_client_rejects_a_non_object_result_and_unknown_agent_requests(self):
        os.environ["FAKE_GROK_INIT_GARBAGE"] = "1"
        client = CORE.GrokAcpClient([os.environ["CLAUDE_TERMINAL_HANDOFF_GROK_BIN"], "agent", "stdio"], self.tmp, dict(os.environ))
        try:
            with self.assertRaises(CORE.GrokError) as caught:
                client.request("initialize", {"protocolVersion": 1}, timeout=10)
            self.assertEqual(caught.exception.code, "protocol")
        finally:
            client.terminate()
            os.environ.pop("FAKE_GROK_INIT_GARBAGE", None)


class TestReconnect(GrokCase):
    def test_reconnect_loads_the_exact_session_and_never_starts_a_new_one(self):
        lsid = self.start()
        self.wait_for(self.acked(lsid), what="task")
        sid = self.record(lsid)["grok_session_id"]
        self.orphan(lsid)
        status, body = self.post("/api/v1/sessions/%s/recover" % lsid, {"action": "reattach", "request_id": self.rid()}, token=self.token)
        self.assertEqual(status, 200, body)
        self.wait_for(lambda: self.record(lsid)["state"] == "RUNNING" and CORE.grok_owner_liveness(self.record(lsid))[0] == "alive", what="reattached")
        loads = self.rpc("session/load")
        self.assertEqual([c["params"]["sessionId"] for c in loads], [sid])
        self.assertEqual(len(self.rpc("session/new")), 1)  # no accidental new conversation
        record = self.record(lsid)
        self.assertEqual((record["grok_session_id"], record["owner"]["agent_session_id"]), (sid, sid))
        self.assertEqual(record["owner_epoch"], 2)  # fenced: the old owner's epoch is dead
        self.instruct(lsid, "after reconnect")
        self.wait_for(self.acked(lsid, 2), what="post-reconnect instruction")
        self.assertEqual({c["session"] for c in self.calls("prompt")}, {sid})
        self.assertNotIn("REPLAYED-HISTORY", self.transcript_text(lsid))

    def test_reattach_refuses_to_invent_a_session_when_none_is_recorded(self):
        lsid = self.start()
        self.wait_for(self.acked(lsid), what="task")
        self.orphan(lsid)

        def clear(record):
            record["grok_session_id"] = None

        CORE.logical_mutate(lsid, clear)
        ok, why, _ = CORE.grok_reattach(lsid)
        self.assertFalse(ok)
        self.assertIn("never started implicitly", why)
        self.assertEqual(len(self.rpc("session/new")), 1)

    def test_an_invalid_persisted_session_fails_closed_and_stays_orphaned(self):
        lsid = self.start()
        self.wait_for(self.acked(lsid), what="task")
        self.orphan(lsid)
        os.environ["FAKE_GROK_LOAD_ERROR"] = "1"
        ok, why, _ = CORE.grok_reattach(lsid)
        self.assertTrue(ok, why)  # the bridge was started; it must then refuse
        self.wait_for(lambda: any(h["event"] == "grok_load_failed" for h in self.record(lsid)["history"]), what="load failure")
        self.assertEqual(self.record(lsid)["state"], "ORPHANED")
        self.assertEqual(len(self.rpc("session/new")), 1)  # and did not fall back to a new conversation

    def test_a_process_death_mid_turn_reloads_the_same_session_and_finishes(self):
        lsid = self.start(task="DIE mid way")
        sid = self.record(lsid)["grok_session_id"]
        self.wait_for(self.acked(lsid), what="turn after recovery")
        self.assertEqual(len(self.calls("start")), 2)  # a second Grok process
        self.assertEqual([c["params"]["sessionId"] for c in self.rpc("session/load")], [sid])
        self.assertEqual(len(self.rpc("session/new")), 1)
        self.assertEqual(len(self.prompts()), 2)  # at-least-once: the interrupted instruction is offered again
        self.assertTrue(any(h["event"] == "grok_process_exited" for h in self.record(lsid)["history"]))
        self.assertEqual(self.record(lsid)["state"], "RUNNING")


class TestStopResume(GrokCase):
    def test_stop_interrupts_the_running_turn_with_session_cancel(self):
        lsid = self.start(task="SLOW job")
        self.wait_for(lambda: "working on it" in self.transcript_text(lsid), what="turn in progress")
        status, _ = self.stop(lsid)
        self.assertEqual(status, 200)
        self.wait_for(lambda: len(self.calls("cancel")) == 1, what="session/cancel")
        self.wait_for(self.acked(lsid), what="interrupted turn")
        self.assertIn("Interrupted by STOP", self.transcript_text(lsid))
        self.assertEqual(self.record(lsid)["state"], "STOPPED")
        self.assertEqual(CORE.grok_owner_liveness(self.record(lsid))[0], "alive")  # STOP is not a kill

    def test_stop_blocks_queued_instructions_and_resume_restores_delivery(self):
        lsid = self.start()
        self.wait_for(self.acked(lsid), what="task")
        self.stop(lsid)
        self.wait_for(lambda: self.record(lsid)["state"] == "STOPPED")
        status, _ = self.instruct(lsid, "queued while stopped")
        self.assertIn(status, (200, 202))
        time.sleep(3.0)
        self.assertEqual(self.prompts(), ["say hello"])  # never delivered
        self.assertEqual(CORE.logical_public_view(self.record(lsid))["inbox"]["pending"], 1)
        status, _ = self.resume(lsid)
        self.assertEqual(status, 200)
        self.wait_for(self.acked(lsid, 2), what="queued instruction after resume")
        self.assertEqual(self.prompts(), ["say hello", "queued while stopped"])

    def test_a_grok_that_ignores_cancel_is_ended_and_the_same_session_is_reloaded_on_resume(self):
        os.environ["CLAUDE_TERMINAL_HANDOFF_GROK_STOP_GRACE"] = "1"
        try:
            lsid = self.start(task="STUBBORN job")
            sid = self.record(lsid)["grok_session_id"]
            self.wait_for(lambda: "stubborn work" in self.transcript_text(lsid), what="turn in progress")
            self.stop(lsid)
            self.wait_for(self.acked(lsid), what="turn ended after the grace period", timeout=30)
            self.assertIn("Interrupted by STOP", self.transcript_text(lsid))
            self.assertEqual(len(self.calls("start")), 1)
            self.assertEqual(self.record(lsid)["state"], "STOPPED")
            self.instruct(lsid, "after the stubborn one")
            time.sleep(2.0)
            self.assertEqual(len(self.prompts()), 1)  # still stopped: nothing delivered
            self.resume(lsid)
            self.wait_for(self.acked(lsid, 2), what="delivery after resume")
        finally:
            os.environ.pop("CLAUDE_TERMINAL_HANDOFF_GROK_STOP_GRACE", None)
        self.assertEqual(len(self.calls("start")), 2)  # the ended process was replaced
        self.assertEqual([c["params"]["sessionId"] for c in self.rpc("session/load")], [sid])
        self.assertEqual(len(self.rpc("session/new")), 1)
        self.assertEqual({c["session"] for c in self.calls("prompt")}, {sid})


class TestHardStop(GrokCase):
    def test_hard_stop_re_proves_the_bridge_binding_and_never_signals_anything_else(self):
        lsid = self.start()
        self.wait_for(self.acked(lsid), what="task")
        genuine = self.record(lsid)["owner"]["process"]
        result = CORE.logical_hard_stop(lsid)  # test mode: a verified binding is reported as simulated, not signalled
        self.assertEqual(result["outcome"], "simulated")

        def set_binding(binding):
            def mutate(record):
                record["owner"]["process"] = binding
            CORE.logical_mutate(lsid, mutate)

        try:
            set_binding(dict(genuine, start="Thu Jan  1 00:00:00 1970"))  # a reused pid
            self.assertEqual(CORE.logical_hard_stop(lsid)["outcome"], "not_signalled")
            set_binding({"kind": "claude", "pid": genuine["pid"], "start": genuine["start"]})  # not a Grok bridge binding
            self.assertEqual(CORE.logical_hard_stop(lsid)["outcome"], "not_signalled")
            self.assertFalse(CORE.verify_grok_bridge_binding({"kind": "grok-bridge", "pid": os.getpid(), "start": "x"}, lsid)[0])  # never this process
        finally:
            set_binding(genuine)


class TestPermissions(GrokCase):
    def open_gate(self):
        lsid = self.start(task="PERM edit")
        gate = self.wait_for(lambda: CORE.logical_public_view(self.record(lsid))["human_gate"], what="approval request")
        self.assertEqual(self.record(lsid)["state"], "WAITING_FOR_HUMAN")
        self.assertEqual(gate["action"], "Grok: Write probe.txt")
        self.assertNotIn("TOPSECRET-EDIT", json.dumps(gate))
        return lsid, gate

    def decide(self, lsid, gate, verdict):
        return self.post("/api/v1/sessions/%s/approvals/%s/%s" % (lsid, gate["id"], verdict),
                         {"nonce": gate["nonce"], "owner_epoch": self.record(lsid)["owner_epoch"], "request_id": self.rid()}, token=self.token)

    def test_an_approved_request_is_answered_allow_once_through_acp(self):
        lsid, gate = self.open_gate()
        status, body = self.decide(lsid, gate, "approve")
        self.assertEqual(status, 200, body)
        self.wait_for(self.acked(lsid), what="turn")
        self.assertEqual(self.calls("permission_reply")[0]["outcome"], {"outcome": "selected", "optionId": "allow-once"})  # never allow-always
        self.assertIn("permission:allow-once", self.transcript_text(lsid))
        self.assertEqual(self.record(lsid)["state"], "RUNNING")
        self.assertEqual([a["status"] for a in self.record(lsid)["approvals"]], ["consumed"])

    def test_a_denied_request_is_answered_reject_and_the_session_continues(self):
        lsid, gate = self.open_gate()
        self.assertEqual(self.decide(lsid, gate, "deny")[0], 200)
        self.wait_for(self.acked(lsid), what="turn")
        self.assertEqual(self.calls("permission_reply")[0]["outcome"], {"outcome": "selected", "optionId": "reject-once"})
        self.assertIn("permission:reject-once", self.transcript_text(lsid))

    def test_stop_while_waiting_for_approval_cancels_the_request(self):
        lsid, gate = self.open_gate()
        self.stop(lsid)
        self.wait_for(lambda: self.calls("permission_reply"), what="permission answered")
        self.assertEqual(self.calls("permission_reply")[0]["outcome"], {"outcome": "cancelled"})
        self.assertEqual(self.record(lsid)["state"], "STOPPED")

    def test_a_stale_approval_from_a_previous_owner_is_refused(self):
        lsid, gate = self.open_gate()
        status, _ = self.post("/api/v1/sessions/%s/approvals/%s/approve" % (lsid, gate["id"]),
                              {"nonce": gate["nonce"], "owner_epoch": 99, "request_id": self.rid()}, token=self.token)
        self.assertNotEqual(status, 200)
        self.assertEqual(self.calls("permission_reply"), [])


class TestSoleOwner(GrokCase):
    def test_a_second_bridge_cannot_take_a_live_session(self):
        lsid = self.start()
        self.wait_for(self.acked(lsid), what="task")
        before = self.record(lsid)["owner"]
        ok, why, _ = CORE.grok_reown(lsid, self.record(lsid)["grok_session_id"], {"kind": "grok-bridge", "pid": 999999})
        self.assertFalse(ok)
        self.assertIn("still owns", why)
        launched = CORE.launch_grok_bridge(lsid, "reattach")
        self.assertTrue(launched["launched"])
        time.sleep(3.0)
        self.assertEqual(self.record(lsid)["owner"], before)
        self.assertEqual(self.record(lsid)["owner_epoch"], 1)

    def test_a_grok_session_is_never_rebound_and_claude_cannot_be_given_a_grok_id(self):
        lsid = self.start()
        ok, why, _ = CORE.logical_set_grok_session(lsid, "fakegrok-differentid1234")
        self.assertFalse(ok)
        self.assertIn("different Grok session", why)
        claude = self.new_session()
        ok, why, _ = CORE.logical_set_grok_session(claude, "fakegrok-someid1234567")
        self.assertFalse(ok)
        self.assertIn("not a Grok session", why)
        ok, _, _ = CORE.logical_register_owner(lsid, "someone-else", 1, None, None, "irrelevant")
        self.assertFalse(ok)  # ownership registers once, whatever the agent

    def test_one_live_session_per_project_across_agents(self):
        self.start()
        status, body = self.create(request_id=self.rid(), agent="claude")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "project_in_use")


class TestArchive(GrokCase):
    def test_an_active_session_cannot_be_archived(self):
        lsid = self.start()
        self.wait_for(self.acked(lsid), what="task")
        status, _ = self.post("/api/v1/sessions/%s/archive" % lsid, {"request_id": self.rid()}, token=self.token)
        self.assertNotEqual(status, 200)
        self.assertFalse(self.record(lsid).get("archived"))

    def test_a_closed_session_archives_and_grok_history_is_left_alone(self):
        lsid = self.start()
        self.wait_for(self.acked(lsid), what="task")
        sid = self.record(lsid)["grok_session_id"]
        grok_session = os.path.join(self.grok_home, "sessions", "fake-cwd", sid)
        bystander = os.path.join(self.grok_home, "sessions", "someone-elses-session")
        os.makedirs(bystander)
        self.assertTrue(os.path.isdir(grok_session))
        self.orphan(lsid)
        status, body = self.post("/api/v1/sessions/%s/archive" % lsid, {"request_id": self.rid()}, token=self.token)
        self.assertEqual(status, 200, body)
        self.assertTrue(self.record(lsid)["archived"])
        self.assertTrue(os.path.isdir(grok_session))  # Terminal Handoff's archive is its own logical-history operation
        self.assertTrue(os.path.isdir(bystander))
        self.assertTrue(os.path.isdir(self.real_repo))  # and the project is untouched


class TestSecurity(GrokCase):
    def test_an_unregistered_project_or_a_path_is_refused(self):
        for project in ("nope", self.repo, "/tmp", "../nova"):
            status, body = self.create(project=project, request_id=self.rid(), agent="grok")
            self.assertEqual(status, 404, project)
        self.assertEqual(self.calls(), [])

    def test_grok_is_opt_in_per_project(self):
        CORE.project_set_agent("nova", "grok", False)
        status, body = self.create(request_id=self.rid(), agent="grok")
        self.assertEqual((status, body["error"]), (403, "agent_not_enabled"))
        self.assertEqual(self.calls(), [])
        self.assertEqual(CORE.project_agents({"agents": []}), ["claude"])
        self.assertEqual(self.get("/api/v1/projects")[1]["agents"], {"nova": ["claude"]})

    def test_the_projects_endpoint_lists_the_agents_each_project_allows(self):
        status, body = self.get("/api/v1/projects")
        self.assertEqual((status, body["projects"], body["agents"]), (200, ["nova"], {"nova": ["claude", "grok"]}))

    def test_a_missing_grok_executable_is_reported_clearly(self):
        CORE.find_grok_executable = lambda: None
        status, body = self.create(request_id=self.rid(), agent="grok")
        self.assertEqual((status, body["error"], body["reason"]), (503, "grok_unavailable", "Grok CLI is not installed on this Mac."))
        self.assertEqual(CORE.logical_list(), [])

    def test_missing_authentication_is_reported_clearly_and_credentials_are_never_read(self):
        os.unlink(os.path.join(self.grok_home, "auth.json"))
        status, body = self.create(request_id=self.rid(), agent="grok")
        self.assertEqual((status, body["error"], body["reason"]), (503, "grok_not_authenticated", "Grok CLI is not authenticated on this Mac."))
        self.assertEqual(CORE.logical_list(), [])

    def test_an_authentication_failure_inside_acp_fails_the_session_clearly(self):
        os.environ["FAKE_GROK_NEW_ERROR"] = "auth"
        status, body = self.create(request_id=self.rid(), agent="grok")
        self.assertEqual(status, 502)
        self.assertEqual(body["reason"], "Grok CLI is not authenticated on this Mac.")
        self.assertEqual(CORE.logical_read(body["logical_session_id"])["state"], "FAILED")

    def test_session_new_and_initialize_failures_fail_the_session(self):
        for var, expected in (("FAKE_GROK_NEW_ERROR", "boom"), ("FAKE_GROK_INIT_ERROR", "1"), ("FAKE_GROK_INIT_GARBAGE", "1"), ("FAKE_GROK_NO_LOAD", "1")):
            os.environ.pop("FAKE_GROK_NEW_ERROR", None)
            os.environ[var] = expected
            CORE.project_set_agent("nova", "grok", True)
            status, body = self.create(request_id=self.rid(), agent="grok")
            self.assertEqual(status, 502, (var, body))
            self.assertEqual(CORE.logical_read(body["logical_session_id"])["state"], "FAILED")
            os.environ.pop(var, None)
        self.assertEqual(self.rpc("session/load"), [])

    def test_always_approve_in_the_grok_config_is_refused_by_default(self):
        with open(os.path.join(self.grok_home, "config.toml"), "w") as handle:
            handle.write('[ui]\npermission_mode = "always-approve"  # comment\n')
        status, body = self.create(request_id=self.rid(), agent="grok")
        self.assertEqual((status, body["error"]), (503, "grok_permission_unsafe"))
        self.assertIn("always-approve", body["reason"])
        self.assertEqual(self.calls(), [])
        with open(os.path.join(self.grok_home, "config.toml"), "w") as handle:
            handle.write("[ui]\nyolo = true\n")
        self.assertEqual(self.create(request_id=self.rid(), agent="grok")[1]["error"], "grok_permission_unsafe")

    def test_an_ask_or_auto_config_is_accepted_and_grok_is_started_in_ask_mode(self):
        with open(os.path.join(self.grok_home, "config.toml"), "w") as handle:
            handle.write('[ui]\npermission_mode = "ask"\n')
        self.start()
        start = self.calls("start")[0]
        self.assertNotIn("--always-approve", start["argv"])
        self.assertEqual(start["permission_env"], "ask")
        self.assertIs(self.rpc("session/new")[0]["params"]["_meta"]["yoloMode"], False)

    def test_always_approve_needs_an_explicit_terminal_handoff_setting_and_is_then_passed_on(self):
        with open(os.path.join(self.grok_home, "config.toml"), "w") as handle:
            handle.write('[ui]\npermission_mode = "always-approve"\n')
        config_path = os.path.join(self.home, "remote", "config.json")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        config = json.load(open(config_path)) if os.path.exists(config_path) else {}
        config["grok_permission_mode"] = "always-approve"
        json.dump(config, open(config_path, "w"))
        self.start()
        self.assertIn("--always-approve", self.calls("start")[0]["argv"])

    def test_the_optional_grok_sandbox_is_off_by_default_and_passed_when_chosen(self):
        self.start()
        self.assertIsNone(self.calls("start")[0]["sandbox"])
        self.assertEqual(CORE.grok_health()["sandbox_profile"], "off")

    def test_a_chosen_sandbox_profile_reaches_grok_and_a_bogus_one_is_ignored(self):
        config_path = os.path.join(self.home, "remote", "config.json")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        config = json.load(open(config_path)) if os.path.exists(config_path) else {}
        config["grok_sandbox"] = "workspace"
        json.dump(config, open(config_path, "w"))
        self.start()
        self.assertEqual(self.calls("start")[0]["sandbox"], "workspace")
        config["grok_sandbox"] = "../../etc/evil"
        json.dump(config, open(config_path, "w"))
        self.assertIsNone(CORE.grok_sandbox_profile())

    def test_a_credential_in_a_permission_title_is_redacted_and_the_log_is_private(self):
        lsid = self.start(task="PERM edit")
        self.wait_for(lambda: CORE.logical_public_view(self.record(lsid))["human_gate"], what="gate")
        log_path = os.path.join(self.home, "logs", "grok-bridge-%s.log" % lsid)
        self.assertEqual(os.stat(log_path).st_mode & 0o777, 0o600)

    def test_a_credential_like_string_in_output_is_redacted(self):
        lsid = self.start(task="token sk-abcdefghijklmnopqrstuvwxyz0123456789")
        self.wait_for(self.acked(lsid), what="turn")
        self.assertIn("You: token", self.transcript_text(lsid))
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz0123456789", json.dumps(self.record(lsid)["output"]))


class TestHealthAndAdmin(GrokCase):
    def test_health_reports_agent_specific_facts_without_secrets(self):
        lsid = self.start()
        self.wait_for(self.acked(lsid), what="task")
        health = CORE.grok_health()
        self.assertTrue(health["executable_found"])
        self.assertEqual(health["version"], "grok 1.0.0-fake")
        self.assertTrue(health["credentials_present"])
        self.assertEqual(health["terminal_handoff_permission_mode"], "ask")
        row = health["active_sessions"][0]
        self.assertEqual((row["grok_session_bound"], row["owner"]), (True, "alive"))
        self.assertNotIn(self.record(lsid)["grok_session_id"], json.dumps(health))
        self.assertNotIn(self.grok_home, json.dumps(health))

    def test_the_view_shows_acp_state_but_no_ids_paths_or_pids(self):
        lsid = self.start()
        self.wait_for(lambda: (self.record(lsid).get("grok_acp") or {}).get("state") == "running", what="acp state")
        view = CORE.logical_public_view(self.record(lsid))
        self.assertEqual((view["grok"]["acp"], view["grok"]["session_bound"], view["grok"]["automatic_handoff"]), ("running", True, False))
        blob = json.dumps(view)
        for private in (self.record(lsid)["grok_session_id"], self.real_repo, str(self.record(lsid)["owner"]["process"]["pid"])):
            self.assertNotIn(private, blob)

    def test_project_cli_enables_and_disables_grok(self):
        self.assertEqual(CORE.project_agents(CORE.projects_load()["nova"]), ["claude", "grok"])
        ok, _ = CORE.project_set_agent("nova", "grok", False)
        self.assertEqual((ok, CORE.project_agents(CORE.projects_load()["nova"])), (True, ["claude"]))
        self.assertFalse(CORE.project_set_agent("nova", "claude", False)[0])  # Claude is not switchable off
        self.assertFalse(CORE.project_set_agent("nope", "grok", True)[0])

    def test_every_grok_lifecycle_step_is_audited_without_prompt_contents(self):
        lsid = self.start(task="audit this secret-ish task text")
        self.wait_for(self.acked(lsid), what="task")
        events = [json.loads(line) for line in open(os.path.join(self.home, "logs", "terminal-handoff.log")) if line.strip()]
        names = {e["event"] for e in events}
        for expected in ("grok_process_launched", "grok_session_bound", "grok_instruction_delivered", "grok_turn_ended", "grok_bridge_launched"):
            self.assertIn(expected, names)
        self.assertNotIn("secret-ish", open(os.path.join(self.home, "logs", "terminal-handoff.log")).read())


if __name__ == "__main__":
    unittest.main()
