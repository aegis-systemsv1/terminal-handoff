"""The mobile interface: what the gateway serves, and what the page does."""

import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import CORE  # noqa: E402
from test_remote_api import HOST, LOGIN, RemoteCase  # noqa: E402

HARNESS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui", "harness.js")
NODE = shutil.which("node")
LSID = "ls_" + "a" * 24


def view(**over):
    base = {
        "logical_session_id": LSID, "project": "Nova", "state": "RUNNING", "branch": "main", "title": "Audit retrieval latency",
        "created_utc": "2026-01-01T00:00:00Z", "owner": {"generation": 3, "epoch": 3}, "stop": {"active": False}, "paused": False,
        "remote_control": {"state": "healthy"}, "inbox": {"pending": 1, "delivered": 0, "acked": 2},
        "recent_output": [{"ts": "t", "text": "reading tests"}], "human_gate": None, "approvals": [], "agent_poll_age_seconds": 4.0,
        "project_available": True, "orphaned": None,
    }
    base.update(over)
    return base


def run_ui(hash_, routes, steps=None):
    payload = {"js": CORE.UI_APP_JS, "hash": hash_, "routes": routes, "steps": steps or []}
    proc = subprocess.run([NODE, HARNESS], input=json.dumps(payload).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    assert proc.returncode == 0, proc.stderr.decode()
    return json.loads(proc.stdout.decode())


ME = ("GET /api/v1/me", [200, {"device": "phone", "csrf": "csrf-token-1"}])


class TestServedAssets(RemoteCase):
    def test_pages_are_served_with_a_strict_csp_and_no_inline_script(self):
        status, html, resp = self.call("GET", "/")
        self.assertEqual(status, 200)
        csp = resp.getheader("Content-Security-Policy")
        for directive in ("default-src 'none'", "script-src 'self'", "style-src 'self'", "frame-ancestors 'none'", "base-uri 'none'", "form-action 'none'"):
            self.assertIn(directive, csp)
        self.assertNotIn("unsafe-inline", csp)
        self.assertNotIn("unsafe-eval", csp)
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        self.assertEqual(resp.getheader("X-Frame-Options"), "DENY")
        self.assertIn('src="/app.js"', html)
        self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)[^>]*>")  # no inline script
        self.assertNotIn("onclick=", html)
        self.assertNotIn(" style=", html)
        self.assertIn('name="viewport"', html)
        for path, kind in (("/app.js", "javascript"), ("/app.css", "css")):
            status, body, resp = self.call("GET", path)
            self.assertEqual(status, 200)
            self.assertIn(kind, resp.getheader("Content-Type"))

    def test_the_page_still_needs_the_private_network_identity(self):
        for path in ("/", "/app.js", "/app.css"):
            self.assertEqual(self.call("GET", path, login=None)[0], 403, path)
            self.assertEqual(self.call("GET", path, login="mallory@example.com")[0], 403, path)
            self.assertEqual(self.call("GET", path, host="evil.example.com")[0], 403, path)

    def test_the_page_contains_no_secret_or_path(self):
        _, html, _ = self.call("GET", "/")
        _, js, _ = self.call("GET", "/app.js")
        joined = json.dumps(html) + json.dumps(js)
        for forbidden in (self.home, "thd_", "/Us" + "ers/", "thc_"):
            self.assertNotIn(forbidden, joined)

    def test_the_api_is_still_authenticated(self):
        self.call("GET", "/")
        self.assertEqual(self.call("GET", "/api/v1/sessions")[0], 401)


class TestStaticRules(unittest.TestCase):
    def test_the_script_never_writes_html_or_stores_credentials(self):
        js = CORE.UI_APP_JS
        for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "localStorage", "sessionStorage", "document.cookie", "console.", "XMLHttpRequest", "window.open", "postMessage"):
            self.assertNotIn(forbidden, js, forbidden)
        self.assertIn("textContent", js)
        self.assertNotRegex(js, r"setAttribute\(\s*['\"]style")
        self.assertNotRegex(js, r"['\"]on[a-z]+['\"]\s*:\s*['\"]")  # no string event handlers

    def test_the_only_network_calls_are_same_origin_api_paths(self):
        for path in re.findall(r"api\('(?:GET|POST)',\s*'([^']+)'", CORE.UI_APP_JS):
            self.assertTrue(path.startswith("/api/v1/"), path)
        self.assertNotIn("http://", CORE.UI_APP_JS)
        self.assertNotIn("https://", CORE.UI_APP_JS)

    def test_no_control_takes_a_path_command_or_pid(self):
        js = CORE.UI_APP_JS
        for word in ("cwd", "'path'", "'command'", "'pid'", "shell", "argv"):
            self.assertNotIn(word, js, word)

    def test_syntax_is_valid(self):
        if not NODE:
            self.skipTest("node is not installed")
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
            handle.write(CORE.UI_APP_JS)
        try:
            proc = subprocess.run([NODE, "--check", handle.name], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        finally:
            os.unlink(handle.name)


@unittest.skipUnless(NODE, "node is not installed")
class TestScreens(unittest.TestCase):
    def test_an_unenrolled_device_is_shown_the_enrollment_screen(self):
        out = run_ui("", {"GET /api/v1/me": [401, {"error": "unauthorized"}]})
        self.assertIn("Enroll this device", out["all"])
        self.assertIn("Enroll this device", out["buttons"])
        self.assertEqual(out["inputs"], ["input"])

    def test_enrollment_posts_only_the_code(self):
        routes = {"GET /api/v1/me": [401, {}], "POST /api/v1/enroll": [200, {"csrf": "c"}]}
        out = run_ui("", routes, [{"type": "input", "value": "thc_abc"}, {"click": "Enroll this device"}])
        post = [c for c in out["calls"] if c["method"] == "POST"][0]
        self.assertEqual(post["body"], {"code": "thc_abc"})

    def test_the_session_list(self):
        sessions = [view(), view(logical_session_id="ls_" + "b" * 24, project="Vertex", state="WAITING_FOR_HUMAN", remote_control={"state": "degraded"})]
        out = run_ui("#/", {ME[0]: ME[1], "GET /api/v1/sessions": [200, {"sessions": sessions}]})
        text = out["all"]
        for expected in ("Active sessions", "Nova", "RUNNING", "Vertex", "WAITING FOR HUMAN", "Approval required", "Remote Control: Healthy", "Remote Control: Degraded"):
            self.assertIn(expected, text)
        self.assertIn("+ New Session", out["buttons"])

    def test_session_view_shows_everything_needed(self):
        out = run_ui("#/s/" + LSID, {ME[0]: ME[1], "GET /api/v1/sessions/" + LSID: [200, view()]})
        text = out["all"]
        for expected in ("Nova", "RUNNING", "Audit retrieval latency", "Branch: main", "Owner generation: 3", "Remote Control: Healthy", "Instructions pending: 1", "reading tests", "Claude is listening"):
            self.assertIn(expected, text)
        for button in ("Send", "Pause", "Resume", "STOP"):
            self.assertIn(button, out["buttons"])
        self.assertIn(["textarea", "placeholder", "Tell Claude…"], out["attrs"])

    def test_the_approval_card_shows_the_exact_action_and_binds_the_decision(self):
        gate = {"id": "ap_" + "1" * 16, "action": "Restart the Nova production service after deploying commit abc123.", "reason": "The updated service cannot take effect until restarted.", "nonce": "N0NCE", "status": "pending", "bound_epoch": 3}
        routes = {ME[0]: ME[1], "GET /api/v1/sessions/" + LSID: [200, view(state="WAITING_FOR_HUMAN", human_gate=gate)],
                  "POST /api/v1/sessions/%s/approvals/%s/approve" % (LSID, gate["id"]): [200, view()]}
        out = run_ui("#/s/" + LSID, routes, [{"click": "APPROVE"}])
        text = out["all"]
        for expected in ("APPROVAL REQUIRED", "Project: Nova", "Requested action:", gate["action"], "Reason:", gate["reason"], "not a Claude permission prompt"):
            self.assertIn(expected, text)
        self.assertIn("DENY", out["buttons"])
        self.assertIn("APPROVE", out["buttons"])
        post = [c for c in out["calls"] if c["method"] == "POST"][0]
        self.assertTrue(post["path"].endswith("/approvals/%s/approve" % gate["id"]))
        self.assertEqual(sorted(post["body"]), ["nonce", "owner_epoch", "request_id"])
        self.assertEqual((post["body"]["nonce"], post["body"]["owner_epoch"]), ("N0NCE", 3))
        self.assertEqual(post["headers"]["X-CSRF-Token"], "csrf-token-1")

    def test_a_stale_approval_is_reported_not_hidden(self):
        gate = {"id": "ap_" + "2" * 16, "action": "Deploy", "reason": "r", "nonce": "n", "status": "pending", "bound_epoch": 3}
        routes = {ME[0]: ME[1], "GET /api/v1/sessions/" + LSID: [200, view(state="WAITING_FOR_HUMAN", human_gate=gate)],
                  "POST /api/v1/sessions/%s/approvals/%s/deny" % (LSID, gate["id"]): [409, {"error": "refused", "reason": "stale: ownership changed"}]}
        out = run_ui("#/s/" + LSID, routes, [{"click": "DENY"}])
        self.assertIn("Refused: stale: ownership changed", out["all"])

    def test_stop_needs_a_deliberate_confirmation(self):
        routes = {ME[0]: ME[1], "GET /api/v1/sessions/" + LSID: [200, view()], "POST /api/v1/sessions/%s/stop" % LSID: [200, view(state="STOPPED")]}
        out = run_ui("#/s/" + LSID, routes, [{"click": "STOP"}])
        self.assertIn("Confirm STOP", out["buttons"])
        self.assertEqual([c for c in out["calls"] if c["method"] == "POST"], [])  # one tap does nothing
        out = run_ui("#/s/" + LSID, routes, [{"click": "STOP"}, {"click": "Confirm STOP"}])
        post = [c for c in out["calls"] if c["method"] == "POST"][0]
        self.assertTrue(post["path"].endswith("/stop"))
        self.assertEqual(post["body"]["hard"], False)

    def test_clearing_a_stop_needs_a_reason(self):
        routes = {ME[0]: ME[1], "GET /api/v1/sessions/" + LSID: [200, view(state="STOPPED", stop={"active": True})],
                  "POST /api/v1/sessions/%s/resume" % LSID: [200, view()]}
        out = run_ui("#/s/" + LSID, routes, [{"click": "Resume…"}, {"click": "Clear STOP"}])
        self.assertEqual([c for c in out["calls"] if c["method"] == "POST"], [])
        self.assertIn("Give a reason.", out["all"])
        out = run_ui("#/s/" + LSID, routes, [{"click": "Resume…"}, {"type": "input", "value": "reviewed the diff"}, {"click": "Clear STOP"}])
        post = [c for c in out["calls"] if c["method"] == "POST"][0]
        self.assertEqual((post["body"]["clear_stop"], post["body"]["reason"]), (True, "reviewed the diff"))

    def test_sending_an_instruction(self):
        routes = {ME[0]: ME[1], "GET /api/v1/sessions/" + LSID: [200, view()], "POST /api/v1/sessions/%s/instructions" % LSID: [202, {"message_id": "im_1", "seq": 2}]}
        out = run_ui("#/s/" + LSID, routes, [{"type": "textarea", "value": "also check the tests"}, {"click": "Send"}])
        post = [c for c in out["calls"] if c["method"] == "POST"][0]
        self.assertEqual(post["body"]["text"], "also check the tests")
        self.assertRegex(post["body"]["request_id"], r"^[A-Za-z0-9._:-]{8,80}$")

    def test_orphaned_sessions_offer_deliberate_recovery_only(self):
        out = run_ui("#/s/" + LSID, {ME[0]: ME[1], "GET /api/v1/sessions/" + LSID: [200, view(state="ORPHANED", orphaned={"reason": "the bound Claude process has exited"})]})
        self.assertIn("Claude stopped responding.", out["all"])
        self.assertIn("It will not be replaced automatically.", out["all"])
        self.assertIn("Re-check", out["buttons"])
        self.assertIn("Abandon", out["buttons"])

    def test_hostile_text_is_rendered_as_text(self):
        evil = "<img src=x onerror=alert(1)><script>alert(2)</script>"
        out = run_ui("#/s/" + LSID, {ME[0]: ME[1], "GET /api/v1/sessions/" + LSID: [200, view(title=evil, recent_output=[{"ts": "t", "text": evil}])]})
        self.assertIn(evil, out["all"])  # present verbatim as a text node
        self.assertEqual([a for a in out["attrs"] if a[1].startswith("on")], [])

    def test_new_session_offers_only_registry_projects_and_no_path_field(self):
        routes = {ME[0]: ME[1], "GET /api/v1/projects": [200, {"projects": ["nova", "vertex"]}],
                  "POST /api/v1/sessions": [201, view()]}
        out = run_ui("#/new", routes, [{"type": "textarea", "value": "Audit first. Do not deploy."}, {"click": "Start Session"}])
        self.assertEqual(sorted(set(out["inputs"])), ["select", "textarea"])  # no free-text path or command input
        self.assertIn("nova", out["texts"])
        post = [c for c in out["calls"] if c["method"] == "POST"][0]
        self.assertEqual(sorted(post["body"]), ["project", "request_id", "task"])
        self.assertEqual(post["body"]["project"], "nova")

    def test_a_failed_start_is_reported_plainly(self):
        routes = {ME[0]: ME[1], "GET /api/v1/projects": [200, {"projects": ["nova"]}],
                  "POST /api/v1/sessions": [503, {"error": "isolation_unverified", "reason": "permission isolation has not been verified"}]}
        out = run_ui("#/new", routes, [{"type": "textarea", "value": "task"}, {"click": "Start Session"}])
        self.assertIn("Not started: permission isolation has not been verified", out["all"])


if __name__ == "__main__":
    unittest.main()
