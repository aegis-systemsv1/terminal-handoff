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

import importlib.util
import json
import os
import signal
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

SLEEP_BIN = "/bin/sleep"
SHELL_BIN = "/bin/zsh"


def load_core():
    """Import the implementation under test as a module.

    Some behaviour - name sanitising, process identity, transfer transitions -
    is easier to prove directly than through a subprocess, and every function
    reads its state home from the environment at call time.
    """
    spec = importlib.util.spec_from_file_location("terminal_handoff_under_test", TH_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CORE = load_core()


STANDIN_SOURCE = """#include <stdlib.h>
int main(int argc, char **argv) {
    if (argc < 2) return 2;
    return system(argv[1]) == 0 ? 0 : 1;
}
"""


def compiler():
    for candidate in ("/usr/bin/cc", "/usr/bin/clang"):
        if os.path.exists(candidate):
            return candidate
    return shutil.which("cc") or shutil.which("clang")


def compile_standin_claude(directory):
    """Build a real executable named `claude` that can start child processes.

    Needed to reproduce Claude Code's actual topology, where the status-line
    process is a grandchild. /bin/sleep cannot start children; a shell cannot
    be used either, because `sh -c "one command"` execs that command and loses
    its own identity; and a copied system binary is killed by the code-signing
    check. A small compiled program keeps its own name and forks a shell.
    """
    cc = compiler()
    if cc is None:
        return None
    os.makedirs(directory, exist_ok=True)
    source = os.path.join(directory, "standin.c")
    with open(source, "w") as handle:
        handle.write(STANDIN_SOURCE)
    binary = os.path.join(directory, "claude")
    result = subprocess.run(
        [cc, "-O0", "-o", binary, source], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    if result.returncode != 0 or not os.path.exists(binary):
        return None
    return binary


def process_alive(pid):
    """True only for a live process.

    A stand-in started by the test suite is our own child, so after it exits it
    remains a zombie until it is reaped: `kill(pid, 0)` would still succeed.
    """
    proc = subprocess.run(
        ["/bin/ps", "-o", "stat=", "-p", str(int(pid))],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    state = proc.stdout.decode("utf-8", "replace").strip()
    return bool(state) and not state.startswith("Z")


def wait_for_exit(pid, timeout=15.0, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not process_alive(pid):
            return True
        time.sleep(interval)
    return not process_alive(pid)


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
        self._standins = []
        self.fake_claude = os.path.join(self.tmp, "claude")
        with open(self.fake_claude, "w") as handle:
            handle.write("#!/bin/sh\necho 'fake claude'\n")
        os.chmod(self.fake_claude, 0o700)

    def tearDown(self):
        for proc in getattr(self, "_standins", []):
            try:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=5)
            except Exception:
                pass
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
        session_name=None,
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
        if session_name is not None:
            data["session_name"] = session_name
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

    def transfer(self, session_id):
        return os.path.join(self.home, "transfers", "%s.json" % session_id)

    def chain_state(self, chain_id):
        return os.path.join(self.home, "chains", "%s.json" % chain_id)

    def standin_claude(self, label="parent", seconds=120, name="claude"):
        """Start a real process whose executable file is named `claude`.

        A symlink to /bin/sleep: macOS records the exec path, so `ps -o comm=`
        reports the symlink's own name. A copied system binary is killed by the
        code-signing check and a shell script reports its interpreter instead,
        so neither is a usable stand-in.
        """
        directory = os.path.join(self.tmp, "standin-%s" % label)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        if not os.path.exists(path):
            os.symlink(SLEEP_BIN, path)
        proc = subprocess.Popen(
            [path, str(seconds)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self._standins.append(proc)
        time.sleep(0.4)
        return proc

    def binding_for(self, pid, session_id="parent-session", current_dir=None, **overrides):
        """Build a parent-process binding for an explicitly known PID.

        Tests never bind by ancestry when a signal may follow: the bound PID is
        always one the test started itself.
        """
        os.environ["CLAUDE_TERMINAL_HANDOFF_HOME"] = self.home
        identity = CORE.process_identity(pid)
        assert identity is not None, "stand-in process %s is not running" % pid
        binding = {
            "pid": identity["pid"],
            "ppid": identity["ppid"],
            "uid": identity["uid"],
            "tty": identity["tty"],
            "start": identity["start"],
            "name": identity["name"],
            "command": identity["command"],
            "process_cwd": None,
            "session_id": session_id,
            "session_current_dir": current_dir or self.workdir,
            "ancestry_depth": 0,
            "ancestry": [],
            "bound_utc": "1970-01-01T00:00:00Z",
            "bound_by_pid": os.getpid(),
        }
        binding.update(overrides)
        return binding

    def write_transfer(self, session_id, binding, state="LAUNCHING", **overrides):
        """Write a transfer record directly, for shutdown-boundary tests."""
        os.environ["CLAUDE_TERMINAL_HANDOFF_HOME"] = self.home
        os.makedirs(os.path.join(self.home, "transfers"), exist_ok=True)
        record = {
            "schema_version": 2,
            "terminal_handoff_version": CORE.TERMINAL_HANDOFF_VERSION,
            "state": state,
            "owner": CORE.TRANSFER_OWNER.get(state, "parent"),
            "chain_id": "abcdef012345",
            "parent_generation": 1,
            "successor_generation": 2,
            "parent_session_id": session_id,
            "parent_display_name": "Ranger",
            "successor_display_name": "Ranger 2",
            "manifest_path": self.manifest(session_id),
            "parent_process": binding,
            "parent_process_bound": bool(binding),
            "expected_successor": {},
            "successor": {},
            "stop": {"enabled": True, "signal": "SIGTERM", "escalates": False, "attempts": 0},
            "created_utc": "1970-01-01T00:00:00Z",
            "history": [{"state": state, "ts": "1970-01-01T00:00:00Z", "reason": "test fixture"}],
        }
        record.update(overrides)
        path = self.transfer(session_id)
        with open(path, "w") as handle:
            json.dump(record, handle, indent=2, sort_keys=True)
        return path

    def trigger_and_wait(self, payload, env=None, timeout=25.0):
        session_id = payload["_session_id"]
        code, out, err = self.statusline(payload, env)
        self.assertEqual(code, 0, "statusline failed: %s" % err)
        ok = wait_for(self.launch_record(session_id), timeout)
        return ok, out
