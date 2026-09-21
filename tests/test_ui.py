"""The mobile interface: what the gateway serves, and what the page does."""

import json
import os
import re
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import CORE  # noqa: E402
from test_remote_api import RemoteCase  # noqa: E402

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


def item(n, text, kind="output", generation=1):
    """One transcript item as the gateway serves it."""
    return {"id": ("h%d" % n) if kind == "event" else ("o%d" % n), "kind": kind, "ts": "2026-01-01T00:00:%02dZ" % min(n, 59),
            "text": text, "ordinal": n, "generation": generation}


def transcript_route(items, has_more=False, lsid=None, query="?limit=200"):
    path = "GET /api/v1/sessions/%s/transcript%s" % (lsid or LSID, query)
    return {path: [200, {"items": items, "has_more": has_more, "total": len(items)}]}


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

    def test_nothing_in_the_page_can_interfere_with_native_ios_paste(self):
        code = "\n".join(line.split("//")[0] for line in CORE.UI_APP_JS.splitlines())  # ignore comments
        for forbidden in ("paste", "clipboard", "beforeinput", "preventDefault", "stopPropagation", ".focus(", "setSelectionRange", ".select(",
                          "execCommand", "selectionStart", "selectionEnd", "contenteditable", "touchstart", "touchend", "pointerdown", "keydown", "keyup",
                          "addEventListener('input'", "oninput", "onpaste", "onbeforeinput"):
            self.assertNotIn(forbidden, code, forbidden)
        # the only listeners on the instruction box merely record a timestamp
        self.assertEqual(sorted(re.findall(r"text\.addEventListener\('([a-z]+)'", code)), ["blur", "focus"])
        for body in re.findall(r"text\.addEventListener\('[a-z]+', function \(\) \{([^}]*)\}", code):
            self.assertEqual(body.strip(), "lastFocusEvent = Date.now();")
        # the box's value is written in exactly one place: clearing it after a successful Send
        self.assertEqual(re.findall(r"\btext\.value\s*=[^=]", code), ["text.value = "])
        self.assertEqual(re.findall(r"\btext\.value\s*=\s*''", code), ["text.value = ''"])  # and it is only ever set to empty

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
        routes = {ME[0]: ME[1], "GET /api/v1/sessions/" + LSID: [200, view()]}
        routes.update(transcript_route([item(1, "reading tests")]))
        out = run_ui("#/s/" + LSID, routes)
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

    # ---- the instruction box must survive polling (iPhone Safari copy/paste, keyboard, draft) ----

    def session_routes(self, **over):
        routes = {ME[0]: ME[1], "GET /api/v1/sessions/" + LSID: [200, view(**over)],
                  "POST /api/v1/sessions/%s/instructions" % LSID: [202, {"message_id": "im_1", "seq": 2}]}
        routes.update(transcript_route([item(1, "reading tests")]))
        return routes

    def newer(self, text="a brand new output line", **over):
        # Each distinct line is a distinct transcript item, exactly as the gateway numbers them.
        self._seq = getattr(self, "_seq", 1) + 1
        self._lines = getattr(self, "_lines", [item(1, "reading tests")]) + [item(self._seq, text)]
        routes = {"GET /api/v1/sessions/" + LSID: [200, view(recent_output=[{"ts": "t", "text": text}], **over)]}
        routes.update(transcript_route(list(self._lines)))
        return {"routes": routes}

    def box(self, snap):
        return [b for b in snap["boxes"] if b["tag"] == "textarea"]

    def test_the_instruction_box_is_never_recreated_by_polling(self):
        steps = [{"snap": "a"}, {"type": "textarea", "value": "half-typed draft"}, {"snap": "b"}, self.newer(), {"poll": True}, {"snap": "c"},
                 self.newer("and another one"), {"poll": True}, {"poll": True}, {"snap": "d"}]  # never focused: refreshes freely
        out = run_ui("#/s/" + LSID, self.session_routes(), steps)["snaps"]
        first = self.box(out["a"])
        self.assertEqual(len(first), 1)
        for later in ("b", "c", "d"):
            self.assertEqual(self.box(out[later]), [dict(first[0], value=self.box(out[later])[0]["value"])])  # same node
        self.assertEqual(self.box(out["d"])[0]["value"], "half-typed draft")  # the draft never disappears
        self.assertIn("and another one", out["d"]["text"])  # the page still refreshes around it

    def test_an_empty_focused_box_is_not_replaced_either(self):
        """The original bug: an empty box with the paste menu open was rebuilt on every poll."""
        steps = [{"snap": "a"}, {"focus": "textarea"}, self.newer(), {"poll": True}, {"poll": True}, {"snap": "b"}]
        out = run_ui("#/s/" + LSID, self.session_routes(), steps)["snaps"]
        self.assertEqual(self.box(out["a"])[0]["uid"], self.box(out["b"])[0]["uid"])

    def test_the_layout_is_held_still_while_focused_and_for_a_quiet_period_after(self):
        """iOS can blur the box while its own paste dialog is up: nothing may shift around it then.

        The transcript is exempt: it is a fixed-height container that is only ever appended
        to, so it never reflows the box, and holding it back would defeat live follow.
        """
        steps = [{"focus": "textarea"}, {"type": "textarea", "value": "pasted text"}, {"snap": "a"},
                 self.newer("streamed while typing", title="changed while typing"), {"poll": True},
                 {"snap": "b"}, {"blur": True}, {"poll": True}, {"snap": "c"}, {"advance": 9000}, {"poll": True}, {"snap": "d"}]
        out = run_ui("#/s/" + LSID, self.session_routes(), steps)["snaps"]
        self.assertNotIn("changed while typing", out["b"]["text"])  # focused: the details do not shift
        self.assertNotIn("changed while typing", out["c"]["text"])  # just blurred (the paste dialog case): still held
        self.assertIn("changed while typing", out["d"]["text"])  # applied by a later poll once quiet
        self.assertIn("streamed while typing", out["b"]["text"])  # the transcript keeps up regardless
        self.assertEqual([b["uid"] for b in self.box(out["a"])], [b["uid"] for b in self.box(out["d"])])
        self.assertEqual(self.box(out["d"])[0]["value"], "pasted text")

    def test_something_the_user_must_not_miss_is_shown_even_while_typing(self):
        gate = {"id": "ap_" + "3" * 16, "action": "Deploy build abc123", "reason": "needed", "nonce": "n", "status": "pending", "bound_epoch": 3}
        steps = [{"focus": "textarea"}, {"type": "textarea", "value": "my draft"}, {"snap": "a"},
                 self.newer(state="WAITING_FOR_HUMAN", human_gate=gate), {"poll": True}, {"snap": "b"}]
        out = run_ui("#/s/" + LSID, self.session_routes(), steps)["snaps"]
        self.assertIn("APPROVAL REQUIRED", out["b"]["text"])
        self.assertIn("Deploy build abc123", out["b"]["text"])
        self.assertEqual(self.box(out["a"])[0]["uid"], self.box(out["b"])[0]["uid"])
        self.assertEqual(self.box(out["b"])[0]["value"], "my draft")

    def test_send_clears_on_success_and_keeps_the_draft_on_failure(self):
        ok = run_ui("#/s/" + LSID, self.session_routes(), [{"type": "textarea", "value": "do the thing"}, {"click": "Send"}, {"snap": "s"}])["snaps"]
        self.assertEqual(self.box(ok["s"])[0]["value"], "")
        routes = self.session_routes()
        routes["POST /api/v1/sessions/%s/instructions" % LSID] = [409, {"error": "refused", "reason": "session is STOPPED"}]
        bad = run_ui("#/s/" + LSID, routes, [{"type": "textarea", "value": "do the thing"}, {"click": "Send"}, {"snap": "s"}])
        self.assertEqual(self.box(bad["snaps"]["s"])[0]["value"], "do the thing")  # kept for a retry
        self.assertIn("Refused: session is STOPPED", bad["all"])
        gone = self.session_routes()
        gone["POST /api/v1/sessions/%s/instructions" % LSID] = [401, {"error": "unauthorized"}]
        auth = run_ui("#/s/" + LSID, gone, [{"type": "textarea", "value": "keep me"}, {"click": "Send"}])
        self.assertTrue(any(c["path"].endswith("/instructions") for c in auth["calls"]))

    def test_a_reason_being_typed_in_the_stop_panel_survives_polling(self):
        routes = self.session_routes(state="STOPPED", stop={"active": True})
        steps = [{"click": "Resume\u2026"}, {"type": "input", "value": "reviewed the diff carefully"}, {"snap": "a"}, self.newer(state="STOPPED", stop={"active": True}),
                 {"poll": True}, {"poll": True}, {"snap": "b"}]
        out = run_ui("#/s/" + LSID, routes, steps)["snaps"]
        def inputs(snap):
            return [b for b in snap["boxes"] if b["tag"] == "input"]

        self.assertEqual(inputs(out["a"]), inputs(out["b"]))
        self.assertEqual(inputs(out["b"])[0]["value"], "reviewed the diff carefully")

    def test_unchanged_data_causes_no_redraw_and_the_box_has_no_ios_text_mangling(self):
        out = run_ui("#/s/" + LSID, self.session_routes(), [{"poll": True}, {"poll": True}])
        attrs = {a[1]: a[2] for a in out["attrs"] if a[0] == "textarea"}
        self.assertEqual((attrs["autocapitalize"], attrs["autocorrect"], attrs["spellcheck"]), ("off", "off", "false"))

    def test_the_session_page_polling_and_controls_still_work(self):
        routes = self.session_routes()
        routes["POST /api/v1/sessions/%s/pause" % LSID] = [200, view(state="PAUSED", paused=True)]
        out = run_ui("#/s/" + LSID, routes, [{"poll": True}, {"click": "Pause"}])
        self.assertTrue([c for c in out["calls"] if c["method"] == "POST" and c["path"].endswith("/pause")])
        self.assertGreaterEqual(len([c for c in out["calls"] if c["method"] == "GET" and c["path"].endswith(LSID)]), 3)  # initial + poll + refresh

    def test_hostile_text_is_rendered_as_text(self):
        evil = "<img src=x onerror=alert(1)><script>alert(2)</script>"
        out = run_ui("#/s/" + LSID, {ME[0]: ME[1], "GET /api/v1/sessions/" + LSID: [200, view(title=evil, recent_output=[{"ts": "t", "text": evil}])]})
        self.assertIn(evil, out["all"])  # present verbatim as a text node
        self.assertEqual([a for a in out["attrs"] if a[1].startswith("on")], [])

    def test_new_session_offers_only_registry_projects_and_no_path_field(self):
        routes = {ME[0]: ME[1], "GET /api/v1/projects": [200, {"projects": ["nova", "vertex"]}],
                  "POST /api/v1/sessions": [201, view()]}
        out = run_ui("#/new", routes, [{"type": "textarea", "value": "Audit first. Do not deploy."}, {"click": "Start Session"}])
        self.assertEqual(sorted(set(out["inputs"])), ["input", "select", "textarea"])
        labels = [a[2] for a in out["attrs"] if a[1] == "aria-label"]
        self.assertEqual(sorted(labels), ["Agent", "Project", "Session name", "Task"])  # Agent is a fixed choice; the one text input is the display name, never a path or command
        self.assertIn("nova", out["texts"])
        post = [c for c in out["calls"] if c["method"] == "POST"][0]
        self.assertEqual(sorted(post["body"]), ["project", "request_id", "task"])
        self.assertEqual(post["body"]["project"], "nova")

    def test_new_session_defaults_to_claude_and_sends_grok_only_when_chosen(self):
        routes = {ME[0]: ME[1], "GET /api/v1/projects": [200, {"projects": ["scratch"], "agents": {"scratch": ["claude", "grok"]}}],
                  "POST /api/v1/sessions": [201, view()]}
        default = run_ui("#/new", routes, [{"type": "textarea", "value": "task"}, {"click": "Start Session"}])
        self.assertIn("Claude Code", default["texts"])
        self.assertIn("Grok", default["texts"])
        self.assertNotIn("agent", [c for c in default["calls"] if c["method"] == "POST"][0]["body"])  # exactly the v1.4.2 request
        grok = run_ui("#/new", routes, [{"aria": "Agent", "value": "grok"}, {"type": "textarea", "value": "task"},
                                        {"input": None} if False else {"type": "input", "value": "Grok iPhone Test"}, {"click": "Start Session"}])
        body = [c for c in grok["calls"] if c["method"] == "POST"][0]["body"]
        self.assertEqual((body["agent"], body["project"], body["name"]), ("grok", "scratch", "Grok iPhone Test"))
        self.assertEqual(sorted(body), ["agent", "name", "project", "request_id", "task"])

    def test_a_grok_session_is_labelled_and_a_claude_session_reads_as_before(self):
        grok = view(agent_type="grok", grok={"session_bound": True, "acp": "running", "automatic_handoff": False}, remote_control={"state": "unknown"})
        claude = view(logical_session_id="ls_" + "b" * 24)
        lst = run_ui("#/", {ME[0]: ME[1], "GET /api/v1/sessions": [200, {"sessions": [grok, claude]}]})
        self.assertIn("Project: Nova \u00b7 Grok", lst["all"])
        self.assertIn("Project: Nova \u00b7 Claude Code", lst["all"])
        self.assertEqual(lst["all"].count("Remote Control:"), 1)  # the Claude card only: Grok has no Remote Control
        page = run_ui("#/s/" + LSID, {ME[0]: ME[1], "GET /api/v1/sessions/" + LSID: [200, grok]})
        self.assertIn("Agent: Grok", page["all"])
        self.assertIn("no automatic handoff", page["all"])
        self.assertNotIn("Remote Control:", page["all"])
        self.assertIn(["textarea", "placeholder", "Tell Grok\u2026"], page["attrs"])
        old = run_ui("#/s/" + LSID, {ME[0]: ME[1], "GET /api/v1/sessions/" + LSID: [200, view()]})  # no agent_type at all: a v1.4.2 payload
        self.assertIn(["textarea", "placeholder", "Tell Claude\u2026"], old["attrs"])
        self.assertIn("Claude is listening", old["all"])
        self.assertIn("Remote Control: Healthy", old["all"])

    def test_a_grok_approval_and_orphan_screens_name_grok(self):
        gate = {"id": "ap_" + "a" * 16, "nonce": "n", "action": "Grok: Write probe.txt", "reason": "Grok is asking to run a edit action."}
        page = run_ui("#/s/" + LSID, {ME[0]: ME[1], "GET /api/v1/sessions/" + LSID: [200, view(agent_type="grok", human_gate=gate, state="WAITING_FOR_HUMAN")]})
        self.assertIn("Approving allows this one Grok action only.", page["all"])
        self.assertNotIn("not a Claude permission prompt", page["all"])
        orphan = run_ui("#/s/" + LSID, {ME[0]: ME[1], "GET /api/v1/sessions/" + LSID: [200, view(agent_type="grok", state="ORPHANED", orphaned={"reason": "gone"})]})
        self.assertIn("Grok stopped responding.", orphan["all"])

    def test_new_session_sends_a_name_only_when_one_is_given(self):
        routes = {ME[0]: ME[1], "GET /api/v1/projects": [200, {"projects": ["nova"]}], "POST /api/v1/sessions": [201, view()]}
        blank = run_ui("#/new", routes, [{"type": "textarea", "value": "task"}, {"click": "Start Session"}])
        self.assertNotIn("name", [c for c in blank["calls"] if c["method"] == "POST"][0]["body"])
        named = run_ui("#/new", routes, [{"type": "input", "value": "  Nova Voice Fix "}, {"type": "textarea", "value": "task"}, {"click": "Start Session"}])
        self.assertEqual([c for c in named["calls"] if c["method"] == "POST"][0]["body"]["name"], "Nova Voice Fix")
        self.assertIn(["input", "maxlength", "60"], named["attrs"])

    def test_names_show_in_the_list_and_on_the_session_page(self):
        s = view(name="Nova Voice Fix")
        lst = run_ui("#/", {ME[0]: ME[1], "GET /api/v1/sessions": [200, {"sessions": [s, view(logical_session_id="ls_" + "b" * 24)]}]})
        self.assertIn("Nova Voice Fix", lst["all"])
        self.assertIn("Project: Nova", lst["all"])
        page = run_ui("#/s/" + LSID, {ME[0]: ME[1], "GET /api/v1/sessions/" + LSID: [200, s]})
        self.assertIn("Nova Voice Fix", page["all"])
        self.assertIn("Rename", page["buttons"])
        plain = run_ui("#/s/" + LSID, {ME[0]: ME[1], "GET /api/v1/sessions/" + LSID: [200, view()]})
        self.assertIn("Nova", plain["all"])  # no custom name: the default label

    def test_renaming_from_the_phone(self):
        routes = {ME[0]: ME[1], "GET /api/v1/sessions/" + LSID: [200, view(name="Remote scratch")],
                  "POST /api/v1/sessions/%s/rename" % LSID: [200, view(name="Nova Voice Fix")]}
        out = run_ui("#/s/" + LSID, routes, [{"click": "Rename"}, {"snap": "open"}, {"type": "input", "value": "Nova Voice Fix"}, {"click": "Save name"}])
        self.assertIn("Rename this session", out["snaps"]["open"]["text"])
        self.assertEqual([b["value"] for b in out["snaps"]["open"]["boxes"] if b["tag"] == "input"], ["Remote scratch"])  # prefilled
        post = [c for c in out["calls"] if c["method"] == "POST"][0]
        self.assertTrue(post["path"].endswith("/rename"))
        self.assertEqual(sorted(post["body"]), ["name", "request_id"])
        self.assertEqual(post["body"]["name"], "Nova Voice Fix")

    def test_the_rename_box_survives_polling_and_never_touches_the_instruction_box(self):
        routes = {ME[0]: ME[1], "GET /api/v1/sessions/" + LSID: [200, view()]}
        steps = [{"type": "textarea", "value": "half-typed instruction"}, {"click": "Rename"}, {"type": "input", "value": "New nam"}, {"snap": "a"},
                 self.newer("output moved on"), {"poll": True}, {"poll": True}, {"snap": "b"}]
        out = run_ui("#/s/" + LSID, routes, steps)["snaps"]
        self.assertEqual(out["a"]["boxes"], out["b"]["boxes"])  # both boxes: same nodes, same text
        self.assertEqual(sorted(b["value"] for b in out["b"]["boxes"]), ["New nam", "half-typed instruction"])

    def test_the_rename_panel_opens_at_the_top_where_the_button_is(self):
        """A phone user pressed Rename and saw nothing: the panel had opened below the fold."""
        routes = {ME[0]: ME[1], "GET /api/v1/sessions/" + LSID: [200, view(name="Remote scratch")]}
        out = run_ui("#/s/" + LSID, routes, [{"click": "Rename"}, {"snap": "open"}])["snaps"]["open"]["text"]
        self.assertIn("Rename this session", out)
        self.assertLess(out.index("Rename this session"), out.index("Current task"))  # above the details
        self.assertLess(out.index("Rename this session"), out.index("Transcript"))
        self.assertLess(out.index("Rename this session"), out.index("Instructions pending"))

    def test_saving_or_cancelling_closes_the_panel_and_a_refused_name_keeps_it_open(self):
        base = {ME[0]: ME[1], "GET /api/v1/sessions/" + LSID: [200, view(name="Old")]}
        ok = dict(base, **{"POST /api/v1/sessions/%s/rename" % LSID: [200, view(name="New")]})
        saved = run_ui("#/s/" + LSID, ok, [{"click": "Rename"}, {"type": "input", "value": "New"}, {"click": "Save name"}, {"snap": "s"}])["snaps"]["s"]
        self.assertNotIn("Rename this session", saved["text"])
        self.assertIn("Renamed.", saved["text"])
        cancelled = run_ui("#/s/" + LSID, ok, [{"click": "Rename"}, {"click": "Cancel"}, {"snap": "c"}])
        self.assertNotIn("Rename this session", cancelled["snaps"]["c"]["text"])
        self.assertEqual([c for c in cancelled["calls"] if c["method"] == "POST"], [])
        bad = dict(base, **{"POST /api/v1/sessions/%s/rename" % LSID: [400, {"error": "bad_request", "reason": "that name looks like an identifier or path"}]})
        refused = run_ui("#/s/" + LSID, bad, [{"click": "Rename"}, {"type": "input", "value": "../x"}, {"click": "Save name"}, {"snap": "r"}])["snaps"]["r"]
        self.assertIn("Refused: that name looks like an identifier or path", refused["text"])
        self.assertIn("Rename this session", refused["text"])  # still open so the name can be corrected

    def test_a_hostile_name_is_only_ever_text(self):
        evil = "<img src=x onerror=alert(1)>"
        out = run_ui("#/s/" + LSID, {ME[0]: ME[1], "GET /api/v1/sessions/" + LSID: [200, view(name=evil)]})
        self.assertIn(evil, out["all"])
        self.assertEqual([a for a in out["attrs"] if a[1].startswith("on")], [])

    def test_a_failed_start_is_reported_plainly(self):
        routes = {ME[0]: ME[1], "GET /api/v1/projects": [200, {"projects": ["nova"]}],
                  "POST /api/v1/sessions": [503, {"error": "isolation_unverified", "reason": "permission isolation has not been verified"}]}
        out = run_ui("#/new", routes, [{"type": "textarea", "value": "task"}, {"click": "Start Session"}])
        self.assertIn("Not started: permission isolation has not been verified", out["all"])


@unittest.skipIf(NODE is None, "node is required for the UI tests")
class TestTranscript(unittest.TestCase):
    """Reading the session on a phone: follow the latest, or hold still while you read."""

    GEOM = {"measure": {"lineHeight": 10, "clientHeight": 100, "toBottom": True}}

    def routes(self, items, **over):
        routes = {ME[0]: ME[1], "GET /api/v1/sessions/" + LSID: [200, view(**over)]}
        routes.update(transcript_route(items))
        return routes

    def start(self, count=30):
        return self.routes([item(n, "line %d" % n) for n in range(count)])

    def arrive(self, count, extra):
        """The next poll carries `extra` more lines."""
        items = [item(n, "line %d" % n) for n in range(count + extra)]
        return {"routes": transcript_route(items)}

    def test_the_transcript_shows_the_session_history_not_a_tiny_snapshot(self):
        out = run_ui("#/s/" + LSID, self.start(30), [self.GEOM])
        self.assertIn("Transcript", out["all"])
        self.assertEqual(len(out["transcript"]["lines"]), 30)
        self.assertIn("line 29", out["transcript"]["lines"][-1])

    def test_at_the_bottom_new_output_follows_automatically(self):
        steps = [self.GEOM, {"scrollTo": "bottom"}, self.arrive(30, 3), {"poll": True}, {"readTranscript": "after"}]
        after = run_ui("#/s/" + LSID, self.start(30), steps)["snaps"]["after"]
        self.assertEqual(len(after["lines"]), 33)
        self.assertTrue(after["atBottom"], "a reader already at the bottom must keep seeing the newest line")
        self.assertTrue(after["pillHidden"], "no catch-up prompt is needed while following")

    def test_scrolling_up_stops_the_auto_scroll_and_holds_the_reading_position(self):
        steps = [self.GEOM, {"scrollTo": 40}, {"readTranscript": "reading"},
                 self.arrive(30, 5), {"poll": True}, {"readTranscript": "after"}]
        snaps = run_ui("#/s/" + LSID, self.start(30), steps)["snaps"]
        self.assertEqual(snaps["reading"]["scrollTop"], 40)
        self.assertEqual(snaps["after"]["scrollTop"], 40, "incoming output must not move what the reader is looking at")
        self.assertFalse(snaps["after"]["atBottom"], "it must not drag the reader back down")
        self.assertEqual(len(snaps["after"]["lines"]), 35, "the new output is still received in the background")

    def test_new_output_while_reading_is_announced_and_counted(self):
        steps = [self.GEOM, {"scrollTo": 40}, self.arrive(30, 3), {"poll": True}, {"readTranscript": "after"}]
        after = run_ui("#/s/" + LSID, self.start(30), steps)["snaps"]["after"]
        self.assertFalse(after["pillHidden"])
        self.assertEqual(after["pillText"], "3 new updates ↓")

    def test_one_new_update_is_not_announced_in_the_plural(self):
        steps = [self.GEOM, {"scrollTo": 40}, self.arrive(30, 1), {"poll": True}, {"readTranscript": "after"}]
        self.assertEqual(run_ui("#/s/" + LSID, self.start(30), steps)["snaps"]["after"]["pillText"], "1 new update ↓")

    def test_jump_to_latest_returns_to_the_bottom_and_follows_again(self):
        steps = [self.GEOM, {"scrollTo": 40}, self.arrive(30, 3), {"poll": True},
                 {"click": "3 new updates ↓"}, {"readTranscript": "jumped"},
                 self.arrive(30, 6), {"poll": True}, {"readTranscript": "following"}]
        snaps = run_ui("#/s/" + LSID, self.start(30), steps)["snaps"]
        self.assertTrue(snaps["jumped"]["atBottom"])
        self.assertTrue(snaps["jumped"]["pillHidden"])
        self.assertTrue(snaps["following"]["atBottom"], "tapping it must restore live follow")
        self.assertEqual(len(snaps["following"]["lines"]), 36)

    def test_the_latest_button_is_always_available(self):
        steps = [self.GEOM, {"scrollTo": 40}, {"click": "↓ Latest"}, {"readTranscript": "after"}]
        self.assertTrue(run_ui("#/s/" + LSID, self.start(30), steps)["snaps"]["after"]["atBottom"])

    def test_polling_appends_and_never_rebuilds_the_transcript(self):
        steps = [self.GEOM, {"scrollTo": "bottom"}, self.arrive(30, 2), {"poll": True}, {"poll": True}, {"poll": True}]
        out = run_ui("#/s/" + LSID, self.start(30), steps)
        self.assertEqual(len(out["transcript"]["lines"]), 32, "repeated polls must not duplicate or rebuild lines")

    def test_lifecycle_events_are_marked_in_the_one_logical_history(self):
        items = [item(0, "generation 1 working"), item(1, "Handoff started", kind="event"),
                 item(2, "Generation 2 became owner", kind="event"), item(3, "generation 2 working", generation=2),
                 item(4, "STOPPED", kind="event"), item(5, "Resumed", kind="event")]
        out = run_ui("#/s/" + LSID, self.routes(items), [self.GEOM])
        for marker in ("Handoff started", "Generation 2 became owner", "STOPPED", "Resumed"):
            self.assertIn("— %s —" % marker, out["transcript"]["lines"])
        self.assertIn("generation 1 working", out["transcript"]["lines"])
        self.assertIn("generation 2 working", out["transcript"]["lines"])

    def test_older_history_is_fetched_lazily_when_the_reader_scrolls_to_the_top(self):
        routes = self.routes([item(n, "line %d" % n) for n in range(10, 30)])
        routes.update(transcript_route([item(n, "line %d" % n) for n in range(0, 10)], query="?limit=200&before=10"))
        steps = [self.GEOM, {"scrollTo": 0}, {"readTranscript": "after"}]
        after = run_ui("#/s/" + LSID, routes, steps)["snaps"]["after"]
        self.assertEqual(len(after["lines"]), 30, "older output is prepended")
        self.assertIn("line 0", after["lines"][0])
        self.assertGreater(after["scrollTop"], 0, "prepending must not throw the reader to the very top")

    def test_the_instruction_box_survives_the_transcript_updating(self):
        steps = [self.GEOM, {"type": "textarea", "value": "draft I am still writing"}, {"focus": "textarea"},
                 self.arrive(30, 4), {"poll": True}, {"poll": True}, {"snap": "after"}]
        out = run_ui("#/s/" + LSID, self.start(30), steps)
        boxes = [b for b in out["snaps"]["after"]["boxes"] if b["tag"] == "textarea"]
        self.assertEqual(len(boxes), 1)
        self.assertEqual(boxes[0]["value"], "draft I am still writing", "the draft must survive transcript updates")
        self.assertEqual(len(out["transcript"]["lines"]), 34, "the transcript still updates while the box is focused")


@unittest.skipIf(NODE is None, "node is required for the UI tests")
class TestSessionCleanup(unittest.TestCase):
    """Old sessions can be cleared off the phone; live ones cannot."""

    def listing(self, sessions, **extra):
        routes = {ME[0]: ME[1], "GET /api/v1/sessions": [200, {"sessions": sessions}]}
        routes.update(extra)
        return routes

    def test_the_list_separates_active_closed_and_archived(self):
        sessions = [view(name="Live one", state="RUNNING"),
                    view(logical_session_id="ls_" + "b" * 24, name="Done one", state="COMPLETED"),
                    view(logical_session_id="ls_" + "c" * 24, name="Old one", state="FAILED", archived={"utc": "2026-01-01T00:00:00Z", "by": "device:d_1"})]
        out = run_ui("#/", self.listing(sessions))
        for expected in ("Active sessions", "Recent / closed", "Archived", "Live one", "Done one", "Old one"):
            self.assertIn(expected, out["all"])

    def test_a_closed_session_can_be_removed_after_a_clear_confirmation(self):
        closed = view(logical_session_id="ls_" + "b" * 24, name="Done one", state="COMPLETED")
        routes = self.listing([closed], **{"POST /api/v1/sessions/%s/archive" % closed["logical_session_id"]: [200, dict(closed, archived={"utc": "t", "by": "device:d_1"})]})
        out = run_ui("#/", routes, [{"click": "Remove session"}, {"snap": "asked"}, {"click": "Remove session"}])
        self.assertIn("Remove this session from Terminal Handoff history?", out["snaps"]["asked"]["text"])
        self.assertIn("files and its repository are not touched", out["snaps"]["asked"]["text"])
        posts = [c for c in out["calls"] if c["method"] == "POST"]
        self.assertEqual([c["path"] for c in posts], ["/api/v1/sessions/%s/archive" % closed["logical_session_id"]])
        self.assertEqual(set(posts[0]["body"]), {"request_id"}, "removal carries no path, project or command")

    def test_an_active_session_offers_no_removal_control(self):
        for state in ("RUNNING", "WAITING_FOR_HUMAN", "PAUSED", "STOPPED"):
            out = run_ui("#/", self.listing([view(state=state)]))
            self.assertNotIn("Remove session", out["buttons"], "%s must not be removable from the list" % state)

    def test_an_archived_session_can_be_restored(self):
        old = view(logical_session_id="ls_" + "c" * 24, name="Old one", state="COMPLETED", archived={"utc": "t", "by": "device:d_1"})
        routes = self.listing([old], **{"POST /api/v1/sessions/%s/restore" % old["logical_session_id"]: [200, view(state="COMPLETED")]})
        out = run_ui("#/", routes, [{"click": "Restore"}])
        self.assertEqual([c["path"] for c in out["calls"] if c["method"] == "POST"], ["/api/v1/sessions/%s/restore" % old["logical_session_id"]])

    def test_a_refusal_is_shown_plainly(self):
        closed = view(logical_session_id="ls_" + "b" * 24, state="COMPLETED")
        routes = self.listing([closed], **{"POST /api/v1/sessions/%s/archive" % closed["logical_session_id"]: [409, {"error": "refused", "reason": "this session still has a live owner"}]})
        out = run_ui("#/", routes, [{"click": "Remove session"}, {"click": "Remove session"}, {"snap": "after"}])
        self.assertIn("Refused: this session still has a live owner", out["snaps"]["after"]["text"])


if __name__ == "__main__":
    unittest.main()
