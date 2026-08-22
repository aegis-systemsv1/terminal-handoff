"""Manual /handoff recovery and personal skill installation."""

import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import CORE, THTestCase, json_file  # noqa: E402


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_SOURCE = os.path.join(
    REPO_ROOT, "src", "terminal_handoff", "templates", "handoff"
)


class TestLiveSessionSnapshot(THTestCase):
    def setUp(self):
        super(TestLiveSessionSnapshot, self).setUp()
        self.old_home = os.environ.get("CLAUDE_TERMINAL_HANDOFF_HOME")
        os.environ["CLAUDE_TERMINAL_HANDOFF_HOME"] = self.home

    def tearDown(self):
        if self.old_home is None:
            os.environ.pop("CLAUDE_TERMINAL_HANDOFF_HOME", None)
        else:
            os.environ["CLAUDE_TERMINAL_HANDOFF_HOME"] = self.old_home
        super(TestLiveSessionSnapshot, self).tearDown()

    def test_snapshot_contains_only_the_minimum_verified_fields(self):
        payload = self.payload(percent=42, session_name="Ranger")
        session_id = payload.pop("_session_id")
        payload["secret_that_must_not_be_cached"] = "never-store-this"
        facts = CORE.extract_facts(payload)
        CORE.record_live_session(facts)

        record = json_file(CORE.live_session_path(session_id))
        serialized = json.dumps(record)
        self.assertNotIn("secret_that_must_not_be_cached", serialized)
        self.assertNotIn("never-store-this", serialized)
        self.assertEqual(record["payload"]["session_id"], session_id)
        self.assertEqual(record["payload"]["model"]["id"], payload["model"]["id"])
        self.assertEqual(record["payload"]["effort"]["level"], "high")
        self.assertFalse(record["privacy"]["stores_transcript_contents"])

    def test_stale_snapshot_is_refused(self):
        payload = self.payload(percent=42)
        session_id = payload.pop("_session_id")
        CORE.record_live_session(CORE.extract_facts(payload), now=time.time() - 120)
        _, _, reason = CORE.load_live_session(session_id)
        self.assertIn("stale", reason)

    def test_statusline_refresh_records_the_snapshot_used_by_handoff(self):
        payload = self.payload(percent=25)
        session_id = payload["_session_id"]
        code, _, error = self.statusline(
            payload, self.env(CLAUDE_TERMINAL_HANDOFF_TEST_MODE="1")
        )
        self.assertEqual(code, 0, error)
        self.assertEqual(json_file(CORE.live_session_path(session_id))["session_id"], session_id)


class TestManualHandoff(THTestCase):
    def setUp(self):
        super(TestManualHandoff, self).setUp()
        self.saved_env = dict(os.environ)
        os.environ.update(self.env(CLAUDE_TERMINAL_HANDOFF_TEST_MODE="1"))
        os.environ["CLAUDE_TERMINAL_HANDOFF_HOME"] = self.home

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved_env)
        super(TestManualHandoff, self).tearDown()

    def prepare(self, session_name="Ranger"):
        payload = self.payload(percent=51, session_name=session_name)
        session_id = payload.pop("_session_id")
        CORE.record_live_session(CORE.extract_facts(payload))
        binding = {
            "pid": 424242,
            "uid": os.getuid(),
            "name": "claude",
            "session_id": session_id,
            "process_cwd": self.workdir,
        }
        return payload, session_id, binding

    def run_manual(self, session_id, binding):
        with mock.patch.object(
            CORE, "bind_parent_claude_process", return_value=(binding, None)
        ), mock.patch.object(CORE, "verify_parent_binding", return_value=(True, None)):
            return CORE.run_manual_handoff(session_id)

    def test_manual_handoff_launches_with_exact_live_facts(self):
        payload, session_id, binding = self.prepare()
        result = self.run_manual(session_id, binding)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["state"], "simulated")

        manifest = json_file(CORE.manifest_path(session_id))
        self.assertEqual(manifest["trigger"]["mode"], "manual")
        self.assertEqual(manifest["model"]["id"], payload["model"]["id"])
        self.assertEqual(manifest["effort"]["level"], payload["effort"]["level"])
        self.assertEqual(manifest["outgoing"]["current_dir"], self.workdir)
        self.assertEqual(manifest["display"]["successor_display_name"], "Ranger 2")
        claim = json_file(CORE.th_path("triggered", session_id))
        self.assertEqual(claim["mode"], "manual")

    def test_a_live_transfer_blocks_a_duplicate_manual_successor(self):
        _, session_id, binding = self.prepare()
        first = self.run_manual(session_id, binding)
        self.assertTrue(first["ok"], first)
        second = self.run_manual(session_id, binding)
        self.assertFalse(second["ok"])
        self.assertEqual(second["state"], "in_progress")

    def test_a_failed_transfer_is_archived_before_manual_retry(self):
        _, session_id, binding = self.prepare()
        first = self.run_manual(session_id, binding)
        self.assertTrue(first["ok"], first)
        transfer_path = CORE.transfer_path(session_id)
        transfer = json_file(transfer_path)
        transfer["state"] = CORE.TRANSFER_FAILED
        transfer["owner"] = "parent"
        CORE.write_json_private(transfer_path, transfer)

        second = self.run_manual(session_id, binding)
        self.assertTrue(second["ok"], second)
        archive = second["archived_failed_attempt"]
        self.assertTrue(os.path.isfile(archive))
        archived = json_file(archive)
        self.assertEqual(archived["prior"]["transfer"]["state"], CORE.TRANSFER_FAILED)
        self.assertEqual(json_file(transfer_path)["state"], CORE.TRANSFER_LAUNCHING)

    def test_unproven_parent_refuses_without_claiming_or_launching(self):
        _, session_id, _ = self.prepare()
        with mock.patch.object(
            CORE,
            "bind_parent_claude_process",
            return_value=(None, "no Claude Code process was found"),
        ):
            result = CORE.run_manual_handoff(session_id)
        self.assertFalse(result["ok"])
        self.assertIn("could not be proven", result["reason"])
        self.assertFalse(os.path.exists(CORE.th_path("triggered", session_id)))
        self.assertFalse(os.path.exists(CORE.transfer_path(session_id)))

    def test_completed_transfer_cannot_be_reopened(self):
        _, session_id, binding = self.prepare()
        first = self.run_manual(session_id, binding)
        self.assertTrue(first["ok"], first)
        transfer = json_file(CORE.transfer_path(session_id))
        transfer["state"] = CORE.TRANSFER_COMPLETE
        transfer["owner"] = "successor"
        CORE.write_json_private(CORE.transfer_path(session_id), transfer)
        result = self.run_manual(session_id, binding)
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "already_complete")

    def test_recent_orphan_claim_is_not_replaced_during_the_launcher_race_window(self):
        _, session_id, binding = self.prepare()
        CORE.ensure_dirs()
        CORE.write_json_private(
            CORE.th_path("triggered", session_id),
            {"claimed_epoch": time.time(), "claimed_utc": CORE.utc_stamp()},
        )
        result = self.run_manual(session_id, binding)
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "claimed")

    def test_stale_orphan_claim_is_archived_and_recovered(self):
        _, session_id, binding = self.prepare()
        CORE.ensure_dirs()
        CORE.write_json_private(
            CORE.th_path("triggered", session_id),
            {
                "claimed_epoch": time.time() - 300,
                "claimed_utc": "2000-01-01T00:00:00Z",
                "mode": "automatic",
            },
        )
        result = self.run_manual(session_id, binding)
        self.assertTrue(result["ok"], result)
        self.assertTrue(os.path.isfile(result["archived_failed_attempt"]))
        archived = json_file(result["archived_failed_attempt"])
        self.assertIn("orphaned", archived["reason"])


class TestHandoffSkillInstall(THTestCase):
    def setUp(self):
        super(TestHandoffSkillInstall, self).setUp()
        self.old_home = os.environ.get("CLAUDE_TERMINAL_HANDOFF_HOME")
        os.environ["CLAUDE_TERMINAL_HANDOFF_HOME"] = self.home
        self.destination = os.path.join(self.tmp, ".claude", "skills", "handoff")

    def tearDown(self):
        if self.old_home is None:
            os.environ.pop("CLAUDE_TERMINAL_HANDOFF_HOME", None)
        else:
            os.environ["CLAUDE_TERMINAL_HANDOFF_HOME"] = self.old_home
        super(TestHandoffSkillInstall, self).tearDown()

    def test_installs_idempotently_and_uninstalls_only_managed_files(self):
        first = CORE.install_handoff_skill(SKILL_SOURCE, self.destination)
        second = CORE.install_handoff_skill(SKILL_SOURCE, self.destination)
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        skill = os.path.join(self.destination, "SKILL.md")
        helper = os.path.join(self.destination, "manual-handoff.py")
        with open(skill, "r") as handle:
            skill_text = handle.read()
        self.assertIn("disable-model-invocation: true", skill_text)
        self.assertIn("${CLAUDE_SESSION_ID}", skill_text)
        self.assertEqual(os.stat(helper).st_mode & 0o777, 0o700)

        preview = CORE.uninstall_handoff_skill(dry_run=True)
        self.assertTrue(preview["dry_run"])
        removed = CORE.uninstall_handoff_skill(dry_run=False)
        self.assertTrue(removed["ok"])
        self.assertFalse(os.path.exists(skill))
        self.assertFalse(os.path.exists(helper))

    def test_refuses_to_replace_a_user_owned_handoff_skill(self):
        os.makedirs(self.destination)
        with open(os.path.join(self.destination, "SKILL.md"), "w") as handle:
            handle.write("---\nname: handoff\n---\nMy own skill.\n")
        result = CORE.install_handoff_skill(SKILL_SOURCE, self.destination)
        self.assertFalse(result["ok"])
        self.assertIn("user-owned", result["error"])
        with open(os.path.join(self.destination, "SKILL.md"), "r") as handle:
            self.assertIn("My own skill", handle.read())


if __name__ == "__main__":
    unittest.main()
