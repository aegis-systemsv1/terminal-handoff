"""Very large prompts, end to end and byte-exact.

The task travels as request data, is stored exactly in a per-session payload file (atomic, 0600),
and reaches the agent by reference: Claude reads it part by part through `session task --part N`;
Grok is sent the exact stored text over ACP. Nothing about it is ever put in an argv, a URL, a
launch script, a log, a window title or the inbox record beyond a short pointer.

Every round trip here compares SHA-256 of what was submitted with what the agent side receives.
"""

import hashlib
import http.client
import io
import json
import os
import random
import stat
import subprocess
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import CORE, PYTHON, TH_SCRIPT  # noqa: E402
from test_remote_api import HOST, LOGIN  # noqa: E402
from test_remote_launch import AGENT, LaunchCase  # noqa: E402

CTX = {"device_id": "d_0123456789abcdef"}
KB = 1024


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sized(unit, nbytes):
    """Text made of `unit` repeated, then padded with ASCII, whose UTF-8 length is exactly `nbytes`."""
    out, total = [], 0
    while total + len(unit.encode("utf-8")) <= nbytes:
        out.append(unit)
        total += len(unit.encode("utf-8"))
    return "".join(out) + "p" * (nbytes - total)


PROSE = "The quick brown fox jumps over the lazy dog; Nova's cache-key is 'k1' (not \"k2\").\n"
UNICODE = "Grüße, 世界 — 日本語のテキスト 🚀🔥👩🏽‍💻 café e\u0301 שלום مرحبا \u200d\u2028 end\n"
MARKDOWN = (
    "# Title\n\n- item one\n- item **two** with `inline code`\n\n```python\ndef f(x):\n    return \"$(touch PWNED)\" + 'it\\'s'\n```\n\n"
    "```bash\n$ echo \"hello\" && ls `pwd`\n<<'EOF'\nEOF\n```\n\n> quote\tTabbed\r\nCRLF line\r\n"
)
SHELLISH = (
    "$(touch PWNED_SUBST)\n`touch PWNED_TICK`\n; touch PWNED_SEMI ;\n'; touch PWNED_QUOTE #\n\"$IFS\"${IFS}\n"
    "--dangerously-skip-permissions\n-flag $HOME ~ * ? [a-z] {a,b}\n\\\\ \\n \\x41\nEOF\nEOF\n<(touch PWNED_PROC)\n| tee PWNED_PIPE\n"
)


def corpus():
    rnd = random.Random(20260927)
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliet"]
    prose = lambda n: sized(PROSE, n)  # noqa: E731
    cases = [
        ("10KB prose", prose(10 * KB)),
        ("50KB prose", prose(50 * KB)),
        ("100KB prose", prose(100 * KB)),
        ("250KB prose", prose(250 * KB)),
        ("100KB unicode", sized(UNICODE, 100 * KB)),
        ("250KB unicode", sized(UNICODE, 250 * KB)),
        ("100KB markdown and code fences", sized(MARKDOWN, 100 * KB)),
        ("100KB quotes backticks shell-looking", sized(SHELLISH, 100 * KB)),
        ("300K-char single line", "".join(rnd.choice(words) for _ in range(60000))[:300000]),
        ("40,000 short lines", "".join("line %d\n" % i for i in range(40000))),
        ("exact leading and trailing whitespace", "   indented start\n\n\ttabbed\n" + prose(20 * KB) + "\n\n  trailing  \n\n"),
        ("mixed everything 250KB", sized(UNICODE + MARKDOWN + SHELLISH + PROSE, 250 * KB)),
        ("exactly the maximum", sized(UNICODE, CORE.MAX_TASK_BYTES)),
    ]
    return cases


class PayloadCase(LaunchCase):
    def setUp(self):
        super().setUp()
        self._i = 0

    # -- creating ---------------------------------------------------------------
    def create_direct(self, task, project="nova"):
        self._i += 1
        for record in CORE.logical_list():  # one active session per project: retire the previous case's
            if record["project"] == project and record["state"] not in CORE.LS_TERMINAL:
                CORE.logical_mutate(record["logical_session_id"], lambda rec: rec.__setitem__("state", CORE.LS_COMPLETED))
        body = {"project": project, "task": task, "request_id": "req-big-%06d" % self._i}
        return CORE.remote_create_session(
            body, CTX, terminal=self.fake_terminal, wait_seconds=15, health_wait=0.0, isolation_check=self.isolation_check
        )

    def raw_post(self, raw, path="/api/v1/sessions"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        headers = {"Host": HOST, "Tailscale-User-Login": LOGIN, "Origin": "https://" + HOST, "Content-Type": "application/json",
                   "Authorization": "Bearer " + self.token}
        conn.request("POST", path, raw, headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        conn.close()
        return resp.status, data

    # -- the agent side ---------------------------------------------------------
    def agent_parts(self, lsid, agent=AGENT, subprocess_run=False):
        """Read the task exactly as the agent would: `session task --part N` for every part. Returns (text, parts)."""
        if subprocess_run:
            env = self.env(CLAUDE_TERMINAL_HANDOFF_LOGICAL_SESSION=lsid, CLAUDE_CODE_SESSION_ID=agent)

            def call(part):
                args = [PYTHON, TH_SCRIPT, "session", "task"] + (["--part", str(part)] if part else [])
                proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=120)
                self.assertEqual(proc.returncode, 0, proc.stderr[-300:])
                return proc.stdout
        else:
            def call(part):
                return self.in_process(lsid, agent, part)

        info = json.loads(call(None).decode("utf-8"))
        chunks = []
        for number in range(1, info["parts"] + 1):
            raw = call(number)
            text = raw.decode("utf-8")
            header, _, rest = text.partition("\n")
            footer = "END OF TASK PART %d OF %d" % (number, info["parts"])
            self.assertTrue(rest.endswith("\n" + footer + "\n"), "part %d is not framed" % number)
            body = rest[: -(len(footer) + 2)]
            self.assertIn("part sha256 %s" % sha(body), header)
            self.assertIn("task sha256 %s" % info["sha256"], header)
            self.assertLessEqual(len(text), 30000 - 1, "a part must fit comfortably in one tool result")
            chunks.append(body)
        return "".join(chunks), info

    def in_process(self, lsid, agent, part):
        out = io.StringIO()
        out.buffer = io.BytesIO()
        real = sys.stdout
        sys.stdout = out
        try:
            code = CORE._cmd_session_task(CORE.logical_read(lsid), lsid, agent, part)
        finally:
            sys.stdout = real
        self.assertEqual(code, 0, out.getvalue())
        return out.buffer.getvalue() + out.getvalue().encode("utf-8")

    # -- what must never carry the prompt --------------------------------------
    def files_containing(self, needle, skip_dirs=("tasks",)):
        hits = []
        needle = needle.encode("utf-8")
        for root, dirs, files in os.walk(self.home):
            dirs[:] = [d for d in dirs if not (root == self.home and d in skip_dirs)]
            for name in files:
                path = os.path.join(root, name)
                try:
                    with open(path, "rb") as handle:
                        if needle in handle.read():
                            hits.append(os.path.relpath(path, self.home))
                except OSError:
                    pass
        return hits

    def check_round_trip(self, task, subprocess_run=False, expect_pointer=None, canary=True):
        canary_text = None
        if canary and len(task) > 2000:
            # A unique marker in the middle of the prompt (never in its first line, which is the session title by design).
            canary_text = "<<CANARY-%s>>" % uuid.uuid4().hex
            mid = len(task) // 2
            task = task[:mid] + canary_text + task[mid:]
        status, view = self.create_direct(task)
        self.assertIn(status, (200, 201), view)
        lsid = view["logical_session_id"]
        record = CORE.logical_read(lsid)
        meta = record["task"]
        raw = task.encode("utf-8")
        # what was accepted, reported independently of the code that computed it
        self.assertEqual((meta["bytes"], meta["chars"], meta["sha256"]), (len(raw), len(task), hashlib.sha256(raw).hexdigest()))
        self.assertEqual(meta["lines"], task.count("\n") + (0 if task.endswith("\n") else 1))
        self.assertEqual({k: v for k, v in view["task"].items() if k != "inline"},
                         {"chars": len(task), "bytes": len(raw), "lines": meta["lines"], "parts": meta["parts"],
                          "parts_read": 0, "fingerprint": meta["sha256"][:12]})
        # stored exactly, privately, atomically
        path = CORE.task_path(lsid)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), raw)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
        self.assertEqual([n for n in os.listdir(os.path.dirname(path)) if ".tmp-" in n], [])
        # the inbox holds a short pointer for anything large or not exactly representable inline; the record stays small
        message = record["inbox"]["messages"][0]
        inline = message.get("task_ref") is None
        self.assertEqual(view["task"]["inline"], inline)
        if expect_pointer is not None:
            self.assertEqual(not inline, expect_pointer)
        if inline:
            self.assertEqual(message["text"], task)  # exact: not trimmed, not cleaned
        else:
            self.assertLess(len(message["text"]), 3000)
            self.assertEqual(message["task_ref"]["sha256"], meta["sha256"])
            self.assertLess(len(json.dumps(record)), 60000, "the session record must not carry the task")
        # the agent side receives exactly what was submitted
        received, info = self.agent_parts(lsid, subprocess_run=subprocess_run) if not inline else (task, None)
        if not inline:
            self.assertEqual(sha(received), sha(task))
            self.assertEqual(received, task)
            self.assertEqual(len(received.encode("utf-8")), len(raw))
            self.assertEqual(CORE.logical_public_view(CORE.logical_read(lsid))["task"]["parts_read"], info["parts"])
            # and it never leaked into a launch script, a log, an event, a window title or the inbox record
            if canary_text:
                self.assertIn(canary_text, received)  # it really travelled...
                self.assertNotIn(canary_text, self.script_texts[-1])  # ...and never through a launch script (argv)
                self.assertEqual(self.files_containing(canary_text, skip_dirs=("tasks", "logical")), [])  # nor any log, event or notification
                self.assertNotIn(canary_text, open(os.path.join(self.home, "logical", lsid + ".json")).read())  # nor the session record
                self.assertNotIn(canary_text, json.dumps(CORE.logical_public_view(CORE.logical_read(lsid))))
            self.assertLess(len(self.script_texts[-1]), 20000)  # the launch is a short bootstrap, not the prompt
        return lsid, meta


class TestValidationAndStorage(unittest.TestCase):
    def test_validate_keeps_the_task_exactly_and_refuses_only_what_cannot_be_stored(self):
        for text in ("  lead\n\ttab\r\nend  \n\n", "x", "\u00e9\u4e16\U0001f680", "a\x1bb\x07c", "line\u2028sep"):
            self.assertEqual(CORE.validate_task_text(text), (text, None))  # not trimmed, not stripped, not normalised
        for bad, needle in (("", "non-empty"), ("  \n\t ", "non-empty"), (None, "non-empty"), (5, "non-empty"), ("a\x00b", "NUL"), ("a\ud800b", "surrogate")):
            text, why = CORE.validate_task_text(bad)
            self.assertIsNone(text)
            self.assertIn(needle, why)

    def test_the_maximum_is_in_bytes_and_reported_with_both_counts(self):
        at_limit = "x" * CORE.MAX_TASK_BYTES
        self.assertEqual(CORE.validate_task_text(at_limit)[0], at_limit)
        text, why = CORE.validate_task_text(at_limit + "x")
        self.assertIsNone(text)
        self.assertEqual(why, "Task is too large: 524,289 bytes (524,289 characters); the maximum is 524,288 bytes")
        two_byte = "\u00e9" * (CORE.MAX_TASK_BYTES // 2 + 1)
        self.assertIn("(262,145 characters)", CORE.validate_task_text(two_byte)[1])

    def test_parts_always_concatenate_to_the_exact_text(self):
        rnd = random.Random(7)
        samples = ["", "x", "y" * CORE.TASK_PART_CHARS, "y" * (CORE.TASK_PART_CHARS + 1), "a\n" * 50000, "z" * 100000,
                   "".join(rnd.choice("ab\n\u4e16\U0001f680 ") for _ in range(120000)), "\n" * 40000, "no newline at all " * 5000]
        for text in samples:
            spans = CORE.task_part_spans(text)
            self.assertEqual("".join(text[a:b] for a, b in spans), text)
            self.assertTrue(all(0 < b - a <= CORE.TASK_PART_CHARS for a, b in spans))
            self.assertTrue(all(spans[i][1] == spans[i + 1][0] for i in range(len(spans) - 1)))

    def test_a_long_single_line_is_split_by_characters_and_a_normal_text_ends_parts_on_newlines(self):
        line = "w" * (CORE.TASK_PART_CHARS * 3 + 5)
        self.assertEqual(len(CORE.task_part_spans(line)), 4)
        prose = "0123456789\n" * 5000
        spans = CORE.task_part_spans(prose)
        self.assertTrue(all(prose[b - 1] == "\n" for _, b in spans))

    def test_the_write_is_atomic_and_a_failed_write_leaves_the_old_file_and_no_debris(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t", "task")
            CORE.write_bytes_private_atomic(path, b"first")
            real = os.replace

            def boom(src, dst):
                raise OSError("no space")

            os.replace = boom
            try:
                with self.assertRaises(OSError):
                    CORE.write_bytes_private_atomic(path, b"second, longer")
            finally:
                os.replace = real
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), b"first")
            self.assertEqual(os.listdir(os.path.dirname(path)), ["task"])
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)


class TestRoundTrips(PayloadCase):
    def test_every_payload_arrives_byte_for_byte(self):
        for name, task in corpus():
            with self.subTest(name):
                self.check_round_trip(task, expect_pointer=None if len(task) < 8000 else True, canary=(name != "exactly the maximum"))

    def test_the_agent_process_receives_the_exact_bytes_through_the_real_command(self):
        for name, task in (("250KB unicode", sized(UNICODE + MARKDOWN, 250 * KB)), ("one 300K line", "q" * 300000),
                           ("the maximum", sized(SHELLISH + UNICODE, CORE.MAX_TASK_BYTES - 64))):
            with self.subTest(name):
                lsid, meta = self.check_round_trip(task, subprocess_run=True, expect_pointer=True)
                self.assertGreater(meta["parts"], 1)

    def test_shell_looking_text_is_inert_data(self):
        task = SHELLISH * 40
        self.check_round_trip(task, expect_pointer=True)
        for name in os.listdir(self.repo) + os.listdir(self.tmp) + os.listdir(os.getcwd()):
            self.assertFalse(name.startswith("PWNED"), name)

    def test_small_ordinary_tasks_are_unchanged_and_still_inline(self):
        lsid, meta = self.check_round_trip("Audit the retrieval pipeline.\nDo not deploy.", expect_pointer=False)
        ok, _, claimed = CORE.inbox_claim(lsid, AGENT)
        self.assertEqual(claimed[0]["text"], "Audit the retrieval pipeline.\nDo not deploy.")

    def test_small_tasks_stay_exact_inline_even_with_whitespace_and_control_characters(self):
        for task in ("  leading spaces matter\nfor a code block\n\n", "has an escape \x1b[31m char\r\nand a CRLF", "\ttab first"):
            with self.subTest(task[:20]):
                lsid, _ = self.check_round_trip(task, expect_pointer=False)
                ok, _, claimed = CORE.inbox_claim(lsid, AGENT)
                self.assertEqual(claimed[0]["text"], task)

    def test_a_task_over_the_inline_size_goes_by_reference(self):
        self.check_round_trip("x" * 8001, expect_pointer=True, canary=False)
        self.check_round_trip("y" * 8000, expect_pointer=False, canary=False)

    def test_the_pointer_tells_the_agent_exactly_how_to_read_everything(self):
        lsid, meta = self.check_round_trip(sized(PROSE, 120 * KB), expect_pointer=True)
        text = CORE.logical_read(lsid)["inbox"]["messages"][0]["text"]
        self.assertIn("session task --part 1", text)
        self.assertIn("session task --part %d" % meta["parts"], text)
        self.assertIn(meta["sha256"], text)
        self.assertIn("Do not begin the work until you have read all %d part" % meta["parts"], text)

    def test_the_task_file_outlives_the_launch_sweep_and_the_session(self):
        lsid, meta = self.check_round_trip(sized(PROSE, 60 * KB), expect_pointer=True)
        CORE.logical_mutate(lsid, lambda rec: rec.__setitem__("state", CORE.LS_FAILED))
        CORE.sweep_launch_artifacts(max_age=0)
        text, why = CORE.load_task(lsid)
        self.assertEqual((sha(text), why), (meta["sha256"], None))


class TestAgentCommandSafety(PayloadCase):
    def setUp(self):
        super().setUp()
        status, view = self.create_direct(sized(PROSE, 40 * KB))
        self.assertEqual(status, 201, view)
        self.lsid = view["logical_session_id"]

    def run_cmd(self, agent, part, tty=False):
        class Stdin(object):
            def isatty(self):
                return tty

        out = io.StringIO()
        out.buffer = io.BytesIO()
        real, real_in = sys.stdout, sys.stdin
        sys.stdout, sys.stdin = out, Stdin()
        try:
            code = CORE._cmd_session_task(CORE.logical_read(self.lsid), self.lsid, agent, part)
        finally:
            sys.stdout, sys.stdin = real, real_in
        return code, out.getvalue() + out.buffer.getvalue().decode("utf-8")

    def test_only_the_owner_can_read_and_progress_is_counted_only_for_the_owner(self):
        code, text = self.run_cmd("some-other-session", 1)
        self.assertEqual(code, 3)
        self.assertIn("not the current owner", text)
        self.assertEqual(CORE.logical_public_view(CORE.logical_read(self.lsid))["task"]["parts_read"], 0)
        code, text = self.run_cmd(None, 1, tty=False)  # no agent id and no terminal: an agent with a cleared environment
        self.assertEqual(code, 3)
        self.assertIn("not the current owner", text)
        self.assertEqual(self.run_cmd(None, 1, tty=True)[0], 0)  # a person at a terminal may read it, uncounted
        self.assertEqual(CORE.logical_public_view(CORE.logical_read(self.lsid))["task"]["parts_read"], 0)
        self.assertEqual(self.run_cmd(AGENT, 1)[0], 0)
        self.assertEqual(CORE.logical_public_view(CORE.logical_read(self.lsid))["task"]["parts_read"], 1)

    def test_a_part_out_of_range_is_refused_not_clamped(self):
        parts = CORE.logical_read(self.lsid)["task"]["parts"]
        for bad in (0, -1, parts + 1):
            code, text = self.run_cmd(AGENT, bad)
            self.assertEqual(code, 3, bad)
            self.assertIn("between 1 and %d" % parts, text)

    def test_a_tampered_or_missing_payload_is_refused_never_served_partly(self):
        path = CORE.task_path(self.lsid)
        with open(path, "ab") as handle:
            handle.write(b"appended by an attacker")
        code, text = self.run_cmd(AGENT, 1)
        self.assertEqual(code, 3)
        self.assertIn("no longer matches", text)
        os.unlink(path)
        code, text = self.run_cmd(AGENT, None)
        self.assertEqual(code, 3)
        self.assertIn("could not be read", text)

    def test_the_command_is_on_the_agents_allow_list_and_the_payload_is_readable_by_it(self):
        rules = " ".join(CORE.agent_cli_allow_rules())
        self.assertIn("session task:*", rules)


class TestOverHttp(PayloadCase):
    def test_the_maximum_task_goes_through_the_gateway_and_arrives_exact(self):
        task = sized(UNICODE + MARKDOWN, CORE.MAX_TASK_BYTES)
        status, view = self.create(task=task, request_id="req-http-max-0001")
        self.assertEqual(status, 201, view)
        self.assertEqual(view["task"]["bytes"], CORE.MAX_TASK_BYTES)
        received, info = self.agent_parts(view["logical_session_id"])
        self.assertEqual(sha(received), sha(task))

    def test_one_byte_over_is_rejected_with_the_numbers_before_anything_launches(self):
        status, body = self.create(task="x" * (CORE.MAX_TASK_BYTES + 1), request_id="req-http-over-0001")
        self.assertEqual(status, 413)
        self.assertEqual(body["error"], "task_too_large")
        self.assertEqual((body["bytes"], body["chars"], body["limit_bytes"]), (524289, 524289, 524288))
        self.assertIn("524,289 bytes", body["reason"])
        self.assertEqual((self.launched, CORE.logical_list()), ([], []))
        self.assertEqual(os.path.exists(os.path.join(self.home, "tasks")) and os.listdir(os.path.join(self.home, "tasks")), False)

    def test_an_enormous_body_gets_a_clear_413_not_a_dropped_connection(self):
        raw = json.dumps({"project": "nova", "task": "z" * (5 * 1024 * 1024), "request_id": "req-http-huge-0001"})
        status, body = self.raw_post(raw)
        self.assertEqual(status, 413)
        self.assertEqual(body["error"], "payload_too_large")
        self.assertIn("the maximum for this request is 4,194,304 bytes", body["reason"])
        self.assertEqual(self.launched, [])

    def test_other_endpoints_keep_their_small_body_limit(self):
        status, body = self.raw_post(json.dumps({"text": "x" * 40000, "request_id": "req-http-inst-0001"}), "/api/v1/sessions/ls_%s/instructions" % ("a" * 24))
        self.assertEqual((status, body["error"]), (413, "payload_too_large"))

    def test_unstorable_characters_are_refused_plainly(self):
        base = '{"project": "nova", "task": "%s", "request_id": "req-http-%s-0001"}'
        status, body = self.raw_post(base % ("a\\u0000b", "nul"))
        self.assertEqual(status, 400)
        self.assertIn("NUL", body["reason"])
        status, body = self.raw_post(base % ("a\\ud800b", "sur"))
        self.assertEqual(status, 400)
        self.assertIn("surrogate", body["reason"])
        self.assertEqual(self.launched, [])

    def test_a_valid_surrogate_pair_escape_is_an_emoji_not_an_error(self):
        status, body = self.raw_post('{"project": "nova", "task": "rocket \\ud83d\\ude80 ok", "request_id": "req-http-pair-0001"}')
        self.assertEqual(status, 201, body)
        self.assertEqual(CORE.load_task(body["logical_session_id"])[0], "rocket \U0001f680 ok")

    def test_a_retry_after_a_definitive_rejection_is_a_new_request_not_a_replay(self):
        first = self.create(task="x" * (CORE.MAX_TASK_BYTES + 1), request_id="req-http-retry-0001")
        self.assertEqual(first[0], 413)
        second = self.create(task="fine now", request_id="req-http-retry-0002")
        self.assertEqual(second[0], 201, second[1])


class TestReviewFixes(PayloadCase):
    def cli(self, lsid, *args, agent=AGENT, stdin=subprocess.DEVNULL, with_agent=True):
        env = self.env(CLAUDE_TERMINAL_HANDOFF_LOGICAL_SESSION=lsid)
        if with_agent:
            env["CLAUDE_CODE_SESSION_ID"] = agent
        else:
            env.pop("CLAUDE_CODE_SESSION_ID", None)
        proc = subprocess.run([PYTHON, TH_SCRIPT] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, stdin=stdin, timeout=120)
        return proc.returncode, proc.stdout.decode("utf-8")

    def test_inline_versus_pointer_is_decided_by_the_escaped_size_the_agent_must_receive(self):
        cases = (("\u4e16" * 3000, True), ("\u4e16" * 1900, False), ("\U0001f680" * 1200, True), ("\U0001f680" * 500, False), ("\x01" * 3000, True), ("a\nb" * 2000, False))
        for task, pointer in cases:
            with self.subTest(task[:3] + str(len(task))):
                lsid, meta = self.check_round_trip(task, expect_pointer=pointer, canary=False)
                code, out = self.cli(lsid, "session", "inbox")
                self.assertEqual(code, 0)
                self.assertLess(len(out), 20000, "the inbox output must fit one tool result (Claude truncates near 30k characters)")
                message = json.loads(out)["messages"][0]
                if not pointer:
                    self.assertEqual(message["text"], task)  # inline is exact through the real command's JSON

    def test_the_pointer_names_only_commands_that_exist(self):
        lsid, meta = self.check_round_trip(sized(PROSE, 60 * KB), expect_pointer=True)
        text = CORE.logical_read(lsid)["inbox"]["messages"][0]["text"]
        self.assertNotIn("--info", text)
        self.assertEqual(self.cli(lsid, "session", "task", "--info")[0], 0)  # and the flag is accepted anyway
        self.assertEqual(json.loads(self.cli(lsid, "session", "task")[1])["parts"], meta["parts"])

    def test_acknowledging_a_partly_read_task_is_refused_and_a_fully_read_one_is_accepted(self):
        self._i += 1
        status, view = self.create_direct(sized(PROSE, 100 * KB))
        lsid = view["logical_session_id"]
        parts = CORE.logical_read(lsid)["task"]["parts"]
        mid = json.loads(self.cli(lsid, "session", "inbox")[1])["messages"][0]
        self.assertEqual(mid["task_progress"], {"parts": parts, "parts_read": 0, "next_part": 1, "complete": False})
        for number in range(1, parts):
            self.assertEqual(self.cli(lsid, "session", "task", "--part", str(number))[0], 0)
        code, out = self.cli(lsid, "session", "ack", "--message-id", mid["id"])
        self.assertEqual(code, 3)
        self.assertIn("not been fully read: %d of %d parts. Read part %d next" % (parts - 1, parts, parts), json.loads(out)["error"])
        self.assertEqual(self.cli(lsid, "session", "task", "--part", str(parts))[0], 0)
        code, out = self.cli(lsid, "session", "ack", "--message-id", mid["id"])
        self.assertEqual(code, 0, out)
        self.assertEqual([m["status"] for m in CORE.logical_read(lsid)["inbox"]["messages"]], ["acked"])

    def test_an_inline_task_is_acknowledged_as_before(self):
        status, view = self.create_direct("small task")
        lsid = view["logical_session_id"]
        mid = json.loads(self.cli(lsid, "session", "inbox")[1])["messages"][0]
        self.assertNotIn("task_progress", mid)
        self.assertEqual(self.cli(lsid, "session", "ack", "--message-id", mid["id"])[0], 0)

    def test_an_agent_with_no_identity_and_no_terminal_cannot_read_a_sessions_task(self):
        status, view = self.create_direct(sized(PROSE, 30 * KB))
        lsid = view["logical_session_id"]
        code, out = self.cli(lsid, "session", "task", "--part", "1", with_agent=False)
        self.assertEqual(code, 3)
        self.assertIn("not the current owner", out)
        code, out = self.cli(lsid, "session", "task", "--part", "1", agent="someone-else")
        self.assertEqual(code, 3)

    def test_the_title_carries_no_control_format_or_separator_characters(self):
        self.assertEqual(CORE.task_title("\u202eevil\u200b title\u2028rest\nsecond"), "evil titlerest")
        self.assertEqual(CORE.task_title("\n\n  hello  \nx"), "hello")
        self.assertEqual(CORE.task_title("\x1b[31mred\x07"), "[31mred")
        self.assertEqual(CORE.task_title("\u200b\u200b"), "Task")
        self.assertEqual(len(CORE.task_title("w" * 500)), 120)

    def test_only_crash_debris_is_swept_from_the_task_store_never_a_payload(self):
        status, view = self.create_direct(sized(PROSE, 30 * KB))
        directory = os.path.dirname(CORE.task_path(view["logical_session_id"]))
        stale = os.path.join(directory, "ls_x.task.tmp-99-abcd1234")
        fresh = os.path.join(directory, "ls_y.task.tmp-99-abcd1234")
        for path in (stale, fresh):
            open(path, "wb").write(b"partial")
        os.utime(stale, (1, 1))
        CORE.sweep_launch_artifacts(max_age=0)
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(fresh))  # a write that may still be in flight is left alone
        self.assertTrue(os.path.exists(CORE.task_path(view["logical_session_id"])))

    def test_a_body_read_that_trickles_past_its_deadline_is_abandoned(self):
        ticks = iter(range(0, 1000, 60))

        class Trickle(object):
            def read(self, n):
                return b"x"

        with self.assertRaises(TimeoutError):
            CORE._read_exact(Trickle(), 10 ** 6, deadline=120, clock=lambda: next(ticks))
        self.assertEqual(CORE._read_exact(io.BytesIO(b"abcdef"), 6), b"abcdef")

    def test_an_inline_task_is_not_shown_as_a_stored_task_on_the_phone(self):
        status, view = self.create_direct("tiny")
        self.assertTrue(view["task"]["inline"])
        status, view = self.create_direct(sized(PROSE, 30 * KB))
        self.assertFalse(view["task"]["inline"])


if __name__ == "__main__":
    unittest.main()
