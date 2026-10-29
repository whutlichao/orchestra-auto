#!/usr/bin/env python3
"""Orca inbox loop: persist every delivery batch, and end the process on a wake event.

These safety guarantees must survive every change to this file:

* every batch the CLI returns is written to disk atomically, keyed by a hash of its
  ``deliveryId``, before anything else happens, and the parent directory is fsynced after
  the rename, so a power loss can neither lose a message nor roll back the saved name;
* an already saved delivery is never overwritten, so a FIFO replay cannot erase the
  evidence of the first read;
* every ack — an automatic heartbeat-only one and a coordinator one through ``--ack`` —
  appends ``{delivery_id, request_id, acked_at, result}`` to the append-only receipt file.
  A receipt is appended as ``pending`` before the Orca call and as ``success`` or ``failed``
  after it, so local state never claims an ack Orca refused and a failed ack stays unacked;
* a batch whose every message is a heartbeat is acknowledged automatically, because it
  carries no decision; any other batch is left for the coordinator to handle and
  acknowledge itself;
* a CLI failure, a JSON error or a lost binding is reported as an error and exits
  non-zero. It is never reinterpreted as "the queue was empty", and the loop never retries
  by itself;
* a CLI child never outlives the call that owns it. An exception from the worker-state probe,
  a wake, or a signal reaps every process this loop started, because a survivor keeps its
  waiter slot: the next install then fails while the Run still looks watched.

On top of them this process owns the wake contract, because a file on disk cannot start a
coordinator turn by itself — only the end of a command the harness surfaces can:

* a **newly persisted** batch that carries anything other than heartbeats ends the process
  and prints one machine-readable line on stdout (``event``, ``reason``, ``deliveryId``);
  the end of the process, not the file, is the wake;
* a **reinstall** compares every saved delivery with the successful receipts. If the oldest
  unacknowledged batch was saved but never announced, it emits exactly one wake naming the
  reconciliation, records ``reconciled_delivery_id`` and a local observation in
  ``state.json``, and exits. A batch already announced is never news again, and the consumer
  never writes the ledger — the coordinator transcribes the observation;
* a change in this Run's worker signature — the read-only ``worker-list --run``, reduced to
  ``dispatchId:workerState:dispatchStatus:stage`` per worker — is the second wake leg, so
  the coordinator does not have to wait for a ``worker_done`` that may never come;
* a fatal error of the loop itself is the third leg: it exits non-zero and prints the same
  line with ``reason=error``.

Waiting is a native block (``check --wait --types --timeout-ms``) rather than a short
poll-and-sleep: the CLI holds the connection and writes its keepalive lines to stderr,
which never reach this process's stdout. Every wake event ends the process, so the
coordinator handles the batch and installs the next wait; not re-installing one is a silent
stall. ``--status`` still inspects a directory without consuming its queue.

A handled batch is acknowledged through ``--ack DELIVERY_ID STATE_DIR``: the entry writes
the pending receipt, asks Orca, then records ``success`` or ``failed``. It refuses an id
this consumer never saved, because a read-only ``check --all`` cannot yield an ackable id.

Only the Python standard library is used (POSIX file locks; macOS, Linux and WSL).
"""

import contextlib
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

CLI = os.environ.get("ORCA_CLI_COMMAND", "orca")

#: The child the signal handler names first — always the innermost CLI call.
CHILD = None

#: Every child this process started and has not reaped yet. ``CHILD`` alone cannot serve
#: cleanup: a nested call restores the previous registration on its way out, so the outer
#: waiter is no longer in the slot when the exception or the signal arrives. Nothing outside
#: this list is ever signalled, which is what keeps cleanup off unrelated processes.
LIVE_CHILDREN = []

#: Set by the signal handler, so a child spawned just before the signal is still reaped.
STOPPING = False

#: The wake condition handed to a blocking ``check --wait``. It decides when the wait
#: returns; the returned Delivery is always the whole FIFO batch, so it is never filtered.
WAKE_TYPES = "worker_done,escalation,question"

#: How long one finite CLI call (plain check, ack, worker-list) may take.
CLI_TIMEOUT_SECONDS = 45

#: Grace added on top of ``--timeout-ms`` so the CLI's own timeout, not ours, decides.
WAIT_GRACE_SECONDS = 15

#: Append-only ack receipts in the state directory. A delivery is acknowledged locally only
#: by a ``success`` entry; ``pending`` and ``failed`` are evidence, not proof.
RECEIPTS_FILE = "receipts.jsonl"

#: Local durable markers. They survive every state rewrite because they answer questions a
#: transient phase cannot: which batches were already announced, and which one a reinstall
#: reconciliation already woke for.
WOKEN_KEY = "woken_deliveries"
RECONCILED_KEY = "reconciled_delivery_id"
OBSERVATION_KEY = "observation"
LOCAL_STATE_KEYS = (WOKEN_KEY, RECONCILED_KEY, OBSERVATION_KEY)


class Wake(Exception):
    """A wake event detected while a ``check --wait`` child was still blocked."""

    def __init__(self, payload):
        super().__init__(payload.get("detail", "wake"))
        self.payload = payload


def env_number(name, default, cast):
    """Read a numeric environment override, falling back on a bad value.

    A typo in an override must not take the inbox down: an unusable value falls back
    to the documented default and is reported on stderr.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return cast(raw)
    except ValueError:
        print(f"ignoring invalid {name}={raw!r}; using {default}", file=sys.stderr)
        return default


def env_flag(name, default):
    """Read a boolean environment override, falling back on an unusable value.

    The switch is documented, so it must be honest: an unrecognised value falls back to
    the documented default and says so on stderr rather than silently meaning either.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    lowered = raw.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    print(f"ignoring invalid {name}={raw!r}; using {default}", file=sys.stderr)
    return default


def fsync_directory(directory):
    """Flush a directory entry, so a completed rename survives power loss.

    The file's own fsync makes its bytes durable; the name that points at them is metadata
    in the parent directory, and a crash after ``os.replace`` but before that directory is
    flushed can roll the rename back. Opening the directory read-only and syncing it closes
    that window on POSIX filesystems.
    """
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic(path, value):
    """Write ``value`` to ``path`` atomically and durably.

    The temporary file is written and fsynced in the same directory as the target, so
    ``os.replace`` is a same-filesystem rename: a reader either sees the previous
    content or the complete new content, never a half-written file. The parent directory
    is then fsynced, because the rename itself is a directory-entry change: without it a
    delivery that the loop reported as saved could disappear after a power loss.
    """
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("w") as out:
            out.write(value)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
        fsync_directory(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def read_json(path):
    """Read JSON, returning ``{}`` when the file is missing or unreadable."""
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def log(directory, message):
    with (directory / "watch.log").open("a") as out:
        out.write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + " " + message + "\n")


def write_state(directory, payload, current=None):
    """Write ``state.json`` while keeping the durable local markers.

    Phase, error text and the pid of the moment are transient: a new install must be able
    to replace them. ``woken_deliveries``, ``reconciled_delivery_id`` and the last
    reconciliation ``observation`` are not — they are how a restart knows what was already
    announced, so every rewrite carries them forward. ``current`` lets the install-time
    reconciliation reuse the one read it already made.
    """
    if current is None:
        current = read_json(directory / "state.json")
    merged = {key: current[key] for key in LOCAL_STATE_KEYS if key in current}
    merged.update(payload)
    atomic(directory / "state.json", json.dumps(merged))


def ack_request_id(delivery_id):
    """The stable local key for one delivery's ack.

    A retry after a refused Orca ack reuses it, so the receipt log shows one logical ack
    attempt rather than a sequence of unrelated ones.
    """
    return "ack:" + delivery_id


def append_receipt(directory, delivery_id, result):
    """Append one ack receipt line and fsync it before returning.

    ``pending`` is appended before the Orca call and the outcome after it, so a local
    record can never claim a success that Orca never granted. The append is serialized with
    ``flock`` because the coordinator's ``--ack`` is a separate process. A partial trailing
    line left by a crash is ignored on read rather than mistaken for a decision.
    """
    entry = {
        "delivery_id": delivery_id,
        "request_id": ack_request_id(delivery_id),
        "acked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "result": result,
    }
    path = directory / RECEIPTS_FILE
    line = json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
    with path.open("a") as out:
        fcntl.flock(out, fcntl.LOCK_EX)
        try:
            out.write(line)
            out.flush()
            os.fsync(out.fileno())
        finally:
            fcntl.flock(out, fcntl.LOCK_UN)
    fsync_directory(directory)
    return entry


def read_receipts(directory):
    """Read every complete receipt line, in append order."""
    try:
        text = (directory / RECEIPTS_FILE).read_text()
    except OSError:
        return []
    entries = []
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            # A crash can leave a trailing partial append. It proves nothing, so it is
            # skipped instead of being read as a failed or a successful ack.
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def acked_delivery_ids(directory):
    """Delivery ids with at least one successful receipt.

    Success is sticky: once Orca accepted an ack, a later retry of the same local key
    cannot make the delivery unacknowledged again.
    """
    acked = set()
    for entry in read_receipts(directory):
        if entry.get("result") == "success" and isinstance(entry.get("delivery_id"), str):
            acked.add(entry["delivery_id"])
    return acked


def delivery_rows(directory):
    """Every saved delivery, oldest first, with the id its content names.

    File mtime is the only local FIFO evidence: it is when this consumer persisted the
    batch, and the oldest unacknowledged one is the head Orca would return. A corrupt file
    raises instead of being skipped — an unreadable delivery is an error, not an empty
    queue.
    """
    rows = []
    for path in (directory / "deliveries").glob("*.json"):
        try:
            payload = json.loads(path.read_text())
            result = payload["result"]
            delivery_id = result["deliveryId"]
            messages = result["messages"]
        except (OSError, ValueError, KeyError, TypeError) as broken:
            raise RuntimeError(f"unreadable delivery file {path.name}: {broken}") from broken
        if not isinstance(delivery_id, str) or not delivery_id:
            raise RuntimeError(f"delivery file {path.name} carries no deliveryId")
        if not isinstance(messages, list):
            raise RuntimeError(f"delivery file {path.name} carries no messages array")
        rows.append({
            "path": path,
            "deliveryId": delivery_id,
            "types": [m.get("type") if isinstance(m, dict) else None for m in messages],
            "mtime": path.stat().st_mtime,
        })
    return sorted(rows, key=lambda row: (row["mtime"], row["path"].name))


def inbox_progress(directory):
    """Local saved/unacked arithmetic and the blocked FIFO head.

    ``unacked`` is the saved deliveries without a successful receipt, and ``blocked`` names
    the oldest of them with its reason. This reads local files only: it never calls the CLI
    and never consumes the queue, and it never invents a delivery id.
    """
    rows = delivery_rows(directory)
    acked = acked_delivery_ids(directory)
    unacked = [row for row in rows if row["deliveryId"] not in acked]
    blocked = None
    if unacked:
        blocked = {"deliveryId": unacked[0]["deliveryId"], "reason": "unacknowledged"}
    return {
        "saved": len(rows),
        "unacked": len(unacked),
        "unacked_delivery_ids": [row["deliveryId"] for row in unacked],
        "blocked": blocked,
        "oldest_unacked": unacked[0] if unacked else None,
        "rows": rows,
    }


def woken_deliveries(directory, state=None):
    """Delivery ids whose arrival this consumer already announced."""
    if state is None:
        state = read_json(directory / "state.json")
    woken = state.get(WOKEN_KEY)
    if not isinstance(woken, list):
        return []
    return [item for item in woken if isinstance(item, str)]


def mark_woken(directory, delivery_id):
    """Record that a wake for this batch was emitted, before the process ends.

    The write happens before the wake line, so a batch that was really announced is never
    reconciled as if it had been silent. If the process dies between the delivery write and
    this marker, the reinstall reconciliation is exactly what covers that window.
    """
    current = read_json(directory / "state.json")
    woken = woken_deliveries(directory, current)
    if delivery_id not in woken:
        woken = woken + [delivery_id]
    write_state(directory, {WOKEN_KEY: woken}, current=current)


def reconcile_reinstall(directory, run_id, terminal, baseline):
    """Compare saved deliveries with receipts once per install.

    Returns the one wake payload this install must emit, or ``None``. The oldest
    unacknowledged batch that the consumer saved but never announced is the gap a crash
    leaves between the delivery write and the wake line; waking once for it is how a
    reinstall does not silently absorb it. A batch already announced stays the
    coordinator's outstanding duty and is not news. ``reconciled_delivery_id`` and the
    local observation are written before the wake, so neither a crash nor a second install
    can wake twice for the same batch.

    The observation is deliberately local: the consumer writes ``state.json`` only, and the
    coordinator transcribes the record into the ledger.
    """
    current = read_json(directory / "state.json")
    progress = inbox_progress(directory)
    woken = woken_deliveries(directory, current)
    previous = current.get(RECONCILED_KEY)
    reconciled_id = previous if isinstance(previous, str) and previous else None
    oldest = progress["oldest_unacked"]
    payload = None
    if oldest is not None and oldest["deliveryId"] not in woken \
            and oldest["deliveryId"] != reconciled_id:
        reconciled_id = oldest["deliveryId"]
        woken = woken + [reconciled_id]
        payload = wake_event(
            "delivery", run_id, terminal,
            f"reconciliation: oldest unacknowledged batch {reconciled_id} was saved but "
            f"never announced; {progress['unacked']} unacknowledged of "
            f"{progress['saved']} saved",
            delivery_id=reconciled_id, types=oldest["types"], saved=str(oldest["path"]))
    observation = {
        "kind": "inbox_reconciliation",
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "saved": progress["saved"],
        "unacked": progress["unacked"],
        "unacked_delivery_ids": progress["unacked_delivery_ids"],
        "blocked": progress["blocked"],
        "reconciled_delivery_id": reconciled_id,
        "baseline_worker_signature": baseline,
        "write_scope": "state.json",
        "transcribe_to": "ledger",
    }
    write_state(directory, {
        WOKEN_KEY: woken,
        RECONCILED_KEY: reconciled_id,
        OBSERVATION_KEY: observation,
        "phase": "reconciling",
    }, current=current)
    return payload


def parse_cli_result(code, raw, problem):
    """Turn one finished CLI call into its envelope, or raise the real reason.

    The exit code is examined *before* the output is parsed. A failing CLI often prints
    nothing on stdout, and parsing that empty text first would replace the real reason
    (the exit code and stderr) with a JSON syntax error — exactly the kind of lost
    evidence that makes a hung orchestration hard to diagnose.
    """
    if code != 0:
        raise RuntimeError(f"CLI exit={code}: {raw[:1000]} {problem[-1000:]}")
    try:
        data = json.loads(raw)
    except ValueError as invalid:
        raise RuntimeError(f"CLI exit=0 with unparsable JSON: {raw[:1000]} {problem[-1000:]}") from invalid
    if not isinstance(data, dict):
        # An envelope that is not even an object (a bare list, a number, a string) is the
        # same class of failure as a missing ``ok``: it has to reach the fatal path, which
        # is a wake with evidence, instead of escaping as an AttributeError that leaves no
        # wake line and no ``state.json phase=error`` behind.
        raise RuntimeError(f"CLI exit=0 without a JSON object: {raw[:1000]} {problem[-1000:]}")
    if data.get("ok") is not True or not isinstance(data.get("result"), dict):
        raise RuntimeError(f"CLI exit=0 without an ok result: {raw[:1000]} {problem[-1000:]}")
    return data


@contextlib.contextmanager
def cli_streams(directory, command, suffix):
    """Run a CLI child with its streams in files, and reap it on every exit path.

    Streams go to files rather than pipes so a CLI that keeps the connection alive cannot
    fill a pipe buffer and wedge the loop. Yields ``(child, output, error)``: the caller
    decides how to wait — a bounded wait, or the supervised block ``check`` needs — and
    reads the two paths once the child has exited. The child is published as ``CHILD`` so
    the signal handler reaps it, and registered in ``LIVE_CHILDREN`` so cleanup also finds
    the children a nested call left behind. The previous registration is restored afterwards
    so a short call nested inside a long ``check --wait`` cannot hide the waiter from it.

    The child is reaped here, not by the caller, because the caller's ``finally`` does not run
    when a probe raises: the blocked waiter would survive the very call that was supposed to
    end it, and keep the CLI waiter slot that the next install needs.
    """
    global CHILD
    previous = CHILD
    token = uuid.uuid4().hex
    output = directory / (token + suffix + ".stdout.tmp")
    error = directory / (token + suffix + ".stderr.tmp")
    child = None
    try:
        with output.open("w") as out, error.open("w") as err:
            child = subprocess.Popen(command, stdout=out, stderr=err)
            # Registration happens before anything else can raise, so every exit path can
            # reach this child — including the signal that interrupted the spawn.
            LIVE_CHILDREN.append(child)
            CHILD = child
            if STOPPING:
                # The handler ran between the spawn and this line and could not see the
                # child yet; the exit path takes over here instead.
                raise SystemExit(0)
            yield child, output, error
    finally:
        reap(child)
        if child in LIVE_CHILDREN:
            LIVE_CHILDREN.remove(child)
        CHILD = previous
        output.unlink(missing_ok=True)
        error.unlink(missing_ok=True)


def reap(child):
    """Terminate and reap one child this process started, if it is still running.

    A wake ends the process, so nothing may hold a CLI waiter slot after it: a child left
    behind outlives the call that owned it and makes the next install fail while the Run
    still looks watched. Only a child of ours is ever signalled.
    """
    if child is None or child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=3)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def reap_all():
    """Reap every child this process is still responsible for."""
    for child in list(LIVE_CHILDREN):
        reap(child)


def cli_run(directory, command, label, timeout=CLI_TIMEOUT_SECONDS):
    """Run one finite CLI command and return ``(exit_code, stdout, stderr)``."""
    with cli_streams(directory, command, ".call") as (child, output, error):
        try:
            child.wait(timeout=timeout)
        except subprocess.TimeoutExpired as expired:
            child.kill()
            child.wait()
            raise RuntimeError(f"{label} timeout") from expired
        return child.returncode, output.read_text(), error.read_text()


def supervise(child, timeout, watch):
    """Wait for a CLI child for at most ``timeout`` seconds, polling ``watch`` meanwhile.

    A child that outlives the deadline is killed and reported as a CLI timeout. ``watch``
    is an optional ``(seconds, probe)`` pair: while the CLI is blocked, the probe runs
    every ``seconds``, and a reported wake stops the child immediately — so a worker-state
    change does not have to wait for the full ``--timeout-ms``, and no waiter slot is left
    held by a process that is about to end.
    """
    deadline = time.monotonic() + timeout
    interval, watch_probe = watch if watch else (0.0, None)
    next_poll = time.monotonic() + interval
    while child.poll() is None:
        now = time.monotonic()
        if now >= deadline:
            child.kill()
            child.wait()
            raise RuntimeError("CLI timeout")
        if watch_probe is not None and now >= next_poll:
            payload = watch_probe()
            if payload is not None:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                raise Wake(payload)
            next_poll = time.monotonic() + interval
        time.sleep(min(0.2, max(0.0, deadline - now)))
    return child.returncode


def check(directory, run_id, terminal, args, timeout, watch=None):
    """Run one ``orchestration check`` and return its parsed envelope.

    The complete snapshot is published only after the child exits; a partial read is
    therefore impossible. ``watch`` is passed through to ``supervise`` and turns the call
    into a supervised block.
    """
    cmd = [CLI, "orchestration", "check", "--run", run_id,
           "--terminal", terminal, *args, "--json"]
    with cli_streams(directory, cmd, "") as (child, output, error):
        write_state(directory, {
            "run": run_id, "terminal": terminal, "loopPid": os.getpid(),
            "childPid": child.pid, "phase": "waiting" if "--wait" in args else "checking",
            "updatedAt": time.time()})
        supervise(child, timeout, watch)
        return parse_cli_result(child.returncode, output.read_text(), error.read_text())


def worker_signature(directory, run_id):
    """Reduce this Run's worker states to one comparable, read-only signature.

    The signature is the sorted set of ``dispatchId:workerState:dispatchStatus:stage``
    lines from ``worker-list --run``. Comparing whole lines rather than a count is the
    point: a count cannot see one worker replace another, and a snapshot taken after an
    event is exactly how a real wake-up was missed once already. A failure here is
    reported like any other CLI failure — never as "nothing changed".
    """
    code, raw, problem = cli_run(
        directory,
        [CLI, "orchestration", "worker-list", "--run", run_id, "--json"],
        "worker-list",
    )
    data = parse_cli_result(code, raw, problem)
    workers = data["result"].get("workers")
    if not isinstance(workers, list):
        raise RuntimeError("worker-list returned no workers array")
    lines = []
    for worker in workers:
        if not isinstance(worker, dict):
            continue
        projection = worker.get("projection")
        stage = projection.get("stage") if isinstance(projection, dict) else None
        detail = stage.get("detail") if isinstance(stage, dict) else None
        lines.append(":".join(str(part) for part in (
            worker.get("dispatchId"), worker.get("workerState"),
            worker.get("dispatchStatus"), detail)))
    return "\n".join(sorted(lines))


def worker_changes(before, after):
    """Describe every worker whose signature line changed, appeared or disappeared.

    The dispatchId is the key, so a worker that moved from ``ready`` to ``working`` reads
    as one before→after line rather than a removal plus an unrelated addition.
    """
    def parse(signature):
        mapping = {}
        for line in (signature or "").splitlines():
            dispatch_id, _, rest = line.partition(":")
            mapping[dispatch_id] = rest
        return mapping

    old, new = parse(before), parse(after)
    changes = []
    for dispatch_id in sorted(set(old) | set(new)):
        if old.get(dispatch_id) != new.get(dispatch_id):
            changes.append(
                f"{dispatch_id} {old.get(dispatch_id) or 'absent'} -> {new.get(dispatch_id) or 'absent'}"
            )
    return changes


def wake_event(reason, run_id, terminal, detail, delivery_id=None, types=(), saved=None):
    """Build the one-line wake payload.

    The shape is a published contract: ``event`` is always ``wake``; ``reason`` is one of
    ``delivery``, ``worker_state`` or ``error``; ``types`` is the whole FIFO batch's message
    types in order and ``messageCount`` its length (empty/0 when the wake was not a batch);
    ``deliveryId`` and ``saved`` name the batch and the file it was persisted to, and are
    null for the other two legs. ``detail`` is one line, because the payload is one line:
    payloads and error texts are flattened and truncated.
    """
    return {
        "event": "wake",
        "reason": reason,
        "run": run_id,
        "terminal": terminal,
        "deliveryId": delivery_id,
        "types": list(types),
        "messageCount": len(types),
        "saved": saved,
        "detail": " ".join(str(detail).split())[:500],
    }


def emit(payload):
    """Print the wake line. stdout carries this line and nothing else."""
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def fatal(directory, binding, run_id, terminal, error):
    """Report a terminal failure on disk, as a wake line, and non-zero.

    Retrying forever or borrowing another identity is forbidden: a permission, binding,
    waiter or CLI failure stops this process explicitly, and that stop is itself the third
    wake leg — including the ones that happen before the first wait is installed.
    """
    log(directory, f"ERROR {error}")
    write_state(directory, {**binding, "phase": "error", "error": str(error)})
    emit(wake_event("error", run_id, terminal, error))
    return 1


def batch_messages(data):
    """Return a batch's message array, or raise when the envelope is unusable."""
    messages = data["result"].get("messages")
    if not isinstance(messages, list):
        raise RuntimeError("missing messages array")
    return messages


def delivery_path(directory, delivery_id):
    """The durable file this consumer saves for a delivery id."""
    return directory / "deliveries" / (hashlib.sha256(delivery_id.encode()).hexdigest() + ".json")


def ack_main(directory, delivery_id):
    """Acknowledge one saved delivery for the coordinator, recording the attempt.

    The receipt is appended as ``pending`` before Orca is asked and as ``success`` or
    ``failed`` after it answers, so the local record can never claim an ack Orca refused.
    A refused ack keeps the delivery unacked and the delivery file untouched; a retry uses
    the same delivery id and therefore the same local request id. The entry never calls
    Orca for an id this consumer never saved: an id read from a read-only ``check --all``
    names a message, not an ackable delivery.
    """
    if not isinstance(delivery_id, str) or not delivery_id:
        print("--ack needs a delivery id", file=sys.stderr)
        return 2
    if not directory.is_dir():
        print("state directory missing: " + str(directory), file=sys.stderr)
        return 2
    binding = read_json(directory / "binding.json")
    run_id, terminal = binding.get("run"), binding.get("terminal")
    if not isinstance(run_id, str) or not run_id \
            or not isinstance(terminal, str) or not terminal:
        print("state directory has no Run/terminal binding", file=sys.stderr)
        return 2
    if not delivery_path(directory, delivery_id).exists():
        print(f"refusing to ack {delivery_id}: no saved delivery in this state directory",
              file=sys.stderr)
        log(directory, f"ACK-REFUSED {delivery_id} no saved delivery")
        return 1
    request_id = ack_request_id(delivery_id)
    append_receipt(directory, delivery_id, "pending")
    try:
        code, raw, problem = cli_run(
            directory,
            [CLI, "orchestration", "check", "--run", run_id, "--terminal", terminal,
             "--ack", delivery_id, "--json"],
            "check --ack",
        )
        parse_cli_result(code, raw, problem)
    except (OSError, ValueError, RuntimeError) as error:
        append_receipt(directory, delivery_id, "failed")
        log(directory, f"ACK-FAILED {delivery_id} request={request_id}: {error}")
        print(f"ack failed for {delivery_id}: {error}", file=sys.stderr)
        return 1
    append_receipt(directory, delivery_id, "success")
    log(directory, f"ACK {delivery_id} request={request_id}")
    return 0


def persist_batch(directory, run_id, terminal, data):
    """Persist one batch and report what it means for the coordinator.

    Returns ``(wake, acknowledged)``. ``wake`` is the payload for a batch that was not on
    disk before and carries anything other than heartbeats — the "new message" leg, raised
    after the file is durable. ``acknowledged`` says the batch was heartbeat-only and has
    been acknowledged. An empty batch is ``(None, False)``, and so is a batch already on
    disk: a FIFO replay of a batch the coordinator already saw is its outstanding duty, not
    news, so it must neither wake again nor be overwritten.
    """
    messages = batch_messages(data)
    if not messages:
        return None, False
    did = data["result"].get("deliveryId")
    if not isinstance(did, str) or not did:
        raise RuntimeError("nonempty batch without deliveryId")
    file = delivery_path(directory, did)
    # Existence, not a count, is the "is this news?" test: a count taken before an event
    # can miss a delivery that landed before the snapshot, while a saved file is never
    # overwritten, so its presence is the durable fingerprint of having seen the batch.
    saved = not file.exists()
    if saved:
        # A replay never overwrites evidence, a restart never reuses an index, and an
        # unhandled file is never rotated away.
        atomic(file, json.dumps(data, ensure_ascii=False, indent=2))
    if all(isinstance(m, dict) and m.get("type") == "heartbeat" for m in messages):
        # A heartbeat batch carries no decision, so it is acknowledged automatically — but
        # an Orca refusal is still a CLI failure with evidence: the receipt says failed, the
        # delivery stays unacked, and the raised error ends the loop in one error wake. It
        # is never retried here, and it is never read as "nothing to do".
        append_receipt(directory, did, "pending")
        try:
            check(directory, run_id, terminal, ["--ack", did], CLI_TIMEOUT_SECONDS)
        except (OSError, ValueError, RuntimeError) as error:
            append_receipt(directory, did, "failed")
            log(directory, f"ACK-FAILED heartbeat-only {did}: {error}")
            raise
        append_receipt(directory, did, "success")
        log(directory, f"AUTO-ACK heartbeat-only {did}")
        return None, True
    if not saved:
        return None, False
    atomic(directory / "NEW-MESSAGE", f"{did}\nfile={file}\n")
    log(directory, f"PENDING {did} file={file}")
    # Record the announcement before the wake line is printed: the durable marker is what
    # lets a later install tell "already news" from "saved, then the process died before
    # saying so". The window before this line is what reconciliation covers.
    mark_woken(directory, did)
    types = [(m.get("type") if isinstance(m, dict) else None) for m in messages]
    return wake_event("delivery", run_id, terminal,
                      f"new batch {did} saved: {len(messages)} message(s) {types}",
                      delivery_id=did, types=types, saved=str(file)), False


def idle(seconds, poll, watch_probe):
    """Sleep in bounded slices, polling the worker signature.

    Returns a wake payload when the poll reports one, else ``None``. Without a probe this
    is a plain sleep. Polling during the pause matters: a pause is exactly when the loop is
    not blocked on the CLI, and a Run whose workers change state without sending anything
    would otherwise be invisible.
    """
    if watch_probe is None:
        time.sleep(seconds)
        return None
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(poll, remaining))
        payload = watch_probe()
        if payload is not None:
            return payload


def locked(directory):
    """Take the exclusive lock for this state directory, or return ``None``.

    The lock is advisory and per-directory: it keeps a second instance from consuming
    the same Run's queue. Orca's own waiter slots remain the cross-directory limit.
    """
    handle = (directory / "inbox.lock").open("a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def stop(signum, frame):
    """Reap every CLI child this process owns before exiting so none keeps a waiter slot.

    A signal usually arrives while a nested ``worker-list`` holds ``CHILD``; walking the whole
    list is what also ends the long ``check --wait`` around it.
    """
    global STOPPING
    STOPPING = True
    reap_all()
    raise SystemExit(0)


def status(path):
    """Print this directory's lock and state without consuming the queue."""
    directory = Path(path).resolve()
    if not directory.is_dir():
        print("loop: DEAD (state directory missing)")
        return 1
    handle = locked(directory)
    alive = handle is None
    if handle is not None:
        handle.close()
    print("loop:", "ALIVE" if alive else "DEAD")
    print("state:", json.dumps(read_json(directory / "state.json"), ensure_ascii=False))
    saved = sorted((directory / "deliveries").glob("*.json"))
    print("saved batches:", len(saved), "(durable records; not necessarily unhandled)")
    marker = directory / "NEW-MESSAGE"
    if marker.exists():
        print(marker.read_text())
    print("Run an explicit check --peek to inspect the queue; status never consumes messages.")
    return 0 if alive else 1


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--status":
        return status(sys.argv[2])
    if len(sys.argv) >= 2 and sys.argv[1] == "--ack":
        if len(sys.argv) != 4:
            print("usage: inbox-loop.sh --ack DELIVERY_ID STATE_DIR", file=sys.stderr)
            return 2
        return ack_main(Path(sys.argv[3]).resolve(), sys.argv[2])
    if len(sys.argv) != 4:
        print("usage: inbox-loop.sh RUN STATE_DIR COORDINATOR_HANDLE"
              " | --status STATE_DIR | --ack DELIVERY_ID STATE_DIR", file=sys.stderr)
        return 2
    run_id, path, terminal = sys.argv[1:]
    directory = Path(path).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    handle = locked(directory)
    if handle is None:
        print("already running for this state directory", file=sys.stderr)
        log(directory, f"ERROR already running; refused to take over run={run_id} "
                       f"terminal={terminal}")
        emit(wake_event("error", run_id, terminal, "another loop holds this state directory"))
        # 2, not 1: this is a refusal before the loop ever started, the same class as a
        # refused rebinding or a usage error — not a failure of a running loop.
        return 2
    binding = {"run": run_id, "terminal": terminal, "cli": CLI}
    previous = read_json(directory / "binding.json")
    if previous and previous != binding:
        print("state directory belongs to another Run/terminal/CLI", file=sys.stderr)
        # The state file belongs to the Run that already owns this directory, so this
        # refusal is recorded in the shared log instead of overwriting that record: the
        # failure still lands on disk, and it never destroys the other Run's evidence.
        log(directory, f"ERROR refused rebinding to run={run_id} terminal={terminal}; "
                       f"the directory is bound to {previous}")
        emit(wake_event("error", run_id, terminal,
                        "state directory belongs to another Run/terminal/CLI"))
        handle.close()
        return 2
    atomic(directory / "binding.json", json.dumps(binding))
    atomic(directory / "run-id", run_id + "\n")
    atomic(directory / "inbox-loop.pid", str(os.getpid()) + "\n")
    (directory / "deliveries").mkdir(exist_ok=True)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    wait_ms = env_number("WAIT_TIMEOUT_MS", 30000, int)
    backoff = env_number("BACKOFF_SECONDS", 5.0, float)
    pending_backoff = env_number("PENDING_BACKOFF_SECONDS", 30.0, float)
    # A zero poll interval would probe in a tight loop, so the floor is part of the switch.
    worker_poll = max(env_number("WORKER_POLL_SECONDS", 15.0, float), 0.05)
    watch_workers = env_flag("WORKER_WATCH", True)
    try:
        # Reading the baseline is a CLI call like any other: a failure here is reported
        # as an error, never swallowed into "nothing changed".
        try:
            # One read of the worker list at install time, before any queue call. Its
            # signature becomes the baseline this install compares against, and it is
            # recorded in the local reconciliation observation too.
            baseline = worker_signature(directory, run_id) if watch_workers else None
            reconciled = reconcile_reinstall(directory, run_id, terminal, baseline)
        except (OSError, ValueError, RuntimeError) as error:
            return fatal(directory, binding, run_id, terminal, error)
        if reconciled is not None:
            # The observation and reconciled id are already durable; the regular wake path
            # below adds phase=wake and prints the one line the harness surfaces.
            raise Wake(reconciled)

        def watch_probe():
            """Wake when this Run's worker signature no longer matches the baseline."""
            changes = worker_changes(baseline, worker_signature(directory, run_id))
            if not changes:
                return None
            return wake_event("worker_state", run_id, terminal,
                              "worker state changed: " + "; ".join(changes))

        watch = (worker_poll, watch_probe) if watch_workers else None
        while True:
            try:
                # Unfiltered first, every cycle, so heartbeats and ordinary messages
                # cannot sit at the head of the queue behind a type filter.
                data = check(directory, run_id, terminal, [], CLI_TIMEOUT_SECONDS)
                messages = batch_messages(data)
                woken, acknowledged = persist_batch(directory, run_id, terminal, data)
                if woken is None and watch is not None:
                    woken = watch_probe()
                if messages:
                    # A batch already on disk is the coordinator's outstanding duty: it
                    # must not wake again, and it must not busy-loop either, so pause and
                    # re-check rather than block behind it. A batch we just acknowledged
                    # was real work, but the cycle still ends with the configured pause so
                    # a stream of heartbeats cannot hammer the CLI.
                    pause = backoff if acknowledged else pending_backoff
                else:
                    data = check(directory, run_id, terminal,
                                 ["--wait", "--types", WAKE_TYPES, "--timeout-ms", str(wait_ms)],
                                 wait_ms / 1000 + WAIT_GRACE_SECONDS, watch=watch)
                    messages = batch_messages(data)
                    woken, _ = persist_batch(directory, run_id, terminal, data)
                    if woken is None and watch is not None:
                        woken = watch_probe()
                    # An empty batch means the CLI timed out: back off and install the next
                    # wait, which is where the reinstall/backoff semantics live.
                    pause = backoff
                if woken is not None:
                    raise Wake(woken)
                if pause > 0:
                    woken = idle(pause, worker_poll if watch else None,
                                 watch_probe if watch else None)
                    if woken is not None:
                        raise Wake(woken)
                # The marker is not cleared: an empty queue does not prove that the
                # batches already on disk have been handled.
                write_state(directory, {**binding, "loopPid": os.getpid(),
                                        "phase": "backoff", "updatedAt": time.time()})
            except (OSError, ValueError, RuntimeError) as error:
                # Permission, binding and waiter errors stop explicitly. Retrying
                # forever or borrowing another identity is forbidden.
                return fatal(directory, binding, run_id, terminal, error)
    except Wake as wake:
        payload = wake.payload
        write_state(directory, {**binding, "loopPid": os.getpid(),
                                "phase": "wake", "reason": payload["reason"],
                                "detail": payload["detail"],
                                "deliveryId": payload["deliveryId"],
                                "updatedAt": time.time()})
        log(directory, "WAKE " + json.dumps(payload, ensure_ascii=False))
        emit(payload)
        return 0
    finally:
        (directory / "inbox-loop.pid").unlink(missing_ok=True)
        handle.close()


if __name__ == "__main__":
    sys.exit(main())
