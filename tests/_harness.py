#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Terminal Handoff - shared test harness.

Every test runs against synthetic Claude Code status-line JSON fixtures in an
isolated CLAUDE_TERMINAL_HANDOFF_HOME. No real Terminal window is ever opened,
no real Claude session is ever started, no real context window is consumed, and
no real repository is modified: git tests build throwaway repositories in a
temporary directory.

Run:  python3 -m unittest discover -s tests -v
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import shutil
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
TH_SCRIPT = os.path.join(os.path.dirname(HERE), "src", "terminal_handoff", "core.py")
PYTHON = sys.executable
GIT = shutil.which("git") or "/usr/bin/git"

REAL_MODEL_ID = "claude-opus-5[1m]"  # brackets are a genuine shell hazard
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def base_env(home, **overrides):
    env = {}
    for key, value in os.environ.items():
        if key.startswith("CLAUDE_TERMINAL_HANDOFF_"):
            continue
        if key.startswith("CLAUDE_CODE_"):
            continue
        env[key] = value
    env["CLAUDE_TERMINAL_HANDOFF_HOME"] = home
    env["CLAUDE_TERMINAL_HANDOFF_TEST_MODE"] = "1"
    env["CLAUDE_TERMINAL_HANDOFF_MIN_OBSERVATIONS"] = "1"
    env["CLAUDE_TERMINAL_HANDOFF_COOLDOWN"] = "0"
    env["CLAUDE_TERMINAL_HANDOFF_STORM_MAX"] = "1000"
    env["CLAUDE_TERMINAL_HANDOFF_STORM_WINDOW"] = "600"
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = str(value)
    return env


def run_th(args, stdin_text="", env=None, timeout=60):
    proc = subprocess.Popen(
        [PYTHON, TH_SCRIPT] + args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    out, err = proc.communicate(stdin_text.encode("utf-8"), timeout=timeout)
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def run_git(repo, *args):
    return subprocess.run(
        [GIT] + list(args), cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )



def json_file(path):
    with open(path, "r") as handle:
        return json.load(handle)


def text_file(path):
    with open(path, "r") as handle:
        return handle.read()


def wait_for(path, timeout=25.0, interval=0.15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            return True
        time.sleep(interval)
    return False


class THTestCase(unittest.TestCase):
    """Base case: isolated Terminal Handoff home, fake claude binary, fixtures."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="terminal-handoff-test-")
        self.home = os.path.join(self.tmp, "th-home")
        os.makedirs(self.home)
        self.workdir = os.path.join(self.tmp, "work")
        os.makedirs(self.workdir)
        self.fake_claude = os.path.join(self.tmp, "claude")
        with open(self.fake_claude, "w") as handle:
            handle.write("#!/bin/sh\necho 'fake claude'\n")
        os.chmod(self.fake_claude, 0o700)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- fixtures ---------------------------------------------------------

    def env(self, **overrides):
        overrides.setdefault("CLAUDE_TERMINAL_HANDOFF_CLAUDE_BIN", self.fake_claude)
        return base_env(self.home, **overrides)

    def make_transcript(self, session_id, lines=None, empty=False, jsonl=True, directory=None):
        directory = directory or self.tmp
        path = os.path.join(directory, "%s.jsonl" % session_id)
        if empty:
            open(path, "w").close()
            return path
        if not jsonl:
            with open(path, "w") as handle:
                handle.write("this is not JSON at all\nneither is this\n")
            return path
        rows = lines or [
            {"type": "user", "sessionId": session_id, "message": {"role": "user", "content": "do the work"}},
            {"type": "assistant", "sessionId": session_id, "message": {"role": "assistant", "content": "done"}},
        ]
        with open(path, "w") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        return path

    def payload(
        self,
        percent=85.0,
        session_id=None,
        model_id=REAL_MODEL_ID,
        model_display="Opus 5",
        effort="high",
        workdir=None,
        transcript=None,
        include_effort=True,
        include_context=True,
        include_model=True,
        include_transcript=True,
        include_session=True,
        rate_limit_percent=None,
        transcript_dir=None,
    ):
        session_id = session_id or str(uuid.uuid4())
        workdir = workdir or self.workdir
        if transcript is None and include_transcript:
            transcript = self.make_transcript(session_id, directory=transcript_dir)
        data = {
            "version": "2.1.234",
            "cwd": workdir,
            "workspace": {"current_dir": workdir, "project_dir": workdir, "added_dirs": []},
            "output_style": {"name": "default"},
            "thinking": {"enabled": True},
        }
        if include_session:
            data["session_id"] = session_id
        if include_transcript:
            data["transcript_path"] = transcript
        if include_model:
            data["model"] = {"id": model_id, "display_name": model_display}
        if include_effort:
            data["effort"] = {"level": effort}
        if include_context:
            data["context_window"] = {
                "total_input_tokens": 160000,
                "total_output_tokens": 900,
                "context_window_size": 200000,
                "current_usage": {
                    "input_tokens": 160000,
                    "output_tokens": 900,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
                "used_percentage": percent,
                "remaining_percentage": (None if percent is None else 100 - percent),
            }
        if rate_limit_percent is not None:
            data["rate_limits"] = {
                "five_hour": {"used_percentage": rate_limit_percent, "resets_at": 1787040000},
                "seven_day": {"used_percentage": rate_limit_percent, "resets_at": 1787040000},
            }
        data["_session_id"] = session_id
        return data

    # -- helpers ----------------------------------------------------------

    def evaluate(self, payload, env=None):
        sid = payload.pop("_session_id", None)
        code, out, err = run_th(["evaluate"], json.dumps(payload), env or self.env())
        if sid:
            payload["_session_id"] = sid
        self.assertEqual(code, 0, "evaluate failed: %s" % err)
        return json.loads(out)

    def statusline(self, payload, env=None, wrap=None):
        sid = payload.pop("_session_id", None)
        args = ["statusline"]
        if wrap:
            args += ["--wrap", wrap]
        code, out, err = run_th(args, json.dumps(payload), env or self.env())
        if sid:
            payload["_session_id"] = sid
        return code, out, err

    def launch_record(self, session_id):
        return os.path.join(self.home, "completed", "%s.launch.json" % session_id)

    def manifest(self, session_id):
        return os.path.join(self.home, "handoffs", "%s.json" % session_id)

    def trigger_and_wait(self, payload, env=None, timeout=25.0):
        session_id = payload["_session_id"]
        code, out, err = self.statusline(payload, env)
        self.assertEqual(code, 0, "statusline failed: %s" % err)
        ok = wait_for(self.launch_record(session_id), timeout)
        return ok, out
