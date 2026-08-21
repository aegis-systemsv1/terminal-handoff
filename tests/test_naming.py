"""Successor session naming.

A Claude session named `Ranger` must hand off to `Ranger 2`, then `Ranger 3`.
The base name is captured once from the official status-line JSON, preserved as
explicit chain metadata, and never re-derived by parsing trailing digits off a
visible session name. The machine-safe chain identifier is never shown.
"""

import os
import re
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import (  # noqa: E402
    CORE,
    THTestCase,
    json_file,
    text_file,
)

INTERNAL_NAME_RE = re.compile(r"terminal[-_]handoff-[0-9a-f]{6,}-g\d+", re.IGNORECASE)


class TestDisplayNameRules(THTestCase):
    """Pure naming rules, proved directly."""

    def test_generation_one_keeps_its_name(self):
        self.assertEqual(CORE.generation_display_name("Ranger", 1), "Ranger")

    def test_sequential_generations(self):
        self.assertEqual(CORE.generation_display_name("Ranger", 2), "Ranger 2")
        self.assertEqual(CORE.generation_display_name("Ranger", 3), "Ranger 3")
        self.assertEqual(CORE.generation_display_name("Ranger", 4), "Ranger 4")

    def test_multi_word_names(self):
        self.assertEqual(CORE.generation_display_name("Nova Drone", 2), "Nova Drone 2")
        self.assertEqual(CORE.generation_display_name("Nova Drone", 3), "Nova Drone 3")

    def test_names_containing_legitimate_numbers_are_preserved(self):
        """Digits inside a name are content, not a generation number."""
        for base, expected in (
            ("Project 42", "Project 42 2"),
            ("Nova 2 Drone", "Nova 2 Drone 2"),
            ("v1.2.3 upgrade", "v1.2.3 upgrade 2"),
            ("Ranger 2", "Ranger 2 2"),
        ):
            with self.subTest(base=base):
                self.assertEqual(CORE.generation_display_name(base, 2), expected)

    def test_sanitising_removes_control_characters_only(self):
        self.assertEqual(CORE.sanitize_display_name("Ran\nger\tX"), "Ran ger X")
        self.assertEqual(CORE.sanitize_display_name("  Ranger  "), "Ranger")
        self.assertEqual(CORE.sanitize_display_name("Ranger\x00"), "Ranger")
        # Unicode is content and is preserved.
        self.assertEqual(CORE.sanitize_display_name(u"Ранд"), u"Ранд")

    def test_a_name_can_never_look_like_a_flag(self):
        self.assertEqual(CORE.sanitize_display_name("--dangerously-skip-permissions"),
                         "dangerously-skip-permissions")
        self.assertEqual(CORE.sanitize_display_name("-rf"), "rf")

    def test_missing_or_malformed_base_names_use_the_documented_fallback(self):
        for raw in (None, "", "   ", 42, "\n\t", "-----"):
            with self.subTest(raw=raw):
                self.assertIsNone(CORE.sanitize_display_name(raw))
        base, source = CORE.resolve_base_display_name("abcdef012345", 1, None)
        self.assertEqual(source, "fallback")
        self.assertEqual(base, "Terminal Handoff abcdef01")
        self.assertEqual(CORE.generation_display_name(base, 2), "Terminal Handoff abcdef01 2")

    def test_long_names_are_bounded(self):
        base = CORE.sanitize_display_name("R" * 500)
        self.assertEqual(len(base), CORE.DISPLAY_NAME_MAX)
        self.assertTrue(CORE.generation_display_name(base, 2).endswith(" 2"))

    def test_internal_chain_id_is_separate_from_the_display_name(self):
        self.assertEqual(CORE.successor_session_name("Ranger", 2), "Ranger 2")
        self.assertNotIn("g2", CORE.successor_session_name("Ranger", 2))
        self.assertIsNone(INTERNAL_NAME_RE.search(CORE.successor_session_name("Ranger", 2)))


class TestNamingThroughTheLauncher(THTestCase):
    """The name that actually reaches `claude --name` and the window title."""

    def _launch(self, payload, env=None):
        ok, _ = self.trigger_and_wait(payload, env)
        self.assertTrue(ok, "no launch record produced")
        return json_file(self.launch_record(payload["_session_id"]))

    def _name_in(self, record):
        argv = record["argv"]
        return argv[argv.index("--name") + 1]

    def test_ranger_produces_ranger_2(self):
        record = self._launch(self.payload(percent=90.0, session_name="Ranger"))
        self.assertEqual(self._name_in(record), "Ranger 2")
        self.assertEqual(record["title"], "Ranger 2")
        self.assertEqual(record["base_display_name"], "Ranger")
        self.assertEqual(record["base_name_source"], "session_name")

    def test_nova_drone_produces_nova_drone_2(self):
        record = self._launch(self.payload(percent=90.0, session_name="Nova Drone"))
        self.assertEqual(self._name_in(record), "Nova Drone 2")

    def test_generation_one_is_not_renamed(self):
        """The original session keeps its name; no `1` is ever appended."""
        record = self._launch(self.payload(percent=90.0, session_name="Ranger"))
        manifest = json_file(self.manifest(record["session_id"]))
        self.assertEqual(manifest["display"]["outgoing_display_name"], "Ranger")
        self.assertEqual(manifest["display"]["successor_display_name"], "Ranger 2")

    def test_no_internal_name_is_ever_exposed(self):
        record = self._launch(self.payload(percent=90.0, session_name="Ranger"))
        name = self._name_in(record)
        self.assertIsNone(INTERNAL_NAME_RE.search(name), "internal chain name exposed: %r" % name)
        manifest = json_file(self.manifest(record["session_id"]))
        self.assertNotIn(manifest["chain_id"], name)
        self.assertNotIn("-g2", name)

    def test_missing_session_name_uses_the_documented_fallback(self):
        record = self._launch(self.payload(percent=90.0))
        manifest = json_file(self.manifest(record["session_id"]))
        expected = "Terminal Handoff %s" % manifest["chain_id"][:8]
        self.assertEqual(record["base_display_name"], expected)
        self.assertEqual(record["base_name_source"], "fallback")
        self.assertEqual(self._name_in(record), expected + " 2")


class TestChainNamingAcrossGenerations(THTestCase):
    """`Ranger` -> `Ranger 2` -> `Ranger 3`, driven by trusted chain state."""

    def _handoff(self, payload, env=None):
        ok, _ = self.trigger_and_wait(payload, env)
        self.assertTrue(ok)
        return json_file(self.launch_record(payload["_session_id"]))

    def _successor_env(self, chain_id, generation, parent_session, base_name=None, **extra):
        values = dict(
            CLAUDE_TERMINAL_HANDOFF_CHAIN_ID=chain_id,
            CLAUDE_TERMINAL_HANDOFF_GENERATION=str(generation),
            CLAUDE_TERMINAL_HANDOFF_PARENT_SESSION=parent_session,
            CLAUDE_TERMINAL_HANDOFF_MANIFEST=self.manifest(parent_session),
            CLAUDE_TERMINAL_HANDOFF_COOLDOWN="0",
        )
        if base_name is not None:
            values["CLAUDE_TERMINAL_HANDOFF_BASE_NAME"] = base_name
        values.update(extra)
        return self.env(**values)

    def test_three_generations_are_numbered_sequentially(self):
        first = self.payload(percent=90.0, session_name="Ranger")
        gen1 = self._handoff(first)
        chain = gen1["chain_id"]
        self.assertEqual(gen1["argv"][gen1["argv"].index("--name") + 1], "Ranger 2")

        second = self.payload(percent=90.0, session_name="Ranger 2")
        gen2 = self._handoff(second, self._successor_env(chain, 2, first["_session_id"], "Ranger"))
        self.assertEqual(gen2["chain_id"], chain)
        self.assertEqual(gen2["argv"][gen2["argv"].index("--name") + 1], "Ranger 3")
        self.assertEqual(gen2["base_name_source"], "chain_state")

        third = self.payload(percent=90.0, session_name="Ranger 3")
        gen3 = self._handoff(third, self._successor_env(chain, 3, second["_session_id"], "Ranger"))
        self.assertEqual(gen3["argv"][gen3["argv"].index("--name") + 1], "Ranger 4")

    def test_base_name_comes_from_chain_state_not_the_visible_name(self):
        """A renamed successor does not rewrite the chain's base name."""
        first = self.payload(percent=90.0, session_name="Ranger")
        gen1 = self._handoff(first)
        chain = gen1["chain_id"]
        renamed = self.payload(percent=90.0, session_name="Something Else Entirely 9")
        gen2 = self._handoff(renamed, self._successor_env(chain, 2, first["_session_id"], "Ranger"))
        self.assertEqual(gen2["argv"][gen2["argv"].index("--name") + 1], "Ranger 3")

    def test_generation_derives_from_trusted_chain_metadata(self):
        """Chain state wins over the environment's generation number."""
        first = self.payload(percent=90.0, session_name="Ranger")
        gen1 = self._handoff(first)
        chain = gen1["chain_id"]
        successor = self.payload(percent=90.0, session_name="Ranger 5")
        os.environ["CLAUDE_TERMINAL_HANDOFF_HOME"] = self.home
        CORE.record_chain_generation(
            chain, 5, session_id=successor["_session_id"], display_name="Ranger 5"
        )
        # The environment claims generation 2; trusted chain state says 5.
        record = self._handoff(successor, self._successor_env(chain, 2, first["_session_id"], "Ranger"))
        self.assertEqual(record["generation"], 5)
        self.assertEqual(record["argv"][record["argv"].index("--name") + 1], "Ranger 6")

    def test_environment_base_name_is_used_only_when_chain_state_is_absent(self):
        payload = self.payload(percent=90.0, session_name="Whatever")
        record = self._handoff(
            payload, self._successor_env("abcdef012345", 2, "no-such-parent-session", "Ranger")
        )
        self.assertEqual(record["base_name_source"], "environment")
        self.assertEqual(record["argv"][record["argv"].index("--name") + 1], "Ranger 3")


class TestNamingCannotInject(THTestCase):
    """A session name is untrusted text and can never become a command."""

    def _launch(self, name):
        payload = self.payload(percent=90.0, session_name=name)
        ok, _ = self.trigger_and_wait(payload)
        self.assertTrue(ok, "no launch record produced for %r" % name)
        return json_file(self.launch_record(payload["_session_id"]))

    def test_shell_metacharacters_are_carried_as_one_argv_element(self):
        canary = os.path.join(self.tmp, "naming-canary")
        hostile = '"; touch %s; echo "' % canary
        record = self._launch(hostile)
        argv = record["argv"]
        name = argv[argv.index("--name") + 1]
        self.assertIn("touch", name, "the hostile text should survive as literal text")
        self.assertFalse(os.path.exists(canary), "shell injection executed")

        # And it must survive the generated launch script as one argument.
        argv_out = os.path.join(self.tmp, "argv.txt")
        with open(self.fake_claude, "w") as handle:
            handle.write('#!/bin/sh\nfor a in "$@"; do echo "$a"; done > %s\n' % argv_out)
        os.chmod(self.fake_claude, 0o700)
        probe = os.path.join(self.tmp, "probe.sh")
        with open(probe, "w") as handle:
            handle.write("#!/bin/zsh\n" + text_file(record["script_file"]).split("\n", 1)[1])
        os.chmod(probe, 0o700)
        subprocess.run(["/bin/zsh", probe], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        captured = text_file(argv_out).splitlines()
        self.assertIn(name, captured, "the name was mangled by the shell: %r" % captured)
        self.assertFalse(os.path.exists(canary), "shell injection executed via the launch script")

    def test_command_substitution_in_a_name_is_inert(self):
        canary = os.path.join(self.tmp, "subst-canary")
        record = self._launch("$(touch %s)Ranger" % canary)
        self.assertFalse(os.path.exists(canary))
        argv = record["argv"]
        self.assertIn("$(touch", argv[argv.index("--name") + 1])

    def test_backticks_and_newlines_in_a_name_are_inert(self):
        canary = os.path.join(self.tmp, "tick-canary")
        record = self._launch("Ranger`touch %s`\nrm -rf /" % canary)
        self.assertFalse(os.path.exists(canary))
        name = record["argv"][record["argv"].index("--name") + 1]
        self.assertNotIn("\n", name, "a newline survived into the session name")

    def test_unicode_names_are_preserved_and_safe(self):
        name = u"ノヴァ Дрон \U0001f680"
        record = self._launch(name)
        self.assertEqual(record["argv"][record["argv"].index("--name") + 1], name + " 2")

    def test_a_name_cannot_alter_the_applescript_title(self):
        """The title must be one well-formed AppleScript string literal."""
        hostile = 'Ranger" & (do shell script "touch /tmp/th-applescript-canary") & "'
        record = self._launch(hostile)
        applescript = record.get("applescript") or ""
        marker = "set custom title of front window to "
        self.assertIn(marker, applescript)
        literal = applescript.split(marker, 1)[1].split("\n", 1)[0].strip()
        self.assertTrue(literal.startswith('"') and literal.endswith('"'))
        # Every quote inside the literal must be escaped, so the literal cannot
        # be closed early and no AppleScript can follow it.
        inner = literal[1:-1]
        index = 0
        decoded = []
        while index < len(inner):
            char = inner[index]
            if char == "\\":
                self.assertLess(index + 1, len(inner), "trailing escape in %r" % literal)
                decoded.append(inner[index + 1])
                index += 2
                continue
            self.assertNotEqual(char, '"', "an unescaped quote closed the literal early")
            decoded.append(char)
            index += 1
        expected = CORE.generation_display_name(CORE.sanitize_display_name(hostile), 2)
        self.assertEqual("".join(decoded), expected)
        self.assertIn('do shell script', expected, "the fixture lost its hostile payload")


if __name__ == "__main__":
    unittest.main(verbosity=2)
