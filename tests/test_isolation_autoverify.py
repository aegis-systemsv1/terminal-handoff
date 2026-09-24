"""Automatic isolation verification on unrecorded Claude versions.

ensure_isolation_verified() runs the same probe as `remote verify-isolation`
for a version with no valid record, records only a PASS, and fails closed on
every other outcome. It never trusts a version it has not itself verified."""

import os
import re
import sys
import fcntl
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import CORE, json_file  # noqa: E402
from test_logical import LogicalCase  # noqa: E402

VERSION = "2.1.283"


class AutoVerifyCase(LogicalCase):
    def run_for(self, version=VERSION, broken=False, granted=True, fail_probe_calls=False, calls=None):
        def run(argv, **kw):
            class R:
                stdout = ("%s (Claude Code)" % version).encode()
                stderr = b""
                returncode = 0

            if "--version" in argv:
                return R()
            if calls is not None:
                calls.append(argv)
            if fail_probe_calls:
                raise OSError("claude exploded")
            target = re.search(r"touch (\S+)", argv[argv.index("-p") + 1]).group(1)
            has_profile = "--settings" in argv
            excluded = argv[argv.index("--setting-sources") + 1] == ""
            if (has_profile and granted) or (not excluded and not broken) or (broken and not has_profile):
                open(target, "w").close()
            return R()

        return run

    def versions(self):
        if not os.path.exists(CORE.isolation_state_path()):
            return {}
        return (json_file(CORE.isolation_state_path()) or {}).get("versions", {})


class TestAutoVerify(AutoVerifyCase):
    def test_unknown_version_that_passes_is_recorded_and_allowed(self):
        ok, why = CORE.ensure_isolation_verified(self.fake_claude, run=self.run_for())
        self.assertTrue(ok, why)
        self.assertTrue(self.versions()[VERSION]["broader_project_allow_blocked"])
        self.assertEqual(self.versions()[VERSION]["claude_version"], VERSION)

    def test_unknown_version_that_fails_is_denied_with_the_failed_check(self):
        ok, why = CORE.ensure_isolation_verified(self.fake_claude, run=self.run_for(broken=True))
        self.assertFalse(ok)
        self.assertIn("broader_project_allow_blocked", why)
        self.assertNotIn(VERSION, self.versions())

    def test_profile_settings_failure_names_that_check(self):
        ok, why = CORE.ensure_isolation_verified(self.fake_claude, run=self.run_for(granted=False))
        self.assertFalse(ok)
        self.assertIn("profile_settings_effective", why)

    def test_already_verified_version_is_not_reprobed(self):
        CORE.ensure_isolation_verified(self.fake_claude, run=self.run_for())
        calls = []
        ok, _ = CORE.ensure_isolation_verified(self.fake_claude, run=self.run_for(calls=calls))
        self.assertTrue(ok)
        self.assertEqual(calls, [])

    def test_only_the_exact_version_is_trusted(self):
        CORE.ensure_isolation_verified(self.fake_claude, run=self.run_for())
        calls = []
        ok, _ = CORE.ensure_isolation_verified(self.fake_claude, run=self.run_for(version="2.1.284", calls=calls))
        self.assertTrue(ok)
        self.assertEqual(len(calls), 2)  # a new patch version was probed itself, not trusted transitively
        self.assertEqual(set(self.versions()), {VERSION, "2.1.284"})

    def test_malformed_record_is_reverified_not_trusted(self):
        for bad in ({"broader_project_allow_blocked": "yes", "profile_settings_effective": True, "claude_version": VERSION},
                    {"broader_project_allow_blocked": True},
                    "garbage", None):
            CORE.update_json_locked(CORE.isolation_state_path(), lambda d, b=bad: d.__setitem__("versions", {VERSION: b}))
            calls = []
            ok, why = CORE.ensure_isolation_verified(self.fake_claude, run=self.run_for(broken=True, calls=calls))
            self.assertFalse(ok, bad)
            self.assertTrue(calls, bad)

    def test_stale_record_for_another_version_is_reverified(self):
        stale = {"broader_project_allow_blocked": True, "profile_settings_effective": True, "claude_version": "2.0.0"}
        CORE.update_json_locked(CORE.isolation_state_path(), lambda d: d.__setitem__("versions", {VERSION: stale}))
        calls = []
        ok, _ = CORE.ensure_isolation_verified(self.fake_claude, run=self.run_for(broken=True, calls=calls))
        self.assertFalse(ok)
        self.assertTrue(calls)

    def test_command_error_fails_closed_and_is_not_recorded(self):
        ok, why = CORE.ensure_isolation_verified(self.fake_claude, run=self.run_for(fail_probe_calls=True))
        self.assertFalse(ok)
        self.assertNotIn(VERSION, self.versions())
        calls = []  # not persisted, so the next launch retries and can heal
        ok, _ = CORE.ensure_isolation_verified(self.fake_claude, run=self.run_for(calls=calls))
        self.assertTrue(ok)
        self.assertTrue(calls)

    def test_probe_exception_fails_closed(self):
        def boom(claude_bin, run=None):
            raise RuntimeError("probe crashed")

        ok, why = CORE.ensure_isolation_verified(self.fake_claude, run=self.run_for(), probe=boom)
        self.assertFalse(ok)
        self.assertIn("failed unexpectedly", why)

    def test_undeterminable_version_fails_closed_without_probing(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            raise OSError("no")

        ok, why = CORE.ensure_isolation_verified(self.fake_claude, run=run)
        self.assertFalse(ok)
        self.assertEqual(len(calls), 1)  # only the --version call

    def test_concurrent_launches_probe_once_and_share_the_result(self):
        calls = []
        results = []
        gate = threading.Barrier(4)

        def worker():
            gate.wait()
            results.append(CORE.ensure_isolation_verified(self.fake_claude, run=self.run_for(calls=calls)))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(len(results), 4)
        self.assertTrue(all(ok for ok, _ in results))
        self.assertEqual(len(calls), 2)  # one probe (two attempts), not four
        self.assertTrue(self.versions()[VERSION]["profile_settings_effective"])


class TestSingleFlightLocking(AutoVerifyCase):
    def good_result(self, version=VERSION, **extra):
        rec = {"claude_version": version, "broader_project_allow_blocked": True,
               "profile_settings_effective": True, "verified_utc": "T"}
        rec.update(extra)
        return rec

    def run_thread(self, fn, box):
        t = threading.Thread(target=lambda: box.append(fn()))
        t.start()
        return t

    def test_the_probe_does_not_hold_the_general_json_lock(self):
        entered, release = threading.Event(), threading.Event()

        def slow_probe(claude_bin, run=None):
            entered.set()
            release.wait(30)
            return self.good_result()

        box = []
        t = self.run_thread(lambda: CORE.ensure_isolation_verified(self.fake_claude, run=self.run_for(), probe=slow_probe), box)
        self.assertTrue(entered.wait(10))
        started = time.monotonic()
        CORE.update_json_locked(CORE.isolation_state_path(), lambda d: d.setdefault("versions", {}).__setitem__("other", {"x": 1}))
        self.assertLess(time.monotonic() - started, 2.0)  # not blocked behind the in-flight probe
        release.set()
        t.join(30)
        self.assertTrue(box[0][0])
        self.assertIn("other", self.versions())
        self.assertIn(VERSION, self.versions())

    def test_unrelated_versions_verify_in_parallel(self):
        both = threading.Barrier(2, timeout=10)

        def probe_for(version):
            def probe(claude_bin, run=None):
                both.wait()  # only passes if BOTH probes are in flight at once
                return self.good_result(version)
            return probe

        box = []
        threads = [
            self.run_thread(lambda v=v: CORE.ensure_isolation_verified(self.fake_claude, run=self.run_for(version=v), probe=probe_for(v)), box)
            for v in ("2.1.290", "2.1.291")
        ]
        for t in threads:
            t.join(30)
        self.assertEqual([ok for ok, _ in box], [True, True])
        self.assertEqual(set(self.versions()), {"2.1.290", "2.1.291"})

    def test_a_waiter_consumes_the_holders_failure_without_reprobing(self):
        entered, release = threading.Event(), threading.Event()
        probes = []

        def failing_probe(claude_bin, run=None):
            probes.append(1)
            entered.set()
            release.wait(30)
            return self.good_result(broader_project_allow_blocked=False)

        box = []
        first = self.run_thread(lambda: CORE.ensure_isolation_verified(self.fake_claude, run=self.run_for(), probe=failing_probe), box)
        self.assertTrue(entered.wait(10))
        second = self.run_thread(lambda: CORE.ensure_isolation_verified(self.fake_claude, run=self.run_for(), probe=failing_probe), box)
        time.sleep(0.3)  # let the second launch queue on the version lock
        release.set()
        first.join(30)
        second.join(30)
        self.assertEqual(len(probes), 1)
        self.assertEqual([ok for ok, _ in box], [False, False])
        self.assertTrue(all("broader_project_allow_blocked" in why for _, why in box))
        self.assertNotIn(VERSION, self.versions())  # failures are never written to isolation.json

    def test_a_later_independent_launch_reprobes_after_a_failure(self):
        probes = []

        def probe(claude_bin, run=None):
            probes.append(1)
            return self.good_result(broader_project_allow_blocked=len(probes) > 1)

        self.assertFalse(CORE.ensure_isolation_verified(self.fake_claude, run=self.run_for(), probe=probe)[0])
        time.sleep(0.05)
        self.assertTrue(CORE.ensure_isolation_verified(self.fake_claude, run=self.run_for(), probe=probe)[0])
        self.assertEqual(len(probes), 2)

    def test_a_stuck_verifier_makes_a_launch_fail_closed(self):
        lock = CORE.th_path("remote", "isolation-verify-%s.lock" % CORE._isolation_version_slug(VERSION))
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        probes = []
        try:
            ok, why = CORE.ensure_isolation_verified(
                self.fake_claude, run=self.run_for(), probe=lambda *a, **k: probes.append(1),
                wait_timeout=0.3, poll=0.02)
        finally:
            os.close(fd)
        self.assertFalse(ok)
        self.assertIn("in progress", why)
        self.assertEqual(probes, [])

    def test_a_result_recorded_during_the_probe_is_kept_not_overwritten(self):
        def probe(claude_bin, run=None):
            CORE.update_json_locked(CORE.isolation_state_path(), lambda d: d.setdefault("versions", {}).__setitem__(
                VERSION, self.good_result(verified_utc="MANUAL")))
            return self.good_result(verified_utc="PROBE")

        ok, _ = CORE.ensure_isolation_verified(self.fake_claude, run=self.run_for(), probe=probe)
        self.assertTrue(ok)
        self.assertEqual(self.versions()[VERSION]["verified_utc"], "MANUAL")

    def test_the_verifying_lock_is_released_after_every_outcome(self):
        def boom(claude_bin, run=None):
            raise RuntimeError("x")

        CORE.ensure_isolation_verified(self.fake_claude, run=self.run_for(), probe=boom)
        ok, _ = CORE.ensure_isolation_verified(self.fake_claude, run=self.run_for(), wait_timeout=0.5)
        self.assertTrue(ok)  # would time out if the first call leaked the lock


if __name__ == "__main__":
    unittest.main()
