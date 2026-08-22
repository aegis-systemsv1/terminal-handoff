"""Durable notification outbox, routing, signing and privacy guarantees."""

import hashlib
import hmac
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import CORE, THTestCase, json_file, run_th  # noqa: E402


class FakeResponse(object):
    def __init__(self, status=202):
        self.status = status
        self.closed = False

    def getcode(self):
        return self.status

    def read(self, limit=None):
        return b"accepted"

    def close(self):
        self.closed = True


class FakeProcessResult(object):
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class NotificationTestCase(THTestCase):
    def setUp(self):
        super(NotificationTestCase, self).setUp()
        self._old_home = os.environ.get("CLAUDE_TERMINAL_HANDOFF_HOME")
        self._old_test = os.environ.get("CLAUDE_TERMINAL_HANDOFF_TEST_MODE")
        os.environ["CLAUDE_TERMINAL_HANDOFF_HOME"] = self.home
        os.environ["CLAUDE_TERMINAL_HANDOFF_TEST_MODE"] = "1"
        CORE.ensure_dirs()
        CORE.save_notification_config(CORE.default_notification_config())
        CORE.set_notification_presence("home", source="test")

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("CLAUDE_TERMINAL_HANDOFF_HOME", None)
        else:
            os.environ["CLAUDE_TERMINAL_HANDOFF_HOME"] = self._old_home
        if self._old_test is None:
            os.environ.pop("CLAUDE_TERMINAL_HANDOFF_TEST_MODE", None)
        else:
            os.environ["CLAUDE_TERMINAL_HANDOFF_TEST_MODE"] = self._old_test
        os.environ.pop("TERMINAL_HANDOFF_WEBHOOK_SECRET", None)
        super(NotificationTestCase, self).tearDown()


class TestNotificationEvents(NotificationTestCase):
    def transfer_record(self):
        return {
            "parent_session_id": "parent-12345678",
            "chain_id": "abcdef012345",
            "parent_generation": 1,
            "successor_generation": 2,
            "parent_display_name": "Ranger",
            "successor_display_name": "Ranger 2",
            "owner": "successor",
            "manifest_path": "/private/path/that/must/not/leave",
            "transcript_path": "/private/transcript.jsonl",
        }

    def test_success_message_names_both_sessions_and_owner(self):
        event = CORE.transfer_notification_event(
            self.transfer_record(), "TRANSFER_COMPLETE", "parent stopped"
        )
        self.assertEqual(event["event_type"], "terminal_handoff.complete")
        self.assertEqual(
            event["message"],
            "Handoff complete: Ranger → Ranger 2. Ranger 2 now owns the work.",
        )
        self.assertEqual(event["routing_hint"], "sms_when_away")
        self.assertIn("sms", event["suggested_channels"])
        encoded = json.dumps(event)
        self.assertNotIn("transcript", encoded)
        self.assertNotIn("/private/", encoded)

    def test_failure_message_keeps_parent_as_owner(self):
        record = self.transfer_record()
        record["owner"] = "parent"
        event = CORE.transfer_notification_event(
            record, "TRANSFER_FAILED", "successor heartbeat timed out"
        )
        self.assertEqual(event["urgency"], "critical")
        self.assertIn("Ranger remains owner", event["message"])
        self.assertIn("heartbeat timed out", event["message"])
        self.assertIn("sms", event["suggested_channels"])

    def test_terminal_transition_commits_then_queues_once(self):
        path = self.write_transfer("queue-12345678", None, state="PARENT_STOP_REQUESTED")
        ok, record = CORE.transfer_transition(path, "TRANSFER_COMPLETE", reason="stopped")
        self.assertTrue(ok)
        event = CORE.transfer_notification_event(record, "TRANSFER_COMPLETE", "stopped")
        pending = CORE.notification_outbox_path("pending", event["event_id"])
        self.assertTrue(os.path.isfile(pending))
        self.assertEqual(json_file(path)["state"], "TRANSFER_COMPLETE")

        repeated, _ = CORE.transfer_transition(path, "TRANSFER_COMPLETE", reason="again")
        self.assertFalse(repeated)
        names = [name for name in os.listdir(os.path.dirname(pending)) if name.endswith(".json")]
        self.assertEqual(len(names), 1)

    def test_enqueue_is_idempotent_across_retries(self):
        event = CORE.transfer_notification_event(
            self.transfer_record(), "TRANSFER_COMPLETE", "stopped"
        )
        first = CORE.enqueue_notification(event, spawn=False)
        second = CORE.enqueue_notification(event, spawn=False)
        self.assertEqual(first, second)
        self.assertEqual(len(os.listdir(CORE.th_path("outbox", "pending"))), 1)

    def test_reconciliation_repairs_commit_then_crash_gap(self):
        self.write_transfer(
            "reconcile-12345678",
            None,
            state="TRANSFER_COMPLETE",
            owner="successor",
            terminal_handoff_version=CORE.TERMINAL_HANDOFF_VERSION,
        )
        self.assertEqual(os.listdir(CORE.th_path("outbox", "pending")), [])
        self.assertEqual(CORE.reconcile_notification_outbox(), 1)
        pending = os.listdir(CORE.th_path("outbox", "pending"))
        self.assertEqual(len(pending), 1)
        self.assertEqual(
            json_file(os.path.join(CORE.th_path("outbox", "pending"), pending[0]))["event"][
                "event_type"
            ],
            "terminal_handoff.complete",
        )

    def test_reconciliation_does_not_alert_on_pre_notification_history(self):
        self.write_transfer(
            "historical-12345678",
            None,
            state="TRANSFER_COMPLETE",
            owner="successor",
            terminal_handoff_version="1.1.1",
        )
        self.assertEqual(CORE.reconcile_notification_outbox(), 0)
        self.assertEqual(os.listdir(CORE.th_path("outbox", "pending")), [])


class TestNotificationRouting(NotificationTestCase):
    def event(self, kind="complete"):
        event = CORE._notification_test_event("local")
        event.pop("test_channel")
        event["kind"] = kind
        return event

    def config(self):
        config = CORE.default_notification_config()
        config["webhook"]["enabled"] = True
        config["messages"]["enabled"] = True
        config["messages"]["recipient"] = "+15551234567"
        return config

    def test_home_uses_local_and_webhook_but_not_messages(self):
        channels = CORE.selected_notification_channels(self.config(), self.event(), "home")
        self.assertEqual(channels, ["local", "webhook"])

    def test_away_adds_messages_sms_relay(self):
        channels = CORE.selected_notification_channels(self.config(), self.event(), "away")
        self.assertEqual(channels, ["local", "webhook", "messages"])

    def test_unknown_presence_only_escalates_failures_to_messages(self):
        complete = CORE.selected_notification_channels(self.config(), self.event(), "unknown")
        failed = CORE.selected_notification_channels(
            self.config(), self.event(kind="failed"), "unknown"
        )
        self.assertNotIn("messages", complete)
        self.assertIn("messages", failed)


class TestWebhookDelivery(NotificationTestCase):
    def test_webhook_is_hmac_signed_and_idempotent(self):
        os.environ["TERMINAL_HANDOFF_WEBHOOK_SECRET"] = "test-secret"
        event = CORE._notification_test_event("webhook")
        captured = {}

        def opener(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse(202)

        status, detail = CORE.deliver_webhook_notification(
            event,
            {
                "url": "https://notify.example.test/handoff",
                "secret_env": "TERMINAL_HANDOFF_WEBHOOK_SECRET",
                "timeout_seconds": 7,
            },
            opener=opener,
        )
        self.assertEqual(status, "delivered", detail)
        request = captured["request"]
        timestamp = request.headers["X-terminal-handoff-timestamp"]
        expected = hmac.new(
            b"test-secret",
            timestamp.encode("ascii") + b"." + request.data,
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(request.headers["X-terminal-handoff-signature"], "sha256=" + expected)
        self.assertEqual(request.headers["Idempotency-key"], event["event_id"])
        self.assertEqual(captured["timeout"], 7)

    def test_enabled_webhook_fails_closed_without_https_or_secret(self):
        event = CORE._notification_test_event("webhook")
        status, detail = CORE.deliver_webhook_notification(
            event, {"url": "http://example.test", "secret_env": "TERMINAL_HANDOFF_WEBHOOK_SECRET"}
        )
        self.assertEqual(status, "failed")
        self.assertIn("HTTPS", detail)

    def test_keychain_secret_signs_without_entering_configuration(self):
        event = CORE._notification_test_event("webhook")
        captured = {}

        def opener(request, timeout):
            captured["request"] = request
            return FakeResponse(204)

        config = {
            "url": "https://notify.example.test/handoff",
            "secret_env": "TERMINAL_HANDOFF_WEBHOOK_SECRET",
            "keychain_service": "terminal-handoff-webhook",
            "keychain_account": "terminal-handoff",
        }
        with mock.patch.object(CORE.sys, "platform", "darwin"), mock.patch.object(
            CORE.os.path, "isfile", return_value=True
        ), mock.patch.object(
            CORE.subprocess,
            "run",
            return_value=FakeProcessResult(stdout=b"keychain-secret\n"),
        ) as keychain:
            status, detail = CORE.deliver_webhook_notification(event, config, opener=opener)
        self.assertEqual(status, "delivered", detail)
        keychain.assert_called_once()
        request = captured["request"]
        timestamp = request.headers["X-terminal-handoff-timestamp"]
        expected = hmac.new(
            b"keychain-secret",
            timestamp.encode("ascii") + b"." + request.data,
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(request.headers["X-terminal-handoff-signature"], "sha256=" + expected)
        self.assertNotIn("keychain-secret", json.dumps(config))

    def test_webhook_url_cannot_hide_credentials_in_a_query(self):
        os.environ["TERMINAL_HANDOFF_WEBHOOK_SECRET"] = "test-secret"
        event = CORE._notification_test_event("webhook")
        status, detail = CORE.deliver_webhook_notification(
            event,
            {
                "url": "https://notify.example.test/handoff?token=secret",
                "secret_env": "TERMINAL_HANDOFF_WEBHOOK_SECRET",
            },
        )
        self.assertEqual(status, "failed")
        self.assertIn("query", detail)


class TestOutboxDelivery(NotificationTestCase):
    def test_failure_retries_without_repeating_successful_channels(self):
        config = CORE.default_notification_config()
        config["webhook"]["enabled"] = True
        config["webhook"]["url"] = "https://example.test/handoff"
        config["retry"]["base_seconds"] = 5
        event = CORE._notification_test_event("local")
        event.pop("test_channel")
        path = CORE.enqueue_notification(event, spawn=False)
        calls = []

        def first_attempt(channel, supplied_event, supplied_config):
            calls.append(channel)
            if channel == "webhook":
                return "failed", "offline"
            return "delivered", "ok"

        with mock.patch.object(CORE, "_deliver_notification_channel", side_effect=first_attempt):
            CORE.deliver_pending_notification(path, config=config, now=100.0)
        pending = json_file(path)
        self.assertEqual(pending["deliveries"]["local"]["status"], "delivered")
        self.assertEqual(pending["deliveries"]["webhook"]["status"], "failed")

        def second_attempt(channel, supplied_event, supplied_config):
            calls.append(channel)
            return "delivered", "online"

        with mock.patch.object(CORE, "_deliver_notification_channel", side_effect=second_attempt):
            destination = CORE.deliver_pending_notification(path, config=config, now=106.0)
        self.assertIn(os.path.join("outbox", "delivered"), destination)
        self.assertEqual(calls.count("local"), 1, "a delivered channel was called twice")
        self.assertEqual(calls.count("webhook"), 2)

    def test_cli_status_redacts_webhook_url_and_messages_recipient(self):
        config = CORE.default_notification_config()
        config["webhook"].update(
            {"enabled": True, "url": "https://private.example/hook", "secret_env": "SECRET_NAME"}
        )
        config["messages"].update({"enabled": True, "recipient": "+15551234567"})
        CORE.save_notification_config(config)
        code, out, err = run_th(["notifications", "status"], env=self.env())
        self.assertEqual(code, 0, err)
        self.assertNotIn("private.example", out)
        self.assertNotIn("+15551234567", out)
        self.assertIn('"url_configured": true', out)
        self.assertIn('"recipient_configured": true', out)


class TestMessagesSafety(NotificationTestCase):
    def test_applescript_strings_cannot_break_out_of_the_literal(self):
        hostile = '+1555" & do shell script "touch /tmp/owned" & "'
        literal = CORE._applescript_literal(hostile)
        self.assertTrue(literal.startswith('"') and literal.endswith('"'))
        self.assertIn('\\"', literal)
        self.assertNotIn('" & do shell script "', literal)

    def test_messages_uses_osascript_argv_and_never_a_shell(self):
        event = CORE._notification_test_event("messages")
        recipient = '+1555" & do shell script "false" & "'
        with mock.patch.object(CORE.sys, "platform", "darwin"), mock.patch.object(
            CORE.os.path, "isfile", return_value=True
        ), mock.patch.object(
            CORE.subprocess, "run", return_value=FakeProcessResult()
        ) as runner:
            status, detail = CORE.deliver_messages_notification(
                event, {"recipient": recipient}
            )
        self.assertEqual(status, "delivered", detail)
        argv = runner.call_args.args[0]
        kwargs = runner.call_args.kwargs
        self.assertEqual(argv[:2], ["/usr/bin/osascript", "-e"])
        self.assertIsInstance(argv, list)
        self.assertNotIn("shell", kwargs)
        self.assertIn('\\"', argv[2])
        self.assertNotIn('" & do shell script "', argv[2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
