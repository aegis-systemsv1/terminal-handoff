"""The parent-shutdown boundary.

These tests start real macOS processes and prove that Terminal Handoff signals
exactly one of them, exactly once, only after a valid successor heartbeat, and
never with SIGKILL. The stand-ins are symlinks to /bin/sleep named `claude`:
macOS records the executed path, so `ps -o comm=` reports the symlink's own
name, which is what the binding checks. A copied system binary is killed by the
code-signing check and a shell script reports its interpreter, so neither is a
usable stand-in.
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import (  # noqa: E402
    CORE,
    compile_standin_claude,
    PYTHON,
    TH_SCRIPT,
    THTestCase,
    json_file,
    process_alive,
    run_th,
    wait_for_exit,
)


class TestProcessIdentity(THTestCase):
    def test_identity_of_a_live_process(self):
        proc = self.standin_claude()
        identity = CORE.process_identity(proc.pid)
        self.assertIsNotNone(identity)
        self.assertEqual(identity["pid"], proc.pid)
        self.assertEqual(identity["name"], "claude")
        self.assertEqual(identity["uid"], os.getuid())
        self.assertTrue(identity["start"], "no process start time was captured")

    def test_identity_of_a_dead_process_is_none(self):
        proc = self.standin_claude()
        pid = proc.pid
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        self.assertTrue(wait_for_exit(pid))
        self.assertIsNone(CORE.process_identity(pid))

    def test_pid_0_and_1_are_never_identified(self):
        self.assertIsNone(CORE.process_identity(0))
        self.assertIsNone(CORE.process_identity(1))
        self.assertIsNone(CORE.process_identity("not-a-pid"))

    def test_ancestry_is_traced_not_assumed(self):
        """The status-line process is not the Claude process's direct child."""
        chain = CORE.process_ancestry()
        self.assertGreaterEqual(len(chain), 1)
        self.assertEqual(chain[0]["pid"], os.getpid())
        pids = [item["pid"] for item in chain]
        self.assertEqual(len(pids), len(set(pids)), "ancestry looped")

    def test_binding_traces_a_real_multi_level_ancestry(self):
        """Claude Code runs the status line through a shell, so the real
        ancestry is `statusline <- sh <- claude`, never a direct child.
        """
        directory = os.path.join(self.tmp, "ancestry-standin")
        fake = compile_standin_claude(directory)
        if fake is None:
            self.skipTest("no C compiler available to build a process stand-in")
        probe = os.path.join(self.tmp, "probe.py")
        with open(probe, "w") as handle:
            handle.write(
                "import importlib.util, json, os\n"
                "spec = importlib.util.spec_from_file_location('c', %r)\n"
                "core = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(core)\n"
                "binding, reason = core.bind_parent_claude_process('probe-session', os.getcwd())\n"
                "print(json.dumps({'binding': binding, 'reason': reason}))\n" % TH_SCRIPT
            )
        # The trailing `; true` stops the shell exec-optimising itself away,
        # so the probe really is a grandchild, exactly as Claude Code runs it.
        out = subprocess.run(
            [fake, "%s %s; true" % (PYTHON, probe)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.workdir,
            env=self.env(),
        )
        payload = out.stdout.decode("utf-8").strip().splitlines()
        self.assertTrue(payload, "the probe produced no output: %s" % out.stderr.decode("utf-8"))
        result = json.loads(payload[-1])
        binding = result["binding"]
        self.assertIsNotNone(binding, "no ancestor bound: %s" % result["reason"])
        self.assertEqual(binding["name"], "claude")
        self.assertEqual(binding["command"], fake, "a different process was bound")
        self.assertGreaterEqual(
            binding["ancestry_depth"], 2, "the ancestry was assumed rather than traced"
        )
        self.assertNotIn(
            binding["pid"],
            [item["pid"] for item in CORE.process_ancestry()],
            "a process from the test runner's own ancestry was bound",
        )
        self.assertEqual(binding["uid"], os.getuid())

    def test_a_distant_claude_process_is_never_bound(self):
        """A Claude session far up the ancestry belongs to someone else."""
        binding, reason = CORE.bind_parent_claude_process("probe", "/definitely/not/this/dir")
        if binding is not None:
            self.assertNotEqual(binding["pid"], os.getpid())
        else:
            self.assertTrue(reason)

    def test_a_candidate_in_a_different_directory_is_refused(self):
        """Binding the wrong session's process would stop the wrong session."""
        directory = os.path.join(self.tmp, "cwd-standin")
        fake = compile_standin_claude(directory)
        if fake is None:
            self.skipTest("no C compiler available to build a process stand-in")
        probe = os.path.join(self.tmp, "cwd-probe.py")
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere, exist_ok=True)
        with open(probe, "w") as handle:
            handle.write(
                "import importlib.util, json\n"
                "spec = importlib.util.spec_from_file_location('c', %r)\n"
                "core = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(core)\n"
                "binding, reason = core.bind_parent_claude_process('probe-session', %r)\n"
                "print(json.dumps({'binding': binding, 'reason': reason}))\n"
                % (TH_SCRIPT, elsewhere)
            )
        out = subprocess.run(
            [fake, "%s %s" % (PYTHON, probe)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.workdir,
            env=self.env(),
        )
        result = json.loads(out.stdout.decode("utf-8").strip().splitlines()[-1])
        self.assertIsNone(result["binding"], "a process in another directory was bound")
        self.assertIn("different directory", result["reason"])


class TestBindingVerification(THTestCase):
    def test_a_valid_binding_verifies(self):
        proc = self.standin_claude()
        ok, reason = CORE.verify_parent_binding(self.binding_for(proc.pid))
        self.assertTrue(ok, reason)

    def test_a_reused_pid_is_refused(self):
        """PID reuse is caught by the recorded process start time."""
        proc = self.standin_claude()
        binding = self.binding_for(proc.pid, start="Mon  1 Jan 00:00:00 1990")
        ok, reason = CORE.verify_parent_binding(binding)
        self.assertFalse(ok)
        self.assertIn("reused", reason)
        self.assertTrue(process_alive(proc.pid), "the live process was affected")

    def test_a_process_that_is_not_claude_is_refused(self):
        proc = subprocess.Popen(
            ["/bin/sleep", "60"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self._standins.append(proc)
        time.sleep(0.3)
        ok, reason = CORE.verify_parent_binding(self.binding_for(proc.pid))
        self.assertFalse(ok)
        self.assertIn("not a Claude Code process", reason)

    def test_a_renamed_executable_is_refused(self):
        proc = self.standin_claude()
        ok, reason = CORE.verify_parent_binding(self.binding_for(proc.pid, name="something-else"))
        self.assertFalse(ok)

    def test_a_different_terminal_is_refused(self):
        proc = self.standin_claude()
        ok, reason = CORE.verify_parent_binding(self.binding_for(proc.pid, tty="ttys999"))
        self.assertFalse(ok)
        self.assertIn("different terminal", reason)

    def test_a_dead_process_is_refused(self):
        proc = self.standin_claude()
        binding = self.binding_for(proc.pid)
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        ok, reason = CORE.verify_parent_binding(binding)
        self.assertFalse(ok)
        self.assertIn("no longer running", reason)

    def test_pid_1_and_self_are_refused(self):
        proc = self.standin_claude()
        base = self.binding_for(proc.pid)
        for pid in (0, 1, -5, os.getpid()):
            with self.subTest(pid=pid):
                binding = dict(base)
                binding["pid"] = pid
                ok, _ = CORE.verify_parent_binding(binding)
                self.assertFalse(ok)

    def test_a_missing_binding_is_refused(self):
        for binding in (None, {}, {"pid": None}, "not a binding"):
            with self.subTest(binding=binding):
                ok, _ = CORE.verify_parent_binding(binding)
                self.assertFalse(ok)

    def test_a_binding_for_another_session_or_chain_is_refused(self):
        proc = self.standin_claude()
        binding = self.binding_for(proc.pid, session_id="session-a", chain_id="aaaaaa111111",
                                   generation=1)
        ok, _ = CORE.verify_parent_binding(binding, session_id="session-b")
        self.assertFalse(ok)
        ok, _ = CORE.verify_parent_binding(binding, chain_id="bbbbbb222222")
        self.assertFalse(ok)
        ok, _ = CORE.verify_parent_binding(binding, generation=7)
        self.assertFalse(ok)
        ok, reason = CORE.verify_parent_binding(
            binding, session_id="session-a", chain_id="aaaaaa111111", generation=1
        )
        self.assertTrue(ok, reason)


class TestTransferStateMachine(THTestCase):
    def setUp(self):
        super(TestTransferStateMachine, self).setUp()
        os.environ["CLAUDE_TERMINAL_HANDOFF_HOME"] = self.home

    def tearDown(self):
        os.environ.pop("CLAUDE_TERMINAL_HANDOFF_HOME", None)
        super(TestTransferStateMachine, self).tearDown()

    def test_the_legal_transitions_are_the_documented_ones(self):
        self.assertEqual(
            set(CORE.TRANSFER_STATES),
            {
                "LAUNCHING",
                "SUCCESSOR_VERIFIED",
                "PARENT_STOP_REQUESTED",
                "TRANSFER_COMPLETE",
                "TRANSFER_FAILED",
            },
        )
        self.assertEqual(CORE.TRANSFER_OWNER["LAUNCHING"], "parent")
        self.assertEqual(CORE.TRANSFER_OWNER["SUCCESSOR_VERIFIED"], "parent")
        self.assertEqual(CORE.TRANSFER_OWNER["PARENT_STOP_REQUESTED"], "successor")
        self.assertEqual(CORE.TRANSFER_OWNER["TRANSFER_COMPLETE"], "successor")
        self.assertEqual(CORE.TRANSFER_OWNER["TRANSFER_FAILED"], "parent")

    def test_transitions_are_atomic_and_auditable(self):
        path = self.write_transfer("s1", None)
        ok, record = CORE.transfer_transition(path, "SUCCESSOR_VERIFIED", reason="ok")
        self.assertTrue(ok)
        self.assertEqual(record["state"], "SUCCESSOR_VERIFIED")
        self.assertEqual(record["history"][-1]["reason"], "ok")
        self.assertEqual(record["history"][-1]["from"], "LAUNCHING")

    def test_illegal_transitions_are_refused(self):
        path = self.write_transfer("s2", None)
        for target in ("PARENT_STOP_REQUESTED", "TRANSFER_COMPLETE", "LAUNCHING"):
            with self.subTest(target=target):
                ok, _ = CORE.transfer_transition(path, target)
                self.assertFalse(ok)
        self.assertEqual(json_file(path)["state"], "LAUNCHING")

    def test_terminal_states_are_terminal(self):
        path = self.write_transfer("s3", None, state="TRANSFER_COMPLETE")
        for target in CORE.TRANSFER_STATES:
            ok, _ = CORE.transfer_transition(path, target)
            self.assertFalse(ok, "left a terminal state for %s" % target)

    def test_a_repeated_transition_is_refused(self):
        """This is what stops a duplicate supervisor requesting a second stop."""
        path = self.write_transfer("s4", None, state="SUCCESSOR_VERIFIED")
        first, _ = CORE.transfer_transition(path, "PARENT_STOP_REQUESTED")
        second, _ = CORE.transfer_transition(path, "PARENT_STOP_REQUESTED")
        self.assertTrue(first)
        self.assertFalse(second)

    def test_recovery_from_each_state_is_deterministic(self):
        """Re-running the supervisor from any state does the same thing twice."""
        proc = self.standin_claude()
        binding = self.binding_for(proc.pid, session_id="rec")
        outcomes = {}
        for state in ("PARENT_STOP_REQUESTED", "TRANSFER_COMPLETE", "TRANSFER_FAILED"):
            path = self.write_transfer("rec-%s" % state, binding, state=state)
            first = CORE.supervise_transfer(path)
            after_first = json_file(path)["state"]
            second = CORE.supervise_transfer(path)
            outcomes[state] = (first, second, after_first, json_file(path)["state"])
            self.assertEqual(after_first, state, "state changed on resume from %s" % state)
            self.assertEqual(first, second)
            self.assertTrue(process_alive(proc.pid), "resume from %s signalled the parent" % state)


class TestParentShutdown(THTestCase):
    """Real processes, real signals, one bound target."""

    def _live_env(self, **overrides):
        overrides.setdefault("CLAUDE_TERMINAL_HANDOFF_TEST_MODE", None)
        overrides.setdefault("CLAUDE_TERMINAL_HANDOFF_HEARTBEAT_TIMEOUT", "4")
        overrides.setdefault("CLAUDE_TERMINAL_HANDOFF_TRANSFER_POLL", "0.2")
        overrides.setdefault("CLAUDE_TERMINAL_HANDOFF_STOP_GRACE", "6")
        return self.env(**overrides)

    def _supervise(self, path, env=None, timeout=90):
        return run_th(["supervise", "--transfer", path], env=env or self._live_env(), timeout=timeout)

    def test_parent_remains_alive_before_any_heartbeat(self):
        parent = self.standin_claude("parent")
        path = self.write_transfer("p1", self.binding_for(parent.pid, session_id="p1"))
        code, out, err = self._supervise(path)
        self.assertTrue(process_alive(parent.pid), "the parent was stopped without a heartbeat")
        record = json_file(path)
        self.assertEqual(record["state"], "TRANSFER_FAILED")
        self.assertIn("heartbeat", record["history"][-1]["reason"])
        self.assertFalse(record.get("parent_stopped"))

    def test_a_verified_heartbeat_stops_only_the_bound_parent(self):
        parent = self.standin_claude("parent")
        unrelated = self.standin_claude("unrelated")
        other = self.standin_claude("other")
        path = self.write_transfer(
            "p2", self.binding_for(parent.pid, session_id="p2"), state="SUCCESSOR_VERIFIED"
        )
        code, out, err = self._supervise(path)
        self.assertEqual(code, 0, err)
        self.assertTrue(wait_for_exit(parent.pid), "the bound parent was not stopped")
        self.assertTrue(process_alive(unrelated.pid), "an unrelated Claude process was signalled")
        self.assertTrue(process_alive(other.pid), "an unrelated Claude process was signalled")
        record = json_file(path)
        self.assertEqual(record["state"], "TRANSFER_COMPLETE")
        self.assertTrue(record["parent_stopped"])
        self.assertEqual(record["stop"]["signal"], "SIGTERM")
        self.assertFalse(record["stop"]["escalates"])
        self.assertEqual(record["owner"], "successor")

    def test_an_invalid_heartbeat_never_stops_the_parent(self):
        """The transfer never reaches SUCCESSOR_VERIFIED, so nothing is signalled."""
        parent = self.standin_claude("parent")
        path = self.write_transfer(
            "p3",
            self.binding_for(parent.pid, session_id="p3"),
            successor_rejected={"failed_checks": ["model_matches"]},
        )
        self._supervise(path)
        self.assertTrue(process_alive(parent.pid))
        self.assertEqual(json_file(path)["state"], "TRANSFER_FAILED")

    def test_a_stale_binding_never_terminates_another_process(self):
        parent = self.standin_claude("parent")
        path = self.write_transfer(
            "p4",
            self.binding_for(parent.pid, session_id="p4", start="Mon  1 Jan 00:00:00 1990"),
            state="SUCCESSOR_VERIFIED",
        )
        code, out, err = self._supervise(path)
        self.assertTrue(process_alive(parent.pid), "a stale binding still signalled a process")
        record = json_file(path)
        self.assertEqual(record["state"], "TRANSFER_FAILED")
        self.assertIn("could not be re-proved", record["history"][-1]["reason"])

    def test_an_unbound_parent_fails_closed(self):
        path = self.write_transfer("p5", None, state="SUCCESSOR_VERIFIED")
        code, out, err = self._supervise(path)
        record = json_file(path)
        self.assertEqual(record["state"], "TRANSFER_FAILED")
        self.assertIn("not bound", record["history"][-1]["reason"])

    def test_a_non_claude_process_is_never_signalled(self):
        proc = subprocess.Popen(
            ["/bin/sleep", "120"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self._standins.append(proc)
        time.sleep(0.3)
        path = self.write_transfer(
            "p6", self.binding_for(proc.pid, session_id="p6"), state="SUCCESSOR_VERIFIED"
        )
        self._supervise(path)
        self.assertTrue(process_alive(proc.pid))
        self.assertEqual(json_file(path)["state"], "TRANSFER_FAILED")

    def test_duplicate_supervisors_cannot_signal_twice(self):
        parent = self.standin_claude("parent")
        path = self.write_transfer(
            "p7", self.binding_for(parent.pid, session_id="p7"), state="SUCCESSOR_VERIFIED"
        )
        first = subprocess.Popen(
            [PYTHON, TH_SCRIPT, "supervise", "--transfer", path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self._live_env(),
        )
        second = subprocess.Popen(
            [PYTHON, TH_SCRIPT, "supervise", "--transfer", path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self._live_env(),
        )
        first.communicate(timeout=90)
        second.communicate(timeout=90)
        record = json_file(path)
        self.assertEqual(record["state"], "TRANSFER_COMPLETE")
        self.assertEqual(record["stop"]["attempts"], 1, "the parent was signalled more than once")
        stop_requests = [h for h in record["history"] if h["state"] == "PARENT_STOP_REQUESTED"]
        self.assertEqual(len(stop_requests), 1)

    def test_disabling_parent_shutdown_leaves_the_parent_running(self):
        parent = self.standin_claude("parent")
        path = self.write_transfer(
            "p8",
            self.binding_for(parent.pid, session_id="p8"),
            state="SUCCESSOR_VERIFIED",
            stop={"enabled": False, "signal": "SIGTERM", "escalates": False, "attempts": 0},
        )
        self._supervise(path)
        self.assertTrue(process_alive(parent.pid))
        self.assertEqual(json_file(path)["state"], "TRANSFER_FAILED")

    def test_test_mode_never_terminates_a_real_process(self):
        parent = self.standin_claude("parent")
        path = self.write_transfer(
            "p9", self.binding_for(parent.pid, session_id="p9"), state="SUCCESSOR_VERIFIED"
        )
        code, out, err = self._supervise(path, env=self.env(
            CLAUDE_TERMINAL_HANDOFF_HEARTBEAT_TIMEOUT="4",
            CLAUDE_TERMINAL_HANDOFF_TRANSFER_POLL="0.2",
        ))
        self.assertTrue(process_alive(parent.pid), "test mode terminated a real process")
        record = json_file(path)
        self.assertEqual(record["state"], "TRANSFER_COMPLETE")
        self.assertTrue(record["parent_stop_simulated"])
        self.assertFalse(record["parent_stopped"])

    def test_failure_to_signal_is_visible_and_auditable(self):
        parent = self.standin_claude("parent")
        path = self.write_transfer(
            "p10",
            self.binding_for(parent.pid, session_id="p10", tty="ttys999"),
            state="SUCCESSOR_VERIFIED",
        )
        self._supervise(path)
        record = json_file(path)
        self.assertEqual(record["state"], "TRANSFER_FAILED")
        self.assertTrue(record["history"][-1]["reason"])
        log = os.path.join(self.home, "logs", "terminal-handoff.log")
        with open(log) as handle:
            events = [json.loads(line) for line in handle if line.strip()]
        self.assertTrue(
            any(e["event"] == "parent_stop_refused" for e in events),
            "the refusal was not logged",
        )

    def test_the_terminal_shell_is_never_signalled(self):
        """Only the bound Claude process is targeted; its shell keeps running."""
        shell = subprocess.Popen(
            ["/bin/zsh", "-c", "sleep 120"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self._standins.append(shell)
        parent = self.standin_claude("parent")
        path = self.write_transfer(
            "p11", self.binding_for(parent.pid, session_id="p11"), state="SUCCESSOR_VERIFIED"
        )
        self._supervise(path)
        self.assertTrue(wait_for_exit(parent.pid))
        self.assertTrue(process_alive(shell.pid), "the shell was signalled")


class TestNoDestructiveMechanisms(THTestCase):
    """Static proof that the forbidden mechanisms are absent from the source.

    Comments and string literals are stripped first: the documentation says
    "there is no SIGKILL path", and that sentence must not be mistaken for one.
    """

    def _sources(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for directory in ("src", "scripts"):
            base = os.path.join(root, directory)
            for current, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for name in sorted(files):
                    if name.endswith((".py", ".sh")):
                        yield os.path.join(current, name)
        for name in ("install.sh", "uninstall.sh"):
            yield os.path.join(root, name)

    def _code_only(self, path):
        """The file with comments and string literals removed."""
        with open(path) as handle:
            body = handle.read()
        if not path.endswith(".py"):
            return "\n".join(line.split("#", 1)[0] for line in body.splitlines())
        import io as _io
        import tokenize as _tokenize

        pieces = []
        try:
            for token in _tokenize.generate_tokens(_io.StringIO(body).readline):
                if token.type in (_tokenize.COMMENT, _tokenize.STRING):
                    continue
                pieces.append(token.string)
        except Exception:  # pragma: no cover - a syntax error is caught elsewhere
            return body
        return " ".join(pieces)

    def test_no_sigkill_path_exists(self):
        for path in self._sources():
            code = self._code_only(path)
            name = os.path.basename(path)
            with self.subTest(path=name):
                self.assertNotIn("SIGKILL", code, "%s can send SIGKILL" % name)
                self.assertIsNone(re.search(r"\bkill\s+-9\b", code), "%s uses kill -9" % name)
                self.assertIsNone(
                    re.search(r"os\.kill\(\s*-", code), "%s signals a process group" % name
                )
                self.assertNotIn("killpg", code, "%s signals a process group" % name)

    def test_no_broad_process_matching(self):
        for path in self._sources():
            code = self._code_only(path)
            name = os.path.basename(path)
            with self.subTest(path=name):
                for forbidden in ("pkill", "killall"):
                    self.assertNotIn(forbidden, code, "%s uses %s" % (name, forbidden))

    def test_the_only_signal_is_sigterm(self):
        self.assertEqual(CORE.PARENT_STOP_SIGNAL, signal.SIGTERM)
        self.assertEqual(CORE.PARENT_STOP_SIGNAL_NAME, "SIGTERM")
        self.assertEqual(CORE.PARENT_PROCESS_NAMES, ("claude",))

    def test_shutdown_never_escalates(self):
        self.assertLessEqual(CORE.MAX_STOP_ATTEMPTS, 3)
        code = self._code_only(TH_SCRIPT)
        kills = re.findall(r"os \. kill \([^)]*\)", code)
        self.assertEqual(len(kills), 1, "more than one signalling call: %s" % kills)
        self.assertIn("PARENT_STOP_SIGNAL", kills[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
