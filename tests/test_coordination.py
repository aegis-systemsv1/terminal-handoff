"""Multi-session presence, conflict detection and coordination boundaries."""

import json
import os
import sys
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import CORE, THTestCase, run_th  # noqa: E402


class TestCoordinationPresence(THTestCase):
    def record(self, payload, observed):
        payload.pop("_session_id", None)
        CORE.record_live_session(CORE.extract_facts(payload), now=observed)

    def test_same_workspace_sessions_are_peers(self):
        now = time.time()
        first = self.payload(percent=20, session_name="Nova A")
        second = self.payload(percent=30, session_name="Nova B")
        first_id = first["_session_id"]
        second_id = second["_session_id"]
        with mock.patch.dict(os.environ, self.env(), clear=True):
            self.record(first, now)
            self.record(second, now)
            peers = CORE.coordination_peers_for_session(first_id, now=now)

        self.assertEqual(len(peers), 1)
        self.assertEqual(peers[0]["session_id"], second_id)
        self.assertEqual(peers[0]["display_name"], "Nova B")
        self.assertEqual(peers[0]["relation"], "same_workspace")

    def test_nested_workspace_is_a_conflict_but_sibling_worktrees_are_not(self):
        now = time.time()
        nested = os.path.join(self.workdir, "src")
        os.makedirs(nested)
        sibling = os.path.join(self.tmp, "other-worktree")
        os.makedirs(sibling)
        current = self.payload(workdir=self.workdir, session_name="Current")
        child = self.payload(workdir=nested, session_name="Child")
        isolated = self.payload(workdir=sibling, session_name="Isolated")
        current_id = current["_session_id"]
        with mock.patch.dict(os.environ, self.env(), clear=True):
            for payload in (current, child, isolated):
                self.record(payload, now)
            peers = CORE.coordination_peers_for_session(current_id, now=now)
            status = CORE.coordination_status(now=now)

        self.assertEqual([peer["display_name"] for peer in peers], ["Child"])
        self.assertEqual(peers[0]["relation"], "nested_workspace")
        self.assertEqual(len(status["conflicting_workspaces"]), 1)
        self.assertEqual(
            status["conflicting_workspaces"][0]["relation"], "nested_workspace"
        )

    def test_stale_sessions_are_not_reported(self):
        now = time.time()
        current = self.payload(session_name="Current")
        stale = self.payload(session_name="Stale")
        current_id = current["_session_id"]
        with mock.patch.dict(os.environ, self.env(), clear=True):
            self.record(current, now)
            self.record(stale, now - 25)
            peers = CORE.coordination_peers_for_session(
                current_id, now=now, max_age=20
            )
        self.assertEqual(peers, [])

    def test_coordination_status_never_turns_peer_messages_into_consent(self):
        with mock.patch.dict(os.environ, self.env(), clear=True):
            result = CORE.coordination_status()
        self.assertEqual(result["native_messaging"]["tools"], ["ListAgents", "SendMessage"])
        self.assertFalse(result["native_messaging"]["messages_are_user_approval"])

    def test_cli_reports_shared_workspace_sessions(self):
        now = time.time()
        with mock.patch.dict(os.environ, self.env(), clear=True):
            self.record(self.payload(session_name="One"), now)
            self.record(self.payload(session_name="Two"), now)
        code, output, error = run_th(
            ["coordination", "status"], env=self.env()
        )
        self.assertEqual(code, 0, error)
        result = json.loads(output)
        self.assertEqual(result["active_sessions"], 2)
        self.assertEqual(len(result["conflicting_workspaces"]), 1)


class TestCoordinationStatusLine(THTestCase):
    def test_statusline_shows_a_fresh_peer_count(self):
        first = self.payload(percent=20, session_name="One")
        second = self.payload(percent=30, session_name="Two")
        code, _, error = self.statusline(first, self.env())
        self.assertEqual(code, 0, error)
        code, output, error = self.statusline(second, self.env())
        self.assertEqual(code, 0, error)
        self.assertIn("peers 1", output)
