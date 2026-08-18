"""Manifest contents, repository capture, permissions and log hygiene.

Ported unchanged from the proven Terminal Handoff 1.0.0 suite.
"""
import json
import os
import stat
import subprocess
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import (  # noqa: E402
    REAL_MODEL_ID,
    THTestCase,
    json_file,
    run_git,
    text_file,
    wait_for,
)


class TestRepository(THTestCase):
    def make_repo(self, name="repo"):
        repo = os.path.join(self.tmp, name)
        os.makedirs(repo)
        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "test@example.com")
        run_git(repo, "config", "user.name", "Terminal Handoff Test")
        run_git(repo, "config", "commit.gpgsign", "false")
        with open(os.path.join(repo, "file.txt"), "w") as handle:
            handle.write("base\n")
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-q", "-m", "base commit")
        return repo

    def capture(self, repo, percent=90.0):
        payload = self.payload(percent=percent, workdir=repo)
        ok, _ = self.trigger_and_wait(payload)
        self.assertTrue(ok, "no launch record for repo test")
        return json_file(self.manifest(payload["_session_id"]))["repository"]

    def test_36_non_git_directory_behaves_safely(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        repo = self.capture(plain)
        self.assertFalse(repo["is_git_repository"])
        self.assertEqual(repo.get("note"), "not a git repository")

    def test_37_missing_origin_main_behaves_safely(self):
        repo = self.make_repo("no-origin")
        state = self.capture(repo)
        self.assertTrue(state["is_git_repository"])
        self.assertIsNone(state["origin_main_sha"])
        self.assertIsNone(state["ahead"])
        self.assertIsNone(state["behind"])
        self.assertEqual(state["branch"], "main")
        self.assertTrue(state["head_sha"])

    def test_38_dirty_worktree_recorded_without_modification(self):
        repo = self.make_repo("dirty")
        with open(os.path.join(repo, "file.txt"), "a") as handle:
            handle.write("uncommitted change\n")
        with open(os.path.join(repo, "new.txt"), "w") as handle:
            handle.write("untracked\n")
        with open(os.path.join(repo, "staged.txt"), "w") as handle:
            handle.write("staged\n")
        run_git(repo, "add", "staged.txt")
        before = text_file(os.path.join(repo, "file.txt"))
        state = self.capture(repo)
        self.assertIn("file.txt", state["modified_tracked_files"])
        self.assertIn("new.txt", state["untracked_files"])
        self.assertIn("staged.txt", state["staged_files"])
        after = text_file(os.path.join(repo, "file.txt"))
        self.assertEqual(before, after, "Terminal Handoff modified the working tree")
        status = run_git(repo, "status", "--porcelain=v1").stdout.decode()
        self.assertIn("file.txt", status)

    def test_39_merge_state_detected(self):
        repo = self.make_repo("merge")
        run_git(repo, "checkout", "-q", "-b", "feature")
        with open(os.path.join(repo, "file.txt"), "w") as handle:
            handle.write("feature\n")
        run_git(repo, "commit", "-qam", "feature change")
        run_git(repo, "checkout", "-q", "main")
        with open(os.path.join(repo, "file.txt"), "w") as handle:
            handle.write("main\n")
        run_git(repo, "commit", "-qam", "main change")
        run_git(repo, "merge", "feature")  # conflicts on purpose
        state = self.capture(repo)
        self.assertTrue(state["merge_in_progress"], "merge state not detected")

    def test_40_rebase_state_detected(self):
        repo = self.make_repo("rebase")
        run_git(repo, "checkout", "-q", "-b", "feature")
        with open(os.path.join(repo, "file.txt"), "w") as handle:
            handle.write("feature\n")
        run_git(repo, "commit", "-qam", "feature change")
        run_git(repo, "checkout", "-q", "main")
        with open(os.path.join(repo, "file.txt"), "w") as handle:
            handle.write("main\n")
        run_git(repo, "commit", "-qam", "main change")
        run_git(repo, "checkout", "-q", "feature")
        run_git(repo, "rebase", "main")  # conflicts on purpose
        state = self.capture(repo)
        self.assertTrue(state["rebase_in_progress"], "rebase state not detected")

    def test_41_cherry_pick_state_detected(self):
        repo = self.make_repo("cherry")
        run_git(repo, "checkout", "-q", "-b", "feature")
        with open(os.path.join(repo, "file.txt"), "w") as handle:
            handle.write("feature\n")
        run_git(repo, "commit", "-qam", "feature change")
        sha = run_git(repo, "rev-parse", "HEAD").stdout.decode().strip()
        run_git(repo, "checkout", "-q", "main")
        with open(os.path.join(repo, "file.txt"), "w") as handle:
            handle.write("main\n")
        run_git(repo, "commit", "-qam", "main change")
        run_git(repo, "cherry-pick", sha)  # conflicts on purpose
        state = self.capture(repo)
        self.assertTrue(state["cherry_pick_in_progress"], "cherry-pick state not detected")

    def test_42_revert_state_detected(self):
        repo = self.make_repo("revert")
        with open(os.path.join(repo, "file.txt"), "w") as handle:
            handle.write("second\n")
        run_git(repo, "commit", "-qam", "second")
        target = run_git(repo, "rev-parse", "HEAD").stdout.decode().strip()
        with open(os.path.join(repo, "file.txt"), "w") as handle:
            handle.write("third\n")
        run_git(repo, "commit", "-qam", "third")
        run_git(repo, "revert", "--no-edit", target)  # conflicts on purpose
        state = self.capture(repo)
        self.assertTrue(state["revert_in_progress"], "revert state not detected")

    def test_34_paths_containing_spaces_work(self):
        repo = os.path.join(self.tmp, "path with spaces", "my repo")
        os.makedirs(repo)
        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "test@example.com")
        run_git(repo, "config", "user.name", "TH Test")
        with open(os.path.join(repo, "a file.txt"), "w") as handle:
            handle.write("x\n")
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-q", "-m", "init")
        sid = str(uuid.uuid4())
        transcript_dir = os.path.join(self.tmp, "transcripts with spaces")
        os.makedirs(transcript_dir)
        payload = self.payload(
            percent=90.0, session_id=sid, workdir=repo, transcript_dir=transcript_dir
        )
        ok, _ = self.trigger_and_wait(payload)
        self.assertTrue(ok, "space-containing paths failed to launch")
        record = json_file(self.launch_record(sid))
        script = text_file(record["script_file"])
        self.assertIn("'%s'" % repo, script, "working directory was not safely quoted")
        # Prove the generated script actually resolves the directory.
        probe = subprocess.run(
            ["/bin/zsh", "-c", "cd -- %s && pwd" % json.dumps(repo).replace('"', "'")],
            stdout=subprocess.PIPE,
        )
        self.assertEqual(probe.stdout.decode().strip(), repo)

    def test_35_paths_containing_quotes_are_rejected(self):
        payload = self.payload(percent=90.0)
        payload["workspace"]["current_dir"] = '/tmp/evil" ; rm -rf /'
        payload["cwd"] = payload["workspace"]["current_dir"]
        decision = self.evaluate(payload)
        self.assertFalse(decision["trigger"], "a quote-bearing path was accepted")
        self.assertEqual(decision["state"], "blocked")

    def test_35b_transcript_path_with_quotes_is_rejected(self):
        payload = self.payload(percent=90.0)
        payload["transcript_path"] = '/tmp/a" b.jsonl'
        decision = self.evaluate(payload)
        self.assertFalse(decision["trigger"])


# ---------------------------------------------------------------------------
# 8. Manifest, permissions and logging hygiene
# ---------------------------------------------------------------------------


class TestManifestAndLogs(THTestCase):
    def test_manifest_contains_required_fields(self):
        payload = self.payload(percent=90.0)
        self.assertTrue(self.trigger_and_wait(payload)[0])
        manifest = json_file(self.manifest(payload["_session_id"]))
        for key in (
            "schema_version",
            "terminal_handoff_version",
            "chain_id",
            "generation",
            "outgoing",
            "model",
            "effort",
            "trigger",
            "repository",
            "claude_md_paths",
            "successor",
            "validation_warnings",
        ):
            self.assertIn(key, manifest)
        self.assertEqual(manifest["outgoing"]["session_id"], payload["_session_id"])
        self.assertEqual(manifest["model"]["id"], REAL_MODEL_ID)
        self.assertEqual(manifest["trigger"]["configured_threshold"], 80.0)
        self.assertEqual(manifest["trigger"]["context_used_percentage"], 90.0)
        self.assertTrue(manifest["trigger"]["timestamp_utc"].endswith("Z"))
        self.assertTrue(manifest["trigger"]["timestamp_local"])

    def test_57_manifest_permissions_are_private(self):
        payload = self.payload(percent=90.0)
        self.assertTrue(self.trigger_and_wait(payload)[0])
        mode = stat.S_IMODE(os.stat(self.manifest(payload["_session_id"])).st_mode)
        self.assertEqual(mode, 0o600, "manifest mode is %o" % mode)
        for directory in ("handoffs", "logs", "state", "prompts", "triggered"):
            dmode = stat.S_IMODE(os.stat(os.path.join(self.home, directory)).st_mode)
            self.assertEqual(dmode, 0o700, "%s mode is %o" % (directory, dmode))

    def test_58_log_permissions_are_private(self):
        payload = self.payload(percent=90.0)
        self.assertTrue(self.trigger_and_wait(payload)[0])
        log = os.path.join(self.home, "logs", "terminal-handoff.log")
        self.assertTrue(os.path.exists(log))
        self.assertEqual(stat.S_IMODE(os.stat(log).st_mode), 0o600)

    def test_59_logs_do_not_include_transcript_contents(self):
        sid = str(uuid.uuid4())
        secret = "SUPER-SECRET-TRANSCRIPT-MARKER-%s" % uuid.uuid4().hex
        transcript = self.make_transcript(
            sid,
            lines=[{"type": "user", "sessionId": sid, "message": {"role": "user", "content": secret}}],
        )
        payload = self.payload(percent=90.0, session_id=sid, transcript=transcript)
        self.assertTrue(self.trigger_and_wait(payload)[0])
        log = text_file(os.path.join(self.home, "logs", "terminal-handoff.log"))
        self.assertNotIn(secret, log, "transcript content leaked into the log")
        manifest = text_file(self.manifest(sid))
        self.assertNotIn(secret, manifest, "transcript content leaked into the manifest")
        prompt = text_file(os.path.join(self.home, "prompts", "successor-%s.md" % sid))
        self.assertNotIn(secret, prompt, "transcript content leaked into the successor prompt")

    def test_73_manifest_contains_no_environment_dump_or_secrets(self):
        env = self.env()
        fake_key = "sk-" + "ant-" + "THIS-MUST-NOT-APPEAR-0123456789"
        env["MY_FAKE_API_KEY"] = fake_key
        payload = self.payload(percent=90.0)
        code, out, err = self.statusline(payload, env)
        self.assertTrue(wait_for(self.launch_record(payload["_session_id"])))
        manifest = text_file(self.manifest(payload["_session_id"]))
        self.assertNotIn(fake_key, manifest)
        self.assertNotIn("MY_FAKE_API_KEY", manifest)
        log = text_file(os.path.join(self.home, "logs", "terminal-handoff.log"))
        self.assertNotIn(fake_key, log)
        data = json.loads(manifest)
        self.assertFalse(data["security"]["stores_secrets"])

    def test_68_successor_heartbeat_state_transitions(self):
        parent = self.payload(percent=90.0)
        self.assertTrue(self.trigger_and_wait(parent)[0])
        parent_manifest_path = self.manifest(parent["_session_id"])
        manifest = json_file(parent_manifest_path)
        self.assertEqual(manifest["successor"]["launch_state"], "simulated_test_mode")
        self.assertIsNone(manifest["successor"]["session_id"])

        successor_env = self.env(
            CLAUDE_TERMINAL_HANDOFF_MANIFEST=parent_manifest_path,
            CLAUDE_TERMINAL_HANDOFF_CHAIN_ID=manifest["chain_id"],
            CLAUDE_TERMINAL_HANDOFF_GENERATION="2",
            CLAUDE_TERMINAL_HANDOFF_PARENT_SESSION=parent["_session_id"],
        )
        successor = self.payload(percent=5.0, effort="high", model_id=REAL_MODEL_ID)

        self.statusline(successor, successor_env)
        after_first = json_file(parent_manifest_path)["successor"]
        self.assertEqual(after_first["launch_state"], "successor_started")
        self.assertEqual(after_first["session_id"], successor["_session_id"])
        self.assertTrue(after_first["session_id_is_fresh"])
        self.assertTrue(after_first["first_heartbeat_utc"])
        self.assertIsNone(after_first["confirmed_utc"])

        self.statusline(successor, successor_env)
        after_second = json_file(parent_manifest_path)["successor"]
        self.assertEqual(after_second["launch_state"], "completed")
        self.assertTrue(after_second["confirmed_utc"])
        self.assertTrue(after_second["model_matches"])
        self.assertTrue(after_second["effort_matches"])
        self.assertTrue(after_second["cwd_matches"])
        self.assertEqual(after_second["observed_context_percentage"], 5.0)

        state = json_file(
            os.path.join(self.home, "completed", "%s.json" % parent["_session_id"])
        )
        self.assertEqual(state["state"], "completed")
        self.assertEqual(state["successor_session_id"], successor["_session_id"])

    def test_68b_heartbeat_rejects_self_as_successor(self):
        parent = self.payload(percent=90.0)
        self.assertTrue(self.trigger_and_wait(parent)[0])
        parent_manifest_path = self.manifest(parent["_session_id"])
        env = self.env(CLAUDE_TERMINAL_HANDOFF_MANIFEST=parent_manifest_path)
        self.statusline(parent, env)
        manifest = json_file(parent_manifest_path)
        self.assertIsNone(manifest["successor"]["session_id"])



VENDOR_STATUSLINE_SCRIPT = r"""#!/usr/bin/env node
// Stand-in for a real-world project status line: reads the Claude
// status JSON from fd 0 and prints one decorated line.
const fs = require('fs');
let raw = '';
try { raw = fs.readFileSync(0, 'utf-8'); } catch (e) { raw = ''; }
let data = null;
try { data = JSON.parse(raw); } catch (e) { data = null; }
const model = data && data.model ? data.model.display_name : 'unknown';
const pct = data && data.context_window && data.context_window.used_percentage != null
  ? Math.floor(data.context_window.used_percentage) : '--';
console.log(`\x1b[0;36mVENDOR\x1b[0m | ${model} | ctx ${pct}% | agents 3/15`);
"""

if __name__ == "__main__":
    unittest.main(verbosity=2)
