"""Threshold detection and malformed-input safety.

Ported unchanged from the proven Terminal Handoff 1.0.0 suite.
"""
import json
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import (  # noqa: E402
    THTestCase,
    run_th,
)


class TestThreshold(THTestCase):
    def test_01_seventy_nine_does_not_trigger(self):
        decision = self.evaluate(self.payload(percent=79.0))
        self.assertFalse(decision["trigger"])
        self.assertEqual(decision["state"], "below")

    def test_02_seventy_nine_point_nine_nine_does_not_trigger(self):
        decision = self.evaluate(self.payload(percent=79.99))
        self.assertFalse(decision["trigger"])
        self.assertEqual(decision["state"], "below")

    def test_03_eighty_triggers(self):
        decision = self.evaluate(self.payload(percent=80.0))
        self.assertTrue(decision["trigger"])
        self.assertEqual(decision["state"], "trigger")

    def test_04_eighty_one_triggers(self):
        decision = self.evaluate(self.payload(percent=81.0))
        self.assertTrue(decision["trigger"])

    def test_05_null_percentage_does_not_trigger(self):
        decision = self.evaluate(self.payload(percent=None))
        self.assertFalse(decision["trigger"])
        self.assertEqual(decision["state"], "no_percent")

    def test_06_missing_percentage_does_not_trigger(self):
        decision = self.evaluate(self.payload(include_context=False))
        self.assertFalse(decision["trigger"])
        self.assertEqual(decision["state"], "no_percent")

    def test_07_rate_limit_eighty_does_not_trigger_when_context_low(self):
        decision = self.evaluate(self.payload(percent=40.0, rate_limit_percent=80.0))
        self.assertFalse(decision["trigger"])
        self.assertEqual(decision["state"], "below")
        self.assertEqual(decision["percent"], 40.0)

    def test_custom_threshold_env(self):
        env = self.env(CLAUDE_TERMINAL_HANDOFF_THRESHOLD="75")
        self.assertTrue(self.evaluate(self.payload(percent=76.0), env)["trigger"])
        self.assertFalse(self.evaluate(self.payload(percent=74.0), env)["trigger"])


# ---------------------------------------------------------------------------
# 2. Malformed and missing input
# ---------------------------------------------------------------------------


class TestMalformedInput(THTestCase):
    def test_08_malformed_json_fails_safely(self):
        code, out, err = run_th(["evaluate"], "{not json at all", self.env())
        self.assertEqual(code, 0)
        decision = json.loads(out)
        self.assertFalse(decision["trigger"])
        self.assertEqual(decision["state"], "invalid")

    def test_08b_empty_stdin_fails_safely(self):
        code, out, err = run_th(["evaluate"], "", self.env())
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out)["trigger"])

    def test_08c_statusline_survives_malformed_json(self):
        code, out, err = run_th(["statusline"], "<<<garbage>>>", self.env())
        self.assertEqual(code, 0)
        self.assertIn("TH", out)

    def test_09_missing_session_id_fails_safely(self):
        decision = self.evaluate(self.payload(percent=95.0, include_session=False))
        self.assertFalse(decision["trigger"])
        self.assertEqual(decision["state"], "blocked")
        self.assertTrue(any("session_id" in e for e in decision["errors"]))

    def test_10_missing_transcript_path_fails_safely(self):
        decision = self.evaluate(self.payload(percent=95.0, include_transcript=False))
        self.assertFalse(decision["trigger"])
        self.assertEqual(decision["state"], "blocked")
        self.assertTrue(any("transcript" in e for e in decision["errors"]))

    def test_11_missing_model_id_fails_safely(self):
        decision = self.evaluate(self.payload(percent=95.0, include_model=False))
        self.assertFalse(decision["trigger"])
        self.assertEqual(decision["state"], "blocked")
        self.assertTrue(any("model.id" in e for e in decision["errors"]))

    def test_31_missing_transcript_file_fails_safely(self):
        payload = self.payload(percent=95.0)
        os.unlink(payload["transcript_path"])
        decision = self.evaluate(payload)
        self.assertFalse(decision["trigger"])
        self.assertTrue(any("does not exist" in e for e in decision["errors"]))

    def test_32_empty_transcript_fails_safely(self):
        sid = str(uuid.uuid4())
        transcript = self.make_transcript(sid, empty=True)
        decision = self.evaluate(self.payload(percent=95.0, session_id=sid, transcript=transcript))
        self.assertFalse(decision["trigger"])
        self.assertTrue(any("empty" in e for e in decision["errors"]))

    def test_33_non_jsonl_transcript_fails_safely(self):
        sid = str(uuid.uuid4())
        transcript = self.make_transcript(sid, jsonl=False)
        decision = self.evaluate(self.payload(percent=95.0, session_id=sid, transcript=transcript))
        self.assertFalse(decision["trigger"])
        self.assertTrue(any("JSONL" in e for e in decision["errors"]))

    def test_transcript_directory_is_rejected(self):
        payload = self.payload(percent=95.0)
        payload["transcript_path"] = self.tmp
        decision = self.evaluate(payload)
        self.assertFalse(decision["trigger"])

    def test_relative_transcript_is_rejected(self):
        payload = self.payload(percent=95.0)
        payload["transcript_path"] = "relative/path.jsonl"
        decision = self.evaluate(payload)
        self.assertFalse(decision["trigger"])
        self.assertTrue(any("absolute" in e for e in decision["errors"]))


# ---------------------------------------------------------------------------
# 3. Effort preservation
# ---------------------------------------------------------------------------


class TestFixtureFiles(THTestCase):
    """Drive the detector from the on-disk fixture files in tests/fixtures/.

    The fixtures carry placeholder paths so the repository contains no
    machine-specific or personal path of any kind.
    """

    FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

    def load(self, name):
        with open(os.path.join(self.FIXTURES, name)) as handle:
            raw = handle.read()
        session_id = json.loads(raw)["session_id"]
        transcript = self.make_transcript(session_id)
        raw = raw.replace("<TRANSCRIPT>", transcript).replace("<WORKDIR>", self.workdir)
        payload = json.loads(raw)
        payload["_session_id"] = session_id
        return payload

    def test_fixture_below_threshold_does_not_trigger(self):
        self.assertFalse(self.evaluate(self.load("below_threshold.json"))["trigger"])

    def test_fixture_at_threshold_triggers(self):
        self.assertTrue(self.evaluate(self.load("at_threshold.json"))["trigger"])

    def test_fixture_null_percentage_does_not_trigger(self):
        decision = self.evaluate(self.load("null_percentage.json"))
        self.assertFalse(decision["trigger"])
        self.assertEqual(decision["state"], "no_percent")

    def test_fixture_high_rate_limit_low_context_does_not_trigger(self):
        decision = self.evaluate(self.load("rate_limit_high_context_low.json"))
        self.assertFalse(decision["trigger"])
        self.assertEqual(decision["percent"], 40.0)

    def test_fixture_missing_effort_triggers_without_effort_flag(self):
        decision = self.evaluate(self.load("missing_effort.json"))
        self.assertTrue(decision["trigger"])
        self.assertFalse(decision["effort_available"])

    def test_fixtures_contain_no_personal_paths(self):
        for name in os.listdir(self.FIXTURES):
            with open(os.path.join(self.FIXTURES, name)) as handle:
                body = handle.read()
            self.assertNotIn("/" + "Users/", body, "%s contains an absolute personal path" % name)
            self.assertNotIn("/" + "home/", body, "%s contains an absolute home path" % name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
