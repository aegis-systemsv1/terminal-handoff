"""Remote session launcher: create_session(project, task) through the real API.

Terminal windows are never opened: a fake terminal callable stands in and, where
a test needs a registered session, runs the real status-line hook exactly as a
launched Claude would.
"""

import functools
import json
import os
import re
import subprocess
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import CORE, GIT, json_file, run_th, text_file  # noqa: E402
from test_continuation import ContinuationCase, SUCCESSOR  # noqa: E402
from test_remote_api import HOST, LOGIN, ORIGIN, good_profile  # noqa: E402
from test_logical import LogicalCase  # noqa: E402
import http.client  # noqa: E402

AGENT = "remote-agent-session-0001"


class LaunchCase(LogicalCase):
    def setUp(self):
        super().setUp()
        self._sessions_env = os.environ.get("CLAUDE_TERMINAL_HANDOFF_CLAUDE_SESSIONS_DIR")
        self._bin_env = os.environ.get("CLAUDE_TERMINAL_HANDOFF_CLAUDE_BIN")
        self.sessions_dir = os.path.join(self.tmp, "claude-sessions")
        os.makedirs(self.sessions_dir)
        os.environ["CLAUDE_TERMINAL_HANDOFF_CLAUDE_SESSIONS_DIR"] = self.sessions_dir
        os.environ["CLAUDE_TERMINAL_HANDOFF_CLAUDE_BIN"] = self.fake_claude
        self.repo = os.path.join(self.tmp, "nova")
        os.makedirs(self.repo)
        self.real_repo = os.path.realpath(self.repo)
        CORE.project_add("nova", self.repo)
        CORE.project_set_permissions("nova", good_profile())
        CORE.project_set_remote_launch("nova", True)
        self.launched = []
        self.script_texts = []
        self.script_modes = []
        self.token_files = []
        self.isolation_check = lambda claude_bin: (True, None)
        self.behaviour = "register"
        self.bridge = True
        self.server = None
        self.start_server()
        code, _ = CORE.device_enroll_begin("phone")
        _, self.token = CORE.device_enroll_complete(code)

    def tearDown(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        for key, saved in (("CLAUDE_TERMINAL_HANDOFF_CLAUDE_SESSIONS_DIR", self._sessions_env),
                           ("CLAUDE_TERMINAL_HANDOFF_CLAUDE_BIN", self._bin_env)):
            if saved is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = saved
        super().tearDown()

    # -- fake terminal ------------------------------------------------------
    def fake_terminal(self, manifest, script_file, title, test_mode):
        script = text_file(script_file)
        self.launched.append(script_file)
        self.script_texts.append(script)
        self.script_modes.append(os.stat(script_file).st_mode & 0o777)
        self.token_files.append(re.search(r"TH_TOKEN_FILE=(\S+)", script).group(1))
        if self.behaviour == "fail":
            return {"launched": False, "error": "no terminal"}
        if self.behaviour == "register":
            lsid = re.search(r"LOGICAL_SESSION=(ls_[a-f0-9]{24})", script).group(1)
            token = self.token_of(script)
            threading.Thread(target=self.register, args=(lsid, token), daemon=True).start()
        return {"launched": True, "test_mode": False}

    def token_of(self, script):
        return text_file(re.search(r"TH_TOKEN_FILE=(\S+)", script).group(1))

    def register(self, lsid, token, session_id=AGENT, workdir=None, env_token=None):
        payload = self.payload(percent=4.0, session_id=session_id, workdir=workdir or self.real_repo)
        env = self.env(
            CLAUDE_TERMINAL_HANDOFF_LOGICAL_SESSION=lsid,
            CLAUDE_TERMINAL_HANDOFF_LAUNCH_TOKEN=env_token or token,
            CLAUDE_TERMINAL_HANDOFF_CLAUDE_SESSIONS_DIR=self.sessions_dir,
        )
        if self.bridge:
            with open(os.path.join(self.sessions_dir, "1.json"), "w") as handle:
                json.dump({"pid": os.getpid(), "sessionId": session_id, "bridgeSessionId": "b"}, handle)
        code, _, err = self.statusline(payload, env)
        return code

    def start_server(self, wait=5.0):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        launcher = functools.partial(
            CORE.remote_create_session, terminal=self.fake_terminal, wait_seconds=wait, health_wait=0.0,
            isolation_check=self.isolation_check,
        )
        config = {"allowed_host": HOST, "tailscale_users": [LOGIN], "port": 8787}
        self.server = CORE.make_remote_server(config, port=0, launcher=launcher, tailscale_checker=lambda h: (True, None))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def create(self, project="nova", task="Investigate latency. Do not deploy.", request_id=None, token=None, **extra):
        body = {"project": project, "task": task, "request_id": request_id or "req-create-%06d" % len(self.launched)}
        body.update(extra)
        return self.post("/api/v1/sessions", body, token=token or self.token)

    def post(self, path, body, token=None, origin=ORIGIN):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        headers = {"Host": HOST, "Tailscale-User-Login": LOGIN, "Origin": origin, "Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        conn.request("POST", path, json.dumps(body), headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        conn.close()
        return resp.status, data


class TestCreateSession(LaunchCase):
    def test_authorised_create_launches_claude_on_the_trusted_project(self):
        status, view = self.create()
        self.assertEqual(status, 201, view)
        self.assertEqual(view["state"], "RUNNING")
        self.assertEqual(view["project"], "nova")
        self.assertEqual(view["owner"]["generation"], 1)
        self.assertEqual(view["remote_control"]["state"], "healthy")
        record = CORE.logical_read(view["logical_session_id"])
        self.assertEqual(record["repository"], self.real_repo)
        self.assertEqual(record["owner"]["agent_session_id"], AGENT)
        self.assertIsNone(record["launch"]["token_sha256"])  # single use, cleared
        script = self.script_texts[0]
        self.assertIn("cd -- %s" % self.real_repo, script)
        self.assertIn("--remote-control", script)
        self.assertIn("--settings", script)
        self.assertIn("--setting-sources ''", script)  # user/project/local settings are excluded
        self.assertEqual(self.script_modes[0], 0o700)

    def test_the_task_is_a_durable_first_instruction(self):
        status, view = self.create(task="Audit the retrieval pipeline.")
        record = CORE.logical_read(view["logical_session_id"])
        first = record["inbox"]["messages"][0]
        self.assertEqual((first["seq"], first["text"]), (1, "Audit the retrieval pipeline."))
        self.assertTrue(first["source"].startswith("device:"))
        ok, _, claimed = CORE.inbox_claim(view["logical_session_id"], AGENT)
        self.assertEqual(claimed[0]["text"], "Audit the retrieval pipeline.")

    def test_success_is_only_reported_when_the_mac_confirms(self):
        self.behaviour = "silent"
        self.start_server(wait=1.0)
        status, view = self.create()
        self.assertEqual(status, 504)
        self.assertEqual(view["state"], "FAILED")
        record = CORE.logical_read(view["logical_session_id"])
        self.assertEqual(record["state"], "FAILED")
        # a late session can no longer register, and is told it is not the owner
        self.register(record["logical_session_id"], "irrelevant")
        self.assertIsNone(CORE.logical_read(record["logical_session_id"])["owner"])

    def test_accepted_but_unconfirmed_is_creating_not_running(self):
        self.behaviour = "silent"
        self.start_server(wait=0.0)
        status, view = self.create()
        self.assertEqual(status, 202)
        self.assertEqual(view["state"], "CREATING")

    def test_terminal_failure_is_reported_as_failed(self):
        self.behaviour = "fail"
        status, view = self.create()
        self.assertEqual(status, 502)
        self.assertEqual(view["state"], "FAILED")

    def test_missing_remote_control_is_degraded_but_the_session_runs(self):
        self.bridge = False
        status, view = self.create()
        self.assertEqual(status, 201)
        self.assertEqual(view["state"], "RUNNING")
        self.assertEqual(view["remote_control"]["state"], "degraded")
        pending = os.listdir(os.path.join(self.home, "outbox", "pending"))
        kinds = [json_file(os.path.join(self.home, "outbox", "pending", n))["event"]["kind"] for n in pending]
        self.assertIn("remote_degraded", kinds)

    def test_duplicate_request_does_not_create_two_sessions(self):
        self.behaviour = "silent"
        self.start_server(wait=0.0)
        self.create(request_id="req-dup-000001")
        status, view = self.create(request_id="req-dup-000001")
        self.assertTrue(view.get("replayed") or view.get("duplicate"))
        self.assertEqual(len(CORE.logical_list()), 1)
        self.assertEqual(len(self.launched), 1)

    def test_only_one_active_session_per_project(self):
        self.behaviour = "silent"
        self.start_server(wait=0.0)
        self.create(request_id="req-one-000001")
        status, view = self.create(request_id="req-two-000001")
        self.assertEqual(status, 409)
        self.assertEqual(view["error"], "project_in_use")
        self.assertEqual(len(self.launched), 1)


class TestCreateRefusals(LaunchCase):
    def test_unauthenticated_and_revoked_callers_are_refused(self):
        self.assertEqual(self.create(token="thd_d_0123456789abcdef.forged")[0], 401)
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("POST", "/api/v1/sessions", "{}", {"Host": HOST, "Tailscale-User-Login": LOGIN, "Origin": ORIGIN, "Content-Type": "application/json"})
        self.assertEqual(conn.getresponse().status, 401)
        device_id = self.token[4:].split(".")[0]
        CORE.device_revoke(device_id)
        self.assertEqual(self.create()[0], 401)
        self.assertEqual(self.launched, [])

    def test_unknown_project_and_traversal_are_rejected(self):
        for project in ("vertex", "../nova", "..", "/tmp", self.repo, "nova/../..", "", "NOVA", "nova;id", 5, None):
            status, _ = self.create(project=project)
            self.assertIn(status, (400, 404), repr(project))
        self.assertEqual(self.launched, [])

    def test_a_path_or_cwd_cannot_be_supplied(self):
        for field in ("path", "cwd", "directory", "repo", "argv", "command", "pid", "model", "permission_mode"):
            status, _ = self.create(**{field: "/etc"})
            self.assertEqual(status, 400, field)
        self.assertEqual(self.launched, [])

    def test_a_symlink_swapped_project_is_refused(self):
        link = os.path.join(self.tmp, "novalink")
        os.symlink(self.repo, link)
        CORE.projects_update(lambda p: p.pop("nova"))
        CORE.project_add("nova", link)
        CORE.project_set_permissions("nova", good_profile())
        CORE.project_set_remote_launch("nova", True)
        os.unlink(link)
        os.symlink(self.tmp, link)  # now points somewhere else entirely
        self.assertEqual(self.create()[0], 404)
        self.assertEqual(self.launched, [])

    def test_missing_invalid_or_changed_permission_profile_fails_closed(self):
        CORE.projects_update(lambda p: p.__setitem__("bare", {"path": self.repo, "realpath": self.real_repo, "enabled": True, "remote_launch": True, "permissions": None}))
        status, body = self.create(project="bare")
        self.assertEqual((status, body["error"]), (403, "permission_profile_required"))
        CORE.projects_update(lambda p: p["nova"]["permissions"]["profile"]["allow"].append("Bash(git status:*)"))
        status, body = self.create()
        self.assertEqual((status, body["error"]), (403, "permission_profile_required"))
        CORE.projects_update(lambda p: p["nova"]["permissions"]["profile"].update(allow=["Bash"]))
        self.assertEqual(self.create()[0], 403)
        self.assertEqual(self.launched, [])

    def test_remote_launch_must_be_explicitly_enabled(self):
        CORE.project_set_remote_launch("nova", False)
        self.assertEqual(self.create()[1]["error"], "remote_launch_disabled")

    def test_bad_task_is_rejected(self):
        for task in ("", "   ", None, 5, "x" * 9000):
            self.assertEqual(self.create(task=task)[0], 400, repr(task)[:20])

    def test_a_repository_mid_operation_is_refused(self):
        subprocess.run([GIT, "init", "-q", self.repo], check=True)
        with open(os.path.join(self.repo, ".git", "MERGE_HEAD"), "w") as handle:
            handle.write("0" * 40 + "\n")
        status, body = self.create()
        self.assertEqual((status, body["error"]), (409, "repository_busy"))
        self.assertEqual(self.launched, [])


class TestNoInjection(LaunchCase):
    PAYLOAD = "x'; touch {p}/PWNED1; echo \"$(touch {p}/PWNED2)\" `touch {p}/PWNED3` \\\n$(id) && rm -rf ~ #"

    def test_task_text_never_reaches_a_shell_or_argv(self):
        task = self.PAYLOAD.format(p=self.tmp)
        status, view = self.create(task=task)
        self.assertEqual(status, 201)
        lsid = view["logical_session_id"]
        script = self.script_texts[0]
        prompt = text_file(CORE.th_path("prompts", "remote-%s.md" % lsid))
        for artefact in (script, prompt):
            self.assertNotIn("PWNED", artefact)
            self.assertNotIn("$(id)", artefact)
        for name in ("PWNED1", "PWNED2", "PWNED3"):
            self.assertFalse(os.path.exists(os.path.join(self.tmp, name)))
        stored = CORE.logical_read(lsid)["inbox"]["messages"][0]["text"]
        self.assertIn("touch %s/PWNED1" % self.tmp, stored)  # kept verbatim, as inert data

    def test_launch_argv_is_safe_and_contains_no_bypass(self):
        self.create()
        script = self.script_texts[0]
        for token in CORE.FORBIDDEN_LAUNCH_TOKENS:
            self.assertNotRegex(script, r"exec .*\s%s(\s|$)" % re.escape(token))
        self.assertNotIn("dangerously", script)
        argv = CORE.build_remote_launch_argv("/bin/claude", "Remote nova", "/s.json", "prompt")
        self.assertEqual(CORE.assert_remote_argv_safe(argv), [])
        self.assertTrue(CORE.assert_remote_argv_safe(["claude", "--dangerously-skip-permissions", "p"]))

    def test_permission_settings_hold_only_the_validated_profile(self):
        status, view = self.create()
        path = CORE.th_path("remote", "profiles", "%s.json" % view["logical_session_id"])
        settings = json_file(path)
        self.assertEqual(sorted(settings), ["hooks", "permissions", "statusLine"])
        self.assertEqual(sorted(settings["permissions"]), ["allow", "deny", "disableAutoMode", "disableBypassPermissionsMode"])
        self.assertEqual(settings["permissions"]["disableBypassPermissionsMode"], "disable")
        allow = settings["permissions"]["allow"]
        self.assertEqual(allow[: len(good_profile()["allow"])], good_profile()["allow"])
        extra = allow[len(good_profile()["allow"]):]
        self.assertEqual(len(extra), len(CORE.AGENT_CLI_SUBCOMMANDS) + len(CORE.agent_read_allow_rules(self.real_repo)))
        joined = " ".join(extra)
        for admin in ("decide", "stop", "pause", "resume", "post", "recover", "reconcile", "session show", "session list"):
            self.assertNotIn("session %s" % admin, joined)  # the agent may not approve its own gate or clear STOP
        self.assertNotIn("continuation resume:*)" if False else "session resume", joined)
        self.assertNotIn("defaultMode", json.dumps(settings))
        self.assertIn("statusline", settings["statusLine"]["command"])
        self.assertIn("hook-stop", json.dumps(settings["hooks"]))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)


class TestAgentReadRules(LaunchCase):
    def test_successor_read_access_is_narrow_and_never_covers_launch_tokens(self):
        rules = CORE.agent_read_allow_rules(self.real_repo)
        joined = "\n".join(rules)
        self.assertEqual(len(rules), 5)
        for rule in rules:
            self.assertTrue(rule.startswith("Read(//"), rule)  # Claude's absolute-path prefix
        self.assertIn("/handoffs/**", joined)
        self.assertIn("/transfers/**", joined)
        self.assertIn("prompts/successor-*.md", joined)
        self.assertIn("prompts/remote-*.md", joined)
        self.assertNotIn(".tok", joined)
        self.assertNotIn("prompts/**", joined)  # would expose one-time launch tokens
        self.assertNotIn("/.claude/projects/**", joined)  # other projects' transcripts stay private
        self.assertIn(re.sub(r"[^A-Za-z0-9]", "-", self.real_repo), joined)
        self.assertEqual(len(CORE.agent_read_allow_rules(None)), 4)


class TestRegistration(LaunchCase):
    def make(self):
        self.behaviour = "silent"
        self.start_server(wait=0.0)
        status, view = self.create()
        lsid = view["logical_session_id"]
        return lsid, self.script_texts[0]

    def test_wrong_token_or_directory_or_replay_cannot_register(self):
        lsid, script = self.make()
        token = self.token_of(script)
        self.register(lsid, token, env_token="wrong-token")
        self.assertIsNone(CORE.logical_read(lsid)["owner"])
        self.register(lsid, token, workdir=self.tmp)
        self.assertIsNone(CORE.logical_read(lsid)["owner"])
        self.register(lsid, token)
        self.assertEqual(CORE.logical_read(lsid)["owner"]["agent_session_id"], AGENT)
        self.register(lsid, token, session_id="second-claim-session-0002")
        self.assertEqual(CORE.logical_read(lsid)["owner"]["agent_session_id"], AGENT)

    def test_expired_launch_token_is_refused(self):
        lsid, script = self.make()
        token = self.token_of(script)
        CORE.logical_mutate(lsid, lambda r: r["launch"].update(expires_epoch=1.0))
        self.register(lsid, token)
        self.assertIsNone(CORE.logical_read(lsid)["owner"])

    def test_launch_token_is_stored_only_as_a_hash_and_never_logged(self):
        lsid, script = self.make()
        token = self.token_of(script)
        self.assertNotIn(token, script)  # the launcher holds no secret
        self.assertNotIn(token, text_file(CORE.logical_path(lsid)))
        self.assertNotIn(token, text_file(os.path.join(self.home, "logs", "terminal-handoff.log")))
        self.assertNotIn(token, text_file(CORE.th_path("prompts", "remote-%s.md" % lsid)))

    def test_the_launch_token_is_never_propagated_to_successors(self):
        os.environ["CLAUDE_TERMINAL_HANDOFF_LAUNCH_TOKEN"] = "secret-launch-token"
        try:
            self.assertNotIn("CLAUDE_TERMINAL_HANDOFF_LAUNCH_TOKEN", CORE.propagated_environment())
        finally:
            del os.environ["CLAUDE_TERMINAL_HANDOFF_LAUNCH_TOKEN"]


class TestSessionCheck(LaunchCase):
    def test_unregistered_or_foreign_sessions_are_told_to_stop(self):
        status, view = self.create()
        lsid = view["logical_session_id"]
        env = self.env(CLAUDE_TERMINAL_HANDOFF_CLAUDE_SESSIONS_DIR=self.sessions_dir)
        code, out, _ = run_th(["session", "check", "--logical-session", lsid, "--session-id", "intruder-session-9"], env=env)
        self.assertEqual(json.loads(out)["directive"], "STOP")
        code, out, _ = run_th(["session", "check", "--logical-session", lsid, "--session-id", AGENT], env=env)
        report = json.loads(out)
        self.assertEqual((report["directive"], report["is_owner"], report["remote_control"]), ("CONTINUE", True, "healthy"))
        CORE.logical_stop(lsid, by="phone", reason="x")
        self.assertEqual(json.loads(run_th(["session", "check", "--logical-session", lsid, "--session-id", AGENT], env=env)[1])["directive"], "HALT")

    def test_the_agent_prompt_states_the_gates_and_forbids_bypass(self):
        status, view = self.create()
        prompt = text_file(CORE.th_path("prompts", "remote-%s.md" % view["logical_session_id"]))
        for gate in CORE.MANDATORY_HUMAN_GATES:
            self.assertIn(gate, prompt)
        self.assertIn("Never use the\n--dangerously-skip-permissions flag", prompt)
        self.assertIn("session inbox", prompt)
        self.assertIn("DO NOT end\nyour turn", prompt)  # a halted agent must keep waiting, or it can never be resumed
        self.assertNotIn("{{", prompt)


class TestHandoffKeepsTheSessionControllable(ContinuationCase):
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

    def logical(self):
        import hashlib

        record = CORE.logical_create(project="nova", repository=self.workdir, launch_token_sha256=hashlib.sha256(b"t").hexdigest(), launch_expires_epoch=9999999999)
        CORE.logical_register_owner(record["logical_session_id"], self.parent_id, 1, None, None, "t")
        return record["logical_session_id"]

    def test_remote_control_survives_a_successor_transition(self):
        lsid = self.logical()
        CORE.logical_mutate(lsid, lambda r: r.update(remote_control={"state": "healthy"}))
        self.make_transfer("TRANSFER_COMPLETE", logical_session_id=lsid)
        self.session_record()  # the successor's live bridge
        _, report, _ = self.cont("wait")
        self.assertEqual(report["directive"], "CONTINUE")
        record = CORE.logical_read(lsid)
        self.assertEqual(record["owner"]["agent_session_id"], SUCCESSOR)
        self.assertEqual(record["remote_control"]["state"], "healthy")  # re-verified for the new owner
        # the remote client, addressing only the logical id, now reaches the new owner
        view = CORE.logical_public_view(record)
        self.assertEqual((view["owner"]["generation"], view["state"]), (2, "RUNNING"))

    def test_degraded_successor_remote_is_recorded_on_the_logical_session(self):
        lsid = self.logical()
        self.make_transfer("TRANSFER_COMPLETE", logical_session_id=lsid)
        self.session_record(bridge=None)
        _, report, _ = self.cont("wait")
        self.assertEqual(report["directive"], "CONTINUE")  # still working
        self.assertEqual(CORE.logical_read(lsid)["remote_control"]["state"], "degraded")

    def test_phone_disconnect_never_stops_work(self):
        lsid = self.logical()
        self.make_transfer("TRANSFER_COMPLETE", logical_session_id=lsid)
        self.session_record()
        self.assertEqual(self.cont("wait")[1]["directive"], "CONTINUE")
        # no gateway, no device, no connection at all: the agent side is unaffected
        self.assertEqual(self.cont("wait")[1]["directive"], "CONTINUE")
        _, out, _ = (lambda r: (r[0], json.loads(r[1]), r[2]))(run_th(["session", "check", "--logical-session", lsid, "--session-id", SUCCESSOR], env=self.th_env()))
        self.assertEqual(out["directive"], "CONTINUE")

    def test_the_successor_prompt_names_the_command_the_allow_rules_cover(self):
        lsid = self.logical()
        CORE.logical_mutate(lsid, lambda r: r.update(th_command={"python": "/opt/py/bin/python3.99", "core": "/opt/th/core.py"}))
        manifest = {"model": {"id": "claude-opus-5"}, "effort": {"level": "high", "available": True}, "chain_id": "abcdef012345",
                    "generation": 1, "display": {"successor_display_name": "S 2"}, "logical_session_id": lsid,
                    "outgoing": {"session_id": self.parent_id}}
        prompt = CORE.render_successor_prompt(manifest)
        self.assertIn("/opt/py/bin/python3.99 /opt/th/core.py continuation wait", prompt)
        self.assertNotIn(CORE.shlex.quote(CORE.sys.executable) + " " + CORE.shlex.quote(CORE.os.path.abspath(CORE.__file__)) + " continuation", prompt)
        plain = dict(manifest, logical_session_id=None)
        self.assertIn(CORE.os.path.abspath(CORE.__file__), CORE.render_successor_prompt(plain))  # ordinary handoffs unchanged

    def test_successor_argv_carries_the_same_permission_settings_and_gates(self):
        lsid = self.logical()
        settings = CORE.write_permission_settings(lsid, good_profile())
        CORE.logical_mutate(lsid, lambda r: r.update(permissions={"profile": "development", "human_gate": list(CORE.MANDATORY_HUMAN_GATES), "settings_file": settings}))
        manifest = {"model": {"id": "claude-opus-5"}, "effort": {"level": "high", "available": True}, "chain_id": "abcdef012345",
                    "generation": 1, "display": {"successor_display_name": "S 2"}, "logical_session_id": lsid,
                    "outgoing": {"session_id": self.parent_id}}
        argv = CORE.build_launch_argv(manifest, "/bin/claude", "PROMPT")
        self.assertEqual(argv[argv.index("--settings") + 1], settings)
        self.assertEqual(CORE.assert_launch_argv_safe(argv), [])
        self.assertIn("production deployment", CORE.render_successor_prompt(manifest))
        # a settings path outside the profiles directory is ignored
        CORE.logical_mutate(lsid, lambda r: r["permissions"].update(settings_file="/etc/passwd"))
        self.assertNotIn("--settings", CORE.build_launch_argv(manifest, "/bin/claude", "PROMPT"))


if __name__ == "__main__":
    unittest.main()
