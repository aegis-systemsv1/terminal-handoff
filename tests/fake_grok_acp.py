#!/usr/bin/env python3
"""A stand-in for `grok agent stdio`: the Agent Client Protocol over JSON lines, scriptable.

It never talks to a network or a model. Tests steer it with markers in the prompt text and with
FAKE_GROK_* environment variables, and read what it saw from FAKE_GROK_DIR/calls.jsonl.

Prompt markers:  SLOW (works until cancelled)   PERM (asks permission)   THOUGHT (thoughts + a tool call)
                 DIE (exits mid-turn, once)     MALFORMED (garbage lines first)   ERR402 (payment error)   FLOOD (40 garbage lines)
"""

import json
import os
import queue
import sys
import threading
import uuid

STATE = os.environ["FAKE_GROK_DIR"]
CALLS = os.path.join(STATE, "calls.jsonl")
GROK_HOME = os.environ.get("GROK_HOME") or os.path.expanduser("~/.grok")
CANCELLED = False


def log(kind, **fields):
    fields["kind"] = kind
    with open(CALLS, "a") as handle:
        handle.write(json.dumps(fields) + "\n")


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def update(session_id, payload):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": session_id, "update": payload}})


def chunk(session_id, text):
    update(session_id, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}})


INBOX = queue.Queue()


def _pump():
    for line in sys.stdin:
        INBOX.put(line)
    INBOX.put(None)


def read(timeout=None):
    try:
        line = INBOX.get(timeout=timeout)
    except queue.Empty:
        return None
    if line is None:
        os._exit(0)
    return json.loads(line)


def session_dir(session_id):
    return os.path.join(GROK_HOME, "sessions", "fake-cwd", session_id)


def prompt_turn(request_id, session_id, text):
    global CANCELLED
    CANCELLED = False
    log("prompt", session=session_id, chars=len(text), text=text)
    if "MALFORMED" in text:
        sys.stdout.write("this is not json\n[1, 2, 3]\n")
        sys.stdout.flush()
    if "FLOOD" in text:
        sys.stdout.write("garbage\n" * 40)
        sys.stdout.flush()
    if "ERR402" in text:
        send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": "Internal error",
                                                              "data": {"message": "API error (status 402 Payment Required): balance exhausted", "http_status": 402}}})
        return
    if "DIE" in text:
        marker = os.path.join(STATE, "died-once")
        if not os.path.exists(marker):
            open(marker, "w").close()
            chunk(session_id, "about to die")
            os._exit(3)
    if "THOUGHT" in text:
        update(session_id, {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "PRIVATE-REASONING-SECRET"}})
        update(session_id, {"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "Read README.md", "kind": "read", "status": "pending",
                            "rawInput": {"command": "echo TOPSECRET-RAW-INPUT"}})
        update(session_id, {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed",
                            "content": [{"type": "content", "content": {"type": "text", "text": "TOPSECRET-TOOL-OUTPUT"}}]})
    if "PERM" in text:
        send({"jsonrpc": "2.0", "id": 900, "method": "session/request_permission", "params": {
            "sessionId": session_id,
            "toolCall": {"toolCallId": "t2", "title": "Write probe.txt", "kind": "edit", "status": "pending", "rawInput": {"content": "TOPSECRET-EDIT"}},
            "options": [{"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                        {"optionId": "allow-always", "name": "Allow always", "kind": "allow_always"},
                        {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"}]}})
        while True:
            message = read()
            if message.get("id") == 900:
                outcome = (message.get("result") or {}).get("outcome") or {}
                log("permission_reply", outcome=outcome)
                chunk(session_id, "permission:%s" % (outcome.get("optionId") or outcome.get("outcome")))
                break
            if message.get("method") == "session/cancel":
                CANCELLED = True
                log("cancel", session=session_id)
    if "STUBBORN" in text:  # ignores session/cancel entirely
        chunk(session_id, "stubborn work")
        while True:
            message = read(timeout=0.2)
            if message and message.get("method") == "session/cancel":
                log("cancel", session=session_id)
    if "SLOW" in text:
        chunk(session_id, "working on it")
        while not CANCELLED:
            message = read(timeout=0.2)
            if message and message.get("method") == "session/cancel":
                CANCELLED = True
                log("cancel", session=session_id)
        send({"jsonrpc": "2.0", "id": request_id, "result": {"stopReason": "cancelled"}})
        return
    chunk(session_id, "ok: %s\nsecond line" % text[:24].replace("\n", " "))
    send({"jsonrpc": "2.0", "id": request_id, "result": {"stopReason": "end_turn"}})


def main():
    threading.Thread(target=_pump, daemon=True).start()
    if "--version" in sys.argv or "version" in sys.argv[1:2]:
        print("grok 1.0.0-fake")
        return
    log("start", argv=sys.argv[1:], permission_env=os.environ.get("GROK_DEFAULT_PERMISSION_MODE"),
        has_launch_token=bool(os.environ.get("CLAUDE_TERMINAL_HANDOFF_LAUNCH_TOKEN")), sandbox=os.environ.get("GROK_SANDBOX"), cwd=os.getcwd())
    while True:
        message = read()
        method = message.get("method")
        rid = message.get("id")
        params = message.get("params") or {}
        if method != "session/cancel":
            log("rpc", method=method, params={k: v for k, v in params.items() if k not in ("prompt",)})
        if method == "initialize":
            if os.environ.get("FAKE_GROK_INIT_ERROR"):
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "init failed"}})
            elif os.environ.get("FAKE_GROK_INIT_GARBAGE"):
                send({"jsonrpc": "2.0", "id": rid, "result": "not-an-object"})
            else:
                send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": 1, "agentCapabilities": {"loadSession": not os.environ.get("FAKE_GROK_NO_LOAD")}}})
        elif method == "session/new":
            if os.environ.get("FAKE_GROK_NEW_ERROR") == "auth":
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "Authentication required: not authenticated"}})
                continue
            if os.environ.get("FAKE_GROK_NEW_ERROR") == "boom":
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": "Internal error"}})
                continue
            session_id = "fakegrok-" + uuid.uuid4().hex[:16]
            os.makedirs(session_dir(session_id))
            open(os.path.join(session_dir(session_id), "info.json"), "w").write(json.dumps({"cwd": params.get("cwd")}))
            send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": session_id, "configOptions": []}})
        elif method == "session/load":
            session_id = params.get("sessionId")
            if os.environ.get("FAKE_GROK_LOAD_ERROR") or not os.path.isdir(session_dir(str(session_id))):
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32002, "message": "Session not found"}})
                continue
            chunk(session_id, "REPLAYED-HISTORY-MUST-NOT-APPEAR")  # a real agent replays history on load
            send({"jsonrpc": "2.0", "id": rid, "result": {"configOptions": []}})
        elif method == "session/prompt":
            text = "".join(block.get("text", "") for block in params.get("prompt") or [])
            prompt_turn(rid, params.get("sessionId"), text)
        elif rid is not None:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "unknown method"}})


if __name__ == "__main__":
    main()
