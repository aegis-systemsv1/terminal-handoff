"""The wrapped status-line command must be unreachable from untrusted input.

Terminal Handoff runs a pre-existing status-line command through `/bin/sh -c`.
That command is fixed at install time from the user's own Claude settings. These
tests prove that nothing carried by the status-line JSON - model IDs, effort
values, session IDs, transcript paths, transcript contents, manifest contents -
can alter, extend or escape it.

Each test plants shell metacharacters and a filesystem canary in the payload,
runs the status line through a wrapped command that records exactly how it was
invoked, and asserts the invocation is byte-identical to the configured string
and that no canary was ever created.
"""

import json
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import (  # noqa: E402
    THTestCase,
    json_file,
    run_th,
    text_file,
)

# A wrapped command that records how the shell invoked it: the script path, the
# argument count, every argument, and the stdin it received.
RECORDER = r"""#!/bin/sh
{
  echo "ARGV0=$0"
  echo "ARGC=$#"
  for a in "$@"; do echo "ARG=$a"; done
} > "%s"
cat > "%s"
echo "RECORDER OK"
"""


class TestWrapCommandIntegrity(THTestCase):
    def setUp(self):
        super(TestWrapCommandIntegrity, self).setUp()
        self.invocation = os.path.join(self.tmp, "invocation.txt")
        self.received_stdin = os.path.join(self.tmp, "received.json")
        self.canary_dir = os.path.join(self.tmp, "canaries")
        os.makedirs(self.canary_dir)
        self.recorder = os.path.join(self.tmp, "recorder.sh")
        with open(self.recorder, "w") as handle:
            handle.write(RECORDER % (self.invocation, self.received_stdin))
        os.chmod(self.recorder, 0o700)
        self.wrap_command = self.recorder

    # -- helpers ---------------------------------------------------------

    def canary(self, name):
        return os.path.join(self.canary_dir, name)

    def hostile(self, canary_name):
        """A shell fragment that would create a canary file if ever executed."""
        return '"; touch %s; echo "' % self.canary(canary_name)

    def assert_no_canaries(self):
        created = os.listdir(self.canary_dir)
        self.assertEqual(created, [], "shell injection executed: %s" % created)

    def assert_invocation_clean(self):
        """The wrapped command ran with no arguments appended by anything."""
        self.assertTrue(os.path.exists(self.invocation), "wrapped command never ran")
        recorded = text_file(self.invocation)
        self.assertIn("ARGV0=%s" % self.recorder, recorded)
        self.assertIn("ARGC=0", recorded, "arguments were appended to the wrapped command")
        self.assertNotIn("ARG=", recorded, "an argument reached the wrapped command")

    def run_wrapped(self, payload):
        return self.statusline(payload, wrap=self.wrap_command)

    # -- the wrapped command is exactly what was configured ---------------

    def test_wrapped_command_runs_exactly_as_configured(self):
        code, out, err = self.run_wrapped(self.payload(percent=40.0))
        self.assertEqual(code, 0, err)
        self.assert_invocation_clean()
        self.assertIn("RECORDER OK", out)
        self.assert_no_canaries()

    def test_status_json_reaches_the_wrapped_command_only_as_stdin(self):
        payload = self.payload(percent=40.0)
        clean = {k: v for k, v in payload.items() if k != "_session_id"}
        self.run_wrapped(payload)
        received = json_file(self.received_stdin)
        self.assertEqual(received, clean, "the wrapped command received different data")
        recorded = text_file(self.invocation)
        self.assertNotIn(payload["_session_id"], recorded,
                         "a payload value leaked into the command invocation")

    # -- every untrusted field, one at a time -----------------------------

    def test_hostile_model_id_cannot_alter_the_wrapped_command(self):
        payload = self.payload(percent=40.0)
        payload["model"]["id"] = self.hostile("model")
        code, out, err = self.run_wrapped(payload)
        self.assertEqual(code, 0, err)
        self.assert_invocation_clean()
        self.assert_no_canaries()

    def test_hostile_effort_cannot_alter_the_wrapped_command(self):
        payload = self.payload(percent=40.0)
        payload["effort"]["level"] = self.hostile("effort")
        self.run_wrapped(payload)
        self.assert_invocation_clean()
        self.assert_no_canaries()

    def test_hostile_session_id_cannot_alter_the_wrapped_command(self):
        payload = self.payload(percent=40.0)
        payload["session_id"] = self.hostile("session")
        self.run_wrapped(payload)
        self.assert_invocation_clean()
        self.assert_no_canaries()

    def test_hostile_session_name_cannot_alter_the_wrapped_command(self):
        payload = self.payload(percent=40.0)
        payload["session_name"] = self.hostile("name")
        self.run_wrapped(payload)
        self.assert_invocation_clean()
        self.assert_no_canaries()

    def test_hostile_transcript_path_cannot_alter_the_wrapped_command(self):
        payload = self.payload(percent=40.0)
        payload["transcript_path"] = self.hostile("transcript")
        self.run_wrapped(payload)
        self.assert_invocation_clean()
        self.assert_no_canaries()

    def test_hostile_working_directory_cannot_alter_the_wrapped_command(self):
        payload = self.payload(percent=40.0)
        payload["workspace"]["current_dir"] = self.hostile("cwd")
        payload["cwd"] = payload["workspace"]["current_dir"]
        self.run_wrapped(payload)
        self.assert_invocation_clean()
        self.assert_no_canaries()

    def test_hostile_transcript_contents_cannot_alter_the_wrapped_command(self):
        session_id = str(uuid.uuid4())
        rows = [
            {"type": "user", "sessionId": session_id,
             "message": {"role": "user", "content": self.hostile("transcript-body")}},
            {"type": "assistant", "sessionId": session_id,
             "message": {"role": "assistant", "content": "$(touch %s)" % self.canary("subst")}},
        ]
        transcript = self.make_transcript(session_id, lines=rows)
        payload = self.payload(percent=40.0, session_id=session_id, transcript=transcript)
        self.run_wrapped(payload)
        self.assert_invocation_clean()
        self.assert_no_canaries()
        recorded = text_file(self.invocation)
        self.assertNotIn("transcript-body", recorded)

    def test_every_field_hostile_at_once_including_at_the_threshold(self):
        """The trigger path also builds a manifest and a launch command."""
        session_id = str(uuid.uuid4())
        rows = [{"type": "user", "sessionId": session_id,
                 "message": {"role": "user", "content": self.hostile("all-transcript")}}]
        transcript = self.make_transcript(session_id, lines=rows)
        payload = self.payload(percent=95.0, session_id=session_id, transcript=transcript)
        payload["session_name"] = self.hostile("all-name")
        payload["output_style"] = {"name": self.hostile("all-style")}
        payload["version"] = self.hostile("all-version")
        code, out, err = self.run_wrapped(payload)
        self.assertEqual(code, 0, err)
        self.assert_invocation_clean()
        self.assert_no_canaries()

    # -- the manifest and launch path cannot reach the wrapped command ----

    def test_manifest_contents_cannot_alter_the_wrapped_command(self):
        payload = self.payload(percent=95.0)
        ok, _ = self.trigger_and_wait(payload)
        self.assertTrue(ok, "no launch record produced")
        manifest_path = self.manifest(payload["_session_id"])

        # Tamper with the manifest exactly as a hostile actor would, then run
        # the status line again. The wrapped command must be unaffected.
        manifest = json_file(manifest_path)
        manifest["model"]["id"] = self.hostile("manifest-model")
        manifest["outgoing"]["current_dir"] = self.hostile("manifest-cwd")
        manifest["chain_id"] = self.hostile("manifest-chain")
        with open(manifest_path, "w") as handle:
            json.dump(manifest, handle)

        follow_up = self.payload(percent=40.0)
        env = self.env(CLAUDE_TERMINAL_HANDOFF_MANIFEST=manifest_path)
        code, out, err = self.statusline(follow_up, env, wrap=self.wrap_command)
        self.assertEqual(code, 0, err)
        self.assert_invocation_clean()
        self.assert_no_canaries()

    def test_a_tampered_manifest_cannot_inject_a_wrap_command(self):
        payload = self.payload(percent=95.0)
        self.assertTrue(self.trigger_and_wait(payload)[0])
        manifest_path = self.manifest(payload["_session_id"])
        manifest = json_file(manifest_path)
        manifest["statusLine"] = {"type": "command",
                                  "command": "touch %s" % self.canary("manifest-wrap")}
        manifest["wrap"] = "touch %s" % self.canary("manifest-wrap-2")
        with open(manifest_path, "w") as handle:
            json.dump(manifest, handle)
        env = self.env(CLAUDE_TERMINAL_HANDOFF_MANIFEST=manifest_path)
        self.statusline(self.payload(percent=40.0), env, wrap=self.wrap_command)
        self.assert_no_canaries()

    def test_payload_cannot_supply_its_own_wrap_command(self):
        """A `statusLine` or `wrap` key inside the payload must be ignored."""
        payload = self.payload(percent=40.0)
        payload["statusLine"] = {"type": "command",
                                 "command": "touch %s" % self.canary("payload-statusline")}
        payload["wrap"] = "touch %s" % self.canary("payload-wrap")
        self.run_wrapped(payload)
        self.assert_invocation_clean()
        self.assert_no_canaries()

    def test_no_wrap_configured_means_no_command_is_run(self):
        payload = self.payload(percent=40.0)
        payload["statusLine"] = {"type": "command",
                                 "command": "touch %s" % self.canary("no-wrap")}
        code, out, err = self.statusline(payload)
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(self.invocation),
                         "a command ran with no --wrap configured")
        self.assert_no_canaries()

    # -- the configured command is preserved verbatim ---------------------

    def test_installed_wrap_string_is_byte_identical_to_the_original(self):
        original = "node \"$CLAUDE_PROJECT_DIR/.claude/helpers/statusline.js\" --compact"
        settings = os.path.join(self.tmp, "settings.json")
        with open(settings, "w") as handle:
            json.dump({"statusLine": {"type": "command", "command": original}}, handle, indent=2)
        code, out, err = run_th(["install", "--settings", settings, "--skip-claude-md"], "", self.env())
        self.assertEqual(code, 0, err)

        registry = json_file(os.path.join(self.home, "state", "installed.json"))
        self.assertEqual(registry["targets"][settings]["wrapped_command"], original,
                         "the wrapped command was altered during installation")
        installed = json_file(settings)["statusLine"]["command"]
        import shlex

        parts = shlex.split(installed)
        self.assertIn("--wrap", parts)
        self.assertEqual(parts[parts.index("--wrap") + 1], original,
                         "the wrapped command did not survive quoting byte-for-byte")

    def test_wrapped_command_failure_cannot_break_the_status_line(self):
        code, out, err = self.statusline(self.payload(percent=40.0),
                                         wrap="exit 7; touch %s" % self.canary("after-exit"))
        self.assertEqual(code, 0)
        self.assertIn("TH", out)

    def test_wrapped_command_output_is_not_interpreted(self):
        """Whatever the wrapped command prints is data, never re-executed."""
        emitter = os.path.join(self.tmp, "emitter.sh")
        with open(emitter, "w") as handle:
            handle.write("#!/bin/sh\ncat > /dev/null\nprintf '%s' '$(touch %s)`touch %s`'\n"
                         % ("%s", self.canary("out-subst"), self.canary("out-backtick")))
        os.chmod(emitter, 0o700)
        code, out, err = self.statusline(self.payload(percent=40.0), wrap=emitter)
        self.assertEqual(code, 0, err)
        self.assert_no_canaries()
        self.assertIn("$(touch", out, "the wrapped command's output was not passed through")


if __name__ == "__main__":
    unittest.main(verbosity=2)
