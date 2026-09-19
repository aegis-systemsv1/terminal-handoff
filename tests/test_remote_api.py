"""Remote control plane: project registry, permission profiles, devices, HTTP API.

The server is started on a loopback ephemeral port only. Nothing here touches
Tailscale, a real Terminal window, or a real Claude session.
"""

import http.client
import json
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import CORE, TH_SCRIPT, run_th, text_file  # noqa: E402
from test_logical import LogicalCase  # noqa: E402

HOST = "mac-test.tail1234.ts.net"
LOGIN = "john@example.com"
ORIGIN = "https://" + HOST


def good_profile(**overrides):
    profile = CORE.starter_permission_profile()
    profile.update(overrides)
    return profile


class RemoteCase(LogicalCase):
    def setUp(self):
        super().setUp()
        self.config = {"allowed_host": HOST, "tailscale_users": [LOGIN], "port": 8787}
        self.server = CORE.make_remote_server(self.config, port=0, tailscale_checker=lambda host: (True, None))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def enroll_token(self, name="John iPhone"):
        code, _ = CORE.device_enroll_begin(name)
        device_id, token = CORE.device_enroll_complete(code)
        return device_id, token

    def call(self, method, path, body=None, token=None, headers=None, host=HOST, login=LOGIN, origin=ORIGIN,
             content_type="application/json", raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = {"Host": host}
        if login:
            hdrs["Tailscale-User-Login"] = login
        if token:
            hdrs["Authorization"] = "Bearer " + token
        if method == "POST":
            if origin:
                hdrs["Origin"] = origin
            hdrs["Content-Type"] = content_type
        hdrs.update(headers or {})
        data = raw if raw is not None else (json.dumps(body) if body is not None else None)
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        payload = resp.read().decode()
        conn.close()
        try:
            payload = json.loads(payload)
        except ValueError:
            pass
        return resp.status, payload, resp

    def session(self):
        return self.new_session()


class TestServerStartsClosed(LogicalCase):
    def test_incomplete_configuration_refuses_to_start(self):
        for config in ({}, {"allowed_host": "evil.example.com", "tailscale_users": ["a@b.c"], "port": 8787},
                       {"allowed_host": HOST, "tailscale_users": [], "port": 8787},
                       {"allowed_host": HOST, "tailscale_users": ["a@b.c"], "port": 80}):
            with self.assertRaises(ValueError):
                CORE.make_remote_server(config, port=0, tailscale_checker=lambda h: (True, None))

    def test_private_network_failure_refuses_to_start(self):
        config = {"allowed_host": HOST, "tailscale_users": [LOGIN], "port": 8787}
        with self.assertRaises(ValueError):
            CORE.make_remote_server(config, port=0, tailscale_checker=lambda h: (False, "tailscale is down"))
        with self.assertRaises(ValueError):
            CORE.make_remote_server(config, port=0)

    def test_binds_loopback_only(self):
        config = {"allowed_host": HOST, "tailscale_users": [LOGIN], "port": 8787}
        server = CORE.make_remote_server(config, port=0, tailscale_checker=lambda h: (True, None))
        try:
            self.assertEqual(server.server_address[0], "127.0.0.1")
        finally:
            server.server_close()

    def test_tailscale_status_parsing(self):
        good = {"BackendState": "Running", "Self": {"DNSName": HOST + "."}}
        self.assertTrue(CORE.tailscale_status_ok(good, HOST)[0])
        self.assertFalse(CORE.tailscale_status_ok(dict(good, BackendState="Stopped"), HOST)[0])
        self.assertFalse(CORE.tailscale_status_ok({"BackendState": "Running", "Self": {"DNSName": "other.ts.net."}}, HOST)[0])
        self.assertFalse(CORE.tailscale_status_ok(None, HOST)[0])


class TestAuthentication(RemoteCase):
    def test_unauthenticated_requests_are_rejected(self):
        for path in ("/api/v1/sessions", "/api/v1/me", "/api/v1/projects"):
            self.assertEqual(self.call("GET", path)[0], 401, path)
        self.assertEqual(self.call("GET", "/api/v1/sessions", token="thd_garbage")[0], 401)
        self.assertEqual(self.call("GET", "/api/v1/sessions", token="thd_d_0123456789abcdef.nope")[0], 401)

    def test_valid_device_token_is_accepted(self):
        _, token = self.enroll_token()
        status, payload, _ = self.call("GET", "/api/v1/me", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["device"], "John iPhone")

    def test_revoked_token_is_rejected(self):
        device_id, token = self.enroll_token()
        self.assertEqual(self.call("GET", "/api/v1/sessions", token=token)[0], 200)
        self.assertTrue(CORE.device_revoke(device_id))
        self.assertEqual(self.call("GET", "/api/v1/sessions", token=token)[0], 401)

    def test_expired_token_is_rejected(self):
        device_id, token = self.enroll_token()

        def expire(data):
            data["devices"][device_id]["expires_epoch"] = 1.0

        CORE.update_json_locked(CORE.devices_path(), expire)
        self.assertEqual(self.call("GET", "/api/v1/sessions", token=token)[0], 401)

    def test_a_token_is_stored_only_as_a_hash(self):
        _, token = self.enroll_token()
        secret = token.split(".", 1)[1]
        for root, _, files in os.walk(self.home):
            for name in files:
                self.assertNotIn(secret, text_file(os.path.join(root, name)), name)
        listing = json.dumps(CORE.device_list())
        self.assertNotIn(secret, listing)
        self.assertNotIn("sha256", listing)

    def test_enrollment_code_is_single_use_and_expires(self):
        code, _ = CORE.device_enroll_begin("phone")
        self.assertIsNotNone(CORE.device_enroll_complete(code)[0])
        self.assertIsNone(CORE.device_enroll_complete(code)[0])
        code2, _ = CORE.device_enroll_begin("phone")

        def expire(data):
            for e in data["enrollments"].values():
                e["expires_epoch"] = 1.0

        CORE.update_json_locked(CORE.devices_path(), expire)
        self.assertIsNone(CORE.device_enroll_complete(code2)[0])
        self.assertIsNone(CORE.device_enroll_complete("thc_forged")[0])

    def test_enrollment_over_http_sets_a_hardened_cookie(self):
        code, _ = CORE.device_enroll_begin("phone")
        status, payload, resp = self.call("POST", "/api/v1/enroll", {"code": code})
        self.assertEqual(status, 200)
        cookie = resp.getheader("Set-Cookie")
        for attr in ("__Host-thd=", "Secure", "HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(attr, cookie)
        self.assertNotIn("Domain", cookie)
        self.assertNotIn("thd_", json.dumps(payload))  # the token is never in the body
        self.assertEqual(self.call("POST", "/api/v1/enroll", {"code": code})[0], 401)

    def test_wrong_host_or_tailnet_identity_is_refused(self):
        _, token = self.enroll_token()
        self.assertEqual(self.call("GET", "/api/v1/sessions", token=token, host="evil.example.com")[0], 403)
        self.assertEqual(self.call("GET", "/api/v1/sessions", token=token, host="127.0.0.1")[0], 403)
        self.assertEqual(self.call("GET", "/api/v1/sessions", token=token, login=None)[0], 403)
        self.assertEqual(self.call("GET", "/api/v1/sessions", token=token, login="mallory@example.com")[0], 403)

    def test_repeated_failures_lock_the_client_out(self):
        _, token = self.enroll_token()
        for _ in range(CORE.AUTH_FAIL_LIMIT):
            self.assertEqual(self.call("GET", "/api/v1/sessions", token="thd_bad.token")[0], 401)
        self.assertEqual(self.call("GET", "/api/v1/sessions", token="thd_bad.token")[0], 429)
        self.assertEqual(self.call("GET", "/api/v1/sessions", token=token)[0], 429)  # locked even for a valid token

    def test_tokens_never_reach_the_log(self):
        device_id, token = self.enroll_token()
        self.call("GET", "/api/v1/sessions", token=token)
        self.call("GET", "/api/v1/sessions", token="thd_d_0123456789abcdef.SECRETVALUE123")
        CORE.device_revoke(device_id)
        log = text_file(os.path.join(self.home, "logs", "terminal-handoff.log"))
        self.assertNotIn(token.split(".", 1)[1], log)
        self.assertNotIn("SECRETVALUE123", log)
        self.assertIn("remote_auth_failed", log)


class TestCsrfAndOrigin(RemoteCase):
    def test_mutation_requires_a_matching_origin(self):
        _, token = self.enroll_token()
        lsid = self.session()
        body = {"text": "hello there", "request_id": "req-00000001"}
        path = "/api/v1/sessions/%s/instructions" % lsid
        self.assertEqual(self.call("POST", path, body, token=token, origin=None)[0], 403)
        self.assertEqual(self.call("POST", path, body, token=token, origin="https://evil.example.com")[0], 403)
        self.assertEqual(self.call("POST", path, body, token=token, origin="http://" + HOST)[0], 403)
        self.assertEqual(self.call("POST", path, body, token=token)[0], 202)

    def test_cookie_authenticated_mutation_needs_the_csrf_token(self):
        code, _ = CORE.device_enroll_begin("phone")
        _, payload, resp = self.call("POST", "/api/v1/enroll", {"code": code})
        cookie = resp.getheader("Set-Cookie").split(";")[0]
        csrf = payload["csrf"]
        lsid = self.session()
        path = "/api/v1/sessions/%s/instructions" % lsid
        body = {"text": "via cookie", "request_id": "req-00000002"}
        self.assertEqual(self.call("POST", path, body, headers={"Cookie": cookie})[0], 403)  # forged cross-site request
        self.assertEqual(self.call("POST", path, body, headers={"Cookie": cookie, "X-CSRF-Token": "0" * 64})[0], 403)
        self.assertEqual(self.call("POST", path, body, headers={"Cookie": cookie, "X-CSRF-Token": csrf})[0], 202)

    def test_csrf_token_from_another_device_is_refused(self):
        code, _ = CORE.device_enroll_begin("phone")
        _, _, resp = self.call("POST", "/api/v1/enroll", {"code": code})
        cookie = resp.getheader("Set-Cookie").split(";")[0]
        other_id, other_token = self.enroll_token("other")
        other_csrf = self.call("GET", "/api/v1/me", token=other_token)[1]["csrf"]
        lsid = self.session()
        path = "/api/v1/sessions/%s/instructions" % lsid
        body = {"text": "x text", "request_id": "req-00000003"}
        self.assertEqual(self.call("POST", path, body, headers={"Cookie": cookie, "X-CSRF-Token": other_csrf})[0], 403)

    def test_form_style_posts_are_refused(self):
        _, token = self.enroll_token()
        lsid = self.session()
        path = "/api/v1/sessions/%s/instructions" % lsid
        for content_type in ("application/x-www-form-urlencoded", "text/plain", "multipart/form-data"):
            self.assertEqual(self.call("POST", path, token=token, content_type=content_type, raw='{"text":"x"}')[0], 400)


class TestApiSurface(RemoteCase):
    def setUp(self):
        super().setUp()
        _, self.token = self.enroll_token()
        self.lsid = self.session()
        self.base = "/api/v1/sessions/%s" % self.lsid

    def post(self, suffix, body, **kw):
        return self.call("POST", self.base + suffix, body, token=self.token, **kw)

    def test_session_listing_and_view_expose_no_pids_or_paths(self):
        status, payload, _ = self.call("GET", "/api/v1/sessions", token=self.token)
        self.assertEqual(status, 200)
        text = json.dumps(payload)
        for forbidden in ("pid", "process", self.workdir, "sha256", "token"):
            self.assertNotIn(forbidden, text)
        status, view, _ = self.call("GET", self.base, token=self.token)
        self.assertEqual(view["logical_session_id"], self.lsid)

    def test_instruction_is_queued_and_replay_is_deduplicated(self):
        body = {"text": "audit the retrieval pipeline", "request_id": "req-aaaaaaaa"}
        first = self.post("/instructions", body)
        second = self.post("/instructions", body)
        self.assertEqual(first[0], 202)
        self.assertEqual(second[0], 202)
        self.assertTrue(second[1].get("replayed"))
        self.assertEqual(first[1]["message_id"], second[1]["message_id"])
        self.assertEqual(len(CORE.logical_read(self.lsid)["inbox"]["messages"]), 1)
        # a fresh request id with identical text is a new instruction
        self.post("/instructions", dict(body, request_id="req-bbbbbbbb"))
        self.assertEqual(len(CORE.logical_read(self.lsid)["inbox"]["messages"]), 2)

    def test_replay_after_the_in_memory_cache_is_lost_is_still_deduplicated(self):
        body = {"text": "one", "request_id": "req-cccccccc"}
        self.post("/instructions", body)
        self.server.gateway.replay.clear()  # e.g. a gateway restart
        self.post("/instructions", body)
        self.assertEqual(len(CORE.logical_read(self.lsid)["inbox"]["messages"]), 1)

    def test_stop_pause_resume_over_the_api(self):
        self.assertEqual(self.post("/pause", {"request_id": "req-p0000001"})[1]["state"], "PAUSED")
        self.assertEqual(self.post("/resume", {"request_id": "req-r0000001"})[1]["state"], "RUNNING")
        status, view, _ = self.post("/stop", {"request_id": "req-s0000001", "reason": "phone stop"})
        self.assertEqual((status, view["state"], view["stop"]["active"]), (200, "STOPPED", True))
        # STOP is not cleared by a plain resume
        self.assertEqual(self.post("/resume", {"request_id": "req-r0000002"})[0], 409)
        self.assertEqual(self.post("/resume", {"request_id": "req-r0000003", "clear_stop": True})[0], 409)
        status, view, _ = self.post("/resume", {"request_id": "req-r0000004", "clear_stop": True, "reason": "reviewed"})
        self.assertEqual((status, view["state"]), (200, "RUNNING"))

    def test_there_is_no_way_to_target_a_pid_or_run_a_command(self):
        for field, value in (("pid", 1), ("command", "id"), ("shell", "ls"), ("cwd", "/"), ("path", "/etc"), ("argv", ["ls"])):
            status, _, _ = self.post("/instructions", {"text": "hello world", "request_id": "req-x0000001", field: value})
            self.assertEqual(status, 400, field)
            status, _, _ = self.post("/stop", {"request_id": "req-x0000002", field: value})
            self.assertEqual(status, 400, field)
        for path in ("/api/v1/exec", "/api/v1/shell", "/api/v1/run", "/api/v1/pid/123/stop", "/api/v1/sessions/123/stop",
                     "/api/v1/sessions/%s/exec" % self.lsid, "/api/v1/terminal"):
            self.assertIn(self.call("POST", path, {"request_id": "req-y0000001", "command": "id"}, token=self.token)[0], (404,))
        source = text_file(TH_SCRIPT)
        handler = source[source.index("def make_remote_handler"):source.index("class _LoopbackServer")]
        for forbidden in ("subprocess", "os.system", "os.kill", "Popen", "os.exec"):
            self.assertNotIn(forbidden, handler)

    def test_a_forged_or_malformed_session_id_is_not_found(self):
        for sid in ("ls_" + "0" * 24, "ls_../../x", "..%2f..%2fetc", "ls_" + "A" * 24, "1"):
            self.assertEqual(self.call("GET", "/api/v1/sessions/" + sid, token=self.token)[0], 404, sid)
            self.assertEqual(self.call("POST", "/api/v1/sessions/%s/stop" % sid, {"request_id": "req-z0000001"}, token=self.token)[0], 404, sid)

    def test_malformed_bodies_are_rejected(self):
        path = self.base + "/instructions"
        for raw in ("not json", "[]", '"x"', "{", "null"):
            self.assertEqual(self.call("POST", path, token=self.token, raw=raw)[0], 400, raw)
        self.assertEqual(self.post("/instructions", {"text": "hello", "request_id": "short"})[0], 400)
        self.assertEqual(self.post("/instructions", {"text": "hello"})[0], 400)
        self.assertEqual(self.post("/instructions", {"text": "x" * 9000, "request_id": "req-big00001"})[0], 400)
        self.assertEqual(self.call("POST", path, token=self.token, raw="x" * 40000)[0], 400)

    def test_other_methods_are_refused(self):
        for method in ("PUT", "DELETE", "PATCH"):
            self.assertEqual(self.call(method, self.base, token=self.token)[0], 405)

    def test_session_creation_is_not_available_until_the_launcher_exists(self):
        status, _, _ = self.call("POST", "/api/v1/sessions", {"project": "nova", "task": "x", "request_id": "req-c0000001"}, token=self.token)
        self.assertEqual(status, 501)

    def test_security_headers_and_no_server_banner_details(self):
        _, _, resp = self.call("GET", "/api/v1/sessions", token=self.token)
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(resp.getheader("X-Frame-Options"), "DENY")
        self.assertNotIn("Python", resp.getheader("Server") or "")


class TestProjectRegistry(LogicalCase):
    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "nova")
        os.makedirs(self.repo)

    def test_registered_project_resolves_to_its_realpath(self):
        self.assertTrue(CORE.project_add("nova", self.repo)[0])
        real, project, why = CORE.project_resolve("nova")
        self.assertEqual(real, os.path.realpath(self.repo))
        self.assertIsNone(why)

    def test_unknown_names_paths_and_traversal_are_rejected(self):
        CORE.project_add("nova", self.repo)
        for bad in ("vertex", "../nova", "nova/..", "/tmp", self.repo, "..", "", None, "NOVA", "nova\x00", "nova ", "-x"):
            self.assertIsNone(CORE.project_resolve(bad)[0], repr(bad))

    def test_bad_registrations_are_refused(self):
        self.assertFalse(CORE.project_add("../evil", self.repo)[0])
        self.assertFalse(CORE.project_add("Nova", self.repo)[0])
        self.assertFalse(CORE.project_add("nova", "relative/path")[0])
        self.assertFalse(CORE.project_add("nova", os.path.join(self.tmp, "missing"))[0])
        self.assertFalse(CORE.project_add("root", "/")[0])
        self.assertFalse(CORE.project_add("home", os.path.expanduser("~"))[0])
        self.assertFalse(CORE.project_add("th", self.home)[0])
        self.assertTrue(CORE.project_add("nova", self.repo)[0])
        self.assertFalse(CORE.project_add("nova", self.repo)[0])

    def test_symlink_retargeted_after_registration_is_refused(self):
        secret = os.path.join(self.tmp, "elsewhere")
        os.makedirs(secret)
        link = os.path.join(self.tmp, "link")
        os.symlink(self.repo, link)
        self.assertTrue(CORE.project_add("nova", link)[0])
        self.assertEqual(CORE.project_resolve("nova")[0], os.path.realpath(self.repo))
        os.unlink(link)
        os.symlink(secret, link)  # swap the symlink to point outside
        real, _, why = CORE.project_resolve("nova")
        self.assertIsNone(real)
        self.assertIn("changed", why)

    def test_symlinked_directory_is_pinned_to_its_real_target(self):
        link = os.path.join(self.tmp, "link2")
        os.symlink(self.repo, link)
        CORE.project_add("nova", link)
        self.assertEqual(CORE.projects_load()["nova"]["realpath"], os.path.realpath(self.repo))

    def test_disabled_project_does_not_resolve(self):
        CORE.project_add("nova", self.repo)
        CORE.projects_update(lambda p: p["nova"].update(enabled=False))
        self.assertIsNone(CORE.project_resolve("nova")[0])


class TestPermissionProfiles(LogicalCase):
    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "nova")
        os.makedirs(self.repo)
        CORE.project_add("nova", self.repo)

    def test_the_starter_profile_is_valid_but_not_applied_automatically(self):
        self.assertEqual(CORE.validate_permission_profile(CORE.starter_permission_profile()), [])
        self.assertIsNone(CORE.projects_load()["nova"]["permissions"])
        self.assertFalse(CORE.project_set_remote_launch("nova", True)[0])  # fail closed: none configured

    def test_invalid_profiles_are_rejected(self):
        cases = {
            "not an object": [],
            "bypass mode key": good_profile(defaultMode="bypassPermissions"),
            "permission_mode key": good_profile(permission_mode="acceptEdits"),
            "dangerously": good_profile(dangerously_skip_permissions=True),
            "empty allow": good_profile(allow=[]),
            "no allow": {k: v for k, v in good_profile().items() if k != "allow"},
            "bare Bash": good_profile(allow=["Bash"]),
            "Bash star": good_profile(allow=["Bash(*)"]),
            "Bash colon star": good_profile(allow=["Bash(:*)"]),
            "bare Edit": good_profile(allow=["Edit"]),
            "rm": good_profile(allow=["Bash(rm:*)"]),
            "sudo": good_profile(allow=["Bash(sudo make:*)"]),
            "git push": good_profile(allow=["Bash(git push:*)"]),
            "kubectl": good_profile(allow=["Bash(kubectl apply:*)"]),
            "curl": good_profile(allow=["Bash(curl:*)"]),
            "comma rule": good_profile(allow=["Bash(git log,rm)"]),
            "malformed rule": good_profile(allow=["Bash(git status"]),
            "non string rule": good_profile(allow=[1]),
            "missing gates": good_profile(human_gate=["production deployment"]),
            "no gates": good_profile(human_gate=[]),
            "bad name": good_profile(profile="Bad Name!"),
        }
        for label, profile in cases.items():
            with self.subTest(label):
                self.assertTrue(CORE.validate_permission_profile(profile), label)
                if isinstance(profile, dict):
                    self.assertFalse(CORE.project_set_permissions("nova", profile)[0])
        self.assertIsNone(CORE.projects_load()["nova"]["permissions"])

    def test_a_valid_profile_can_be_saved_then_enabled(self):
        ok, problems = CORE.project_set_permissions("nova", good_profile())
        self.assertTrue(ok, problems)
        self.assertFalse(CORE.projects_load()["nova"]["remote_launch"])  # explicit enable required
        self.assertTrue(CORE.project_set_remote_launch("nova", True)[0])

    def test_changing_the_profile_disables_remote_launch(self):
        CORE.project_set_permissions("nova", good_profile())
        CORE.project_set_remote_launch("nova", True)
        CORE.project_set_permissions("nova", good_profile(allow=["Read", "Grep"]))
        self.assertFalse(CORE.projects_load()["nova"]["remote_launch"])

    def test_tampering_with_a_validated_profile_fails_closed(self):
        CORE.project_set_permissions("nova", good_profile())
        CORE.project_set_remote_launch("nova", True)

        def tamper(projects):
            projects["nova"]["permissions"]["profile"]["allow"].append("Bash(git status:*)")

        CORE.projects_update(tamper)
        _, why = CORE.project_permissions_ok(CORE.projects_load()["nova"])
        self.assertIn("changed since", why)

        def tamper2(projects):
            projects["nova"]["permissions"]["profile"]["allow"] = ["Bash"]

        CORE.projects_update(tamper2)
        self.assertIsNone(CORE.project_permissions_ok(CORE.projects_load()["nova"])[0])

    def test_cli_show_validate_edit(self):
        env = self.env()
        code, out, _ = run_th(["project", "permissions", "show", "nova"], env=env)
        self.assertIn("no profile configured", out)
        code, out, _ = run_th(["project", "permissions", "validate", "nova"], env=env)
        self.assertEqual(code, 3)
        path = os.path.join(self.tmp, "profile.json")
        json.dump(good_profile(allow=["Bash"]), open(path, "w"))
        code, out, _ = run_th(["project", "permissions", "edit", "nova", "--from-file", path], env=env)
        self.assertEqual(code, 3)
        self.assertIn("unrestricted", out)
        json.dump(good_profile(), open(path, "w"))
        code, out, _ = run_th(["project", "permissions", "edit", "nova", "--from-file", path], env=env)
        self.assertEqual(code, 0, out)
        code, out, _ = run_th(["project", "permissions", "validate", "nova"], env=env)
        self.assertEqual((code, out.strip()), (0, "valid"))
        code, out, _ = run_th(["project", "enable-remote", "nova"], env=env)
        self.assertEqual(code, 0)
        code, out, _ = run_th(["project", "list"], env=env)
        self.assertEqual(json.loads(out)[0]["remote_launch"], True)

    def test_enable_remote_without_a_profile_is_refused_via_the_cli(self):
        code, out, _ = run_th(["project", "enable-remote", "nova"], env=self.env())
        self.assertEqual(code, 3)
        self.assertIn("no remote permission profile", out)


class TestDeviceCli(LogicalCase):
    def test_enroll_list_revoke(self):
        env = self.env()
        code, out, _ = run_th(["remote", "enroll-device", "--name", "John iPhone"], env=env)
        self.assertEqual(code, 0)
        self.assertIn("thc_", out)
        self.assertNotIn("thd_", out)
        code = None
        import re

        enrol_code = re.search(r"thc_[A-Za-z0-9_-]+", out).group(0)
        device_id, token = CORE.device_enroll_complete(enrol_code)
        code, out, _ = run_th(["remote", "list-devices"], env=env)
        rows = json.loads(out)
        self.assertEqual(rows[0]["device_id"], device_id)
        self.assertFalse(rows[0]["revoked"])
        code, out, _ = run_th(["remote", "revoke-device", "--device", device_id], env=env)
        self.assertEqual(code, 0)
        self.assertIsNone(CORE.device_authenticate(token)[0])
        self.assertEqual(run_th(["remote", "revoke-device", "--device", "d_nope"], env=env)[0], 3)

    def test_serve_refuses_when_unconfigured(self):
        code, out, _ = run_th(["remote", "serve"], env=self.env())
        self.assertEqual(code, 3)
        self.assertIn("refusing to start", out)

    def test_configure_sets_a_complete_configuration(self):
        env = self.env()
        code, out, _ = run_th(["remote", "configure", "--host", HOST, "--tailscale-user", LOGIN], env=env)
        self.assertEqual(code, 0, out)
        self.assertEqual(CORE.remote_config_problems(CORE.remote_config()), [])
        self.assertEqual(os.stat(CORE.remote_config_path()).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
