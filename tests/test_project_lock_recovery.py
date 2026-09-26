"""A stale session must not block a project forever.

Before `project_in_use` is returned, the sessions holding the project are reconciled with the same
authoritative owner-health evidence the supported `session recover ... abandon` rests on:

  * a live owner keeps blocking (and is never touched),
  * an ORPHANED session whose owner is conclusively dead is abandoned automatically and the launch continues,
  * anything ambiguous, refused or failing stays blocked, with a reason,
  * only sessions of the requested project are ever examined or changed,
  * two racing launches cannot both recover or both take the project.

Liveness here is the REAL owner_liveness reading real evidence (a live pid, an exited pid, or nothing).
"""

import copy
import hashlib
import json
import os
import subprocess
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import CORE  # noqa: E402
from test_logical import TOKEN  # noqa: E402
from test_remote_launch import LaunchCase, good_profile  # noqa: E402

CTX = {"device_id": "d_0123456789abcdef"}


class StaleCase(LaunchCase):
    """A LaunchCase whose terminal never registers, so a launch stays CREATING unless a test says otherwise."""

    def setUp(self):
        super().setUp()
        self.behaviour = "silent"
        self._n = 0
        self.dead_pid = self.exited_pid()

    @staticmethod
    def exited_pid():
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        return proc.pid

    def evidence(self, agent, pid):
        """Write the Claude session record owner_liveness reads: `pid` alive or exited is the evidence."""
        with open(os.path.join(self.sessions_dir, "%s.json" % agent), "w") as handle:
            json.dump({"pid": pid, "sessionId": agent}, handle)

    def held(self, project="nova", state=None, agent=None, pid="none", name=None, orphaned=True):
        """Create a non-terminal session holding `project`. pid: an int (evidence), or 'none' (no evidence at all)."""
        self._n += 1
        record = CORE.logical_create(
            project=project, repository=self.real_repo if project == "nova" else self.other_repo,
            launch_token_sha256=hashlib.sha256(TOKEN.encode()).hexdigest(), launch_expires_epoch=9999999999, title="held %d" % self._n,
        )
        lsid = record["logical_session_id"]
        agent = agent or "agent-held-%04d" % self._n
        ok, why, _ = CORE.logical_register_owner(lsid, agent, 1, "chain%d" % self._n, None, TOKEN)
        self.assertTrue(ok, why)
        if pid != "none":
            self.evidence(agent, pid)
        if name:
            CORE.logical_mutate(lsid, lambda rec: rec.__setitem__("name", name))
        if state and state != CORE.LS_RUNNING:
            def mutate(rec):
                rec["state"] = state
                if state == CORE.LS_ORPHANED:
                    rec["orphaned"] = {"since_utc": "2026-09-27T00:00:00Z", "previous_state": "RUNNING", "reason": "test", "owner_epoch": rec.get("owner_epoch")}

            CORE.logical_mutate(lsid, mutate)
        return lsid

    def launch(self, request_id=None, **kw):
        self._n += 1
        body = {"project": kw.pop("project", "nova"), "task": "New work", "request_id": request_id or "req-stale-%06d" % self._n}
        return CORE.remote_create_session(
            body, CTX, terminal=self.fake_terminal, wait_seconds=0, isolation_check=self.isolation_check, **kw
        )

    def state(self, lsid):
        return CORE.logical_read(lsid)["state"]

    def add_other_project(self):
        self.other_repo = os.path.join(self.tmp, "other")
        os.makedirs(self.other_repo, exist_ok=True)
        CORE.project_add("other", self.other_repo)
        CORE.project_set_permissions("other", good_profile())
        CORE.project_set_remote_launch("other", True)


class TestStaleProjectRecovery(StaleCase):
    def setUp(self):
        super().setUp()
        self.add_other_project()

    def test_an_orphaned_session_with_a_conclusively_dead_owner_is_recovered_and_the_launch_continues(self):
        old = self.held(state=CORE.LS_ORPHANED, pid=self.dead_pid, name="Brag Skill")
        status, view = self.launch()
        self.assertEqual(status, 202, view)  # started; nothing here registers, so it is CREATING, not blocked
        self.assertEqual(len(self.launched), 1)
        record = CORE.logical_read(old)
        self.assertEqual(record["state"], CORE.LS_FAILED)
        self.assertIn("abandoned automatically", record["failure"]["reason"])
        events = [h for h in record["history"] if h["event"] == "abandoned"]
        self.assertEqual([e.get("by") for e in events], ["auto:project-launch"])
        self.assertEqual([r["logical_session_id"] for r in view["auto_recovered"]], [old])
        self.assertEqual(view["auto_recovered"][0]["name"], "Brag Skill")
        self.assertEqual(view["auto_recovered"][0]["previous_state"], "ORPHANED")

    def test_the_recovery_is_reported_through_the_phone_api_and_the_launch_is_the_same_request(self):
        old = self.held(state=CORE.LS_ORPHANED, pid=self.dead_pid, name="Old work")
        status, view = self.create(request_id="req-api-recover-1")
        self.assertIn(status, (201, 202), view)
        self.assertEqual(view["auto_recovered"][0]["logical_session_id"], old)
        self.assertEqual(len(self.launched), 1)

    def test_a_genuinely_running_session_with_a_live_owner_still_blocks_and_is_untouched(self):
        live = self.held(state=CORE.LS_RUNNING, pid=os.getpid(), name="Nova Health")
        before = copy.deepcopy(CORE.logical_read(live))
        status, view = self.launch()
        self.assertEqual((status, view["error"]), (409, "project_in_use"))
        self.assertEqual(view["logical_session_id"], live)
        self.assertEqual(view["session"]["name"], "Nova Health")
        self.assertEqual(view["session"]["state"], "RUNNING")
        self.assertIn("Nova Health", view["reason"])
        self.assertIn("RUNNING", view["reason"])
        self.assertIn("owner is alive", view["reason"])
        self.assertNotIn("auto_recovered", view)
        self.assertEqual(self.launched, [])
        after = CORE.logical_read(live)
        self.assertEqual(after["state"], "RUNNING")
        for key in ("state", "owner", "owner_epoch", "inbox", "history"):
            self.assertEqual(before[key], after[key], key)

    def test_an_orphaned_session_whose_owner_is_actually_alive_is_not_recovered(self):
        old = self.held(state=CORE.LS_ORPHANED, pid=os.getpid())
        status, view = self.launch()
        self.assertEqual((status, view["error"]), (409, "project_in_use"))
        self.assertIn("still alive", view["reason"])
        self.assertEqual(view["recovery"]["outcome"], "not_conclusive")
        self.assertEqual(self.state(old), CORE.LS_ORPHANED)
        self.assertEqual(self.launched, [])

    def test_ambiguous_health_stays_blocked_and_says_why(self):
        old = self.held(state=CORE.LS_ORPHANED, pid="none")  # no process binding, no Claude session record: no evidence either way
        status, view = self.launch()
        self.assertEqual((status, view["error"]), (409, "project_in_use"))
        self.assertIn("could not be proven dead", view["reason"])
        self.assertIn("no independent evidence", view["reason"])
        self.assertEqual(view["recovery"], {"attempted": False, "outcome": "not_conclusive", "verdict": "unknown"})
        self.assertEqual(self.state(old), CORE.LS_ORPHANED)
        self.assertEqual(self.launched, [])

    def test_a_recovery_the_system_refuses_fails_closed(self):
        old = self.held(state=CORE.LS_ORPHANED, pid=self.dead_pid)
        real = CORE.logical_recover
        CORE.logical_recover = lambda *a, **k: (False, "refused for the test", None)
        try:
            status, view = self.launch()
        finally:
            CORE.logical_recover = real
        self.assertEqual((status, view["error"]), (409, "project_in_use"))
        self.assertEqual(view["recovery"]["outcome"], "refused")
        self.assertEqual(self.state(old), CORE.LS_ORPHANED)
        self.assertEqual(self.launched, [])

    def test_a_recovery_that_raises_fails_closed_and_leaves_the_session_alone(self):
        old = self.held(state=CORE.LS_ORPHANED, pid=self.dead_pid)
        real = CORE.logical_recover

        def boom(*a, **k):
            raise RuntimeError("disk on fire")

        CORE.logical_recover = boom
        try:
            status, view = self.launch()
        finally:
            CORE.logical_recover = real
        self.assertEqual((status, view["error"]), (409, "project_in_use"))
        self.assertEqual(view["recovery"]["outcome"], "failed")
        self.assertEqual(self.state(old), CORE.LS_ORPHANED)
        self.assertEqual(self.launched, [])

    def test_ownership_that_changes_between_the_check_and_the_abandon_is_never_abandoned(self):
        old = self.held(state=CORE.LS_ORPHANED, pid=self.dead_pid)
        epoch = CORE.logical_read(old)["owner_epoch"]
        ok, why, _ = CORE.logical_recover(old, "abandon", by="auto:project-launch", expect_epoch=epoch + 1)
        self.assertFalse(ok)
        self.assertIn("ownership changed", why)
        self.assertEqual(self.state(old), CORE.LS_ORPHANED)

    def test_a_running_session_with_a_dead_owner_is_reconciled_through_the_normal_path_then_recovered(self):
        old = self.held(state=CORE.LS_RUNNING, pid=self.dead_pid, name="Crashed")
        status, view = self.launch()
        self.assertEqual(status, 202, view)
        record = CORE.logical_read(old)
        self.assertEqual(record["state"], CORE.LS_FAILED)
        self.assertIn("orphaned", [h["event"] for h in record["history"]])  # it went RUNNING -> ORPHANED -> FAILED, never straight
        self.assertEqual(view["auto_recovered"][0]["previous_state"], "RUNNING")

    def test_a_verdict_that_flips_to_alive_at_the_moment_of_recovery_is_never_abandoned(self):
        old = self.held(state=CORE.LS_ORPHANED, pid=self.dead_pid)
        calls = {"n": 0}

        def flips(record, now=None):
            calls["n"] += 1
            return ("dead", "gone") if calls["n"] == 1 else ("alive", "reattached meanwhile")

        status, view = self.launch(project_liveness=flips)
        self.assertEqual((status, view["error"]), (409, "project_in_use"))
        self.assertEqual(view["recovery"]["outcome"], "refused")
        self.assertIn("not conclusively dead at the moment of recovery", view["recovery"]["detail"])
        self.assertEqual(self.state(old), CORE.LS_ORPHANED)
        self.assertEqual(self.launched, [])

    def test_a_dead_looking_running_session_is_left_alone_unless_a_second_check_confirms_it(self):
        old = self.held(state=CORE.LS_RUNNING, pid=self.dead_pid)
        answers = iter([("dead", "gone"), ("unknown", "evidence flapped")] + [("unknown", "evidence flapped")] * 10)
        before = copy.deepcopy(CORE.logical_read(old))
        status, view = self.launch(project_liveness=lambda record, now=None: next(answers))
        self.assertEqual((status, view["error"]), (409, "project_in_use"))
        self.assertEqual(view["recovery"]["outcome"], "not_conclusive")
        after = CORE.logical_read(old)
        self.assertEqual(after["state"], CORE.LS_RUNNING)  # never orphaned on a single or an unconfirmed reading
        self.assertEqual(after["history"], before["history"])
        self.assertEqual(self.launched, [])

    def test_no_owner_lost_alert_is_sent_for_a_session_the_launch_is_recovering(self):
        for state in (CORE.LS_ORPHANED, CORE.LS_RUNNING):
            self.held(state=state, pid=self.dead_pid)
            alerts = []
            real = CORE._notify_attention
            CORE._notify_attention = lambda *a, **k: alerts.append(a)
            try:
                status, view = self.launch()
            finally:
                CORE._notify_attention = real
            self.assertEqual(status, 202, view)
            self.assertEqual(alerts, [], state)  # the user is not told to recover what is being recovered
            CORE.logical_mutate(view["logical_session_id"], lambda rec: rec.__setitem__("state", CORE.LS_COMPLETED))

    def test_a_running_session_with_no_evidence_is_not_touched(self):
        old = self.held(state=CORE.LS_RUNNING, pid="none")
        status, view = self.launch()
        self.assertEqual((status, view["error"]), (409, "project_in_use"))
        self.assertEqual(self.state(old), CORE.LS_RUNNING)

    def test_a_session_still_starting_blocks_and_one_that_never_registered_is_cleared(self):
        starting = CORE.logical_create(
            project="nova", repository=self.real_repo, launch_token_sha256=hashlib.sha256(TOKEN.encode()).hexdigest(), launch_expires_epoch=9999999999,
        )["logical_session_id"]
        status, view = self.launch()
        self.assertEqual((status, view["error"]), (409, "project_in_use"))
        self.assertIn("still starting", view["reason"])
        self.assertEqual(self.state(starting), CORE.LS_CREATING)
        CORE.logical_mutate(starting, lambda rec: rec["launch"].__setitem__("expires_epoch", 1.0))  # its launch window is long gone
        status, view = self.launch()
        self.assertEqual(status, 202, view)
        self.assertEqual(self.state(starting), CORE.LS_FAILED)

    def test_no_unrelated_session_is_modified(self):
        stale = self.held(state=CORE.LS_ORPHANED, pid=self.dead_pid)
        other_orphan = self.held(project="other", state=CORE.LS_ORPHANED, pid=self.dead_pid)  # equally dead, but another project
        other_live = self.held(project="other", state=CORE.LS_RUNNING, pid=os.getpid())
        finished = self.held(state=CORE.LS_RUNNING, pid=os.getpid())
        CORE.logical_mutate(finished, lambda rec: rec.__setitem__("state", CORE.LS_COMPLETED))
        frozen = {lsid: copy.deepcopy(CORE.logical_read(lsid)) for lsid in (other_orphan, other_live, finished)}
        status, view = self.launch()
        self.assertEqual(status, 202, view)
        self.assertEqual([r["logical_session_id"] for r in view["auto_recovered"]], [stale])
        for lsid, before in frozen.items():
            self.assertEqual(CORE.logical_read(lsid), before, lsid)  # byte-for-byte identical, updated_utc included

    def test_one_active_blocker_still_blocks_after_a_stale_one_was_recovered(self):
        stale = self.held(state=CORE.LS_ORPHANED, pid=self.dead_pid)
        live = self.held(state=CORE.LS_RUNNING, pid=os.getpid(), name="Still working")
        status, view = self.launch()
        self.assertEqual((status, view["error"]), (409, "project_in_use"))
        self.assertEqual(view["logical_session_id"], live)
        cleared = [r["logical_session_id"] for r in view.get("auto_recovered", [])]  # order is unspecified: the stale one may or may not be reached first
        self.assertIn(cleared, ([], [stale]))
        self.assertEqual(self.state(stale), CORE.LS_FAILED if cleared else CORE.LS_ORPHANED)  # and the report is honest either way
        self.assertEqual(self.state(live), CORE.LS_RUNNING)
        self.assertEqual(self.launched, [])

    def test_the_manual_recover_command_is_unchanged(self):
        old = self.held(state=CORE.LS_ORPHANED, pid=self.dead_pid)
        ok, why, record = CORE.logical_recover(old, "abandon", by="local")
        self.assertTrue(ok, why)
        self.assertEqual(record["failure"]["reason"], "abandoned after owner loss")
        ok, why, _ = CORE.logical_recover(old, "abandon", by="local")
        self.assertFalse(ok)  # it is no longer ORPHANED


class TestRacingLaunches(StaleCase):
    def test_two_launches_for_the_same_stale_project_recover_it_once_and_only_one_takes_the_project(self):
        old = self.held(state=CORE.LS_ORPHANED, pid=self.dead_pid)
        gate = threading.Barrier(2, timeout=30)
        results = []

        def racer(n):
            gate.wait()
            results.append(self.launch(request_id="req-race-%06d" % n))

        threads = [threading.Thread(target=racer, args=(n,)) for n in (1, 2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(len(results), 2)
        self.assertEqual(sorted(status for status, _ in results), [202, 409])
        loser = next(view for status, view in results if status == 409)
        self.assertEqual(loser["error"], "project_in_use")
        self.assertEqual(loser["session"]["state"], "CREATING")  # it names the winner, which is genuinely starting
        winner = next(view for status, view in results if status == 202)
        self.assertEqual([r["logical_session_id"] for r in winner["auto_recovered"]], [old])
        self.assertNotIn("auto_recovered", loser)  # only one launch recovered it
        self.assertEqual(len(self.launched), 1)  # exactly one Terminal window
        record = CORE.logical_read(old)
        self.assertEqual(record["state"], CORE.LS_FAILED)
        self.assertEqual(len([h for h in record["history"] if h["event"] == "abandoned"]), 1)
        active = [r for r in CORE.logical_list() if r["project"] == "nova" and r["state"] not in CORE.LS_TERMINAL]
        self.assertEqual(len(active), 1)


class TestStaleRecoveryGrok(StaleCase):
    """The Grok create path shares the same reconciliation."""

    def test_grok_path_recovers_a_dead_orphan_and_blocks_a_live_one(self):
        from test_grok_agent import FAKE  # noqa: F401  (the fixture module must be importable in this environment)

        CORE.project_set_agent("nova", "grok", True)
        old = self.held(state=CORE.LS_ORPHANED, pid=self.dead_pid)
        launched = []
        env_key = "CLAUDE_TERMINAL_HANDOFF_GROK_BIN"
        saved = os.environ.get(env_key)
        fake_bin = os.path.join(self.tmp, "grok")
        with open(fake_bin, "w") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        os.chmod(fake_bin, 0o700)
        os.environ[env_key] = fake_bin
        try:
            def launcher(lsid, mode):
                launched.append(lsid)
                return {"launched": True}

            body = {"project": "nova", "task": "Grok work", "agent": "grok", "request_id": "req-grok-stale-1"}
            status, view = CORE.remote_create_session(
                body, CTX, terminal=self.fake_terminal, wait_seconds=0, isolation_check=self.isolation_check,
                grok_preflight=lambda binary: (True, None, None), grok_launcher=launcher,
            )
            self.assertEqual(status, 202, view)
            self.assertEqual([r["logical_session_id"] for r in view["auto_recovered"]], [old])
            self.assertEqual(self.state(old), CORE.LS_FAILED)
            self.assertEqual(len(launched), 1)
            body["request_id"] = "req-grok-stale-2"
            status, view = CORE.remote_create_session(
                body, CTX, terminal=self.fake_terminal, wait_seconds=0, isolation_check=self.isolation_check,
                grok_preflight=lambda binary: (True, None, None), grok_launcher=launcher,
            )
            self.assertEqual((status, view["error"]), (409, "project_in_use"))  # the Grok session just started now holds it
            self.assertEqual(len(launched), 1)
        finally:
            if saved is None:
                os.environ.pop(env_key, None)
            else:
                os.environ[env_key] = saved


if __name__ == "__main__":
    unittest.main()
