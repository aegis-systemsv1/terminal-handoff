"""Session names are display metadata only: settable at creation, renamable later, and
persistent, without ever touching identity, ownership or state."""

import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import CORE, text_file  # noqa: E402
from test_remote_api import HOST, LOGIN  # noqa: E402
from test_remote_launch import AGENT, LaunchCase  # noqa: E402


class NameCase(LaunchCase):
    def api(self, method, path, body=None, token=None):
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        headers = {"Host": HOST, "Tailscale-User-Login": LOGIN, "Authorization": "Bearer " + (token or self.token)}
        if method == "POST":
            headers.update({"Origin": "https://" + HOST, "Content-Type": "application/json"})
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        conn.close()
        return resp.status, data

    def rename(self, lsid, name, n=[0]):
        n[0] += 1
        return self.api("POST", "/api/v1/sessions/%s/rename" % lsid, {"name": name, "request_id": "req-name-%06d" % n[0]})

    def running(self, name=None):
        extra = {} if name is None else {"name": name}
        status, view = self.create(request_id="req-name-create-%d" % len(self.launched), **extra)
        self.assertEqual(status, 201, view)
        return view["logical_session_id"], view


class TestCreate(NameCase):
    def test_create_with_a_custom_name(self):
        lsid, view = self.running("Nova Voice Fix")
        self.assertEqual(view["name"], "Nova Voice Fix")
        self.assertEqual(CORE.logical_read(lsid)["name"], "Nova Voice Fix")
        self.assertIn("--name 'Nova Voice Fix'", self.script_texts[0])  # the Claude session carries it too

    def test_create_without_a_name_keeps_the_default_naming(self):
        lsid, view = self.running()
        self.assertIsNone(view["name"])
        self.assertIn("--name 'Remote nova'", self.script_texts[0])

    def test_a_blank_name_is_the_same_as_none(self):
        lsid, view = self.running("   ")
        self.assertIsNone(view["name"])

    def test_bad_names_stop_creation_before_anything_starts(self):
        ctx = {"device_id": "d_0123456789abcdef"}
        for bad in ("x" * 61, "../../etc", "a/b", "$(touch x)", "`id`", "ls_" + "a" * 24, "-flag", ".hidden", 5, ["a"], {"n": 1}):
            body = {"project": "nova", "task": "do it", "name": bad, "request_id": "req-name-bad-%s" % abs(hash(str(bad)) % 10 ** 6)}
            status, payload = CORE.remote_create_session(body, ctx, terminal=self.fake_terminal, wait_seconds=0, isolation_check=self.isolation_check)
            self.assertEqual(status, 400, repr(bad))
        self.assertEqual(self.launched, [])
        self.assertEqual(CORE.logical_list(), [])
        # and through the real API, once (the persistent create rate limit is deliberately small)
        status, _ = self.create(name="../x", request_id="req-name-bad-api-1")
        self.assertEqual(status, 400)


class TestRename(NameCase):
    def test_rename_changes_the_label_and_nothing_else(self):
        lsid, _ = self.running("Remote scratch")
        self.api("POST", "/api/v1/sessions/%s/instructions" % lsid, {"text": "queued work", "request_id": "req-name-inst-01"})
        CORE.approval_request(lsid, AGENT, "Deploy build abc123", "needed")
        before = copy.deepcopy(CORE.logical_read(lsid))
        status, view = self.rename(lsid, "Nova Voice Fix")
        self.assertEqual((status, view["name"]), (200, "Nova Voice Fix"))
        after = CORE.logical_read(lsid)
        for key in ("logical_session_id", "owner", "owner_epoch", "project", "repository", "branch", "permissions", "inbox", "approvals",
                    "stop", "paused", "remote_control", "state", "created_utc", "created_by", "launch", "th_command", "created_request_id"):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(len(CORE.logical_list()), 1)  # no new logical session
        self.assertEqual(after["name"], "Nova Voice Fix")

    def test_rename_is_audited_with_old_and_new_names(self):
        lsid, _ = self.running("First")
        self.rename(lsid, "Second")
        entry = [h for h in CORE.logical_read(lsid)["history"] if h["event"] == "renamed"][-1]
        self.assertEqual((entry["old_name"], entry["new_name"]), ("First", "Second"))
        self.assertTrue(entry["by"].startswith("device:"))
        log = [json.loads(line) for line in text_file(os.path.join(self.home, "logs", "terminal-handoff.log")).splitlines() if '"logical_renamed"' in line]
        self.assertEqual((log[-1]["old_name"], log[-1]["new_name"], log[-1]["logical_session_id"]), ("First", "Second", lsid))

    def test_blank_rename_clears_the_custom_name(self):
        lsid, _ = self.running("Something")
        self.assertIsNone(self.rename(lsid, "")[1]["name"])
        self.assertIsNone(CORE.logical_read(lsid)["name"])

    def test_invalid_names_are_refused_and_change_nothing(self):
        lsid, _ = self.running("Keep me")
        for bad in ("x" * 61, "../x", "a\\b", "$(id)", "a;b", "a|b", "<b>x</b>", "ls_" + "b" * 24, "ap_" + "1" * 16, "~/x", "😀", 7, None if False else ["x"]):
            status, body = self.rename(lsid, bad)
            self.assertEqual(status, 400, repr(bad))
        self.assertEqual(CORE.logical_read(lsid)["name"], "Keep me")
        self.assertEqual(len([h for h in CORE.logical_read(lsid)["history"] if h["event"] == "renamed"]), 0)

    def test_the_boundary_length_is_accepted_and_whitespace_is_normalised(self):
        lsid, _ = self.running()
        self.assertEqual(self.rename(lsid, "a" * 60)[0], 200)
        self.assertEqual(self.rename(lsid, "  Nova \t Voice\n Fix  ")[1]["name"], "Nova Voice Fix")
        self.assertEqual(self.rename(lsid, "Café & Co. (v2): #1!")[0], 200)

    def test_the_endpoint_takes_only_a_name_and_needs_authentication(self):
        lsid, _ = self.running()
        path = "/api/v1/sessions/%s/rename" % lsid
        for extra in ({"project": "other"}, {"owner": "x"}, {"logical_session_id": "ls_" + "c" * 24}, {"pid": 1}):
            self.assertEqual(self.api("POST", path, dict({"name": "ok", "request_id": "req-name-x-0001"}, **extra))[0], 400)
        self.assertEqual(self.api("POST", path, {"name": "ok", "request_id": "req-name-x-0002"}, token="thd_d_0123456789abcdef.forged")[0], 401)
        self.assertEqual(self.api("POST", "/api/v1/sessions/ls_" + "0" * 24 + "/rename", {"name": "ok", "request_id": "req-name-x-0003"})[0], 404)

    def test_rename_is_allowed_while_stopped_and_does_not_clear_the_stop(self):
        lsid, _ = self.running("Before")
        CORE.logical_stop(lsid, by="phone", reason="hold")
        self.assertEqual(self.rename(lsid, "After")[0], 200)
        record = CORE.logical_read(lsid)
        self.assertTrue(record["stop"]["active"])
        self.assertEqual(record["state"], "STOPPED")

    def test_the_local_command_line_can_rename_too(self):
        lsid, _ = self.running()
        from _harness import run_th

        code, out, _ = run_th(["session", "rename", "--logical-session", lsid, "--text", "From the Mac"], env=self.env())
        self.assertEqual(json.loads(out)["state"], "RUNNING")
        self.assertEqual(CORE.logical_read(lsid)["name"], "From the Mac")
        code, out, _ = run_th(["session", "rename", "--logical-session", lsid, "--text", "../bad"], env=self.env())
        self.assertNotEqual(code, 0)


class TestPersistence(NameCase):
    def test_the_name_survives_refresh_and_a_gateway_restart(self):
        lsid, _ = self.running("Persistent")
        self.rename(lsid, "Persistent 2")
        self.assertEqual(self.api("GET", "/api/v1/sessions/" + lsid)[1]["name"], "Persistent 2")  # a page refresh
        self.assertEqual(self.api("GET", "/api/v1/sessions")[1]["sessions"][0]["name"], "Persistent 2")
        self.server.shutdown()
        self.server.server_close()
        CORE.service_recover(liveness=lambda r, n: ("alive", "test"), sleep=lambda s: None)  # what a restart runs
        self.start_server()
        self.assertEqual(self.api("GET", "/api/v1/sessions/" + lsid)[1]["name"], "Persistent 2")

    def test_the_name_survives_stop_resume_and_a_disconnect(self):
        lsid, _ = self.running("Steady")
        CORE.logical_stop(lsid, by="phone", reason="x")
        CORE.logical_resume(lsid, by="phone", clear_stop=True, reason="ok")
        self.assertEqual(CORE.logical_read(lsid)["name"], "Steady")

    def test_the_name_survives_a_handoff_and_further_successors(self):
        lsid, _ = self.running("Nova Voice Fix")
        def transfer(parent, successor, gen):
            return {"state": "TRANSFER_COMPLETE", "parent_session_id": parent, "successor": {"session_id": successor},
                    "successor_generation": gen, "chain_id": "chain1"}

        before = CORE.logical_read(lsid)
        ok, why, _ = CORE.logical_adopt_successor(lsid, transfer(AGENT, "agent-B-session", 2))
        self.assertTrue(ok, why)
        ok, why, _ = CORE.logical_adopt_successor(lsid, transfer("agent-B-session", "agent-C-session", 3))
        self.assertTrue(ok, why)
        after = CORE.logical_read(lsid)
        self.assertEqual(after["name"], "Nova Voice Fix")
        self.assertEqual(after["logical_session_id"], before["logical_session_id"])  # same logical session, new owner
        self.assertEqual(after["owner"]["agent_session_id"], "agent-C-session")
        # renaming after a handoff still works on the same logical session
        self.assertEqual(self.rename(lsid, "Renamed after handoff")[1]["logical_session_id"], lsid)
        self.assertEqual(CORE.logical_read(lsid)["owner"]["agent_session_id"], "agent-C-session")

    def test_a_successors_prompt_and_launch_carry_the_same_logical_session(self):
        lsid, _ = self.running("Nova Voice Fix")
        manifest = {"model": {"id": "claude-opus-5"}, "effort": {"level": "high", "available": True}, "chain_id": "abcdef012345", "generation": 1,
                    "display": {"successor_display_name": "Nova Voice Fix 2"}, "logical_session_id": lsid, "outgoing": {"session_id": AGENT}}
        argv = CORE.build_launch_argv(manifest, "/bin/claude", "PROMPT")
        self.assertEqual(argv[argv.index("--name") + 1], "Nova Voice Fix 2")  # chain naming appends the generation
        self.assertEqual(CORE.logical_read(lsid)["name"], "Nova Voice Fix")


if __name__ == "__main__":
    unittest.main()
