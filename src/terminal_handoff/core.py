#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Terminal Handoff - core implementation.

Terminal Handoff monitors an active Claude Code session using Claude Code's
official live status-line JSON. When the session's context window reaches the
configured threshold (default 80%), Terminal Handoff opens a new macOS Terminal
window running a genuinely fresh Claude Code session that uses the outgoing
session's exact model and exact effort level, hands it a secure handoff
manifest, and instructs it to reconstruct the work through a context-isolated
subagent rather than by loading the parent transcript into its main context.

Each Claude session may trigger exactly one successor. Every successor is itself
monitored by Terminal Handoff and may trigger the next generation, so the chain
continues indefinitely until explicitly disabled.

Target: Python 3.9+ (macOS system python3). No third-party dependencies.
"""

from __future__ import print_function

import errno
import fcntl
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

TERMINAL_HANDOFF_VERSION = "1.1.1"
MANIFEST_SCHEMA_VERSION = 2

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_THRESHOLD = 80.0
DEFAULT_MIN_OBSERVATIONS = 2
DEFAULT_COOLDOWN_SECONDS = 45
DEFAULT_STORM_MAX_LAUNCHES = 3
DEFAULT_STORM_WINDOW_SECONDS = 600
DEFAULT_CIRCUIT_OPEN_SECONDS = 1800
WRAPPED_STATUSLINE_TIMEOUT = 3.0
GIT_TIMEOUT = 5.0

# Effort levels accepted by Claude Code 2.1.x, as reported by `.effort.level`
# in the official status-line JSON and validated by `claude --effort`.
ALLOWED_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

# `--effort ultracode` is accepted by the CLI but is NOT an effort level: it
# resolves to xhigh plus a separate internal `ultracode` boolean that is never
# exposed in the status-line JSON. Terminal Handoff therefore cannot detect or
# preserve ultracode. See README.md, "Known limitations".
UNDETECTABLE_EFFORT_ALIASES = ("ultracode",)

# Model IDs are untrusted input. Brackets are required: real model IDs such as
# `claude-opus-5[1m]` contain them. Shell metacharacters are not permitted.
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._:@/\[\]-]{1,128}$")
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,128}$")
CHAIN_ID_RE = re.compile(r"^[A-Za-z0-9]{6,32}$")

# Paths that will be embedded in a generated shell script / AppleScript string.
UNSAFE_PATH_RE = re.compile(r'["\\\x00-\x1f\x7f]')

# Terminal Handoff recognises its own status-line command by this marker rather
# than by a filename. Detecting on a literal filename meant that installing from
# a differently-named module (e.g. the packaged `terminal_handoff/core.py`) made
# the installer fail to recognise itself and wrap its own command recursively.
TH_COMMAND_MARKER = re.compile(r"terminal[-_]handoff", re.IGNORECASE)


def is_terminal_handoff_command(command):
    """True if `command` is a Terminal Handoff status-line command."""
    return bool(command) and bool(TH_COMMAND_MARKER.search(str(command)))


STATE_DIRS = (
    "handoffs",
    "triggered",
    "launching",
    "completed",
    "failed",
    "logs",
    "tests",
    "backups",
    "prompts",
    "state",
    "chains",
    "transfers",
)

TRUTHY = ("1", "true", "yes", "on")


def env_flag(name):
    return os.environ.get(name, "").strip().lower() in TRUTHY


def env_float(name, default):
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value


def env_int(name, default):
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def th_home():
    override = os.environ.get("CLAUDE_TERMINAL_HANDOFF_HOME")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.expanduser("~"), ".claude", "terminal-handoff")


def th_path(*parts):
    return os.path.join(th_home(), *parts)


def ensure_dirs():
    home = th_home()
    _mkdir_private(home)
    for name in STATE_DIRS:
        _mkdir_private(os.path.join(home, name))


def _mkdir_private(path):
    try:
        os.makedirs(path, 0o700)
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            raise
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def threshold():
    value = env_float("CLAUDE_TERMINAL_HANDOFF_THRESHOLD", DEFAULT_THRESHOLD)
    if value <= 0 or value > 100:
        return DEFAULT_THRESHOLD
    return value


def max_generations():
    raw = os.environ.get("CLAUDE_TERMINAL_HANDOFF_MAX_GENERATIONS", "").strip()
    if raw == "":
        return None  # unlimited (documented default)
    try:
        value = int(raw)
    except ValueError:
        return None
    if value <= 0:
        return None
    return value


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def utc_now():
    return datetime.now(timezone.utc)


def utc_stamp(dt=None):
    return (dt or utc_now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_stamp(dt=None):
    return (dt or datetime.now()).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def file_stamp(dt=None):
    return (dt or utc_now()).strftime("%Y%m%dT%H%M%SZ")


# ---------------------------------------------------------------------------
# Secure IO
# ---------------------------------------------------------------------------


def write_private(path, data, mode=0o600):
    """Atomically write `data` (str) to `path` with private permissions."""
    directory = os.path.dirname(path) or "."
    _mkdir_private(directory)
    tmp = "%s.tmp.%d.%s" % (path, os.getpid(), uuid.uuid4().hex[:8])
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)
    try:
        os.chmod(path, mode)
    except OSError:
        pass
    return path


def write_json_private(path, obj, mode=0o600, sort_keys=True):
    return write_private(
        path, json.dumps(obj, indent=2, sort_keys=sort_keys, ensure_ascii=False) + "\n", mode
    )


def read_json(path, default=None):
    try:
        with open(path, "r") as handle:
            return json.load(handle)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Logging (bounded rotation, private, never contains transcript content)
# ---------------------------------------------------------------------------

LOG_MAX_BYTES = 1024 * 1024
LOG_KEEP = 5


def log_path():
    return th_path("logs", "terminal-handoff.log")


def rotate_logs():
    path = log_path()
    try:
        if os.path.getsize(path) < LOG_MAX_BYTES:
            return
    except OSError:
        return
    for index in range(LOG_KEEP - 1, 0, -1):
        src = "%s.%d" % (path, index)
        dst = "%s.%d" % (path, index + 1)
        if os.path.exists(src):
            try:
                os.replace(src, dst)
            except OSError:
                pass
    try:
        os.replace(path, path + ".1")
    except OSError:
        pass


def log_event(event, **fields):
    """Append one structured Terminal Handoff log record.

    Never logs transcript contents, prompt bodies, secrets or environment dumps.
    """
    try:
        ensure_dirs()
        rotate_logs()
        record = {"ts": utc_stamp(), "event": event, "th_version": TERMINAL_HANDOFF_VERSION}
        record.update(fields)
        line = json.dumps(record, sort_keys=True, default=str) + "\n"
        fd = os.open(log_path(), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:
        pass  # Logging must never break the status line.


# ---------------------------------------------------------------------------
# Validation of the official status-line JSON
# ---------------------------------------------------------------------------


class Facts(object):
    """Validated view of one status-line JSON payload."""

    __slots__ = (
        "ok",
        "errors",
        "warnings",
        "session_id",
        "session_name",
        "transcript_path",
        "cwd",
        "current_dir",
        "project_dir",
        "worktree_path",
        "worktree_name",
        "git_worktree",
        "model_id",
        "model_display_name",
        "effort_level",
        "effort_available",
        "percent",
        "percent_valid",
        "cc_version",
    )

    def __init__(self):
        self.ok = False
        self.errors = []
        self.warnings = []
        self.session_id = None
        self.session_name = None
        self.transcript_path = None
        self.cwd = None
        self.current_dir = None
        self.project_dir = None
        self.worktree_path = None
        self.worktree_name = None
        self.git_worktree = None
        self.model_id = None
        self.model_display_name = None
        self.effort_level = None
        self.effort_available = False
        self.percent = None
        self.percent_valid = False
        self.cc_version = None

    def to_dict(self):
        return dict((k, getattr(self, k)) for k in self.__slots__)


def _dget(payload, *keys):
    node = payload
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _safe_path_for_shell(path):
    if not isinstance(path, str) or path == "":
        return False
    if UNSAFE_PATH_RE.search(path):
        return False
    return True


def validate_transcript(path, session_id):
    """Validate transcript_path. Returns (ok, errors, warnings)."""
    errors = []
    warnings = []
    if not isinstance(path, str) or path.strip() == "":
        return False, ["transcript_path missing"], warnings
    if not os.path.isabs(path):
        return False, ["transcript_path is not absolute"], warnings
    if UNSAFE_PATH_RE.search(path):
        return False, ["transcript_path contains unsafe characters"], warnings
    normalized = os.path.normpath(path)
    if ".." in normalized.split(os.sep):
        return False, ["transcript_path contains traversal segments"], warnings
    if not os.path.exists(normalized):
        return False, ["transcript_path does not exist"], warnings
    real = os.path.realpath(normalized)
    if not os.path.isfile(real):
        return False, ["transcript_path is not a regular file"], warnings
    if not os.access(real, os.R_OK):
        return False, ["transcript_path is not readable"], warnings
    try:
        size = os.path.getsize(real)
    except OSError:
        return False, ["transcript_path is not stat-able"], warnings
    if size <= 0:
        return False, ["transcript_path is empty"], warnings
    # Shape check: the transcript must be JSON Lines.
    try:
        with open(real, "r", errors="replace") as handle:
            first = handle.readline(65536).strip()
    except Exception:
        return False, ["transcript_path could not be read"], warnings
    if not first:
        return False, ["transcript first line is empty"], warnings
    try:
        json.loads(first)
    except ValueError:
        return False, ["transcript is not JSONL"], warnings
    # Session correlation, as far as the available schema allows.
    if session_id and os.path.basename(real) != ("%s.jsonl" % session_id):
        warnings.append("transcript basename does not match session_id")
    return True, errors, warnings


def extract_facts(payload, validate_files=True):
    facts = Facts()
    if not isinstance(payload, dict):
        facts.errors.append("payload is not a JSON object")
        return facts

    facts.cc_version = _dget(payload, "version")

    session_id = _dget(payload, "session_id")
    if isinstance(session_id, str) and SESSION_ID_RE.match(session_id):
        facts.session_id = session_id
    else:
        facts.errors.append("session_id missing or invalid")

    name = _dget(payload, "session_name")
    if isinstance(name, str) and name.strip():
        facts.session_name = name.strip()[:120]

    facts.cwd = _dget(payload, "cwd")
    facts.current_dir = _dget(payload, "workspace", "current_dir") or facts.cwd
    facts.project_dir = _dget(payload, "workspace", "project_dir") or facts.current_dir
    facts.git_worktree = _dget(payload, "workspace", "git_worktree")
    facts.worktree_name = _dget(payload, "worktree", "name") or facts.git_worktree
    facts.worktree_path = _dget(payload, "worktree", "path")

    if not _safe_path_for_shell(facts.current_dir):
        facts.errors.append("workspace.current_dir missing or unsafe for launch")
    elif not (validate_files and os.path.isdir(facts.current_dir)) and validate_files:
        facts.errors.append("workspace.current_dir is not a directory")

    model_id = _dget(payload, "model", "id")
    if isinstance(model_id, str) and MODEL_ID_RE.match(model_id):
        facts.model_id = model_id
    else:
        facts.errors.append("model.id missing or invalid")
    display = _dget(payload, "model", "display_name")
    facts.model_display_name = display if isinstance(display, str) else None

    # `.effort` is optional: present only when the model supports reasoning
    # effort. Absent is legitimate; present-but-invalid is a hard failure.
    effort_node = payload.get("effort")
    if effort_node is None:
        facts.effort_available = False
        facts.effort_level = None
        facts.warnings.append("effort unavailable: .effort absent from status JSON")
    elif not isinstance(effort_node, dict):
        facts.errors.append("effort is not an object")
    else:
        level = effort_node.get("level")
        if isinstance(level, str) and level in ALLOWED_EFFORT_LEVELS:
            facts.effort_available = True
            facts.effort_level = level
        else:
            facts.errors.append("effort.level invalid: %r" % (level,))

    context = payload.get("context_window")
    if not isinstance(context, dict):
        facts.warnings.append("context_window missing")
    else:
        used = context.get("used_percentage")
        if isinstance(used, bool):
            facts.warnings.append("used_percentage is not numeric")
        elif isinstance(used, (int, float)):
            value = float(used)
            if value != value or value < 0 or value > 100:  # NaN / out of range
                facts.warnings.append("used_percentage out of range")
            else:
                facts.percent = value
                facts.percent_valid = True
        elif used is None:
            facts.warnings.append("used_percentage is null")
        else:
            facts.warnings.append("used_percentage is not numeric")

    transcript = _dget(payload, "transcript_path")
    if validate_files:
        ok, errs, warns = validate_transcript(transcript, facts.session_id)
        if ok:
            facts.transcript_path = os.path.realpath(transcript)
        else:
            facts.errors.extend(errs)
        facts.warnings.extend(warns)
    else:
        facts.transcript_path = transcript if isinstance(transcript, str) else None

    facts.ok = not facts.errors
    return facts


# ---------------------------------------------------------------------------
# Per-session observation state (storm protection: stability requirement)
# ---------------------------------------------------------------------------


def session_state_path(session_id):
    return th_path("state", "session-%s.json" % session_id)


def record_observation(session_id, percent):
    """Record one non-null percentage observation for THIS session only.

    A successor never inherits the parent's observations because the key is the
    successor's own, different session_id.
    """
    path = session_state_path(session_id)
    state = read_json(path, {}) or {}
    state["session_id"] = session_id
    state["observations"] = int(state.get("observations", 0)) + 1
    state["last_percent"] = percent
    state["last_seen"] = utc_stamp()
    if "first_seen" not in state:
        state["first_seen"] = state["last_seen"]
    try:
        write_json_private(path, state)
    except Exception:
        pass
    return state


def observation_count(session_id):
    state = read_json(session_state_path(session_id), {}) or {}
    try:
        return int(state.get("observations", 0))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Cooldown and storm circuit breaker
# ---------------------------------------------------------------------------


def launches_log():
    return th_path("state", "launches.jsonl")


def circuit_file():
    return th_path("state", "circuit-open.json")


def cooldown_file():
    return th_path("state", "last-launch.json")


def storm_settings():
    return (
        env_int("CLAUDE_TERMINAL_HANDOFF_STORM_MAX", DEFAULT_STORM_MAX_LAUNCHES),
        env_int("CLAUDE_TERMINAL_HANDOFF_STORM_WINDOW", DEFAULT_STORM_WINDOW_SECONDS),
        env_int("CLAUDE_TERMINAL_HANDOFF_COOLDOWN", DEFAULT_COOLDOWN_SECONDS),
        env_int("CLAUDE_TERMINAL_HANDOFF_CIRCUIT_SECONDS", DEFAULT_CIRCUIT_OPEN_SECONDS),
    )


def storm_reset_file():
    return th_path("state", "storm-reset.json")


def storm_reset_epoch():
    """Launches at or before this epoch are excluded from the storm window.

    Set by `reset-circuit` so a deliberate manual reset actually permits the
    next launch instead of instantly re-tripping on already-counted launches.
    """
    data = read_json(storm_reset_file())
    if not isinstance(data, dict):
        return 0.0
    try:
        return float(data.get("epoch", 0))
    except (TypeError, ValueError):
        return 0.0


def recent_launch_times(window_seconds, now=None):
    now = now if now is not None else time.time()
    reset_at = storm_reset_epoch()
    times = []
    try:
        with open(launches_log(), "r") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    stamp = float(record.get("epoch", 0))
                except (ValueError, TypeError):
                    continue
                if stamp <= reset_at:
                    continue
                if now - stamp <= window_seconds:
                    times.append(stamp)
    except IOError:
        pass
    return times


def circuit_is_open(now=None):
    now = now if now is not None else time.time()
    data = read_json(circuit_file())
    if not isinstance(data, dict):
        return False, None
    try:
        until = float(data.get("until", 0))
    except (TypeError, ValueError):
        return False, None
    if until > now:
        return True, data
    return False, data


def trip_circuit(reason, count, now=None):
    now = now if now is not None else time.time()
    _, _, _, circuit_seconds = storm_settings()
    payload = {
        "opened_at": utc_stamp(),
        "opened_epoch": now,
        "until": now + circuit_seconds,
        "until_human": utc_stamp(datetime.fromtimestamp(now + circuit_seconds, timezone.utc)),
        "reason": reason,
        "launches_in_window": count,
        "reset_hint": "rm %s" % circuit_file(),
    }
    try:
        write_json_private(circuit_file(), payload)
    except Exception:
        pass
    log_event("circuit_breaker_open", reason=reason, launches_in_window=count)
    return payload


def cooldown_remaining(now=None):
    now = now if now is not None else time.time()
    _, _, cooldown, _ = storm_settings()
    data = read_json(cooldown_file())
    if not isinstance(data, dict):
        return 0
    try:
        last = float(data.get("epoch", 0))
    except (TypeError, ValueError):
        return 0
    remaining = cooldown - (now - last)
    return remaining if remaining > 0 else 0


def record_launch(session_id, chain_id, generation, now=None):
    now = now if now is not None else time.time()
    ensure_dirs()
    record = {
        "epoch": now,
        "ts": utc_stamp(),
        "session_id": session_id,
        "chain_id": chain_id,
        "generation": generation,
    }
    fd = os.open(launches_log(), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    write_json_private(cooldown_file(), record)


# ---------------------------------------------------------------------------
# Chain / generation identity
# ---------------------------------------------------------------------------


def chain_identity(session_id=None, session_name=None):
    """Return (chain_id, generation, parent_manifest_path, parent_session_id).

    A successor learns its chain from environment variables exported by the
    Terminal Handoff launcher into its Terminal window. An ordinary session
    starts a new chain at generation 1.

    The generation is cross-checked against trusted Terminal Handoff chain
    state, which is authoritative when it knows this session. It is never
    inferred by parsing trailing digits off a visible session name.
    """
    chain = os.environ.get("CLAUDE_TERMINAL_HANDOFF_CHAIN_ID", "").strip()
    if not CHAIN_ID_RE.match(chain or ""):
        chain = None
    generation = env_int("CLAUDE_TERMINAL_HANDOFF_GENERATION", 0)
    parent_manifest = os.environ.get("CLAUDE_TERMINAL_HANDOFF_MANIFEST", "").strip() or None
    parent_session = os.environ.get("CLAUDE_TERMINAL_HANDOFF_PARENT_SESSION", "").strip() or None
    if chain is None:
        return uuid.uuid4().hex[:12], 1, None, None
    recorded = chain_generation_for_session(chain, session_id)
    if recorded:
        generation = recorded
    if generation < 1:
        generation = 1
    return chain, generation, parent_manifest, parent_session


# ---------------------------------------------------------------------------
# Human-facing session display names
# ---------------------------------------------------------------------------

# A Claude session name is human-facing text. Terminal Handoff keeps the base
# name exactly as the user chose it and appends the generation number:
#
#     Ranger      ->  Ranger 2  ->  Ranger 3  ->  Ranger 4
#     Nova Drone  ->  Nova Drone 2
#
# The machine-safe chain identifier stays separate and is never shown as a
# session name.
DISPLAY_NAME_MAX = 64
DISPLAY_NAME_CONVENTION = "SessionName, SessionName 2, SessionName 3, SessionName 4"

# Control characters, line separators and paragraph separators are removed
# before a name is used. Everything else - including Unicode - is preserved:
# a display name is only ever passed as a single argv element (never as shell
# text) and escaped for AppleScript, so it cannot alter a command.
DISPLAY_NAME_STRIP_RE = re.compile(u"[\x00-\x1f\x7f\u2028\u2029]")


def sanitize_display_name(raw):
    """Return a safe human-facing name, or None when there isn't one."""
    if not isinstance(raw, str):
        return None
    text = DISPLAY_NAME_STRIP_RE.sub(" ", raw)
    text = " ".join(text.split())
    # A leading dash would make the name look like a command-line flag.
    text = text.lstrip("-").strip()
    if not text:
        return None
    if len(text) > DISPLAY_NAME_MAX:
        text = text[:DISPLAY_NAME_MAX].rstrip()
    return text or None


def fallback_base_name(chain_id):
    """Documented safe fallback when no Claude session name is available.

    Terminal Handoff never invents a repository, directory or project name to
    stand in for a session name.
    """
    return "Terminal Handoff %s" % (str(chain_id or "")[:8] or "chain")


def generation_display_name(base_name, generation):
    """`Ranger` -> `Ranger 2`. Generation 1 keeps the base name unchanged."""
    base = sanitize_display_name(base_name)
    if base is None:
        return None
    try:
        gen = int(generation)
    except (TypeError, ValueError):
        return base
    if gen <= 1:
        return base
    return "%s %d" % (base, gen)


def successor_session_name(base_name, successor_generation):
    """The successor's human-facing Claude session name."""
    return generation_display_name(base_name, successor_generation)


# ---------------------------------------------------------------------------
# Trusted chain metadata
# ---------------------------------------------------------------------------


def update_json_locked(path, mutator, default=None):
    """Read-modify-write a private JSON file under an exclusive lock."""
    ensure_dirs()
    _mkdir_private(os.path.dirname(path) or ".")
    lock_path = path + ".lock"
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        data = read_json(path)
        if not isinstance(data, dict):
            data = dict(default or {})
        mutator(data)
        write_json_private(path, data)
        return data
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def chain_state_path(chain_id):
    return th_path("chains", "%s.json" % chain_id)


def read_chain_state(chain_id):
    if not chain_id or not CHAIN_ID_RE.match(str(chain_id)):
        return None
    data = read_json(chain_state_path(chain_id))
    return data if isinstance(data, dict) else None


def record_chain_generation(
    chain_id, generation, session_id=None, display_name=None, base_name=None, source=None, **extra
):
    """Record trusted chain metadata for one generation.

    The base display name is written once, when the chain is created, and is
    never rewritten by a later generation: it is the chain's identity.
    """
    if not chain_id or not CHAIN_ID_RE.match(str(chain_id)):
        return None

    def mutate(data):
        data["chain_id"] = chain_id
        data["schema_version"] = MANIFEST_SCHEMA_VERSION
        if base_name and not data.get("base_display_name"):
            data["base_display_name"] = base_name
            data["base_name_source"] = source or "unknown"
            data["naming_convention"] = DISPLAY_NAME_CONVENTION
            data["created_utc"] = utc_stamp()
        generations = data.setdefault("generations", {})
        entry = generations.setdefault(str(int(generation)), {})
        entry["generation"] = int(generation)
        entry["updated_utc"] = utc_stamp()
        if display_name:
            entry["display_name"] = display_name
        if session_id:
            entry["session_id"] = session_id
            seen = data.setdefault("session_ids", [])
            if session_id not in seen:
                seen.append(session_id)
        entry.update(extra)
        try:
            latest = int(data.get("latest_generation") or 0)
        except (TypeError, ValueError):
            latest = 0
        data["latest_generation"] = max(int(generation), latest)

    return update_json_locked(chain_state_path(chain_id), mutate, {"chain_id": chain_id})


def chain_generation_for_session(chain_id, session_id):
    """The generation this session occupies, from trusted chain state only."""
    data = read_chain_state(chain_id)
    if not data or not session_id:
        return None
    for key, entry in (data.get("generations") or {}).items():
        if not isinstance(entry, dict):
            continue
        if entry.get("session_id") == session_id:
            try:
                return int(entry.get("generation", key))
            except (TypeError, ValueError):
                return None
    return None


def resolve_base_display_name(chain_id, generation, session_name):
    """Return (base_name, source) for a chain.

    Generation 1 captures the live Claude session name from the official
    status-line JSON. Every later generation takes the base name from trusted
    Terminal Handoff chain state - never by parsing trailing digits off a
    visible session name.
    """
    try:
        generation = int(generation)
    except (TypeError, ValueError):
        generation = 1
    if generation > 1:
        state = read_chain_state(chain_id) or {}
        stored = sanitize_display_name(state.get("base_display_name"))
        if stored:
            return stored, "chain_state"
        from_env = sanitize_display_name(os.environ.get("CLAUDE_TERMINAL_HANDOFF_BASE_NAME"))
        if from_env:
            return from_env, "environment"
        return fallback_base_name(chain_id), "fallback"
    captured = sanitize_display_name(session_name)
    if captured:
        return captured, "session_name"
    return fallback_base_name(chain_id), "fallback"


# ---------------------------------------------------------------------------
# Decision engine
# ---------------------------------------------------------------------------

# Decision states, mapped to the status-line badge vocabulary.
BADGES = {
    "disabled": "TH disabled",
    "no_percent": "TH ready",
    "below": None,  # rendered as "TH nn%"
    "eligible": "TH ready",
    "trigger": "TH launching",
    "handed_off": "TH handed off",
    "launching": "TH launching",
    "blocked": "TH blocked",
    "cooldown": "TH retrying",
    "circuit_open": "TH circuit open",
    "unstable": "TH ready",
    "max_generations": "TH blocked",
    "invalid": "TH blocked",
}


def decide(payload, validate_files=True, now=None, record=True):
    """Pure-ish trigger decision. Returns a dict; never raises."""
    now = now if now is not None else time.time()
    limit = threshold()
    result = {
        "th_version": TERMINAL_HANDOFF_VERSION,
        "trigger": False,
        "state": "invalid",
        "reason": "",
        "threshold": limit,
        "percent": None,
        "session_id": None,
        "model_id": None,
        "effort_level": None,
        "effort_available": False,
        "errors": [],
        "warnings": [],
    }

    if env_flag("CLAUDE_TERMINAL_HANDOFF_DISABLED"):
        result["state"] = "disabled"
        result["reason"] = "CLAUDE_TERMINAL_HANDOFF_DISABLED is set"
        return result

    if payload is None:
        result["state"] = "invalid"
        result["reason"] = "malformed or absent status-line JSON"
        result["errors"] = ["malformed json"]
        return result

    facts = extract_facts(payload, validate_files=validate_files)
    result["errors"] = list(facts.errors)
    result["warnings"] = list(facts.warnings)
    result["session_id"] = facts.session_id
    result["model_id"] = facts.model_id
    result["effort_level"] = facts.effort_level
    result["effort_available"] = facts.effort_available
    result["percent"] = facts.percent

    # Gate 1: context percentage must be present, numeric and in range.
    # Terminal Handoff never triggers on a missing, null or unverified value,
    # and never reads rate-limit percentages.
    if not facts.percent_valid:
        result["state"] = "no_percent"
        result["reason"] = "context_window.used_percentage unavailable"
        return result

    # Gate 2: below threshold. Cheap path; record stability observation only.
    if record and facts.session_id:
        record_observation(facts.session_id, facts.percent)

    if facts.percent < limit:
        result["state"] = "below"
        result["reason"] = "%.2f%% < %.2f%%" % (facts.percent, limit)
        return result

    # At or above threshold from here.
    if facts.errors:
        result["state"] = "blocked"
        result["reason"] = "validation failed: %s" % "; ".join(facts.errors)
        return result

    # Gate 3: one trigger per session (atomic marker keyed by session_id).
    if os.path.exists(th_path("triggered", facts.session_id)):
        result["state"] = "handed_off"
        result["reason"] = "this session has already handed off"
        return result

    # Gate 4: optional generation ceiling.
    chain_id, generation, parent_manifest, parent_session = chain_identity(
        facts.session_id, facts.session_name
    )
    base_name, base_source = resolve_base_display_name(chain_id, generation, facts.session_name)
    result["chain_id"] = chain_id
    result["generation"] = generation
    result["parent_manifest"] = parent_manifest
    result["parent_session_id"] = parent_session
    result["base_display_name"] = base_name
    result["base_name_source"] = base_source
    result["display_name"] = generation_display_name(base_name, generation)
    result["successor_display_name"] = generation_display_name(base_name, generation + 1)
    ceiling = max_generations()
    if ceiling is not None and generation >= ceiling:
        result["state"] = "max_generations"
        result["reason"] = "generation %d has reached CLAUDE_TERMINAL_HANDOFF_MAX_GENERATIONS=%d" % (
            generation,
            ceiling,
        )
        return result

    # Gate 5: storm circuit breaker.
    open_now, circuit = circuit_is_open(now)
    if open_now:
        result["state"] = "circuit_open"
        result["reason"] = "storm circuit breaker is open until %s" % (circuit or {}).get(
            "until_human", "?"
        )
        return result

    storm_max, storm_window, _, _ = storm_settings()
    recent = recent_launch_times(storm_window, now)
    if len(recent) >= storm_max:
        trip_circuit("%d launches within %ds" % (len(recent), storm_window), len(recent), now)
        result["state"] = "circuit_open"
        result["reason"] = "storm circuit breaker tripped (%d launches in %ds)" % (
            len(recent),
            storm_window,
        )
        return result

    # Gate 6: launch cooldown.
    remaining = cooldown_remaining(now)
    if remaining > 0:
        result["state"] = "cooldown"
        result["reason"] = "cooldown active, %.0fs remaining" % remaining
        return result

    # Gate 7: stability. The session must have produced at least N of its own
    # non-null percentage observations. A brand-new successor cannot trigger on
    # a stale or inherited reading.
    minimum = env_int("CLAUDE_TERMINAL_HANDOFF_MIN_OBSERVATIONS", DEFAULT_MIN_OBSERVATIONS)
    seen = observation_count(facts.session_id) if record else minimum
    if seen < minimum:
        result["state"] = "unstable"
        result["reason"] = "only %d/%d stable observations for this session" % (seen, minimum)
        return result

    result["trigger"] = True
    result["state"] = "trigger"
    result["reason"] = "%.2f%% >= %.2f%%" % (facts.percent, limit)
    return result


# ---------------------------------------------------------------------------
# Git and repository capture (trigger path only, never the status-line path)
# ---------------------------------------------------------------------------


def run_git(repo, args, timeout=GIT_TIMEOUT):
    try:
        proc = subprocess.Popen(
            ["/usr/bin/git"] + args,
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )
        out, _ = proc.communicate(timeout=timeout)
        if proc.returncode != 0:
            return None
        return out.decode("utf-8", "replace").strip()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        return None


def capture_repo_state(directory):
    state = {
        "is_git_repository": False,
        "repo_root": None,
        "branch": None,
        "head_sha": None,
        "origin_main_ref": None,
        "origin_main_sha": None,
        "ahead": None,
        "behind": None,
        "status_porcelain_count": 0,
        "staged_files": [],
        "modified_tracked_files": [],
        "untracked_files": [],
        "recent_commits": [],
        "merge_in_progress": False,
        "rebase_in_progress": False,
        "cherry_pick_in_progress": False,
        "revert_in_progress": False,
        "bisect_in_progress": False,
        "is_linked_worktree": False,
        "worktree_path": None,
    }
    if not directory or not os.path.isdir(directory):
        state["note"] = "directory does not exist"
        return state

    root = run_git(directory, ["rev-parse", "--show-toplevel"])
    if not root:
        state["note"] = "not a git repository"
        return state

    state["is_git_repository"] = True
    state["repo_root"] = root
    state["branch"] = run_git(directory, ["rev-parse", "--abbrev-ref", "HEAD"])
    state["head_sha"] = run_git(directory, ["rev-parse", "HEAD"])

    for ref in ("origin/main", "origin/master"):
        sha = run_git(directory, ["rev-parse", "--verify", "--quiet", ref])
        if sha:
            state["origin_main_ref"] = ref
            state["origin_main_sha"] = sha
            break

    if state["origin_main_ref"]:
        counts = run_git(
            directory, ["rev-list", "--left-right", "--count", "HEAD...%s" % state["origin_main_ref"]]
        )
        if counts:
            parts = counts.split()
            if len(parts) == 2:
                try:
                    state["ahead"] = int(parts[0])
                    state["behind"] = int(parts[1])
                except ValueError:
                    pass

    porcelain = run_git(directory, ["status", "--porcelain=v1"])
    if porcelain is not None:
        lines = [line for line in porcelain.splitlines() if line.strip()]
        state["status_porcelain_count"] = len(lines)

    def name_list(args, cap=200):
        out = run_git(directory, args)
        if not out:
            return []
        names = [line for line in out.splitlines() if line.strip()]
        if len(names) > cap:
            return names[:cap] + ["<%d more omitted>" % (len(names) - cap)]
        return names

    state["staged_files"] = name_list(["diff", "--cached", "--name-only"])
    state["modified_tracked_files"] = name_list(["diff", "--name-only"])
    state["untracked_files"] = name_list(["ls-files", "--others", "--exclude-standard"])

    commits = run_git(directory, ["log", "-10", "--date=iso-strict", "--pretty=%h|%ad|%an|%s"])
    if commits:
        for line in commits.splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4:
                state["recent_commits"].append(
                    {"sha": parts[0], "date": parts[1], "author": parts[2], "subject": parts[3][:200]}
                )

    git_dir = run_git(directory, ["rev-parse", "--absolute-git-dir"])
    common_dir = run_git(directory, ["rev-parse", "--path-format=absolute", "--git-common-dir"])
    if git_dir:
        state["merge_in_progress"] = os.path.exists(os.path.join(git_dir, "MERGE_HEAD"))
        state["rebase_in_progress"] = os.path.exists(
            os.path.join(git_dir, "rebase-merge")
        ) or os.path.exists(os.path.join(git_dir, "rebase-apply"))
        state["cherry_pick_in_progress"] = os.path.exists(os.path.join(git_dir, "CHERRY_PICK_HEAD"))
        state["revert_in_progress"] = os.path.exists(os.path.join(git_dir, "REVERT_HEAD"))
        state["bisect_in_progress"] = os.path.exists(os.path.join(git_dir, "BISECT_LOG"))
        if common_dir and os.path.normpath(common_dir) != os.path.normpath(git_dir):
            state["is_linked_worktree"] = True
            state["worktree_path"] = root
    return state


def applicable_claude_md(current_dir, project_dir):
    paths = []
    seen = set()
    home = os.path.expanduser("~")
    for candidate in (os.path.join(home, ".claude", "CLAUDE.md"),):
        if os.path.isfile(candidate) and candidate not in seen:
            paths.append(candidate)
            seen.add(candidate)
    walk_from = current_dir if current_dir and os.path.isdir(current_dir) else None
    if walk_from:
        node = os.path.abspath(walk_from)
        while True:
            for name in ("CLAUDE.md", "CLAUDE.local.md"):
                candidate = os.path.join(node, name)
                if os.path.isfile(candidate) and candidate not in seen:
                    paths.append(candidate)
                    seen.add(candidate)
            parent = os.path.dirname(node)
            if parent == node or node == home:
                break
            node = parent
    if project_dir and os.path.isdir(project_dir):
        candidate = os.path.join(project_dir, "CLAUDE.md")
        if os.path.isfile(candidate) and candidate not in seen:
            paths.append(candidate)
            seen.add(candidate)
    return paths


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def manifest_path(session_id):
    return th_path("handoffs", "%s.json" % session_id)


def build_manifest(facts, decision, now=None):
    now = now if now is not None else time.time()
    chain_id = decision.get("chain_id") or uuid.uuid4().hex[:12]
    generation = decision.get("generation") or 1
    base_name = decision.get("base_display_name")
    base_source = decision.get("base_name_source")
    if not base_name:
        base_name, base_source = resolve_base_display_name(
            chain_id, generation, facts.session_name
        )
    repo = capture_repo_state(facts.current_dir)
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "terminal_handoff_version": TERMINAL_HANDOFF_VERSION,
        "system": "Terminal Handoff",
        "chain_id": chain_id,
        "generation": generation,
        "display": {
            "base_name": base_name,
            "base_name_source": base_source,
            "outgoing_display_name": generation_display_name(base_name, generation),
            "successor_display_name": generation_display_name(base_name, generation + 1),
            "convention": DISPLAY_NAME_CONVENTION,
            "note": (
                "The human-facing session name is the chain's base name plus the "
                "generation number. Generation 1 keeps the base name unchanged. The "
                "chain_id above is a machine-safe identifier and is never shown as a "
                "session name."
            ),
        },
        "parent_handoff_manifest": decision.get("parent_manifest"),
        "parent_of_outgoing_session_id": decision.get("parent_session_id"),
        "outgoing": {
            "session_id": facts.session_id,
            "session_name": facts.session_name,
            "transcript_path": facts.transcript_path,
            "cwd": facts.cwd,
            "current_dir": facts.current_dir,
            "project_dir": facts.project_dir,
            "worktree_path": facts.worktree_path or repo.get("worktree_path"),
            "worktree_name": facts.worktree_name,
            "claude_code_version": facts.cc_version,
        },
        "model": {
            "id": facts.model_id,
            "display_name": facts.model_display_name,
        },
        "effort": {
            "level": facts.effort_level,
            "available": facts.effort_available,
            "ultracode": "undetectable",
            "note": (
                "Claude Code's status-line JSON never reports ultracode; "
                "`--effort ultracode` resolves to xhigh plus a hidden flag. "
                "Terminal Handoff preserves the reported level only."
            ),
        },
        "trigger": {
            "context_used_percentage": facts.percent,
            "configured_threshold": decision.get("threshold"),
            "timestamp_utc": utc_stamp(),
            "timestamp_local": local_stamp(),
            "epoch": now,
            "reason": decision.get("reason"),
        },
        "repository": repo,
        "claude_md_paths": applicable_claude_md(facts.current_dir, facts.project_dir),
        "successor": {
            "launch_state": "eligible",
            "session_id": None,
            "first_heartbeat_utc": None,
            "confirmed_utc": None,
            "expected_model_id": facts.model_id,
            "expected_effort_level": facts.effort_level,
            "expected_current_dir": facts.current_dir,
            "expected_display_name": generation_display_name(base_name, generation + 1),
            "expected_chain_id": chain_id,
            "generation": generation + 1,
        },
        "validation_warnings": list(facts.warnings) + list(decision.get("warnings") or []),
        "security": {
            "stores_secrets": False,
            "stores_transcript_contents": False,
            "stores_environment_dump": False,
            "file_mode": "0600",
        },
    }
    return manifest


def update_manifest(path, mutator):
    """Read-modify-write a manifest under an exclusive lock."""
    ensure_dirs()
    lock_path = path + ".lock"
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        data = read_json(path)
        if not isinstance(data, dict):
            return None
        mutator(data)
        write_json_private(path, data)
        return data
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# ---------------------------------------------------------------------------
# Launch command construction
# ---------------------------------------------------------------------------


def find_claude_executable():
    explicit = os.environ.get("CLAUDE_TERMINAL_HANDOFF_CLAUDE_BIN", "").strip()
    candidates = []
    if explicit:
        candidates.append(explicit)
    candidates.extend(
        [
            os.path.join(os.path.expanduser("~"), ".local", "bin", "claude"),
            "/usr/local/bin/claude",
            "/opt/homebrew/bin/claude",
        ]
    )
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    # Last resort: PATH lookup without a shell.
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(directory, "claude")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def build_launch_argv(manifest, claude_bin, prompt_text):
    """Construct the successor launch argv.

    Never includes --continue, --resume, -c, -r, --fork-session or any
    permission bypass. The model ID and effort level are passed as separate
    argv elements, never as shell text.
    """
    model_id = (manifest.get("model") or {}).get("id")
    effort = (manifest.get("effort") or {}).get("level")
    effort_available = bool((manifest.get("effort") or {}).get("available"))
    chain_id = manifest.get("chain_id")
    generation = int(manifest.get("generation") or 1)

    if not isinstance(model_id, str) or not MODEL_ID_RE.match(model_id):
        raise ValueError("refusing to launch: model id is missing or unsafe")

    argv = [claude_bin, "--model", model_id]

    if effort_available:
        if effort not in ALLOWED_EFFORT_LEVELS:
            raise ValueError("refusing to launch: effort %r is not an allowed level" % (effort,))
        argv += ["--effort", effort]
    # else: effort proven unavailable -> --effort omitted, recorded in manifest.

    display = manifest.get("display") or {}
    successor_name = sanitize_display_name(display.get("successor_display_name"))
    if not successor_name:
        # Fail closed on to the documented fallback rather than inventing a
        # name from the repository, the directory or the chain identifier.
        successor_name = successor_session_name(fallback_base_name(chain_id), generation + 1)
    argv += ["--name", successor_name]
    argv += [prompt_text]
    return argv


FORBIDDEN_LAUNCH_TOKENS = (
    "--continue",
    "-c",
    "--resume",
    "-r",
    "--fork-session",
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--permission-mode",
    "--fallback-model",
)


def assert_launch_argv_safe(argv):
    """Defence in depth: verify the constructed argv contains nothing banned."""
    problems = []
    for token in FORBIDDEN_LAUNCH_TOKENS:
        if token in argv[:-1]:  # the final element is the prompt text
            problems.append("forbidden flag present: %s" % token)
    if "--model" not in argv:
        problems.append("missing --model")
    return problems


# ---------------------------------------------------------------------------
# Successor prompt rendering
# ---------------------------------------------------------------------------


def successor_prompt_template_path():
    """Locate the successor prompt template.

    Checked in order: beside this module (the installed layout), then the
    packaged `templates/` directory (the development layout), then the state
    home. The template ships with the code, never with runtime state.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (
        os.path.join(here, "successor-prompt.md"),
        os.path.join(here, "templates", "successor-prompt.md"),
    ):
        if os.path.isfile(candidate):
            return candidate
    return th_path("successor-prompt.md")


def render_successor_prompt(manifest, template_path=None):
    template_path = template_path or successor_prompt_template_path()
    try:
        with open(template_path, "r") as handle:
            template = handle.read()
    except IOError:
        template = FALLBACK_PROMPT_TEMPLATE
    effort = manifest.get("effort") or {}
    effort_display = effort.get("level") if effort.get("available") else "unavailable (not exposed by this model/version)"
    display = manifest.get("display") or {}
    generation = int(manifest.get("generation") or 1)
    base_name = display.get("base_name")
    values = {
        "{{GENERATION}}": str(generation + 1),
        "{{DISPLAY_NAME}}": str(display.get("successor_display_name") or "this session"),
        "{{SUCCESSOR_DISPLAY_NAME}}": str(
            generation_display_name(base_name, generation + 2) or "the next generation"
        ),
        "{{BASE_DISPLAY_NAME}}": str(base_name or "unavailable"),
        "{{PARENT_DISPLAY_NAME}}": str(display.get("outgoing_display_name") or "unknown"),
        "{{TRANSFER_PATH}}": transfer_path((manifest.get("outgoing") or {}).get("session_id")),
        "{{CHAIN_ID}}": str(manifest.get("chain_id")),
        "{{PARENT_SESSION_ID}}": str((manifest.get("outgoing") or {}).get("session_id")),
        "{{PARENT_GENERATION}}": str(manifest.get("generation")),
        "{{HANDOFF_MANIFEST_PATH}}": manifest_path((manifest.get("outgoing") or {}).get("session_id")),
        "{{MODEL_ID}}": str((manifest.get("model") or {}).get("id")),
        "{{MODEL_DISPLAY_NAME}}": str((manifest.get("model") or {}).get("display_name")),
        "{{EFFORT_LEVEL}}": str(effort_display),
        "{{TRANSCRIPT_PATH}}": str((manifest.get("outgoing") or {}).get("transcript_path")),
        "{{WORKING_DIRECTORY}}": str((manifest.get("outgoing") or {}).get("current_dir")),
        "{{THRESHOLD}}": str(manifest.get("trigger", {}).get("configured_threshold")),
        "{{TH_VERSION}}": TERMINAL_HANDOFF_VERSION,
    }
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace(key, value)
    return rendered


FALLBACK_PROMPT_TEMPLATE = """TERMINAL HANDOFF SUCCESSOR

You are {{DISPLAY_NAME}}: generation {{GENERATION}} of Terminal Handoff chain
{{CHAIN_ID}}.
Parent session: {{PARENT_SESSION_ID}}
Handoff manifest: {{HANDOFF_MANIFEST_PATH}}
Required model: {{MODEL_ID}}
Required effort: {{EFFORT_LEVEL}}
Transfer state: {{TRANSFER_PATH}}

Your parent session may still be running and still own the work. Do not mutate
any repository file until your heartbeat has been validated, your repository
verification is complete, and the transfer state reads PARENT_STOP_REQUESTED or
TRANSFER_COMPLETE. Reading and verifying are always allowed.

Read the manifest, delegate parent-transcript analysis to a context-isolated
subagent, verify repository state independently, then produce a TERMINAL HANDOFF
CONTINUATION REPORT before continuing any work.
"""


def bootstrap_prompt(manifest, prompt_file):
    """Short argv-safe prompt. The full instructions live in `prompt_file`,
    which keeps them out of the process listing."""
    return (
        "TERMINAL HANDOFF SUCCESSOR - chain %s, generation %d. "
        "Read %s in full and follow it exactly before anything else. "
        "Mutate nothing until the transfer state authorises it. Manifest: %s."
        % (
            manifest.get("chain_id"),
            int(manifest.get("generation") or 1) + 1,
            prompt_file,
            manifest_path((manifest.get("outgoing") or {}).get("session_id")),
        )
    )


# ---------------------------------------------------------------------------
# Terminal launch (osascript)
# ---------------------------------------------------------------------------


def applescript_quote(value):
    """Quote a value for embedding in an AppleScript string literal."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


# Terminal Handoff configuration is carried into the successor's Terminal
# window so that a chain keeps the operator's settings. Apple Terminal starts a
# fresh login shell that does not inherit the launcher's environment.
PROPAGATED_ENV_RE = re.compile(r"^CLAUDE_TERMINAL_HANDOFF_[A-Z0-9_]+$")

# Set explicitly per handoff; never copied from the launcher's environment.
PER_HANDOFF_ENV = (
    "CLAUDE_TERMINAL_HANDOFF_MANIFEST",
    "CLAUDE_TERMINAL_HANDOFF_CHAIN_ID",
    "CLAUDE_TERMINAL_HANDOFF_GENERATION",
    "CLAUDE_TERMINAL_HANDOFF_PARENT_SESSION",
    "CLAUDE_TERMINAL_HANDOFF_BASE_NAME",
    "CLAUDE_TERMINAL_HANDOFF_DISPLAY_NAME",
    "CLAUDE_TERMINAL_HANDOFF_TRANSFER",
)


def propagated_environment(environ=None):
    """The Terminal Handoff settings a successor should inherit."""
    environ = os.environ if environ is None else environ
    carried = {}
    for key in sorted(environ):
        if key in PER_HANDOFF_ENV:
            continue
        if not PROPAGATED_ENV_RE.match(key):
            continue
        value = environ[key]
        if not isinstance(value, str) or "\x00" in value:
            continue
        carried[key] = value
    return carried


def build_launch_script(manifest, argv, workdir, manifest_file, transfer_file=None):
    """Generate the per-handoff shell script executed in the new Terminal window.

    Every value is single-quoted with shlex.quote. No eval, no interpolation of
    transcript content, no clipboard use.
    """
    display = manifest.get("display") or {}
    generation = int(manifest.get("generation") or 1)
    successor_name = display.get("successor_display_name") or ""
    lines = [
        "#!/bin/zsh",
        "# Terminal Handoff " + TERMINAL_HANDOFF_VERSION + " - successor launcher",
        "# Generated %s for chain %s generation %d"
        % (utc_stamp(), manifest.get("chain_id"), generation + 1),
        "set -e",
        "cd -- %s || { echo 'Terminal Handoff: working directory unavailable'; exit 1; }" % shlex.quote(workdir),
    ]
    for key, value in propagated_environment().items():
        lines.append("export %s=%s" % (key, shlex.quote(value)))
    lines += [
        "export CLAUDE_TERMINAL_HANDOFF_MANIFEST=%s" % shlex.quote(manifest_file),
        "export CLAUDE_TERMINAL_HANDOFF_CHAIN_ID=%s" % shlex.quote(str(manifest.get("chain_id"))),
        "export CLAUDE_TERMINAL_HANDOFF_GENERATION=%s" % shlex.quote(str(generation + 1)),
        "export CLAUDE_TERMINAL_HANDOFF_PARENT_SESSION=%s"
        % shlex.quote(str((manifest.get("outgoing") or {}).get("session_id"))),
        "export CLAUDE_TERMINAL_HANDOFF_BASE_NAME=%s" % shlex.quote(str(display.get("base_name") or "")),
        "export CLAUDE_TERMINAL_HANDOFF_DISPLAY_NAME=%s" % shlex.quote(str(successor_name)),
    ]
    if transfer_file:
        lines.append("export CLAUDE_TERMINAL_HANDOFF_TRANSFER=%s" % shlex.quote(transfer_file))
    lines += [
        "echo 'Terminal Handoff: generation %d of chain %s'"
        % (generation + 1, manifest.get("chain_id")),
        "exec " + " ".join(shlex.quote(part) for part in argv),
        "",
    ]
    return "\n".join(lines)


def launch_terminal(manifest, script_file, title, test_mode):
    """Open one macOS Terminal window running `script_file`."""
    command = "/bin/zsh -l %s" % shlex.quote(script_file)
    applescript = "\n".join(
        [
            'tell application "Terminal"',
            "    activate",
            "    do script " + applescript_quote(command),
            "    try",
            "        set custom title of front window to " + applescript_quote(title),
            "    end try",
            "end tell",
        ]
    )
    if test_mode:
        return {
            "launched": False,
            "test_mode": True,
            "applescript": applescript,
            "command": command,
        }
    try:
        proc = subprocess.Popen(
            ["/usr/bin/osascript", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out, err = proc.communicate(applescript.encode("utf-8"), timeout=45)
        return {
            "launched": proc.returncode == 0,
            "test_mode": False,
            "returncode": proc.returncode,
            "stdout": out.decode("utf-8", "replace").strip()[:500],
            "stderr": err.decode("utf-8", "replace").strip()[:500],
            "command": command,
        }
    except Exception as exc:
        return {"launched": False, "test_mode": False, "error": str(exc)[:500], "command": command}


# ---------------------------------------------------------------------------
# Parent Claude process identity and binding
# ---------------------------------------------------------------------------

# Terminal Handoff never uses pkill, killall, process-name pattern matching,
# process groups, front-window AppleScript targeting or SIGKILL. It binds one
# exact process at trigger time and re-proves that binding immediately before
# signalling it.
PS_BIN = "/bin/ps"
PS_TIMEOUT = 5.0
LSOF_CANDIDATES = ("/usr/sbin/lsof", "/usr/bin/lsof")
MAX_ANCESTRY_DEPTH = 24

# Only a process whose executable file is named exactly `claude` is ever
# considered a Claude Code session process.
PARENT_PROCESS_NAMES = ("claude",)

# Claude Code runs the status-line command through a shell, so the owning
# session process is a near ancestor. A Claude process further away than this
# is somebody else's session and is never bound.
MAX_BIND_DEPTH = 6

# The only signal Terminal Handoff ever sends. There is no escalation path.
PARENT_STOP_SIGNAL = signal.SIGTERM
PARENT_STOP_SIGNAL_NAME = "SIGTERM"


def _ps_field(pid, fmt):
    """Run `ps -o <fmt> -p <pid>`. `fmt` must end with the widest field.

    macOS truncates `comm` to 16 characters unless it is the final column, so
    it is always requested on its own.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    try:
        proc = subprocess.Popen(
            [PS_BIN, "-o", fmt, "-p", str(pid)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        out, _ = proc.communicate(timeout=PS_TIMEOUT)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    text = out.decode("utf-8", "replace").strip()
    return text or None


def process_identity(pid):
    """Stable identity for one live process, or None if it is not running.

    `start` is the process start time: together with the PID it distinguishes
    the bound process from any later process that reuses the same PID.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 1:
        return None
    head = _ps_field(pid, "ppid=,uid=,tty=,stat=,lstart=")
    if not head:
        return None
    tokens = head.split()
    if len(tokens) < 5:
        return None
    try:
        ppid = int(tokens[0])
        uid = int(tokens[1])
    except (TypeError, ValueError):
        return None
    state = tokens[3]
    if state.startswith("Z"):
        return None  # a zombie has already exited; it is not a live session
    command = (_ps_field(pid, "comm=") or "").strip()
    return {
        "pid": pid,
        "ppid": ppid,
        "uid": uid,
        "tty": tokens[2],
        "state": state,
        "start": " ".join(tokens[4:]),
        "command": command[:400],
        "name": os.path.basename(command)[:120],
    }


def process_cwd(pid):
    """Best-effort working directory of a live process. Never fatal."""
    lsof = None
    for candidate in LSOF_CANDIDATES:
        if os.path.exists(candidate):
            lsof = candidate
            break
    if lsof is None:
        return None
    try:
        proc = subprocess.Popen(
            [lsof, "-a", "-p", str(int(pid)), "-d", "cwd", "-Fn"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        out, _ = proc.communicate(timeout=PS_TIMEOUT)
    except Exception:
        return None
    for line in out.decode("utf-8", "replace").splitlines():
        if line.startswith("n"):
            return line[1:].strip() or None
    return None


def process_ancestry(pid=None, limit=MAX_ANCESTRY_DEPTH):
    """Walk the real process ancestry upwards from `pid` (default: self).

    The status-line process is a descendant of the Claude Code session process
    but not necessarily its direct child - Claude Code runs the status-line
    command through a shell - so the ancestry is traced rather than assumed.
    """
    chain = []
    try:
        current = os.getpid() if pid is None else int(pid)
    except (TypeError, ValueError):
        return chain
    seen = set()
    while current > 1 and len(chain) < limit and current not in seen:
        seen.add(current)
        identity = process_identity(current)
        if identity is None:
            break
        chain.append(identity)
        current = identity["ppid"]
    return chain


def bind_parent_claude_process(session_id, current_dir=None, start_pid=None):
    """Bind the exact Claude Code process that owns this status-line run.

    Returns `(binding, reason)`. `binding` is None when no Claude Code process
    could be proved, in which case the handoff still proceeds and the parent is
    simply never stopped.
    """
    chain = process_ancestry(start_pid)
    ancestry = [
        {"pid": item["pid"], "name": item["name"], "tty": item["tty"]} for item in chain
    ]
    uid = os.getuid()
    for depth, identity in enumerate(chain):
        if identity["name"] not in PARENT_PROCESS_NAMES:
            continue
        if depth > MAX_BIND_DEPTH:
            return None, (
                "the nearest Claude Code process is %d levels away, which is too "
                "distant to be this session" % depth
            )
        if identity["uid"] != uid:
            return None, "candidate parent %d belongs to another user" % identity["pid"]
        if identity["pid"] == os.getpid():
            return None, "refusing to bind this process as its own parent"
        observed_cwd = process_cwd(identity["pid"])
        if current_dir and observed_cwd:
            try:
                same = os.path.realpath(observed_cwd) == os.path.realpath(current_dir)
            except Exception:
                same = False
            if not same:
                # The candidate is running a different session's work. Binding
                # it could stop the wrong Claude session, so refuse.
                return None, (
                    "candidate parent %d is working in a different directory"
                    % identity["pid"]
                )
        return (
            {
                "pid": identity["pid"],
                "ppid": identity["ppid"],
                "uid": identity["uid"],
                "tty": identity["tty"],
                "start": identity["start"],
                "name": identity["name"],
                "command": identity["command"],
                "process_cwd": observed_cwd,
                "session_id": session_id,
                "session_current_dir": current_dir,
                "ancestry_depth": depth,
                "ancestry": ancestry,
                "bound_utc": utc_stamp(),
                "bound_by_pid": os.getpid(),
            },
            None,
        )
    return None, "no Claude Code process was found in this process's ancestry"


def verify_parent_binding(binding, chain_id=None, generation=None, session_id=None):
    """Re-prove that a binding still names the same live Claude process.

    Returns `(ok, reason)`. Fails closed on every mismatch: a reused PID, a
    changed executable, a different controlling terminal, a different user or a
    moved working directory all abort the shutdown.
    """
    if not isinstance(binding, dict):
        return False, "no parent process binding was recorded"
    try:
        pid = int(binding.get("pid"))
    except (TypeError, ValueError):
        return False, "binding has no usable pid"
    if pid <= 1:
        return False, "refusing to signal pid %d" % pid
    if pid == os.getpid():
        return False, "refusing to signal this process"
    if session_id and binding.get("session_id") and binding["session_id"] != session_id:
        return False, "binding belongs to a different session"
    if chain_id and binding.get("chain_id") and binding["chain_id"] != chain_id:
        return False, "binding belongs to a different chain"
    if (
        generation is not None
        and binding.get("generation") is not None
        and int(binding["generation"]) != int(generation)
    ):
        return False, "binding belongs to a different generation"
    identity = process_identity(pid)
    if identity is None:
        return False, "bound parent process %d is no longer running" % pid
    if not binding.get("start") or identity["start"] != binding.get("start"):
        return False, "pid %d has been reused: start time differs" % pid
    if identity["uid"] != os.getuid():
        return False, "pid %d now belongs to another user" % pid
    if identity["name"] not in PARENT_PROCESS_NAMES:
        return False, "pid %d is not a Claude Code process" % pid
    if binding.get("name") and identity["name"] != binding["name"]:
        return False, "pid %d has a different executable name" % pid
    if binding.get("tty") and identity["tty"] != binding["tty"]:
        return False, "pid %d is on a different terminal" % pid
    recorded_cwd = binding.get("process_cwd")
    if recorded_cwd:
        observed = process_cwd(pid)
        if observed and observed != recorded_cwd:
            return False, "pid %d has a different working directory" % pid
    return True, None


def parent_binding_path(session_id):
    return th_path("launching", "%s.parent.json" % session_id)


# ---------------------------------------------------------------------------
# Transfer of ownership: an auditable, atomic state machine
# ---------------------------------------------------------------------------

TRANSFER_LAUNCHING = "LAUNCHING"
TRANSFER_SUCCESSOR_VERIFIED = "SUCCESSOR_VERIFIED"
TRANSFER_PARENT_STOP_REQUESTED = "PARENT_STOP_REQUESTED"
TRANSFER_COMPLETE = "TRANSFER_COMPLETE"
TRANSFER_FAILED = "TRANSFER_FAILED"

TRANSFER_STATES = (
    TRANSFER_LAUNCHING,
    TRANSFER_SUCCESSOR_VERIFIED,
    TRANSFER_PARENT_STOP_REQUESTED,
    TRANSFER_COMPLETE,
    TRANSFER_FAILED,
)

# Before SUCCESSOR_VERIFIED the parent owns continuation. After
# PARENT_STOP_REQUESTED the successor owns it. There is exactly one boundary,
# and every transition is recorded.
TRANSFER_TRANSITIONS = {
    TRANSFER_LAUNCHING: (TRANSFER_SUCCESSOR_VERIFIED, TRANSFER_FAILED),
    TRANSFER_SUCCESSOR_VERIFIED: (TRANSFER_PARENT_STOP_REQUESTED, TRANSFER_FAILED),
    TRANSFER_PARENT_STOP_REQUESTED: (TRANSFER_COMPLETE, TRANSFER_FAILED),
    TRANSFER_COMPLETE: (),
    TRANSFER_FAILED: (),
}

TRANSFER_OWNER = {
    TRANSFER_LAUNCHING: "parent",
    TRANSFER_SUCCESSOR_VERIFIED: "parent",
    TRANSFER_PARENT_STOP_REQUESTED: "successor",
    TRANSFER_COMPLETE: "successor",
    TRANSFER_FAILED: "parent",
}

DEFAULT_HEARTBEAT_TIMEOUT = 300.0
DEFAULT_TRANSFER_POLL = 2.0
DEFAULT_STOP_GRACE = 20.0
DEFAULT_STOP_ATTEMPTS = 2
MAX_STOP_ATTEMPTS = 3


def stop_parent_enabled():
    """Parent shutdown is on by default; `...STOP_PARENT=0` disables it."""
    raw = os.environ.get("CLAUDE_TERMINAL_HANDOFF_STOP_PARENT")
    if raw is None or raw.strip() == "":
        return True
    return raw.strip().lower() in TRUTHY


def heartbeat_timeout():
    value = env_float("CLAUDE_TERMINAL_HANDOFF_HEARTBEAT_TIMEOUT", DEFAULT_HEARTBEAT_TIMEOUT)
    return value if value > 0 else DEFAULT_HEARTBEAT_TIMEOUT


def transfer_poll_seconds():
    value = env_float("CLAUDE_TERMINAL_HANDOFF_TRANSFER_POLL", DEFAULT_TRANSFER_POLL)
    if value <= 0 or value > 60:
        return DEFAULT_TRANSFER_POLL
    return value


def stop_grace_seconds():
    value = env_float("CLAUDE_TERMINAL_HANDOFF_STOP_GRACE", DEFAULT_STOP_GRACE)
    return value if value > 0 else DEFAULT_STOP_GRACE


def stop_attempts():
    value = env_int("CLAUDE_TERMINAL_HANDOFF_STOP_ATTEMPTS", DEFAULT_STOP_ATTEMPTS)
    if value < 1:
        return 1
    return min(value, MAX_STOP_ATTEMPTS)


def transfer_path(parent_session_id):
    return th_path("transfers", "%s.json" % parent_session_id)


def read_transfer(path):
    data = read_json(path)
    return data if isinstance(data, dict) else None


def build_transfer_record(manifest, binding, now=None):
    now = now if now is not None else time.time()
    outgoing = manifest.get("outgoing") or {}
    successor = manifest.get("successor") or {}
    display = manifest.get("display") or {}
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "terminal_handoff_version": TERMINAL_HANDOFF_VERSION,
        "state": TRANSFER_LAUNCHING,
        "owner": TRANSFER_OWNER[TRANSFER_LAUNCHING],
        "chain_id": manifest.get("chain_id"),
        "parent_generation": manifest.get("generation"),
        "successor_generation": successor.get("generation"),
        "parent_session_id": outgoing.get("session_id"),
        "parent_display_name": display.get("outgoing_display_name"),
        "successor_display_name": display.get("successor_display_name"),
        "manifest_path": manifest_path(outgoing.get("session_id")),
        "parent_process": binding,
        "parent_process_bound": bool(binding),
        "expected_successor": {
            "model_id": (manifest.get("model") or {}).get("id"),
            "effort_level": (manifest.get("effort") or {}).get("level"),
            "effort_available": bool((manifest.get("effort") or {}).get("available")),
            "current_dir": successor.get("expected_current_dir"),
            "chain_id": manifest.get("chain_id"),
            "generation": successor.get("generation"),
            "display_name": display.get("successor_display_name"),
        },
        "successor": {},
        "stop": {
            "enabled": stop_parent_enabled(),
            "signal": PARENT_STOP_SIGNAL_NAME,
            "escalates": False,
            "attempts": 0,
        },
        "created_utc": utc_stamp(),
        "created_epoch": now,
        "history": [
            {"state": TRANSFER_LAUNCHING, "ts": utc_stamp(), "reason": "successor launch started"}
        ],
    }


def transfer_transition(path, target, reason=None, **fields):
    """Atomically move a transfer record to `target`.

    Returns `(ok, record)`. An illegal or repeated transition is refused, which
    is what makes duplicate status-line invocations and duplicate supervisors
    unable to request a second shutdown.
    """
    if target not in TRANSFER_STATES:
        return False, None
    outcome = {"ok": False, "record": None}

    def mutate(data):
        current = data.get("state")
        if current not in TRANSFER_STATES:
            data["state"] = TRANSFER_LAUNCHING
            current = TRANSFER_LAUNCHING
        if target not in TRANSFER_TRANSITIONS.get(current, ()):
            outcome["ok"] = False
            outcome["record"] = dict(data)
            return
        data["state"] = target
        data["owner"] = TRANSFER_OWNER[target]
        data["updated_utc"] = utc_stamp()
        for key, value in fields.items():
            data[key] = value
        history = data.setdefault("history", [])
        history.append(
            {"state": target, "ts": utc_stamp(), "reason": reason, "from": current, "pid": os.getpid()}
        )
        del history[:-40]
        outcome["ok"] = True
        outcome["record"] = dict(data)

    if not os.path.isfile(path):
        return False, None
    update_json_locked(path, mutate)
    if outcome["ok"]:
        log_event(
            "transfer_state",
            transfer=os.path.basename(path),
            state=target,
            reason=reason,
            chain_id=(outcome["record"] or {}).get("chain_id"),
            parent_session_id=(outcome["record"] or {}).get("parent_session_id"),
        )
    return outcome["ok"], outcome["record"]


def update_transfer_fields(path, **fields):
    """Record observations without changing the transfer state."""
    if not os.path.isfile(path):
        return None

    def mutate(data):
        for key, value in fields.items():
            data[key] = value
        data["updated_utc"] = utc_stamp()

    return update_json_locked(path, mutate)


# ---------------------------------------------------------------------------
# Trigger claim and launch pipeline
# ---------------------------------------------------------------------------


def claim_trigger(session_id):
    """Atomically claim the one-shot trigger for this session.

    Uses O_CREAT|O_EXCL so concurrent status-line processes produce exactly one
    launch. Keyed by session_id, never by PID, cwd or generation, so a successor
    never inherits its parent's claim.
    """
    ensure_dirs()
    path = th_path("triggered", session_id)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            return False
        raise
    try:
        os.write(
            fd,
            json.dumps({"claimed_utc": utc_stamp(), "pid": os.getpid()}, sort_keys=True).encode(
                "utf-8"
            ),
        )
    finally:
        os.close(fd)
    return True


def set_state(session_id, state, **extra):
    """Record the handoff lifecycle state: eligible -> launching -> launched ->
    successor_started -> completed (or failed)."""
    ensure_dirs()
    record = {"session_id": session_id, "state": state, "ts": utc_stamp()}
    record.update(extra)
    directory = {
        "launching": "launching",
        "launched": "launching",
        "successor_started": "launching",
        "completed": "completed",
        "failed": "failed",
    }.get(state, "launching")
    write_json_private(th_path(directory, "%s.json" % session_id), record)
    if state in ("completed", "failed"):
        stale = th_path("launching", "%s.json" % session_id)
        if os.path.exists(stale):
            try:
                os.unlink(stale)
            except OSError:
                pass
    return record


MAX_LAUNCH_RETRIES = 2


def fail_launch(session_id, reason, detail=None, allow_retry=True, manifest_file=None, state=None):
    """Record a failed launch, retain diagnostics, and permit a bounded retry.

    Never marks the handoff completed. Releases the one-shot trigger claim only
    while the retry budget lasts, and applies the launch cooldown so a failing
    session cannot spin.
    """
    if manifest_file and os.path.isfile(manifest_file):
        update_manifest(
            manifest_file,
            lambda m: m.setdefault("successor", {}).update(
                {"launch_state": state or "failed", "failure_reason": reason}
            ),
        )
    set_state(session_id, "failed", reason=reason, detail=(str(detail)[:300] if detail else None))

    retry_path = th_path("failed", "%s.retries" % session_id)
    try:
        with open(retry_path, "r") as handle:
            retries = int((handle.read() or "0").strip() or "0")
    except Exception:
        retries = 0

    released = False
    if allow_retry and retries < MAX_LAUNCH_RETRIES:
        write_private(retry_path, str(retries + 1))
        try:
            os.unlink(th_path("triggered", session_id))
            released = True
        except OSError:
            released = False
        # Apply the cooldown between retries so failures cannot spin.
        try:
            write_json_private(
                cooldown_file(),
                {"epoch": time.time(), "ts": utc_stamp(), "session_id": session_id, "failed": True},
            )
        except Exception:
            pass
    log_event(
        "launch_failed",
        session_id=session_id,
        reason=reason,
        detail=(str(detail)[:300] if detail else None),
        retries=retries + (1 if released else 0),
        retry_released=released,
        retry_exhausted=(not released and allow_retry),
    )
    return released


def perform_launch(payload_file):
    """Detached child process: build manifest, render prompt, open Terminal.

    Runs independently of the short-lived status-line process.
    """
    ensure_dirs()
    # Test hook: proves the detached launcher outlives the short-lived
    # status-line process. Never set in normal operation.
    delay = env_float("CLAUDE_TERMINAL_HANDOFF_LAUNCH_DELAY", 0.0)
    if delay > 0:
        time.sleep(min(delay, 30.0))
    payload = read_json(payload_file)
    if payload is None:
        log_event("launch_abort", reason="payload unreadable", payload_file=payload_file)
        return 2

    facts = extract_facts(payload, validate_files=True)
    if not facts.ok:
        log_event("launch_abort", reason="validation failed", errors=facts.errors)
        return 3

    decision = decide(payload, validate_files=True, record=False)
    session_id = facts.session_id
    chain_id, generation, parent_manifest, parent_session = chain_identity(
        facts.session_id, facts.session_name
    )
    base_name, base_source = resolve_base_display_name(chain_id, generation, facts.session_name)
    decision["chain_id"] = chain_id
    decision["generation"] = generation
    decision["parent_manifest"] = parent_manifest
    decision["parent_session_id"] = parent_session
    decision["base_display_name"] = base_name
    decision["base_name_source"] = base_source

    test_mode = env_flag("CLAUDE_TERMINAL_HANDOFF_TEST_MODE")
    set_state(session_id, "launching", chain_id=chain_id, generation=generation)
    log_event(
        "launch_begin",
        session_id=session_id,
        chain_id=chain_id,
        generation=generation,
        percent=facts.percent,
        model_id=facts.model_id,
        effort_level=facts.effort_level,
        effort_available=facts.effort_available,
        test_mode=test_mode,
    )

    manifest = build_manifest(facts, decision)
    mpath = manifest_path(session_id)
    write_json_private(mpath, manifest)

    # Re-check storm protection immediately before the real launch.
    storm_max, storm_window, _, _ = storm_settings()
    open_now, circuit = circuit_is_open()
    recent = recent_launch_times(storm_window)
    if open_now or len(recent) >= storm_max:
        if not open_now:
            trip_circuit("%d launches within %ds" % (len(recent), storm_window), len(recent))
        fail_launch(
            session_id,
            "storm circuit breaker open",
            manifest_file=mpath,
            state="blocked_circuit_open",
        )
        return 4

    claude_bin = find_claude_executable()
    if not claude_bin:
        fail_launch(
            session_id,
            "claude executable not found",
            manifest_file=mpath,
            state="failed_no_claude",
        )
        return 5

    workdir = facts.current_dir
    if not workdir or not os.path.isdir(workdir) or not _safe_path_for_shell(workdir):
        fail_launch(
            session_id,
            "working directory invalid or unsafe",
            detail=workdir,
            manifest_file=mpath,
            state="failed_bad_workdir",
        )
        return 6

    prompt_file = th_path("prompts", "successor-%s.md" % session_id)
    try:
        write_private(prompt_file, render_successor_prompt(manifest))
    except Exception as exc:
        fail_launch(session_id, "prompt render failed", detail=exc, manifest_file=mpath)
        return 7

    try:
        argv = build_launch_argv(manifest, claude_bin, bootstrap_prompt(manifest, prompt_file))
    except ValueError as exc:
        # The reported model or effort cannot be launched. Never fall back to a
        # different model or effort: fail visibly and allow a controlled retry.
        fail_launch(
            session_id,
            "model or effort could not be preserved: %s" % exc,
            manifest_file=mpath,
            state="failed_unpreservable",
        )
        return 8

    problems = assert_launch_argv_safe(argv)
    if problems:
        fail_launch(session_id, "unsafe launch argv: %s" % "; ".join(problems), manifest_file=mpath)
        return 9

    display = manifest.get("display") or {}
    binding = read_json(parent_binding_path(session_id))
    if isinstance(binding, dict):
        binding["chain_id"] = chain_id
        binding["generation"] = generation
    else:
        binding = None

    transfer_file = transfer_path(session_id)
    write_json_private(transfer_file, build_transfer_record(manifest, binding))

    script_file = th_path("launching", "%s.launch.sh" % session_id)
    write_private(
        script_file,
        build_launch_script(manifest, argv, workdir, mpath, transfer_file),
        mode=0o700,
    )

    title = display.get("successor_display_name") or (
        "Terminal Handoff, Generation %d" % (generation + 1)
    )
    record_chain_generation(
        chain_id,
        generation,
        session_id=session_id,
        display_name=display.get("outgoing_display_name"),
        base_name=display.get("base_name"),
        source=display.get("base_name_source"),
    )
    record_chain_generation(
        chain_id, generation + 1, display_name=display.get("successor_display_name")
    )
    record_launch(session_id, chain_id, generation)
    result = launch_terminal(manifest, script_file, title, test_mode)

    launch_record = {
        "session_id": session_id,
        "chain_id": chain_id,
        "generation": generation,
        "successor_generation": generation + 1,
        "successor_display_name": display.get("successor_display_name"),
        "outgoing_display_name": display.get("outgoing_display_name"),
        "base_display_name": display.get("base_name"),
        "base_name_source": display.get("base_name_source"),
        "argv": argv,
        "argv_redacted_prompt": argv[:-1] + ["<bootstrap prompt in %s>" % prompt_file],
        "script_file": script_file,
        "prompt_file": prompt_file,
        "manifest": mpath,
        "transfer_file": transfer_file,
        "parent_process_bound": bool(binding),
        "parent_pid": (binding or {}).get("pid"),
        "title": title,
        "test_mode": test_mode,
        "result": dict((k, v) for k, v in result.items() if k != "applescript"),
        "applescript": result.get("applescript"),
        "ts": utc_stamp(),
    }
    # The launch record is written last on every path, so its appearance means
    # every other record for this handoff - manifest, lifecycle state, transfer
    # - has already been written. Writing it first left a window in which the
    # record existed but the manifest still said `eligible`.
    def record_outcome(code):
        write_json_private(th_path("completed", "%s.launch.json" % session_id), launch_record)
        return code

    if test_mode:
        update_manifest(
            mpath, lambda m: m["successor"].update({"launch_state": "simulated_test_mode"})
        )
        set_state(session_id, "completed", reason="test mode: launch simulated", test_mode=True)
        update_transfer_fields(transfer_file, supervisor_spawned=False, test_mode=True)
        log_event(
            "launch_simulated",
            session_id=session_id,
            chain_id=chain_id,
            generation=generation,
            successor_display_name=display.get("successor_display_name"),
            argv=launch_record["argv_redacted_prompt"],
        )
        return record_outcome(0)

    if result.get("launched"):
        update_manifest(mpath, lambda m: m["successor"].update({"launch_state": "launched"}))
        set_state(session_id, "launched", reason="Terminal window opened; awaiting successor heartbeat")
        # With parent shutdown disabled there is nothing to supervise: the
        # transfer still records the successor's verification, and the parent
        # keeps running by choice rather than by failure.
        supervised = False
        if stop_parent_enabled():
            supervised = spawn_supervisor(transfer_file)
            if not supervised:
                transfer_transition(
                    transfer_file,
                    TRANSFER_FAILED,
                    reason=(
                        "the shutdown supervisor could not be started; "
                        "the parent is left running"
                    ),
                    parent_stopped=False,
                )
        update_transfer_fields(transfer_file, supervisor_spawned=bool(supervised))
        log_event(
            "launch_confirmed",
            session_id=session_id,
            chain_id=chain_id,
            generation=generation,
            model_id=facts.model_id,
            effort_level=facts.effort_level,
            successor_display_name=display.get("successor_display_name"),
            parent_process_bound=bool(binding),
            supervisor_spawned=bool(supervised),
        )
        return record_outcome(0)

    transfer_transition(
        transfer_file,
        TRANSFER_FAILED,
        reason="the successor Terminal window could not be opened; the parent is left running",
        parent_stopped=False,
    )
    fail_launch(
        session_id,
        "osascript could not open the Terminal window",
        detail=result.get("stderr") or result.get("error"),
        manifest_file=mpath,
    )
    return record_outcome(10)


def spawn_launcher(payload, parent_binding=None):
    """Write the payload securely and spawn the detached launch process.

    `parent_binding` is the exact Claude Code process bound by the status-line
    invocation that claimed the trigger. It is recorded here because process
    ancestry is only visible from that process, never from the detached
    launcher, which starts its own session.
    """
    ensure_dirs()
    session_id = _dget(payload, "session_id") or uuid.uuid4().hex
    payload_file = th_path("launching", "%s.payload.json" % session_id)
    write_json_private(payload_file, payload)
    if parent_binding:
        write_json_private(parent_binding_path(session_id), parent_binding)
    self_path = os.path.abspath(__file__)
    try:
        devnull = open(os.devnull, "wb")
        subprocess.Popen(
            [preferred_python(), self_path, "launch", "--payload", payload_file],
            stdin=subprocess.DEVNULL,
            stdout=devnull,
            stderr=devnull,
            start_new_session=True,
            close_fds=True,
        )
        return True
    except Exception as exc:
        log_event("spawn_failed", session_id=session_id, error=str(exc)[:300])
        return False


# ---------------------------------------------------------------------------
# Successor heartbeat: the transfer-of-ownership gate
# ---------------------------------------------------------------------------

# Two heartbeats are required. The first proves a session exists; the second
# proves it is alive and reporting its own live context percentage.
HEARTBEATS_REQUIRED = 2
MAX_HEARTBEATS = 8

SUCCESSOR_CHECK_NAMES = (
    "session_id_present",
    "session_id_is_fresh",
    "session_id_unused",
    "model_matches",
    "effort_matches",
    "cwd_matches",
    "chain_matches",
    "generation_matches",
    "context_percentage_live",
)


def successor_expectations_from_env():
    """What this session was told it is, by the Terminal Handoff launcher."""
    chain = os.environ.get("CLAUDE_TERMINAL_HANDOFF_CHAIN_ID", "").strip() or None
    generation = env_int("CLAUDE_TERMINAL_HANDOFF_GENERATION", 0) or None
    transfer = os.environ.get("CLAUDE_TERMINAL_HANDOFF_TRANSFER", "").strip() or None
    return chain, generation, transfer


def evaluate_successor_checks(manifest, facts, env_chain, env_generation, parent_session_id):
    """Every condition that must hold before a parent may be stopped.

    A single false value leaves the parent running.
    """
    successor = manifest.get("successor") or {}
    expected_model = (manifest.get("model") or {}).get("id")
    expected_effort = (manifest.get("effort") or {}).get("level")
    expected_effort_available = bool((manifest.get("effort") or {}).get("available"))
    expected_dir = successor.get("expected_current_dir")
    expected_chain = manifest.get("chain_id")
    try:
        expected_generation = int(successor.get("generation") or 0)
    except (TypeError, ValueError):
        expected_generation = 0
    prior_generation = chain_generation_for_session(expected_chain, facts.session_id)
    return {
        "session_id_present": bool(facts.session_id),
        "session_id_is_fresh": bool(facts.session_id) and facts.session_id != parent_session_id,
        "session_id_unused": prior_generation is None
        or int(prior_generation) == expected_generation,
        "model_matches": bool(expected_model) and facts.model_id == expected_model,
        "effort_matches": facts.effort_level == expected_effort
        and bool(facts.effort_available) == expected_effort_available,
        "cwd_matches": bool(expected_dir) and facts.current_dir == expected_dir,
        "chain_matches": bool(env_chain) and bool(expected_chain) and env_chain == expected_chain,
        "generation_matches": bool(env_generation)
        and expected_generation > 0
        and int(env_generation) == expected_generation,
        "context_percentage_live": bool(facts.percent_valid),
    }


def resolve_transfer_file(env_transfer, parent_session_id):
    if env_transfer and os.path.isfile(env_transfer):
        return env_transfer
    if parent_session_id:
        candidate = transfer_path(parent_session_id)
        if os.path.isfile(candidate):
            return candidate
    return None


def successor_heartbeat(facts):
    """If this session is a Terminal Handoff successor, prove it to the parent.

    The parent's manifest is only marked `completed`, and the transfer is only
    moved to SUCCESSOR_VERIFIED, once this session has reported its own fresh
    session ID, the required model, the required effort, the required working
    directory, the correct chain, the correct generation and its own live
    context percentage. Any failure leaves the parent fully operational.
    """
    parent_manifest = os.environ.get("CLAUDE_TERMINAL_HANDOFF_MANIFEST", "").strip()
    if not parent_manifest or not facts.session_id:
        return None
    if not os.path.isfile(parent_manifest):
        return None

    marker = th_path("state", "heartbeat-%s.json" % facts.session_id)
    existing = read_json(marker, {}) or {}
    beats = int(existing.get("beats", 0)) + 1
    if beats > MAX_HEARTBEATS:
        return existing.get("state")

    data = read_json(parent_manifest, {}) or {}
    parent_session = (data.get("outgoing") or {}).get("session_id")
    if parent_session and parent_session == facts.session_id:
        # Refuse to treat a session as its own successor.
        log_event("heartbeat_rejected", session_id=facts.session_id, reason="parent==successor")
        return None

    env_chain, env_generation, env_transfer = successor_expectations_from_env()
    checks = evaluate_successor_checks(data, facts, env_chain, env_generation, parent_session)
    failed = sorted(name for name in SUCCESSOR_CHECK_NAMES if not checks.get(name))

    if failed:
        state = "successor_mismatch"
    elif beats >= HEARTBEATS_REQUIRED:
        state = "completed"
    else:
        state = "successor_started"

    def mutate(manifest):
        successor = manifest.setdefault("successor", {})
        successor["session_id"] = facts.session_id
        successor["launch_state"] = state
        successor["observed_model_id"] = facts.model_id
        successor["observed_effort_level"] = facts.effort_level
        successor["observed_current_dir"] = facts.current_dir
        successor["observed_context_percentage"] = facts.percent
        successor["observed_display_name"] = facts.session_name
        successor["observed_chain_id"] = env_chain
        successor["observed_generation"] = env_generation
        successor["heartbeats"] = beats
        successor["checks"] = checks
        successor["failed_checks"] = failed
        successor["model_matches"] = checks["model_matches"]
        successor["effort_matches"] = checks["effort_matches"]
        successor["cwd_matches"] = checks["cwd_matches"]
        successor["session_id_is_fresh"] = checks["session_id_is_fresh"]
        if not successor.get("first_heartbeat_utc"):
            successor["first_heartbeat_utc"] = utc_stamp()
        if state == "completed":
            successor["confirmed_utc"] = utc_stamp()

    update_manifest(parent_manifest, mutate)
    write_json_private(
        marker,
        {
            "session_id": facts.session_id,
            "parent_manifest": parent_manifest,
            "beats": beats,
            "state": state,
            "failed_checks": failed,
            "ts": utc_stamp(),
        },
    )

    transfer_file = resolve_transfer_file(env_transfer, parent_session)
    if state == "completed":
        record_chain_generation(
            data.get("chain_id"),
            env_generation or (data.get("successor") or {}).get("generation") or 2,
            session_id=facts.session_id,
            display_name=facts.session_name,
        )
        if transfer_file:
            transfer_transition(
                transfer_file,
                TRANSFER_SUCCESSOR_VERIFIED,
                reason=(
                    "successor heartbeat validated: fresh session ID, required model, "
                    "required effort, correct working directory, correct chain and generation"
                ),
                successor={
                    "session_id": facts.session_id,
                    "display_name": facts.session_name,
                    "model_id": facts.model_id,
                    "effort_level": facts.effort_level,
                    "current_dir": facts.current_dir,
                    "chain_id": env_chain,
                    "generation": env_generation,
                    "context_percentage": facts.percent,
                    "heartbeats": beats,
                    "checks": checks,
                    "verified_utc": utc_stamp(),
                },
            )
    elif failed and transfer_file:
        update_transfer_fields(
            transfer_file,
            successor_rejected={
                "session_id": facts.session_id,
                "failed_checks": failed,
                "checks": checks,
                "observed_utc": utc_stamp(),
            },
        )

    if parent_session:
        set_state(parent_session, state, successor_session_id=facts.session_id)
    log_event(
        "successor_heartbeat",
        session_id=facts.session_id,
        parent_session_id=parent_session,
        state=state,
        beats=beats,
        model_id=facts.model_id,
        effort_level=facts.effort_level,
        failed_checks=failed,
    )
    return state


# ---------------------------------------------------------------------------
# Parent shutdown supervisor
# ---------------------------------------------------------------------------


def supervisor_lock_path(parent_session_id):
    return th_path("transfers", "%s.supervisor.lock" % parent_session_id)


def claim_supervisor(parent_session_id):
    """One supervisor per transfer, claimed atomically.

    Duplicate status-line invocations, a retried launch or a second supervisor
    process can therefore never produce a second shutdown attempt.
    """
    ensure_dirs()
    path = supervisor_lock_path(parent_session_id)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            return False
        raise
    try:
        os.write(
            fd,
            json.dumps({"pid": os.getpid(), "claimed_utc": utc_stamp()}, sort_keys=True).encode(
                "utf-8"
            ),
        )
    finally:
        os.close(fd)
    return True


def parent_process_gone(binding):
    """True when the exact bound process is no longer running."""
    ok, _ = verify_parent_binding(binding)
    return not ok


def request_parent_stop(path, record):
    """Send one graceful stop request to the exact bound parent process.

    Never SIGKILL, never a process group, never a name match. If the binding
    cannot be re-proved, nothing is signalled and the transfer fails closed
    with the parent left fully operational.
    """
    binding = record.get("parent_process")
    chain_id = record.get("chain_id")
    generation = record.get("parent_generation")
    parent_session_id = record.get("parent_session_id")

    ok, reason = verify_parent_binding(
        binding, chain_id=chain_id, generation=generation, session_id=parent_session_id
    )
    if not ok:
        transfer_transition(
            path,
            TRANSFER_FAILED,
            reason="parent process identity could not be re-proved: %s" % reason,
            parent_stopped=False,
        )
        log_event(
            "parent_stop_refused",
            parent_session_id=parent_session_id,
            chain_id=chain_id,
            reason=reason,
        )
        return False, reason

    moved, _ = transfer_transition(
        path,
        TRANSFER_PARENT_STOP_REQUESTED,
        reason="successor verified; requesting graceful parent shutdown",
        stop_requested_utc=utc_stamp(),
    )
    if not moved:
        # Another supervisor already owns the shutdown, or the transfer moved
        # on. Never signal twice.
        log_event(
            "parent_stop_skipped",
            parent_session_id=parent_session_id,
            reason="transfer was not in %s" % TRANSFER_SUCCESSOR_VERIFIED,
        )
        return False, "transfer already past SUCCESSOR_VERIFIED"

    simulated = env_flag("CLAUDE_TERMINAL_HANDOFF_TEST_MODE") or env_flag(
        "CLAUDE_TERMINAL_HANDOFF_STOP_DRY_RUN"
    )
    pid = int(binding["pid"])
    attempts = 0
    grace = stop_grace_seconds()
    budget = stop_attempts()

    if simulated:
        update_transfer_fields(
            path,
            stop={
                "enabled": True,
                "signal": PARENT_STOP_SIGNAL_NAME,
                "escalates": False,
                "attempts": 0,
                "simulated": True,
                "pid": pid,
            },
        )
        transfer_transition(
            path,
            TRANSFER_COMPLETE,
            reason="test mode: parent shutdown simulated, no signal sent",
            parent_stopped=False,
            parent_stop_simulated=True,
        )
        log_event("parent_stop_simulated", parent_session_id=parent_session_id, pid=pid)
        return True, None

    while attempts < budget:
        # Re-prove immediately before every signal: the identity check and the
        # signal must not be separated by a wait.
        ok, reason = verify_parent_binding(
            binding, chain_id=chain_id, generation=generation, session_id=parent_session_id
        )
        if not ok:
            if attempts > 0:
                break  # the parent exited between attempts: that is success
            transfer_transition(
                path,
                TRANSFER_FAILED,
                reason="parent process identity changed before signalling: %s" % reason,
                parent_stopped=False,
            )
            log_event("parent_stop_refused", parent_session_id=parent_session_id, reason=reason)
            return False, reason
        attempts += 1
        try:
            os.kill(pid, PARENT_STOP_SIGNAL)
        except OSError as exc:
            transfer_transition(
                path,
                TRANSFER_FAILED,
                reason="could not signal parent pid %d: %s" % (pid, exc),
                parent_stopped=False,
            )
            log_event(
                "parent_stop_failed", parent_session_id=parent_session_id, pid=pid, error=str(exc)
            )
            return False, str(exc)
        log_event(
            "parent_stop_signalled",
            parent_session_id=parent_session_id,
            chain_id=chain_id,
            pid=pid,
            signal=PARENT_STOP_SIGNAL_NAME,
            attempt=attempts,
        )
        deadline = time.time() + grace
        while time.time() < deadline:
            if parent_process_gone(binding):
                update_transfer_fields(
                    path,
                    stop={
                        "enabled": True,
                        "signal": PARENT_STOP_SIGNAL_NAME,
                        "escalates": False,
                        "attempts": attempts,
                        "pid": pid,
                    },
                )
                transfer_transition(
                    path,
                    TRANSFER_COMPLETE,
                    reason="parent Claude session stopped gracefully; its Terminal remains open",
                    parent_stopped=True,
                    parent_stopped_utc=utc_stamp(),
                )
                set_state(
                    parent_session_id,
                    "completed",
                    reason="successor verified; parent stopped gracefully",
                    parent_stopped=True,
                )
                log_event(
                    "parent_stopped",
                    parent_session_id=parent_session_id,
                    chain_id=chain_id,
                    pid=pid,
                    attempts=attempts,
                )
                return True, None
            time.sleep(min(0.5, transfer_poll_seconds()))

    if parent_process_gone(binding):
        transfer_transition(
            path,
            TRANSFER_COMPLETE,
            reason="parent Claude session stopped gracefully",
            parent_stopped=True,
            parent_stopped_utc=utc_stamp(),
        )
        log_event("parent_stopped", parent_session_id=parent_session_id, pid=pid, attempts=attempts)
        return True, None

    # The parent did not exit. Terminal Handoff does not escalate: it records
    # the failure visibly and leaves the parent running.
    update_transfer_fields(
        path,
        stop={
            "enabled": True,
            "signal": PARENT_STOP_SIGNAL_NAME,
            "escalates": False,
            "attempts": attempts,
            "pid": pid,
            "unconfirmed": True,
        },
    )
    transfer_transition(
        path,
        TRANSFER_FAILED,
        reason=(
            "parent pid %d did not exit after %d %s request(s); it is still running and "
            "Terminal Handoff will not escalate" % (pid, attempts, PARENT_STOP_SIGNAL_NAME)
        ),
        parent_stopped=False,
    )
    log_event(
        "parent_stop_unconfirmed",
        parent_session_id=parent_session_id,
        chain_id=chain_id,
        pid=pid,
        attempts=attempts,
    )
    return False, "parent did not exit"


def supervise_transfer(path, wait=True):
    """Wait for a verified successor heartbeat, then stop the exact parent.

    Runs detached from the status-line process. Every exit path is recorded in
    the transfer record and the log.
    """
    ensure_dirs()
    record = read_transfer(path)
    if record is None:
        log_event("supervisor_abort", reason="transfer record unreadable", transfer=path)
        return 2

    parent_session_id = record.get("parent_session_id")
    if not parent_session_id:
        log_event("supervisor_abort", reason="transfer record has no parent session")
        return 2
    if not claim_supervisor(parent_session_id):
        log_event("supervisor_skipped", parent_session_id=parent_session_id, reason="already claimed")
        return 0

    log_event(
        "supervisor_started",
        parent_session_id=parent_session_id,
        chain_id=record.get("chain_id"),
        successor_display_name=record.get("successor_display_name"),
        stop_enabled=bool((record.get("stop") or {}).get("enabled")),
    )

    if not (record.get("stop") or {}).get("enabled", True):
        transfer_transition(
            path,
            TRANSFER_FAILED,
            reason="parent shutdown is disabled by configuration; parent left running",
            parent_stopped=False,
        )
        return 0

    if not record.get("parent_process_bound"):
        transfer_transition(
            path,
            TRANSFER_FAILED,
            reason=(
                "the parent Claude process was not bound at trigger time; "
                "the parent is left fully operational"
            ),
            parent_stopped=False,
        )
        log_event("parent_stop_refused", parent_session_id=parent_session_id, reason="unbound")
        return 3

    deadline = time.time() + heartbeat_timeout()
    poll = transfer_poll_seconds()
    while wait:
        record = read_transfer(path) or record
        state = record.get("state")
        if state == TRANSFER_SUCCESSOR_VERIFIED:
            break
        if state in (TRANSFER_COMPLETE, TRANSFER_FAILED, TRANSFER_PARENT_STOP_REQUESTED):
            log_event(
                "supervisor_exit", parent_session_id=parent_session_id, state=state
            )
            return 0
        if time.time() >= deadline:
            transfer_transition(
                path,
                TRANSFER_FAILED,
                reason=(
                    "no verified successor heartbeat within %.0fs; the parent session is "
                    "left fully operational" % heartbeat_timeout()
                ),
                parent_stopped=False,
            )
            log_event(
                "successor_heartbeat_timeout",
                parent_session_id=parent_session_id,
                chain_id=record.get("chain_id"),
                timeout_seconds=heartbeat_timeout(),
            )
            return 4
        time.sleep(poll)

    record = read_transfer(path) or record
    if record.get("state") != TRANSFER_SUCCESSOR_VERIFIED:
        return 0
    ok, _ = request_parent_stop(path, record)
    return 0 if ok else 5


def spawn_supervisor(transfer_file):
    """Spawn the detached shutdown supervisor for one transfer."""
    try:
        devnull = open(os.devnull, "wb")
        subprocess.Popen(
            [preferred_python(), os.path.abspath(__file__), "supervise", "--transfer", transfer_file],
            stdin=subprocess.DEVNULL,
            stdout=devnull,
            stderr=devnull,
            start_new_session=True,
            close_fds=True,
        )
        return True
    except Exception as exc:
        log_event("supervisor_spawn_failed", transfer=transfer_file, error=str(exc)[:300])
        return False


# ---------------------------------------------------------------------------
# Status line rendering
# ---------------------------------------------------------------------------


def run_wrapped_statusline(command, raw_input_bytes):
    """Run a pre-existing status-line command, feeding it the identical JSON.

    `command` comes from the Claude settings file that already configured it;
    it is executed exactly as Claude Code itself would, via `/bin/sh -c`, with
    the same stdin bytes. Its stdout is preserved byte-for-byte.
    """
    if not command:
        return b""
    try:
        proc = subprocess.Popen(
            ["/bin/sh", "-c", command],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        out, _ = proc.communicate(raw_input_bytes, timeout=WRAPPED_STATUSLINE_TIMEOUT)
        return out
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        return b""


def badge_for(decision):
    state = decision.get("state")
    if state == "below":
        percent = decision.get("percent")
        if percent is None:
            return "TH ready"
        return "TH %d%%" % int(percent)
    return BADGES.get(state, "TH blocked")


def render_default_statusline(payload, decision):
    parts = []
    model = _dget(payload, "model", "display_name") or _dget(payload, "model", "id")
    if model:
        parts.append(str(model))
    effort = _dget(payload, "effort", "level")
    if effort:
        parts.append(str(effort))
    percent = decision.get("percent")
    if percent is not None:
        parts.append("ctx %d%%" % int(percent))
    else:
        parts.append("ctx --")
    directory = _dget(payload, "workspace", "current_dir")
    if directory:
        parts.append(os.path.basename(directory.rstrip("/")) or directory)
    return " · ".join(parts)


def cmd_statusline(args):
    """Status-line entry point. Must never raise and must return promptly."""
    raw = b""
    try:
        raw = sys.stdin.buffer.read()
    except Exception:
        raw = b""

    wrapped_output = b""
    if args.wrap:
        wrapped_output = run_wrapped_statusline(args.wrap, raw)

    payload = None
    try:
        text = raw.decode("utf-8", "replace").strip()
        if text:
            payload = json.loads(text)
        if not isinstance(payload, dict):
            payload = None
    except Exception:
        payload = None

    decision = {"state": "invalid", "percent": None}
    try:
        ensure_dirs()
        decision = decide(payload, validate_files=True)
        facts = extract_facts(payload, validate_files=True) if payload else None
        if facts is not None:
            try:
                successor_heartbeat(facts)
            except Exception:
                pass
        if decision.get("trigger"):
            session_id = decision.get("session_id")
            if session_id and claim_trigger(session_id):
                # The Claude Code session process is an ancestor of this
                # status-line process and of nothing else Terminal Handoff can
                # see later, so it is bound here and only here.
                binding, bind_reason = None, "parent shutdown disabled by configuration"
                if stop_parent_enabled():
                    binding, bind_reason = bind_parent_claude_process(
                        session_id, _dget(payload, "workspace", "current_dir")
                    )
                log_event(
                    "trigger_claimed",
                    session_id=session_id,
                    percent=decision.get("percent"),
                    threshold=decision.get("threshold"),
                    chain_id=decision.get("chain_id"),
                    generation=decision.get("generation"),
                    model_id=decision.get("model_id"),
                    effort_level=decision.get("effort_level"),
                    successor_display_name=decision.get("successor_display_name"),
                    parent_process_bound=bool(binding),
                    parent_pid=(binding or {}).get("pid"),
                    parent_bind_reason=bind_reason,
                )
                spawn_launcher(payload, binding)
                decision["state"] = "launching"
            else:
                decision["state"] = "handed_off"
        elif decision.get("state") == "blocked":
            log_event(
                "blocked",
                session_id=decision.get("session_id"),
                reason=decision.get("reason"),
                percent=decision.get("percent"),
            )
    except Exception as exc:
        log_event("statusline_error", error=str(exc)[:300])
        decision = {"state": "blocked", "percent": None}

    badge = badge_for(decision)
    if payload is not None and not args.wrap:
        line = render_default_statusline(payload, decision) + " · " + badge
        sys.stdout.write(line)
    elif args.wrap:
        prefix = wrapped_output.rstrip(b"\r\n")
        sys.stdout.buffer.write(prefix)
        if prefix:
            sys.stdout.buffer.write(b" ")
        sys.stdout.buffer.write(("· " + badge).encode("utf-8"))
        sys.stdout.buffer.flush()
    else:
        sys.stdout.write(badge)
    sys.stdout.write("\n")
    return 0


# ---------------------------------------------------------------------------
# Settings installation / uninstallation
# ---------------------------------------------------------------------------


def registry_path():
    return th_path("state", "installed.json")


def load_registry():
    return read_json(registry_path(), {"targets": {}, "claude_md": None}) or {
        "targets": {},
        "claude_md": None,
    }


def save_registry(registry):
    # sort_keys=False preserves the exact key order of any original statusLine
    # object so an uninstall restores it byte-for-byte.
    write_json_private(registry_path(), registry, sort_keys=False)


def backup_file(path, tag):
    """Timestamped private backup. The path digest keeps same-named settings
    files (several projects all have `settings.json`) from colliding."""
    ensure_dirs()
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as handle:
        data = handle.read()
    digest = hashlib.sha256(os.path.abspath(path).encode("utf-8")).hexdigest()[:8]
    stamp = file_stamp()
    base = "%s.%s.%s.%s" % (os.path.basename(path), tag, stamp, digest)
    for attempt in range(100):
        suffix = "" if attempt == 0 else ".%d" % attempt
        destination = th_path("backups", base + suffix + ".bak")
        try:
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                continue
            raise
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        write_private(
            destination + ".source",
            json.dumps({"source": os.path.abspath(path), "tag": tag, "utc": utc_stamp()}, indent=2)
            + "\n",
        )
        return destination
    raise RuntimeError("could not create a unique backup for %s" % path)


def preferred_python():
    """Pin the stable system interpreter.

    sys.executable resolves to /Library/Developer/CommandLineTools/... under the
    /usr/bin/python3 shim; that path can disappear on an Xcode update and would
    silently break the status line in every session.
    """
    for candidate in ("/usr/bin/python3", sys.executable):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return sys.executable


# Emitted into every generated status-line command. Detection keys on this
# rather than on the module's filename or directory, so a renamed executable in
# a neutrally-named directory is still recognised and never wrapped recursively.
TH_SELF_MARKER = "terminal-handoff"


def th_statusline_command(wrap=None):
    self_path = os.path.abspath(__file__)
    command = "%s %s statusline --marker %s" % (
        shlex.quote(preferred_python()),
        shlex.quote(self_path),
        shlex.quote(TH_SELF_MARKER),
    )
    if wrap:
        command += " --wrap %s" % shlex.quote(wrap)
    return command


def _is_ascii(text):
    try:
        text.encode("ascii")
    except (UnicodeEncodeError, AttributeError):
        return False
    return True


def _serialize_settings(settings, original_text, ascii_only=None):
    """Serialize settings without disturbing anything unrelated.

    Two conventions of the original file are preserved, because both change
    bytes in keys that have nothing to do with Terminal Handoff:

    * **Non-ASCII escaping.** A file that already contains literal non-ASCII
      characters (an emoji in an `attribution` key, say) keeps them literal; a
      file written with backslash-u escapes keeps them escaped.
    * **Trailing newline.** Matched to whatever the original file had.
    """
    if ascii_only is None:
        ascii_only = True if original_text is None else _is_ascii(original_text)
    text = json.dumps(settings, indent=2, ensure_ascii=bool(ascii_only))
    if original_text is None or original_text.endswith("\n"):
        text += "\n"
    return text


def install_statusline(settings_file, tag):
    """Install or wrap the status line in one settings file. Idempotent."""
    ensure_dirs()
    original_text = None
    if os.path.isfile(settings_file):
        try:
            with open(settings_file, "r") as handle:
                original_text = handle.read()
        except IOError:
            original_text = None
    settings = read_json(settings_file, None)
    created = False
    if settings is None:
        if os.path.isfile(settings_file):
            return {"ok": False, "error": "existing settings file is not valid JSON"}
        settings = {}
        created = True
    if not isinstance(settings, dict):
        return {"ok": False, "error": "settings root is not an object"}

    existing = settings.get("statusLine")

    if isinstance(existing, dict) and is_terminal_handoff_command(existing.get("command", "")):
        return {"ok": True, "already_installed": True, "settings_file": settings_file}

    backup = backup_file(settings_file, tag) if not created else None

    wrap_command = None
    original = None
    if isinstance(existing, dict):
        original = existing
        if existing.get("type") == "command" and isinstance(existing.get("command"), str):
            if is_terminal_handoff_command(existing["command"]):
                return {"ok": True, "already_installed": True, "settings_file": settings_file}
            wrap_command = existing["command"]
        else:
            return {
                "ok": False,
                "error": "existing statusLine is not a command type; refusing to replace it",
                "existing": existing,
            }
    elif existing is not None:
        return {"ok": False, "error": "existing statusLine has an unexpected shape"}

    new_statusline = {"type": "command", "command": th_statusline_command(wrap_command)}
    if isinstance(original, dict):
        for key in ("padding",):
            if key in original:
                new_statusline[key] = original[key]

    settings["statusLine"] = new_statusline
    serialized = _serialize_settings(settings, original_text)
    json.loads(serialized)  # validate before install
    mode = 0o600 if created else (os.stat(settings_file).st_mode & 0o777)
    write_private(settings_file, serialized, mode=mode)

    registry = load_registry()
    registry["targets"][settings_file] = {
        "original_statusLine": original,
        "original_ends_with_newline": bool(original_text is None or original_text.endswith("\n")),
        "original_is_ascii": bool(original_text is None or _is_ascii(original_text)),
        "file_existed": not created,
        "backup": backup,
        "installed_at": utc_stamp(),
        "wrapped_command": wrap_command,
        "th_command": new_statusline["command"],
    }
    save_registry(registry)
    log_event(
        "statusline_installed",
        settings_file=settings_file,
        wrapped=bool(wrap_command),
        backup=backup,
    )
    return {
        "ok": True,
        "settings_file": settings_file,
        "backup": backup,
        "wrapped_command": wrap_command,
        "th_command": new_statusline["command"],
        "created_file": created,
    }


def extract_wrapped_command(command):
    """Recover the wrapped command from a Terminal Handoff status-line command.

    The install registry records the original status line, but it can be lost:
    an interrupted install, a deleted state directory, a relocated module. In
    that case the original is still recoverable from the installed command's
    own `--wrap` argument, which is preferable to silently removing a status
    line the user did not install through Terminal Handoff.
    """
    if not command:
        return None
    try:
        parts = shlex.split(str(command))
    except ValueError:
        return None
    if "--wrap" not in parts:
        return None
    index = parts.index("--wrap")
    if index + 1 >= len(parts):
        return None
    recovered = parts[index + 1]
    # Never "recover" our own command: that would reinstate a self-wrap.
    if is_terminal_handoff_command(recovered):
        return None
    return recovered or None


def uninstall_statusline(settings_file, dry_run=True):
    registry = load_registry()
    entry = (registry.get("targets") or {}).get(settings_file)
    settings = read_json(settings_file, None)
    if settings is None:
        return {"ok": False, "error": "settings file missing or invalid", "settings_file": settings_file}
    current = settings.get("statusLine")
    if not (isinstance(current, dict) and is_terminal_handoff_command(current.get("command", ""))):
        return {"ok": True, "settings_file": settings_file, "note": "Terminal Handoff not installed here"}

    recovered = extract_wrapped_command(current.get("command", ""))
    if entry and entry.get("original_statusLine") is not None:
        planned = entry["original_statusLine"]
        action = "restore original statusLine"
    elif recovered:
        # No registry entry, but the installed command still names what it
        # wrapped. Restoring that is strictly safer than dropping it.
        planned = {"type": "command", "command": recovered}
        action = "restore wrapped statusLine recovered from the installed command"
    elif entry and not entry.get("file_existed"):
        planned = None
        action = "remove generated settings file"
    else:
        planned = None
        action = "remove statusLine key"

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "settings_file": settings_file,
            "action": action,
            "planned_statusLine": planned,
        }

    backup_file(settings_file, "uninstall")
    ascii_only = None
    if entry is not None and "original_ends_with_newline" in entry:
        original_text = "{}\n" if entry["original_ends_with_newline"] else "{}"
        if "original_is_ascii" in entry:
            ascii_only = bool(entry["original_is_ascii"])
    else:
        try:
            with open(settings_file, "r") as handle:
                original_text = handle.read()
        except IOError:
            original_text = None
    if planned is None:
        settings.pop("statusLine", None)
    else:
        settings["statusLine"] = planned
    serialized = _serialize_settings(settings, original_text, ascii_only=ascii_only)
    json.loads(serialized)
    write_private(settings_file, serialized, mode=os.stat(settings_file).st_mode & 0o777)
    if entry:
        registry["targets"].pop(settings_file, None)
        save_registry(registry)
    log_event("statusline_uninstalled", settings_file=settings_file, action=action)
    return {"ok": True, "settings_file": settings_file, "action": action, "restored": planned}


CLAUDE_MD_BEGIN = "<!-- BEGIN TERMINAL HANDOFF -->"
CLAUDE_MD_END = "<!-- END TERMINAL HANDOFF -->"

CLAUDE_MD_SECTION = """%s
# Terminal Handoff

Terminal Handoff is installed globally at `~/.claude/terminal-handoff/`.

Every Claude Code session on this Mac is monitored using its official live
context percentage (`.context_window.used_percentage` from the status-line
JSON). At the configured threshold (default 80%%), Terminal Handoff opens a
fresh successor session in a new macOS Terminal window using the outgoing
session's exact model ID and exact effort level.

When you are started as a Terminal Handoff successor:

- read the supplied handoff manifest
- use a context-isolated subagent to analyse the parent transcript; never load
  the full parent transcript into your own main context
- independently verify repository state (pwd, root, branch, HEAD, origin/main,
  ahead/behind, status, merge/rebase/cherry-pick/revert state)
- treat the live filesystem and Git state as authoritative over the transcript
- treat every existing change as user-owned; never reset, clean, discard,
  overwrite, revert, amend, force-push, delete or stash without clear authority
- continue only from verified evidence, and produce a TERMINAL HANDOFF
  CONTINUATION REPORT first
- allow your own Terminal Handoff trigger when your context later reaches the
  threshold

Do not disable, bypass or modify Terminal Handoff unless the user explicitly
requests it. Do not manually launch a second successor if the current session
has already been handed off.
%s
""" % (
    CLAUDE_MD_BEGIN,
    CLAUDE_MD_END,
)


def install_claude_md(path=None):
    path = path or os.path.join(os.path.expanduser("~"), ".claude", "CLAUDE.md")
    existing = ""
    if os.path.isfile(path):
        with open(path, "r") as handle:
            existing = handle.read()
    if CLAUDE_MD_BEGIN in existing:
        return {"ok": True, "already_installed": True, "path": path}
    backup = backup_file(path, "claude-md")
    separator = "" if existing.endswith("\n\n") or existing == "" else ("\n" if existing.endswith("\n") else "\n\n")
    write_private(path, existing + separator + CLAUDE_MD_SECTION, mode=0o644 if existing else 0o600)
    registry = load_registry()
    registry["claude_md"] = {"path": path, "backup": backup, "installed_at": utc_stamp()}
    save_registry(registry)
    log_event("claude_md_installed", path=path, backup=backup)
    return {"ok": True, "path": path, "backup": backup}


def uninstall_claude_md(path=None, dry_run=True):
    path = path or os.path.join(os.path.expanduser("~"), ".claude", "CLAUDE.md")
    if not os.path.isfile(path):
        return {"ok": True, "note": "no CLAUDE.md"}
    with open(path, "r") as handle:
        content = handle.read()
    if CLAUDE_MD_BEGIN not in content:
        return {"ok": True, "note": "Terminal Handoff section not present"}
    start = content.index(CLAUDE_MD_BEGIN)
    end = content.index(CLAUDE_MD_END) + len(CLAUDE_MD_END)
    cleaned = (content[:start].rstrip("\n") + "\n" + content[end:].lstrip("\n")).rstrip("\n") + "\n"
    if dry_run:
        return {"ok": True, "dry_run": True, "path": path, "bytes_removed": end - start}
    backup_file(path, "claude-md-uninstall")
    write_private(path, cleaned, mode=os.stat(path).st_mode & 0o777)
    log_event("claude_md_uninstalled", path=path)
    return {"ok": True, "path": path, "bytes_removed": end - start}


# ---------------------------------------------------------------------------
# Coverage report
# ---------------------------------------------------------------------------


def discover_settings_files(roots=None, max_depth=6):
    home = os.path.expanduser("~")
    found = []
    skip = {"Library", "node_modules", ".git", ".Trash", "Pictures", "Movies", "Music"}
    for dirpath, dirnames, filenames in os.walk(home):
        depth = dirpath[len(home):].count(os.sep)
        if depth >= max_depth:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in skip]
        if os.path.basename(dirpath) != ".claude":
            continue
        for name in filenames:
            if name.startswith("settings") and name.endswith(".json"):
                found.append(os.path.join(dirpath, name))
    return sorted(found)


def coverage_report():
    files = discover_settings_files()
    rows = []
    for path in files:
        data = read_json(path, None)
        if data is None:
            rows.append({"settings_file": path, "statusLine": "INVALID JSON", "terminal_handoff": "unknown"})
            continue
        status_line = data.get("statusLine")
        if status_line is None:
            rows.append(
                {
                    "settings_file": path,
                    "statusLine": None,
                    "terminal_handoff": "inherits global",
                }
            )
        else:
            command = str((status_line or {}).get("command", ""))
            active = is_terminal_handoff_command(command)
            rows.append(
                {
                    "settings_file": path,
                    "statusLine": command[:160],
                    "terminal_handoff": "ACTIVE (integrated)" if active else "OVERRIDE - NOT COVERED",
                }
            )
    global_settings = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
    global_data = read_json(global_settings, {}) or {}
    global_active = is_terminal_handoff_command(
        (global_data.get("statusLine") or {}).get("command", "")
    )
    uncovered = [r for r in rows if r["terminal_handoff"] == "OVERRIDE - NOT COVERED"]
    return {
        "generated_utc": utc_stamp(),
        "global_settings": global_settings,
        "global_terminal_handoff_active": global_active,
        "settings_files_scanned": len(rows),
        "rows": rows,
        "uncovered_overrides": uncovered,
        "full_coverage": bool(global_active and not uncovered),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def cmd_evaluate(args):
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else None
        if not isinstance(payload, dict):
            payload = None
    except ValueError:
        payload = None
    decision = decide(payload, validate_files=not args.no_file_validation, record=not args.no_record)
    print(json.dumps(decision, indent=2, sort_keys=True, default=str))
    return 0


def cmd_launch(args):
    return perform_launch(args.payload)


def cmd_build_command(args):
    manifest = read_json(args.manifest)
    if manifest is None:
        print(json.dumps({"ok": False, "error": "manifest unreadable"}))
        return 1
    claude_bin = args.claude_bin or find_claude_executable() or "/usr/bin/false"
    prompt_file = th_path("prompts", "successor-%s.md" % (manifest.get("outgoing") or {}).get("session_id"))
    try:
        argv = build_launch_argv(manifest, claude_bin, bootstrap_prompt(manifest, prompt_file))
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 2
    problems = assert_launch_argv_safe(argv)
    script = build_launch_script(
        manifest, argv, (manifest.get("outgoing") or {}).get("current_dir") or "/tmp", args.manifest
    )
    print(
        json.dumps(
            {"ok": not problems, "argv": argv, "problems": problems, "launch_script": script},
            indent=2,
        )
    )
    return 0 if not problems else 3


def cmd_install(args):
    ensure_dirs()
    results = {"targets": [], "claude_md": None}
    for settings_file in args.settings:
        results["targets"].append(install_statusline(os.path.abspath(os.path.expanduser(settings_file)), args.tag))
    if not args.skip_claude_md:
        results["claude_md"] = install_claude_md()
    print(json.dumps(results, indent=2, default=str))
    return 0 if all(t.get("ok") for t in results["targets"]) else 1


def cmd_uninstall(args):
    registry = load_registry()
    targets = args.settings or sorted((registry.get("targets") or {}).keys())
    results = {"dry_run": not args.apply, "targets": [], "claude_md": None}
    for settings_file in targets:
        results["targets"].append(uninstall_statusline(os.path.abspath(os.path.expanduser(settings_file)), dry_run=not args.apply))
    if not args.skip_claude_md:
        results["claude_md"] = uninstall_claude_md(dry_run=not args.apply)
    results["retained"] = {
        "handoffs": th_path("handoffs"),
        "logs": th_path("logs"),
        "backups": th_path("backups"),
        "note": "Manifests, logs and backups are preserved. Transcripts are never touched.",
    }
    print(json.dumps(results, indent=2, default=str))
    return 0


def cmd_coverage(args):
    report = coverage_report()
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0 if report["full_coverage"] else 1
    print("Terminal Handoff %s - status-line coverage report" % TERMINAL_HANDOFF_VERSION)
    print("Generated: %s" % report["generated_utc"])
    print("")
    print("Global settings: %s" % report["global_settings"])
    print("Global Terminal Handoff active: %s" % ("YES" if report["global_terminal_handoff_active"] else "NO"))
    print("Settings files scanned: %d" % report["settings_files_scanned"])
    print("")
    for row in report["rows"]:
        print("  [%s] %s" % (row["terminal_handoff"], row["settings_file"]))
        if row["statusLine"]:
            print("        statusLine: %s" % row["statusLine"])
    print("")
    print("FULL COVERAGE: %s" % ("YES" if report["full_coverage"] else "NO"))
    if report["uncovered_overrides"]:
        print("Uncovered overrides:")
        for row in report["uncovered_overrides"]:
            print("  - %s" % row["settings_file"])
    return 0 if report["full_coverage"] else 1


def cmd_status(args):
    ensure_dirs()
    open_now, circuit = circuit_is_open()
    storm_max, storm_window, cooldown, _ = storm_settings()
    info = {
        "terminal_handoff_version": TERMINAL_HANDOFF_VERSION,
        "home": th_home(),
        "threshold": threshold(),
        "disabled": env_flag("CLAUDE_TERMINAL_HANDOFF_DISABLED"),
        "test_mode": env_flag("CLAUDE_TERMINAL_HANDOFF_TEST_MODE"),
        "max_generations": max_generations() or "unlimited",
        "min_observations": env_int("CLAUDE_TERMINAL_HANDOFF_MIN_OBSERVATIONS", DEFAULT_MIN_OBSERVATIONS),
        "cooldown_seconds": cooldown,
        "storm_max_launches": storm_max,
        "storm_window_seconds": storm_window,
        "circuit_open": open_now,
        "circuit": circuit,
        "claude_executable": find_claude_executable(),
        "triggered_sessions": len(os.listdir(th_path("triggered"))) if os.path.isdir(th_path("triggered")) else 0,
        "manifests": len([f for f in os.listdir(th_path("handoffs"))]) if os.path.isdir(th_path("handoffs")) else 0,
        "recent_launches_in_window": len(recent_launch_times(storm_window)),
        "stop_parent_enabled": stop_parent_enabled(),
        "parent_stop_signal": PARENT_STOP_SIGNAL_NAME,
        "heartbeat_timeout_seconds": heartbeat_timeout(),
        "stop_grace_seconds": stop_grace_seconds(),
        "stop_attempts": stop_attempts(),
        "transfers": transfer_summary(),
    }
    print(json.dumps(info, indent=2, default=str))
    return 0


def transfer_summary():
    """Current state of every recorded transfer of ownership."""
    directory = th_path("transfers")
    rows = []
    if not os.path.isdir(directory):
        return rows
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        record = read_json(os.path.join(directory, name))
        if not isinstance(record, dict):
            continue
        rows.append(
            {
                "parent_session_id": record.get("parent_session_id"),
                "chain_id": record.get("chain_id"),
                "state": record.get("state"),
                "owner": record.get("owner"),
                "successor_display_name": record.get("successor_display_name"),
                "parent_stopped": record.get("parent_stopped"),
            }
        )
    return rows


def cmd_supervise(args):
    return supervise_transfer(args.transfer)


def cmd_reset_circuit(args):
    ensure_dirs()
    path = circuit_file()
    was_open = os.path.exists(path)
    if was_open:
        os.unlink(path)
    now = time.time()
    # Exclude already-counted launches so the reset is not undone immediately.
    write_json_private(storm_reset_file(), {"epoch": now, "ts": utc_stamp(), "by": "cli"})
    # Cooldown is a separate guard; a manual reset clears it too.
    if os.path.exists(cooldown_file()):
        try:
            os.unlink(cooldown_file())
        except OSError:
            pass
    log_event("circuit_breaker_reset", by="cli", was_open=was_open)
    if was_open:
        print("Terminal Handoff: storm circuit breaker reset; launch window cleared.")
    else:
        print("Terminal Handoff: circuit breaker was not open; launch window cleared.")
    return 0


def cmd_version(args):
    print("Terminal Handoff %s (manifest schema %d)" % (TERMINAL_HANDOFF_VERSION, MANIFEST_SCHEMA_VERSION))
    return 0


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(prog="terminal-handoff", description="Terminal Handoff")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("statusline", help="Render the status line and evaluate the trigger")
    p.add_argument("--wrap", default=None, help="Pre-existing status-line command to wrap")
    p.add_argument(
        "--marker",
        default=None,
        help=(
            "Self-identifying marker emitted by the installer. It has no runtime "
            "effect; it lets the installer recognise its own command under any "
            "module name or directory and refuse to wrap itself."
        ),
    )
    p.set_defaults(func=cmd_statusline)

    p = sub.add_parser("evaluate", help="Evaluate a status-line JSON payload from stdin")
    p.add_argument("--no-file-validation", action="store_true")
    p.add_argument("--no-record", action="store_true")
    p.set_defaults(func=cmd_evaluate)

    p = sub.add_parser("launch", help="Detached launcher (internal)")
    p.add_argument("--payload", required=True)
    p.set_defaults(func=cmd_launch)

    p = sub.add_parser(
        "supervise",
        help=(
            "Detached shutdown supervisor (internal): wait for a verified successor "
            "heartbeat, then gracefully stop the exact bound parent Claude process"
        ),
    )
    p.add_argument("--transfer", required=True)
    p.set_defaults(func=cmd_supervise)

    p = sub.add_parser("build-command", help="Print the successor launch argv for a manifest")
    p.add_argument("--manifest", required=True)
    p.add_argument("--claude-bin", default=None)
    p.set_defaults(func=cmd_build_command)

    p = sub.add_parser("install", help="Install or wrap the status line in settings files")
    p.add_argument("--settings", nargs="+", required=True)
    p.add_argument("--tag", default="install")
    p.add_argument("--skip-claude-md", action="store_true")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("uninstall", help="Remove Terminal Handoff from settings files")
    p.add_argument("--settings", nargs="*", default=None)
    p.add_argument("--apply", action="store_true", help="Actually apply (default is a dry run)")
    p.add_argument("--skip-claude-md", action="store_true")
    p.set_defaults(func=cmd_uninstall)

    p = sub.add_parser("coverage", help="Report status-line coverage across all Claude settings")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_coverage)

    p = sub.add_parser("status", help="Show Terminal Handoff runtime state")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("reset-circuit", help="Reset the storm circuit breaker")
    p.set_defaults(func=cmd_reset_circuit)

    p = sub.add_parser("version", help="Print the Terminal Handoff version")
    p.set_defaults(func=cmd_version)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
