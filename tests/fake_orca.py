#!/usr/bin/env python3
"""Shared fake Orca CLI plus injected authority adapters for the ledger tests.

The ledger never talks to a real Orca, the network or any credential. ``FakeOrca``
generates a small executable in a temporary directory and tests point
``ORCA_CLI_COMMAND`` at it, exactly as ``tests/test_inbox_loop.py`` already does. The
product resolves its CLI through that variable, so no private attribute is patched.

What the fake covers (the read commands and mutation classes the reconciliation
tests need):

* ``orchestration check`` for inbox tests, including ``--ack`` and ``--wait``
  semantics and keepalive lines;
* ``orchestration worker-list`` with cursor pagination: pages answer
  ``page.hasMore`` and ``page.nextCursor`` and remote rows are served verbatim;
* ``orchestration request-show --request <id>`` with fixture states
  ``completed`` / ``pending`` / ``absent``, or a scripted failure;
* ``orchestration worker-show --dispatch <id>`` and ``orchestration task-list``;
* the preflight probes ``status --json`` (app up) and ``agent hooks status --json``
  (the current profile's ``result.settingsPath``), both scripted fixtures rather than
  claims about a live Orca shape;
* the mutation classes ``worker-start``, ``send``, ``reply`` (plus the other
  lifecycle verbs) so joinable-replay tests can observe that an action ran, in
  what order, and with which ``--retry-request`` key.

Every invocation is appended to ``calls.txt`` and ``calls.jsonl`` in order. A
mutation also appends to ``mutations.txt`` / ``mutations.jsonl``. When a lock
probe path is configured, every call tries ``flock(LOCK_EX | LOCK_NB)`` on the
ledger lock and records ``free`` / ``busy`` / ``missing``; this is how the
"never hold the ledger lock while calling Orca" rule is tested.

Receipt field shapes for a live dispatch are unverified. The fake's receipts are
fixtures: tests may assert our own command contract and the request id, and must
not treat any receipt field as a stable Orca contract.

``FakeHost`` (PR host, tracker, git) and ``FakeAuthority`` are injected adapters:
they serve a recorded authority snapshot, record replays, and never touch the
network. ``FakeAuthority`` accepts a plain snapshot for pure reconciliation tests;
``FakeHost`` is what the CLI's ``--authority`` file and the live adapter use.
"""

import copy
import json
import os
import stat
import sys
from pathlib import Path

CHECK_PREFIX = '["orchestration", "check"'


SHIM_TEMPLATE = '''#!__PYTHON__
import fcntl, json, os, sys, time

CONFIG = json.loads(open("__CONFIG__").read())
CALLS = "__CALLS__"
CALLS_JSON = "__CALLS_JSON__"
MUTATIONS = "__MUTATIONS__"
MUTATIONS_JSON = "__MUTATIONS_JSON__"
ACKS = "__ACKS__"
CHECK_COUNTER = "__CHECK_COUNTER__"
MUTATION_COUNTER = "__MUTATION_COUNTER__"
LOCK_PROBE = __LOCK_PROBE__

argv = sys.argv[1:]

with open(CALLS, "a") as out:
    out.write(" ".join(argv) + "\\n")
with open(CALLS_JSON, "a") as out:
    out.write(json.dumps(argv) + "\\n")

if LOCK_PROBE:
    try:
        descriptor = os.open(LOCK_PROBE, os.O_RDWR | os.O_NOFOLLOW)
    except OSError:
        verdict = "missing"
    else:
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                verdict = "busy"
            else:
                verdict = "free"
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
    with open(LOCK_PROBE + ".probes", "a") as out:
        out.write(verdict + "\\n")

if CONFIG.get("delay"):
    time.sleep(CONFIG["delay"])

def fail(reason):
    sys.stderr.write(reason)
    sys.exit(1)

def emit(result):
    sys.stdout.write(json.dumps({"ok": True, "result": result}))
    sys.exit(0)

def flag(name, default=None):
    if name in argv:
        index = argv.index(name)
        if index + 1 < len(argv):
            return argv[index + 1]
    return default

if argv and argv[0] == "status":
    response = CONFIG.get("status")
    if isinstance(response, dict) and "fail" in response:
        fail(response["fail"])
    emit(response if response is not None else {"app": "up"})

if argv[:3] == ["agent", "hooks", "status"]:
    response = CONFIG.get("agent_hooks_status")
    if isinstance(response, dict) and "fail" in response:
        fail(response["fail"])
    emit(response if response is not None else {"settingsPath": ""})

if not argv or argv[0] != "orchestration":
    fail("unexpected command: " + " ".join(argv))

kind = argv[1] if len(argv) > 1 else ""
if kind in CONFIG.get("fail", {}):
    fail(CONFIG["fail"][kind])

def counter(path):
    try:
        return int(open(path).read())
    except (OSError, ValueError):
        return 0

def bump(path, value):
    with open(path, "w") as out:
        out.write(str(value))

def page_for(pages, cursor, rows_key):
    if cursor.startswith("cursor-"):
        try:
            index = int(cursor[len("cursor-"):])
        except ValueError:
            fail("bad cursor: " + cursor)
    elif cursor:
        fail("unknown cursor: " + cursor)
    else:
        index = 0
    if index >= len(pages):
        fail("cursor past the last page: " + cursor)
    page = pages[index]
    if isinstance(page, dict) and "fail" in page:
        fail(page["fail"])
    if isinstance(page, dict) and "page" in page:
        page_object = page["page"]
    else:
        more = index + 1 < len(pages)
        page_object = {
            "hasMore": more,
            "nextCursor": ("cursor-" + str(index + 1)) if more else None,
        }
    result = {rows_key: page.get(rows_key, []) if isinstance(page, dict) else []}
    result["page"] = page_object
    emit(result)

if kind == "check":
    if "--ack" in argv:
        delivery_id = flag("--ack")
        with open(ACKS, "a") as out:
            out.write(delivery_id + "\\n")
        emit({"acknowledged": delivery_id})
    responses = CONFIG.get("check") or [{"messages": [], "deliveryId": "delivery_empty"}]
    index = counter(CHECK_COUNTER)
    if "--wait" not in argv:
        bump(CHECK_COUNTER, index + 1)
    if CONFIG.get("check_keepalive") and "--wait" in argv:
        sys.stderr.write(json.dumps({"_keepalive": True}) + "\\n")
    index = min(index, len(responses) - 1)
    response = responses[index]
    if isinstance(response, dict) and "fail" in response:
        fail(response["fail"])
    if isinstance(response, str):
        sys.stdout.write(response)
        sys.exit(0)
    emit(response)

if kind == "worker-list":
    page_for(CONFIG.get("worker_pages") or [{"workers": []}], flag("--cursor", ""), "workers")

if kind == "request-show":
    request_id = flag("--request")
    configured = CONFIG.get("request_show", {})
    if request_id not in configured:
        fail("unscripted request-show: " + str(request_id))
    entry = configured[request_id]
    if isinstance(entry, dict) and "fail" in entry:
        fail(entry["fail"])
    emit({"request": entry})

if kind == "worker-show":
    dispatch_id = flag("--dispatch")
    configured = CONFIG.get("worker_show", {})
    if dispatch_id not in configured:
        fail("unscripted worker-show: " + str(dispatch_id))
    entry = configured[dispatch_id]
    if isinstance(entry, dict) and "fail" in entry:
        fail(entry["fail"])
    emit({"worker": entry})

if kind == "task-list":
    page_for(CONFIG.get("task_pages") or [{"tasks": []}], flag("--cursor", ""), "tasks")

MUTATION_KINDS = (
    "worker-start", "send", "reply", "worker-stop", "worker-abandon",
    "worker-retain", "worker-release", "merge", "pr-create", "tracker-close",
)
if kind in MUTATION_KINDS:
    request_id = flag("--retry-request", "")
    with open(MUTATIONS, "a") as out:
        out.write(" ".join(argv) + "\\n")
    with open(MUTATIONS_JSON, "a") as out:
        out.write(json.dumps(argv) + "\\n")
    responses = CONFIG.get("mutations", {}).get(kind) or []
    index = counter(MUTATION_COUNTER + "-" + kind)
    bump(MUTATION_COUNTER + "-" + kind, index + 1)
    if index < len(responses):
        response = responses[index]
    else:
        response = {"state": "completed", "requestId": request_id}
    if isinstance(response, dict) and "fail" in response:
        fail(response["fail"])
    emit(response)

fail("unexpected command: " + " ".join(argv))
'''


class FakeOrca:
    """A generated ``orca`` executable serving scripted responses.

    ``worker_pages`` and ``task_pages`` are lists of page dictionaries. A page may
    carry its own ``page`` object; otherwise the shim derives
    ``hasMore``/``nextCursor`` from the list position, and a later call passes the
    cursor back through ``--cursor``. ``request_show`` and ``worker_show`` are
    dictionaries keyed by id; an entry of ``{"fail": "reason"}`` makes the command
    exit non-zero. ``mutations`` maps an action name to a list of scripted
    responses (``{"state": ..., ...}``); an unscripted call gets a fixture
    completion carrying the same ``--retry-request`` id.

    The object is intentionally configuration-only: it writes files and returns
    paths, so a test can point ``ORCA_CLI_COMMAND`` at ``self.path`` and drive the
    product script as a subprocess. It never touches a live Orca or the network.
    """

    def __init__(
        self,
        directory,
        check=None,
        worker_pages=None,
        request_show=None,
        worker_show=None,
        task_pages=None,
        mutations=None,
        fail=None,
        delay=0.0,
        check_keepalive=False,
        lock_probe=None,
        status=None,
        agent_hooks_status=None,
    ):
        self.directory = Path(directory)
        self.config = {
            "check": list(check or []),
            "worker_pages": list(worker_pages or []),
            "request_show": dict(request_show or {}),
            "worker_show": dict(worker_show or {}),
            "task_pages": list(task_pages or []),
            "mutations": {key: list(value) for key, value in (mutations or {}).items()},
            "fail": dict(fail or {}),
            "delay": delay,
            "check_keepalive": check_keepalive,
            "status": dict(status) if isinstance(status, dict) else status,
            "agent_hooks_status": (
                dict(agent_hooks_status)
                if isinstance(agent_hooks_status, dict) else agent_hooks_status
            ),
        }
        self.lock_probe = Path(lock_probe) if lock_probe else None
        self.path = self.directory / "orca"
        self.calls_log = self.directory / "calls.txt"
        self.calls_json_log = self.directory / "calls.jsonl"
        self.mutations_log = self.directory / "mutations.txt"
        self.mutations_json_log = self.directory / "mutations.jsonl"
        self.acks_log = self.directory / "acks.txt"
        self.check_counter = self.directory / "check-counter.txt"
        self.mutation_counter = self.directory / "mutation-counter"

    def write(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        config_path = self.directory / "config.json"
        config_path.write_text(json.dumps(self.config))
        shim = (
            SHIM_TEMPLATE
            .replace("__PYTHON__", sys.executable)
            .replace("__CONFIG__", str(config_path))
            .replace("__CALLS__", str(self.calls_log))
            .replace("__CALLS_JSON__", str(self.calls_json_log))
            .replace("__MUTATIONS__", str(self.mutations_log))
            .replace("__MUTATIONS_JSON__", str(self.mutations_json_log))
            .replace("__ACKS__", str(self.acks_log))
            .replace("__CHECK_COUNTER__", str(self.check_counter))
            .replace("__MUTATION_COUNTER__", str(self.mutation_counter))
            .replace("__LOCK_PROBE__", repr(str(self.lock_probe)) if self.lock_probe else "None")
        )
        self.path.write_text(shim)
        self.path.chmod(self.path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return self

    def _lines(self, path):
        if not path.exists():
            return []
        return [line for line in path.read_text().splitlines() if line]

    def calls(self):
        """Every invocation, in order, as a single space-joined string."""
        return self._lines(self.calls_log)

    def call_argv(self):
        """Every invocation, in order, as its exact argument list."""
        return [json.loads(line) for line in self._lines(self.calls_json_log)]

    def mutations(self):
        """Every mutation invocation, in order, as a space-joined string."""
        return self._lines(self.mutations_log)

    def mutation_argv(self):
        return [json.loads(line) for line in self._lines(self.mutations_json_log)]

    def acks(self):
        return self._lines(self.acks_log)

    def lock_probes(self):
        """One ``free`` / ``busy`` / ``missing`` verdict per invocation."""
        if self.lock_probe is None:
            return []
        return self._lines(Path(str(self.lock_probe) + ".probes"))


class FakeHost:
    """An injected PR-host, tracker or git adapter serving a recorded section."""

    def __init__(self, section):
        self.section = section

    def snapshot(self, run_id, records):
        return copy.deepcopy(self.section)


class RaisingHost:
    """An adapter that is unavailable; the source must become ``unknown``."""

    def __init__(self, reason="adapter unavailable"):
        self.reason = reason

    def snapshot(self, run_id, records):
        raise RuntimeError(self.reason)


class FakeAuthority:
    """A snapshot and replay adapter for module-level reconciliation tests.

    ``snapshot`` may be a full authority snapshot or a partial one; the product
    fills missing sources with ``unknown``. ``replay_responses`` maps an action to
    a response dict, a list of dicts consumed per call, or a callable. Every replay
    is recorded with the request id, action, subject and command so a test can
    assert that a completed receipt never replays and a pending joinable one does.
    """

    def __init__(self, snapshot, replay_responses=None):
        self.snapshot_value = snapshot
        self.replay_responses = replay_responses or {}
        self.replays = []

    def snapshot(self, run_id, records):
        data = copy.deepcopy(self.snapshot_value)
        data.setdefault("run_id", run_id)
        return data

    def replay(self, request_id, action, subject, command):
        self.replays.append({
            "request_id": request_id,
            "action": action,
            "subject": copy.deepcopy(subject),
            "command": list(command or []),
        })
        response = self.replay_responses.get(action)
        if callable(response):
            return response(request_id, action, subject, command)
        if isinstance(response, list):
            return copy.deepcopy(response.pop(0)) if response else {"state": "unknown", "reason": "no scripted replay"}
        if isinstance(response, dict):
            return copy.deepcopy(response)
        return {"state": "completed", "request_id": request_id}
