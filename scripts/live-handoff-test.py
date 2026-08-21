#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Terminal Handoff - controlled live handoff test.

This is not a unit test. It opens real macOS Terminal windows through real
`osascript`, runs the real installed launcher, traces real process ancestry and
sends a real `SIGTERM` to a real process. What it does not do is start a real
Claude session or consume a real context window.

The stand-in for `claude` is a small compiled program named `claude`. That name
matters: Terminal Handoff only ever binds a process whose executable file is
named exactly `claude`. A shell script would report its interpreter to `ps`, and
a copied system binary is killed by the code-signing check, so neither works.
The stand-in parses `--model`, `--effort` and `--name` exactly as the launcher
passes them, then runs a driver that feeds synthetic status-line JSON - in the
official schema - to the real Terminal Handoff status-line command, from a real
child process, so the ancestry Terminal Handoff must trace is genuine.

Everything happens inside a throwaway `CLAUDE_TERMINAL_HANDOFF_HOME`. Your own
Claude sessions, your own settings and your own Terminal windows are untouched.

Usage:
    python3 scripts/live-handoff-test.py            # run, then clean up
    python3 scripts/live-handoff-test.py --keep     # leave the evidence in place
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CORE = os.path.join(ROOT, "src", "terminal_handoff", "core.py")

MODEL_ID = "claude-opus-5[1m]"
EFFORT = "high"
BASE_NAME = "Ranger"

STANDIN_C = r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifndef TH_DRIVER
#define TH_DRIVER ""
#endif
#ifndef TH_CORE
#define TH_CORE ""
#endif
#ifndef TH_EVIDENCE
#define TH_EVIDENCE ""
#endif
#ifndef TH_MODEL_SUFFIX
#define TH_MODEL_SUFFIX ""
#endif

/* A stand-in for the Claude Code CLI, named `claude` so that it presents the
   same process identity Terminal Handoff binds. It parses the flags the
   launcher passes, exports them, and runs the driver as a child process. */
int main(int argc, char **argv) {
    int i;
    for (i = 1; i + 1 < argc; i++) {
        if (strcmp(argv[i], "--model") == 0) setenv("FAKE_CLAUDE_MODEL", argv[i + 1], 1);
        else if (strcmp(argv[i], "--effort") == 0) setenv("FAKE_CLAUDE_EFFORT", argv[i + 1], 1);
        else if (strcmp(argv[i], "--name") == 0) setenv("FAKE_CLAUDE_NAME", argv[i + 1], 1);
    }
    if (argc > 1) setenv("FAKE_CLAUDE_PROMPT", argv[argc - 1], 1);
    setenv("FAKE_CLAUDE_CORE", TH_CORE, 1);
    setenv("FAKE_CLAUDE_EVIDENCE", TH_EVIDENCE, 1);
    if (strlen(TH_MODEL_SUFFIX) > 0) setenv("FAKE_CLAUDE_MODEL_SUFFIX", TH_MODEL_SUFFIX, 1);
    printf("stand-in claude: name=%s model=%s\n",
           getenv("FAKE_CLAUDE_NAME") ? getenv("FAKE_CLAUDE_NAME") : "(none)",
           getenv("FAKE_CLAUDE_MODEL") ? getenv("FAKE_CLAUDE_MODEL") : "(none)");
    fflush(stdout);
    if (strlen(TH_DRIVER) == 0) { fprintf(stderr, "no driver compiled in\n"); return 2; }
    return system(TH_DRIVER) == 0 ? 0 : 1;
}
'''

DRIVER_PY = r'''#!/usr/bin/env python3
"""Drives one stand-in Claude session: emits official-schema status JSON."""
import json
import os
import subprocess
import sys
import time
import uuid

name = os.environ.get("FAKE_CLAUDE_NAME") or "unnamed"
model = os.environ.get("FAKE_CLAUDE_MODEL")
effort = os.environ.get("FAKE_CLAUDE_EFFORT")
core = os.environ["FAKE_CLAUDE_CORE"]
evidence = os.environ["FAKE_CLAUDE_EVIDENCE"]
# A suffix makes every generation report a model that differs from the one it
# was launched with, which is what a genuinely mis-started successor looks like.
reported_model = (model or "") + os.environ.get("FAKE_CLAUDE_MODEL_SUFFIX", "")
low_beats = int(os.environ.get("FAKE_CLAUDE_LOW_BEATS", "4"))
low = float(os.environ.get("FAKE_CLAUDE_LOW", "10"))
high = float(os.environ.get("FAKE_CLAUDE_HIGH", "85"))
interval = float(os.environ.get("FAKE_CLAUDE_INTERVAL", "2"))
limit = int(os.environ.get("FAKE_CLAUDE_MAX_BEATS", "600"))

session_id = str(uuid.uuid4())
cwd = os.getcwd()
transcripts = os.path.join(evidence, "transcripts")
os.makedirs(transcripts, exist_ok=True)
transcript = os.path.join(transcripts, session_id + ".jsonl")
with open(transcript, "w") as handle:
    handle.write(json.dumps({"type": "user", "sessionId": session_id,
                             "message": {"role": "user", "content": "controlled live test"}}) + "\n")

claude_pid = os.getppid()
record = {
    "session_id": session_id,
    "display_name": name,
    "model_launched_with": model,
    "model_reported": reported_model,
    "effort": effort,
    "cwd": cwd,
    "claude_pid": claude_pid,
    "chain_id": os.environ.get("CLAUDE_TERMINAL_HANDOFF_CHAIN_ID"),
    "generation": os.environ.get("CLAUDE_TERMINAL_HANDOFF_GENERATION"),
    "base_name_env": os.environ.get("CLAUDE_TERMINAL_HANDOFF_BASE_NAME"),
    "transfer": os.environ.get("CLAUDE_TERMINAL_HANDOFF_TRANSFER"),
    "started": time.time(),
}
os.makedirs(evidence, exist_ok=True)
with open(os.path.join(evidence, "session-%s.json" % session_id), "w") as handle:
    json.dump(record, handle, indent=2, sort_keys=True)

beats = 0
while beats < limit:
    if os.getppid() != claude_pid:
        break  # the stand-in claude process has gone; stop with it
    percent = low if beats < low_beats else high
    payload = {
        "version": "2.1.238",
        "session_id": session_id,
        "session_name": name,
        "transcript_path": transcript,
        "cwd": cwd,
        "workspace": {"current_dir": cwd, "project_dir": cwd, "added_dirs": []},
        "model": {"id": reported_model, "display_name": "Stand-in"},
        "output_style": {"name": "default"},
        "context_window": {"context_window_size": 200000, "used_percentage": percent,
                           "remaining_percentage": 100 - percent},
    }
    if effort:
        payload["effort"] = {"level": effort}
    try:
        subprocess.run([sys.executable, core, "statusline"],
                       input=json.dumps(payload).encode("utf-8"),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    except Exception:
        pass
    beats += 1
    time.sleep(interval)
'''


def run(argv, **kwargs):
    kwargs.setdefault("stdout", subprocess.PIPE)
    kwargs.setdefault("stderr", subprocess.PIPE)
    return subprocess.run(argv, **kwargs)


def osascript(script):
    proc = run(["/usr/bin/osascript", "-"], input=script.encode("utf-8"))
    return proc.returncode, proc.stdout.decode("utf-8", "replace").strip(), proc.stderr.decode(
        "utf-8", "replace"
    ).strip()


def ps_field(pid, fmt):
    proc = run(["/bin/ps", "-o", fmt, "-p", str(pid)])
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace").strip() or None


def alive(pid):
    state = ps_field(pid, "stat=")
    return bool(state) and not state.startswith("Z")


def compiler():
    for candidate in ("/usr/bin/cc", "/usr/bin/clang"):
        if os.path.exists(candidate):
            return candidate
    return shutil.which("cc") or shutil.which("clang")


class LiveTest(object):
    def __init__(self, core_path, keep=False):
        self.core = core_path
        self.keep = keep
        self.root = tempfile.mkdtemp(prefix="th-live-")
        self.home = os.path.join(self.root, "th-home")
        self.work = os.path.join(self.root, "work")
        self.evidence = os.path.join(self.root, "evidence")
        self.bin = os.path.join(self.root, "bin")
        for path in (self.home, self.work, self.evidence, self.bin):
            os.makedirs(path, exist_ok=True)
        self.driver = os.path.join(self.root, "driver.py")
        self.windows = []
        self.homes = set()
        self.expected_titles = set()
        self.results = []
        self.evidence_log = {"checks": [], "root": self.root}

    # -- reporting --------------------------------------------------------

    def check(self, ok, label, detail=None):
        self.results.append(bool(ok))
        self.evidence_log["checks"].append(
            {"ok": bool(ok), "check": label, "detail": detail}
        )
        print("  %s  %s" % ("ok  " if ok else "FAIL", label))
        if detail is not None:
            print("        %s" % (detail,))
        return bool(ok)

    # -- setup ------------------------------------------------------------

    def build(self, name="claude", model_suffix=None, subdir="bin"):
        cc = compiler()
        if cc is None:
            raise SystemExit("no C compiler found; cannot build a process stand-in")
        directory = os.path.join(self.root, subdir)
        os.makedirs(directory, exist_ok=True)
        source = os.path.join(directory, "standin.c")
        with open(source, "w") as handle:
            handle.write(STANDIN_C)
        binary = os.path.join(directory, name)
        argv = [
            cc, "-O0", "-o", binary,
            '-DTH_DRIVER="%s %s"' % (sys.executable, self.driver),
            '-DTH_CORE="%s"' % self.core,
            '-DTH_EVIDENCE="%s"' % self.evidence,
        ]
        if model_suffix:
            argv.append('-DTH_MODEL_SUFFIX="%s"' % model_suffix)
        argv.append(source)
        proc = run(argv)
        if proc.returncode != 0 or not os.path.exists(binary):
            raise SystemExit("could not compile the stand-in:\n%s" % proc.stderr.decode())
        return binary

    def write_driver(self):
        with open(self.driver, "w") as handle:
            handle.write(DRIVER_PY)
        os.chmod(self.driver, 0o700)

    def env_exports(self, extra=None):
        values = {
            "CLAUDE_TERMINAL_HANDOFF_HOME": self.home,
            "CLAUDE_TERMINAL_HANDOFF_THRESHOLD": "80",
            "CLAUDE_TERMINAL_HANDOFF_MIN_OBSERVATIONS": "2",
            "CLAUDE_TERMINAL_HANDOFF_COOLDOWN": "3",
            "CLAUDE_TERMINAL_HANDOFF_STORM_MAX": "20",
            "CLAUDE_TERMINAL_HANDOFF_STORM_WINDOW": "600",
            "CLAUDE_TERMINAL_HANDOFF_MAX_GENERATIONS": "3",
            "CLAUDE_TERMINAL_HANDOFF_HEARTBEAT_TIMEOUT": "90",
            "CLAUDE_TERMINAL_HANDOFF_STOP_GRACE": "15",
            "CLAUDE_TERMINAL_HANDOFF_TRANSFER_POLL": "1",
        }
        values.update(extra or {})
        return values

    def open_generation_one(self, claude_bin, workdir, name=BASE_NAME, extra_env=None):
        """Open a real Terminal window running the generation-1 stand-in."""
        import shlex

        script = os.path.join(self.root, "gen1-%s.sh" % uuid.uuid4().hex[:6])
        env = self.env_exports(extra_env)
        env["CLAUDE_TERMINAL_HANDOFF_CLAUDE_BIN"] = claude_bin
        lines = ["#!/bin/zsh", "cd -- %s" % shlex.quote(workdir)]
        for key, value in sorted(env.items()):
            lines.append("export %s=%s" % (key, shlex.quote(value)))
        lines.append(
            "exec %s --model %s --effort %s --name %s %s"
            % (
                shlex.quote(claude_bin),
                shlex.quote(MODEL_ID),
                shlex.quote(EFFORT),
                shlex.quote(name),
                shlex.quote("controlled live test bootstrap"),
            )
        )
        lines.append("")
        with open(script, "w") as handle:
            handle.write("\n".join(lines))
        os.chmod(script, 0o700)
        code, out, err = osascript(
            "\n".join(
                [
                    'tell application "Terminal"',
                    "    activate",
                    '    do script "/bin/zsh -l %s"' % script,
                    "    set custom title of front window to %s" % json.dumps(name),
                    "    return id of front window as text",
                    "end tell",
                ]
            )
        )
        if code != 0:
            raise SystemExit("could not open a Terminal window: %s" % err)
        self.windows.append(out)
        self.expected_titles.add(name)
        self.homes.add(env["CLAUDE_TERMINAL_HANDOFF_HOME"])
        return out

    # -- polling ----------------------------------------------------------

    def wait_for(self, predicate, timeout, label):
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.5)
        return None

    def launch_records(self, home=None):
        directory = os.path.join(home or self.home, "completed")
        rows = []
        if not os.path.isdir(directory):
            return rows
        for name in sorted(os.listdir(directory)):
            if name.endswith(".launch.json"):
                try:
                    with open(os.path.join(directory, name)) as handle:
                        rows.append(json.load(handle))
                except Exception:
                    pass
        return sorted(rows, key=lambda row: row.get("generation") or 0)

    def sessions(self):
        rows = []
        if not os.path.isdir(self.evidence):
            return rows
        for name in sorted(os.listdir(self.evidence)):
            if name.startswith("session-") and name.endswith(".json"):
                try:
                    with open(os.path.join(self.evidence, name)) as handle:
                        rows.append(json.load(handle))
                except Exception:
                    pass
        return sorted(rows, key=lambda row: row.get("started") or 0)

    def transfers(self):
        directory = os.path.join(self.home, "transfers")
        rows = {}
        if not os.path.isdir(directory):
            return rows
        for name in sorted(os.listdir(directory)):
            if name.endswith(".json"):
                try:
                    with open(os.path.join(directory, name)) as handle:
                        record = json.load(handle)
                    rows[record.get("parent_session_id")] = record
                except Exception:
                    pass
        return rows

    # -- cleanup ----------------------------------------------------------

    def stop_everything(self):
        import signal as signal_module

        for session in self.sessions():
            pid = session.get("claude_pid")
            if pid and alive(pid):
                try:
                    os.kill(int(pid), signal_module.SIGTERM)
                except OSError:
                    pass
        time.sleep(1.5)
        for window in self.windows:
            osascript(
                'tell application "Terminal" to close (every window whose id is %s) saving no'
                % window
            )
        # The launcher opened the successor windows, so their ids were never
        # ours to record. Close them by the exact titles this run created, and
        # only when the window is not busy, so a real Claude session that
        # happens to share a title is never touched.
        titles = set(self.expected_titles)
        for record in self.launch_records():
            if record.get("title"):
                titles.add(record["title"])
        for home in self.homes:
            for record in self.launch_records(home):
                if record.get("title"):
                    titles.add(record["title"])
        if titles:
            condition = " or ".join('t is %s' % json.dumps(title) for title in sorted(titles))
            osascript(
                "\n".join(
                    [
                        'tell application "Terminal"',
                        "    repeat with w in (windows as list)",
                        "        try",
                        "            set t to (custom title of w) as text",
                        "            if (%s) and (busy of w is false) then close w saving no"
                        % condition,
                        "        end try",
                        "    end repeat",
                        "end tell",
                    ]
                )
            )

    def cleanup(self):
        self.stop_everything()
        if self.keep:
            print("\nEvidence kept in %s" % self.root)
            return
        shutil.rmtree(self.root, ignore_errors=True)


def scenario_chain(test):
    """Ranger -> Ranger 2 -> Ranger 3, with the parent stopped each time."""
    print("\nScenario 1: a three-generation chain with verified parent shutdown")
    claude_bin = test.build()
    unrelated_bin = test.build(subdir="unrelated-bin")
    unrelated_dir = os.path.join(test.root, "unrelated-work")
    os.makedirs(unrelated_dir, exist_ok=True)

    # An unrelated stand-in Claude session that must never be touched.
    unrelated = subprocess.Popen(
        [unrelated_bin, "--model", MODEL_ID, "--effort", EFFORT, "--name", "Unrelated"],
        cwd=unrelated_dir,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=dict(
            os.environ,
            FAKE_CLAUDE_LOW_BEATS="100000",
            CLAUDE_TERMINAL_HANDOFF_DISABLED="1",
            CLAUDE_TERMINAL_HANDOFF_HOME=os.path.join(test.root, "unrelated-home"),
        ),
    )
    time.sleep(1.0)
    test.check(alive(unrelated.pid), "an unrelated stand-in Claude session is running",
               "pid %d" % unrelated.pid)

    test.open_generation_one(claude_bin, test.work)

    records = test.wait_for(
        lambda: (test.launch_records() if len(test.launch_records()) >= 2 else None), 180,
        "two handoffs",
    )
    if not records:
        records = test.launch_records()

    names = [row["argv"][row["argv"].index("--name") + 1] for row in records]
    test.check(len(records) >= 1, "generation 1 handed off", "launch records: %d" % len(records))
    if records:
        test.check(names[0] == "Ranger 2", "Ranger hands off to Ranger 2", "--name %r" % names[0])
        test.check(records[0]["title"] == "Ranger 2",
                   "the Terminal window title is the display name",
                   records[0]["title"])
        test.check(records[0]["base_name_source"] == "session_name",
                   "the base name came from the live Claude session name")
        test.check("-g2" not in names[0] and records[0]["chain_id"] not in names[0],
                   "no internal chain identifier is exposed in the name")
    if len(records) >= 2:
        test.check(names[1] == "Ranger 3", "Ranger 2 hands off to Ranger 3", "--name %r" % names[1])
        test.check(records[1]["base_name_source"] == "chain_state",
                   "generation 2's base name came from trusted chain state")

    sessions = test.sessions()
    by_name = dict((row["display_name"], row) for row in sessions)
    test.check("Ranger 2" in by_name, "a session named 'Ranger 2' actually started")
    if "Ranger 2" in by_name:
        successor = by_name["Ranger 2"]
        test.check(successor["model_launched_with"] == MODEL_ID,
                   "the successor received the exact model", successor["model_launched_with"])
        test.check(successor["effort"] == EFFORT,
                   "the successor received the exact effort", successor["effort"])
        test.check(os.path.realpath(successor["cwd"]) == os.path.realpath(test.work),
                   "the successor started in the correct working directory")
        test.check(successor["generation"] == "2", "the successor knows its generation")
    third = test.wait_for(
        lambda: any(row["display_name"] == "Ranger 3" for row in test.sessions()), 90,
        "generation 3",
    )
    test.check(bool(third), "a session named 'Ranger 3' actually started")
    by_name = dict((row["display_name"], row) for row in test.sessions())

    # The parent must have been stopped, and only the parent.
    parent = by_name.get("Ranger")
    transfers = test.transfers()
    if parent:
        record = transfers.get(parent["session_id"])
        test.check(record is not None, "a transfer record exists for generation 1")
        if record:
            test.check(record["state"] == "TRANSFER_COMPLETE",
                       "the transfer completed", record["state"])
            test.check(record.get("parent_stopped") is True, "the parent was stopped")
            test.check(record["stop"]["signal"] == "SIGTERM" and not record["stop"]["escalates"],
                       "the parent was stopped with SIGTERM and no escalation",
                       json.dumps(record["stop"]))
            test.check(record["owner"] == "successor",
                       "continuation ownership moved to the successor")
            states = [item["state"] for item in record["history"]]
            test.check(
                states == ["LAUNCHING", "SUCCESSOR_VERIFIED", "PARENT_STOP_REQUESTED",
                           "TRANSFER_COMPLETE"],
                "the transfer followed the documented state machine", " -> ".join(states))
            checks = (record.get("successor") or {}).get("checks") or {}
            test.check(checks and all(checks.values()),
                       "every successor heartbeat check passed",
                       ", ".join(sorted(checks)))
        gone = test.wait_for(lambda: not alive(parent["claude_pid"]), 60, "parent exit")
        test.check(bool(gone), "the generation-1 Claude process is no longer running",
                   "pid %d" % parent["claude_pid"])
        # The Terminal window's own shell must have survived.
        tty = None
        binding = (record or {}).get("parent_process") or {}
        tty = binding.get("tty")
        if tty and tty != "??":
            shells = run(["/bin/ps", "-t", tty, "-o", "pid=,comm="]).stdout.decode(
                "utf-8", "replace"
            ).strip()
            test.check(bool(shells),
                       "the parent's Terminal still has a live shell on %s" % tty,
                       shells.replace("\n", " | ")[:200])

    test.check(alive(unrelated.pid), "the unrelated Claude session was never touched",
               "pid %d" % unrelated.pid)
    windows = osascript('tell application "Terminal" to return count of windows')[1]
    test.check(bool(windows), "Terminal windows are still open", "count: %s" % windows)

    # The chain must stop at the configured ceiling, not run away.
    time.sleep(6)
    test.check(len(test.launch_records()) <= 2,
               "the generation ceiling stopped the chain",
               "launch records: %d" % len(test.launch_records()))
    try:
        unrelated.terminate()
    except Exception:
        pass


def scenario_invalid_heartbeat(test):
    """A successor reporting the wrong model must leave the parent running."""
    print("\nScenario 2: an invalid successor heartbeat leaves the parent running")
    claude_bin = test.build(model_suffix="-wrong", subdir="bad-bin")
    work = os.path.join(test.root, "bad-work")
    os.makedirs(work, exist_ok=True)
    home = os.path.join(test.root, "bad-home")
    os.makedirs(home, exist_ok=True)
    previous_home = test.home
    test.home = home
    try:
        test.open_generation_one(
            claude_bin, work,
            extra_env={"CLAUDE_TERMINAL_HANDOFF_HOME": home,
                       "CLAUDE_TERMINAL_HANDOFF_HEARTBEAT_TIMEOUT": "25"},
        )
        records = test.wait_for(lambda: test.launch_records() or None, 120, "one handoff")
        test.check(bool(records), "the handoff launched a successor")
        transfers = test.wait_for(
            lambda: (test.transfers() if any(
                row.get("state") == "TRANSFER_FAILED" for row in test.transfers().values()
            ) else None),
            120, "a failed transfer",
        ) or test.transfers()
        failed = [row for row in transfers.values() if row.get("state") == "TRANSFER_FAILED"]
        test.check(bool(failed), "the transfer failed closed",
                   failed[0]["history"][-1]["reason"] if failed else "no failed transfer")
        if failed:
            test.check(failed[0].get("parent_stopped") in (None, False),
                       "the parent was never stopped")
            rejected = failed[0].get("successor_rejected") or {}
            test.check("model_matches" in (rejected.get("failed_checks") or []),
                       "the wrong model was the recorded reason",
                       json.dumps(rejected.get("failed_checks")))
        parents = [row for row in test.sessions() if row["display_name"] == BASE_NAME
                   and os.path.realpath(row["cwd"]) == os.path.realpath(work)]
        if parents:
            test.check(alive(parents[0]["claude_pid"]),
                       "the parent Claude process is still running",
                       "pid %d" % parents[0]["claude_pid"])
    finally:
        test.home = previous_home


def main():
    parser = argparse.ArgumentParser(description="Terminal Handoff controlled live handoff test")
    parser.add_argument("--core", default=DEFAULT_CORE, help="terminal-handoff core to exercise")
    parser.add_argument("--keep", action="store_true", help="keep the throwaway state directory")
    parser.add_argument("--evidence", default=None, help="write the evidence JSON here")
    args = parser.parse_args()

    if sys.platform != "darwin":
        raise SystemExit("this test drives Apple Terminal and requires macOS")
    core_path = os.path.abspath(os.path.expanduser(args.core))
    if not os.path.isfile(core_path):
        raise SystemExit("no such core: %s" % core_path)

    print("Terminal Handoff - controlled live handoff test")
    print("==============================================")
    print("core:     %s" % core_path)
    version = run([sys.executable, core_path, "version"]).stdout.decode().strip()
    print("version:  %s" % version)

    test = LiveTest(core_path, keep=args.keep)
    print("state:    %s" % test.root)
    test.write_driver()
    test.evidence_log["core"] = core_path
    test.evidence_log["version"] = version
    try:
        scenario_chain(test)
        scenario_invalid_heartbeat(test)
    finally:
        passed = sum(1 for item in test.results if item)
        total = len(test.results)
        test.evidence_log["passed"] = passed
        test.evidence_log["total"] = total
        if args.evidence:
            with open(os.path.expanduser(args.evidence), "w") as handle:
                json.dump(test.evidence_log, handle, indent=2, sort_keys=True)
            print("\nEvidence written to %s" % args.evidence)
        test.cleanup()
        print("\n%d/%d checks passed" % (passed, total))
    return 0 if total and passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
