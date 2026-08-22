#!/usr/bin/python3
# Terminal Handoff managed /handoff skill helper.
"""Invoke the installed manual-handoff runtime without a shell."""

import json
import os
import sys


def main():
    if len(sys.argv) != 2:
        print(json.dumps({
            "ok": False,
            "state": "refused",
            "reason": "Claude Code did not supply exactly one trusted session ID",
        }))
        return 2
    skill_dir = os.path.dirname(os.path.realpath(__file__))
    runtime = os.path.realpath(
        os.path.join(skill_dir, "..", "..", "terminal-handoff", "terminal-handoff.py")
    )
    if not os.path.isfile(runtime):
        print(json.dumps({
            "ok": False,
            "state": "refused",
            "reason": "Terminal Handoff runtime is not installed at %s" % runtime,
        }))
        return 1
    os.execv(
        "/usr/bin/python3",
        [
            "/usr/bin/python3",
            runtime,
            "manual-handoff",
            "--session-id",
            sys.argv[1],
        ],
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
