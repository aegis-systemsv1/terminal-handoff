"""Configuration integrity, install/uninstall restoration and coverage reporting.

Ported unchanged from the proven Terminal Handoff 1.0.0 suite.
"""
import json
import os
import re
import shutil
import stat
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import (  # noqa: E402
    THTestCase,
    json_file,
    run_th,
    text_file,
)


class TestInstallUninstall(THTestCase):
    def write_settings(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as handle:
            json.dump(data, handle, indent=2)
        return path

    def test_install_creates_statusline_when_absent(self):
        settings = self.write_settings("settings.json", {"theme": "light"})
        code, out, err = run_th(
            ["install", "--settings", settings, "--skip-claude-md"], "", self.env()
        )
        self.assertEqual(code, 0, err)
        data = json_file(settings)
        self.assertEqual(data["theme"], "light", "unrelated settings were altered")
        self.assertTrue(re.search(r"terminal[-_]handoff", data["statusLine"]["command"]))
        self.assertIn("statusline", data["statusLine"]["command"])
        self.assertEqual(data["statusLine"]["type"], "command")

    def test_install_wraps_existing_statusline(self):
        original = {"type": "command", "command": 'node "$CLAUDE_PROJECT_DIR/.claude/helpers/statusline.cjs"'}
        settings = self.write_settings("project-settings.json", {"enabledPlugins": {"x": True}, "statusLine": original})
        code, out, err = run_th(
            ["install", "--settings", settings, "--skip-claude-md"], "", self.env()
        )
        self.assertEqual(code, 0, err)
        data = json_file(settings)
        command = data["statusLine"]["command"]
        self.assertTrue(re.search(r"terminal[-_]handoff", command))
        self.assertIn("--wrap", command)
        self.assertIn("statusline.cjs", command)
        self.assertEqual(data["enabledPlugins"], {"x": True}, "unrelated settings were altered")
        registry = json_file(os.path.join(self.home, "state", "installed.json"))
        self.assertEqual(registry["targets"][settings]["original_statusLine"], original)
        self.assertTrue(os.path.exists(registry["targets"][settings]["backup"]))

    def test_install_is_idempotent(self):
        settings = self.write_settings("idem.json", {"theme": "dark"})
        run_th(["install", "--settings", settings, "--skip-claude-md"], "", self.env())
        first = text_file(settings)
        code, out, err = run_th(["install", "--settings", settings, "--skip-claude-md"], "", self.env())
        self.assertEqual(code, 0)
        self.assertEqual(first, text_file(settings), "second install changed the file")
        self.assertIn("already_installed", out)

    def test_60_uninstall_restores_backed_up_configuration(self):
        settings = self.write_settings("restore.json", {"theme": "light"})
        before = text_file(settings)
        run_th(["install", "--settings", settings, "--skip-claude-md"], "", self.env())
        self.assertIn("statusLine", json_file(settings))
        code, out, err = run_th(["uninstall", "--settings", settings, "--skip-claude-md"], "", self.env())
        self.assertEqual(code, 0)
        self.assertIn("dry_run", out)
        self.assertIn("statusLine", json_file(settings), "dry run modified the file")
        code, out, err = run_th(
            ["uninstall", "--settings", settings, "--apply", "--skip-claude-md"], "", self.env()
        )
        self.assertEqual(code, 0, err)
        self.assertNotIn("statusLine", json_file(settings))
        self.assertEqual(json.loads(before), json.loads(text_file(settings)))

    def test_66_uninstall_restores_original_project_statusline(self):
        original = {"type": "command", "command": 'node "$CLAUDE_PROJECT_DIR/.claude/helpers/statusline.cjs"'}
        payload = {"enabledPlugins": {"a": True}, "statusLine": original}
        settings = self.write_settings("project-with-statusline.json", payload)
        before = text_file(settings)
        run_th(["install", "--settings", settings, "--skip-claude-md"], "", self.env())
        self.assertIn("--wrap", json_file(settings)["statusLine"]["command"])
        code, out, err = run_th(
            ["uninstall", "--settings", settings, "--apply", "--skip-claude-md"], "", self.env()
        )
        self.assertEqual(code, 0, err)
        restored = json_file(settings)
        self.assertEqual(restored["statusLine"], original, "project status line was not restored")
        self.assertEqual(json.loads(before), restored)

    def test_72_uninstall_dry_run_changes_nothing(self):
        settings = self.write_settings("dry.json", {"theme": "light"})
        run_th(["install", "--settings", settings, "--skip-claude-md"], "", self.env())
        snapshot = text_file(settings)
        code, out, err = run_th(["uninstall", "--skip-claude-md"], "", self.env())
        self.assertEqual(code, 0)
        self.assertEqual(snapshot, text_file(settings))
        report = json.loads(out)
        self.assertTrue(report["dry_run"])
        self.assertIn("retained", report)

    def test_install_refuses_invalid_json_settings(self):
        path = os.path.join(self.tmp, "broken.json")
        with open(path, "w") as handle:
            handle.write("{ this is not json")
        code, out, err = run_th(["install", "--settings", path, "--skip-claude-md"], "", self.env())
        self.assertNotEqual(code, 0)
        self.assertIn("not valid JSON", out)
        self.assertEqual(text_file(path), "{ this is not json", "broken file was overwritten")

    def test_install_refuses_non_command_statusline(self):
        settings = self.write_settings("weird.json", {"statusLine": {"type": "static", "text": "hi"}})
        before = text_file(settings)
        code, out, err = run_th(["install", "--settings", settings, "--skip-claude-md"], "", self.env())
        self.assertNotEqual(code, 0)
        self.assertEqual(before, text_file(settings))

    def test_claude_md_section_is_added_and_removed_idempotently(self):
        md = os.path.join(self.tmp, "CLAUDE.md")
        original = "# gstack\n\nExisting user content that must survive.\n"
        with open(md, "w") as handle:
            handle.write(original)
        env = self.env()
        env["HOME"] = self.tmp
        os.makedirs(os.path.join(self.tmp, ".claude"), exist_ok=True)
        shutil.copy(md, os.path.join(self.tmp, ".claude", "CLAUDE.md"))
        target = os.path.join(self.tmp, ".claude", "CLAUDE.md")
        settings = self.write_settings("s.json", {})
        run_th(["install", "--settings", settings], "", env)
        content = text_file(target)
        self.assertIn("Existing user content that must survive.", content)
        self.assertIn("BEGIN TERMINAL HANDOFF", content)
        run_th(["install", "--settings", settings], "", env)
        self.assertEqual(content, text_file(target), "second install duplicated the section")
        run_th(["uninstall", "--apply", "--settings", settings], "", env)
        after = text_file(target)
        self.assertNotIn("TERMINAL HANDOFF", after)
        self.assertIn("Existing user content that must survive.", after)

    def test_install_upgrades_only_the_managed_claude_md_block(self):
        env = self.env()
        env["HOME"] = self.tmp
        directory = os.path.join(self.tmp, ".claude")
        os.makedirs(directory, exist_ok=True)
        target = os.path.join(directory, "CLAUDE.md")
        original = (
            "# User instructions\n\nKeep this exactly.\n\n"
            "<!-- BEGIN TERMINAL HANDOFF -->\n"
            "# Terminal Handoff\n\nOld managed instructions.\n"
            "<!-- END TERMINAL HANDOFF -->\n"
        )
        with open(target, "w") as handle:
            handle.write(original)
        settings = self.write_settings("coordination-upgrade.json", {})

        code, output, error = run_th(["install", "--settings", settings], "", env)
        self.assertEqual(code, 0, error)
        result = json.loads(output)
        self.assertTrue(result["claude_md"]["updated"])
        content = text_file(target)
        self.assertIn("Keep this exactly.", content)
        self.assertNotIn("Old managed instructions.", content)
        self.assertIn("ListAgents", content)
        self.assertIn("SendMessage", content)
        self.assertIn("never user consent", content.lower())

    def test_install_refuses_a_malformed_managed_claude_md_block(self):
        env = self.env()
        env["HOME"] = self.tmp
        directory = os.path.join(self.tmp, ".claude")
        os.makedirs(directory, exist_ok=True)
        target = os.path.join(directory, "CLAUDE.md")
        original = "# User content\n\n<!-- BEGIN TERMINAL HANDOFF -->\nbroken\n"
        with open(target, "w") as handle:
            handle.write(original)
        settings = self.write_settings("malformed-coordination-block.json", {})

        code, output, _ = run_th(["install", "--settings", settings], "", env)
        self.assertNotEqual(code, 0)
        self.assertIn("malformed", output)
        self.assertEqual(text_file(target), original)


# ---------------------------------------------------------------------------
# 11. Coverage reporting
# ---------------------------------------------------------------------------


class TestCoverage(THTestCase):
    def test_coverage_reports_uncovered_overrides(self):
        code, out, err = run_th(["coverage", "--json"], "", self.env())
        report = json.loads(out)
        self.assertIn("rows", report)
        self.assertIn("full_coverage", report)
        for row in report["rows"]:
            self.assertIn("terminal_handoff", row)


class TestSecurityBoundary(unittest.TestCase):
    """Direct unit tests for the validation and quoting boundary."""

    def setUp(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
        import terminal_handoff.security as security

        self.security = security

    def test_model_id_allowlist_accepts_real_ids_including_brackets(self):
        for model in ("claude-opus-5[1m]", "claude-sonnet-5", "claude-haiku-4-5-20251001",
                      "claude-fable-5[1m]", "us.anthropic.claude-v2"):
            self.assertTrue(self.security.MODEL_ID_RE.match(model), model)

    def test_model_id_allowlist_rejects_shell_metacharacters(self):
        for model in ('a"; rm -rf /', "a$(id)", "a`id`", "a b", "a\nb", "a;b", "a|b", "a&b",
                      "a>b", "a<b", "a'b", "a\\b", "a*b", "a?b", "a!b", ""):
            self.assertIsNone(self.security.MODEL_ID_RE.match(model), repr(model))

    def test_effort_allowlist_is_exactly_the_documented_five(self):
        self.assertEqual(self.security.ALLOWED_EFFORT_LEVELS,
                         ("low", "medium", "high", "xhigh", "max"))
        self.assertNotIn("ultracode", self.security.ALLOWED_EFFORT_LEVELS)
        self.assertIn("ultracode", self.security.UNDETECTABLE_EFFORT_ALIASES)

    def test_unsafe_path_pattern_rejects_quotes_backslashes_and_controls(self):
        for bad in ('/tmp/a"b', "/tmp/a\\b", "/tmp/a\nb", "/tmp/a\x00b", "/tmp/a\x7fb"):
            self.assertTrue(self.security.UNSAFE_PATH_RE.search(bad), repr(bad))
        for good in ("/tmp/a b", "/tmp/a-b_c.d", "/tmp/Ünïcödé", "/tmp/a(b)c"):
            self.assertIsNone(self.security.UNSAFE_PATH_RE.search(good), repr(good))

    def test_applescript_quote_escapes_quotes_and_backslashes(self):
        self.assertEqual(self.security.applescript_quote('a"b'), '"a\\"b"')
        self.assertEqual(self.security.applescript_quote("a\\b"), '"a\\\\b"')
        self.assertEqual(self.security.applescript_quote("plain"), '"plain"')

    def test_forbidden_flags_are_detected_in_an_argv(self):
        for flag in ("--continue", "--resume", "--fork-session",
                     "--dangerously-skip-permissions", "--fallback-model"):
            argv = ["/bin/claude", "--model", "claude-opus-5", flag, "prompt"]
            self.assertTrue(self.security.assert_launch_argv_safe(argv), flag)
        clean = ["/bin/claude", "--model", "claude-opus-5", "--effort", "high", "prompt"]
        self.assertEqual(self.security.assert_launch_argv_safe(clean), [])

    def test_missing_model_flag_is_reported(self):
        self.assertIn("missing --model",
                      self.security.assert_launch_argv_safe(["/bin/claude", "prompt"]))

    def test_write_private_creates_owner_only_files(self):
        import tempfile

        directory = tempfile.mkdtemp(prefix="th-sec-")
        try:
            path = os.path.join(directory, "secret.json")
            self.security.write_json_private(path, {"a": 1})
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_transcript_validation_rejects_unsafe_inputs(self):
        import tempfile

        directory = tempfile.mkdtemp(prefix="th-sec-")
        try:
            ok, errors, _ = self.security.validate_transcript("relative.jsonl", None)
            self.assertFalse(ok)
            ok, errors, _ = self.security.validate_transcript(
                os.path.join(directory, "missing.jsonl"), None)
            self.assertFalse(ok)
            empty = os.path.join(directory, "empty.jsonl")
            open(empty, "w").close()
            ok, errors, _ = self.security.validate_transcript(empty, None)
            self.assertFalse(ok)
            bad = os.path.join(directory, "bad.jsonl")
            with open(bad, "w") as handle:
                handle.write("not json\n")
            ok, errors, _ = self.security.validate_transcript(bad, None)
            self.assertFalse(ok)
            good = os.path.join(directory, "good.jsonl")
            with open(good, "w") as handle:
                handle.write(json.dumps({"type": "user"}) + "\n")
            ok, errors, _ = self.security.validate_transcript(good, None)
            self.assertTrue(ok, errors)
        finally:
            shutil.rmtree(directory, ignore_errors=True)


class TestRepositoryPrivacy(unittest.TestCase):
    """The published source tree must carry no personal or machine-specific data."""

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # Assembled from fragments so this file contains no literal match of its own
    # denylist; the scan below must find zero hits across the whole tree.
    # Build and cache directories are gitignored and never published; their
    # contents legitimately embed absolute local paths.
    SKIP_DIRS = frozenset((
        ".git", "__pycache__", ".pytest_cache", "node_modules", ".ruff_cache",
        ".mypy_cache", ".venv", "venv", "build", "dist", ".eggs", ".idea", ".vscode",
    ))

    FORBIDDEN = (
        "/" + "Users/",
        "/" + "home/",
        "sk-" + "ant-",
        "gh" + "p_",
        "github" + "_pat_",
        "BEGIN RSA PRIVATE" + " KEY",
        "BEGIN OPENSSH PRIVATE" + " KEY",
        "xo" + "xb-",
        "AKI" + "A",
    )

    def iter_files(self):
        for dirpath, dirnames, filenames in os.walk(self.ROOT):
            dirnames[:] = [d for d in dirnames if d not in self.SKIP_DIRS
                           and not d.endswith(".egg-info")]
            for name in filenames:
                if name.endswith((".pyc", ".png", ".jpg", ".gif")):
                    continue
                yield os.path.join(dirpath, name)

    def test_no_forbidden_strings_anywhere_in_the_tree(self):
        offences = []
        for path in self.iter_files():
            try:
                with open(path, "r", errors="ignore") as handle:
                    body = handle.read()
            except (IOError, OSError):
                continue
            for needle in self.FORBIDDEN:
                if needle in body:
                    offences.append("%s contains %r" % (os.path.relpath(path, self.ROOT), needle))
        self.assertEqual(offences, [], "\n".join(offences))



class TestSelfWrapRegression(THTestCase):
    """Regression: the installer must never wrap its own command.

    Detection used to key on the literal filename `terminal-handoff.py`. Under
    any other module name the installer failed to recognise itself and wrapped
    its own status-line command recursively, and the uninstaller could no longer
    find the installation to restore.
    """

    def write_settings(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as handle:
            json.dump(data, handle, indent=2)
        return path

    def test_repeated_installs_never_nest_the_command(self):
        settings = self.write_settings("nest.json", {"theme": "light"})
        for _ in range(3):
            run_th(["install", "--settings", settings, "--skip-claude-md"], "", self.env())
        command = json_file(settings)["statusLine"]["command"]
        self.assertEqual(command.count("--wrap"), 0, "the installer wrapped itself: %s" % command)
        self.assertEqual(command.count("statusline"), 1)

    def test_installer_recognises_itself_under_any_module_name(self):
        for command in (
            "/usr/bin/python3 /opt/terminal-handoff/terminal-handoff.py statusline",
            "/usr/bin/python3 /opt/th/src/terminal_handoff/core.py statusline",
            "python3 -m terminal_handoff.cli statusline",
        ):
            settings = self.write_settings("m-%d.json" % abs(hash(command)), {
                "statusLine": {"type": "command", "command": command}})
            code, out, err = run_th(["install", "--settings", settings, "--skip-claude-md"], "", self.env())
            self.assertEqual(code, 0, err)
            self.assertIn("already_installed", out, "did not recognise itself in: %s" % command)
            self.assertEqual(json_file(settings)["statusLine"]["command"], command,
                             "an already-installed command was modified")

    def test_a_genuine_third_party_statusline_is_still_wrapped(self):
        original = "node /opt/vendor/statusline.js --compact"
        settings = self.write_settings("third-party.json", {
            "statusLine": {"type": "command", "command": original}})
        run_th(["install", "--settings", settings, "--skip-claude-md"], "", self.env())
        command = json_file(settings)["statusLine"]["command"]
        self.assertIn("--wrap", command)
        self.assertIn(original, command)


if __name__ == "__main__":
    unittest.main(verbosity=2)
