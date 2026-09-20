"""End-to-end local acceptance: the full remote-session lifecycle through the real
gateway, real status-line hooks, a real handoff transfer and real process signals.

    create -> register owner -> instruction -> wake -> claim -> handoff ->
    successor owns -> second instruction -> human gate -> remote approval ->
    consume once -> STOP -> mutation refused -> deliberate resume -> owner dies
    -> ORPHANED

No Terminal window, real Claude session, network or Tailscale is involved.
"""

import json
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import CORE, REAL_MODEL_ID, json_file, process_alive, run_th, wait_for, wait_for_exit  # noqa: E402
from test_remote_launch import AGENT, LaunchCase  # noqa: E402
from test_transfer import TransferTestCase  # noqa: E402

SUCCESSOR_AGENT = "successor-agent-session-0002"
NOSLEEP = lambda s: time.sleep(0.02)  # noqa: E731


class TestFullLifecycle(LaunchCase):
    bind_transfer_to = TransferTestCase.bind_transfer_to
    successor_env = TransferTestCase.successor_env
    beat = TransferTestCase.beat
    supervise = TransferTestCase.supervise

    def api(self, method, path, body=None):
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        headers = {"Host": "mac-test.tail1234.ts.net", "Tailscale-User-Login": "john@example.com", "Authorization": "Bearer " + self.token}
        if method == "POST":
            headers.update({"Origin": "https://mac-test.tail1234.ts.net", "Content-Type": "application/json"})
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        conn.close()
        return resp.status, data

    def act(self, lsid, action, body=None):
        self.n = getattr(self, "n", 0) + 1
        return self.api("POST", "/api/v1/sessions/%s/%s" % (lsid, action), dict(body or {}, request_id="req-accept-%06d" % self.n))

    def sessions_env(self, **extra):
        return dict(CLAUDE_TERMINAL_HANDOFF_CLAUDE_SESSIONS_DIR=self.sessions_dir, **extra)

    def write_bridge(self, session_id, pid=None):
        with open(os.path.join(self.sessions_dir, "%s.json" % session_id), "w") as handle:
            json.dump({"pid": pid or os.getpid(), "sessionId": session_id, "bridgeSessionId": "b"}, handle)

    def test_the_whole_lifecycle(self):
        # 1. create from the "phone": success only once the Mac has registered the owner
        self.write_bridge(AGENT)
        status, created = self.api("POST", "/api/v1/sessions", {"project": "nova", "task": "Audit the retrieval pipeline. Do not deploy.", "request_id": "req-accept-create"})
        self.assertEqual((status, created["state"]), (201, "RUNNING"), created)
        lsid = created["logical_session_id"]
        self.assertEqual(created["owner"]["generation"], 1)
        self.assertEqual(created["remote_control"]["state"], "healthy")

        # 2-3. an instruction from the phone wakes the owner, which claims the task and the instruction in order
        woke = {}
        waiter = threading.Thread(target=lambda: woke.update(CORE.logical_wait(lsid, AGENT, 15, sleep=NOSLEEP)))
        self.assertEqual(self.act(lsid, "instructions", {"text": "Start with the tests."})[0], 202)
        waiter.start(); waiter.join(20)
        self.assertEqual(woke["directive"], "INSTRUCTIONS")
        self.assertEqual([m["text"] for m in woke["messages"]], ["Audit the retrieval pipeline. Do not deploy.", "Start with the tests."])
        for message in woke["messages"]:
            self.assertTrue(CORE.inbox_ack(lsid, AGENT, message["id"])[0])

        # 4. the owner nears its threshold: a real handoff is triggered and a real transfer is created
        standin = self.standin_claude("parent")
        parent = self.payload(percent=90.0, session_id=AGENT, workdir=self.real_repo, session_name="Nova")
        trigger_env = self.env(CLAUDE_TERMINAL_HANDOFF_LOGICAL_SESSION=lsid, **self.sessions_env())
        code, _, err = self.statusline(parent, trigger_env)
        self.assertEqual(code, 0, err)
        self.assertTrue(wait_for(self.launch_record(AGENT), 30), "handoff never launched")
        manifest = json_file(self.manifest(AGENT))
        self.assertEqual(manifest["logical_session_id"], lsid)
        self.bind_transfer_to(AGENT, standin.pid)
        transfer = json_file(self.transfer(AGENT))
        self.assertEqual(transfer["logical_session_id"], lsid)

        # 5. the successor's heartbeat verifies it; an instruction arrives mid-transfer; the parent is stopped
        successor = self.payload(percent=4.0, session_id=SUCCESSOR_AGENT, workdir=self.real_repo, session_name="Nova 2")
        env = self.successor_env(parent, **self.sessions_env())
        self.beat(successor, env)
        self.assertEqual(json_file(self.transfer(AGENT))["state"], "SUCCESSOR_VERIFIED")
        self.assertEqual(self.act(lsid, "instructions", {"text": "Second instruction, sent mid-transfer."})[0], 202)
        self.assertEqual(CORE.inbox_claim(lsid, SUCCESSOR_AGENT)[0], False)  # the successor is not yet the owner
        self.supervise(AGENT)
        self.assertTrue(wait_for_exit(standin.pid))
        self.assertEqual(json_file(self.transfer(AGENT))["state"], "TRANSFER_COMPLETE")
        # the parent is gone by design and is NOT declared orphaned while the handoff completes
        CORE.logical_reconcile(lsid, now=time.time() + 500)
        self.assertEqual(CORE.logical_read(lsid)["state"], "RUNNING")

        # 6. the successor becomes the sole owner and continues; the phone follows the logical session
        self.write_bridge(SUCCESSOR_AGENT)
        code, out, err = run_th(["continuation", "wait", "--timeout", "5", "--session-id", SUCCESSOR_AGENT], env=env)
        report = json.loads(out)
        self.assertEqual((report["directive"], report["logical_session_id"]), ("CONTINUE", lsid), report)
        status, view = self.api("GET", "/api/v1/sessions/" + lsid)
        self.assertEqual((view["owner"]["generation"], view["owner"]["epoch"], view["state"]), (2, 2, "RUNNING"))
        self.assertEqual(view["remote_control"]["state"], "healthy")
        self.assertEqual(CORE.logical_wait(lsid, AGENT, 0, sleep=NOSLEEP)["directive"], "STOP")  # the old owner is fenced out
        self.assertFalse(CORE.inbox_claim(lsid, AGENT)[0])

        # 7. the successor receives the instruction sent during the transfer
        got = CORE.logical_wait(lsid, SUCCESSOR_AGENT, 5, sleep=NOSLEEP)
        self.assertEqual([m["text"] for m in got["messages"]], ["Second instruction, sent mid-transfer."])
        CORE.inbox_ack(lsid, SUCCESSOR_AGENT, got["messages"][0]["id"])

        # 8. a genuine human gate: the phone sees the exact action and approves it
        action = "Restart the Nova production service after deploying commit abc123."
        code, out, _ = run_th(["continuation", "gate", "--session-id", SUCCESSOR_AGENT, "--reason", "The service must restart.", "--requested-action", action], env=env)
        self.assertEqual(json.loads(out)["directive"], "HOLD_FOR_HUMAN")
        status, view = self.api("GET", "/api/v1/sessions/" + lsid)
        self.assertEqual((view["state"], view["human_gate"]["action"]), ("WAITING_FOR_HUMAN", action))
        gate = view["human_gate"]
        # nothing may run yet: consuming an undecided approval is refused
        self.assertFalse(CORE.approval_consume(lsid, SUCCESSOR_AGENT, gate["id"], action)[0])
        code, out, _ = run_th(["continuation", "resume", "--session-id", SUCCESSOR_AGENT], env=env)
        self.assertIn("error", json.loads(out))  # the agent cannot resume its own gate
        status, after = self.api("POST", "/api/v1/sessions/%s/approvals/%s/approve" % (lsid, gate["id"]),
                                 {"nonce": gate["nonce"], "owner_epoch": view["owner"]["epoch"], "request_id": "req-accept-approve"})
        self.assertEqual((status, after["state"]), (200, "RUNNING"), after)
        again = self.api("POST", "/api/v1/sessions/%s/approvals/%s/approve" % (lsid, gate["id"]),
                         {"nonce": gate["nonce"], "owner_epoch": view["owner"]["epoch"], "request_id": "req-accept-approve-2"})
        self.assertEqual(again[0], 409)  # no duplicate approval
        run_th(["continuation", "resume", "--session-id", SUCCESSOR_AGENT], env=env)
        code, out, _ = run_th(["session", "consume", "--session-id", SUCCESSOR_AGENT, "--approval-id", gate["id"], "--requested-action", action], env=env)
        self.assertTrue(json.loads(out)["proceed"])
        code, out, _ = run_th(["session", "consume", "--session-id", SUCCESSOR_AGENT, "--approval-id", gate["id"], "--requested-action", action], env=env)
        self.assertFalse(json.loads(out)["proceed"])  # executed once only

        # 9. STOP from the phone: further autonomous work is refused, and queued work is kept
        status, stopped = self.act(lsid, "stop", {"reason": "Stopped from phone"})
        self.assertEqual((status, stopped["state"]), (200, "STOPPED"))
        code, out, _ = run_th(["continuation", "wait", "--timeout", "0", "--session-id", SUCCESSOR_AGENT], env=env)
        self.assertEqual(json.loads(out)["directive"], "HALT")
        self.assertFalse(CORE.approval_request(lsid, SUCCESSOR_AGENT, "Another action", "why")[0])
        self.assertFalse(CORE.inbox_claim(lsid, SUCCESSOR_AGENT)[0])
        self.assertEqual(self.act(lsid, "instructions", {"text": "Queued while stopped."})[0], 202)
        self.assertEqual(CORE.logical_wait(lsid, SUCCESSOR_AGENT, 0, sleep=NOSLEEP)["directive"], "HALT")
        self.assertEqual(self.act(lsid, "resume")[0], 409)  # a plain resume cannot clear STOP

        # 10. deliberate resume: the queued instruction is delivered
        status, resumed = self.act(lsid, "resume", {"clear_stop": True, "reason": "Reviewed and safe to continue."})
        self.assertEqual((status, resumed["state"]), (200, "RUNNING"))
        got = CORE.logical_wait(lsid, SUCCESSOR_AGENT, 5, sleep=NOSLEEP)
        self.assertEqual([m["text"] for m in got["messages"]], ["Queued while stopped."])

        # 11. the owner dies: after a grace period the session is ORPHANED, never shown as RUNNING, never replaced
        standin_b = self.standin_claude("successor")
        binding = self.binding_for(standin_b.pid, session_id=SUCCESSOR_AGENT)
        CORE.logical_mutate(lsid, lambda r: r["owner"].update(process=binding))
        self.assertEqual(CORE.owner_liveness(CORE.logical_read(lsid))[0], "alive")
        standin_b.terminate()
        self.assertTrue(wait_for_exit(standin_b.pid))
        os.remove(os.path.join(self.sessions_dir, "%s.json" % SUCCESSOR_AGENT))
        CORE.write_json_private(CORE.live_session_path(SUCCESSOR_AGENT), {"observed_epoch": 1.0})
        base = time.time()
        for offset in (0, 30, 65, 100):
            CORE.logical_reconcile(lsid, now=base + offset)
        status, view = self.api("GET", "/api/v1/sessions/" + lsid)
        self.assertEqual(view["state"], "ORPHANED")
        self.assertEqual(len(CORE.logical_list()), 1)  # no replacement was launched
        self.assertEqual(self.act(lsid, "recover", {"action": "reattach"})[0], 409)  # nothing alive to re-attach to
        status, ended = self.act(lsid, "recover", {"action": "abandon"})
        self.assertEqual((status, ended["state"]), (200, "FAILED"))

        # at no point were two owners possible
        owners = [h for h in CORE.logical_read(lsid)["history"] if h["event"] in ("owner_registered", "ownership_changed")]
        self.assertEqual([h["epoch"] for h in owners], [1, 2])


if __name__ == "__main__":
    unittest.main()
