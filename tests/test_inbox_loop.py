#!/usr/bin/env python3
"""Behaviour tests for ``inbox-loop.py``, driven through a fake Orca CLI.

The loop's job is small but safety-critical: never lose a message, never let a FIFO
replay overwrite the first read, never acknowledge anything a human still has to look
at, never mistake a CLI failure for an empty queue — and end the process, with one
machine-readable line on stdout, when there is something the coordinator must get up for.
These tests exercise exactly those behaviours end to end by running the real script as a
subprocess against a stub ``orca`` executable, so the assertions are about what the script
actually writes to the filesystem and to stdout rather than about its internals.

Nothing here reaches the network, reads credentials, or depends on this machine's Orca
profile: the CLI is a shim generated per-test in a temporary directory.
"""

import contextlib
import hashlib
import importlib.util
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIRS = (
    REPO_ROOT / "skills" / "orchestra-auto",
    REPO_ROOT / "skills" / "orchestra-auto-zh",
)
SCRIPT_RELATIVE = Path("scripts") / "inbox-loop.py"

CHECK_PREFIX = '["orchestration", "check", "--run"]'

# The stub is a template with placeholders rather than an f-string: the JSON payloads it
# writes contain braces, and doubling every one of them would make the shim unreadable.
SHIM_TEMPLATE = '''#!__PYTHON__
import json, sys

RESPONSES = json.loads(open("__RESPONSES__").read())
WORKERS = json.loads(open("__WORKERS__").read())
ACK_RESPONSES = json.loads(open("__ACK_RESPONSES__").read())
CALLS = "__CALLS__"
ACKS = "__ACKS__"
ACK_COUNTER = "__ACK_COUNTER__"
COUNTER = "__COUNTER__"
WORKER_COUNTER = "__WORKER_COUNTER__"
KEEPALIVE = __KEEPALIVE__

argv = sys.argv[1:]
with open(CALLS, "a") as out:
    out.write(" ".join(argv) + "\\n")

if argv[:2] == ["orchestration", "worker-list"]:
    try:
        index = int(open(WORKER_COUNTER).read())
    except (OSError, ValueError):
        index = 0
    with open(WORKER_COUNTER, "w") as out:
        out.write(str(index + 1))
    index = min(index, len(WORKERS) - 1)
    entry = WORKERS[index]
    if isinstance(entry, str):
        # A raw envelope, printed verbatim: how a CLI that answers with something that is
        # not a JSON object at all is exercised.
        sys.stdout.write(entry)
        sys.exit(0)
    if isinstance(entry, dict) and "fail" in entry:
        sys.stderr.write(entry["fail"])
        sys.exit(1)
    sys.stdout.write(json.dumps({"ok": True, "result": {"workers": entry}}))
    sys.exit(0)

if argv[:3] != __CHECK_PREFIX__:
    sys.stderr.write("unexpected command: " + " ".join(argv))
    sys.exit(2)

if "--ack" in argv:
    did = argv[argv.index("--ack") + 1]
    with open(ACKS, "a") as out:
        out.write(did + "\\n")
    try:
        index = int(open(ACK_COUNTER).read())
    except (OSError, ValueError):
        index = 0
    with open(ACK_COUNTER, "w") as out:
        out.write(str(index + 1))
    # Scripted refusals exercise the "record failed and stay unacked" path; an
    # unscripted ack succeeds, exactly as the live CLI does for a real delivery.
    entry = ACK_RESPONSES[min(index, len(ACK_RESPONSES) - 1)] if ACK_RESPONSES else {}
    if isinstance(entry, dict) and "fail" in entry:
        sys.stderr.write(entry["fail"])
        sys.exit(1)
    sys.stdout.write(json.dumps({"ok": True, "result": {"acknowledged": did}}))
    sys.exit(0)

if "--wait" not in argv:
    try:
        index = int(open(COUNTER).read())
    except (OSError, ValueError):
        index = 0
    with open(COUNTER, "w") as out:
        out.write(str(index + 1))
else:
    # A wait serves the next unconsumed response and does not consume it: the real CLI
    # replays the same FIFO batch until it is acknowledged.
    try:
        index = int(open(COUNTER).read())
    except (OSError, ValueError):
        index = 0
    if KEEPALIVE:
        for _ in range(2):
            sys.stderr.write(json.dumps({"_keepalive": True}) + "\\n")
index = min(index, len(RESPONSES) - 1)
response = RESPONSES[index]

if "fail" in response:
    sys.stderr.write(response["fail"])
    sys.exit(1)

sys.stdout.write(json.dumps(response))
sys.exit(0)
'''

# A second stub, for the process-lifecycle tests: every call records the child's own pid, a
# ``check --wait`` blocks for ``CHECK_SLEEP`` seconds, and ``worker-list`` blocks while a
# marker file the test creates exists. That marker is what lets a test hold a waiter and a
# nested poll open at the same moment, which is the only state in which the leak is visible.
BLOCKING_SHIM_TEMPLATE = '''#!__PYTHON__
import json, os, sys, time

PIDS = "__PIDS__"
CALLS = "__CALLS__"
MARKER = "__MARKER__"
argv = sys.argv[1:]

with open(PIDS, "a") as out:
    out.write(str(os.getpid()) + "\\n")
with open(CALLS, "a") as out:
    out.write(" ".join(argv) + "\\n")

if argv[:2] == ["orchestration", "worker-list"]:
    if os.path.exists(MARKER):
        time.sleep(__WORKER_SLEEP__)
    sys.stdout.write(json.dumps({"ok": True, "result": {"workers": []}}))
    sys.exit(0)

if argv[:3] != __CHECK_PREFIX__:
    sys.stderr.write("unexpected command: " + " ".join(argv))
    sys.exit(2)

if "--wait" in argv:
    time.sleep(__CHECK_SLEEP__)
sys.stdout.write(json.dumps({"ok": True, "result": {"messages": []}}))
sys.exit(0)
'''


def load_inbox_module(skill_dir):
    """Import a skill's inbox-loop.py by path, without touching sys.path."""
    path = skill_dir / SCRIPT_RELATIVE
    spec = importlib.util.spec_from_file_location(
        "inbox_loop_" + skill_dir.name.replace("-", "_"), path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot build an import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_inbox_module_with_cli(skill_dir, command):
    """Import the script with ``ORCA_CLI_COMMAND`` set, as the loop really resolves its CLI.

    The module reads the variable once, at import time, so pointing it at a stub here is the
    same configuration path an installed loop uses — no private attribute is patched.
    """
    previous = os.environ.get("ORCA_CLI_COMMAND")
    os.environ["ORCA_CLI_COMMAND"] = str(command)
    try:
        return load_inbox_module(skill_dir)
    finally:
        if previous is None:
            os.environ.pop("ORCA_CLI_COMMAND", None)
        else:
            os.environ["ORCA_CLI_COMMAND"] = previous


class FakeOrca:
    """A stub ``orca`` that answers ``check`` and ``worker-list`` from scripted lists.

    Each entry in ``responses`` is one complete check response envelope, exactly as the real
    CLI would print it. An entry may instead be ``{"fail": "reason"}`` to make the stub exit
    non-zero, which is how the "a CLI failure must be reported, not mistaken for an empty
    queue" behaviour is exercised.

    A plain (non ``--wait``) check consumes the next response; a ``--wait`` check serves the
    next unconsumed one without consuming it, because the real CLI replays the same FIFO
    batch until ``--ack``. The last response repeats forever, so the loop under test always
    has something to read. ``--ack`` calls are recorded and answered without advancing.

    ``worker_states`` is the scripted sequence of ``worker-list`` row lists, consumed the
    same way; an entry may again be ``{"fail": "reason"}``, or a raw string that is printed
    verbatim so a malformed envelope can be exercised. With ``keepalive`` set, a
    ``--wait`` emits keepalive lines on stderr exactly as the real CLI does.
    """

    def __init__(self, directory, responses, ack_log=None, worker_states=None, keepalive=False,
                 ack_responses=None):
        self.directory = Path(directory)
        self.responses = responses
        self.worker_states = [[]] if worker_states is None else worker_states
        self.keepalive = keepalive
        self.ack_responses = list(ack_responses or [])
        self.ack_log = Path(ack_log) if ack_log else self.directory / "acks.txt"
        self.calls_log = self.directory / "calls.txt"
        self.counter_file = self.directory / "counter.txt"
        self.ack_counter_file = self.directory / "ack-counter.txt"
        self.worker_counter_file = self.directory / "worker-counter.txt"
        self.path = self.directory / "orca"

    def write(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        responses_file = self.directory / "responses.json"
        responses_file.write_text(json.dumps(self.responses))
        workers_file = self.directory / "workers.json"
        workers_file.write_text(json.dumps(self.worker_states))
        ack_responses_file = self.directory / "ack-responses.json"
        ack_responses_file.write_text(json.dumps(self.ack_responses))
        shim = (SHIM_TEMPLATE
                .replace("__PYTHON__", sys.executable)
                .replace("__RESPONSES__", str(responses_file))
                .replace("__WORKERS__", str(workers_file))
                .replace("__ACK_RESPONSES__", str(ack_responses_file))
                .replace("__CALLS__", str(self.calls_log))
                .replace("__ACKS__", str(self.ack_log))
                .replace("__ACK_COUNTER__", str(self.ack_counter_file))
                .replace("__COUNTER__", str(self.counter_file))
                .replace("__WORKER_COUNTER__", str(self.worker_counter_file))
                .replace("__KEEPALIVE__", repr(self.keepalive))
                .replace("__CHECK_PREFIX__", CHECK_PREFIX))
        self.path.write_text(shim)
        self.path.chmod(self.path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return self

    def calls(self):
        if not self.calls_log.exists():
            return []
        return [line for line in self.calls_log.read_text().splitlines() if line]

    def calls_starting(self, prefix):
        return [call for call in self.calls() if call.startswith(prefix)]

    def acks(self):
        if not self.ack_log.exists():
            return []
        return [line for line in self.ack_log.read_text().splitlines() if line]


def batch(delivery_id, messages):
    return {"ok": True, "result": {"deliveryId": delivery_id, "messages": messages}}


def heartbeat():
    return {"id": "msg_hb", "type": "heartbeat", "body": "alive"}


def normal(message_type="worker_done", message_id="msg_done"):
    return {"id": message_id, "type": message_type, "body": "finished"}


def worker(dispatch_id="ctx_worker", state="ready", status="dispatched", detail="input_accepted"):
    """One ``worker-list`` row, in the shape the signature is read from."""
    return {
        "dispatchId": dispatch_id,
        "taskId": "task_worker",
        "runId": "run_test",
        "workerState": state,
        "dispatchStatus": status,
        "projection": {"stage": {"detail": detail}},
    }


def delivery_path(state, delivery_id):
    """The same durable name the product gives a saved batch, derived from its id."""
    digest = hashlib.sha256(delivery_id.encode()).hexdigest()
    return state / "deliveries" / (digest + ".json")


def seed_delivery(state, delivery_id, messages, mtime=None):
    """Put a saved batch on disk without running the loop: the reinstall fixture."""
    path = delivery_path(state, delivery_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(batch(delivery_id, messages), ensure_ascii=False, indent=2))
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def read_receipts(state):
    """Every append-only receipt line, in the order the consumer wrote them."""
    path = state / "receipts.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def read_state(state):
    return json.loads((state / "state.json").read_text())


def seed_binding(state, run_id="run_test", terminal="term_coordinator"):
    """The binding an installed loop leaves behind; the ack entry reads it back."""
    (state / "binding.json").write_text(json.dumps({"run": run_id, "terminal": terminal}))


def run_ack(skill_dir, state, delivery_id, orca_path):
    """Run the product's coordinator-facing ack entry point as a subprocess."""
    env = dict(os.environ)
    env["ORCA_CLI_COMMAND"] = str(orca_path)
    return subprocess.run(
        [sys.executable, str(skill_dir / SCRIPT_RELATIVE),
         "--ack", delivery_id, str(state)],
        env=env, capture_output=True, text=True, timeout=20,
        cwd=str(Path(state).resolve()),
    )


def start_loop(tmp, responses, skill_dir, state, wait_ms=200, env_extra=None,
               worker_states=None, keepalive=False, ack_responses=None):
    """Start the real loop against a scripted stub CLI, in a temporary state directory."""
    fake = FakeOrca(tmp / ("fake-" + skill_dir.name), responses,
                    worker_states=worker_states, keepalive=keepalive,
                    ack_responses=ack_responses).write()
    env = dict(os.environ)
    env["ORCA_CLI_COMMAND"] = str(fake.path)
    env["WAIT_TIMEOUT_MS"] = str(wait_ms)
    env["BACKOFF_SECONDS"] = "0.15"
    env["PENDING_BACKOFF_SECONDS"] = "0.15"
    env["WORKER_POLL_SECONDS"] = "0.1"
    env.update(env_extra or {})
    process = subprocess.Popen(
        [sys.executable, str(skill_dir / SCRIPT_RELATIVE),
         "run_test", str(state), "term_coordinator"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    return process, fake


def drain(process, timeout=30):
    """Return everything the loop printed, once; later calls reuse the drained text."""
    if getattr(process, "inbox_stdout", None) is None:
        stdout, _stderr = process.communicate(timeout=timeout)
        process.inbox_stdout = stdout or ""
    return process.inbox_stdout


def stop_loop(process):
    """Terminate the loop if it is still alive and return its drained stdout."""
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    with contextlib.suppress(subprocess.TimeoutExpired, ValueError):
        return drain(process, timeout=5)
    return getattr(process, "inbox_stdout", "")


def process_alive(pid):
    """True only while ``pid`` is a live, non-zombie process.

    ``os.kill(pid, 0)`` also succeeds for a zombie, and a child the parent never reaped stays
    a zombie — which is exactly the leak under test. Asking ``ps`` for the state is what tells
    "still running" from "killed but never reaped".
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    state = subprocess.run(
        ["ps", "-o", "state=", "-p", str(pid)], capture_output=True, text=True,
    ).stdout.strip()
    return bool(state) and not state.startswith("Z")


def wait_until_gone(pid, timeout=10.0):
    """Wait for ``pid`` to stop being a live process; return whether it did."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not process_alive(pid):
            return True
        time.sleep(0.05)
    return not process_alive(pid)


def wake_of(process, timeout=30):
    """End a loop that is expected to have exited on a wake and return its payload.

    Parsing the *whole* stdout (rather than its last line) is the assertion that the wake
    line is the only thing this process ever prints.
    """
    stdout = drain(process, timeout=timeout)
    returncode = process.returncode
    payload = json.loads(stdout)
    return returncode, payload


class InboxLoopCase(unittest.TestCase):
    """Shared harness: a temporary state dir plus a scripted fake CLI."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="inbox-loop-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.script = SCRIPT_RELATIVE
        self.skill_dir = SKILL_DIRS[0]

    def start(self, responses, skill_dir=None, env_extra=None, wait_ms=200,
              worker_states=None, keepalive=False, ack_responses=None):
        process, fake = start_loop(
            self.tmp, responses, skill_dir or self.skill_dir, self.state,
            wait_ms=wait_ms, env_extra=env_extra, worker_states=worker_states,
            keepalive=keepalive, ack_responses=ack_responses)
        # A failing assertion must not leave a loop (and its stub children) behind.
        self.addCleanup(self.stop, process)
        return process, fake

    def wait_for(self, predicate, timeout=15.0, what="condition"):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        self.fail(f"timed out waiting for {what}")

    def stop(self, process):
        return stop_loop(process)

    def wake(self, process, expect=0):
        """Assert the loop woke with the documented exit code and return its payload."""
        returncode, payload = wake_of(process)
        self.assertEqual(returncode, expect,
                         f"expected exit {expect}, got {returncode}: {payload!r}")
        self.assertEqual(payload["event"], "wake")
        return payload

    def deliveries(self):
        directory = self.state / "deliveries"
        if not directory.exists():
            return []
        return sorted(directory.glob("*.json"))


class AtomicPersistenceTests(InboxLoopCase):
    def test_delivery_is_saved_atomically_and_completely(self):
        """A delivered batch must appear on disk as one complete, valid JSON file."""
        process, _ = self.start([batch("delivery_atomic", [normal()])])
        try:
            self.wait_for(lambda: len(self.deliveries()) == 1, what="one saved delivery")
            saved = json.loads(self.deliveries()[0].read_text())
            self.assertEqual(saved["result"]["deliveryId"], "delivery_atomic")
            self.assertEqual(saved["result"]["messages"][0]["id"], "msg_done")
            # atomic() writes to a sibling temp file and renames it; no temp file may
            # survive a completed write.
            leftovers = list((self.state / "deliveries").glob("*.tmp"))
            self.assertEqual(leftovers, [], f"temp files left behind: {leftovers}")
        finally:
            self.stop(process)

    def test_write_is_durable_against_a_reread(self):
        """The saved file must always be one complete document, never a prefix."""
        process, _ = self.start([batch("delivery_reread", [normal()])])
        try:
            self.wait_for(lambda: len(self.deliveries()) == 1, what="one saved delivery")
            path = self.deliveries()[0]
            for _ in range(20):
                payload = json.loads(path.read_text())
                self.assertIn("result", payload, "file was observed half-written")
            self.assertFalse(path.with_name(path.name + ".tmp").exists())
        finally:
            self.stop(process)


class AtomicDurabilityTests(unittest.TestCase):
    """After a power loss the renamed file must still be there.

    Renaming a complete file is atomic against readers, but the directory entry itself is
    metadata: without an fsync of the parent directory the rename can be lost even though
    the file's own contents were flushed. The test injects the sync and rename calls so it
    can see the window a re-read test cannot.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="inbox-loop-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.module = load_inbox_module(SKILL_DIRS[0])

    def test_atomic_write_fsyncs_the_file_and_its_parent_directory(self):
        target = self.tmp / "delivery.json"
        events = []
        real_fsync, real_replace = os.fsync, os.replace

        def recording_fsync(descriptor):
            mode = os.fstat(descriptor).st_mode
            events.append("dir-fsync" if stat.S_ISDIR(mode) else "file-fsync")
            return real_fsync(descriptor)

        def recording_replace(source, destination):
            events.append("replace")
            return real_replace(source, destination)

        with mock.patch.object(self.module.os, "fsync", recording_fsync), \
                mock.patch.object(self.module.os, "replace", recording_replace):
            self.module.atomic(target, '{"result": "complete"}')

        self.assertEqual(target.read_text(), '{"result": "complete"}')
        self.assertEqual(
            events, ["file-fsync", "replace", "dir-fsync"],
            "the file contents, the rename, and the parent directory entry must all be "
            "durable, in that order",
        )
        self.assertEqual(list(self.tmp.glob("*.tmp")), [], "a temp file survived the write")


class WakeSignalTests(InboxLoopCase):
    def test_new_batch_ends_the_process_with_one_parseable_line(self):
        """A new non-heartbeat batch is persisted first, then announced on stdout."""
        process, _ = self.start([batch("delivery_new", [normal()])])
        payload = self.wake(process)
        self.assertEqual(payload["reason"], "delivery")
        self.assertEqual(payload["run"], "run_test")
        self.assertEqual(payload["terminal"], "term_coordinator")
        self.assertEqual(payload["deliveryId"], "delivery_new")
        self.assertEqual(payload["types"], ["worker_done"])
        self.assertEqual(payload["messageCount"], 1)
        # The file is durable *before* the wake: a coordinator that acts on this line
        # always finds the batch on disk.
        saved = Path(payload["saved"])
        self.assertTrue(saved.exists(), "the wake line named a file that was not saved")
        self.assertEqual(json.loads(saved.read_text())["result"]["deliveryId"], "delivery_new")
        state = read_state(self.state)
        self.assertIn("delivery_new", state["woken_deliveries"],
                      "a wake must be durable: a reinstall must not wake for it again")

    def test_batch_types_are_listed_in_fifo_order(self):
        """``types`` describes the whole batch, in order, not just its first message."""
        process, _ = self.start([batch("delivery_mixed_types",
                                       [normal("question", "msg_q"), heartbeat()])])
        payload = self.wake(process)
        self.assertEqual(payload["types"], ["question", "heartbeat"])
        self.assertEqual(payload["messageCount"], 2)

    def test_state_records_the_wake_for_status(self):
        """``--status`` must be able to say what ended the loop, not only that it ended."""
        process, _ = self.start([batch("delivery_state", [normal()])])
        self.wake(process)
        state = json.loads((self.state / "state.json").read_text())
        self.assertEqual(state["phase"], "wake")
        self.assertEqual(state["reason"], "delivery")
        self.assertEqual(state["deliveryId"], "delivery_state")


class HeartbeatQuietTests(InboxLoopCase):
    def test_heartbeat_only_batch_is_acknowledged_without_waking(self):
        """A batch whose every message is a heartbeat carries no decision: ack, no wake."""
        process, fake = self.start([batch("delivery_hb", [heartbeat(), heartbeat()]),
                                    batch("empty", [])])
        try:
            self.wait_for(lambda: "delivery_hb" in fake.acks(), what="the heartbeat ack")
            log_path = self.state / "watch.log"
            self.wait_for(lambda: log_path.exists()
                          and "AUTO-ACK heartbeat-only delivery_hb" in log_path.read_text(),
                          what="the auto-ack log line")
            self.wait_for(
                lambda: any(entry["result"] == "success"
                            for entry in read_receipts(self.state)),
                what="the automatic ack receipt",
            )
            receipts = read_receipts(self.state)
            self.assertEqual([entry["result"] for entry in receipts], ["pending", "success"])
            self.assertEqual({entry["request_id"] for entry in receipts}, {"ack:delivery_hb"})
            progress = load_inbox_module(SKILL_DIRS[0]).inbox_progress(self.state)
            self.assertEqual(progress["unacked"], 0,
                             "an automatic ack must reduce unacked")
            self.assertIsNone(progress["blocked"], "an automatic ack clears the blocked head")
            time.sleep(0.6)
            self.assertIsNone(process.poll(), "a heartbeat batch must not end the loop")
            self.assertEqual(self.stop(process), "", "a heartbeat batch must print nothing")
        finally:
            self.stop(process)

    def test_failed_automatic_ack_records_failed_and_wakes_once(self):
        """An Orca refusal is evidence, not silence: record it and end in one wake."""
        process, fake = self.start(
            [batch("delivery_hb_failed", [heartbeat()])],
            ack_responses=[{"fail": "simulated automatic ack failure"}],
        )
        payload = self.wake(process, expect=1)
        self.assertEqual(payload["reason"], "error")
        self.assertIn("simulated automatic ack failure", payload["detail"])
        self.assertEqual(fake.acks(), ["delivery_hb_failed"],
                         "the loop must not retry a refused ack by itself")
        self.assertEqual([entry["result"] for entry in read_receipts(self.state)],
                         ["pending", "failed"])
        module = load_inbox_module(SKILL_DIRS[0])
        progress = module.inbox_progress(self.state)
        self.assertEqual(progress["unacked"], 1, "a failed ack leaves the batch unacked")
        self.assertEqual(progress["blocked"]["deliveryId"], "delivery_hb_failed")

    def test_heartbeat_ack_still_honours_the_backoff(self):
        """Backoff is documented for an ack too: a stream of heartbeats must not hammer the CLI."""
        process, fake = self.start([batch("delivery_hb", [heartbeat()]), batch("empty", [])],
                                   env_extra={"BACKOFF_SECONDS": "5"})
        try:
            self.wait_for(lambda: "delivery_hb" in fake.acks(), what="the heartbeat ack")
            before = len(fake.calls_starting("orchestration check"))
            time.sleep(0.8)
            self.assertEqual(len(fake.calls_starting("orchestration check")), before,
                             "the loop kept calling check during the acknowledged backoff")
        finally:
            self.stop(process)

    def test_normal_message_is_not_auto_acknowledged(self):
        """A batch containing anything a human must read is left for the coordinator."""
        process, fake = self.start([batch("delivery_done", [normal()])])
        self.wake(process)
        self.assertEqual(fake.acks(), [], "a worker_done batch must not be auto-acked")
        marker = self.state / "NEW-MESSAGE"
        self.assertTrue(marker.exists(), "the PENDING marker was not written")
        self.assertIn("delivery_done", marker.read_text())

    def test_mixed_batch_is_not_auto_acknowledged(self):
        """One heartbeat plus one real message is still a batch a human must handle."""
        process, fake = self.start([batch("delivery_mixed", [heartbeat(), normal()])])
        payload = self.wake(process)
        self.assertEqual(fake.acks(), [], "a mixed batch must not be auto-acked")
        self.assertEqual(payload["messageCount"], 2)
        saved = json.loads(self.deliveries()[0].read_text())
        self.assertEqual(len(saved["result"]["messages"]), 2)


class ReplayTests(InboxLoopCase):
    def test_replayed_delivery_does_not_overwrite_the_first_read(self):
        """A FIFO replay of the same deliveryId must not clobber saved evidence.

        The first install persists the batch and wakes. A second install sees a replay of a
        batch that is already on disk: that is the coordinator's outstanding duty, not news,
        so the file must survive untouched and the loop must not wake again for it.
        """
        first = batch("delivery_replay", [normal(message_id="first_read")])
        replay = batch("delivery_replay", [normal(message_id="second_read")])
        process, _ = self.start([first])
        self.assertEqual(self.wake(process)["deliveryId"], "delivery_replay")
        path = self.deliveries()[0]
        self.assertIn("delivery_replay", read_state(self.state)["woken_deliveries"],
                      "the first install announced the batch and must have recorded it")

        second, _ = self.start([replay])
        try:
            time.sleep(1.0)
            self.assertIsNone(second.poll(), "a replay of a saved batch must not wake the loop")
            saved = json.loads(path.read_text())
            self.assertEqual(saved["result"]["messages"][0]["id"], "first_read")
            self.assertEqual(len(self.deliveries()), 1, "replay created a second file")
            self.assertIsNone(read_state(self.state).get("reconciled_delivery_id"),
                              "an already announced batch is not a reconciliation")
        finally:
            self.stop(second)

    def test_pending_marker_survives_a_later_empty_queue(self):
        """An empty queue does not prove the saved batches were handled."""
        process, _ = self.start([batch("delivery_pending", [normal()])])
        self.wake(process)
        marker = self.state / "NEW-MESSAGE"
        first = marker.read_text()
        second, _ = self.start([batch("delivery_empty", [])])
        try:
            time.sleep(0.8)
            self.assertIsNone(second.poll(), "an empty queue must not end the loop")
            self.assertTrue(marker.exists(), "the PENDING marker was cleared automatically")
            self.assertEqual(marker.read_text(), first)
            self.assertEqual(read_state(self.state)["woken_deliveries"], ["delivery_pending"],
                             "the announced batch stays not-news across the reinstall")
            self.assertIsNone(read_state(self.state).get("reconciled_delivery_id"))
        finally:
            self.stop(second)


class ReinstallReconciliationTests(InboxLoopCase):
    """A reinstall must not silently absorb a batch that was saved but never announced.

    The delivery file survives a crash before the wake line; comparing it with the
    successful receipts is what turns that gap into exactly one wake. The record is local
    (``state.json``), so the coordinator can transcribe it into the ledger; the consumer
    never writes the ledger itself.
    """

    def test_reinstall_reconciles_the_oldest_never_woken_unacknowledged_batch(self):
        acknowledged = seed_delivery(self.state, "delivery_acked", [normal()],
                                     mtime=1_700_000_000)
        orphan = seed_delivery(self.state, "delivery_orphan", [normal()],
                               mtime=1_700_000_100)
        module = load_inbox_module(SKILL_DIRS[0])
        module.append_receipt(self.state, "delivery_acked", "success")

        process, fake = self.start([batch("empty", [])])
        payload = self.wake(process)

        self.assertEqual(payload["reason"], "delivery")
        self.assertEqual(payload["deliveryId"], "delivery_orphan")
        self.assertEqual(Path(payload["saved"]).resolve(), orphan.resolve())
        self.assertIn("reconciliation", payload["detail"])
        self.assertEqual(fake.calls_starting("orchestration check"), [],
                         "reconciliation happens before the first queue read")
        self.assertEqual(len(fake.calls_starting("orchestration worker-list")), 1,
                         "the install reads the worker baseline exactly once")
        self.assertEqual(fake.acks(), [], "reconciliation never acks by itself")

        state = read_state(self.state)
        self.assertEqual(state["reconciled_delivery_id"], "delivery_orphan")
        self.assertIn("delivery_orphan", state["woken_deliveries"])
        self.assertNotIn("delivery_acked", state["woken_deliveries"])
        observation = state["observation"]
        self.assertEqual(observation["kind"], "inbox_reconciliation")
        self.assertEqual(observation["saved"], 2)
        self.assertEqual(observation["unacked"], 1)
        self.assertEqual(observation["unacked_delivery_ids"], ["delivery_orphan"])
        self.assertEqual(observation["blocked"],
                         {"deliveryId": "delivery_orphan", "reason": "unacknowledged"})
        self.assertEqual(observation["reconciled_delivery_id"], "delivery_orphan")
        self.assertEqual(observation["baseline_worker_signature"], "")
        self.assertEqual(observation["write_scope"], "state.json")
        self.assertEqual(observation["transcribe_to"], "ledger")
        self.assertTrue(acknowledged.exists(), "an acked delivery is kept as evidence")
        self.assertTrue(orphan.exists())

    def test_a_corrupt_delivery_is_an_error_wake_not_an_empty_inbox(self):
        """An unreadable delivery proves nothing; it must never be skipped as 'no news'."""
        broken = self.state / "deliveries" / "broken.json"
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.write_text("{not json")
        process, _ = self.start([batch("empty", [])])

        payload = self.wake(process, expect=1)

        self.assertEqual(payload["reason"], "error")
        self.assertIn("unreadable delivery file", payload["detail"])
        self.assertEqual(read_state(self.state)["phase"], "error")

    def test_second_reinstall_does_not_wake_again_for_the_same_batch(self):
        seed_delivery(self.state, "delivery_orphan", [normal()])
        first, _ = self.start([batch("empty", [])])
        self.assertEqual(self.wake(first)["deliveryId"], "delivery_orphan")
        reconciled = read_state(self.state)["reconciled_delivery_id"]

        second, _ = self.start([batch("empty", [])])
        try:
            time.sleep(1.0)
            self.assertIsNone(second.poll(),
                              "a persisted reconciliation must prevent a second wake")
            self.assertEqual(self.stop(second), "", "the second install printed a wake")
            self.assertEqual(read_state(self.state)["reconciled_delivery_id"], reconciled)
        finally:
            self.stop(second)


class ReceiptStatusTests(unittest.TestCase):
    """The local arithmetic that makes an unacked FIFO head visible as ``blocked``.

    It is computed from the delivery files and the append-only receipts only: status never
    consumes the Orca queue and never invents a delivery id.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="inbox-loop-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.module = load_inbox_module(SKILL_DIRS[0])

    def test_blocked_names_the_oldest_unacknowledged_delivery(self):
        seed_delivery(self.state, "delivery_old", [normal()], mtime=1_700_000_000)
        seed_delivery(self.state, "delivery_new", [normal()], mtime=1_700_000_100)

        progress = self.module.inbox_progress(self.state)

        self.assertEqual(progress["saved"], 2)
        self.assertEqual(progress["unacked"], 2)
        self.assertEqual(progress["unacked_delivery_ids"], ["delivery_old", "delivery_new"])
        self.assertEqual(
            progress["blocked"],
            {"deliveryId": "delivery_old", "reason": "unacknowledged"},
            "the FIFO head is the oldest unacknowledged delivery, named with its reason",
        )

    def test_a_successful_receipt_unblocks_the_next_delivery(self):
        seed_delivery(self.state, "delivery_old", [normal()], mtime=1_700_000_000)
        seed_delivery(self.state, "delivery_new", [normal()], mtime=1_700_000_100)

        self.module.append_receipt(self.state, "delivery_old", "pending")
        self.module.append_receipt(self.state, "delivery_old", "success")
        progress = self.module.inbox_progress(self.state)
        self.assertEqual(progress["unacked"], 1)
        self.assertEqual(progress["blocked"]["deliveryId"], "delivery_new")

        self.module.append_receipt(self.state, "delivery_new", "success")
        progress = self.module.inbox_progress(self.state)
        self.assertEqual(progress["unacked"], 0)
        self.assertIsNone(progress["blocked"], "a fully acknowledged inbox has no blocked head")

    def test_a_failed_receipt_does_not_acknowledge_a_delivery(self):
        seed_delivery(self.state, "delivery_refused", [normal()])
        self.module.append_receipt(self.state, "delivery_refused", "pending")
        self.module.append_receipt(self.state, "delivery_refused", "failed")

        progress = self.module.inbox_progress(self.state)

        self.assertEqual(progress["unacked"], 1)
        self.assertEqual(progress["blocked"]["deliveryId"], "delivery_refused")


class CoordinatorAckTests(unittest.TestCase):
    """The coordinator acks through the script, and the receipt proves what Orca accepted.

    An Orca refusal is recorded as ``failed`` and leaves the delivery unacknowledged; a
    retry reuses the same local request id. The entry never acks an id it did not save, so
    a read-only ``check --all`` cannot be turned into a fabricated acknowledgement.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="inbox-loop-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.module = load_inbox_module(SKILL_DIRS[0])
        seed_binding(self.state)

    def fake(self, name="fake-ack", ack_responses=None):
        return FakeOrca(self.tmp / name, [], ack_responses=ack_responses).write()

    def test_ack_records_pending_then_success_and_clears_the_blocked_head(self):
        delivery = seed_delivery(self.state, "delivery_one", [normal()])
        fake = self.fake()

        result = run_ack(SKILL_DIRS[0], self.state, "delivery_one", fake.path)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(fake.acks(), ["delivery_one"])
        receipts = read_receipts(self.state)
        self.assertEqual([entry["result"] for entry in receipts], ["pending", "success"])
        self.assertEqual({entry["delivery_id"] for entry in receipts}, {"delivery_one"})
        self.assertEqual({entry["request_id"] for entry in receipts}, {"ack:delivery_one"})
        self.assertTrue(all(entry["acked_at"] for entry in receipts))
        progress = self.module.inbox_progress(self.state)
        self.assertEqual(progress["unacked"], 0, "a successful ack must reduce unacked")
        self.assertIsNone(progress["blocked"], "a successful ack must clear the blocked head")
        self.assertTrue(delivery.exists())

    def test_refused_ack_records_failed_and_a_retry_reuses_the_request_id(self):
        delivery = seed_delivery(self.state, "delivery_refused", [normal()])
        before = delivery.read_bytes()
        refusing = self.fake("fake-refuse", [{"fail": "simulated ack refusal"}])

        refused = run_ack(SKILL_DIRS[0], self.state, "delivery_refused", refusing.path)

        self.assertEqual(refused.returncode, 1, "an Orca refusal must exit non-zero")
        self.assertIn("simulated ack refusal", refused.stderr)
        self.assertEqual(refusing.acks(), ["delivery_refused"],
                         "the refusal must not be retried by the entry itself")
        self.assertEqual([entry["result"] for entry in read_receipts(self.state)],
                         ["pending", "failed"])
        progress = self.module.inbox_progress(self.state)
        self.assertEqual(progress["unacked"], 1, "a failed ack leaves the delivery unacked")
        self.assertEqual(progress["blocked"]["deliveryId"], "delivery_refused")
        self.assertEqual(delivery.read_bytes(), before, "the delivery file must be kept")

        accepting = self.fake("fake-retry")
        retried = run_ack(SKILL_DIRS[0], self.state, "delivery_refused", accepting.path)

        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(accepting.acks(), ["delivery_refused"])
        receipts = read_receipts(self.state)
        self.assertEqual([entry["result"] for entry in receipts],
                         ["pending", "failed", "pending", "success"])
        self.assertEqual({entry["request_id"] for entry in receipts}, {"ack:delivery_refused"},
                         "a retry must reuse the same local request id")
        self.assertEqual(self.module.inbox_progress(self.state)["unacked"], 0)
        self.assertEqual(delivery.read_bytes(), before, "the retry must keep the delivery file")

    def test_ack_refuses_an_id_the_consumer_never_saved(self):
        fake = self.fake()

        result = run_ack(SKILL_DIRS[0], self.state, "delivery_fabricated", fake.path)

        self.assertEqual(result.returncode, 1)
        self.assertIn("no saved delivery", result.stderr)
        self.assertEqual(fake.acks(), [],
                         "an id that was never persisted must never reach Orca")
        self.assertEqual(read_receipts(self.state), [],
                         "a refused id must not produce a receipt")
        progress = self.module.inbox_progress(self.state)
        self.assertEqual(progress["unacked"], 0)
        self.assertIsNone(progress["blocked"])


class BlockingWaitTests(InboxLoopCase):
    def test_wait_is_a_blocking_call_with_the_documented_flags(self):
        """The loop waits natively, and --types is the wake condition, not a filter."""
        process, fake = self.start([batch("empty", [])])
        try:
            self.wait_for(lambda: fake.calls_starting(
                "orchestration check --run run_test --terminal term_coordinator --wait"),
                what="the blocking wait")
            waits = [call for call in fake.calls() if " --wait " in call]
            self.assertIn("--types worker_done,escalation,question", waits[0])
            self.assertIn("--timeout-ms 200", waits[0])
        finally:
            self.stop(process)

    def test_keepalive_lines_on_stderr_never_reach_stdout(self):
        """The CLI's 15-second keepalives are stderr noise, not part of the wake contract."""
        process, _ = self.start([batch("empty", []), batch("delivery_after_wait", [normal()])],
                                keepalive=True)
        payload = self.wake(process)
        self.assertEqual(payload["reason"], "delivery")
        self.assertEqual(payload["deliveryId"], "delivery_after_wait")
        self.assertEqual(payload["types"], ["worker_done"])

    def test_wait_timeout_reinstalls_the_next_wait(self):
        """A timed-out wait is not an exit: the loop installs the next one."""
        process, fake = self.start([batch("empty", [])])
        try:
            self.wait_for(lambda: len([c for c in fake.calls() if " --wait " in c]) >= 3,
                          what="three wait installs")
            self.assertIsNone(process.poll(), "a timed-out wait must not end the loop")
            self.assertEqual(self.stop(process), "", "an idle loop prints nothing")
        finally:
            self.stop(process)


class WorkerStateTests(InboxLoopCase):
    def test_worker_state_change_ends_the_wait(self):
        """A worker that changes state without sending anything must still wake the coordinator."""
        process, _ = self.start([batch("empty", [])],
                                worker_states=[[worker(state="ready")],
                                               [worker(state="working", detail="running tests")]])
        payload = self.wake(process)
        self.assertEqual(payload["reason"], "worker_state")
        self.assertIsNone(payload["deliveryId"])
        self.assertEqual(payload["types"], [])
        self.assertEqual(payload["messageCount"], 0)
        self.assertIn("ctx_worker", payload["detail"])
        self.assertIn("ready:dispatched:input_accepted", payload["detail"])
        self.assertIn("working:dispatched:running tests", payload["detail"])

    def test_unchanged_worker_signature_does_not_wake(self):
        """Polling is a comparison, not a reason to interrupt: an unchanged Run stays quiet."""
        process, fake = self.start([batch("empty", [])],
                                   worker_states=[[worker(state="ready")]])
        try:
            self.wait_for(lambda: fake.calls_starting("orchestration worker-list"), what="a poll")
            time.sleep(0.6)
            self.assertIsNone(process.poll(), "an unchanged signature must not end the loop")
            self.assertEqual(self.stop(process), "")
        finally:
            self.stop(process)

    def test_worker_watch_can_be_switched_off(self):
        """The documented switch must actually stop the reads, not only the wake."""
        process, fake = self.start([batch("empty", [])],
                                   worker_states=[[worker(state="ready")],
                                                  [worker(state="working")]],
                                   env_extra={"WORKER_WATCH": "0"})
        try:
            time.sleep(0.8)
            self.assertIsNone(process.poll(), "WORKER_WATCH=0 must leave the loop waiting")
            self.assertEqual(fake.calls_starting("orchestration worker-list"), [],
                             "WORKER_WATCH=0 still read the worker list")
        finally:
            self.stop(process)

    def test_worker_list_failure_is_reported_not_ignored(self):
        """A failing worker-list is a CLI failure like any other: loud, never 'no change'."""
        process, _ = self.start([batch("empty", [])],
                                worker_states=[{"fail": "simulated worker-list failure"}])
        payload = self.wake(process, expect=1)
        self.assertEqual(payload["reason"], "error")
        self.assertIn("simulated worker-list failure", payload["detail"])
        state = json.loads((self.state / "state.json").read_text())
        self.assertEqual(state["phase"], "error")

    def test_non_object_envelope_reaches_the_error_wake(self):
        """A CLI answering with a bare list must wake the error leg, not escape uncaught.

        An AttributeError would leave no wake line and no ``state.json phase=error``: exactly
        the two things that tell "the loop died loudly" from "the loop vanished".
        """
        process, _ = self.start([batch("empty", [])], worker_states=["[]"])
        payload = self.wake(process, expect=1)
        self.assertEqual(payload["reason"], "error")
        self.assertIn("without a JSON object", payload["detail"])
        state = json.loads((self.state / "state.json").read_text())
        self.assertEqual(state["phase"], "error")
        self.assertIn("ERROR", (self.state / "watch.log").read_text())


class FailureReportingTests(InboxLoopCase):
    def test_cli_error_is_reported_and_exits_nonzero(self):
        """A CLI failure must never be reinterpreted as an empty queue."""
        process, _ = self.start([{"fail": "simulated CLI failure"}])
        payload = self.wake(process, expect=1)
        self.assertEqual(payload["reason"], "error")
        self.assertIsNone(payload["deliveryId"])
        self.assertEqual(payload["messageCount"], 0)
        self.assertIn("CLI exit=1", payload["detail"])
        state = json.loads((self.state / "state.json").read_text())
        self.assertEqual(state["phase"], "error")
        self.assertIn("CLI exit=1", state["error"])
        log = (self.state / "watch.log").read_text()
        self.assertIn("ERROR", log)
        self.assertIn("simulated CLI failure", log)

    def test_status_reports_the_reason_after_a_fatal_exit(self):
        """A dead consumer must be readable from state.json, not from the pid file alone.

        The pid file is unlinked on the way out, so "no pid file" is a death like any other and
        the only place that says why is the state the loop wrote before exiting.
        """
        process, _ = self.start([{"fail": "simulated CLI failure"}])
        self.wake(process, expect=1)
        self.wait_for(lambda: not (self.state / "inbox-loop.pid").exists(),
                      what="the pid file to be removed on exit")
        result = subprocess.run(
            [sys.executable, str(self.skill_dir / self.script), "--status", str(self.state)],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("loop: DEAD", result.stdout)
        self.assertIn('"phase": "error"', result.stdout)
        self.assertIn("simulated CLI failure", result.stdout)

    def test_nonempty_batch_without_delivery_id_is_an_error(self):
        """A batch with messages but no deliveryId cannot be acknowledged safely."""
        process, _ = self.start([
            {"ok": True, "result": {"messages": [normal()]}},
        ])
        payload = self.wake(process, expect=1)
        self.assertEqual(payload["reason"], "error")
        state = json.loads((self.state / "state.json").read_text())
        self.assertEqual(state["phase"], "error")
        self.assertIn("deliveryId", state["error"])


class BindingTests(InboxLoopCase):
    def test_state_directory_cannot_be_rebound_to_another_run(self):
        """A directory bound to one Run/terminal/CLI must refuse a different binding."""
        process, _ = self.start([batch("delivery_bind", [heartbeat()])])
        try:
            binding_file = self.state / "binding.json"
            self.wait_for(binding_file.exists, what="binding file")
            self.wait_for(lambda: binding_file.read_text() != "", what="binding content")
        finally:
            self.stop(process)
        # A second invocation for a different Run must refuse to take over.
        env = dict(os.environ)
        env["ORCA_CLI_COMMAND"] = str(FakeOrca(self.tmp / "fake-rebind", []).path)
        result = subprocess.run(
            [sys.executable, str(self.skill_dir / self.script),
             "another_run", str(self.state), "term_other"],
            env=env, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("another Run", result.stderr)
        self.assertEqual(json.loads(result.stdout)["reason"], "error")
        # The refusal is a failure and must leave durable evidence — in the shared log, not
        # by overwriting the state file that belongs to the Run already bound here.
        self.assertIn("refused rebinding", (self.state / "watch.log").read_text())
        binding = json.loads((self.state / "binding.json").read_text())
        self.assertEqual(binding["run"], "run_test")

    def test_status_reports_dead_for_a_missing_directory(self):
        """--status must not consume the queue and must report a missing dir as DEAD."""
        missing = self.tmp / "does-not-exist"
        result = subprocess.run(
            [sys.executable, str(self.skill_dir / self.script), "--status", str(missing)],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("loop: DEAD", result.stdout)

    def test_status_reports_alive_for_a_running_loop(self):
        process, _ = self.start([batch("delivery_alive", [heartbeat()])])
        try:
            self.wait_for(lambda: (self.state / "inbox-loop.pid").exists(), what="pid file")
            result = subprocess.run(
                [sys.executable, str(self.skill_dir / self.script), "--status", str(self.state)],
                capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(result.returncode, 0)
            self.assertIn("loop: ALIVE", result.stdout)
        finally:
            self.stop(process)

    def test_status_reports_dead_after_a_wake(self):
        """After a wake the loop is gone on purpose, and --status must say so, not hang."""
        process, _ = self.start([batch("delivery_after", [normal()])])
        self.wake(process)
        self.wait_for(lambda: not (self.state / "inbox-loop.pid").exists(),
                      what="the pid file to be cleared")
        result = subprocess.run(
            [sys.executable, str(self.skill_dir / self.script), "--status", str(self.state)],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("loop: DEAD", result.stdout)
        self.assertIn('"phase": "wake"', result.stdout)


class SingleInstanceTests(InboxLoopCase):
    def test_second_instance_for_the_same_directory_is_refused(self):
        """The directory lock keeps two consumers off one Run's queue."""
        process, fake = self.start([batch("delivery_lock", [heartbeat()])])
        try:
            self.wait_for(lambda: (self.state / "inbox-loop.pid").exists(), what="pid file")
            env = dict(os.environ)
            env["ORCA_CLI_COMMAND"] = str(fake.path)
            result = subprocess.run(
                [sys.executable, str(self.skill_dir / self.script),
                 "run_test", str(self.state), "term_coordinator"],
                env=env, capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("already running", result.stderr)
            self.assertEqual(json.loads(result.stdout)["reason"], "error")
        finally:
            self.stop(process)


class BlockingCli:
    """A stub CLI whose ``check --wait`` blocks and whose ``worker-list`` can be made to block.

    Every invocation appends its own pid and its argv, so a test can tell which child is
    still running at the moment it intervenes. ``worker-list`` blocks only while ``marker``
    exists, which lets the test choose the exact read that is in flight.
    """

    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / "orca"
        self.pids_file = self.directory / "pids.txt"
        self.calls_file = self.directory / "calls.txt"
        self.marker = self.directory / "slow-worker-list"

    def write(self, check_sleep=30, worker_sleep=10):
        self.directory.mkdir(parents=True, exist_ok=True)
        shim = (BLOCKING_SHIM_TEMPLATE
                .replace("__PYTHON__", sys.executable)
                .replace("__PIDS__", str(self.pids_file))
                .replace("__CALLS__", str(self.calls_file))
                .replace("__MARKER__", str(self.marker))
                .replace("__WORKER_SLEEP__", str(worker_sleep))
                .replace("__CHECK_SLEEP__", str(check_sleep))
                .replace("__CHECK_PREFIX__", CHECK_PREFIX))
        self.path.write_text(shim)
        self.path.chmod(self.path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return self

    def pids(self):
        if not self.pids_file.exists():
            return []
        return [int(line) for line in self.pids_file.read_text().split() if line]

    def calls(self):
        if not self.calls_file.exists():
            return []
        return [line for line in self.calls_file.read_text().splitlines() if line]

    def block_worker_list(self):
        self.marker.write_text("")


class ChildProcessCleanupTests(unittest.TestCase):
    """A CLI child must never outlive the call that owns it.

    The defect these cases pin: ``cli_streams`` restored the ``CHILD`` slot and deleted the
    stream files but left the child running, so an exception raised by the worker-state probe
    (or a signal that arrived while a nested ``worker-list`` held the slot) left the blocked
    ``check --wait`` process behind. That process keeps its CLI waiter slot, so the next
    install fails and the Run looks watched while nothing is watching it.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="inbox-loop-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cli = BlockingCli(self.tmp / "fake-blocking").write()
        # Registered after the rmtree cleanup, so it runs first (LIFO): a case that fails its
        # assertion must still reap the stub children it started, rather than leaving them to
        # sleep out their 30-second block.
        self.addCleanup(self.reap_stub_children)

    def reap_stub_children(self):
        """Terminate and reap every stub child this case started."""
        for pid in self.cli.pids():
            if process_alive(pid):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGTERM)
        for pid in self.cli.pids():
            wait_until_gone(pid, timeout=5)

    def wait_for(self, predicate, timeout=20.0, what="condition"):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        self.fail(f"timed out waiting for {what}")

    def test_a_failing_probe_reaps_the_blocked_wait(self):
        """The reproduced defect: the probe raises, and the waiter must not survive the call."""
        module = load_inbox_module_with_cli(SKILL_DIRS[0], self.cli.path)

        def probe():
            raise RuntimeError("worker-list failed while the wait was in flight")

        with self.assertRaises(RuntimeError):
            module.check(self.tmp, "run_test", "term_coordinator",
                         ["--wait", "--types", "worker_done", "--timeout-ms", "30000"],
                         timeout=30, watch=(0.5, probe))
        child_pid = json.loads((self.tmp / "state.json").read_text())["childPid"]
        self.assertIn(child_pid, self.cli.pids(),
                      "state.json must name the child that was actually started")
        self.assertTrue(wait_until_gone(child_pid),
                        "the blocked check --wait child survived the probe failure")
        self.assertIsNone(module.CHILD, "the CHILD slot must not keep a dead call")

    def test_a_nested_cli_call_does_not_hide_the_wait_from_cleanup(self):
        """The probe starts a nested ``worker-list``; both children must be reaped on failure.

        ``cli_streams`` restores the previous registration on the way out, so a nested call
        leaves the outer waiter in the slot of nothing: cleanup has to walk every child the
        process started, not only the innermost one.
        """
        module = load_inbox_module_with_cli(SKILL_DIRS[0], self.cli.path)
        nested = {}

        def probe():
            nested["signature"] = module.worker_signature(self.tmp, "run_test")
            raise RuntimeError("probe failed after the nested call returned")

        with self.assertRaises(RuntimeError):
            module.check(self.tmp, "run_test", "term_coordinator",
                         ["--wait", "--types", "worker_done", "--timeout-ms", "30000"],
                         timeout=30, watch=(0.5, probe))
        self.assertEqual(nested["signature"], "")
        child_pid = json.loads((self.tmp / "state.json").read_text())["childPid"]
        self.assertIn(child_pid, self.cli.pids(),
                      "the outer wait was never started by the stub")
        self.assertEqual(len(self.cli.pids()), 2,
                         "expected the wait and the nested worker-list")
        for pid in self.cli.pids():
            self.assertTrue(wait_until_gone(pid), f"child {pid} survived the failed call")
        self.assertIsNone(module.CHILD)

    def test_a_signal_during_a_nested_call_reaps_every_child(self):
        """SIGTERM while a nested poll holds the slot must still end both processes.

        This is the same defect on the exit path, and the one that actually bites: a leaked
        waiter is invisible while the loop runs and fatal on the next install.
        """
        state = self.tmp / "state"
        state.mkdir()
        env = dict(os.environ)
        env["ORCA_CLI_COMMAND"] = str(self.cli.path)
        env["WAIT_TIMEOUT_MS"] = "30000"
        env["WORKER_POLL_SECONDS"] = "0.1"
        env["BACKOFF_SECONDS"] = "0"
        process = subprocess.Popen(
            [sys.executable, str(SKILL_DIRS[0] / SCRIPT_RELATIVE),
             "run_test", str(state), "term_coordinator"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            self.wait_for(lambda: any(" --wait " in call for call in self.cli.calls()),
                          what="the blocking wait to be installed")
            before = len(self.cli.pids())
            self.cli.block_worker_list()
            self.wait_for(lambda: len(self.cli.pids()) > before,
                          what="the nested poll to start and block")
            process.terminate()
            process.wait(timeout=20)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
        self.assertEqual(process.returncode, 0, "a signalled loop exits 0")
        for pid in self.cli.pids():
            self.assertTrue(wait_until_gone(pid), f"child {pid} survived SIGTERM")


class BothLanguageVariantsTests(unittest.TestCase):
    """The two language variants must ship the same script, byte for byte."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="inbox-loop-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_scripts_are_identical_across_language_variants(self):
        english = (SKILL_DIRS[0] / SCRIPT_RELATIVE).read_bytes()
        chinese = (SKILL_DIRS[1] / SCRIPT_RELATIVE).read_bytes()
        self.assertEqual(english, chinese)

    def test_the_chinese_variant_wakes_the_same_way(self):
        """Byte equality is the guarantee; this is the behaviour behind it, in one run."""
        state = self.tmp / "zh-state"
        state.mkdir()
        process, _ = start_loop(self.tmp, [batch("delivery_zh", [normal()])],
                                SKILL_DIRS[1], state)
        returncode, payload = wake_of(process)
        self.assertEqual(returncode, 0)
        self.assertEqual(payload["reason"], "delivery")
        self.assertEqual(payload["deliveryId"], "delivery_zh")

    def test_modules_expose_the_same_public_functions(self):
        english = load_inbox_module(SKILL_DIRS[0])
        chinese = load_inbox_module(SKILL_DIRS[1])
        for name in ("atomic", "batch_messages", "check", "cli_streams", "emit", "env_flag",
                     "env_number", "fatal", "idle", "locked", "main", "parse_cli_result",
                     "persist_batch", "status", "wake_event", "worker_changes",
                     "worker_signature"):
            self.assertTrue(hasattr(english, name), f"missing {name}")
            self.assertTrue(hasattr(chinese, name), f"missing {name}")


class SignatureTests(unittest.TestCase):
    """The signature and its diff are pure functions, so they are tested directly."""

    def setUp(self):
        self.module = load_inbox_module(SKILL_DIRS[0])

    def changes(self, before, after):
        return self.module.worker_changes(before, after)

    def test_a_replaced_worker_is_visible(self):
        """Comparing counts would miss this; comparing lines cannot."""
        before = "ctx_a:ready:dispatched:input_accepted"
        after = "ctx_b:ready:dispatched:input_accepted"
        self.assertEqual(len(self.changes(before, after)), 2)

    def test_a_transition_reads_as_one_before_and_after(self):
        before = "ctx_a:ready:dispatched:input_accepted"
        after = "ctx_a:working:dispatched:running tests"
        self.assertEqual(
            self.changes(before, after),
            ["ctx_a ready:dispatched:input_accepted -> working:dispatched:running tests"],
        )

    def test_an_unchanged_signature_reports_nothing(self):
        same = "ctx_a:ready:dispatched:input_accepted\nctx_b:working:dispatched:running"
        self.assertEqual(self.changes(same, same), [])


class EnvironmentOverrideTests(unittest.TestCase):
    def test_invalid_numeric_override_falls_back_to_the_default(self):
        module = load_inbox_module(SKILL_DIRS[0])
        previous = os.environ.get("WAIT_TIMEOUT_MS")
        os.environ["WAIT_TIMEOUT_MS"] = "not-a-number"
        try:
            self.assertEqual(module.env_number("WAIT_TIMEOUT_MS", 30000, int), 30000)
        finally:
            if previous is None:
                os.environ.pop("WAIT_TIMEOUT_MS", None)
            else:
                os.environ["WAIT_TIMEOUT_MS"] = previous

    def test_valid_numeric_override_is_used(self):
        module = load_inbox_module(SKILL_DIRS[0])
        previous = os.environ.get("BACKOFF_SECONDS")
        os.environ["BACKOFF_SECONDS"] = "0.25"
        try:
            self.assertEqual(module.env_number("BACKOFF_SECONDS", 5.0, float), 0.25)
        finally:
            if previous is None:
                os.environ.pop("BACKOFF_SECONDS", None)
            else:
                os.environ["BACKOFF_SECONDS"] = previous

    def test_flag_override_reads_booleans_and_rejects_noise(self):
        """The documented switch must be readable, and a typo must not change the default."""
        module = load_inbox_module(SKILL_DIRS[0])
        previous = os.environ.get("WORKER_WATCH")
        try:
            os.environ["WORKER_WATCH"] = "off"
            self.assertFalse(module.env_flag("WORKER_WATCH", True))
            os.environ["WORKER_WATCH"] = "1"
            self.assertTrue(module.env_flag("WORKER_WATCH", False))
            os.environ["WORKER_WATCH"] = "maybe"
            self.assertTrue(module.env_flag("WORKER_WATCH", True))
        finally:
            if previous is None:
                os.environ.pop("WORKER_WATCH", None)
            else:
                os.environ["WORKER_WATCH"] = previous

    def test_wake_types_match_the_documented_wake_condition(self):
        module = load_inbox_module(SKILL_DIRS[0])
        self.assertEqual(module.WAKE_TYPES, "worker_done,escalation,question")


if __name__ == "__main__":
    unittest.main()
