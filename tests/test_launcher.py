"""Successor launch command construction: model, effort, forbidden flags.

Ported unchanged from the proven Terminal Handoff 1.0.0 suite.
"""
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import (  # noqa: E402
    REAL_MODEL_ID,
    THTestCase,
    json_file,
    text_file,
)


class TestEffort(THTestCase):
    def _argv_for(self, payload):
        ok, _ = self.trigger_and_wait(payload)
        self.assertTrue(ok, "no launch record produced")
        record = json_file(self.launch_record(payload["_session_id"]))
        return record["argv"]

    def test_12_missing_effort_is_handled(self):
        payload = self.payload(percent=90.0, include_effort=False)
        decision = self.evaluate(payload)
        self.assertTrue(decision["trigger"], decision)
        self.assertFalse(decision["effort_available"])
        argv = self._argv_for(payload)
        self.assertNotIn("--effort", argv)
        self.assertIn("--model", argv)
        manifest = json_file(self.manifest(payload["_session_id"]))
        self.assertFalse(manifest["effort"]["available"])
        self.assertIsNone(manifest["effort"]["level"])

    def test_13_invalid_effort_is_rejected(self):
        decision = self.evaluate(self.payload(percent=90.0, effort="turbo"))
        self.assertFalse(decision["trigger"])
        self.assertEqual(decision["state"], "blocked")
        self.assertTrue(any("effort.level invalid" in e for e in decision["errors"]))

    def test_13b_effort_non_object_is_rejected(self):
        payload = self.payload(percent=90.0)
        payload["effort"] = "high"
        decision = self.evaluate(payload)
        self.assertFalse(decision["trigger"])

    def test_14_low_effort_preserved(self):
        argv = self._argv_for(self.payload(percent=90.0, effort="low"))
        self.assertEqual(argv[argv.index("--effort") + 1], "low")

    def test_15_medium_effort_preserved(self):
        argv = self._argv_for(self.payload(percent=90.0, effort="medium"))
        self.assertEqual(argv[argv.index("--effort") + 1], "medium")

    def test_16_high_effort_preserved(self):
        argv = self._argv_for(self.payload(percent=90.0, effort="high"))
        self.assertEqual(argv[argv.index("--effort") + 1], "high")

    def test_17_xhigh_effort_preserved(self):
        argv = self._argv_for(self.payload(percent=90.0, effort="xhigh"))
        self.assertEqual(argv[argv.index("--effort") + 1], "xhigh")

    def test_18_max_effort_preserved(self):
        argv = self._argv_for(self.payload(percent=90.0, effort="max"))
        self.assertEqual(argv[argv.index("--effort") + 1], "max")

    def test_19_ultracode_is_not_accepted_as_an_effort_level(self):
        """Documented compatibility behaviour.

        Claude Code's status-line JSON can never report `ultracode`:
        `--effort ultracode` resolves to xhigh plus a hidden internal flag.
        Terminal Handoff must therefore reject it as an effort level rather
        than invent one, and must record that ultracode is undetectable.
        """
        decision = self.evaluate(self.payload(percent=90.0, effort="ultracode"))
        self.assertFalse(decision["trigger"])
        self.assertEqual(decision["state"], "blocked")
        payload = self.payload(percent=90.0, effort="xhigh")
        self.trigger_and_wait(payload)
        manifest = json_file(self.manifest(payload["_session_id"]))
        self.assertEqual(manifest["effort"]["ultracode"], "undetectable")
        self.assertIn("ultracode", manifest["effort"]["note"])


# ---------------------------------------------------------------------------
# 4. Model preservation and command construction
# ---------------------------------------------------------------------------


class TestLaunchCommand(THTestCase):
    def _record(self, payload):
        ok, _ = self.trigger_and_wait(payload)
        self.assertTrue(ok, "no launch record produced")
        return json_file(self.launch_record(payload["_session_id"]))

    def test_20_exact_model_id_preserved_with_brackets(self):
        payload = self.payload(percent=88.0, model_id=REAL_MODEL_ID)
        argv = self._record(payload)["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], REAL_MODEL_ID)

    def test_20b_other_exact_model_ids_preserved(self):
        for model in ("claude-opus-5", "claude-sonnet-5", "claude-fable-5[1m]", "claude-haiku-4-5-20251001"):
            with self.subTest(model=model):
                payload = self.payload(percent=88.0, model_id=model)
                argv = self._record(payload)["argv"]
                self.assertEqual(argv[argv.index("--model") + 1], model)

    def test_21_no_default_model_substitution(self):
        payload = self.payload(percent=88.0, model_id="claude-opus-5[1m]")
        argv = self._record(payload)["argv"]
        model = argv[argv.index("--model") + 1]
        self.assertNotIn(model, ("opus", "sonnet", "fable", "haiku", "default"))
        self.assertEqual(model, "claude-opus-5[1m]")
        self.assertNotIn("--fallback-model", argv)

    def test_unsafe_model_id_is_rejected(self):
        for bad in ('claude"; rm -rf /', "claude$(whoami)", "claude`id`", "claude 5", "claude\nx"):
            with self.subTest(model=bad):
                decision = self.evaluate(self.payload(percent=90.0, model_id=bad))
                self.assertFalse(decision["trigger"])

    def test_44_command_contains_model_with_correct_argument(self):
        payload = self.payload(percent=88.0, model_id=REAL_MODEL_ID)
        argv = self._record(payload)["argv"]
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], REAL_MODEL_ID)

    def test_45_command_contains_effort_with_correct_argument(self):
        payload = self.payload(percent=88.0, effort="xhigh")
        argv = self._record(payload)["argv"]
        self.assertIn("--effort", argv)
        self.assertEqual(argv[argv.index("--effort") + 1], "xhigh")

    def test_46_47_48_49_no_forbidden_flags(self):
        payload = self.payload(percent=88.0)
        record = self._record(payload)
        argv = record["argv"]
        flags = argv[:-1]
        for forbidden in (
            "--continue",
            "-c",
            "--resume",
            "-r",
            "--fork-session",
            "--dangerously-skip-permissions",
            "--allow-dangerously-skip-permissions",
        ):
            self.assertNotIn(forbidden, flags, "forbidden flag %s present" % forbidden)
        script = text_file(record["script_file"])
        for forbidden in ("--continue", "--resume", "--fork-session", "dangerously-skip-permissions"):
            self.assertNotIn(forbidden, script)

    def test_session_id_flag_is_not_used(self):
        payload = self.payload(percent=88.0)
        argv = self._record(payload)["argv"]
        self.assertNotIn("--session-id", argv)

    def test_69_bracket_model_survives_shell_quoting(self):
        """The generated launch script must pass claude-opus-5[1m] literally."""
        payload = self.payload(percent=88.0, model_id=REAL_MODEL_ID)
        record = self._record(payload)
        script = record["script_file"]
        probe = os.path.join(self.tmp, "probe.sh")
        argv_out = os.path.join(self.tmp, "argv.txt")
        # Replace the fake claude with one that records its argv verbatim.
        with open(self.fake_claude, "w") as handle:
            handle.write('#!/bin/sh\nfor a in "$@"; do echo "$a"; done > %s\n' % argv_out)
        os.chmod(self.fake_claude, 0o700)
        with open(probe, "w") as handle:
            handle.write("#!/bin/zsh\n" + text_file(script).split("\n", 1)[1])
        os.chmod(probe, 0o700)
        subprocess.run(["/bin/zsh", probe], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        captured = text_file(argv_out).splitlines()
        self.assertIn(REAL_MODEL_ID, captured, "model ID was mangled by the shell: %r" % captured)

    def test_74_prompt_is_delivered_by_file_not_argv(self):
        payload = self.payload(percent=88.0)
        record = self._record(payload)
        bootstrap = record["argv"][-1]
        self.assertLess(len(bootstrap), 500, "bootstrap prompt is too large for a process listing")
        self.assertIn(record["prompt_file"], bootstrap)
        prompt = text_file(record["prompt_file"])
        self.assertIn("TERMINAL HANDOFF SUCCESSOR", prompt)
        self.assertIn("context-isolated subagent", prompt)
        transcript_body = text_file(payload["transcript_path"])
        self.assertNotIn("do the work", prompt, "transcript content leaked into the prompt")
        self.assertNotIn(transcript_body.strip(), prompt)


# ---------------------------------------------------------------------------
# 5. One trigger per session, and successor-to-successor looping
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
