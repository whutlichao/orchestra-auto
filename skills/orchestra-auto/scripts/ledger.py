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
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import re
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


def _invalid(message):
    raise ValueError(message)


def _canonical(record):
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
    if len(_canonical(normalized)) > MAX_RECORD_BYTES:
        _invalid("record exceeds 64 KiB")
    return normalized


def validate_record(record):
    """Validate a schema-v1 record; raise ``ValueError`` when it is invalid."""
    validated(record)


def normalize(record):
    """Return the canonical JSON text used to compare re-appended records."""
    return _canonical(validated(record))


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
    stamp = _now().strftime("%Y%m%dT%H%M%S%fZ")
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
    cutoff = (now or _now()) - timedelta(days=days)

    archived = {record["record_id"] for record in records if _observed_at(record) < cutoff}
    sizes = {record["record_id"]: len(_canonical(record).encode("utf-8")) + 1
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
        b"".join((_canonical(record) + "\n").encode("utf-8") for record in moved),
    )
    if corrupt or partial:
        _write_archive(
            run_dir, "corrupt",
            b"".join(item["raw"] + b"\n" for item in corrupt) + partial,
        )
    _replace_active(active, b"".join((_canonical(record) + "\n").encode("utf-8") for record in kept))


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
            if _canonical(candidate) == _canonical(normalized):
                return normalized["record_id"], False
            _invalid("record_id already recorded with a different payload")

        line = (_canonical(normalized) + "\n").encode("utf-8")
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


def _now():
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
        else:
            records, corrupt = read_report(args.run_id, home=home)
            _report_corrupt(args.run_id, corrupt)
            print(json.dumps(_summarize(args.run_id, records, corrupt), ensure_ascii=False))
    except (OSError, TypeError, ValueError) as error:
        print("Ledger error: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
