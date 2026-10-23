#!/usr/bin/env python3
"""Orca inbox loop: persist every delivery batch, and end the process on a wake event.

Four safety guarantees must survive every change to this file:

* every batch the CLI returns is written to disk atomically, keyed by a hash of its
  ``deliveryId``, before anything else happens, so a crash can never lose a message;
* an already saved delivery is never overwritten, so a FIFO replay cannot erase the
  evidence of the first read;
* a batch whose every message is a heartbeat is acknowledged automatically, because it
  carries no decision; any other batch is left for the coordinator to handle and
  acknowledge itself;
* a CLI failure, a JSON error or a lost binding is reported as an error and exits
  non-zero. It is never reinterpreted as "the queue was empty";
* a CLI child never outlives the call that owns it. An exception from the worker-state probe,
  a wake, or a signal reaps every process this loop started, because a survivor keeps its
  waiter slot: the next install then fails while the Run still looks watched.

On top of them this process owns the wake contract, because a file on disk cannot start a
coordinator turn by itself — only the end of a command the harness surfaces can:

* a **newly persisted** batch that carries anything other than heartbeats ends the process
  and prints one machine-readable line on stdout (``event``, ``reason``, ``deliveryId``);
  the end of the process, not the file, is the wake;
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


def atomic(path, value):
    """Write ``value`` to ``path`` atomically.

    The temporary file is written and fsynced in the same directory as the target, so
    ``os.replace`` is a same-filesystem rename: a reader either sees the previous
    content or the complete new content, never a half-written file.
    """
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("w") as out:
            out.write(value)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
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
        atomic(directory / "state.json", json.dumps({
            "run": run_id, "terminal": terminal, "loopPid": os.getpid(),
            "childPid": child.pid, "phase": "waiting" if "--wait" in args else "checking",
            "updatedAt": time.time()}))
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
    atomic(directory / "state.json", json.dumps({**binding,
        "phase": "error", "error": str(error)}))
    emit(wake_event("error", run_id, terminal, error))
    return 1


def batch_messages(data):
    """Return a batch's message array, or raise when the envelope is unusable."""
    messages = data["result"].get("messages")
    if not isinstance(messages, list):
        raise RuntimeError("missing messages array")
    return messages


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
    file = directory / "deliveries" / (hashlib.sha256(did.encode()).hexdigest() + ".json")
    # Existence, not a count, is the "is this news?" test: a count taken before an event
    # can miss a delivery that landed before the snapshot, while a saved file is never
    # overwritten, so its presence is the durable fingerprint of having seen the batch.
    saved = not file.exists()
    if saved:
        # A replay never overwrites evidence, a restart never reuses an index, and an
        # unhandled file is never rotated away.
        atomic(file, json.dumps(data, ensure_ascii=False, indent=2))
    if all(isinstance(m, dict) and m.get("type") == "heartbeat" for m in messages):
        check(directory, run_id, terminal, ["--ack", did], CLI_TIMEOUT_SECONDS)
        log(directory, f"AUTO-ACK heartbeat-only {did}")
        return None, True
    if not saved:
        return None, False
    atomic(directory / "NEW-MESSAGE", f"{did}\nfile={file}\n")
    log(directory, f"PENDING {did} file={file}")
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
    if len(sys.argv) != 4:
        print("usage: inbox-loop.sh RUN STATE_DIR COORDINATOR_HANDLE | --status STATE_DIR",
              file=sys.stderr)
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
            baseline = worker_signature(directory, run_id) if watch_workers else None
        except (OSError, ValueError, RuntimeError) as error:
            return fatal(directory, binding, run_id, terminal, error)

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
                atomic(directory / "state.json", json.dumps({**binding, "loopPid": os.getpid(),
                    "phase": "backoff", "updatedAt": time.time()}))
            except (OSError, ValueError, RuntimeError) as error:
                # Permission, binding and waiter errors stop explicitly. Retrying
                # forever or borrowing another identity is forbidden.
                return fatal(directory, binding, run_id, terminal, error)
    except Wake as wake:
        payload = wake.payload
        atomic(directory / "state.json", json.dumps({**binding, "loopPid": os.getpid(),
            "phase": "wake", "reason": payload["reason"], "detail": payload["detail"],
            "deliveryId": payload["deliveryId"], "updatedAt": time.time()}))
        log(directory, "WAKE " + json.dumps(payload, ensure_ascii=False))
        emit(payload)
        return 0
    finally:
        (directory / "inbox-loop.pid").unlink(missing_ok=True)
        handle.close()


if __name__ == "__main__":
    sys.exit(main())
