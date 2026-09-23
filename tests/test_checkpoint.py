"""`th checkpoint` - deterministic, machine-verifiable session checkpoints.

Slice 1 only: no AI summary, no Smart Compact, no `th resume`, no Codex
integration. Every test here proves one of: correct capture of real git
state, explicit (never null) representation of unavailable optional state,
secret redaction on every free-text field, deterministic serialization,
integrity hashing, fail-closed behaviour, and file permissions.
"""
import json
import os
import stat
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import (  # noqa: E402
    CORE,
    THTestCase,
    json_file,
    run_git,
    run_th,
)


class CheckpointTestCase(THTestCase):
    """Shared git-repo fixture builder for checkpoint tests."""

    def make_repo(self, name="repo", origin=False):
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
        if origin:
            bare = os.path.join(self.tmp, "%s-origin.git" % name)
            run_git(self.tmp, "init", "-q", "--bare", bare)
            run_git(repo, "remote", "add", "origin", bare)
            run_git(repo, "push", "-q", "-u", "origin", "main")
        return repo

    def checkpoint_cli(self, repo, extra_args=None, expect_ok=True):
        args = ["checkpoint", "--repo", repo, "--json"] + list(extra_args or [])
        code, out, err = run_th(args, env=self.env())
        if expect_ok:
            self.assertEqual(code, 0, "checkpoint failed unexpectedly: %s" % err)
            return json.loads(out), out, err
        return code, out, err


# ---------------------------------------------------------------------------
# Repository state capture
# ---------------------------------------------------------------------------


class TestRepositoryCapture(CheckpointTestCase):
    def test_clean_repository(self):
        repo = self.make_repo("clean")
        summary, _, _ = self.checkpoint_cli(repo)
        self.assertFalse(summary["dirty"])
        self.assertEqual(summary["branch"], "main")
        self.assertFalse(summary["detached_head"])
        expected_sha = run_git(repo, "rev-parse", "HEAD").stdout.decode().strip()
        self.assertEqual(summary["head_sha"], expected_sha)

        doc = json_file(summary["path"])
        self.assertEqual(doc["working_tree"]["staged_files"], [])
        self.assertEqual(doc["working_tree"]["modified_files"], [])
        self.assertEqual(doc["working_tree"]["untracked_files"], [])
        self.assertEqual(doc["git"]["provenance"], "machine_verified")

    def test_dirty_repository_modified_file(self):
        repo = self.make_repo("dirty")
        with open(os.path.join(repo, "file.txt"), "a") as handle:
            handle.write("uncommitted change\n")
        summary, _, _ = self.checkpoint_cli(repo)
        self.assertTrue(summary["dirty"])
        doc = json_file(summary["path"])
        paths = [e["path"] for e in doc["working_tree"]["modified_files"]]
        self.assertIn("file.txt", paths)

    def test_staged_files(self):
        repo = self.make_repo("staged")
        with open(os.path.join(repo, "staged.txt"), "w") as handle:
            handle.write("staged\n")
        run_git(repo, "add", "staged.txt")
        summary, _, _ = self.checkpoint_cli(repo)
        doc = json_file(summary["path"])
        paths = [e["path"] for e in doc["working_tree"]["staged_files"]]
        self.assertIn("staged.txt", paths)
        for entry in doc["working_tree"]["staged_files"]:
            self.assertFalse(entry["sensitive"])

    def test_untracked_files(self):
        repo = self.make_repo("untracked")
        with open(os.path.join(repo, "new.txt"), "w") as handle:
            handle.write("untracked\n")
        summary, _, _ = self.checkpoint_cli(repo)
        doc = json_file(summary["path"])
        paths = [e["path"] for e in doc["working_tree"]["untracked_files"]]
        self.assertIn("new.txt", paths)

    def test_working_tree_never_captures_file_contents(self):
        repo = self.make_repo("no-contents")
        secret_body = "AWS_SECRET=sk-FAKEsecretDoNotLeak1234567890"
        with open(os.path.join(repo, "new.txt"), "w") as handle:
            handle.write(secret_body)
        summary, _, _ = self.checkpoint_cli(repo)
        with open(summary["path"]) as handle:
            raw = handle.read()
        self.assertNotIn(secret_body, raw)
        self.assertNotIn("DoNotLeak", raw)

    def test_detached_head(self):
        repo = self.make_repo("detached")
        with open(os.path.join(repo, "file.txt"), "w") as handle:
            handle.write("second\n")
        run_git(repo, "commit", "-qam", "second commit")
        first_sha = run_git(repo, "rev-list", "--max-parents=0", "HEAD").stdout.decode().strip()
        run_git(repo, "checkout", "-q", first_sha)
        summary, _, _ = self.checkpoint_cli(repo)
        self.assertTrue(summary["detached_head"])
        self.assertIsNone(summary["branch"])
        self.assertEqual(summary["head_sha"], first_sha)
        doc = json_file(summary["path"])
        self.assertFalse(doc["git"]["branch"]["available"])
        self.assertIn("reason", doc["git"]["branch"])

    def test_no_upstream_configured(self):
        repo = self.make_repo("no-upstream", origin=False)
        summary, _, _ = self.checkpoint_cli(repo)
        doc = json_file(summary["path"])
        self.assertFalse(doc["git"]["upstream"]["available"])
        self.assertIn("reason", doc["git"]["upstream"])
        self.assertNotIn("ahead", doc["git"]["upstream"])

    def test_upstream_configured(self):
        repo = self.make_repo("with-upstream", origin=True)
        summary, _, _ = self.checkpoint_cli(repo)
        doc = json_file(summary["path"])
        self.assertTrue(doc["git"]["upstream"]["available"])
        self.assertEqual(doc["git"]["upstream"]["name"], "origin/main")
        self.assertEqual(doc["git"]["upstream"]["ahead"], 0)
        self.assertEqual(doc["git"]["upstream"]["behind"], 0)


# ---------------------------------------------------------------------------
# Sensitive filenames
# ---------------------------------------------------------------------------


class TestSensitiveFilenames(CheckpointTestCase):
    def test_sensitive_filenames_flagged_not_hidden(self):
        repo = self.make_repo("sensitive")
        with open(os.path.join(repo, ".env"), "w") as handle:
            handle.write("API_KEY=sk-FAKEenvSecretDoNotLeak0000000000\n")
        with open(os.path.join(repo, "id_rsa"), "w") as handle:
            handle.write("-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n")
        with open(os.path.join(repo, "ordinary.py"), "w") as handle:
            handle.write("print('hi')\n")
        summary, _, _ = self.checkpoint_cli(repo)
        doc = json_file(summary["path"])
        by_path = {e["path"]: e for e in doc["working_tree"]["untracked_files"]}

        self.assertIn(".env", by_path)
        self.assertTrue(by_path[".env"]["sensitive"])
        self.assertIn("reason", by_path[".env"])

        self.assertIn("id_rsa", by_path)
        self.assertTrue(by_path["id_rsa"]["sensitive"])

        self.assertIn("ordinary.py", by_path)
        self.assertFalse(by_path["ordinary.py"]["sensitive"])

        # Filenames are visible (that is the point); contents never are.
        with open(summary["path"]) as handle:
            raw = handle.read()
        self.assertNotIn("DoNotLeak", raw)
        self.assertNotIn("BEGIN PRIVATE KEY", raw)


# ---------------------------------------------------------------------------
# Secret redaction (adversarial fixtures)
# ---------------------------------------------------------------------------


class TestSecretRedaction(CheckpointTestCase):
    def test_secret_in_commit_message_redacted(self):
        repo = self.make_repo("commit-secret")
        with open(os.path.join(repo, "file.txt"), "w") as handle:
            handle.write("second\n")
        run_git(
            repo, "commit", "-qam",
            "temp: hardcode sk-FAKEcommitSecretDoNotLeak00000000 before merge",
        )
        summary, _, _ = self.checkpoint_cli(repo)
        doc = json_file(summary["path"])
        subjects = " ".join(c["subject"] for c in doc["history"]["recent_commits"])
        self.assertNotIn("DoNotLeak", subjects)
        self.assertIn("[redacted]", subjects)

    def test_secret_in_test_command_redacted(self):
        repo = self.make_repo("cmd-secret")
        summary, _, _ = self.checkpoint_cli(
            repo,
            [
                "--test-command",
                "API_KEY=sk-FAKEcmdSecretDoNotLeak0000000000 pytest tests/",
                "--test-exit-code", "0",
                "--test-output", "1 passed",
            ],
        )
        doc = json_file(summary["path"])
        self.assertNotIn("DoNotLeak", doc["tests"]["command"])
        self.assertIn("[redacted]", doc["tests"]["command"])

    def test_secret_in_test_output_redacted(self):
        repo = self.make_repo("output-secret")
        summary, _, _ = self.checkpoint_cli(
            repo,
            [
                "--test-command", "pytest tests/",
                "--test-exit-code", "1",
                "--test-output",
                "AssertionError: token=sk-FAKEoutputSecretDoNotLeak000000 was rejected",
            ],
        )
        doc = json_file(summary["path"])
        self.assertNotIn("DoNotLeak", doc["tests"]["output_tail"])
        self.assertIn("[redacted]", doc["tests"]["output_tail"])
        self.assertEqual(doc["tests"]["exit_code"], 1)
        self.assertFalse(doc["tests"]["successful_execution"])

    def test_private_key_block_fully_redacted_body_and_all(self):
        # redact_secrets() now removes the whole PEM block - header, base64
        # body and footer - not just the header line. The body is the actual
        # key material, so leaving it behind after redacting only the header
        # would have been the real security hole.
        repo = self.make_repo("pem-secret")
        body = (
            "-----BEGIN EC PRIVATE KEY-----\n"
            "MIIFAKEbodyDoNotLeakLine1xxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n"
            "MIIFAKEbodyDoNotLeakLine2yyyyyyyyyyyyyyyyyyyyyyyyyyyyy\n"
            "-----END EC PRIVATE KEY-----"
        )
        summary, _, _ = self.checkpoint_cli(
            repo,
            ["--test-command", "deploy.sh", "--test-exit-code", "0", "--test-output", body],
        )
        doc = json_file(summary["path"])
        tail = doc["tests"]["output_tail"]
        self.assertNotIn("BEGIN EC PRIVATE KEY", tail)
        self.assertNotIn("DoNotLeak", tail)
        self.assertNotIn("MIIFAKE", tail)
        self.assertEqual(tail, "[redacted private key]")

    def test_private_key_without_footer_still_fully_redacted(self):
        # Output truncated upstream (no closing footer in this call's text)
        # must not leave partial key data trailing after the marker.
        repo = self.make_repo("pem-no-footer")
        body = "-----BEGIN EC PRIVATE KEY-----\nMIIFAKEtruncatedDoNotLeakxxxxxxxxxxxxxxx"
        summary, _, _ = self.checkpoint_cli(
            repo,
            ["--test-command", "deploy.sh", "--test-exit-code", "0", "--test-output", body],
        )
        doc = json_file(summary["path"])
        tail = doc["tests"]["output_tail"]
        self.assertNotIn("DoNotLeak", tail)
        self.assertEqual(tail, "[redacted private key]")

    def test_two_private_keys_each_redacted_without_swallowing_text_between(self):
        repo = self.make_repo("pem-two-keys")
        body = (
            "-----BEGIN EC PRIVATE KEY-----\nKEYONEbodyDoNotLeak\n-----END EC PRIVATE KEY-----\n"
            "unrelated log line that must survive\n"
            "-----BEGIN EC PRIVATE KEY-----\nKEYTWObodyDoNotLeak\n-----END EC PRIVATE KEY-----"
        )
        summary, _, _ = self.checkpoint_cli(
            repo,
            ["--test-command", "deploy.sh", "--test-exit-code", "0", "--test-output", body],
        )
        doc = json_file(summary["path"])
        tail = doc["tests"]["output_tail"]
        self.assertNotIn("DoNotLeak", tail)
        self.assertIn("unrelated log line that must survive", tail)
        self.assertEqual(tail.count("[redacted private key]"), 2)


# ---------------------------------------------------------------------------
# Explicit unavailable state (never a bare, authoritative-looking null)
# ---------------------------------------------------------------------------


class TestUnavailableState(CheckpointTestCase):
    def test_tests_block_unavailable_when_no_evidence_given(self):
        repo = self.make_repo("no-tests")
        summary, _, _ = self.checkpoint_cli(repo)
        doc = json_file(summary["path"])
        self.assertEqual(doc["tests"]["provenance"], "unavailable")
        self.assertIn("reason", doc["tests"])
        self.assertNotIn("exit_code", doc["tests"])

    def test_session_block_unavailable_without_session_id(self):
        repo = self.make_repo("no-session")
        summary, _, _ = self.checkpoint_cli(repo)
        doc = json_file(summary["path"])
        self.assertEqual(doc["session"]["provenance"], "unavailable")
        self.assertIn("reason", doc["session"])

    def test_session_block_recorded_evidence_with_session_id(self):
        repo = self.make_repo("with-session")
        summary, _, _ = self.checkpoint_cli(repo, ["--session-id", "abc-123", "--agent-type", "claude"])
        doc = json_file(summary["path"])
        self.assertEqual(doc["session"]["provenance"], "recorded_evidence")
        self.assertEqual(doc["session"]["session_id"], "abc-123")

    def test_conflicting_test_flags_degrade_to_unavailable_not_crash(self):
        repo = self.make_repo("conflicting-flags")
        summary, _, _ = self.checkpoint_cli(
            repo,
            ["--test-command", "pytest", "--run-tests", "--test-exit-code", "0"],
        )
        doc = json_file(summary["path"])
        self.assertEqual(doc["tests"]["provenance"], "unavailable")
        self.assertIn("mutually exclusive", doc["tests"]["reason"])


# ---------------------------------------------------------------------------
# Test-evidence capture (both modes)
# ---------------------------------------------------------------------------


class TestEvidenceCapture(CheckpointTestCase):
    def test_run_tests_executes_and_records_machine_verified(self):
        repo = self.make_repo("run-tests")
        summary, _, _ = self.checkpoint_cli(
            repo, ["--run-tests", "--test-command", "/bin/echo hello-from-test"]
        )
        doc = json_file(summary["path"])
        self.assertEqual(doc["tests"]["provenance"], "machine_verified")
        self.assertEqual(doc["tests"]["executed_by"], "terminal_handoff")
        self.assertEqual(doc["tests"]["exit_code"], 0)
        self.assertTrue(doc["tests"]["successful_execution"])
        self.assertIn("hello-from-test", doc["tests"]["output_tail"])

    def test_recorded_evidence_is_not_executed(self):
        repo = self.make_repo("recorded-evidence")
        marker = os.path.join(repo, "should-not-exist.txt")
        summary, _, _ = self.checkpoint_cli(
            repo,
            [
                "--test-command", "touch %s" % marker,
                "--test-exit-code", "0",
                "--test-output", "1 passed",
            ],
        )
        doc = json_file(summary["path"])
        self.assertEqual(doc["tests"]["provenance"], "recorded_evidence")
        self.assertEqual(doc["tests"]["executed_by"], "caller_reported")
        self.assertFalse(os.path.exists(marker), "Terminal Handoff executed a command marked as recorded evidence")

    def test_never_infers_pass_from_output_text(self):
        repo = self.make_repo("no-text-inference")
        summary, _, _ = self.checkpoint_cli(
            repo,
            [
                "--test-command", "pytest",
                "--test-exit-code", "1",
                "--test-output", "ALL TESTS PASSED!!! 100% success",
            ],
        )
        doc = json_file(summary["path"])
        self.assertFalse(doc["tests"]["successful_execution"], "PASS was inferred from output text, not exit code")
        self.assertEqual(doc["tests"]["exit_code"], 1)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism(CheckpointTestCase):
    def test_unchanged_repository_serializes_identically_except_metadata(self):
        repo = self.make_repo("deterministic")
        first = CORE.build_checkpoint(repo_path=repo)
        second = CORE.build_checkpoint(repo_path=repo)
        for block in ("git", "working_tree", "history", "session", "tests"):
            self.assertEqual(first[block], second[block], "block %r was not deterministic" % block)
        # These are expected to differ between two independent checkpoints.
        self.assertNotEqual(first["checkpoint_id"], second["checkpoint_id"])
        self.assertNotEqual(first["integrity"]["content_sha256"], second["integrity"]["content_sha256"])


# ---------------------------------------------------------------------------
# Integrity
# ---------------------------------------------------------------------------


class TestIntegrity(CheckpointTestCase):
    def test_integrity_hash_verifies_untouched_checkpoint(self):
        repo = self.make_repo("integrity-ok")
        checkpoint = CORE.build_checkpoint(repo_path=repo)
        self.assertTrue(CORE.verify_checkpoint_integrity(checkpoint))

    def test_integrity_excludes_itself_from_the_hash(self):
        repo = self.make_repo("integrity-self")
        checkpoint = CORE.build_checkpoint(repo_path=repo)
        recomputed = CORE.compute_checkpoint_integrity(checkpoint)
        self.assertEqual(recomputed["content_sha256"], checkpoint["integrity"]["content_sha256"])

    def test_corrupted_checkpoint_detected(self):
        repo = self.make_repo("corrupted")
        checkpoint = CORE.build_checkpoint(repo_path=repo)
        checkpoint["git"]["dirty"] = not checkpoint["git"]["dirty"]
        self.assertFalse(CORE.verify_checkpoint_integrity(checkpoint))

    def test_missing_integrity_block_detected(self):
        repo = self.make_repo("missing-integrity")
        checkpoint = CORE.build_checkpoint(repo_path=repo)
        del checkpoint["integrity"]
        self.assertFalse(CORE.verify_checkpoint_integrity(checkpoint))

    def test_tampered_hash_string_detected(self):
        repo = self.make_repo("tampered-hash")
        checkpoint = CORE.build_checkpoint(repo_path=repo)
        checkpoint["integrity"]["content_sha256"] = "0" * 64
        self.assertFalse(CORE.verify_checkpoint_integrity(checkpoint))

    def test_written_file_round_trips_and_verifies(self):
        repo = self.make_repo("round-trip")
        summary, _, _ = self.checkpoint_cli(repo)
        doc = json_file(summary["path"])
        self.assertTrue(CORE.verify_checkpoint_integrity(doc))


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


class TestPermissions(CheckpointTestCase):
    def test_checkpoint_directory_and_file_permissions(self):
        repo = self.make_repo("perms")
        summary, _, _ = self.checkpoint_cli(repo)
        directory = os.path.dirname(summary["path"])
        self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(summary["path"]).st_mode), 0o600)


# ---------------------------------------------------------------------------
# Fail-closed behaviour
# ---------------------------------------------------------------------------


class TestFailClosed(CheckpointTestCase):
    def test_non_git_directory_fails_clearly(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        code, out, err = self.checkpoint_cli(plain, expect_ok=False)
        self.assertNotEqual(code, 0)
        self.assertIn("not a git repository", err)
        checkpoints_dir = os.path.join(self.home, "checkpoints")
        self.assertFalse(
            os.path.exists(checkpoints_dir) and os.listdir(checkpoints_dir),
            "a checkpoint file was written despite capture failure",
        )

    def test_repository_with_no_commits_fails_clearly(self):
        empty_repo = os.path.join(self.tmp, "empty-repo")
        os.makedirs(empty_repo)
        run_git(empty_repo, "init", "-q", "-b", "main")
        code, out, err = self.checkpoint_cli(empty_repo, expect_ok=False)
        self.assertNotEqual(code, 0)
        self.assertIn("HEAD", err)

    def test_nonexistent_path_fails_clearly(self):
        missing = os.path.join(self.tmp, "does-not-exist")
        code, out, err = self.checkpoint_cli(missing, expect_ok=False)
        self.assertNotEqual(code, 0)
        self.assertIn("does not exist", err)

    def test_failure_never_emits_a_partial_checkpoint_file(self):
        empty_repo = os.path.join(self.tmp, "empty-repo-2")
        os.makedirs(empty_repo)
        run_git(empty_repo, "init", "-q", "-b", "main")
        self.checkpoint_cli(empty_repo, expect_ok=False)
        checkpoints_dir = os.path.join(self.home, "checkpoints")
        self.assertFalse(os.path.isdir(checkpoints_dir) and os.listdir(checkpoints_dir))


if __name__ == "__main__":
    unittest.main()
