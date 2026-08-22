# Notifications

Terminal Handoff 1.2 adds deterministic alerts for every terminal handoff
result. A transfer is committed first; only then is a privacy-minimised event
written to a durable outbox. Delivery never blocks the status line or changes
who owns the work.

## Quick start

The installer enables local macOS notifications by default:

```sh
TH="$HOME/.claude/terminal-handoff/terminal-handoff.py"
python3 "$TH" notifications status
python3 "$TH" notifications test --channel local
```

The test should produce a Notification Center alert. If macOS asks whether the
controlling application may send notifications, allow it.

Successful chains read naturally:

```text
Handoff complete: Ranger → Ranger 2. Ranger 2 now owns the work.
Handoff complete: Ranger 2 → Ranger 3. Ranger 3 now owns the work.
```

A failure says which session remains owner and why.

## Delivery model

| Channel | Default | Intended use |
|---|---:|---|
| macOS Notification Center | on | immediate alert while at the Mac |
| signed HTTPS webhook | off | presence-aware push, SMS, Slack, Signal or another gateway |
| Messages on macOS | off | direct iMessage, or SMS/RCS through iPhone Text Message Forwarding |

Outbox files live under
`~/.claude/terminal-handoff/outbox/{pending,delivered,dead}` with mode `0600`.
Successful channels are not called again when another channel retries. Failed
channels use bounded exponential backoff. A status-line refresh restarts a
missing worker and reconciles committed 1.2 terminal states with missing events,
so a crash between commit and enqueue also recovers without holding up a
handoff. Historical pre-1.2 transfers are never replayed automatically.

Delivery is **at least once**, not exactly once. Webhook consumers must dedupe
on `Idempotency-Key` or `event_id`. A provider accepting a request proves only
provider acceptance, not that a person read the message.

## Presence

Terminal Handoff deliberately does not infer location from Wi-Fi, GPS or device
activity. Presence is an explicit three-state input:

```sh
python3 "$TH" notifications presence --presence home
python3 "$TH" notifications presence --presence away
python3 "$TH" notifications presence --presence unknown
```

An existing home-automation or presence service can atomically update
`~/.claude/terminal-handoff/notifications/presence.json`, or set
`TERMINAL_HANDOFF_PRESENCE=home|away|unknown` before starting Claude Code.

The default Messages policy, `away_or_critical`, sends when presence is `away`.
It also sends a failure when presence is `unknown`, because losing a critical
failure is worse than one extra alert. It does not send Messages while presence
is explicitly `home`; the local alert remains active.

## Messages, iMessage and SMS relay

Configure a phone number or Apple Account address:

```sh
python3 "$TH" notifications configure \
  --enable-messages \
  --messages-recipient "+15551234567" \
  --messages-when away_or_critical

python3 "$TH" notifications presence --presence away
python3 "$TH" notifications test --channel messages
```

The first send may prompt for Automation permission to control Messages. Grant
only if you want this channel. Carrier SMS/RCS requires an iPhone signed into
the same Apple Account with Text Message Forwarding enabled for the Mac. Apple
documents that setup in [Forward text messages from your iPhone to other
devices](https://support.apple.com/en-us/102545).

Messages automation reports that Messages accepted the send request. It cannot
prove carrier delivery or that the recipient read it.

Disable the channel at any time:

```sh
python3 "$TH" notifications configure --disable-messages
```

## Signed webhook for push, Messenger or an SMS gateway

The webhook is the recommended boundary for a presence-aware external system:
Terminal Handoff reports a generic event, and your private gateway chooses
web push, SMS, Slack, Signal, Messenger or another provider. The public project
does not contain provider credentials or provider-specific routing.

Store the signing secret in macOS Keychain so every generation can use it
without placing it in a launch script or configuration file:

```sh
security add-generic-password \
  -a terminal-handoff \
  -s terminal-handoff-webhook \
  -w 'replace-with-a-long-random-secret' \
  -U

python3 "$TH" notifications configure \
  --enable-webhook \
  --webhook-url "https://notify.example.com/terminal-handoff" \
  --webhook-keychain-service terminal-handoff-webhook \
  --webhook-keychain-account terminal-handoff

python3 "$TH" notifications test --channel webhook
```

For an ephemeral test, export the same secret under the configured environment
variable instead:

```sh
export TERMINAL_HANDOFF_WEBHOOK_SECRET='replace-with-the-same-secret'
```

This variable is intentionally **not** copied into generated successor scripts.
Use Keychain for a multi-generation chain.

### Request contract

The worker sends canonical JSON with:

| Header | Value |
|---|---|
| `Idempotency-Key` | deterministic `event_id` |
| `X-Terminal-Handoff-Event` | `terminal_handoff.complete`, `.failed` or `.test` |
| `X-Terminal-Handoff-Timestamp` | Unix seconds |
| `X-Terminal-Handoff-Signature` | `sha256=<HMAC-SHA256(secret, timestamp + "." + body)>` |

See [examples/webhook-event.json](../examples/webhook-event.json) and
[examples/notifications.json](../examples/notifications.json) for complete
provider-neutral examples.

Reject stale timestamps before checking the signature, compare the HMAC in
constant time, and dedupe the event ID before routing it. The event contains
display names, generations, chain ID, ownership, urgency, presence and the
human message. `routing_hint` distinguishes `sms_when_away` from the critical
`sms_when_away_or_unknown` policy. It never contains transcript contents or paths, prompts,
repository paths, environment dumps, credentials or the webhook secret.

Suggested routing:

| Event | Home | Away | Presence unknown |
|---|---|---|---|
| complete | local | push; optional SMS | push |
| failed | local | push + SMS | push + SMS |

## Operations

```sh
python3 "$TH" notifications status
python3 "$TH" notifications drain
python3 "$TH" notifications retry
python3 "$TH" notifications retry --event-id <32-hex-event-id>
```

`status` redacts the webhook URL and Messages recipient. `retry` moves dead
events back to pending without repeating channels already marked delivered.

To disable all delivery without deleting history, edit
`~/.claude/terminal-handoff/notifications.json` and set `"enabled": false`, or
set `CLAUDE_TERMINAL_HANDOFF_DISABLE_NOTIFICATIONS=1` before starting Claude
Code.
