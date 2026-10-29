#!/usr/bin/env python3
"""Durable coordination ledger: append-only JSONL outside every repository.

The ledger is an index, never an authority. It records intent, receipts, observations,
decisions, pending items, gaps and closeouts so a coordinator can reconcile a crash
window; Orca, git and the tracker remain the only authorities.

Storage contract (schema v1, documented in ``references/ledger.md``):

* one file per Run at ``$ORCA_LEDGER_HOME/runs/<run_id>/ledger.jsonl``;
* an exclusive per-Run ``flock`` serialises appends, so a long line cannot interleave;
* the file is fsynced after every record and the parent directory when it is first created;
* readers discard a trailing partial line and report every corrupt complete line instead of
  dropping it silently;
* ``record_id`` is idempotent: an identical normalized re-append is a no-op and a different
  payload under the same id is a consistency error that writes nothing;
* records older than the retention window, or beyond the per-Run byte budget, move to
  ``archive/``; no unarchived record is ever deleted.

Only the Python standard library is used. The module is importable for pure validation and
is also the CLI (``append``, ``show``, ``summary``).
"""

import argparse
import copy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

SCHEMA_VERSION = 1

KINDS = (
    "intent", "result", "observation", "decision", "pending", "gap",
    "health", "preflight", "acceptance", "closeout",
)
ACTIONS = (
    "worker-start", "send", "reply", "ack", "stop", "abandon", "retain", "release",
    "merge", "pr-create", "tracker-close", "progress", "health", "preflight",
    "acceptance", "closeout",
)
IDEMPOTENCIES = ("joinable", "replay_safe", "readback_only", "lookup_then_create", "none")
STATES = (
    "intent", "completed", "pending", "absent", "failed", "refused",
    "unknown", "observed", "divergent",
)
ACTORS = ("coordinator", "worker", "human", "rule")

#: Idempotency classes that promise a stable key before the action runs.
KEYED_IDEMPOTENCIES = ("joinable", "replay_safe", "lookup_then_create")

#: The closed subject object of schema v1.
SUBJECT_KEYS = ("task_id", "dispatch_id", "attempt", "pr", "ticket")

FIELDS = (
    "schema_version", "record_id", "kind", "run_id", "subject", "action",
    "request_id", "idempotency", "state", "evidence_ref", "observed_at",
    "actor", "detail",
)

MAX_RECORD_BYTES = 64 * 1024
MAX_REFERENCE_CHARS = 2048
MAX_SUBJECT_CHARS = 512

PORTABLE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
RFC3339_UTC = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(?:Z|\+00:00)\Z"
)

#: Location contract: the environment variable wins, ``~/.orca/ledger`` is the default.
HOME_ENV = "ORCA_LEDGER_HOME"
DEFAULT_HOME = "~/.orca/ledger"

#: Orca CLI contract shared with the inbox loop: the variable names the executable.
CLI_ENV = "ORCA_CLI_COMMAND"
DEFAULT_CLI = "orca"
CLI_TIMEOUT_SECONDS = 30
PAGE_LIMIT = 100
MAX_PAGES = 1000

#: Retention defaults, configurable through the environment (documented as defaults).
RETENTION_DAYS = 30
RETENTION_BYTES = 10 * 1024 * 1024
RETENTION_DAYS_ENV = "ORCA_LEDGER_RETENTION_DAYS"
RETENTION_BYTES_ENV = "ORCA_LEDGER_RETENTION_BYTES"

DIRECTORY_MODE = 0o700
FILE_MODE = 0o600
RUNS_DIR = "runs"
LEDGER_NAME = "ledger.jsonl"
LOCK_NAME = "ledger.lock"
ARCHIVE_DIR = "archive"
REPORTS_DIR = "reports"

#: The five outcomes of crash-window reconciliation.
CLASSIFICATIONS = ("matched", "orphan_intent", "orphan_effect", "divergent", "unknown")

#: The three windows from the action-discipline table.
WINDOWS = ("W1", "W2", "W3")

#: An authority source is either readable or unknown; an empty success is never assumed.
SOURCE_STATES = ("ok", "unknown")

#: The authority snapshot's top-level sources.
AUTHORITY_SOURCES = ("orca", "pr_host", "tracker", "git", "progress")

#: The receipt states ``request-show`` may report for one request id.
RECEIPT_STATES = ("completed", "pending", "absent", "unknown")

#: The repairs reconciliation may apply without a decision.
REPAIR_KINDS = (
    "late_result", "late_observation", "gap", "projection", "divergent_marker",
    "replay", "pending",
)

#: How each action is read back and whether a pending receipt may be replayed.
#:
#: ``probe`` decides which authority collection can prove an effect:
#: ``workers`` reads worker-list and task-list, ``messages`` needs a message
#: authority, ``pr`` the PR host, ``merge`` the PR host plus git ancestry,
#: ``tracker`` the tracker, and ``none`` means only request-show can judge.
#: ``replay`` is ``joinable`` (same request id), ``replay_safe`` (same record id),
#: ``settled`` (only after settlement), ``lookup`` (look up before creating), or
#: ``never`` (merge, tracker close, and the local gate records).
ACTION_POLICY = {
    "worker-start": {"probe": "workers", "replay": "joinable"},
    "send": {"probe": "messages", "replay": "joinable"},
    "reply": {"probe": "messages", "replay": "joinable"},
    "ack": {"probe": "messages", "replay": "replay_safe"},
    "stop": {"probe": "workers", "replay": "joinable"},
    "abandon": {"probe": "workers", "replay": "joinable"},
    "retain": {"probe": "workers", "replay": "settled"},
    "release": {"probe": "workers", "replay": "settled"},
    "merge": {"probe": "merge", "replay": "never"},
    "pr-create": {"probe": "pr", "replay": "lookup"},
    "tracker-close": {"probe": "tracker", "replay": "never"},
    "progress": {"probe": "none", "replay": "replay_safe"},
    "health": {"probe": "none", "replay": "replay_safe"},
    "preflight": {"probe": "none", "replay": "never"},
    "acceptance": {"probe": "none", "replay": "never"},
    "closeout": {"probe": "none", "replay": "never"},
}

#: Actions whose pending receipt must never be replayed; each yields a decision.
NEVER_REPLAY_ACTIONS = ("merge", "tracker-close")


def _invalid(message):
    raise ValueError(message)


def canonical_json(record):
    return json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _uuid_text(value, field):
    if not isinstance(value, str) or not value:
        _invalid(field + " must be a UUID")
    try:
        return str(uuid.UUID(value))
    except ValueError:
        _invalid(field + " must be a UUID")


def _portable_identifier(value, field):
    if not isinstance(value, str) or not PORTABLE_ID.fullmatch(value):
        _invalid(field + " must be a portable identifier")
    if value in (".", ".."):
        _invalid(field + " must not be a path segment")
    return value


def _utc_timestamp(value):
    if not isinstance(value, str):
        _invalid("observed_at must be an RFC3339 UTC string")
    match = RFC3339_UTC.fullmatch(value)
    if match is None:
        _invalid("observed_at must be an RFC3339 UTC string")
    year, month, day, hour, minute, second = (int(part) for part in match.groups()[:6])
    try:
        datetime(year, month, day, hour, minute, second)
    except ValueError:
        _invalid("observed_at is not a real UTC instant")
    return value


def _subject(value):
    if not isinstance(value, dict):
        _invalid("subject must be an object")
    unknown = set(value) - set(SUBJECT_KEYS)
    if unknown:
        _invalid("unknown subject fields: " + ", ".join(sorted(unknown)))
    for key, item in value.items():
        if key == "attempt":
            if type(item) is not int or item < 1:
                _invalid("subject.attempt must be a positive integer")
        elif key == "pr":
            if type(item) is int:
                if item < 0:
                    _invalid("subject.pr must not be negative")
            elif not isinstance(item, str) or len(item) > MAX_SUBJECT_CHARS:
                _invalid("subject.pr must be an integer or a short string")
        elif not isinstance(item, str) or len(item) > MAX_SUBJECT_CHARS:
            _invalid("subject." + key + " must be a short string")
    return value


def _enum(value, allowed, field):
    if not isinstance(value, str) or value not in allowed:
        _invalid("unknown " + field + ": " + repr(value))
    return value


def validated(record):
    """Return the normalized schema-v1 record, raising ``ValueError`` when invalid.

    Normalisation lower-cases ``record_id`` and a non-empty ``request_id`` to their
    canonical UUID form; every other value is preserved exactly, so ``append`` followed
    by ``show`` round-trips the record.
    """
    if not isinstance(record, dict):
        _invalid("record must be a JSON object")
    missing = [field for field in FIELDS if field not in record]
    if missing:
        _invalid("missing fields: " + ", ".join(missing))
    unknown = sorted(set(record) - set(FIELDS))
    if unknown:
        _invalid("unknown fields: " + ", ".join(unknown))

    version = record["schema_version"]
    if type(version) is not int or version != SCHEMA_VERSION:
        _invalid("schema_version must be 1")

    normalized = dict(record)
    normalized["record_id"] = _uuid_text(record["record_id"], "record_id")
    normalized["run_id"] = _portable_identifier(record["run_id"], "run_id")
    normalized["kind"] = _enum(record["kind"], KINDS, "kind")
    normalized["action"] = _enum(record["action"], ACTIONS, "action")
    normalized["idempotency"] = _enum(record["idempotency"], IDEMPOTENCIES, "idempotency")
    normalized["state"] = _enum(record["state"], STATES, "state")
    normalized["actor"] = _enum(record["actor"], ACTORS, "actor")
    normalized["subject"] = _subject(record["subject"])
    normalized["observed_at"] = _utc_timestamp(record["observed_at"])

    request_id = record["request_id"]
    if request_id == "":
        if record["idempotency"] in KEYED_IDEMPOTENCIES:
            _invalid("request_id is required for a keyed idempotency class")
        normalized["request_id"] = ""
    else:
        normalized["request_id"] = _uuid_text(request_id, "request_id")

    evidence = record["evidence_ref"]
    if not isinstance(evidence, str) or len(evidence) > MAX_REFERENCE_CHARS:
        _invalid("evidence_ref must be a string of at most 2048 characters")
    if not isinstance(record["detail"], dict):
        _invalid("detail must be an object")
    if len(canonical_json(normalized)) > MAX_RECORD_BYTES:
        _invalid("record exceeds 64 KiB")
    return normalized


def validate_record(record):
    """Validate a schema-v1 record; raise ``ValueError`` when it is invalid."""
    validated(record)


def normalize(record):
    """Return the canonical JSON text used to compare re-appended records."""
    return canonical_json(validated(record))


def _repository_root(path):
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _inferred_target_repository():
    """The repository containing the invoking shell, if any: the CLI's target repo."""
    try:
        return _repository_root(Path.cwd().resolve())
    except OSError:
        return None


def home_path(raw=None, target_repo=None):
    """Resolve and guard the ledger home; refuse symlinks, repositories and ancestors.

    ``target_repo`` is explicit for the module seam and inferred from the working
    directory by the CLI. With it, the home may be neither inside the target repository
    nor an ancestor of it; without it, no part of the path may sit inside a repository.
    """
    raw = os.environ.get(HOME_ENV, DEFAULT_HOME) if raw is None else raw
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if any(part.is_symlink() for part in (path, *path.parents)):
        _invalid("ledger home must not traverse a symlink")
    path = path.resolve()
    if _repository_root(path) is not None:
        _invalid("the ledger must live outside every repository")
    if target_repo is not None:
        repo = Path(target_repo).expanduser().resolve()
        if path == repo or path in repo.parents or repo in path.parents:
            _invalid("the ledger must be outside and not an ancestor of the target repository")
    return path


def _ensure_directory(path):
    if path.is_symlink():
        _invalid("ledger directory must not be a symlink")
    path.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
    if path.is_symlink():
        _invalid("ledger directory must not be a symlink")
    os.chmod(path, DIRECTORY_MODE)


def _run_directory(home, run_id):
    _portable_identifier(run_id, "run_id")
    _ensure_directory(home)
    runs = home / RUNS_DIR
    _ensure_directory(runs)
    directory = runs / run_id
    _ensure_directory(directory)
    return directory


@contextmanager
def _locked(run_dir):
    """Hold the per-Run exclusive lock for the whole read-modify-write of an append."""
    fd = os.open(
        run_dir / LOCK_NAME,
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
        FILE_MODE,
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _scan(path):
    """Return ``(records, corrupt, partial)`` for one JSONL file.

    A trailing fragment without a newline is an interrupted append: it is returned as
    ``partial`` and never parsed. A complete line that does not hold a valid schema-v1
    record is returned in ``corrupt`` with its raw bytes, so a caller can report it and a
    retention rewrite can preserve it instead of dropping it.
    """
    records, corrupt, seen = [], [], set()
    if not path.exists():
        return records, corrupt, b""
    if path.is_symlink():
        _invalid("ledger file must not be a symlink")
    data = path.read_bytes()
    if data.endswith(b"\n"):
        chunks, partial = data.split(b"\n")[:-1], b""
    else:
        head, _, partial = data.rpartition(b"\n")
        chunks = head.split(b"\n") if head else []
    for number, chunk in enumerate(chunks, start=1):
        try:
            record = validated(json.loads(chunk))
        except (TypeError, ValueError) as error:
            corrupt.append({"line": number, "raw": chunk, "error": str(error)})
            continue
        if record["record_id"] in seen:
            continue
        seen.add(record["record_id"])
        records.append(record)
    return records, corrupt, partial


def _archived_records(run_dir):
    """Complete records already moved to ``archive/``; used only for idempotency."""
    directory = run_dir / ARCHIVE_DIR
    if not directory.is_dir() or directory.is_symlink():
        return []
    records = []
    for path in sorted(directory.glob("*.jsonl")):
        try:
            found, _corrupt, _partial = _scan(path)
        except (OSError, ValueError):
            continue
        records.extend(found)
    return records


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write to the ledger")
        view = view[written:]


def _fsync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _env_int(name, default):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        _invalid(name + " must be a non-negative integer")
    if value < 0:
        _invalid(name + " must be a non-negative integer")
    return value


def _observed_at(record):
    return datetime.fromisoformat(record["observed_at"].replace("Z", "+00:00"))


def _write_archive(run_dir, stem, data):
    """Create an archive file, fsync it and the archive directory, return its path."""
    directory = run_dir / ARCHIVE_DIR
    _ensure_directory(directory)
    stamp = _now_instant().strftime("%Y%m%dT%H%M%S%fZ")
    for index in range(1000):
        path = directory / f"{stem}-{stamp}-{index}.jsonl"
        try:
            fd = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, FILE_MODE
            )
        except FileExistsError:
            continue
        try:
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        _fsync_directory(directory)
        return path
    raise OSError("cannot allocate an archive file name")


def _replace_active(active, data):
    """Rewrite the active ledger atomically after its records were archived durably."""
    tmp = active.with_name(active.name + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, FILE_MODE)
    try:
        _write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(tmp, active)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_directory(active.parent)


def _retain(run_dir, now=None):
    """Move old or over-budget complete records to ``archive/``; never delete them.

    A rewrite also carries corrupt lines and a trailing partial fragment into a corrupt
    archive, because the rewrite is the only operation that could otherwise erase them.
    """
    active = run_dir / LEDGER_NAME
    if not active.exists():
        return
    records, corrupt, partial = _scan(active)
    days = _env_int(RETENTION_DAYS_ENV, RETENTION_DAYS)
    limit = _env_int(RETENTION_BYTES_ENV, RETENTION_BYTES)
    cutoff = (now or _now_instant()) - timedelta(days=days)

    archived = {record["record_id"] for record in records if _observed_at(record) < cutoff}
    sizes = {record["record_id"]: len(canonical_json(record).encode("utf-8")) + 1
             for record in records}
    kept_bytes = sum(size for key, size in sizes.items() if key not in archived)
    for record in records:  # oldest first, matching file order
        if kept_bytes <= limit:
            break
        if record["record_id"] in archived:
            continue
        archived.add(record["record_id"])
        kept_bytes -= sizes[record["record_id"]]
    if not archived:
        return

    moved = [record for record in records if record["record_id"] in archived]
    kept = [record for record in records if record["record_id"] not in archived]
    _write_archive(
        run_dir, "ledger",
        b"".join((canonical_json(record) + "\n").encode("utf-8") for record in moved),
    )
    if corrupt or partial:
        _write_archive(
            run_dir, "corrupt",
            b"".join(item["raw"] + b"\n" for item in corrupt) + partial,
        )
    _replace_active(active, b"".join((canonical_json(record) + "\n").encode("utf-8") for record in kept))


def _append(record, home=None, target_repo=None, now=None):
    """Validate, deduplicate, fsync and retain; return ``(record_id, appended)``."""
    normalized = validated(record)
    root = home_path(home, target_repo)
    run_dir = _run_directory(root, normalized["run_id"])
    with _locked(run_dir):
        active = run_dir / LEDGER_NAME
        first_create = not active.exists()
        records, _corrupt, partial = _scan(active)
        existing = {item["record_id"]: item for item in records}
        candidate = existing.get(normalized["record_id"])
        if candidate is None:
            for archived_record in _archived_records(run_dir):
                if archived_record["record_id"] == normalized["record_id"]:
                    candidate = archived_record
                    break
        if candidate is not None:
            if canonical_json(candidate) == canonical_json(normalized):
                return normalized["record_id"], False
            _invalid("record_id already recorded with a different payload")

        line = (canonical_json(normalized) + "\n").encode("utf-8")
        fd = os.open(
            active, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, FILE_MODE
        )
        try:
            if partial:
                # An interrupted append left an unterminated fragment. Terminate it so the
                # new record is not glued onto it; the fragment then reads as one corrupt
                # complete line (reported, preserved), never as part of another record.
                _write_all(fd, b"\n")
            _write_all(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)
        if first_create:
            _fsync_directory(run_dir)
        _retain(run_dir, now=now)
        return normalized["record_id"], True


def append(record, home=None, target_repo=None):
    """Append one validated record and return its ``record_id``; idempotent by design."""
    return _append(record, home=home, target_repo=target_repo)[0]


def _matches(record, filters):
    for field in ("kind", "state", "action"):
        expected = filters.get(field)
        if expected is not None and record[field] != expected:
            return False
    for key, value in (filters.get("subject") or {}).items():
        if key not in SUBJECT_KEYS:
            _invalid("unknown subject filter: " + key)
        if str(record["subject"].get(key)) != value:
            return False
    return True


def read_report(run_id, filters=None, home=None):
    """Return ``(records, corrupt)``; records are complete, deduplicated, newest last.

    The trailing partial line of an interrupted append is discarded. ``corrupt`` carries
    every complete line that did not validate, so the CLI can report it.
    """
    _portable_identifier(run_id, "run_id")
    root = home_path(home)
    run_dir = root / RUNS_DIR / run_id
    if run_dir.is_symlink():
        _invalid("run directory must not be a symlink")
    records, corrupt, _partial = _scan(run_dir / LEDGER_NAME)
    if filters:
        records = [record for record in records if _matches(record, filters)]
    return records, corrupt


def read(run_id, filters=None, home=None):
    """Read complete records, newest last, with optional kind/state/action/subject filters."""
    return read_report(run_id, filters=filters, home=home)[0]


def _summarize(run_id, records, corrupt):
    kinds = {kind: 0 for kind in KINDS}
    states = {state: 0 for state in STATES}
    for record in records:
        kinds[record["kind"]] += 1
        states[record["state"]] += 1
    results = {
        record["request_id"] for record in records
        if record["kind"] == "result" and record["request_id"]
    }
    open_intents = [
        record["record_id"] for record in records
        if record["kind"] == "intent"
        and (not record["request_id"] or record["request_id"] not in results)
    ]
    pending, pending_seen = [], set()
    for record in records:
        if (record["kind"] == "pending" or record["state"] == "pending") \
                and record["record_id"] not in pending_seen:
            pending_seen.add(record["record_id"])
            pending.append(record["record_id"])
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "records": len(records),
        "kinds": kinds,
        "states": states,
        "open_intents": open_intents,
        "pending_items": pending,
        "corrupt_lines": len(corrupt),
    }


def summary(run_id, home=None):
    """Count records per kind/state and name open intents and pending items."""
    records, corrupt = read_report(run_id, home=home)
    return _summarize(run_id, records, corrupt)


# ---------------------------------------------------------------------------
# Authority snapshot and crash-window classification
# ---------------------------------------------------------------------------
#
# The snapshot is a local contract defined in ``references/ledger.md``. It is
# never a claim about a live Orca receipt shape: rows preserve only the fields
# the reconciliation rules read, and a collection that could not be read is
# ``unknown`` rather than an empty list. The classifier is pure: it does no I/O,
# reads no clock, and mutates neither the records nor the snapshot.


def _source(state="ok", reason="", **payload):
    section = {"state": state, "reason": reason}
    section.update(payload)
    return section


def _unknown_source(reason):
    return _source("unknown", reason)


def _source_state(value, field):
    if not isinstance(value, dict):
        _invalid(field + " must be an object")
    state = value.get("state")
    if state not in SOURCE_STATES:
        _invalid(field + ".state must be 'ok' or 'unknown'")
    reason = value.get("reason", "")
    if not isinstance(reason, str):
        _invalid(field + ".reason must be a string")
    return state, reason


def _rows(value, field, key):
    state, reason = _source_state(value, field)
    rows = value.get(key, [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        _invalid(field + "." + key + " must be a list of objects")
    return state, reason, rows


def _optional_text(row, key, field):
    value = row.get(key)
    if value is not None and (not isinstance(value, str) or len(value) > MAX_SUBJECT_CHARS):
        _invalid(field + "." + key + " must be a short string")
    return value


def _validated_worker_rows(rows, field, *, top_level):
    normalized = []
    for row in rows:
        item = dict(row)
        for key in ("task_id", "dispatch_id", "worktree", "run_id", "worker_state"):
            _optional_text(item, key, field)
        attempt = item.get("attempt")
        if attempt is not None and (type(attempt) is not int or attempt < 1):
            _invalid(field + ".attempt must be a positive integer")
        for key in ("settled", "remote"):
            if key in item and not isinstance(item[key], bool):
                _invalid(field + "." + key + " must be a boolean")
        if top_level and "row" in item:
            _invalid(field + " must not nest a row object")
        normalized.append(item)
    return normalized


def _validated_task_rows(rows, field):
    normalized = []
    for row in rows:
        item = dict(row)
        _optional_text(item, "task_id", field)
        orca_task_id = item.get("orca_task_id")
        if orca_task_id is not None and (
                not isinstance(orca_task_id, str) or len(orca_task_id) > MAX_SUBJECT_CHARS):
            _invalid(field + ".orca_task_id must be a short string or null")
        if "provisional" in item and not isinstance(item["provisional"], bool):
            _invalid(field + ".provisional must be a boolean")
        normalized.append(item)
    return normalized


def _validated_pr_rows(rows, field):
    normalized = []
    for row in rows:
        item = dict(row)
        number = item.get("pr")
        if number is None and item.get("head") is None:
            _invalid(field + " rows need a pr number or a head branch")
        if number is not None and type(number) is not int and not isinstance(number, str):
            _invalid(field + ".pr must be an integer or a string")
        for key in ("head", "head_sha", "merge_sha", "state", "run_id"):
            _optional_text(item, key, field)
        normalized.append(item)
    return normalized


def _validated_ticket_rows(rows, field):
    normalized = []
    for row in rows:
        item = dict(row)
        if not isinstance(item.get("ticket"), str) or not item["ticket"]:
            _invalid(field + " rows need a ticket id")
        _optional_text(item, "state", field)
        normalized.append(item)
    return normalized


def _validated_ancestry_rows(rows, field):
    normalized = []
    for row in rows:
        item = dict(row)
        for key in ("ancestor", "descendant"):
            if not isinstance(item.get(key), str) or not item[key]:
                _invalid(field + " rows need " + key)
        if not isinstance(item.get("result"), bool):
            _invalid(field + ".result must be a boolean")
        normalized.append(item)
    return normalized


def _receipt_entry(entry, field):
    if not isinstance(entry, dict):
        _invalid(field + " must be an object")
    state = entry.get("state")
    if state not in RECEIPT_STATES:
        _invalid(field + ".state must be one of " + ", ".join(RECEIPT_STATES))
    reason = entry.get("reason", "")
    if not isinstance(reason, str):
        _invalid(field + ".reason must be a string")
    receipt = entry.get("receipt", {})
    if not isinstance(receipt, dict):
        _invalid(field + ".receipt must be an object")
    return {"state": state, "reason": reason, "receipt": receipt}


def _validated_request_show(value, field):
    if not isinstance(value, dict):
        _invalid(field + " must be an object keyed by request id")
    normalized = {}
    for request_id, entry in value.items():
        if not isinstance(request_id, str) or not request_id:
            _invalid(field + " keys must be request ids")
        normalized[request_id] = _receipt_entry(entry, field + "[" + request_id + "]")
    return normalized


def _validated_worker_show(value, field):
    if not isinstance(value, dict):
        _invalid(field + " must be an object keyed by dispatch id")
    normalized = {}
    for dispatch_id, entry in value.items():
        if not isinstance(dispatch_id, str) or not dispatch_id:
            _invalid(field + " keys must be dispatch ids")
        if not isinstance(entry, dict):
            _invalid(field + "[" + dispatch_id + "] must be an object")
        state = entry.get("state", "unknown")
        if state not in SOURCE_STATES:
            _invalid(field + "[" + dispatch_id + "].state must be 'ok' or 'unknown'")
        reason = entry.get("reason", "")
        if not isinstance(reason, str):
            _invalid(field + "[" + dispatch_id + "].reason must be a string")
        worker = entry.get("worker", {})
        if not isinstance(worker, dict):
            _invalid(field + "[" + dispatch_id + "].worker must be an object")
        normalized[dispatch_id] = {
            "state": state, "reason": reason, "worker": dict(worker),
        }
    return normalized


def validated_authority(snapshot):
    """Return a normalized authority snapshot or raise ``ValueError``.

    A missing source is filled with ``unknown`` and a reason, never with an empty
    successful read: the report must be able to tell "nothing happened" from
    "could not read". Present sources are validated; unknown top-level fields are
    rejected so a typo cannot silently drop authority.
    """
    if not isinstance(snapshot, dict):
        _invalid("authority snapshot must be an object")
    unknown = sorted(set(snapshot) - {"schema_version", "run_id", "observed_at",
                                      *AUTHORITY_SOURCES})
    if unknown:
        _invalid("unknown authority fields: " + ", ".join(unknown))
    version = snapshot.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION:
        _invalid("authority schema_version must be 1")
    run_id = _portable_identifier(snapshot.get("run_id"), "run_id")
    observed_at = _utc_timestamp(snapshot.get("observed_at"))

    orca_value = snapshot.get("orca", {})
    if not isinstance(orca_value, dict):
        _invalid("orca must be an object")
    unknown_orca = sorted(set(orca_value) - {
        "request_show", "workers", "worker_show", "tasks", "messages"})
    if unknown_orca:
        _invalid("unknown orca authority fields: " + ", ".join(unknown_orca))
    request_show = _validated_request_show(
        orca_value.get("request_show", {}), "orca.request_show")
    worker_state, worker_reason, worker_rows = _rows(
        orca_value.get("workers", _unknown_source("worker-list not provided")),
        "orca.workers", "rows")
    task_state, task_reason, task_rows = _rows(
        orca_value.get("tasks", _unknown_source("task-list not provided")),
        "orca.tasks", "rows")
    worker_show = _validated_worker_show(
        orca_value.get("worker_show", {}), "orca.worker_show")
    messages = orca_value.get("messages")
    if messages is None:
        message_state, message_reason, message_rows = "unknown", "message authority not provided", []
    else:
        message_state, message_reason, message_rows = _rows(
            messages, "orca.messages", "rows")

    pr_state, pr_reason, pr_rows = _rows(
        snapshot.get("pr_host", _unknown_source("PR host not provided")),
        "pr_host", "pull_requests")
    tracker_state, tracker_reason, tracker_rows = _rows(
        snapshot.get("tracker", _unknown_source("tracker not provided")),
        "tracker", "tickets")
    git_state, git_reason, ancestry_rows = _rows(
        snapshot.get("git", _unknown_source("git authority not provided")),
        "git", "ancestry")

    progress_value = snapshot.get("progress")
    if progress_value is None:
        progress = _source("unknown", "progress binding not provided",
                           orca_run_id=None)
    else:
        progress_state, progress_reason = _source_state(progress_value, "progress")
        progress_run_id = progress_value.get("orca_run_id")
        if progress_run_id is not None:
            progress_run_id = _portable_identifier(
                progress_run_id, "progress.orca_run_id")
        progress = _source(progress_state, progress_reason,
                           orca_run_id=progress_run_id)

    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "observed_at": observed_at,
        "orca": {
            "request_show": request_show,
            "workers": _source(worker_state, worker_reason,
                               rows=_validated_worker_rows(worker_rows, "orca.workers",
                                                           top_level=True)),
            "worker_show": worker_show,
            "tasks": _source(task_state, task_reason,
                             rows=_validated_task_rows(task_rows, "orca.tasks")),
            "messages": _source(message_state, message_reason,
                                rows=_validated_worker_rows(message_rows, "orca.messages",
                                                            top_level=False)),
        },
        "pr_host": _source(pr_state, pr_reason,
                           pull_requests=_validated_pr_rows(pr_rows, "pr_host")),
        "tracker": _source(tracker_state, tracker_reason,
                           tickets=_validated_ticket_rows(tracker_rows, "tracker")),
        "git": _source(git_state, git_reason,
                       ancestry=_validated_ancestry_rows(ancestry_rows, "git")),
        "progress": progress,
    }


def _evidence(source, ref, state):
    return {"source": source, "ref": ref, "state": state}


def _ledger_ref(run_id, record_id):
    return "ledger:" + run_id + ":" + record_id


def _ledger_evidence(run_id, record):
    return _evidence("ledger", _ledger_ref(run_id, record["record_id"]), record["state"])


def _decision(decision_class, reason, *, action, request_id, subject, evidence):
    return {
        "class": decision_class,
        "reason": reason,
        "action": action,
        "request_id": request_id,
        "subject": subject,
        "evidence": list(evidence),
    }


def _repair(kind, *, action, request_id, subject, state, evidence, **detail):
    repair = {
        "kind": kind,
        "action": action,
        "request_id": request_id,
        "subject": dict(subject),
        "state": state,
        "evidence": list(evidence),
    }
    repair.update(detail)
    return repair


def _task_join_key(row):
    """The Orca Task ID is the only join key; a provisional row has none."""
    if row.get("provisional"):
        return None
    orca_task_id = row.get("orca_task_id")
    return orca_task_id or None


def _row_matches_task(row, subject):
    key = _task_join_key(row)
    return bool(key) and subject.get("task_id") == key


def _row_matches_worker(row, subject):
    if subject.get("task_id") and row.get("task_id") == subject["task_id"]:
        return True
    if subject.get("dispatch_id") and row.get("dispatch_id") == subject["dispatch_id"]:
        return True
    return False


def _row_matches_pr(row, intent):
    if intent["subject"].get("pr") is not None:
        if str(row.get("pr")) == str(intent["subject"]["pr"]):
            return True
    head = intent["detail"].get("head") if isinstance(intent["detail"], dict) else None
    return bool(head) and row.get("head") == head


def _row_matches_ticket(row, subject):
    return bool(subject.get("ticket")) and row.get("ticket") == subject["ticket"]


#: Worker states that prove a stop/abandon/release reached a terminal point.
TERMINAL_WORKER_STATES = (
    "stopped", "abandoned", "closed", "done", "settled", "exited", "released",
)


def _terminal_worker(row):
    return str(row.get("worker_state", "")).lower() in TERMINAL_WORKER_STATES


def _projection_repair(intent, probe, evidence):
    """A projection update the authority supplies for a progress event.

    The ledger never writes ``progress.json``; it records the observation the
    coordinator can project. A provisional progress task (``orca_task_id: null``)
    is never a join key, so only an authority-supplied Orca Task ID or dispatch
    identity is written into the projection.
    """
    detail = intent["detail"] if isinstance(intent["detail"], dict) else {}
    if not detail.get("progress_event_id"):
        return None
    projection = {}
    worker = next((row for kind, row in probe["rows"] if kind == "worker"), None)
    task = next((row for kind, row in probe["rows"] if kind == "task"), None)
    if worker:
        for key in ("task_id", "dispatch_id", "attempt", "worktree"):
            if worker.get(key) is not None:
                projection[key] = worker[key]
    if task and task.get("orca_task_id"):
        projection["task_id"] = task["orca_task_id"]
    if not projection:
        return None
    return _repair(
        "projection", action=intent["action"], request_id=intent["request_id"],
        subject=intent["subject"], state="observed", evidence=evidence,
        projection=projection,
    )


def _no_effect_decision(intent, evidence):
    """The decision a readable-but-effectless action always needs.

    These are the matrix rows where retrying or replaying is a new decision, not a
    repair: merge and tracker close are read-back only, a missing reply is not a
    failed send, and a stop or release without proof must not be retried.
    """
    action = intent["action"]
    if action == "merge":
        decision_class = "merge_replay"
        reason = "merge is read back, never replayed automatically"
    elif action == "tracker-close":
        decision_class = "tracker_close_replay"
        reason = "tracker close is read back, never replayed automatically"
    elif action in ("send", "reply"):
        decision_class = "send_reply_no_resend"
        reason = "no reply is not proof of failure; a resend is a decision"
    elif action in ("stop", "abandon"):
        decision_class = "stop_abandon_no_retry"
        reason = "the worker is not proven stopped; do not retry without proof"
    elif action in ("retain", "release"):
        decision_class = "unsettled_release"
        reason = "the worker is not proven settled; the action is a decision"
    else:
        decision_class = "orphan_intent"
        reason = "the intent has no authority effect; execute or abandon is a decision"
    return _decision(decision_class, reason, action=action,
                     request_id=intent["request_id"],
                     subject=intent["subject"], evidence=evidence)


def _source_ref(source):
    """A stable evidence ref prefix for an unreadable source."""
    prefixes = {
        "orca.worker-list": "orca:worker-list:",
        "orca.task-list": "orca:task-list:",
        "orca.request-show": "orca:request-show:",
    }
    return prefixes.get(source, source + ":")


def _worker_evidence(row, source="orca.worker-list"):
    identity = row.get("task_id") or row.get("dispatch_id") or "unknown"
    prefix = "orca:worker-show:" if source == "orca.worker-show" else "orca:worker-list:"
    return _evidence(source, prefix + identity, "present")


def _probe(snapshot, intent):
    """Read one intent's effect from the authority without changing anything.

    Returns ``observable`` (whether a negative answer is authoritative), ``effect``
    (a positive effect was found), the matching rows and their evidence.
    """
    policy = ACTION_POLICY[intent["action"]]["probe"]
    result = {"observable": True, "effect": False, "rows": [], "evidence": [],
              "reason": "", "subject": dict(intent["subject"])}
    orca = snapshot["orca"]
    if policy == "workers":
        need_tasks = intent["action"] == "worker-start"
        readable = True
        rows = []
        for kind, section, field in (("worker", orca["workers"], "orca.worker-list"),
                                     ("task", orca["tasks"], "orca.task-list")):
            if section["state"] != "ok":
                readable = False
                result["evidence"].append(
                    _evidence(field, _source_ref(field), "unknown"))
                result["reason"] = result["reason"] or section["reason"]
                continue
            for row in section["rows"]:
                if kind == "worker" and _row_matches_worker(row, intent["subject"]):
                    rows.append((kind, row))
                    result["evidence"].append(_worker_evidence(row, field))
                if kind == "task" and _row_matches_task(row, intent["subject"]):
                    rows.append((kind, row))
                    result["evidence"].append(_evidence(
                        field, "orca:task-list:" + row["orca_task_id"], "present"))
        if need_tasks:
            result["observable"] = readable
            result["rows"] = rows
            result["effect"] = bool(rows)
        else:
            action = intent["action"]
            matching = [row for kind, row in rows if kind == "worker"]
            proof = list(matching)
            worker_show_ok = False
            if action in ("stop", "abandon", "retain", "release"):
                dispatch_id = intent["subject"].get("dispatch_id")
                if not dispatch_id:
                    dispatch_id = next(
                        (row.get("dispatch_id") for row in matching
                         if row.get("dispatch_id")), None)
                entry = orca["worker_show"].get(dispatch_id) if dispatch_id else None
                if entry is not None and entry["state"] == "ok":
                    worker = entry["worker"]
                    worker_show_ok = True
                    proof.append(worker)
                    result["evidence"].append(
                        _worker_evidence(worker, "orca.worker-show"))
                elif entry is not None:
                    result["evidence"].append(_evidence(
                        "orca.worker-show", "orca:worker-show:" + str(dispatch_id),
                        "unknown"))
            workers_ok = orca["workers"]["state"] == "ok"
            if action in ("stop", "abandon", "release"):
                result["observable"] = workers_ok or worker_show_ok
                result["rows"] = [("worker", row) for row in proof]
            else:
                result["observable"] = workers_ok
                result["rows"] = [("worker", row) for row in matching]
            if action in ("stop", "abandon"):
                # A stop proves its effect only through a proof row: a vanished row is
                # absence of proof, not proof that the worker stopped.
                result["effect"] = result["observable"] and bool(proof) and all(
                    _terminal_worker(row) for row in proof)
            elif action == "release":
                # worker-show and worker-list are both allowed proof sources; the union
                # is safe because every proof row must be terminal or settled.
                result["effect"] = result["observable"] and bool(proof) and all(
                    _terminal_worker(row) or row.get("settled") is True for row in proof)
            elif action == "retain":
                result["effect"] = bool(matching) and not all(
                    _terminal_worker(row) for row in matching)
            else:
                result["effect"] = bool(matching)
        if result["observable"] and (not result["effect"]
                                     or not [row for _kind, row in result["rows"]]):
            identity = (intent["subject"].get("task_id")
                        or intent["subject"].get("dispatch_id") or "unknown")
            result["evidence"].append(_evidence(
                "orca.worker-list", "orca:worker-list:" + str(identity), "absent"))
        return result
    if policy in ("messages", "pr", "merge", "tracker"):
        if policy == "messages":
            section = orca["messages"]
            field = "orca.messages"
            matches = [row for row in section["rows"]
                       if intent["request_id"] and row.get("request_id") == intent["request_id"]]
        elif policy == "tracker":
            section = snapshot["tracker"]
            field = "tracker"
            matches = [row for row in section["tickets"]
                       if _row_matches_ticket(row, intent["subject"])]
        else:
            section = snapshot["pr_host"]
            field = "pr_host"
            matches = [row for row in section["pull_requests"]
                       if _row_matches_pr(row, intent)]
        result["observable"] = section["state"] == "ok"
        result["reason"] = section["reason"]
        if section["state"] != "ok":
            result["evidence"].append(
                _evidence(field, _source_ref(field), "unknown"))
            return result
        if policy == "merge":
            merged = [row for row in matches if str(row.get("state", "")).upper() == "MERGED"]
            git = snapshot["git"]
            ancestry = []
            if git["state"] == "ok":
                wanted = {str(row.get("merge_sha")) for row in matches if row.get("merge_sha")}
                ancestry = [row for row in git["ancestry"]
                            if row["result"] and (not wanted or row["descendant"] in wanted)]
                for row in ancestry:
                    result["evidence"].append(_evidence(
                        "git", "git:ancestry:" + row["ancestor"] + ".." + row["descendant"],
                        "merged"))
            elif git["reason"]:
                result["reason"] = result["reason"] or git["reason"]
            for row in merged:
                result["evidence"].append(_evidence(
                    "pr_host", "pr_host:pr:" + str(row.get("pr", row.get("head"))), "MERGED"))
            result["rows"] = [("pr", row) for row in merged] + [
                ("git", row) for row in ancestry]
            result["effect"] = bool(merged)
            return result
        if policy == "pr":
            result["rows"] = [("pr", row) for row in matches]
            for row in matches:
                result["evidence"].append(_evidence(
                    "pr_host", "pr_host:pr:" + str(row.get("pr", row.get("head"))),
                    str(row.get("state", "unknown"))))
            result["effect"] = bool(matches)
            return result
        if policy == "tracker":
            closed = [row for row in matches if row.get("state") == "closed"]
            result["rows"] = [("ticket", row) for row in closed]
            for row in closed:
                result["evidence"].append(_evidence(
                    "tracker", "tracker:ticket:" + row["ticket"], "closed"))
            result["effect"] = bool(closed)
            return result
        result["rows"] = [("message", row) for row in matches]
        result["effect"] = bool(matches)
        for row in matches:
            result["evidence"].append(_evidence(
                "orca.messages", "orca:message:" + str(row.get("message_id", intent["request_id"])),
                str(row.get("state", "observed"))))
        return result
    # No authority probe exists for this action: only request-show can judge it.
    result["observable"] = False
    result["reason"] = "no authority probe for " + intent["action"]
    return result


def _conflicts(snapshot, intent, result_record, probe):
    """Fields where the ledger and a readable authority disagree."""
    conflicts = []
    workers = [row for kind, row in probe["rows"] if kind == "worker"]
    if workers:
        subject = intent["subject"]
        for field in ("attempt", "dispatch_id", "task_id"):
            expected = subject.get(field)
            if expected is None:
                continue
            observed = [row.get(field) for row in workers if row.get(field) is not None]
            if observed and all(str(value) != str(expected) for value in observed):
                conflicts.append({
                    "field": field,
                    "ledger": expected,
                    "authority": observed[0],
                })
        run_id = snapshot["run_id"]
        observed_runs = [row.get("run_id") for row in workers if row.get("run_id")]
        if observed_runs and all(value != run_id for value in observed_runs):
            conflicts.append({"field": "run_id", "ledger": run_id,
                              "authority": observed_runs[0]})
    if result_record["state"] == "completed" and probe["observable"] \
            and not probe["effect"] and ACTION_POLICY[intent["action"]]["probe"] != "none":
        conflicts.append({
            "field": "effect",
            "ledger": "completed",
            "authority": "no matching effect",
        })
    return conflicts


def _outcome(classification, window, intent=None, *, action=None, request_id="",
             subject=None, result_record=None, evidence=None, repairs=None,
             decision=None, conflicts=None):
    return {
        "classification": classification,
        "window": window,
        "action": action if intent is None else intent["action"],
        "request_id": request_id if intent is None else intent["request_id"],
        "subject": dict(subject if subject is not None else
                        (intent["subject"] if intent is not None else {})),
        "intent_record_id": intent["record_id"] if intent is not None else None,
        "result_record_id": result_record["record_id"] if result_record else None,
        "evidence": list(evidence or []),
        "repairs": list(repairs or []),
        "decision": decision,
        "conflicts": list(conflicts or []),
    }


def _matching_result(intent, results_by_request, no_key_results, claimed):
    if intent["request_id"]:
        for result in results_by_request.get(intent["request_id"], []):
            if result["record_id"] not in claimed:
                return result
        return None
    subject = canonical_json(intent["subject"])
    for result in no_key_results:
        if (result["record_id"] not in claimed and result["action"] == intent["action"]
                and canonical_json(result["subject"]) == subject):
            return result
    return None


def _classify_closed_intent(snapshot, intent, result_record):
    evidence = [_ledger_evidence(snapshot["run_id"], intent),
                _ledger_evidence(snapshot["run_id"], result_record)]
    probe = _probe(snapshot, intent)
    evidence.extend(probe["evidence"])
    conflicts = _conflicts(snapshot, intent, result_record, probe)
    if conflicts:
        repair = _repair(
            "divergent_marker", action=intent["action"],
            request_id=intent["request_id"], subject=intent["subject"],
            state="divergent", evidence=evidence, conflicts=conflicts,
        )
        decision = _decision(
            "divergent", "ledger and authority disagree; the coordinator decides",
            action=intent["action"], request_id=intent["request_id"],
            subject=intent["subject"], evidence=evidence)
        return _outcome("divergent", "W2", intent, result_record=result_record,
                        evidence=evidence, repairs=[repair], decision=decision,
                        conflicts=conflicts)
    decision = None
    if result_record["state"] == "failed":
        decision = _decision(
            "unproven_retry",
            "a failed result is not proof that the failure is transient; retry is a decision",
            action=intent["action"], request_id=intent["request_id"],
            subject=intent["subject"], evidence=evidence)
    elif result_record["state"] == "unknown":
        decision = _decision(
            "unknown_outcome",
            "the recorded outcome is unknown; read authority before deciding",
            action=intent["action"], request_id=intent["request_id"],
            subject=intent["subject"], evidence=evidence)
    projection = _projection_repair(intent, probe, evidence)
    return _outcome("matched", "W2", intent, result_record=result_record,
                    evidence=evidence,
                    repairs=[projection] if projection else [],
                    decision=decision)


def _receipt_state(snapshot, request_id):
    if not request_id:
        return None, None
    entry = snapshot["orca"]["request_show"].get(request_id)
    if entry is None:
        return "unknown", "request-show did not answer for this request"
    return entry["state"], entry


def _classify_open_intent(snapshot, intent):
    evidence = [_ledger_evidence(snapshot["run_id"], intent)]
    receipt_state, receipt = _receipt_state(snapshot, intent["request_id"])
    if receipt_state is not None:
        evidence.append(_evidence("orca.request-show",
                                  "orca:request-show:" + intent["request_id"],
                                  receipt_state))
    probe = _probe(snapshot, intent)
    evidence.extend(probe["evidence"])
    policy = ACTION_POLICY[intent["action"]]
    projection = _projection_repair(intent, probe, evidence)

    def adopted(repair):
        return [repair] + ([projection] if projection else [])

    if receipt_state == "completed":
        repair = _repair(
            "late_result", action=intent["action"], request_id=intent["request_id"],
            subject=intent["subject"], state="completed", evidence=evidence,
            receipt=receipt.get("receipt", {}) if receipt else {},
        )
        conflicts = _conflicts(snapshot, intent, {"state": "completed"}, probe)
        if conflicts:
            marker = _repair(
                "divergent_marker", action=intent["action"],
                request_id=intent["request_id"], subject=intent["subject"],
                state="divergent", evidence=evidence, conflicts=conflicts)
            decision = _decision(
                "divergent", "a completed receipt and authority disagree",
                action=intent["action"], request_id=intent["request_id"],
                subject=intent["subject"], evidence=evidence)
            return _outcome("divergent", "W2", intent, evidence=evidence,
                            repairs=[repair, marker]
                            + ([projection] if projection else []),
                            decision=decision, conflicts=conflicts)
        return _outcome("matched", "W2", intent, evidence=evidence,
                        repairs=adopted(repair))

    if receipt_state == "pending":
        replay = policy["replay"]
        if replay == "never":
            if probe["effect"]:
                repair = _repair(
                    "late_result", action=intent["action"],
                    request_id=intent["request_id"], subject=intent["subject"],
                    state="observed", evidence=evidence)
                return _outcome("matched", "W2", intent, evidence=evidence,
                                repairs=adopted(repair))
            decision = _no_effect_decision(intent, evidence)
            if probe["observable"]:
                return _outcome("orphan_intent", "W1", intent, evidence=evidence,
                                decision=decision)
            return _outcome("unknown", "W3", intent, evidence=evidence,
                            decision=decision)
        if replay == "settled":
            settled = [row for kind, row in probe["rows"]
                       if kind == "worker" and row.get("settled") is True]
            if not settled:
                decision = _decision(
                    "unsettled_release",
                    "the worker is not proven settled; release is never replayed",
                    action=intent["action"], request_id=intent["request_id"],
                    subject=intent["subject"], evidence=evidence)
                return _outcome("unknown", "W3", intent, evidence=evidence,
                                decision=decision)
        if replay == "lookup":
            if probe["effect"]:
                repair = _repair(
                    "late_result", action=intent["action"],
                    request_id=intent["request_id"], subject=intent["subject"],
                    state="observed", evidence=evidence)
                return _outcome("matched", "W2", intent, evidence=evidence,
                                repairs=adopted(repair))
            if not probe["observable"]:
                decision = _decision(
                    "pr_create_lookup",
                    "the PR host is unreadable; lookup before create cannot run",
                    action=intent["action"], request_id=intent["request_id"],
                    subject=intent["subject"], evidence=evidence)
                return _outcome("unknown", "W3", intent, evidence=evidence,
                                decision=decision)
        repair = _repair(
            "replay", action=intent["action"], request_id=intent["request_id"],
            subject=intent["subject"], state="pending", evidence=evidence,
            command=(intent["detail"].get("command")
                     if isinstance(intent["detail"], dict) else None),
        )
        return _outcome("matched", "W3", intent, evidence=evidence, repairs=[repair])

    if receipt_state == "absent":
        if probe["observable"] and probe["effect"]:
            repair = _repair(
                "late_result", action=intent["action"],
                request_id=intent["request_id"], subject=intent["subject"],
                state="observed", evidence=evidence)
            return _outcome("matched", "W2", intent, evidence=evidence,
                            repairs=adopted(repair))
        if not probe["observable"]:
            decision = _decision(
                "unreadable_authority",
                "an absent receipt is not proof; the authority is unreadable",
                action=intent["action"], request_id=intent["request_id"],
                subject=intent["subject"], evidence=evidence)
            return _outcome("unknown", "W3", intent, evidence=evidence,
                            decision=decision)
        decision = _no_effect_decision(intent, evidence)
        return _outcome("orphan_intent", "W1", intent, evidence=evidence,
                        decision=decision)

    # Unknown or unasked receipt: a positive authority effect is still adoptable.
    if probe["effect"]:
        repair = _repair(
            "late_result", action=intent["action"], request_id=intent["request_id"],
            subject=intent["subject"], state="observed", evidence=evidence)
        return _outcome("matched", "W2", intent, evidence=evidence,
                        repairs=adopted(repair))
    if known_receipt := (receipt_state == "unknown"):
        decision = _decision(
            "unreadable_authority",
            "request-show is unreadable; no judgment is possible without authority",
            action=intent["action"], request_id=intent["request_id"],
            subject=intent["subject"], evidence=evidence)
        return _outcome("unknown", "W3", intent, evidence=evidence, decision=decision)
    if not probe["observable"]:
        decision = _decision(
            "unreadable_authority",
            "the authority needed for this action is unreadable",
            action=intent["action"], request_id=intent["request_id"],
            subject=intent["subject"], evidence=evidence)
        return _outcome("unknown", "W3", intent, evidence=evidence, decision=decision)
    decision = _no_effect_decision(intent, evidence)
    return _outcome("orphan_intent", "W1", intent, evidence=evidence, decision=decision)


def _authority_effects(snapshot):
    """Every positive authority fact, normalized to an action and subject."""
    effects = []
    workers = snapshot["orca"]["workers"]
    if workers["state"] == "ok":
        for row in workers["rows"]:
            subject = {}
            if row.get("task_id"):
                subject["task_id"] = row["task_id"]
            if row.get("dispatch_id"):
                subject["dispatch_id"] = row["dispatch_id"]
            effects.append({
                "action": "worker-start", "subject": subject,
                "evidence": [_worker_evidence(row)],
                "rows": [("worker", row)],
            })
    tasks = snapshot["orca"]["tasks"]
    if tasks["state"] == "ok":
        for row in tasks["rows"]:
            key = _task_join_key(row)
            if not key:
                continue
            effects.append({
                "action": "worker-start", "subject": {"task_id": key},
                "evidence": [_evidence("orca.task-list",
                                       "orca:task-list:" + key, "present")],
                "rows": [("task", row)],
            })
    pr_host = snapshot["pr_host"]
    if pr_host["state"] == "ok":
        for row in pr_host["pull_requests"]:
            subject = {"pr": row.get("pr")} if row.get("pr") is not None else {}
            merged = str(row.get("state", "")).upper() == "MERGED"
            effects.append({
                "action": "merge" if merged else "pr-create",
                "subject": subject,
                "evidence": [_evidence(
                    "pr_host", "pr_host:pr:" + str(row.get("pr", row.get("head"))),
                    "MERGED" if merged else str(row.get("state", "unknown")))],
                "rows": [("pr", row)],
            })
    tracker = snapshot["tracker"]
    if tracker["state"] == "ok":
        for row in tracker["tickets"]:
            if row.get("state") != "closed":
                continue
            effects.append({
                "action": "tracker-close", "subject": {"ticket": row["ticket"]},
                "evidence": [_evidence("tracker",
                                       "tracker:ticket:" + row["ticket"], "closed")],
                "rows": [("ticket", row)],
            })
    git = snapshot["git"]
    if git["state"] == "ok":
        for row in git["ancestry"]:
            if not row["result"]:
                continue
            effects.append({
                "action": "merge", "subject": {},
                "evidence": [_evidence(
                    "git", "git:ancestry:" + row["ancestor"] + ".." + row["descendant"],
                    "merged")],
                "rows": [("git", row)],
            })
    merged = {}
    order = []
    for effect in effects:
        key = _effect_key(effect)
        if key not in merged:
            merged[key] = effect
            order.append(key)
        else:
            merged[key]["evidence"].extend(effect["evidence"])
            merged[key]["rows"].extend(effect["rows"])
            for field, value in effect["subject"].items():
                merged[key]["subject"].setdefault(field, value)
    return [merged[key] for key in order]


def _effect_key(effect):
    subject = effect["subject"]
    if effect["action"] == "worker-start":
        return "worker-start:" + str(subject.get("task_id") or subject.get("dispatch_id"))
    if effect["action"] == "tracker-close":
        return "tracker-close:" + str(subject.get("ticket"))
    if effect["action"] == "merge":
        row = effect["rows"][0][1] if effect["rows"] else {}
        identity = row.get("merge_sha") or row.get("pr") \
            or (str(row.get("ancestor")) + ".." + str(row.get("descendant")))
        return "merge:" + str(identity)
    return effect["action"] + ":" + str(subject.get("pr") or subject.get("task_id"))


def _effect_claimed(effect, records):
    for record in records:
        subject = record["subject"]
        if effect["action"] == "worker-start":
            if effect["subject"].get("task_id") \
                    and subject.get("task_id") == effect["subject"]["task_id"]:
                return True
            if effect["subject"].get("dispatch_id") \
                    and subject.get("dispatch_id") == effect["subject"]["dispatch_id"]:
                return True
        elif effect["action"] in ("pr-create", "merge"):
            if effect["subject"].get("pr") is not None \
                    and str(subject.get("pr")) == str(effect["subject"]["pr"]):
                return True
            detail = record["detail"]
            for key in ("head", "merge_sha"):
                if isinstance(detail, dict) and detail.get(key) \
                        and any(row.get(key) == detail[key]
                                for kind, row in effect["rows"]):
                    return True
        elif effect["action"] == "tracker-close":
            if effect["subject"].get("ticket") \
                    and subject.get("ticket") == effect["subject"]["ticket"]:
                return True
    return False


def _classify_orphan_effect(snapshot, effect):
    evidence = list(effect["evidence"])
    repair = _repair(
        "late_result", action=effect["action"], request_id="",
        subject=effect["subject"], state="observed", evidence=evidence,
    )
    return _outcome("orphan_effect", "W2", None, action=effect["action"],
                    subject=effect["subject"], evidence=evidence, repairs=[repair])


def _classify_run_binding(snapshot):
    """One run-level outcome when the progress binding names a different Run.

    A progress projection is not authority for progress facts, but its ``orca_run_id``
    is a recorded binding: when it is readable and non-null it must name the ledger's
    Run. An absent or null id is unbound, not a disagreement.
    """
    progress = snapshot["progress"]
    authority_run_id = progress.get("orca_run_id")
    if progress["state"] != "ok" or authority_run_id is None:
        return None
    ledger_run_id = snapshot["run_id"]
    if authority_run_id == ledger_run_id:
        return None
    conflict = {"field": "orca_run_id", "ledger": ledger_run_id,
                "authority": authority_run_id}
    evidence = [
        _evidence("progress", "progress:orca_run_id:" + authority_run_id, "ok"),
        _evidence("ledger", "ledger:run:" + ledger_run_id, "present"),
    ]
    repair = _repair(
        "divergent_marker", action="progress", request_id="", subject={},
        state="divergent", evidence=evidence, conflicts=[conflict])
    decision = _decision(
        "run_binding_mismatch",
        "the ledger Run " + ledger_run_id + " and the progress binding "
        + authority_run_id + " name different Runs; the coordinator decides",
        action="progress", request_id="", subject={}, evidence=evidence)
    return _outcome("divergent", "W2", None, action="progress", evidence=evidence,
                    repairs=[repair], decision=decision, conflicts=[conflict])


def _unreadable_sources(snapshot, request_unknowns):
    """Every source that could not be read, deduplicated and in a stable order."""
    found = []
    seen = set()

    def add(source, reason):
        if source in seen:
            return
        seen.add(source)
        found.append({"source": source, "reason": reason or "unavailable"})

    orca = snapshot["orca"]
    if orca["workers"]["state"] != "ok":
        add("orca.worker-list", orca["workers"]["reason"])
    if orca["tasks"]["state"] != "ok":
        add("orca.task-list", orca["tasks"]["reason"])
    for request_id in sorted(request_unknowns):
        add("orca.request-show", request_unknowns[request_id])
    if snapshot["pr_host"]["state"] != "ok":
        add("pr_host", snapshot["pr_host"]["reason"])
    if snapshot["tracker"]["state"] != "ok":
        add("tracker", snapshot["tracker"]["reason"])
    if snapshot["git"]["state"] != "ok":
        add("git", snapshot["git"]["reason"])
    return found


def _source_outcomes(snapshot, cited):
    """One ``unknown`` classification per unreadable source not already cited."""
    outcomes = []
    for source in _unreadable_sources(snapshot, {}):
        if source["source"] in cited:
            continue
        outcomes.append(_outcome(
            "unknown", "W3", None, action=None, subject={},
            evidence=[_evidence(source["source"],
                                _source_ref(source["source"]), "unknown")],
        ))
    return outcomes


def classify(records, authority_snapshot):
    """Classify ledger records against an authority snapshot; a pure function.

    The five outcomes are ``matched``, ``orphan_intent``, ``orphan_effect``,
    ``divergent`` and ``unknown``. The report names every classification's
    evidence and carries the repairs that reconciliation may apply. An intent
    with an absent receipt and no authority effect is ``orphan_intent`` when the
    absence is positively readable; an unreadable authority is ``unknown``. A
    request-show ``absent`` with no intent is not a judgment at all.
    """
    snapshot = validated_authority(authority_snapshot)
    run_id = snapshot["run_id"]
    normalized = []
    for record in records:
        item = validated(record)
        if item["run_id"] != run_id:
            _invalid("record run_id does not match the authority snapshot")
        normalized.append(item)

    intents = [record for record in normalized if record["kind"] == "intent"]
    results = [record for record in normalized if record["kind"] == "result"]
    results_by_request = {}
    for result in results:
        if result["request_id"]:
            results_by_request.setdefault(result["request_id"], []).append(result)
    no_key_results = [result for result in results if not result["request_id"]]
    claimed = set()
    outcomes = []

    for intent in intents:
        result = _matching_result(intent, results_by_request, no_key_results, claimed)
        if result is not None:
            claimed.add(result["record_id"])
            outcomes.append(_classify_closed_intent(snapshot, intent, result))
        else:
            outcomes.append(_classify_open_intent(snapshot, intent))

    for effect in _authority_effects(snapshot):
        if _effect_claimed(effect, normalized):
            continue
        outcomes.append(_classify_orphan_effect(snapshot, effect))

    run_binding = _classify_run_binding(snapshot)
    if run_binding is not None:
        outcomes.append(run_binding)

    cited = set()
    for outcome in outcomes:
        if outcome["classification"] != "unknown":
            continue
        for item in outcome["evidence"]:
            if item["state"] == "unknown":
                cited.add(item["source"])
    outcomes.extend(_source_outcomes(snapshot, cited))

    counts = {name: 0 for name in CLASSIFICATIONS}
    for outcome in outcomes:
        counts[outcome["classification"]] += 1
    repairs = [repair for outcome in outcomes for repair in outcome["repairs"]]
    decisions = [outcome["decision"] for outcome in outcomes if outcome["decision"]]

    request_unknowns = {
        request_id: entry["reason"]
        for request_id, entry in snapshot["orca"]["request_show"].items()
        if entry["state"] == "unknown"
    }
    report = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "observed_at": snapshot["observed_at"],
        "classifications": outcomes,
        "repairs": repairs,
        "decisions": decisions,
        "unreadable": _unreadable_sources(snapshot, request_unknowns),
        "counts": counts,
        "authority_sources": {
            "orca": "ok" if (snapshot["orca"]["workers"]["state"] == "ok"
                             and snapshot["orca"]["tasks"]["state"] == "ok") else "unknown",
            "pr_host": snapshot["pr_host"]["state"],
            "tracker": snapshot["tracker"]["state"],
            "git": snapshot["git"]["state"],
        },
    }
    validate_report(report)
    return report


def validate_report(report):
    """Validate a reconciliation report; every classification must name evidence."""
    if not isinstance(report, dict):
        _invalid("reconciliation report must be an object")
    required = (
        "schema_version", "run_id", "observed_at", "classifications", "repairs",
        "decisions", "unreadable", "counts", "authority_sources",
    )
    missing = [field for field in required if field not in report]
    if missing:
        _invalid("reconciliation report is missing: " + ", ".join(missing))
    unknown = sorted(set(report) - set(required) - {"closeout", "replays"})
    if unknown:
        _invalid("unknown reconciliation report fields: " + ", ".join(unknown))
    if report["schema_version"] != SCHEMA_VERSION:
        _invalid("reconciliation report schema_version must be 1")
    _portable_identifier(report["run_id"], "run_id")
    _utc_timestamp(report["observed_at"])
    counts = {name: 0 for name in CLASSIFICATIONS}
    for item in report["classifications"]:
        if not isinstance(item, dict):
            _invalid("classifications must be objects")
        classification = item.get("classification")
        if classification not in CLASSIFICATIONS:
            _invalid("unknown classification: " + repr(classification))
        if item.get("window") is not None and item.get("window") not in WINDOWS:
            _invalid("unknown window: " + repr(item.get("window")))
        if item.get("action") is not None and item["action"] not in ACTIONS:
            _invalid("unknown action in classification: " + repr(item.get("action")))
        evidence = item.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            _invalid("every classification must name its evidence")
        for entry in evidence:
            if not isinstance(entry, dict) or set(entry) != {"source", "ref", "state"}:
                _invalid("evidence entries need source, ref and state")
            if not entry["source"] or not entry["ref"]:
                _invalid("evidence source and ref must be non-empty")
        for repair in item.get("repairs", []):
            if repair.get("kind") not in REPAIR_KINDS:
                _invalid("unknown repair kind: " + repr(repair.get("kind")))
        counts[classification] += 1
    if report["counts"] != counts:
        _invalid("counts do not match the classifications")
    for repair in report["repairs"]:
        if repair.get("kind") not in REPAIR_KINDS:
            _invalid("unknown repair kind: " + repr(repair.get("kind")))
    for decision in report["decisions"]:
        if not isinstance(decision, dict) or not decision.get("class") \
                or not decision.get("reason"):
            _invalid("every decision needs a class and a reason")
        if not decision.get("evidence"):
            _invalid("every decision must name its evidence")
    for source in report["unreadable"]:
        if not isinstance(source, dict) or not source.get("source"):
            _invalid("unreadable entries need a source")
    if not isinstance(report["authority_sources"], dict):
        _invalid("authority_sources must be an object")


# ---------------------------------------------------------------------------
# Reconciliation: replay, generated records, closeout and the human report
# ---------------------------------------------------------------------------


def _stable_id(*parts):
    """A deterministic record id for a generated record.

    A repeated reconciliation over the same snapshot must not append the same late
    result twice, so generated ids are derived from their meaning rather than
    minted randomly. The payloads are equally deterministic (``observed_at`` is
    taken from the intent or the snapshot), which makes re-appending a no-op.
    """
    key = "orca-ledger:" + "|".join(
        canonical_json(part) if isinstance(part, (dict, list)) else str(part)
        for part in parts)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


def _generated_by_reconcile(record):
    """A record reconciliation wrote, not a source fact the next run must re-read."""
    if record["kind"] == "closeout":
        return True
    detail = record["detail"]
    return isinstance(detail, dict) and detail.get("repair") in REPAIR_KINDS


def _source_digest(snapshot, records):
    """Identity of one reconciliation: the authority read plus the source ledger.

    Generated repair records, pending items and closeouts are excluded so a second
    reconciliation over the same authority and the same source records is a no-op
    rather than a new closeout on every call.
    """
    source = sorted(
        (record for record in records if not _generated_by_reconcile(record)),
        key=lambda record: record["record_id"])
    facts = {key: value for key, value in snapshot.items() if key != "observed_at"}
    payload = {"snapshot": facts, "records": source}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _write_report_file(run_dir, report, digest):
    """Write the machine report under the Run, mode 0600, and return its path."""
    directory = run_dir / REPORTS_DIR
    _ensure_directory(directory)
    path = directory / ("reconcile-" + digest[:16] + ".json")
    if path.is_symlink():
        _invalid("reconciliation report must not be a symlink")
    if path.exists():
        return path
    data = (json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n").encode("utf-8")
    tmp = directory / (path.name + "." + uuid.uuid4().hex + ".tmp")
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         FILE_MODE)
    try:
        _write_all(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_directory(directory)
    return path


def _authority_ref(evidence):
    for item in evidence:
        if item["source"] != "ledger" and item["ref"]:
            return item["ref"]
    return ""


def _observed_at_for(intent, snapshot):
    return intent["observed_at"] if intent is not None else snapshot["observed_at"]


def _late_result_record(run_id, repair, intent, snapshot):
    request_id = repair.get("request_id") or ""
    idempotency = intent["idempotency"] if intent is not None else "none"
    if not request_id and idempotency in KEYED_IDEMPOTENCIES:
        idempotency = "none"
    state = repair.get("state") or "observed"
    if state not in STATES:
        state = "observed"
    detail = {"repair": "late_result", "evidence": repair["evidence"]}
    receipt = repair.get("receipt")
    if isinstance(receipt, dict) and receipt:
        detail["raw_receipt"] = receipt
    record = {
        "schema_version": SCHEMA_VERSION,
        "record_id": _stable_id(run_id, "late_result", repair["action"], request_id,
                                repair["subject"], state, repair["evidence"], receipt),
        "kind": "result",
        "run_id": run_id,
        "subject": dict(repair["subject"]),
        "action": repair["action"],
        "request_id": request_id,
        "idempotency": idempotency,
        "state": state,
        "evidence_ref": _authority_ref(repair["evidence"]),
        "observed_at": _observed_at_for(intent, snapshot),
        "actor": "rule",
        "detail": detail,
    }
    return record


def _observation_record(run_id, repair, intent, snapshot):
    request_id = repair.get("request_id") or ""
    idempotency = intent["idempotency"] if intent is not None else "none"
    if not request_id and idempotency in KEYED_IDEMPOTENCIES:
        idempotency = "none"
    if repair["kind"] == "divergent_marker":
        state = "divergent"
        action = repair["action"]
    else:
        state = "observed"
        action = "progress"
    detail = {"repair": repair["kind"], "evidence": repair["evidence"]}
    for key in ("conflicts", "projection"):
        if repair.get(key) is not None:
            detail[key] = repair[key]
    record = {
        "schema_version": SCHEMA_VERSION,
        "record_id": _stable_id(run_id, repair["kind"], action, request_id,
                                repair["subject"], state, repair["evidence"],
                                detail.get("conflicts")),
        "kind": "observation",
        "run_id": run_id,
        "subject": dict(repair["subject"]),
        "action": action,
        "request_id": request_id,
        "idempotency": idempotency,
        "state": state,
        "evidence_ref": _authority_ref(repair["evidence"]),
        "observed_at": _observed_at_for(intent, snapshot),
        "actor": "rule",
        "detail": detail,
    }
    return record


def _gap_record(run_id, source, snapshot):
    reason = source.get("reason", "")
    record = {
        "schema_version": SCHEMA_VERSION,
        "record_id": _stable_id(run_id, "gap", source["source"], reason,
                                snapshot["observed_at"]),
        "kind": "gap",
        "run_id": run_id,
        "subject": {},
        "action": "progress",
        "request_id": "",
        "idempotency": "none",
        "state": "observed",
        "evidence_ref": source["source"],
        "observed_at": snapshot["observed_at"],
        "actor": "rule",
        "detail": {"repair": "gap", "source": source["source"], "reason": reason},
    }
    return record


def _pending_record(run_id, decision, intent, snapshot):
    request_id = decision.get("request_id") or ""
    idempotency = intent["idempotency"] if intent is not None else "none"
    if not request_id and idempotency in KEYED_IDEMPOTENCIES:
        idempotency = "none"
    action = decision.get("action") or (intent["action"] if intent else "progress")
    record = {
        "schema_version": SCHEMA_VERSION,
        "record_id": _stable_id(run_id, "pending", decision["class"], action,
                                request_id, decision["subject"], decision["reason"]),
        "kind": "pending",
        "run_id": run_id,
        "subject": dict(decision.get("subject") or {}),
        "action": action,
        "request_id": request_id,
        "idempotency": idempotency,
        "state": "pending",
        "evidence_ref": _authority_ref(decision["evidence"]),
        "observed_at": _observed_at_for(intent, snapshot),
        "actor": "rule",
        "detail": {
            "repair": "pending",
            "decision_class": decision["class"],
            "reason": decision["reason"],
            "evidence": decision["evidence"],
        },
    }
    return record


def _closeout_detail(report, report_path):
    """A bounded summary: every classification and its evidence references."""
    classifications = []
    for outcome in report["classifications"]:
        item = {
            "classification": outcome["classification"],
            "action": outcome["action"],
            "request_id": outcome["request_id"],
            "window": outcome["window"],
            "evidence_refs": [evidence["ref"] for evidence in outcome["evidence"]],
            "repair_kinds": [repair["kind"] for repair in outcome["repairs"]],
        }
        if outcome.get("decision"):
            item["decision_class"] = outcome["decision"]["class"]
        classifications.append(item)
    repair_kinds = {}
    for repair in report["repairs"]:
        repair_kinds[repair["kind"]] = repair_kinds.get(repair["kind"], 0) + 1
    detail = {
        "counts": report["counts"],
        "classifications": classifications,
        "decisions": report["decisions"],
        "unreadable": report["unreadable"],
        "repair_kinds": repair_kinds,
        "report_path": str(report_path),
        "replays": report.get("replays", []),
    }
    if len(canonical_json(detail)) <= MAX_RECORD_BYTES - 2048:
        return detail
    for item in detail["classifications"]:
        item.pop("evidence_refs", None)
    detail["replays"] = [
        {key: replay.get(key) for key in ("request_id", "action", "state")}
        for replay in detail["replays"]
    ]
    if len(canonical_json(detail)) <= MAX_RECORD_BYTES - 2048:
        return detail
    detail["classifications"] = [
        {"classification": item["classification"], "action": item["action"]}
        for item in detail["classifications"]
    ]
    detail["decisions"] = [
        {"class": decision["class"], "reason": decision["reason"]}
        for decision in detail["decisions"]
    ]
    detail["truncated"] = True
    return detail


def _stored_report(closeout, run_dir):
    """The report an earlier reconciliation wrote for this identity, if readable.

    Returning the stored report makes a repeated reconciliation print exactly the
    same result instead of a report whose counts drifted once its own repairs
    landed. A path outside the Run's ``reports/`` directory, a symlink, an
    unreadable file or an invalid report all fall back to a fresh rendering.
    """
    detail = closeout.get("detail")
    raw = detail.get("report_path") if isinstance(detail, dict) else None
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(raw)
    reports = run_dir / REPORTS_DIR
    if path.is_symlink() or path.parent != reports or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        validate_report(data)
    except ValueError:
        return None
    return data


def _closeout_record(report, snapshot, report_path, source_digest):
    detail = _closeout_detail(report, report_path)
    record = {
        "schema_version": SCHEMA_VERSION,
        "record_id": _stable_id(report["run_id"], "closeout", source_digest),
        "kind": "closeout",
        "run_id": report["run_id"],
        "subject": {},
        "action": "closeout",
        "request_id": "",
        "idempotency": "none",
        "state": "completed",
        "evidence_ref": str(report_path),
        "observed_at": snapshot["observed_at"],
        "actor": "rule",
        "detail": detail,
    }
    return record


def _refresh_report(report):
    report["repairs"] = [repair for outcome in report["classifications"]
                         for repair in outcome.get("repairs", [])]
    report["decisions"] = [outcome["decision"] for outcome in report["classifications"]
                           if outcome.get("decision")]


def _apply_replays(report, authority, records_by_id, enabled):
    """Execute only pending joinable replays; every other outcome is a decision."""
    executed = []
    for outcome in report["classifications"]:
        for repair in list(outcome.get("repairs", [])):
            if repair["kind"] != "replay":
                continue
            intent = records_by_id.get(outcome["intent_record_id"]) \
                if outcome["intent_record_id"] else None
            evidence = repair["evidence"]
            if not enabled:
                outcome["repairs"].remove(repair)
                if outcome.get("decision") is None:
                    outcome["decision"] = _decision(
                        "replay_disabled",
                        "replay was disabled for this reconciliation",
                        action=repair["action"], request_id=repair["request_id"],
                        subject=repair["subject"], evidence=evidence)
                continue
            if not hasattr(authority, "replay"):
                outcome["repairs"].remove(repair)
                if outcome.get("decision") is None:
                    outcome["decision"] = _decision(
                        "missing_replay_adapter",
                        "no replay adapter was injected; the action was not repeated",
                        action=repair["action"], request_id=repair["request_id"],
                        subject=repair["subject"], evidence=evidence)
                continue
            command = repair.get("command")
            if not isinstance(command, list) or not command \
                    or not all(isinstance(part, str) and part for part in command):
                outcome["repairs"].remove(repair)
                if outcome.get("decision") is None:
                    outcome["decision"] = _decision(
                        "missing_replay_command",
                        "the intent did not record a command to replay",
                        action=repair["action"], request_id=repair["request_id"],
                        subject=repair["subject"], evidence=evidence)
                continue
            try:
                response = authority.replay(repair["request_id"], repair["action"],
                                            repair["subject"], command)
            except Exception as error:  # an adapter failure is an unknown outcome
                response = {"state": "unknown", "reason": str(error)}
            if not isinstance(response, dict):
                response = {"state": "unknown", "reason": "unreadable replay response"}
            state = response.get("state")
            if state not in STATES:
                state = "unknown"
            executed.append({
                "request_id": repair["request_id"],
                "action": repair["action"],
                "state": state,
                "reason": response.get("reason", ""),
                "evidence": evidence,
            })
            outcome["repairs"].remove(repair)
            if state == "completed":
                outcome["repairs"].append(_repair(
                    "late_result", action=repair["action"],
                    request_id=repair["request_id"], subject=repair["subject"],
                    state="completed", evidence=evidence, receipt=response))
            elif outcome.get("decision") is None:
                outcome["decision"] = _decision(
                    "replay_outcome_unknown",
                    "the replay did not prove its outcome; read authority again",
                    action=repair["action"], request_id=repair["request_id"],
                    subject=repair["subject"], evidence=evidence)
    return executed


def _generated_records(report, records_by_id, snapshot, report_path):
    generated = []
    for outcome in report["classifications"]:
        intent = records_by_id.get(outcome["intent_record_id"]) \
            if outcome["intent_record_id"] else None
        for repair in outcome["repairs"]:
            if repair["kind"] == "late_result":
                generated.append(_late_result_record(
                    report["run_id"], repair, intent, snapshot))
            elif repair["kind"] in ("late_observation", "projection",
                                    "divergent_marker"):
                generated.append(_observation_record(
                    report["run_id"], repair, intent, snapshot))
            elif repair["kind"] == "pending":
                generated.append(_pending_record(
                    report["run_id"], repair, intent, snapshot))
        if outcome.get("decision"):
            generated.append(_pending_record(
                report["run_id"], outcome["decision"], intent, snapshot))
    for source in report["unreadable"]:
        generated.append(_gap_record(report["run_id"], source, snapshot))
    return generated


def reconcile(run_id, authority, home=None, replay=True):
    """Gather authority, classify, apply allowed repairs and write a closeout.

    The ledger is read once, without its lock, so appends can continue: the read is
    a consistent prefix. No Orca or host adapter is called while the per-Run lock
    is held; the lock is taken only by the short appends after every authority
    call has returned. Reconciliation never mutates the PR host, the tracker or
    git; its only side effect beyond ledger appends is a same-key replay of a
    receipt that says ``pending`` for a joinable action.
    """
    _portable_identifier(run_id, "run_id")
    records, _corrupt = read_report(run_id, home=home)
    if hasattr(authority, "snapshot"):
        snapshot = authority.snapshot(run_id, records)
    else:
        snapshot = copy.deepcopy(authority)
    snapshot = validated_authority(snapshot)
    if snapshot["run_id"] != run_id:
        _invalid("authority snapshot run_id does not match --run-id")
    report = classify(records, snapshot)
    records_by_id = {record["record_id"]: record for record in records}
    root = home_path(home)
    run_dir = root / RUNS_DIR / run_id

    source_digest = _source_digest(snapshot, records)
    closeout_id = _stable_id(run_id, "closeout", source_digest)
    existing_closeout = records_by_id.get(closeout_id)
    if existing_closeout is not None:
        # This authority read already produced a closeout for the same source ledger.
        # Never replay again and never append a second closeout for the same read.
        stored = _stored_report(existing_closeout, run_dir)
        if stored is None:
            stored = report
            stored["replays"] = []
        stored["closeout"] = {
            "record_id": closeout_id,
            "report_path": existing_closeout["detail"].get("report_path", ""),
        }
        validate_report(stored)
        return stored

    report["replays"] = _apply_replays(report, authority, records_by_id, replay)
    _refresh_report(report)

    _ensure_directory(run_dir)
    report_path = _write_report_file(run_dir, report, source_digest)

    existing = set(records_by_id)
    for record in _generated_records(report, records_by_id, snapshot, report_path):
        if record["record_id"] in existing:
            continue
        _append(record, home=root)
        existing.add(record["record_id"])

    closeout = _closeout_record(report, snapshot, report_path, source_digest)
    if closeout["record_id"] not in existing:
        _append(closeout, home=root)
    report["closeout"] = {
        "record_id": closeout["record_id"],
        "report_path": str(report_path),
    }
    validate_report(report)
    return report


def render_report(report):
    """A human-readable rendering of a reconciliation report."""
    lines = [
        "Ledger reconciliation for " + report["run_id"],
        "Authority read at " + report["observed_at"],
        "Sources: " + ", ".join(
            source + "=" + state
            for source, state in sorted(report["authority_sources"].items())),
        "Counts: " + ", ".join(
            name + "=" + str(report["counts"].get(name, 0))
            for name in CLASSIFICATIONS),
    ]
    for outcome in report["classifications"]:
        action = outcome.get("action") or "no action"
        label = outcome["classification"] + " " + action
        if outcome.get("window"):
            label += " [" + outcome["window"] + "]"
        lines.append("- " + label)
        for evidence in outcome["evidence"]:
            lines.append("    evidence: " + evidence["source"] + " "
                         + evidence["ref"] + " (" + evidence["state"] + ")")
        if outcome.get("repairs"):
            lines.append("    repairs: " + ", ".join(
                repair["kind"] for repair in outcome["repairs"]))
        if outcome.get("decision"):
            lines.append("    decision: " + outcome["decision"]["class"]
                         + " — " + outcome["decision"]["reason"])
    if report["unreadable"]:
        lines.append("Unreadable sources:")
        for source in report["unreadable"]:
            lines.append("- " + source["source"] + ": " + source["reason"])
    if report.get("replays"):
        lines.append("Replays executed:")
        for replay in report["replays"]:
            lines.append("- " + replay["action"] + " request " + replay["request_id"]
                         + " -> " + replay["state"])
    if report.get("closeout"):
        lines.append("Closeout record: " + report["closeout"]["record_id"])
        lines.append("Report: " + report["closeout"]["report_path"])
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Live authority adapters
# ---------------------------------------------------------------------------


class RecordedSource:
    """A host/tracker/git adapter serving a recorded authority section.

    The coordinator can hand the CLI a recorded file for the sources it does not
    read itself; the section is copied unchanged into the snapshot and validated
    there. A missing section is not this class's job: ``LiveAuthority`` treats an
    absent adapter as ``unknown``.
    """

    def __init__(self, section):
        self.section = section

    def snapshot(self, run_id, records):
        return copy.deepcopy(self.section)


def _orca_command(orca_cli=None):
    return orca_cli or os.environ.get(CLI_ENV, DEFAULT_CLI)


def _snake_aliases(raw, aliases):
    """Copy a row, adding snake_case aliases for the known camelCase receipt fields.

    Orca receipt casing is not a verified contract. The adapter reads both spellings,
    preserves the raw row, and lets validation drop a field it cannot type.
    """
    row = dict(raw)
    for camel, snake in aliases.items():
        if snake not in row and camel in raw:
            row[snake] = raw[camel]
    return row


def _normalized_worker_row(raw):
    row = _snake_aliases(raw, {
        "taskId": "task_id", "dispatchId": "dispatch_id", "runId": "run_id",
        "workerState": "worker_state",
    })
    attempt = row.get("attempt")
    if isinstance(attempt, str) and attempt.isdigit():
        row["attempt"] = int(attempt)
    if "attempt" in row and (type(row["attempt"]) is not int or row["attempt"] < 1):
        row.pop("attempt")
    for key in ("task_id", "dispatch_id", "run_id", "worker_state", "worktree"):
        if key in row and not isinstance(row[key], str):
            row.pop(key)
    for key in ("settled", "remote"):
        if key in row and not isinstance(row[key], bool):
            row.pop(key)
    return row


def _normalized_task_row(raw):
    row = _snake_aliases(raw, {"taskId": "task_id", "orcaTaskId": "orca_task_id"})
    if "task_id" not in row and isinstance(raw.get("id"), str):
        row["task_id"] = raw["id"]
    if not isinstance(row.get("orca_task_id"), str):
        row["orca_task_id"] = None
    if "task_id" in row and not isinstance(row["task_id"], str):
        row.pop("task_id")
    if not isinstance(row.get("provisional"), bool):
        row.pop("provisional", None)
    return row


class LiveAuthority:
    """Gather a snapshot from the Orca CLI plus injected host/tracker/git adapters.

    The Orca command is resolved from ``ORCA_CLI_COMMAND`` (the same contract the
    inbox loop uses), so tests point it at the shared fake and nothing touches a
    live Orca. Every read failure becomes ``unknown`` with a reason; an empty
    success is never invented. Pagination follows ``page.hasMore`` and
    ``page.nextCursor`` for ``worker-list`` (with ``--include-remote``) and
    ``task-list``; a missing or repeated cursor is an unreadable read.

    ``replay`` is the only mutation: it runs the command recorded on the intent
    with ``--retry-request`` set to the same request id. No merge, tracker close,
    settle/unsettle or retry decision is made here.
    """

    def __init__(self, orca_cli=None, pr_host=None, tracker=None, git=None,
                 timeout=CLI_TIMEOUT_SECONDS, progress=None):
        self.orca_cli = orca_cli
        self.hosts = {"pr_host": pr_host, "tracker": tracker, "git": git}
        self.timeout = timeout
        self.progress = progress

    def _call(self, argv):
        command = _orca_command(self.orca_cli)
        try:
            completed = subprocess.run(
                [command, *argv], capture_output=True, text=True,
                timeout=self.timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return None, str(error)
        if completed.returncode != 0:
            reason = (completed.stderr or "").strip() \
                or "exit " + str(completed.returncode)
            return None, reason
        try:
            envelope = json.loads(completed.stdout)
        except (TypeError, ValueError):
            return None, "the CLI did not print JSON"
        if not isinstance(envelope, dict):
            return None, "the CLI envelope is not an object"
        if envelope.get("ok") is False:
            return None, str(envelope.get("error", "the CLI refused the command"))
        result = envelope.get("result", envelope)
        return (result, "") if isinstance(result, dict) else (None, "the CLI result is not an object")

    def _request_show_one(self, request_id):
        result, error = self._call(
            ["orchestration", "request-show", "--request", request_id])
        if result is None:
            return {"state": "unknown", "reason": "request-show: " + error, "receipt": {}}
        request = result.get("request", result)
        if not isinstance(request, dict):
            return {"state": "unknown", "reason": "request-show returned no request",
                    "receipt": {}}
        state = request.get("state")
        if state not in RECEIPT_STATES:
            return {"state": "unknown", "reason": "request-show returned no usable state",
                    "receipt": {}}
        receipt = request.get("receipt")
        if not isinstance(receipt, dict):
            receipt = {key: value for key, value in request.items()
                       if key not in ("state", "reason")}
        return {"state": state, "reason": str(request.get("reason", "")),
                "receipt": receipt}

    def _request_show(self, records):
        """Read every keyed intent's receipt, not only the ones still open.

        Reading a completed intent's receipt too keeps the snapshot facts stable
        across reconciliations: the second run must not look like a different
        authority read merely because the first run recorded the result.
        """
        entries = {}
        for record in records:
            if record["kind"] != "intent" or not record["request_id"]:
                continue
            if record["request_id"] in entries:
                continue
            entries[record["request_id"]] = self._request_show_one(record["request_id"])
        return entries

    def _page(self, argv, rows_key, normalize):
        rows, cursor, seen = [], "", set()
        for _page_number in range(MAX_PAGES):
            call = list(argv)
            if cursor:
                call.extend(["--cursor", cursor])
            result, error = self._call(call)
            if result is None:
                return _unknown_source(rows_key + ": " + error)
            page_rows = result.get(rows_key)
            if not isinstance(page_rows, list):
                return _unknown_source(rows_key + ": the CLI returned no row list")
            rows.extend(normalize(row) for row in page_rows if isinstance(row, dict))
            page = result.get("page")
            if not isinstance(page, dict):
                page = {}
            if not page.get("hasMore"):
                return _source("ok", "", rows=rows)
            following = page.get("nextCursor")
            if not isinstance(following, str) or not following or following in seen:
                return _unknown_source(rows_key + ": pagination has no usable cursor")
            seen.add(following)
            cursor = following
        return _unknown_source(rows_key + ": pagination exceeded the page limit")

    def _worker_list(self, run_id):
        return self._page(
            ["orchestration", "worker-list", "--run", run_id,
             "--include-remote", "--limit", str(PAGE_LIMIT)],
            "workers", _normalized_worker_row)

    def _task_list(self, run_id):
        return self._page(
            ["orchestration", "task-list", "--run", run_id,
             "--limit", str(PAGE_LIMIT)],
            "tasks", _normalized_task_row)

    def _worker_show(self, records, workers):
        dispatch_ids = []
        for record in records:
            dispatch_id = record["subject"].get("dispatch_id")
            if dispatch_id and dispatch_id not in dispatch_ids:
                dispatch_ids.append(dispatch_id)
        for row in workers.get("rows", []):
            dispatch_id = row.get("dispatch_id")
            if dispatch_id and dispatch_id not in dispatch_ids:
                dispatch_ids.append(dispatch_id)
        entries = {}
        for dispatch_id in dispatch_ids:
            result, error = self._call(
                ["orchestration", "worker-show", "--dispatch", dispatch_id])
            if result is None:
                entries[dispatch_id] = {
                    "state": "unknown", "reason": "worker-show: " + error, "worker": {}}
                continue
            worker = result.get("worker", result)
            if isinstance(worker, dict) and isinstance(worker.get("worker"), dict):
                worker = worker["worker"]
            if not isinstance(worker, dict):
                entries[dispatch_id] = {
                    "state": "unknown", "reason": "worker-show returned no worker",
                    "worker": {}}
                continue
            entries[dispatch_id] = {
                "state": "ok", "reason": "",
                "worker": _normalized_worker_row(worker),
            }
        return entries

    def _host(self, name, run_id, records):
        adapter = self.hosts.get(name)
        if adapter is None:
            return _unknown_source(name + " adapter not provided")
        try:
            section = adapter.snapshot(run_id, records)
        except Exception as error:  # an injected adapter failure is unreadable
            return _unknown_source(name + ": " + str(error))
        if not isinstance(section, dict):
            return _unknown_source(name + ": the adapter returned no section")
        section = copy.deepcopy(section)
        section.setdefault("state", "unknown")
        section.setdefault("reason", "")
        return section

    def snapshot(self, run_id, records):
        observed_at = _now_instant().replace(microsecond=0).isoformat().replace("+00:00", "Z")
        request_show = self._request_show(records)
        workers = self._worker_list(run_id)
        worker_show = self._worker_show(records, workers)
        tasks = self._task_list(run_id)
        snapshot = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "observed_at": observed_at,
            "orca": {
                "request_show": request_show,
                "workers": workers,
                "worker_show": worker_show,
                "tasks": tasks,
            },
            "pr_host": self._host("pr_host", run_id, records),
            "tracker": self._host("tracker", run_id, records),
            "git": self._host("git", run_id, records),
        }
        if self.progress is not None:
            snapshot["progress"] = copy.deepcopy(self.progress)
        return snapshot

    def replay(self, request_id, action, subject, command):
        argv = list(command or [])
        if not argv or argv[0] != "orchestration":
            return {"state": "unknown",
                    "reason": "the recorded command is not an orchestration command"}
        cleaned, skip = [], False
        for part in argv:
            if skip:
                skip = False
                continue
            if part == "--retry-request":
                skip = True
                continue
            cleaned.append(part)
        cleaned.extend(["--retry-request", request_id])
        result, error = self._call(cleaned)
        if result is None:
            return {"state": "unknown", "reason": error}
        state = result.get("state")
        if state not in STATES:
            nested = result.get("request")
            state = nested.get("state") if isinstance(nested, dict) else None
        if state not in STATES:
            state = "unknown"
        response = dict(result)
        response["state"] = state
        return response


def _load_authority_file(path):
    """Read a recorded host/tracker/git section file for ``ledger reconcile``."""
    try:
        data = json.loads(Path(path).read_text())
    except OSError as error:
        _invalid("cannot read authority file: " + str(error))
    except ValueError as error:
        _invalid("authority file is not valid JSON: " + str(error))
    if not isinstance(data, dict):
        _invalid("authority file must hold a JSON object")
    unknown = sorted(set(data) - {"schema_version", "pr_host", "tracker", "git",
                                  "progress"})
    if unknown:
        _invalid("authority file may only supply pr_host, tracker, git and progress; "
                 "unknown: " + ", ".join(unknown))
    return data


def _load_progress_file(path):
    """Read the progress snapshot's Run binding for ``ledger reconcile``.

    Only the binding is consumed, but an unreadable file is an unreadable source, not an
    empty successful read: it becomes ``unknown`` with the reason and proves nothing.
    """
    try:
        run = json.loads(Path(path).read_text())
    except (OSError, ValueError) as error:
        return {"state": "unknown",
                "reason": "cannot read progress snapshot: " + str(error),
                "orca_run_id": None}
    if not isinstance(run, dict):
        return {"state": "unknown",
                "reason": "progress snapshot must hold a JSON object",
                "orca_run_id": None}
    progress_run = run.get("run")
    if not isinstance(progress_run, dict):
        return {"state": "unknown",
                "reason": "progress snapshot carries no run object",
                "orca_run_id": None}
    return {"state": "ok", "reason": "",
            "orca_run_id": progress_run.get("orca_run_id")}


def _now_instant():
    return datetime.now(timezone.utc)


def _subject_filters(items):
    filters = {}
    for item in items:
        if "=" not in item:
            _invalid("--subject expects KEY=VALUE")
        key, value = item.split("=", 1)
        if key not in SUBJECT_KEYS:
            _invalid("unknown subject filter: " + key)
        filters[key] = value
    return filters


def _report_corrupt(run_id, corrupt):
    for item in corrupt:
        print(
            "Ledger warning: " + run_id + " ledger line " + str(item["line"])
            + " is corrupt and preserved (" + item["error"] + ")",
            file=sys.stderr,
        )


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ledger.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    append_parser = sub.add_parser("append", help="validate, deduplicate and fsync one record")
    append_parser.add_argument("--run-id", required=True)
    append_parser.add_argument("--record", required=True, help="JSON object, or - for stdin")
    show_parser = sub.add_parser("show", help="print complete records, newest last")
    show_parser.add_argument("--run-id", required=True)
    show_parser.add_argument("--kind", choices=KINDS)
    show_parser.add_argument("--state", choices=STATES)
    show_parser.add_argument("--action", choices=ACTIONS)
    show_parser.add_argument(
        "--subject", action="append", default=[], metavar="KEY=VALUE",
        help="subject field filter; repeat to require every pair",
    )
    summary_parser = sub.add_parser("summary", help="counts per kind/state and open items")
    summary_parser.add_argument("--run-id", required=True)
    reconcile_parser = sub.add_parser(
        "reconcile", help="classify against live authority and apply allowed repairs")
    reconcile_parser.add_argument("--run-id", required=True)
    reconcile_parser.add_argument(
        "--authority", metavar="FILE",
        help="recorded authority file supplying pr_host, tracker and git sections")
    reconcile_parser.add_argument(
        "--progress", metavar="FILE",
        help="progress snapshot supplying the Run binding")
    reconcile_parser.add_argument(
        "--no-replay", action="store_true",
        help="never repeat a pending joinable action; record a decision instead")
    reconcile_parser.add_argument(
        "--human", action="store_true", help="print the human-readable report")
    args = parser.parse_args(argv)

    try:
        home = home_path(target_repo=_inferred_target_repository())
        if args.command == "append":
            raw = sys.stdin.read(MAX_RECORD_BYTES + 1) if args.record == "-" else args.record
            if len(raw) > MAX_RECORD_BYTES:
                _invalid("record exceeds 64 KiB")
            normalized = validated(json.loads(raw))
            if normalized["run_id"] != args.run_id:
                _invalid("record run_id does not match --run-id")
            record_id, appended = _append(normalized, home=home)
            print(json.dumps({
                "schema_version": SCHEMA_VERSION,
                "run_id": args.run_id,
                "record_id": record_id,
                "appended": appended,
            }))
        elif args.command == "show":
            filters = {
                "kind": args.kind,
                "state": args.state,
                "action": args.action,
                "subject": _subject_filters(args.subject),
            }
            records, corrupt = read_report(args.run_id, filters, home=home)
            _report_corrupt(args.run_id, corrupt)
            print(json.dumps(records, ensure_ascii=False))
        elif args.command == "summary":
            records, corrupt = read_report(args.run_id, home=home)
            _report_corrupt(args.run_id, corrupt)
            print(json.dumps(_summarize(args.run_id, records, corrupt), ensure_ascii=False))
        else:
            recorded = _load_authority_file(args.authority) if args.authority else {}
            progress = (_load_progress_file(args.progress) if args.progress
                        else recorded.get("progress"))
            adapter = LiveAuthority(
                pr_host=RecordedSource(recorded["pr_host"])
                if "pr_host" in recorded else None,
                tracker=RecordedSource(recorded["tracker"])
                if "tracker" in recorded else None,
                git=RecordedSource(recorded["git"])
                if "git" in recorded else None,
                progress=progress,
            )
            _records, corrupt = read_report(args.run_id, home=home)
            _report_corrupt(args.run_id, corrupt)
            report = reconcile(args.run_id, adapter, home=home,
                               replay=not args.no_replay)
            if args.human:
                sys.stdout.write(render_report(report))
            else:
                print(json.dumps(report, ensure_ascii=False))
    except (OSError, TypeError, ValueError) as error:
        print("Ledger error: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
