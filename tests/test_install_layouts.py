"""Install and uninstall must be correct under unusual layouts.

Terminal Handoff can be installed under different module names, in symlinked or
space-bearing directories, over a third-party status line, or on top of a
partial or interrupted earlier installation. In every case two properties must
hold:

1. it recognises its own command and never wraps itself recursively; and
2. an uninstall restores whatever was there before, byte-for-byte, including
   settings keys that have nothing to do with Terminal Handoff.
"""

import json
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import (  # noqa: E402
    PYTHON,
    TH_SCRIPT,
    THTestCase,
    json_file,
    run_th,
    text_file,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(REPO_ROOT, "src", "terminal_handoff", "templates", "successor-prompt.md")

THIRD_PARTY = 'node "$CLAUDE_PROJECT_DIR/.claude/helpers/statusline.js" --compact'


def run_module(module, args, env, stdin=""):
    """Invoke a specific installed copy of the runtime."""
    proc = subprocess.Popen(
        [PYTHON, module] + args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    out, err = proc.communicate(stdin.encode("utf-8"), timeout=90)
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


class LayoutTestCase(THTestCase):
    """Helpers for materialising the runtime at an arbitrary path."""

    def module_at(self, *parts):
        """Copy the runtime to a path and return it, template alongside."""
        destination = os.path.join(self.tmp, *parts)
        directory = os.path.dirname(destination)
        if not os.path.isdir(directory):
            os.makedirs(directory)
        shutil.copy(TH_SCRIPT, destination)
        shutil.copy(TEMPLATE, os.path.join(directory, "successor-prompt.md"))
        os.chmod(destination, 0o700)
        return destination

    def settings_at(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as handle:
            json.dump(data, handle, indent=2)
        return path

    def statusline_command(self, settings):
        return json_file(settings)["statusLine"]["command"]

    def assert_not_self_wrapped(self, settings):
        """Structural check: nothing Terminal Handoff wrapped may itself be
        Terminal Handoff, at any depth.

        Counting substrings is not sufficient - a legitimate third-party
        command may be named `statusline.js`.
        """
        import re
        import shlex

        command = self.statusline_command(settings)
        depth = 0
        current = command
        while current:
            self.assertTrue(re.search(r"terminal[-_]handoff", current, re.I),
                            "not a Terminal Handoff command: %s" % current)
            depth += 1
            self.assertLessEqual(depth, 1, "Terminal Handoff wrapped itself: %s" % command)
            parts = shlex.split(current)
            self.assertLessEqual(parts.count("--wrap"), 1, "nested --wrap: %s" % command)
            if "--wrap" not in parts:
                break
            wrapped = parts[parts.index("--wrap") + 1]
            if not re.search(r"terminal[-_]handoff", wrapped, re.I):
                break  # a genuine third-party command; correct
            current = wrapped

    def assert_wraps(self, settings, expected):
        import shlex

        parts = shlex.split(self.statusline_command(settings))
        self.assertIn("--wrap", parts)
        self.assertEqual(parts[parts.index("--wrap") + 1], expected,
                         "the wrapped command was not preserved verbatim")


# ---------------------------------------------------------------------------
# Module naming and location
# ---------------------------------------------------------------------------


class TestModuleLayouts(LayoutTestCase):
    LAYOUTS = (
        ("core.py", ("pkg", "terminal_handoff", "core.py")),
        ("terminal-handoff.py", ("install-dir", "terminal-handoff.py")),
        ("terminal_handoff.py", ("install-dir2", "terminal_handoff.py")),
        ("neutrally named directory", ("opt", "runtime", "th-runtime.py")),
    )

    def test_install_and_uninstall_under_each_module_layout(self):
        for label, parts in self.LAYOUTS:
            with self.subTest(layout=label):
                module = self.module_at(*parts)
                settings = self.settings_at("s-%s.json" % label.replace(" ", "-"),
                                            {"theme": "light"})
                before = text_file(settings)

                code, out, err = run_module(
                    module, ["install", "--settings", settings, "--skip-claude-md"], self.env())
                self.assertEqual(code, 0, err)
                self.assertIn(module, self.statusline_command(settings))
                self.assert_not_self_wrapped(settings)

                code, out, err = run_module(
                    module, ["uninstall", "--apply", "--settings", settings, "--skip-claude-md"],
                    self.env())
                self.assertEqual(code, 0, err)
                self.assertEqual(before, text_file(settings),
                                 "%s: settings were not restored byte-for-byte" % label)

    def test_repeated_install_never_self_wraps_under_any_layout(self):
        for label, parts in self.LAYOUTS:
            with self.subTest(layout=label):
                module = self.module_at(*parts)
                settings = self.settings_at("r-%s.json" % label.replace(" ", "-"), {})
                for _ in range(3):
                    run_module(module, ["install", "--settings", settings, "--skip-claude-md"],
                               self.env())
                self.assert_not_self_wrapped(settings)

    def test_installing_a_second_layout_over_a_first_does_not_wrap_it(self):
        """A renamed or relocated runtime must recognise its predecessor."""
        first = self.module_at("layout-a", "terminal-handoff.py")
        second = self.module_at("layout-b", "core.py")
        third = self.module_at("layout-c", "th-runtime.py")
        settings = self.settings_at("cross.json", {"theme": "dark"})

        run_module(first, ["install", "--settings", settings, "--skip-claude-md"], self.env())
        original = self.statusline_command(settings)
        for module in (second, third):
            code, out, err = run_module(
                module, ["install", "--settings", settings, "--skip-claude-md"], self.env())
            self.assertEqual(code, 0, err)
            self.assertIn("already_installed", out,
                          "%s did not recognise an existing installation" % module)
            self.assertEqual(self.statusline_command(settings), original,
                             "an existing Terminal Handoff command was modified")
        self.assert_not_self_wrapped(settings)

    def test_package_module_invocation_is_recognised_and_works(self):
        env = self.env()
        env["PYTHONPATH"] = os.path.join(REPO_ROOT, "src")
        proc = subprocess.Popen(
            [PYTHON, "-m", "terminal_handoff.cli", "version"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=self.tmp)
        out, err = proc.communicate(timeout=90)
        self.assertEqual(proc.returncode, 0, err.decode())
        self.assertIn("Terminal Handoff", out.decode())

        settings = self.settings_at("pkg.json", {
            "statusLine": {"type": "command",
                           "command": "python3 -m terminal_handoff.cli statusline"}})
        code, out, err = run_th(["install", "--settings", settings, "--skip-claude-md"], "", self.env())
        self.assertEqual(code, 0, err)
        self.assertIn("already_installed", out,
                      "a package-module invocation was not recognised as Terminal Handoff")

    def test_symlinked_installation_directory(self):
        real = self.module_at("real-install", "terminal-handoff.py")
        link = os.path.join(self.tmp, "linked-install")
        os.symlink(os.path.dirname(real), link)
        module = os.path.join(link, "terminal-handoff.py")
        settings = self.settings_at("symlink.json", {"theme": "light"})
        before = text_file(settings)

        code, out, err = run_module(
            module, ["install", "--settings", settings, "--skip-claude-md"], self.env())
        self.assertEqual(code, 0, err)
        self.assert_not_self_wrapped(settings)

        # The generated command must actually run through the symlinked path.
        command = self.statusline_command(settings)
        proc = subprocess.Popen(["/bin/sh", "-c", command], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env())
        out, err = proc.communicate(b"{}", timeout=90)
        self.assertEqual(proc.returncode, 0, err.decode())
        self.assertIn("TH", out.decode())

        run_module(module, ["uninstall", "--apply", "--settings", settings, "--skip-claude-md"],
                   self.env())
        self.assertEqual(before, text_file(settings))

    def test_installation_directory_containing_spaces(self):
        module = self.module_at("my install dir", "sub dir", "terminal-handoff.py")
        settings = self.settings_at("spaces.json", {"theme": "light"})
        before = text_file(settings)

        code, out, err = run_module(
            module, ["install", "--settings", settings, "--skip-claude-md"], self.env())
        self.assertEqual(code, 0, err)
        command = self.statusline_command(settings)
        self.assertIn("'%s'" % module, command, "the module path was not quoted")

        proc = subprocess.Popen(["/bin/sh", "-c", command], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env())
        out, err = proc.communicate(b"{}", timeout=90)
        self.assertEqual(proc.returncode, 0, err.decode())
        self.assertIn("TH", out.decode())

        run_module(module, ["uninstall", "--apply", "--settings", settings, "--skip-claude-md"],
                   self.env())
        self.assertEqual(before, text_file(settings))

    def test_uninstall_after_module_relocation(self):
        module = self.module_at("before-move", "terminal-handoff.py")
        settings = self.settings_at("relocate.json",
                                    {"statusLine": {"type": "command", "command": THIRD_PARTY}})
        before = text_file(settings)
        run_module(module, ["install", "--settings", settings, "--skip-claude-md"], self.env())
        self.assert_wraps(settings, THIRD_PARTY)

        moved_dir = os.path.join(self.tmp, "after-move")
        shutil.move(os.path.dirname(module), moved_dir)
        moved = os.path.join(moved_dir, "terminal-handoff.py")

        code, out, err = run_module(
            moved, ["uninstall", "--apply", "--settings", settings, "--skip-claude-md"], self.env())
        self.assertEqual(code, 0, err)
        self.assertEqual(before, text_file(settings),
                         "uninstall from a relocated module did not restore the original")


# ---------------------------------------------------------------------------
# Pre-existing and damaged configuration states
# ---------------------------------------------------------------------------


class TestConfigurationStates(LayoutTestCase):
    def test_existing_third_party_status_line_is_wrapped_then_restored(self):
        settings = self.settings_at("third-party.json", {
            "theme": "dark",
            "statusLine": {"type": "command", "command": THIRD_PARTY},
            "permissions": {"allow": ["Bash(git status)"]},
        })
        before = text_file(settings)
        run_th(["install", "--settings", settings, "--skip-claude-md"], "", self.env())
        self.assert_wraps(settings, THIRD_PARTY)
        self.assertEqual(json_file(settings)["permissions"], {"allow": ["Bash(git status)"]})

        run_th(["uninstall", "--apply", "--settings", settings, "--skip-claude-md"], "", self.env())
        self.assertEqual(before, text_file(settings))

    def test_settings_file_without_statusline(self):
        settings = self.settings_at("no-sl.json", {"theme": "light", "model": "opus"})
        before = text_file(settings)
        code, out, err = run_th(["install", "--settings", settings, "--skip-claude-md"], "", self.env())
        self.assertEqual(code, 0, err)
        data = json_file(settings)
        self.assertIn("statusLine", data)
        self.assertEqual(data["model"], "opus")
        run_th(["uninstall", "--apply", "--settings", settings, "--skip-claude-md"], "", self.env())
        self.assertEqual(before, text_file(settings))

    def test_new_statusline_uses_a_responsive_refresh_interval(self):
        settings = self.settings_at("fresh-refresh.json", {"theme": "light"})
        code, out, err = run_th(
            ["install", "--settings", settings, "--skip-claude-md"], "", self.env()
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(json_file(settings)["statusLine"]["refreshInterval"], 5)

    def test_upgrade_adds_refresh_interval_without_rewrapping(self):
        legacy = "%s statusline --marker terminal-handoff" % TH_SCRIPT
        settings = self.settings_at(
            "upgrade-refresh.json",
            {"theme": "dark", "statusLine": {"type": "command", "command": legacy}},
        )
        code, out, err = run_th(
            ["install", "--settings", settings, "--skip-claude-md"], "", self.env()
        )
        self.assertEqual(code, 0, err)
        statusline = json_file(settings)["statusLine"]
        self.assertEqual(statusline["command"], legacy)
        self.assertEqual(statusline["refreshInterval"], 5)
        self.assertNotIn("--wrap", statusline["command"])

    def test_existing_statusline_options_are_all_preserved(self):
        original_statusline = {
            "type": "command",
            "command": THIRD_PARTY,
            "padding": 2,
            "refreshInterval": 11,
            "futureClaudeOption": {"compact": True},
        }
        settings = self.settings_at(
            "preserve-options.json", {"theme": "dark", "statusLine": original_statusline}
        )
        before = text_file(settings)
        code, out, err = run_th(
            ["install", "--settings", settings, "--skip-claude-md"], "", self.env()
        )
        self.assertEqual(code, 0, err)
        installed = json_file(settings)["statusLine"]
        self.assertEqual(installed["padding"], 2)
        self.assertEqual(installed["refreshInterval"], 11)
        self.assertEqual(installed["futureClaudeOption"], {"compact": True})
        run_th(
            ["uninstall", "--apply", "--settings", settings, "--skip-claude-md"],
            "",
            self.env(),
        )
        self.assertEqual(text_file(settings), before)

    def test_malformed_settings_json_is_refused_and_left_untouched(self):
        path = os.path.join(self.tmp, "broken.json")
        broken = '{ "theme": "light", oops '
        with open(path, "w") as handle:
            handle.write(broken)
        code, out, err = run_th(["install", "--settings", path, "--skip-claude-md"], "", self.env())
        self.assertNotEqual(code, 0)
        self.assertIn("not valid JSON", out)
        self.assertEqual(text_file(path), broken, "a malformed settings file was rewritten")

    def test_interrupted_installation_registry_lost_still_restores_the_wrapped_command(self):
        """The registry can be lost; the installed command still names what it wrapped."""
        settings = self.settings_at("interrupted.json", {
            "theme": "light", "statusLine": {"type": "command", "command": THIRD_PARTY}})
        run_th(["install", "--settings", settings, "--skip-claude-md"], "", self.env())
        self.assert_wraps(settings, THIRD_PARTY)

        os.unlink(os.path.join(self.home, "state", "installed.json"))

        code, out, err = run_th(["uninstall", "--settings", settings, "--skip-claude-md"], "", self.env())
        self.assertEqual(code, 0, err)
        self.assertIn("recovered", out, "the dry run did not report recovery")

        code, out, err = run_th(
            ["uninstall", "--apply", "--settings", settings, "--skip-claude-md"], "", self.env())
        self.assertEqual(code, 0, err)
        restored = json_file(settings)["statusLine"]
        self.assertEqual(restored["command"], THIRD_PARTY,
                         "a third-party status line was dropped instead of restored")

    def test_interrupted_installation_with_no_wrapped_command_removes_the_key(self):
        settings = self.settings_at("interrupted2.json", {"theme": "light"})
        before = text_file(settings)
        run_th(["install", "--settings", settings, "--skip-claude-md"], "", self.env())
        os.unlink(os.path.join(self.home, "state", "installed.json"))
        run_th(["uninstall", "--apply", "--settings", settings, "--skip-claude-md"], "", self.env())
        self.assertNotIn("statusLine", json_file(settings))
        self.assertEqual(before, text_file(settings))

    def test_partial_installation_registry_entry_without_statusline(self):
        settings = self.settings_at("partial.json", {"theme": "light"})
        run_th(["install", "--settings", settings, "--skip-claude-md"], "", self.env())
        data = json_file(settings)
        del data["statusLine"]
        with open(settings, "w") as handle:
            json.dump(data, handle, indent=2)

        code, out, err = run_th(["install", "--settings", settings, "--skip-claude-md"], "", self.env())
        self.assertEqual(code, 0, err)
        self.assertIn("statusLine", json_file(settings))
        self.assert_not_self_wrapped(settings)

    def test_missing_backup_does_not_prevent_restoration(self):
        settings = self.settings_at("missing-backup.json", {
            "theme": "light", "statusLine": {"type": "command", "command": THIRD_PARTY}})
        before = text_file(settings)
        run_th(["install", "--settings", settings, "--skip-claude-md"], "", self.env())

        backups = os.path.join(self.home, "backups")
        for name in os.listdir(backups):
            os.unlink(os.path.join(backups, name))

        code, out, err = run_th(
            ["uninstall", "--apply", "--settings", settings, "--skip-claude-md"], "", self.env())
        self.assertEqual(code, 0, err)
        self.assertEqual(before, text_file(settings),
                         "restoration failed when the backup file was absent")

    def test_multiple_backups_do_not_confuse_restoration(self):
        settings = self.settings_at("multi-backup.json", {
            "theme": "light", "statusLine": {"type": "command", "command": THIRD_PARTY}})
        before = text_file(settings)
        for _ in range(3):
            run_th(["install", "--settings", settings, "--skip-claude-md"], "", self.env())
            run_th(["uninstall", "--apply", "--settings", settings, "--skip-claude-md"], "",
                   self.env())
        backups = [n for n in os.listdir(os.path.join(self.home, "backups")) if n.endswith(".bak")]
        self.assertGreater(len(backups), 1, "expected several backups")
        self.assertEqual(before, text_file(settings))

    def test_older_layout_command_is_recognised_not_wrapped(self):
        """A command written by an earlier version, without the self marker."""
        for legacy in (
            "/usr/bin/python3 /opt/terminal-handoff/terminal-handoff.py statusline",
            "/usr/bin/python3 /opt/terminal-handoff/terminal-handoff.py statusline --wrap 'node x.js'",
            "python3 $HOME/.claude/terminal-handoff/terminal-handoff.py statusline",
        ):
            with self.subTest(legacy=legacy):
                settings = self.settings_at("legacy-%d.json" % abs(hash(legacy)), {
                    "statusLine": {"type": "command", "command": legacy}})
                code, out, err = run_th(
                    ["install", "--settings", settings, "--skip-claude-md"], "", self.env())
                self.assertEqual(code, 0, err)
                self.assertIn("already_installed", out)
                self.assertEqual(self.statusline_command(settings), legacy,
                                 "a legacy Terminal Handoff command was modified")

    def test_install_reinstall_uninstall_round_trip_preserves_unrelated_settings(self):
        payload = {
            "theme": "light",
            "statusLine": {"type": "command", "command": THIRD_PARTY},
            "permissions": {"allow": ["Bash(git status)", "Read"], "deny": []},
            "env": {"FOO": "bar"},
            "attribution": {"pr": "\U0001f916 generated"},
            "enabledPlugins": {"a@b": True},
        }
        settings = self.settings_at("round-trip.json", payload)
        before = text_file(settings)

        for _ in range(2):
            run_th(["install", "--settings", settings, "--skip-claude-md"], "", self.env())
        self.assert_wraps(settings, THIRD_PARTY)
        self.assert_not_self_wrapped(settings)

        mid = json_file(settings)
        for key in ("theme", "permissions", "env", "attribution", "enabledPlugins"):
            self.assertEqual(mid[key], payload[key], "%s was altered during install" % key)

        run_th(["uninstall", "--apply", "--settings", settings, "--skip-claude-md"], "", self.env())
        self.assertEqual(before, text_file(settings),
                         "the round trip did not restore the file byte-for-byte")

    def test_uninstall_is_idempotent(self):
        settings = self.settings_at("idem-uninstall.json", {"theme": "light"})
        before = text_file(settings)
        run_th(["install", "--settings", settings, "--skip-claude-md"], "", self.env())
        for _ in range(3):
            code, out, err = run_th(
                ["uninstall", "--apply", "--settings", settings, "--skip-claude-md"], "", self.env())
            self.assertEqual(code, 0, err)
        self.assertEqual(before, text_file(settings))

    def test_uninstall_leaves_a_foreign_status_line_alone(self):
        settings = self.settings_at("foreign.json", {
            "statusLine": {"type": "command", "command": THIRD_PARTY}})
        before = text_file(settings)
        code, out, err = run_th(
            ["uninstall", "--apply", "--settings", settings, "--skip-claude-md"], "", self.env())
        self.assertEqual(code, 0, err)
        self.assertEqual(before, text_file(settings),
                         "uninstall modified a status line it did not install")
        self.assertIn("not installed here", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
