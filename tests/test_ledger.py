#!/usr/bin/env python3
"""Behaviour tests for ``ledger.py``, the durable coordination ledger.

The declared seams are deliberate:

* **CLI end to end** for storage behaviour — append, read, filters, summary, retention,
  permissions, path refusal, exit codes and concurrency. These run the real script as a
  subprocess against a temporary ledger home outside every repository, so the assertions
  are about the JSON that went in, the files that came out and the process exit code.
* **Module import** for pure computation — schema-v1 validation, normalisation, the path
  policy that needs an explicit target repository, and the crash-window classifier. The
  module is loaded by path with the same ``importlib`` loader style the inbox tests use;
  the script is never turned into a package and no CLI exists only for tests.
* **CLI + shared fake Orca** for reconciliation — ``ledger reconcile`` runs as a subprocess
  with ``ORCA_CLI_COMMAND`` pointed at ``tests/fake_orca.py``'s generated shim. PR-host,
  tracker and git facts are injected adapters or a recorded authority file. The fake is
  reused by later workstreams; it records command order, serves paginated worker and task
  lists, and answers the joinable mutations.

Nothing here touches the network, real Orca, credentials, or ``~/.orca``: every fixture
gets a temporary home directory and a temporary ``ORCA_LEDGER_HOME``. Receipt shapes in
the fixtures are not claims about a live Orca; a field the design has not verified stays a
fixture and is recorded as ``unknown`` rather than asserted as a stable contract.
"""

import fcntl
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIRS = (
    REPO_ROOT / "skills" / "orchestra-auto",
    REPO_ROOT / "skills" / "orchestra-auto-zh",
)
SCRIPT_RELATIVE = Path("scripts") / "ledger.py"
PROGRESS_RELATIVE = Path("scripts") / "progress.py"
RUN_ID = "run_alpha"


def utc_now(days_ago=0):
    """An RFC3339 UTC instant relative to the test run, stable across calendar dates."""
    moment = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def make_record(**overrides):
    """A complete schema-v1 record; overrides replace one field at a time."""
    record = {
        "schema_version": 1,
        "record_id": str(uuid.uuid4()),
        "kind": "intent",
        "run_id": RUN_ID,
        "subject": {"task_id": "task-1", "attempt": 1},
        "action": "worker-start",
        "request_id": str(uuid.uuid4()),
        "idempotency": "joinable",
        "state": "intent",
        "evidence_ref": "",
        "observed_at": utc_now(),
        "actor": "coordinator",
        "detail": {"expected_effect": "dispatch"},
    }
    record.update(overrides)
    return record


def load_ledger(skill_dir):
    """Import a skill's ``ledger.py`` by path, without touching ``sys.path``."""
    path = skill_dir / SCRIPT_RELATIVE
    spec = importlib.util.spec_from_file_location(
        "ledger_" + skill_dir.name.replace("-", "_"), path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot build an import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_fake_orca():
    """Import the shared fake harness whether or not ``tests`` is a package here.

    ``python3 -m unittest tests.test_ledger`` imports this module as ``tests.test_ledger``,
    while ``discover -s tests`` imports it as ``test_ledger``; the harness module is
    reachable under a different name in each case. Loading it by path keeps both commands
    working without a test-only package shim.
    """
    path = Path(__file__).resolve().parent / "fake_orca.py"
    spec = importlib.util.spec_from_file_location("fake_orca", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot build an import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FAKE = load_fake_orca()


def orca_section(**overrides):
    """A readable Orca authority section with every collection present and empty."""
    section = {
        "request_show": {},
        "workers": {"state": "ok", "reason": "", "rows": []},
        "worker_show": {},
        "tasks": {"state": "ok", "reason": "", "rows": []},
    }
    section.update(overrides)
    return section


def authority(orca=None, pr_host=None, tracker=None, git=None, progress=None,
              **overrides):
    """A complete authority snapshot fixture, readable by default."""
    snapshot = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "observed_at": utc_now(),
        "orca": orca_section() if orca is None else orca,
        "pr_host": {"state": "ok", "reason": "", "pull_requests": []}
        if pr_host is None else pr_host,
        "tracker": {"state": "ok", "reason": "", "tickets": []}
        if tracker is None else tracker,
        "git": {"state": "ok", "reason": "", "ancestry": []}
        if git is None else git,
    }
    if progress is not None:
        snapshot["progress"] = progress
    snapshot.update(overrides)
    return snapshot


def worker_row(task_id="task-1", dispatch_id="dispatch-1", attempt=1, run_id=RUN_ID,
               settled=True, remote=False, worker_state="ready"):
    """One normalized ``worker-list`` row (a fixture, not an Orca contract)."""
    return {
        "task_id": task_id,
        "dispatch_id": dispatch_id,
        "attempt": attempt,
        "run_id": run_id,
        "settled": settled,
        "remote": remote,
        "worker_state": worker_state,
    }


def task_row(task_id="task-1", orca_task_id="task-1", provisional=False):
    return {"task_id": task_id, "orca_task_id": orca_task_id, "provisional": provisional}


def pr_row(pr=12, head="task-1", head_sha="a" * 40, state="OPEN", merge_sha=None,
           run_id=RUN_ID):
    return {
        "pr": pr,
        "head": head,
        "head_sha": head_sha,
        "state": state,
        "merge_sha": merge_sha,
        "run_id": run_id,
    }


def intent_record(action="worker-start", subject=None, request_id=None,
                  idempotency="joinable", detail=None, **overrides):
    """An intent record; ``request_id=None`` mints one, ``""`` keeps the no-key class."""
    if subject is None:
        subject = {"task_id": "task-1", "attempt": 1}
    if request_id is None:
        request_id = str(uuid.uuid4())
    record = make_record(
        kind="intent", state="intent", action=action, subject=subject,
        request_id=request_id, idempotency=idempotency,
        detail={"expected_effect": action} if detail is None else detail,
    )
    record.update(overrides)
    return record


def result_record(intent, state="completed", detail=None, **overrides):
    """The result that closes ``intent`` under the same key and subject."""
    record = make_record(
        kind="result", state=state, action=intent["action"],
        subject=dict(intent["subject"]), request_id=intent["request_id"],
        idempotency=intent["idempotency"],
        detail={} if detail is None else detail,
    )
    record.update(overrides)
    return record


class LedgerTestCase(unittest.TestCase):
    """A ledger home and a HOME, both inside one temporary directory."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # macOS TMPDIR lives under /var, a symlink to /private/var; the ledger rejects
        # symlinked paths, so fixtures start from the resolved root.
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "ledger-home"
        self.env = {
            **os.environ,
            "HOME": str(self.root / "home"),
            "ORCA_LEDGER_HOME": str(self.home),
        }
        self.env.pop("ORCA_LEDGER_RETENTION_DAYS", None)
        self.env.pop("ORCA_LEDGER_RETENTION_BYTES", None)

    def run_cli(self, *args, env=None, cwd=None, stdin=None):
        return subprocess.run(
            [sys.executable, str(SKILL_DIRS[0] / SCRIPT_RELATIVE), *args],
            env=env or self.env, cwd=cwd or str(self.root),
            input=stdin, text=True, capture_output=True,
        )

    def append(self, record=None, ok=True):
        payload = json.dumps(record or make_record())
        result = self.run_cli("append", "--run-id", RUN_ID, "--record", payload)
        self.assertEqual(result.returncode == 0, ok, result.stderr)
        return result

    def read(self, *filters, ok=True):
        result = self.run_cli("show", "--run-id", RUN_ID, *filters)
        self.assertEqual(result.returncode == 0, ok, result.stderr)
        return json.loads(result.stdout) if ok else result

    def ledger_file(self, run_id=RUN_ID):
        return self.home / "runs" / run_id / "ledger.jsonl"

    def records_on_disk(self, run_id=RUN_ID):
        """Every complete JSON line in the active ledger, bypassing the CLI reader."""
        path = self.ledger_file(run_id)
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line]


class ValidationTests(LedgerTestCase):
    """Pure schema-v1 validation through the module interface."""

    def setUp(self):
        super().setUp()
        self.ledger = load_ledger(SKILL_DIRS[0])

    def assert_invalid(self, **overrides):
        with self.assertRaises(ValueError):
            self.ledger.validate_record(make_record(**overrides))

    def test_a_complete_record_is_accepted_and_normalised(self):
        record = make_record()
        self.ledger.validate_record(record)
        normal = self.ledger.normalize(record)
        self.assertEqual(json.loads(normal), record)
        # Canonical form makes key order irrelevant to idempotency.
        reordered = {key: record[key] for key in reversed(list(record))}
        self.assertEqual(self.ledger.normalize(reordered), normal)

    def test_unknown_enum_values_are_rejected(self):
        for field, value in (
            ("kind", "gossip"),
            ("action", "delete-everything"),
            ("state", "done"),
            ("idempotency", "maybe"),
            ("actor", "robot"),
        ):
            with self.subTest(field=field):
                self.assert_invalid(**{field: value})

    def test_schema_version_is_exactly_one(self):
        for value in (0, 2, "1", True, None):
            with self.subTest(value=value):
                self.assert_invalid(schema_version=value)

    def test_every_schema_field_is_required(self):
        for field in make_record():
            with self.subTest(field=field):
                record = make_record()
                del record[field]
                with self.assertRaises(ValueError):
                    self.ledger.validate_record(record)

    def test_unknown_top_level_fields_are_rejected(self):
        self.assert_invalid(secret="x")

    def test_record_id_and_request_id_are_uuids(self):
        self.assert_invalid(record_id="not-a-uuid")
        self.assert_invalid(request_id="not-a-uuid")
        # Canonical UUIDs are normalised to lower case so a retry matches.
        upper_id = str(uuid.uuid4()).upper()
        normal = json.loads(self.ledger.normalize(make_record(record_id=upper_id)))
        self.assertEqual(normal["record_id"], upper_id.lower())

    def test_joinable_actions_need_a_request_id(self):
        self.assert_invalid(idempotency="joinable", request_id="")
        # A no-key class may leave it empty; a result that only reads back may too.
        self.ledger.validate_record(make_record(
            kind="result", idempotency="readback_only", request_id="", action="merge",
            state="completed"))

    def test_run_id_cannot_escape_its_directory(self):
        for value in ("", "../escape", "a/b", ".", "..", "a\\b", "x" * 129):
            with self.subTest(value=value):
                self.assert_invalid(run_id=value)

    def test_observed_at_must_be_rfc3339_utc(self):
        for value in ("2026-10-29", "2026-10-29T12:00:00", "2026-10-29 12:00:00Z",
                      "2026-10-29T12:00:00+08:00", "yesterday", 123):
            with self.subTest(value=value):
                self.assert_invalid(observed_at=value)
        self.ledger.validate_record(make_record(observed_at="2026-10-29T12:00:00.123456Z"))
        self.ledger.validate_record(make_record(observed_at="2026-10-29T12:00:00+00:00"))

    def test_subject_is_a_closed_object_with_typed_values(self):
        self.assert_invalid(subject=[])
        self.assert_invalid(subject="task-1")
        self.assert_invalid(subject={"repo": "elsewhere"})
        self.assert_invalid(subject={"attempt": 0})
        self.assert_invalid(subject={"attempt": "1"})
        self.ledger.validate_record(make_record(subject={}))
        self.ledger.validate_record(make_record(subject={"pr": 12, "ticket": "31"}))

    def test_detail_must_be_an_object(self):
        self.assert_invalid(detail=[])
        self.assert_invalid(detail="receipt")
        self.ledger.validate_record(make_record(detail={}))

    def test_an_oversized_record_is_rejected(self):
        self.assert_invalid(detail={"note": "x" * (64 * 1024)})


class AppendTests(LedgerTestCase):
    """The public append contract: round-trip, idempotency, conflict, exit codes."""

    def test_append_then_show_round_trips_every_schema_field(self):
        record = make_record(
            kind="result",
            state="completed",
            action="worker-start",
            evidence_ref="evidence/receipt.json",
            detail={"expected_effect": "dispatch", "items": [1, 2],
                    "raw_receipt": {"turnStart": "observed", "dispatchId": "d-1"}},
        )
        result = self.append(record)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["appended"])
        self.assertEqual(payload["record_id"], record["record_id"])
        self.assertEqual(payload["run_id"], RUN_ID)

        shown = self.read()
        self.assertEqual(shown, [record])
        self.assertEqual(set(shown[0]), {
            "schema_version", "record_id", "kind", "run_id", "subject", "action",
            "request_id", "idempotency", "state", "evidence_ref", "observed_at",
            "actor", "detail",
        })

    def test_records_are_returned_in_append_order_newest_last(self):
        first = make_record()
        second = make_record(state="observed")
        third = make_record(kind="observation", state="observed")
        for record in (first, second, third):
            self.append(record)
        self.assertEqual([r["record_id"] for r in self.read()],
                         [first["record_id"], second["record_id"], third["record_id"]])

    def test_identical_reappend_is_a_noop(self):
        record = make_record()
        first = json.loads(self.append(record).stdout)
        second = json.loads(self.append(record).stdout)
        self.assertTrue(first["appended"])
        self.assertFalse(second["appended"])
        self.assertEqual(second["record_id"], record["record_id"])
        self.assertEqual(len(self.records_on_disk()), 1)

    def test_reordered_keys_and_uuids_in_upper_case_are_the_same_record(self):
        record = make_record(detail={"alpha": 1, "beta": 2})
        self.append(record)
        reordered = {key: record[key] for key in reversed(list(record))}
        reordered["detail"] = {"beta": 2, "alpha": 1}
        reordered["record_id"] = record["record_id"].upper()
        reordered["request_id"] = record["request_id"].upper()
        result = json.loads(self.append(reordered).stdout)
        self.assertFalse(result["appended"])
        self.assertEqual(len(self.records_on_disk()), 1)

    def test_same_record_id_with_a_different_payload_is_refused_and_not_appended(self):
        record = make_record()
        self.append(record)
        conflict = dict(record, detail={"expected_effect": "something else"})
        result = self.append(conflict, ok=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("different payload", result.stderr)
        self.assertEqual(self.records_on_disk(), [record])

    def test_append_rejects_a_record_filed_under_another_run(self):
        result = self.run_cli(
            "append", "--run-id", "run_other", "--record", json.dumps(make_record())
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertFalse((self.home / "runs" / "run_other").exists())

    def test_append_reads_the_record_from_stdin(self):
        record = make_record()
        result = self.run_cli(
            "append", "--run-id", RUN_ID, "--record", "-",
            stdin=json.dumps(record),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read(), [record])

    def test_append_creates_the_run_directory_with_mode_0700(self):
        self.append(make_record())
        for directory in (self.home, self.home / "runs", self.home / "runs" / RUN_ID):
            self.assertTrue(directory.is_dir(), directory)
            self.assertEqual(
                stat.S_IMODE(directory.stat().st_mode), 0o700, f"{directory} mode"
            )
        self.assertEqual(stat.S_IMODE(self.ledger_file().stat().st_mode), 0o600)


class CorruptAndPartialLineTests(LedgerTestCase):
    """A crashed append must never hide a complete record or erase a broken one."""

    def write_raw(self, data):
        self.ledger_file().parent.mkdir(parents=True, exist_ok=True)
        with self.ledger_file().open("ab") as output:
            output.write(data)

    def test_reader_discards_a_trailing_partial_line_and_keeps_complete_records(self):
        first, second = make_record(), make_record()
        self.append(first)
        self.append(second)
        self.write_raw(b'{"schema_version":1,"record_id":"half-written')
        result = self.read(ok=True)
        self.assertEqual([record["record_id"] for record in result],
                         [first["record_id"], second["record_id"]])
        self.assertEqual(result, [first, second])

    def test_append_after_a_partial_line_isolates_it_and_keeps_the_new_record(self):
        first, second = make_record(), make_record()
        self.append(first)
        self.write_raw(b'{"schema_version":1,"record_id":"half-written')
        self.append(second)
        result = self.run_cli("show", "--run-id", RUN_ID)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([record["record_id"] for record in json.loads(result.stdout)],
                         [first["record_id"], second["record_id"]])
        self.assertIn("corrupt", result.stderr)
        self.assertIn(b'{"schema_version":1,"record_id":"half-written', self.ledger_file().read_bytes())

    def test_corrupt_complete_line_is_reported_and_preserved(self):
        first, second = make_record(), make_record()
        corrupt = b'{not json at all}\n'
        self.ledger_file().parent.mkdir(parents=True, exist_ok=True)
        self.ledger_file().write_bytes(
            (json.dumps(first) + "\n").encode() + corrupt + (json.dumps(second) + "\n").encode()
        )
        result = self.run_cli("show", "--run-id", RUN_ID)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([record["record_id"] for record in json.loads(result.stdout)],
                         [first["record_id"], second["record_id"]])
        self.assertIn("line 2", result.stderr)
        self.assertIn("corrupt", result.stderr)
        self.assertIn(corrupt, self.ledger_file().read_bytes(),
                      "a corrupt line must never be silently removed by a read")

    def test_valid_json_that_fails_schema_validation_is_reported_as_corrupt(self):
        record = make_record()
        self.append(record)
        self.write_raw(b'{"schema_version":1,"kind":"gossip"}\n')
        result = self.run_cli("show", "--run-id", RUN_ID)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)), 1)
        self.assertIn("corrupt", result.stderr)

    def test_reads_deduplicate_by_record_id(self):
        record = make_record()
        self.append(record)
        self.write_raw((json.dumps(record) + "\n").encode())
        self.assertEqual(self.read(), [record])


class DurabilityTests(LedgerTestCase):
    """Durability is observed, not assumed: fsync calls and the per-Run lock."""

    def test_every_append_fsyncs_the_file_and_the_first_create_fsyncs_its_parent(self):
        ledger = load_ledger(SKILL_DIRS[0])
        calls = []
        real_fsync = os.fsync

        def recording(fd):
            info = os.fstat(fd)
            calls.append((stat.S_ISDIR(info.st_mode), info.st_ino))
            return real_fsync(fd)

        try:
            os.fsync = recording
            ledger.append(make_record(), home=self.home)
        finally:
            os.fsync = real_fsync
        file_inode = self.ledger_file().stat().st_ino
        directory_inode = self.ledger_file().parent.stat().st_ino
        self.assertIn((False, file_inode), calls, "the record's file must be fsynced")
        self.assertIn((True, directory_inode), calls,
                      "the newly created file's parent directory must be fsynced")

        calls.clear()
        try:
            os.fsync = recording
            ledger.append(make_record(state="observed"), home=self.home)
        finally:
            os.fsync = real_fsync
        self.assertIn((False, file_inode), calls, "every append must fsync the file")

    def test_a_second_writer_waits_for_the_per_run_lock(self):
        self.append(make_record())
        lock_path = self.ledger_file().parent / "ledger.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        record = make_record()
        child = subprocess.Popen(
            [sys.executable, str(SKILL_DIRS[0] / SCRIPT_RELATIVE),
             "append", "--run-id", RUN_ID, "--record", json.dumps(record)],
            env=self.env, cwd=str(self.root), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.addCleanup(lambda: child.poll() is None and child.kill())
        time.sleep(0.4)
        self.assertIsNone(child.poll(),
                          "append must block while another writer holds the per-Run lock")
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        output, error = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0, error)
        self.assertTrue(json.loads(output)["appended"])
        self.assertIn(record["record_id"], [r["record_id"] for r in self.read()])

    def test_concurrent_appends_from_two_processes_do_not_interleave_lines(self):
        script = str(SKILL_DIRS[0] / SCRIPT_RELATIVE)
        payloads = {
            index: [json.dumps(make_record(
                detail={"writer": index, "n": number, "note": "x" * 3000}))
                for number in range(12)]
            for index in range(2)
        }
        drivers = []
        for index, payload in payloads.items():
            payload_path = self.root / f"writer-{index}.json"
            payload_path.write_text(json.dumps(payload))
            driver = self.root / f"writer-{index}.py"
            driver.write_text(
                "import json, subprocess, sys\n"
                f"payloads = json.load(open({str(payload_path)!r}))\n"
                "for raw in payloads:\n"
                f"    result = subprocess.run([sys.executable, {script!r}, 'append',\n"
                f"        '--run-id', {RUN_ID!r}, '--record', raw],\n"
                "        capture_output=True, text=True)\n"
                "    if result.returncode != 0:\n"
                "        sys.exit(result.stderr)\n"
            )
            drivers.append(subprocess.Popen(
                [sys.executable, str(driver)], env=self.env, cwd=str(self.root),
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            ))
        for driver in drivers:
            _output, error = driver.communicate(timeout=120)
            self.assertEqual(driver.returncode, 0, error)
        lines = self.ledger_file().read_text().splitlines()
        self.assertEqual(len(lines), 24)
        parsed = [json.loads(line) for line in lines]
        self.assertEqual(len({record["record_id"] for record in parsed}), 24)
        self.assertEqual(len(self.read()), 24)


class RetentionTests(LedgerTestCase):
    """Retention archives; it never deletes a record that is not archived first."""

    def archive_directory(self):
        return self.ledger_file().parent / "archive"

    def archive_files(self):
        return sorted(self.archive_directory().glob("*.jsonl"))

    def archived_records(self):
        records = []
        for path in self.archive_files():
            records.extend(json.loads(line) for line in path.read_text().splitlines()
                           if line.strip())
        return records

    def assert_every_record_accounted_for(self, records):
        active = [record["record_id"] for record in self.records_on_disk()]
        archived = [record["record_id"] for record in self.archived_records()]
        self.assertEqual(sorted(active + archived),
                         sorted(record["record_id"] for record in records))
        self.assertEqual(len(active + archived), len(set(active + archived)))

    def test_records_older_than_thirty_days_move_to_archive(self):
        old = make_record(observed_at=utc_now(days_ago=31))
        fresh = make_record(observed_at=utc_now())
        self.append(old)
        self.append(fresh)
        self.assertEqual([record["record_id"] for record in self.read()],
                         [fresh["record_id"]])
        self.assertIn(old["record_id"],
                      [record["record_id"] for record in self.archived_records()])
        self.assertEqual(len(self.archive_files()), 1)
        self.assert_every_record_accounted_for([old, fresh])

    def test_beyond_the_byte_budget_moves_the_oldest_records_to_archive(self):
        env = {**self.env, "ORCA_LEDGER_RETENTION_BYTES": "4000"}
        records = [make_record(detail={"n": number, "note": "x" * 400})
                   for number in range(24)]
        for record in records:
            result = self.run_cli("append", "--run-id", RUN_ID,
                                  "--record", json.dumps(record), env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
        active = self.records_on_disk()
        self.assertLessEqual(self.ledger_file().stat().st_size, 4000)
        self.assertTrue(active, "the newest records stay active")
        self.assertEqual(active[-1]["record_id"], records[-1]["record_id"])
        self.assertTrue(self.archived_records(), "the budget must force an archive")
        self.assert_every_record_accounted_for(records)

    def test_reappending_an_archived_record_is_still_a_noop(self):
        old = make_record(observed_at=utc_now(days_ago=31))
        self.append(old)
        archive_count = len(self.archive_files())
        result = json.loads(self.append(old).stdout)
        self.assertFalse(result["appended"])
        self.assertEqual(len(self.archive_files()), archive_count)
        self.assertNotIn(old["record_id"],
                         [record["record_id"] for record in self.read()])

    def test_archive_files_are_plain_jsonl(self):
        old = make_record(observed_at=utc_now(days_ago=31))
        self.append(old)
        path = self.archive_files()[0]
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(path.read_text(), json.dumps(old, sort_keys=True,
                                                      ensure_ascii=False,
                                                      separators=(",", ":")) + "\n")


class PathSafetyTests(LedgerTestCase):
    """The ledger lives outside every repository, never through a symlink."""

    def repository(self):
        repo = self.root / "repo"
        repo.mkdir(exist_ok=True)
        (repo / ".git").mkdir(exist_ok=True)
        return repo

    def test_a_home_inside_a_repository_is_refused_and_writes_nothing(self):
        repo = self.repository()
        home = repo / "ledger"
        result = self.run_cli(
            "append", "--run-id", RUN_ID, "--record", json.dumps(make_record()),
            env={**self.env, "ORCA_LEDGER_HOME": str(home)},
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("outside every repository", result.stderr)
        self.assertFalse(home.exists())

    def test_a_home_that_is_an_ancestor_of_the_target_repository_is_refused(self):
        repo = self.repository()
        result = self.run_cli(
            "append", "--run-id", RUN_ID, "--record", json.dumps(make_record()),
            env={**self.env, "ORCA_LEDGER_HOME": str(self.root)},
            cwd=str(repo),
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("ancestor", result.stderr)
        self.assertFalse((self.root / "runs").exists())

    def test_a_symlinked_home_is_refused(self):
        real = self.root / "real-home"
        real.mkdir()
        link = self.root / "linked-home"
        link.symlink_to(real, target_is_directory=True)
        result = self.run_cli(
            "append", "--run-id", RUN_ID, "--record", json.dumps(make_record()),
            env={**self.env, "ORCA_LEDGER_HOME": str(link)},
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("symlink", result.stderr)
        self.assertEqual(list(real.iterdir()), [])

    def test_a_symlinked_ledger_file_is_refused_on_read(self):
        self.append(make_record())
        outside = self.root / "outside.jsonl"
        outside.write_text("")
        path = self.ledger_file()
        path.unlink()
        path.symlink_to(outside)
        result = self.run_cli("show", "--run-id", RUN_ID)
        self.assertEqual(result.returncode, 1)
        self.assertIn("symlink", result.stderr)

    def test_the_module_guard_checks_the_explicit_target_repository(self):
        ledger = load_ledger(SKILL_DIRS[0])
        repo = self.repository()
        with self.assertRaises(ValueError):
            ledger.home_path(str(repo / "ledger"), target_repo=repo)
        with self.assertRaises(ValueError):
            ledger.home_path(str(self.root), target_repo=repo)
        with self.assertRaises(ValueError):
            ledger.home_path(str(repo), target_repo=repo)
        accepted = ledger.home_path(str(self.root / "elsewhere"), target_repo=repo)
        self.assertEqual(accepted, (self.root / "elsewhere").resolve())

    def test_the_default_home_is_orca_ledger_under_the_users_home(self):
        env = {key: value for key, value in self.env.items() if key != "ORCA_LEDGER_HOME"}
        record = make_record()
        result = self.run_cli("append", "--run-id", RUN_ID,
                              "--record", json.dumps(record), env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        default = Path(env["HOME"]) / ".orca" / "ledger" / "runs" / RUN_ID / "ledger.jsonl"
        self.assertTrue(default.is_file())
        self.assertNotIn(str(REPO_ROOT), str(default))


class FilterAndSummaryTests(LedgerTestCase):
    """Reading is a projection: filters narrow, summary counts, nothing mutates."""

    def setUp(self):
        super().setUp()
        self.request = str(uuid.uuid4())
        self.intent = make_record(request_id=self.request, subject={"task_id": "task-1"})
        self.result = make_record(
            kind="result", action="worker-start", state="completed",
            request_id=self.request, subject={"task_id": "task-1"},
        )
        self.second_intent = make_record(
            action="send", subject={"task_id": "task-2"},
        )
        self.pending = make_record(
            kind="pending", action="send", state="pending", subject={"task_id": "task-2"},
        )
        self.gap = make_record(
            kind="gap", action="progress", state="observed",
            idempotency="none", request_id="", subject={"task_id": "task-3"},
        )
        self.decision = make_record(
            kind="decision", action="tracker-close", state="completed",
            idempotency="readback_only", request_id="", subject={"ticket": "31"},
        )
        self.fixture = [self.intent, self.result, self.second_intent,
                        self.pending, self.gap, self.decision]
        for record in self.fixture:
            self.append(record)

    def ids(self, records):
        return [record["record_id"] for record in records]

    def test_show_filters_by_kind_state_action_and_subject(self):
        self.assertEqual(self.ids(self.read("--kind", "intent")),
                         [self.intent["record_id"], self.second_intent["record_id"]])
        self.assertEqual(self.ids(self.read("--state", "completed")),
                         [self.result["record_id"], self.decision["record_id"]])
        self.assertEqual(self.ids(self.read("--action", "send")),
                         [self.second_intent["record_id"], self.pending["record_id"]])
        self.assertEqual(self.ids(self.read("--subject", "task_id=task-1")),
                         [self.intent["record_id"], self.result["record_id"]])
        self.assertEqual(self.ids(self.read("--subject", "ticket=31")),
                         [self.decision["record_id"]])
        self.assertEqual(
            self.ids(self.read("--kind", "intent", "--subject", "task_id=task-2")),
            [self.second_intent["record_id"]],
        )

    def test_summary_counts_per_kind_and_state_and_names_open_items(self):
        result = self.run_cli("summary", "--run-id", RUN_ID)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["run_id"], RUN_ID)
        self.assertEqual(report["records"], 6)
        self.assertEqual(report["kinds"]["intent"], 2)
        self.assertEqual(report["kinds"]["result"], 1)
        self.assertEqual(report["kinds"]["pending"], 1)
        self.assertEqual(report["kinds"]["gap"], 1)
        self.assertEqual(report["kinds"]["decision"], 1)
        self.assertEqual(report["kinds"]["health"], 0)
        self.assertEqual(report["states"]["intent"], 2)
        self.assertEqual(report["states"]["completed"], 2)
        self.assertEqual(report["states"]["pending"], 1)
        self.assertEqual(report["states"]["observed"], 1)
        self.assertEqual(report["open_intents"], [self.second_intent["record_id"]])
        self.assertEqual(report["pending_items"], [self.pending["record_id"]])
        self.assertEqual(report["corrupt_lines"], 0)

    def test_summary_reports_a_preserved_corrupt_line(self):
        self.ledger_file().parent.mkdir(parents=True, exist_ok=True)
        with self.ledger_file().open("ab") as output:
            output.write(b"{broken}\n")
        result = self.run_cli("summary", "--run-id", RUN_ID)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["corrupt_lines"], 1)
        self.assertIn("corrupt", result.stderr)
        self.assertIn(b"{broken}", self.ledger_file().read_bytes())

    def test_summary_of_an_unknown_run_is_empty_not_an_error(self):
        result = self.run_cli("summary", "--run-id", "run_missing")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["records"], 0)
        self.assertEqual(report["open_intents"], [])
        self.assertEqual(report["pending_items"], [])


class ExitCodeTests(LedgerTestCase):
    """0 success, 1 invalid input or I/O, 2 usage — and a failure never claims a write."""

    def test_invalid_input_exits_one_writes_nothing_and_claims_nothing(self):
        payloads = (
            "not json",
            json.dumps({key: value for key, value in make_record().items()
                        if key != "actor"}),
            json.dumps(make_record(kind="gossip")),
            json.dumps(make_record(run_id="run_other")),
        )
        for payload in payloads:
            with self.subTest(payload=payload[:40]):
                result = self.run_cli("append", "--run-id", RUN_ID, "--record", payload)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertFalse(self.home.exists(), "an invalid append writes nothing")

    def test_usage_errors_exit_two(self):
        cases = (
            ("append", "--run-id", RUN_ID),
            ("append", "--record", json.dumps(make_record())),
            ("show",),
            ("summary",),
            ("explode",),
            ("show", "--run-id", RUN_ID, "--kind", "gossip"),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments)
                self.assertEqual(result.returncode, 2, result.stderr)

    def test_an_io_failure_exits_one_without_reporting_the_record_as_written(self):
        blocker = self.root / "home-is-a-file"
        blocker.write_text("not a directory")
        result = self.run_cli(
            "append", "--run-id", RUN_ID, "--record", json.dumps(make_record()),
            env={**self.env, "ORCA_LEDGER_HOME": str(blocker)},
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertNotEqual(result.stderr, "")

    def test_a_schema_filter_that_matches_nothing_is_not_a_usage_error(self):
        self.append(make_record())
        result = self.run_cli("show", "--run-id", RUN_ID, "--subject", "task_id=absent")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [])

    def test_an_unknown_subject_filter_is_invalid_input(self):
        result = self.run_cli("show", "--run-id", RUN_ID, "--subject", "repo=x")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")


class AuthoritySnapshotTests(LedgerTestCase):
    """The authority snapshot is a local contract: unavailable is ``unknown``, never blank."""

    def setUp(self):
        super().setUp()
        self.ledger = load_ledger(SKILL_DIRS[0])

    def test_missing_sources_become_unknown_instead_of_empty_success(self):
        normalized = self.ledger.validated_authority({
            "schema_version": 1,
            "run_id": RUN_ID,
            "observed_at": utc_now(),
        })
        for source in ("pr_host", "tracker", "git"):
            with self.subTest(source=source):
                self.assertEqual(normalized[source]["state"], "unknown")
                self.assertTrue(normalized[source]["reason"])
        self.assertEqual(normalized["orca"]["workers"]["state"], "unknown")
        self.assertEqual(normalized["orca"]["tasks"]["state"], "unknown")
        self.assertEqual(normalized["orca"]["request_show"], {})

    def test_a_malformed_snapshot_is_rejected(self):
        good = authority()
        cases = {
            "not an object": [],
            "wrong schema version": dict(good, schema_version=2),
            "missing observed_at": {key: value for key, value in good.items()
                                    if key != "observed_at"},
            "bad observed_at": dict(good, observed_at="yesterday"),
            "bad run id": dict(good, run_id="../escape"),
            "unknown top-level field": dict(good, guesses=[]),
            "orca not an object": dict(good, orca=[]),
            "bad workers state": authority(orca=orca_section(
                workers={"state": "empty", "reason": "", "rows": []})),
            "workers rows not a list": authority(orca=orca_section(
                workers={"state": "ok", "reason": "", "rows": {}})),
            "bad request-show state": authority(orca=orca_section(
                request_show={"r": {"state": "done"}})),
            "pull requests not a list": authority(
                pr_host={"state": "ok", "reason": "", "pull_requests": {}}),
            "ticket row without an id": authority(
                tracker={"state": "ok", "reason": "", "tickets": [{"state": "closed"}]}),
            "ancestry without a boolean": authority(
                git={"state": "ok", "reason": "", "ancestry": [
                    {"ancestor": "a", "descendant": "b", "result": "yes"}]}),
        }
        for label, snapshot in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    self.ledger.validated_authority(snapshot)

    def test_classify_refuses_records_from_another_run(self):
        snapshot = authority(run_id="run_other")
        with self.assertRaises(ValueError):
            self.ledger.classify([intent_record()], snapshot)


class ClassifierTests(LedgerTestCase):
    """The pure classifier covers matched, orphan_intent, orphan_effect, divergent, unknown."""

    def setUp(self):
        super().setUp()
        self.ledger = load_ledger(SKILL_DIRS[0])

    def classify(self, records, snapshot=None):
        report = self.ledger.classify(records, snapshot or authority())
        self.ledger.validate_report(report)
        return report

    def outcomes(self, report, classification=None):
        found = report["classifications"]
        if classification is not None:
            found = [item for item in found if item["classification"] == classification]
        return found

    def test_a_closed_intent_with_an_authority_effect_is_matched(self):
        intent = intent_record(detail={"expected_effect": "dispatch"})
        result = result_record(intent, detail={"raw_receipt": {"state": "completed"}})
        report = self.classify(
            [intent, result],
            authority(orca=orca_section(workers={
                "state": "ok", "reason": "", "rows": [worker_row()]})),
        )
        matched = self.outcomes(report, "matched")
        self.assertEqual(len(matched), 1)
        evidence = matched[0]["evidence"]
        self.assertIn("ledger:" + RUN_ID + ":" + intent["record_id"],
                      [item["ref"] for item in evidence])
        self.assertIn("ledger:" + RUN_ID + ":" + result["record_id"],
                      [item["ref"] for item in evidence])
        self.assertTrue(any(item["source"] == "orca.worker-list" for item in evidence))
        self.assertEqual(matched[0]["repairs"], [])
        self.assertEqual(report["counts"]["matched"], 1)

    def test_absent_receipt_and_no_authority_effect_is_orphan_intent(self):
        intent = intent_record()
        report = self.classify([intent], authority(orca=orca_section(
            request_show={intent["request_id"]: {"state": "absent"}})))
        found = self.outcomes(report, "orphan_intent")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["window"], "W1")
        self.assertEqual(found[0]["intent_record_id"], intent["record_id"])
        self.assertIn("orca.request-show", [item["source"] for item in found[0]["evidence"]])
        self.assertEqual(found[0]["repairs"], [], "W1 never replays by itself")
        self.assertEqual(found[0]["decision"]["class"], "orphan_intent")

    def test_absent_without_an_intent_is_not_a_judgment(self):
        report = self.classify([], authority(orca=orca_section(
            request_show={"unused-request": {"state": "absent"}})))
        self.assertEqual(report["classifications"], [])
        self.assertEqual(report["decisions"], [])

    def test_an_authority_effect_without_an_intent_is_orphan_effect(self):
        report = self.classify([], authority(orca=orca_section(workers={
            "state": "ok", "reason": "", "rows": [worker_row(remote=True)]})))
        found = self.outcomes(report, "orphan_effect")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["action"], "worker-start")
        self.assertEqual(found[0]["subject"], {"task_id": "task-1",
                                               "dispatch_id": "dispatch-1"})
        self.assertEqual([repair["kind"] for repair in found[0]["repairs"]],
                         ["late_result"])
        self.assertEqual(found[0]["evidence"][0]["source"], "orca.worker-list")

    def test_a_conflicting_attempt_is_divergent(self):
        intent = intent_record(subject={"task_id": "task-1", "attempt": 1})
        result = result_record(intent)
        report = self.classify(
            [intent, result],
            authority(orca=orca_section(workers={
                "state": "ok", "reason": "",
                "rows": [worker_row(attempt=2)]})),
        )
        found = self.outcomes(report, "divergent")
        self.assertEqual(len(found), 1)
        self.assertIn("attempt", [conflict["field"] for conflict in found[0]["conflicts"]])
        self.assertEqual([repair["kind"] for repair in found[0]["repairs"]],
                         ["divergent_marker"])
        self.assertEqual(found[0]["decision"]["class"], "divergent")

    def test_an_unreadable_authority_is_unknown_and_escalates(self):
        intent = intent_record()
        snapshot = authority(orca=orca_section(
            request_show={intent["request_id"]: {
                "state": "unknown", "reason": "request-show timed out"}},
            workers={"state": "unknown", "reason": "worker-list timed out", "rows": []},
        ))
        report = self.classify([intent], snapshot)
        found = self.outcomes(report, "unknown")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["window"], "W3")
        self.assertEqual(found[0]["decision"]["class"], "unreadable_authority")
        self.assertEqual(found[0]["repairs"], [], "unreadable authority is never replayed")

    def test_a_provisional_progress_task_is_never_a_join_key(self):
        intent = intent_record()
        snapshot = authority(orca=orca_section(
            request_show={intent["request_id"]: {"state": "absent"}},
            tasks={"state": "ok", "reason": "", "rows": [
                task_row(task_id="task-1", orca_task_id=None, provisional=True)]},
        ))
        report = self.classify([intent], snapshot)
        self.assertEqual(self.outcomes(report, "matched"), [],
                         "a provisional task must not match a ledger subject")
        self.assertEqual(len(self.outcomes(report, "orphan_intent")), 1)

    def test_after_binding_the_orca_task_id_is_the_join_key(self):
        intent = intent_record()
        snapshot = authority(orca=orca_section(
            request_show={intent["request_id"]: {"state": "absent"}},
            tasks={"state": "ok", "reason": "", "rows": [
                task_row(task_id="local-progress-id", orca_task_id="task-1",
                         provisional=False)]},
        ))
        report = self.classify([intent], snapshot)
        matched = self.outcomes(report, "matched")
        self.assertEqual(len(matched), 1)
        self.assertEqual([repair["kind"] for repair in matched[0]["repairs"]],
                         ["late_result"])

    def test_every_classification_names_its_evidence(self):
        matched_intent = intent_record(subject={"task_id": "task-1", "attempt": 1})
        matched_result = result_record(matched_intent)
        absent_intent = intent_record(subject={"task_id": "task-2", "attempt": 1})
        divergent_intent = intent_record(subject={"task_id": "task-3", "attempt": 1})
        divergent_result = result_record(divergent_intent)
        snapshot = authority(
            orca=orca_section(
                request_show={absent_intent["request_id"]: {"state": "absent"}},
                workers={"state": "ok", "reason": "", "rows": [
                    worker_row(task_id="task-1", attempt=1),
                    worker_row(task_id="task-3", dispatch_id="dispatch-3", attempt=2),
                    worker_row(task_id="task-4", dispatch_id="dispatch-4"),
                ]},
            ),
            tracker={"state": "unknown", "reason": "tracker unreachable", "tickets": []},
        )
        report = self.classify(
            [matched_intent, matched_result, absent_intent,
             divergent_intent, divergent_result], snapshot)
        kinds = {item["classification"] for item in report["classifications"]}
        self.assertEqual(kinds, {"matched", "orphan_intent", "orphan_effect",
                                 "divergent", "unknown"})
        for item in report["classifications"]:
            with self.subTest(classification=item["classification"]):
                self.assertTrue(item["evidence"])
                for evidence in item["evidence"]:
                    self.assertEqual(set(evidence), {"source", "ref", "state"})
                    self.assertTrue(evidence["source"])
                    self.assertTrue(evidence["ref"])


class RequestShowRepairTests(LedgerTestCase):
    """``request-show`` completed / pending / absent each have one repair rule."""

    def setUp(self):
        super().setUp()
        self.ledger = load_ledger(SKILL_DIRS[0])

    def classify(self, records, snapshot):
        report = self.ledger.classify(records, snapshot)
        self.ledger.validate_report(report)
        return report

    def repairs(self, report):
        return [repair["kind"] for repair in report["repairs"]]

    def test_a_completed_receipt_plans_a_late_result_without_repeating_the_action(self):
        intent = intent_record(detail={
            "expected_effect": "dispatch",
            "command": ["orchestration", "worker-start", "--task", "task-1"],
        })
        report = self.classify([intent], authority(orca=orca_section(
            request_show={intent["request_id"]: {
                "state": "completed",
                "receipt": {"state": "completed", "dispatchId": "dispatch-1"},
            }},
            workers={"state": "ok", "reason": "",
                     "rows": [worker_row()]},
        )))
        self.assertEqual(report["counts"]["matched"], 1)
        self.assertIn("late_result", self.repairs(report))
        self.assertNotIn("replay", self.repairs(report),
                         "a completed receipt must never repeat the action")
        self.assertEqual(report["decisions"], [])

    def test_a_pending_joinable_receipt_plans_a_same_key_replay(self):
        intent = intent_record(detail={
            "expected_effect": "dispatch",
            "command": ["orchestration", "worker-start", "--task", "task-1"],
        })
        report = self.classify([intent], authority(orca=orca_section(
            request_show={intent["request_id"]: {"state": "pending"}},
        )))
        self.assertEqual(self.repairs(report), ["replay"])
        replay = report["repairs"][0]
        self.assertEqual(replay["request_id"], intent["request_id"])
        self.assertEqual(replay["command"],
                         ["orchestration", "worker-start", "--task", "task-1"])
        self.assertEqual(report["decisions"], [])

    def test_a_pending_readback_only_action_is_read_never_replayed(self):
        intent = intent_record(action="merge", idempotency="readback_only",
                               subject={"pr": 12}, request_id=str(uuid.uuid4()))
        report = self.classify([intent], authority(
            orca=orca_section(request_show={intent["request_id"]: {"state": "pending"}}),
            pr_host={"state": "ok", "reason": "",
                     "pull_requests": [pr_row(pr=12, state="OPEN")]},
        ))
        self.assertNotIn("replay", self.repairs(report))
        self.assertEqual(self.repairs(report), [])
        self.assertEqual(report["classifications"][0]["classification"], "orphan_intent")
        self.assertEqual(report["decisions"][0]["class"], "merge_replay")

    def test_a_completed_receipt_with_a_conflicting_worker_is_divergent(self):
        intent = intent_record(subject={"task_id": "task-1", "attempt": 1})
        report = self.classify([intent], authority(orca=orca_section(
            request_show={intent["request_id"]: {"state": "completed"}},
            workers={"state": "ok", "reason": "", "rows": [worker_row(attempt=2)]},
        )))
        self.assertEqual(report["counts"]["divergent"], 1)
        self.assertIn("late_result", self.repairs(report))
        self.assertIn("divergent_marker", self.repairs(report))
        self.assertNotIn("replay", self.repairs(report))

    def test_absent_plus_unreadable_authority_escalates_instead_of_replaying(self):
        intent = intent_record(action="send",
                               detail={"expected_effect": "send"})
        report = self.classify([intent], authority(orca=orca_section(
            request_show={intent["request_id"]: {"state": "absent"}},
            messages={"state": "unknown", "reason": "no message authority", "rows": []},
        )))
        self.assertEqual(report["classifications"][0]["classification"], "unknown")
        self.assertEqual(report["decisions"][0]["class"], "unreadable_authority")
        self.assertNotIn("replay", self.repairs(report))


class ForbiddenReplayTests(LedgerTestCase):
    """Merge, tracker close, unproven retry and unsettled release never auto-replay."""

    def setUp(self):
        super().setUp()
        self.ledger = load_ledger(SKILL_DIRS[0])

    def classify(self, records, snapshot):
        return self.ledger.classify(records, snapshot)

    def repair_kinds(self, report):
        return [repair["kind"] for repair in report["repairs"]]

    def decision_classes(self, report):
        return [decision["class"] for decision in report["decisions"]]

    def test_merge_is_never_replayed(self):
        intent = intent_record(action="merge", idempotency="readback_only",
                               subject={"pr": 12}, request_id="")
        report = self.classify([intent], authority(pr_host={
            "state": "ok", "reason": "", "pull_requests": [pr_row(pr=12, state="OPEN")]}))
        self.assertNotIn("replay", self.repair_kinds(report))
        self.assertIn("merge_replay", self.decision_classes(report))

    def test_a_merged_pull_request_becomes_a_late_result_without_a_replay(self):
        intent = intent_record(action="merge", idempotency="readback_only",
                               subject={"pr": 12}, request_id="")
        report = self.classify([intent], authority(pr_host={
            "state": "ok", "reason": "",
            "pull_requests": [pr_row(pr=12, state="MERGED", merge_sha="b" * 40)]}))
        self.assertEqual(report["counts"]["matched"], 1)
        self.assertIn("late_result", self.repair_kinds(report))
        self.assertNotIn("replay", self.repair_kinds(report))

    def test_tracker_close_is_never_replayed(self):
        intent = intent_record(action="tracker-close", idempotency="readback_only",
                               subject={"ticket": "32"}, request_id="")
        report = self.classify([intent], authority(tracker={
            "state": "ok", "reason": "",
            "tickets": [{"ticket": "32", "state": "open"}]}))
        self.assertNotIn("replay", self.repair_kinds(report))
        self.assertIn("tracker_close_replay", self.decision_classes(report))

    def test_a_closed_ticket_becomes_a_late_result_without_a_replay(self):
        intent = intent_record(action="tracker-close", idempotency="readback_only",
                               subject={"ticket": "32"}, request_id="")
        report = self.classify([intent], authority(tracker={
            "state": "ok", "reason": "",
            "tickets": [{"ticket": "32", "state": "closed"}]}))
        self.assertEqual(report["counts"]["matched"], 1)
        self.assertIn("late_result", self.repair_kinds(report))
        self.assertNotIn("replay", self.repair_kinds(report))

    def test_a_failed_result_requires_proof_before_a_retry(self):
        intent = intent_record()
        failed = result_record(intent, state="failed")
        report = self.classify([intent, failed], authority(orca=orca_section(
            workers={"state": "ok", "reason": "", "rows": [worker_row()]})))
        self.assertEqual(report["counts"]["matched"], 1)
        self.assertIn("unproven_retry", self.decision_classes(report))
        self.assertNotIn("replay", self.repair_kinds(report))

    def test_an_unsettled_release_is_a_decision_not_a_replay(self):
        intent = intent_record(action="release", idempotency="replay_safe",
                               detail={"expected_effect": "release"})
        report = self.classify([intent], authority(orca=orca_section(
            request_show={intent["request_id"]: {"state": "pending"}},
            workers={"state": "ok", "reason": "",
                     "rows": [worker_row(settled=False, worker_state="ready")]},
        )))
        self.assertNotIn("replay", self.repair_kinds(report))
        self.assertIn("unsettled_release", self.decision_classes(report))

    def test_a_settled_release_is_replayed_with_the_same_key(self):
        intent = intent_record(action="release", idempotency="replay_safe",
                               detail={"expected_effect": "release"})
        report = self.classify([intent], authority(orca=orca_section(
            request_show={intent["request_id"]: {"state": "pending"}},
            workers={"state": "ok", "reason": "",
                     "rows": [worker_row(settled=True, worker_state="settled")]},
        )))
        self.assertEqual(self.repair_kinds(report), ["replay"])
        self.assertEqual(report["repairs"][0]["request_id"], intent["request_id"])


class ActionMatrixTests(LedgerTestCase):
    """The matrix's uncertain-action rules, exercised through ``classify``."""

    def setUp(self):
        super().setUp()
        self.ledger = load_ledger(SKILL_DIRS[0])

    def classify(self, records, snapshot):
        return self.ledger.classify(records, snapshot)

    def repair_kinds(self, report):
        return [repair["kind"] for repair in report["repairs"]]

    def decisions(self, report):
        return [decision["class"] for decision in report["decisions"]]

    def test_send_with_an_absent_receipt_and_no_reply_is_never_auto_resent(self):
        intent = intent_record(action="send", detail={"expected_effect": "send"})
        report = self.classify([intent], authority(orca=orca_section(
            request_show={intent["request_id"]: {"state": "absent"}},
            messages={"state": "ok", "reason": "", "rows": []},
        )))
        self.assertEqual(report["classifications"][0]["classification"], "orphan_intent")
        self.assertIn("send_reply_no_resend", self.decisions(report))
        self.assertNotIn("replay", self.repair_kinds(report))

    def test_stop_with_a_running_worker_is_a_decision_not_a_retry(self):
        intent = intent_record(action="stop", subject={"task_id": "task-1",
                                                       "dispatch_id": "dispatch-1"})
        report = self.classify([intent], authority(orca=orca_section(
            request_show={intent["request_id"]: {"state": "absent"}},
            workers={"state": "ok", "reason": "",
                     "rows": [worker_row(worker_state="ready")]},
        )))
        self.assertEqual(report["classifications"][0]["classification"], "orphan_intent")
        self.assertIn("stop_abandon_no_retry", self.decisions(report))
        self.assertNotIn("replay", self.repair_kinds(report))

    def test_stop_with_no_worker_row_proves_nothing(self):
        """A vanished row is absence of proof, never proof that the stop happened."""
        intent = intent_record(action="stop", subject={"task_id": "task-1",
                                                       "dispatch_id": "dispatch-1"})
        report = self.classify([intent], authority(orca=orca_section(
            request_show={intent["request_id"]: {"state": "absent"}},
            workers={"state": "ok", "reason": "", "rows": []},
        )))
        self.assertEqual(report["classifications"][0]["classification"], "orphan_intent")
        self.assertIn("stop_abandon_no_retry", self.decisions(report))
        self.assertNotIn("late_result", self.repair_kinds(report))

    def test_a_completed_stop_with_no_worker_row_is_divergent(self):
        """A result claiming a stop the authority cannot prove is a conflict."""
        intent = intent_record(action="stop", subject={"task_id": "task-1",
                                                       "dispatch_id": "dispatch-1"})
        completed = result_record(intent, state="completed")
        report = self.classify([intent, completed], authority(orca=orca_section(
            workers={"state": "ok", "reason": "", "rows": []},
        )))
        self.assertEqual(report["classifications"][0]["classification"], "divergent")
        self.assertEqual(report["classifications"][0]["conflicts"],
                         [{"field": "effect", "ledger": "completed",
                           "authority": "no matching effect"}])

    def test_stop_with_a_terminal_worker_show_and_no_list_row_is_matched(self):
        """worker-show is a proof source of its own: an empty list does not disprove it."""
        intent = intent_record(action="stop", subject={"task_id": "task-1",
                                                       "dispatch_id": "dispatch-1"})
        completed = result_record(intent, state="completed")
        report = self.classify([intent, completed], authority(orca=orca_section(
            workers={"state": "ok", "reason": "", "rows": []},
            worker_show={"dispatch-1": {
                "state": "ok",
                "worker": {"task_id": "task-1", "dispatch_id": "dispatch-1",
                           "worker_state": "stopped"}}},
        )))
        self.assertEqual(report["classifications"][0]["classification"], "matched")

    def test_an_unknown_worker_show_does_not_displace_a_terminal_list_row(self):
        """An unreadable worker-show proves nothing, but worker-list may still prove."""
        intent = intent_record(action="stop", subject={"task_id": "task-1",
                                                       "dispatch_id": "dispatch-1"})
        completed = result_record(intent, state="completed")
        report = self.classify([intent, completed], authority(orca=orca_section(
            workers={"state": "ok", "reason": "",
                     "rows": [worker_row(worker_state="stopped")]},
            worker_show={"dispatch-1": {
                "state": "unknown", "reason": "worker-show failed", "worker": {}}},
        )))
        self.assertEqual(report["classifications"][0]["classification"], "matched")
        self.assertIn("unknown", [item["state"]
                                  for item in report["classifications"][0]["evidence"]])

    def test_a_terminal_worker_show_does_not_override_a_running_list_row(self):
        """The proof union is conservative: every readable proof row must agree.

        worker-show and worker-list are both allowed proof sources, so a stop counts
        only when every proof row is terminal; one source still saying "running" keeps
        the claim unproven.
        """
        intent = intent_record(action="stop", subject={"task_id": "task-1",
                                                       "dispatch_id": "dispatch-1"})
        completed = result_record(intent, state="completed")
        report = self.classify([intent, completed], authority(orca=orca_section(
            workers={"state": "ok", "reason": "",
                     "rows": [worker_row(worker_state="ready")]},
            worker_show={"dispatch-1": {
                "state": "ok",
                "worker": {"task_id": "task-1", "dispatch_id": "dispatch-1",
                           "worker_state": "stopped"}}},
        )))
        self.assertEqual(report["classifications"][0]["classification"], "divergent")
        self.assertEqual(report["classifications"][0]["conflicts"],
                         [{"field": "effect", "ledger": "completed",
                           "authority": "no matching effect"}])

    def test_release_with_no_worker_row_is_unproven(self):
        """A missing row is never a release outcome."""
        intent = intent_record(action="release", idempotency="replay_safe",
                               subject={"task_id": "task-1",
                                        "dispatch_id": "dispatch-1"},
                               detail={"expected_effect": "release"})
        report = self.classify([intent], authority(orca=orca_section(
            request_show={intent["request_id"]: {"state": "absent"}},
            workers={"state": "ok", "reason": "", "rows": []},
        )))
        self.assertEqual(report["classifications"][0]["classification"], "orphan_intent")
        self.assertIn("unsettled_release", self.decisions(report))

    def test_a_completed_release_with_no_worker_row_is_divergent(self):
        intent = intent_record(action="release", idempotency="replay_safe",
                               subject={"task_id": "task-1",
                                        "dispatch_id": "dispatch-1"},
                               detail={"expected_effect": "release"})
        completed = result_record(intent, state="completed")
        report = self.classify([intent, completed], authority(orca=orca_section(
            workers={"state": "ok", "reason": "", "rows": []},
        )))
        self.assertEqual(report["classifications"][0]["classification"], "divergent")
        self.assertEqual(report["classifications"][0]["conflicts"],
                         [{"field": "effect", "ledger": "completed",
                           "authority": "no matching effect"}])

    def test_a_settled_worker_show_proves_a_release_with_an_empty_list(self):
        """The settled flag on a worker-show row is proof, even with no list row."""
        intent = intent_record(action="release", idempotency="replay_safe",
                               subject={"task_id": "task-1",
                                        "dispatch_id": "dispatch-1"},
                               detail={"expected_effect": "release"})
        completed = result_record(intent, state="completed")
        report = self.classify([intent, completed], authority(orca=orca_section(
            workers={"state": "ok", "reason": "", "rows": []},
            worker_show={"dispatch-1": {
                "state": "ok",
                "worker": {"task_id": "task-1", "dispatch_id": "dispatch-1",
                           "settled": True, "worker_state": "ready"}}},
        )))
        self.assertEqual(report["classifications"][0]["classification"], "matched")

    def test_pr_create_adopts_an_existing_pr_instead_of_creating_another(self):
        intent = intent_record(action="pr-create", idempotency="lookup_then_create",
                               detail={"expected_effect": "pull request",
                                       "head": "task-1"})
        report = self.classify([intent], authority(
            orca=orca_section(request_show={intent["request_id"]: {"state": "pending"}}),
            pr_host={"state": "ok", "reason": "",
                     "pull_requests": [pr_row(pr=12, head="task-1", state="OPEN")]},
        ))
        self.assertEqual(report["classifications"][0]["classification"], "matched")
        self.assertIn("late_result", self.repair_kinds(report))
        self.assertNotIn("replay", self.repair_kinds(report))

    def test_pr_create_with_a_readable_absence_may_retry(self):
        intent = intent_record(action="pr-create", idempotency="lookup_then_create",
                               detail={"expected_effect": "pull request",
                                       "head": "task-1"})
        report = self.classify([intent], authority(
            orca=orca_section(request_show={intent["request_id"]: {"state": "pending"}}),
            pr_host={"state": "ok", "reason": "", "pull_requests": []},
        ))
        self.assertIn("replay", self.repair_kinds(report))

    def test_ack_pending_replays_under_the_same_key(self):
        intent = intent_record(action="ack", idempotency="replay_safe",
                               detail={"expected_effect": "ack"})
        report = self.classify([intent], authority(orca=orca_section(
            request_show={intent["request_id"]: {"state": "pending"}},
        )))
        self.assertEqual(self.repair_kinds(report), ["replay"])
        self.assertEqual(report["repairs"][0]["request_id"], intent["request_id"])

    def test_a_worker_start_absent_with_readable_authority_is_not_executed(self):
        intent = intent_record()
        report = self.classify([intent], authority(orca=orca_section(
            request_show={intent["request_id"]: {"state": "absent"}},
            workers={"state": "ok", "reason": "", "rows": []},
            tasks={"state": "ok", "reason": "", "rows": []},
        )))
        self.assertEqual(report["classifications"][0]["classification"], "orphan_intent")
        self.assertEqual(report["classifications"][0]["window"], "W1")
        self.assertNotIn("replay", self.repair_kinds(report))


class RunBindingTests(LedgerTestCase):
    """The progress Run binding and the ledger Run must name the same Run."""

    def setUp(self):
        super().setUp()
        self.ledger = load_ledger(SKILL_DIRS[0])

    def classify(self, progress):
        return self.ledger.classify([], authority(progress=progress))

    def test_a_matching_binding_is_not_divergent(self):
        report = self.classify({"state": "ok", "reason": "",
                                "orca_run_id": RUN_ID})
        self.assertEqual(report["counts"]["divergent"], 0)
        self.assertEqual(report["counts"]["unknown"], 0)

    def test_a_null_binding_is_unbound_not_divergent(self):
        report = self.classify({"state": "ok", "reason": "", "orca_run_id": None})
        self.assertEqual(report["counts"]["divergent"], 0)

    def test_an_absent_section_is_unbound_not_divergent(self):
        report = self.ledger.classify([], authority())
        self.assertEqual(report["counts"]["divergent"], 0)

    def test_an_unreadable_binding_is_not_divergent(self):
        report = self.classify({"state": "unknown", "reason": "unreadable",
                                "orca_run_id": "run_other"})
        self.assertEqual(report["counts"]["divergent"], 0)

    def test_a_mismatched_binding_is_one_run_level_divergence(self):
        report = self.classify({"state": "ok", "reason": "",
                                "orca_run_id": "run_other"})
        self.assertEqual(report["counts"]["divergent"], 1)
        outcome = next(item for item in report["classifications"]
                       if item["classification"] == "divergent")
        self.assertEqual(outcome["window"], "W2")
        self.assertEqual(outcome["action"], "progress")
        self.assertEqual(outcome["conflicts"], [{
            "field": "orca_run_id", "ledger": RUN_ID, "authority": "run_other"}])
        self.assertEqual([repair["kind"] for repair in outcome["repairs"]],
                         ["divergent_marker"])
        self.assertEqual(outcome["decision"]["class"], "run_binding_mismatch")
        self.assertIn("run_other", outcome["decision"]["reason"])

    def test_the_divergent_marker_becomes_a_durable_observation(self):
        """Reconciliation records the mismatch once and a repeat is a no-op."""
        progress_path = self.root / "progress.json"
        progress_path.write_text(json.dumps({
            "schema_version": 2,
            "run": {"id": "run_other", "orca_run_id": "run_other"}}))
        ledger = self.ledger
        progress = ledger._load_progress_file(progress_path)
        self.assertEqual(progress, {"state": "ok", "reason": "",
                                    "orca_run_id": "run_other"})
        report = ledger.reconcile(RUN_ID, {"schema_version": 1, "run_id": RUN_ID,
                                           "observed_at": utc_now(),
                                           "progress": progress}, home=self.home)
        markers = [record for record in self.read()
                   if record["kind"] == "observation"
                   and record["detail"].get("repair") == "divergent_marker"]
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0]["state"], "divergent")
        self.assertEqual(markers[0]["detail"]["conflicts"], [{
            "field": "orca_run_id", "ledger": RUN_ID, "authority": "run_other"}])
        again = ledger.reconcile(RUN_ID, {"schema_version": 1, "run_id": RUN_ID,
                                          "observed_at": utc_now(),
                                          "progress": progress}, home=self.home)
        self.assertEqual(again["closeout"], report["closeout"])
        self.assertEqual(len([record for record in self.read()
                              if record["kind"] == "observation"
                              and record["detail"].get("repair")
                              == "divergent_marker"]), 1)


class ReconcileTests(LedgerTestCase):
    """``reconcile`` reads a prefix, applies allowed repairs and writes a closeout."""

    def setUp(self):
        super().setUp()
        self.ledger = load_ledger(SKILL_DIRS[0])

    def append(self, record):
        self.ledger.append(record, home=self.home)
        return record

    def read_records(self):
        return self.ledger.read(RUN_ID, home=self.home)

    def records_of(self, kind):
        return [record for record in self.read_records() if record["kind"] == kind]

    def test_a_completed_receipt_becomes_a_late_result_and_a_closeout(self):
        intent = self.append(intent_record(detail={
            "expected_effect": "dispatch",
            "command": ["orchestration", "worker-start", "--task", "task-1"],
        }))
        snapshot = authority(orca=orca_section(
            request_show={intent["request_id"]: {
                "state": "completed",
                "receipt": {"state": "completed", "dispatchId": "dispatch-1"}}},
            workers={"state": "ok", "reason": "", "rows": [worker_row()]},
        ))
        replica = FAKE.FakeAuthority(snapshot)
        report = self.ledger.reconcile(RUN_ID, replica, home=self.home)

        self.assertEqual(replica.replays, [], "a completed receipt must not be replayed")
        self.assertEqual(report["counts"]["matched"], 1)
        results = self.records_of("result")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["request_id"], intent["request_id"])
        self.assertEqual(results[0]["state"], "completed")
        self.assertEqual(results[0]["detail"]["repair"], "late_result")

        closeouts = self.records_of("closeout")
        self.assertEqual(len(closeouts), 1)
        self.assertEqual(closeouts[0]["detail"]["counts"]["matched"], 1)
        self.assertEqual(report["closeout"]["record_id"], closeouts[0]["record_id"])
        self.assertTrue(Path(report["closeout"]["report_path"]).is_file())

    def test_reconcile_is_idempotent_for_the_same_authority_snapshot(self):
        self.append(intent_record())
        snapshot = authority(orca=orca_section(
            request_show={}, workers={"state": "ok", "reason": "",
                                      "rows": [worker_row()]},
        ))
        first = self.ledger.reconcile(RUN_ID, FAKE.FakeAuthority(snapshot), home=self.home)
        before = len(self.read_records())
        second = self.ledger.reconcile(RUN_ID, FAKE.FakeAuthority(snapshot), home=self.home)
        self.assertEqual(len(self.read_records()), before)
        self.assertEqual(first["counts"], second["counts"])

    def test_the_report_file_is_durable_and_the_human_rendering_names_evidence(self):
        self.append(intent_record())
        snapshot = authority(orca=orca_section(
            request_show={}, workers={"state": "ok", "reason": "",
                                      "rows": [worker_row()]},
        ))
        report = self.ledger.reconcile(RUN_ID, FAKE.FakeAuthority(snapshot), home=self.home)
        path = Path(report["closeout"]["report_path"])
        self.assertTrue(path.is_file())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        stored = json.loads(path.read_text())
        self.assertEqual(stored["counts"], report["counts"])
        human = self.ledger.render_report(report)
        self.assertIn("matched", human)
        self.assertIn("evidence", human)
        self.assertIn(report["closeout"]["report_path"], human)

    def test_a_pending_joinable_receipt_is_replayed_with_the_same_request_id(self):
        intent = self.append(intent_record(detail={
            "expected_effect": "dispatch",
            "command": ["orchestration", "worker-start", "--task", "task-1"],
        }))
        snapshot = authority(orca=orca_section(
            request_show={intent["request_id"]: {"state": "pending"}},
        ))
        replica = FAKE.FakeAuthority(snapshot, replay_responses={
            "worker-start": {"state": "completed", "requestId": intent["request_id"]}})
        report = self.ledger.reconcile(RUN_ID, replica, home=self.home)
        self.assertEqual(len(replica.replays), 1)
        self.assertEqual(replica.replays[0]["request_id"], intent["request_id"])
        self.assertEqual(replica.replays[0]["action"], "worker-start")
        self.assertEqual(replica.replays[0]["command"],
                         ["orchestration", "worker-start", "--task", "task-1"])
        self.assertEqual(report["replays"][0]["state"], "completed")
        results = self.records_of("result")
        self.assertEqual([record["request_id"] for record in results],
                         [intent["request_id"]])

    def test_reconcile_does_not_replay_when_told_not_to(self):
        intent = self.append(intent_record(detail={
            "expected_effect": "dispatch",
            "command": ["orchestration", "worker-start", "--task", "task-1"]}))
        snapshot = authority(orca=orca_section(
            request_show={intent["request_id"]: {"state": "pending"}}))
        replica = FAKE.FakeAuthority(snapshot)
        report = self.ledger.reconcile(RUN_ID, replica, home=self.home, replay=False)
        self.assertEqual(replica.replays, [])
        self.assertIn("replay_disabled", [item["class"] for item in report["decisions"]])

    def test_a_decision_becomes_a_pending_record(self):
        intent = self.append(intent_record(
            action="merge", idempotency="readback_only", subject={"pr": 12},
            request_id=""))
        snapshot = authority(pr_host={
            "state": "ok", "reason": "",
            "pull_requests": [pr_row(pr=12, state="OPEN")]})
        replica = FAKE.FakeAuthority(snapshot)
        report = self.ledger.reconcile(RUN_ID, replica, home=self.home)
        pending = self.records_of("pending")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["detail"]["decision_class"], "merge_replay")
        self.assertEqual(replica.replays, [])
        self.assertIn("merge_replay", [item["class"] for item in report["decisions"]])

    def test_reconcile_writes_gap_records_for_unreadable_sources(self):
        self.append(intent_record())
        snapshot = authority(
            orca=orca_section(
                request_show={}, workers={"state": "ok", "reason": "", "rows": []}),
            tracker={"state": "unknown", "reason": "tracker unreachable", "tickets": []},
        )
        self.ledger.reconcile(RUN_ID, FAKE.FakeAuthority(snapshot), home=self.home)
        gaps = self.records_of("gap")
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["detail"]["source"], "tracker")

    def test_reconcile_never_holds_the_ledger_lock_while_calling_authority(self):
        intent = self.append(intent_record())
        lock_path = self.home / "runs" / RUN_ID / "ledger.lock"
        self.assertTrue(lock_path.exists())
        probe = {}

        class LockProbe(FAKE.FakeAuthority):
            def snapshot(inner, run_id, records):
                descriptor = os.open(lock_path, os.O_RDWR)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    probe["free"] = True
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    probe["free"] = False
                finally:
                    os.close(descriptor)
                return super().snapshot(run_id, records)

        snapshot = authority(orca=orca_section(
            request_show={intent["request_id"]: {"state": "completed"}},
            workers={"state": "ok", "reason": "", "rows": [worker_row()]}))
        report = self.ledger.reconcile(RUN_ID, LockProbe(snapshot), home=self.home)
        self.assertTrue(probe["free"], "reconcile must not hold the ledger lock "
                                       "while reading authority")
        self.assertEqual(report["counts"]["matched"], 1)

    def test_reconcile_reads_a_consistent_prefix_while_appends_continue(self):
        intent = self.append(intent_record())
        snapshot = authority(orca=orca_section(
            request_show={intent["request_id"]: {"state": "completed"}},
            workers={"state": "ok", "reason": "", "rows": [worker_row()]}))
        slow = FAKE.FakeAuthority(snapshot)
        real_snapshot = slow.snapshot

        def delayed(run_id, records):
            time.sleep(0.3)
            return real_snapshot(run_id, records)

        slow.snapshot = delayed
        driver = self.root / "concurrent-writer.py"
        driver.write_text(
            "import importlib.util, json, sys, time\n"
            f"spec = importlib.util.spec_from_file_location('ledger_w', {str(SKILL_DIRS[0] / SCRIPT_RELATIVE)!r})\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            f"payload = json.load(open({str(self.root / 'payload.json')!r}))\n"
            "for record in payload:\n"
            "    module.append(record, home=" + repr(str(self.home)) + ")\n"
            "    time.sleep(0.02)\n"
        )
        payload = [make_record(kind="observation", state="observed", action="progress",
                               idempotency="none", request_id="",
                               detail={"n": number}) for number in range(8)]
        (self.root / "payload.json").write_text(json.dumps(payload))
        writer = subprocess.Popen([sys.executable, str(driver)], cwd=str(self.root),
                                  text=True, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
        report = self.ledger.reconcile(RUN_ID, slow, home=self.home)
        _out, error = writer.communicate(timeout=30)
        self.assertEqual(writer.returncode, 0, error)
        records = self.read_records()
        ids = {record["record_id"] for record in records}
        for record in payload:
            self.assertIn(record["record_id"], ids, "a concurrent append was lost")
        self.assertEqual(report["counts"]["matched"], 1)
        self.assertEqual(self.ledger.read_report(RUN_ID, home=self.home)[1], [])

    def test_reconcile_refuses_a_snapshot_for_another_run(self):
        self.append(intent_record())
        with self.assertRaises(ValueError):
            self.ledger.reconcile(
                RUN_ID, FAKE.FakeAuthority(authority(run_id="run_other")), home=self.home)


class LiveAuthorityTests(LedgerTestCase):
    """The live adapter gathers authority through the CLI and injected hosts."""

    def setUp(self):
        super().setUp()
        self.ledger = load_ledger(SKILL_DIRS[0])

    def fake(self, **config):
        return FAKE.FakeOrca(
            self.root / ("fake-" + uuid.uuid4().hex), **config).write()

    def hosts(self):
        return {
            "pr_host": FAKE.FakeHost({"state": "ok", "reason": "", "pull_requests": []}),
            "tracker": FAKE.FakeHost({"state": "ok", "reason": "", "tickets": []}),
            "git": FAKE.FakeHost({"state": "ok", "reason": "", "ancestry": []}),
        }

    def test_snapshot_reads_a_paginated_worker_list_including_remote_rows(self):
        intent = intent_record(detail={
            "expected_effect": "dispatch",
            "command": ["orchestration", "worker-start", "--task", "task-1"],
        })
        fake = self.fake(
            request_show={intent["request_id"]: {
                "state": "completed",
                "receipt": {"state": "completed", "dispatchId": "dispatch-1"}}},
            worker_pages=[
                {"workers": [worker_row(task_id="task-2", dispatch_id="dispatch-2",
                                        remote=True)]},
                {"workers": [worker_row(task_id="task-1", dispatch_id="dispatch-1")]},
            ],
            worker_show={
                "dispatch-1": {
                    "state": "ok",
                    "worker": {"dispatchId": "dispatch-1", "workerState": "ready"}},
                "dispatch-2": {
                    "state": "ok",
                    "worker": {"dispatchId": "dispatch-2", "workerState": "ready"}},
            },
            task_pages=[{"tasks": [task_row()]}],
        )
        adapter = self.ledger.LiveAuthority(orca_cli=str(fake.path), **self.hosts())
        snapshot = adapter.snapshot(RUN_ID, [intent])
        normalized = self.ledger.validated_authority(snapshot)

        rows = normalized["orca"]["workers"]["rows"]
        self.assertEqual(len(rows), 2, "both pages must be read")
        self.assertTrue(any(row.get("remote") for row in rows))
        self.assertEqual(normalized["orca"]["request_show"][intent["request_id"]]["state"],
                         "completed")
        self.assertEqual(
            normalized["orca"]["worker_show"]["dispatch-1"]["worker"]["worker_state"],
            "ready")
        self.assertEqual(normalized["orca"]["tasks"]["rows"][0]["orca_task_id"], "task-1")

        calls = fake.call_argv()
        self.assertEqual([call[1] for call in calls],
                         ["request-show", "worker-list", "worker-list",
                          "worker-show", "worker-show", "task-list"])
        first_worker_list = calls[1]
        self.assertIn("--include-remote", first_worker_list)
        self.assertIn("--limit", first_worker_list)
        self.assertNotIn("--cursor", first_worker_list)
        self.assertEqual(calls[2][calls[2].index("--cursor") + 1], "cursor-1")
        self.assertEqual(
            [call[call.index("--dispatch") + 1] for call in calls
             if call[1] == "worker-show"],
            ["dispatch-2", "dispatch-1"])

    def test_a_failed_request_show_is_unknown_not_absent(self):
        intent = intent_record()
        fake = self.fake(request_show={
            intent["request_id"]: {"fail": "request-show boom"}})
        adapter = self.ledger.LiveAuthority(orca_cli=str(fake.path), **self.hosts())
        snapshot = adapter.snapshot(RUN_ID, [intent])
        entry = self.ledger.validated_authority(snapshot)["orca"]["request_show"][
            intent["request_id"]]
        self.assertEqual(entry["state"], "unknown")
        self.assertIn("boom", entry["reason"])

    def test_a_failed_worker_list_is_unknown_with_no_rows(self):
        intent = intent_record()
        fake = self.fake(fail={"worker-list": "worker-list boom"},
                         request_show={intent["request_id"]: {"state": "absent"}})
        adapter = self.ledger.LiveAuthority(orca_cli=str(fake.path), **self.hosts())
        snapshot = adapter.snapshot(RUN_ID, [intent])
        workers = self.ledger.validated_authority(snapshot)["orca"]["workers"]
        self.assertEqual(workers["state"], "unknown")
        self.assertEqual(workers["rows"], [])
        self.assertIn("boom", workers["reason"])

    def test_missing_host_adapters_are_unknown_not_empty(self):
        fake = self.fake()
        adapter = self.ledger.LiveAuthority(orca_cli=str(fake.path))
        snapshot = self.ledger.validated_authority(adapter.snapshot(RUN_ID, []))
        for source in ("pr_host", "tracker", "git"):
            with self.subTest(source=source):
                self.assertEqual(snapshot[source]["state"], "unknown")
                self.assertTrue(snapshot[source]["reason"])

    def test_a_raising_host_adapter_is_unknown(self):
        fake = self.fake()
        adapter = self.ledger.LiveAuthority(
            orca_cli=str(fake.path), pr_host=FAKE.RaisingHost("pr host down"))
        snapshot = self.ledger.validated_authority(adapter.snapshot(RUN_ID, []))
        self.assertEqual(snapshot["pr_host"]["state"], "unknown")
        self.assertIn("pr host down", snapshot["pr_host"]["reason"])

    def test_a_provisional_progress_task_survives_normalisation_with_no_join_key(self):
        fake = self.fake(task_pages=[{"tasks": [
            {"taskId": "task-1", "orcaTaskId": None, "provisional": True}]}])
        adapter = self.ledger.LiveAuthority(orca_cli=str(fake.path), **self.hosts())
        snapshot = self.ledger.validated_authority(adapter.snapshot(RUN_ID, []))
        row = snapshot["orca"]["tasks"]["rows"][0]
        self.assertIsNone(row["orca_task_id"])
        self.assertTrue(row["provisional"])

    def test_replay_uses_the_recorded_command_and_the_same_request_id(self):
        intent = intent_record()
        fake = self.fake()
        adapter = self.ledger.LiveAuthority(orca_cli=str(fake.path), **self.hosts())
        response = adapter.replay(
            intent["request_id"], "worker-start", intent["subject"],
            ["orchestration", "worker-start", "--task", "task-1"])
        self.assertEqual(response["state"], "completed")
        self.assertEqual(fake.mutation_argv(), [[
            "orchestration", "worker-start", "--task", "task-1",
            "--retry-request", intent["request_id"]]])

    def test_replay_replaces_an_existing_retry_request_instead_of_duplicating_it(self):
        intent = intent_record()
        fake = self.fake()
        adapter = self.ledger.LiveAuthority(orca_cli=str(fake.path), **self.hosts())
        adapter.replay(intent["request_id"], "worker-start", intent["subject"],
                       ["orchestration", "worker-start", "--retry-request", "old-key",
                        "--task", "task-1"])
        argv = fake.mutation_argv()[0]
        self.assertEqual(argv.count("--retry-request"), 1)
        self.assertEqual(argv[argv.index("--retry-request") + 1], intent["request_id"])


class ReconcileCliTests(LedgerTestCase):
    """``ledger reconcile`` end to end over the shared fake and injected authority."""

    def setUp(self):
        super().setUp()
        self.ledger = load_ledger(SKILL_DIRS[0])
        self.intent = intent_record(detail={
            "expected_effect": "dispatch",
            "command": ["orchestration", "worker-start", "--task", "task-1"],
        })
        self.append(self.intent)
        self.authority_path = self.root / "authority.json"
        self.authority_path.write_text(json.dumps({
            "pr_host": {"state": "ok", "reason": "", "pull_requests": []},
            "tracker": {"state": "ok", "reason": "", "tickets": []},
            "git": {"state": "ok", "reason": "", "ancestry": []},
        }))

    def fake(self, **overrides):
        config = {
            "request_show": {self.intent["request_id"]: {
                "state": "completed",
                "receipt": {"state": "completed", "dispatchId": "dispatch-1"}}},
            "worker_pages": [
                {"workers": [worker_row(task_id="task-2", dispatch_id="dispatch-2",
                                        remote=True)]},
                {"workers": [worker_row(task_id="task-1", dispatch_id="dispatch-1")]},
            ],
            "worker_show": {"dispatch-1": {
                "state": "ok",
                "worker": {"dispatchId": "dispatch-1", "workerState": "ready"}}},
            "task_pages": [{"tasks": [task_row()]}],
            "lock_probe": self.home / "runs" / RUN_ID / "ledger.lock",
        }
        config.update(overrides)
        return FAKE.FakeOrca(self.root / ("fake-" + uuid.uuid4().hex), **config).write()

    def reconcile(self, fake, *args, env_extra=None):
        env = {**self.env, "ORCA_CLI_COMMAND": str(fake.path)}
        env.update(env_extra or {})
        return self.run_cli("reconcile", "--run-id", RUN_ID,
                            "--authority", str(self.authority_path), *args, env=env)

    def test_reconcile_runs_end_to_end_and_prints_the_report(self):
        fake = self.fake()
        result = self.reconcile(fake)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["counts"]["matched"], 1)
        self.assertEqual(report["authority_sources"], {
            "orca": "ok", "pr_host": "ok", "tracker": "ok", "git": "ok"})
        self.assertTrue(report["classifications"][0]["evidence"])
        self.assertIn("closeout", report)
        self.assertTrue(Path(report["closeout"]["report_path"]).is_file())
        self.assertEqual(fake.lock_probes(), ["free"] * len(fake.calls()),
                         "reconcile must never hold the ledger lock while calling Orca")
        self.assertEqual([call[1] for call in fake.call_argv()],
                         ["request-show", "worker-list", "worker-list",
                          "worker-show", "worker-show", "task-list"])
        closeouts = [record for record in self.read() if record["kind"] == "closeout"]
        self.assertEqual([record["record_id"] for record in closeouts],
                         [report["closeout"]["record_id"]])

    def test_a_progress_file_is_compared_with_the_ledger_run(self):
        """``--progress`` supplies the binding; a mismatch is one divergent outcome."""
        progress_path = self.root / "progress.json"
        progress_path.write_text(json.dumps({
            "schema_version": 2,
            "run": {"id": "progress-run", "orca_run_id": "run_other"}}))

        result = self.reconcile(self.fake(), "--progress", str(progress_path))

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["counts"]["divergent"], 1)
        divergent = next(item for item in report["classifications"]
                         if item["classification"] == "divergent")
        self.assertEqual(divergent["conflicts"], [{
            "field": "orca_run_id", "ledger": RUN_ID, "authority": "run_other"}])

    def test_the_authority_file_may_carry_the_progress_section(self):
        self.authority_path.write_text(json.dumps({
            "pr_host": {"state": "ok", "reason": "", "pull_requests": []},
            "tracker": {"state": "ok", "reason": "", "tickets": []},
            "git": {"state": "ok", "reason": "", "ancestry": []},
            "progress": {"state": "ok", "reason": "",
                         "orca_run_id": "run_other"},
        }))

        result = self.reconcile(self.fake())

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["counts"]["divergent"], 1)

    def test_a_second_reconcile_of_the_same_authority_does_not_duplicate_records(self):
        fake = self.fake()
        first = json.loads(self.reconcile(fake).stdout)
        before = len(self.read())
        second = json.loads(self.reconcile(fake).stdout)
        self.assertEqual(len(self.read()), before)
        self.assertEqual(first["closeout"]["record_id"],
                         second["closeout"]["record_id"])
        self.assertEqual(first["counts"], second["counts"])

    def test_the_human_rendering_names_each_classification_and_its_evidence(self):
        fake = self.fake()
        result = self.reconcile(fake, "--human")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Ledger reconciliation", result.stdout)
        self.assertIn("evidence:", result.stdout)
        self.assertIn("matched", result.stdout)
        self.assertIn("Closeout record:", result.stdout)

    def test_a_pending_joinable_action_is_replayed_with_the_same_request_id(self):
        intent = intent_record(detail={
            "expected_effect": "dispatch",
            "command": ["orchestration", "worker-start", "--task", "task-1"]})
        self.append(intent)
        fake = self.fake(request_show={
            intent["request_id"]: {"state": "pending"}})
        result = self.reconcile(fake)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["replays"][0]["request_id"], intent["request_id"])
        self.assertEqual(report["replays"][0]["state"], "completed")
        self.assertEqual(fake.mutation_argv(), [[
            "orchestration", "worker-start", "--task", "task-1",
            "--retry-request", intent["request_id"]]])

    def test_no_replay_flag_never_repeats_a_pending_action(self):
        intent = intent_record(detail={
            "expected_effect": "dispatch",
            "command": ["orchestration", "worker-start", "--task", "task-1"]})
        self.append(intent)
        fake = self.fake(request_show={
            intent["request_id"]: {"state": "pending"}})
        result = self.reconcile(fake, "--no-replay")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(fake.mutations(), [])
        self.assertIn("replay_disabled",
                      [decision["class"] for decision in report["decisions"]])
        pending = [record for record in self.read() if record["kind"] == "pending"]
        self.assertEqual(pending[0]["detail"]["decision_class"], "replay_disabled")

    def test_an_unreadable_orca_is_unknown_and_never_replayed(self):
        fake = self.fake(
            request_show={self.intent["request_id"]: {"fail": "request-show down"}},
            fail={"worker-list": "worker-list down"},
            task_pages=[{"tasks": []}])
        result = self.reconcile(fake)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertGreater(report["counts"]["unknown"], 0)
        self.assertEqual(fake.mutations(), [])
        self.assertIn("unreadable_authority",
                      [decision["class"] for decision in report["decisions"]])

    def test_the_injected_tracker_adapter_is_used(self):
        intent = intent_record(action="tracker-close", idempotency="readback_only",
                               subject={"ticket": "32"}, request_id="")
        self.append(intent)
        self.authority_path.write_text(json.dumps({
            "pr_host": {"state": "ok", "reason": "", "pull_requests": []},
            "tracker": {"state": "ok", "reason": "",
                        "tickets": [{"ticket": "32", "state": "closed"}]},
            "git": {"state": "ok", "reason": "", "ancestry": []},
        }))
        result = self.reconcile(self.fake())
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        tracker_outcomes = [
            item for item in report["classifications"]
            if item["action"] == "tracker-close" and item["classification"] == "matched"]
        self.assertEqual(len(tracker_outcomes), 1)
        self.assertIn("tracker", [evidence["source"]
                                  for evidence in tracker_outcomes[0]["evidence"]])

    def test_an_invalid_authority_file_exits_one_and_claims_nothing(self):
        self.authority_path.write_text(json.dumps({
            "tracker": {"state": "empty", "reason": "", "tickets": []}}))
        result = self.reconcile(self.fake())
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("Ledger error", result.stderr)

    def test_usage_errors_exit_two(self):
        for arguments in (("reconcile",),
                          ("reconcile", "--run-id", RUN_ID, "--explode")):
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments)
                self.assertEqual(result.returncode, 2, result.stderr)


class ProjectionRepairTests(LedgerTestCase):
    """An authority-supplied binding updates only the projection, as an observation."""

    def setUp(self):
        super().setUp()
        self.ledger = load_ledger(SKILL_DIRS[0])

    def intent(self):
        return intent_record(detail={
            "expected_effect": "dispatch",
            "progress_event_id": "event-1",
            "command": ["orchestration", "worker-start", "--task", "task-1"],
        })

    def test_classify_plans_a_projection_update_from_the_authority_binding(self):
        intent = self.intent()
        report = self.ledger.classify([intent], authority(orca=orca_section(
            request_show={intent["request_id"]: {"state": "completed"}},
            workers={"state": "ok", "reason": "", "rows": [worker_row()]},
        )))
        kinds = [repair["kind"] for repair in report["repairs"]]
        self.assertIn("late_result", kinds)
        self.assertIn("projection", kinds)
        projection = next(repair for repair in report["repairs"]
                          if repair["kind"] == "projection")
        self.assertEqual(projection["projection"]["dispatch_id"], "dispatch-1")
        self.assertEqual(projection["projection"]["attempt"], 1)

    def test_reconcile_records_the_projection_as_an_observation(self):
        intent = self.intent()
        self.ledger.append(intent, home=self.home)
        snapshot = authority(orca=orca_section(
            request_show={intent["request_id"]: {"state": "completed"}},
            workers={"state": "ok", "reason": "", "rows": [worker_row()]},
        ))
        self.ledger.reconcile(RUN_ID, FAKE.FakeAuthority(snapshot), home=self.home)
        observations = [record for record in self.ledger.read(RUN_ID, home=self.home)
                        if record["kind"] == "observation"]
        projection = [record for record in observations
                      if record["detail"].get("repair") == "projection"]
        self.assertEqual(len(projection), 1)
        self.assertEqual(projection[0]["action"], "progress")
        self.assertEqual(projection[0]["detail"]["projection"]["dispatch_id"],
                         "dispatch-1")


class DispatchCompositionTests(LedgerTestCase):
    """The fixed dispatch order, composed as subprocesses from the real CLIs.

    The coordinator contract is prose, not a new CLI: preflight receipt, ledger
    intent, ``worker-start --retry-request``, result, best-effort progress and the
    post-receipt observation. These tests run that sequence with the real ledger
    and progress CLIs plus the shared fake Orca, and assert the gates together
    with the record-failure semantics.
    """

    def setUp(self):
        super().setUp()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        self.progress_state = self.root / "progress-state"
        self.evidence = self.root / "evidence"
        self.evidence.mkdir()
        self.authority_path = self.root / "authority.json"
        self.authority_path.write_text(json.dumps({
            "pr_host": {"state": "ok", "reason": "", "pull_requests": []},
            "tracker": {"state": "ok", "reason": "", "tickets": []},
            "git": {"state": "ok", "reason": "", "ancestry": []},
        }))
        self.fake = None

    # -- helpers ---------------------------------------------------------

    def fake_orca(self, **config):
        self.fake = FAKE.FakeOrca(
            self.root / ("fake-" + uuid.uuid4().hex), **config).write()
        self.env["ORCA_CLI_COMMAND"] = str(self.fake.path)
        return self.fake

    def orca(self, *args):
        return subprocess.run(
            [str(self.fake.path), *args], env=self.env, text=True, capture_output=True)

    def progress_process(self, command, data=None, event="progress-1", flags=(),
                         global_flags=()):
        args = [sys.executable, str(SKILL_DIRS[0] / PROGRESS_RELATIVE),
                "--state", str(self.progress_state), *global_flags, command]
        if command != "show":
            args += ["--event-id", event, "--data", json.dumps(data or {}), *flags]
        return subprocess.run(args, env=self.env, text=True, capture_output=True)

    def progress(self, *args, **kwargs):
        result = self.progress_process(*args, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def snapshot(self):
        return json.loads(self.progress("show").stdout)

    def compose(self, steps):
        """Run ``(name, call, gated)`` steps in order; a gated failure stops it."""
        completed = []
        for name, call, gated in steps:
            result = call()
            completed.append((name, result))
            if gated and result.returncode != 0:
                break
        return completed

    def reconcile(self, run_id=RUN_ID):
        return self.run_cli("reconcile", "--run-id", run_id,
                            "--authority", str(self.authority_path))

    def append_step(self, record):
        return lambda: self.run_cli(
            "append", "--run-id", RUN_ID, "--record", json.dumps(record))

    def record_evidence_and_append(self, name, text, record):
        (self.evidence / name).write_text(text)
        return self.run_cli(
            "append", "--run-id", RUN_ID, "--record", json.dumps(record))

    # -- tests -----------------------------------------------------------

    def test_the_fixed_dispatch_order_preflight_intent_start_result_progress_observation(self):
        request_id = str(uuid.uuid4())
        receipt = {"state": "ready", "stage": "input_accepted",
                   "dispatchId": "dispatch-1", "turnStart": "observed"}
        fake = self.fake_orca(
            mutations={"worker-start": [receipt]},
            worker_show={"dispatch-1": {
                "taskId": "orca-task-1", "dispatchId": "dispatch-1", "attempt": 1,
                "workerState": "running", "runId": RUN_ID}},
        )
        captured = {}
        preflight = make_record(
            kind="preflight", action="preflight", request_id="", idempotency="none",
            state="completed", detail={"blocking": False, "items": []})
        intent = make_record(
            kind="intent", state="intent", action="worker-start",
            request_id=request_id, subject={"task_id": "orca-task-1", "attempt": 1},
            detail={"expected_effect": "dispatch", "progress_event_id": "dispatch-w1",
                    "command": ["orchestration", "worker-start", "--task",
                                "orca-task-1", "--spec", "spec.md"]})

        def start():
            result = self.orca("orchestration", "worker-start", "--task",
                               "orca-task-1", "--spec", "spec.md",
                               "--retry-request", request_id)
            if result.returncode == 0:
                captured["receipt"] = json.loads(result.stdout)["result"]
            return result

        def result_step():
            record = make_record(
                kind="result", state="completed", action="worker-start",
                request_id=request_id,
                subject={"task_id": "orca-task-1", "attempt": 1},
                detail={"raw_receipt": captured["receipt"],
                        "attempt_source": "local"})
            return self.append_step(record)()

        def observe():
            shown = self.orca("orchestration", "worker-show", "--dispatch",
                              "dispatch-1")
            record = make_record(
                kind="observation", state="observed", action="worker-start",
                request_id="", idempotency="none",
                subject={"task_id": "orca-task-1", "dispatch_id": "dispatch-1",
                         "attempt": 1},
                evidence_ref="evidence/worker-show.json",
                detail={"turn_start": "observed"})
            return self.record_evidence_and_append(
                "worker-show.json", shown.stdout, record)

        steps = [
            ("preflight", self.append_step(preflight), True),
            ("intent", self.append_step(intent), True),
            ("worker-start", start, True),
            ("result", result_step, True),
            ("progress-init", lambda: self.progress_process(
                "init", {"id": "run-compose", "target_repo": str(self.repo),
                         "mode": "REAL"}), False),
            ("progress-task", lambda: self.progress_process(
                "task", {"id": "local-1", "provisional": True},
                event="task-provisional"), False),
            ("progress-bind", lambda: self.progress_process(
                "task", {"id": "local-1", "provisional": False,
                         "orca_task_id": "orca-task-1"}, event="task-bound"), False),
            ("progress-worker", lambda: self.progress_process(
                "worker", {"id": "w1", "task_id": "local-1",
                           "dispatch_id": "dispatch-1", "attempt": 1,
                           "worktree": "wt-1", "start_confirmation": "observed",
                           "lifecycle": "running"}, event="dispatch-w1"), False),
            ("observation", observe, True),
        ]
        completed = self.compose(steps)
        self.assertEqual([name for name, _ in completed], [name for name, _, _ in steps])
        self.assertEqual([result.returncode for _, result in completed],
                         [0] * len(steps), completed)
        self.assertEqual(fake.mutation_argv(), [[
            "orchestration", "worker-start", "--task", "orca-task-1",
            "--spec", "spec.md", "--retry-request", request_id]])
        records = self.read()
        self.assertEqual([record["kind"] for record in records],
                         ["preflight", "intent", "result", "observation"])
        self.assertEqual(records[1]["request_id"], request_id)
        self.assertEqual(records[2]["request_id"], request_id)
        self.assertEqual(records[2]["detail"]["raw_receipt"], captured["receipt"])
        self.assertEqual(records[2]["detail"]["attempt_source"], "local",
                         "an unverified receipt attempt is a local count")
        self.assertEqual(records[3]["subject"]["dispatch_id"], "dispatch-1")
        state = self.snapshot()
        self.assertEqual([event["type"] for event in state["events"]],
                         ["init", "task", "task", "worker"])
        worker = state["workers"]["w1"]
        self.assertEqual((worker["dispatch_id"], worker["attempt"], worker["worktree"]),
                         ("dispatch-1", 1, "wt-1"))
        self.assertFalse(state["tasks"]["local-1"]["provisional"])
        self.assertEqual(state["tasks"]["local-1"]["orca_task_id"], "orca-task-1")

    def test_a_lost_worker_start_response_becomes_a_late_result_and_never_redispatches(self):
        request_id = str(uuid.uuid4())
        receipt = {"state": "completed", "dispatchId": "dispatch-1"}
        fake = self.fake_orca(
            mutations={"worker-start": [{"fail": "connection reset after the effect"}]},
            request_show={request_id: {"state": "completed", "receipt": receipt}},
            worker_pages=[{"workers": [{
                "taskId": "orca-task-1", "dispatchId": "dispatch-1", "attempt": 1,
                "runId": RUN_ID, "workerState": "running", "settled": True}]}],
        )
        intent = make_record(
            kind="intent", state="intent", action="worker-start",
            request_id=request_id, subject={"task_id": "orca-task-1", "attempt": 1},
            detail={"expected_effect": "dispatch",
                    "command": ["orchestration", "worker-start", "--task",
                                "orca-task-1"]})
        self.append(intent)
        lost = self.orca("orchestration", "worker-start", "--task", "orca-task-1",
                         "--retry-request", request_id)
        self.assertEqual(lost.returncode, 1)
        self.assertEqual(len(fake.mutations()), 1)
        reconciled = self.reconcile()
        self.assertEqual(reconciled.returncode, 0, reconciled.stderr)
        report = json.loads(reconciled.stdout)
        matched = [item for item in report["classifications"]
                   if item["classification"] == "matched"]
        self.assertEqual(len(matched), 1, report)
        self.assertEqual([repair["kind"] for repair in matched[0]["repairs"]],
                         ["late_result"])
        self.assertEqual(len(fake.mutations()), 1,
                         "a completed request is adopted, never dispatched again")
        results = [record for record in self.read() if record["kind"] == "result"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["state"], "completed")
        self.assertEqual(results[0]["detail"]["raw_receipt"], receipt)
        # Without an intent, an absent request is not a judgment at all.
        other_run = "run_without_intent"
        no_intent = self.reconcile(other_run)
        self.assertEqual(no_intent.returncode, 0, no_intent.stderr)
        no_intent_report = json.loads(no_intent.stdout)
        self.assertEqual(no_intent_report["counts"]["orphan_intent"], 0)
        self.assertEqual(no_intent_report["replays"], [])
        self.assertEqual(len(fake.mutations()), 1)
        request_shows = [call for call in fake.call_argv() if call[1] == "request-show"]
        self.assertEqual(len(request_shows), 1,
                         "an absent request with no intent is never looked up or judged")

    def test_a_failed_intent_write_prevents_the_worker_start_call(self):
        fake = self.fake_orca()
        intent = make_record(
            kind="intent", state="intent", action="worker-start",
            request_id=str(uuid.uuid4()),
            subject={"task_id": "orca-task-1", "attempt": 1},
            detail={"expected_effect": "dispatch"})
        intent["owner"] = "nobody"  # an undocumented field fails validation
        steps = [
            ("preflight", self.append_step(make_record(
                kind="preflight", action="preflight", request_id="",
                idempotency="none", state="completed", detail={})), True),
            ("intent", self.append_step(intent), True),
            ("worker-start", lambda: self.orca(
                "orchestration", "worker-start", "--task", "orca-task-1",
                "--retry-request", intent["request_id"]), True),
        ]
        completed = self.compose(steps)
        self.assertEqual([name for name, _ in completed], ["preflight", "intent"])
        self.assertEqual(completed[-1][1].returncode, 1)
        self.assertEqual(fake.calls(), [],
                         "no Orca call may run without a durable intent")

    def test_a_result_write_failure_preserves_the_receipt_records_a_gap_and_never_replays(self):
        request_id = str(uuid.uuid4())
        receipt = {"state": "ready", "stage": "input_accepted",
                   "dispatchId": "dispatch-1"}
        fake = self.fake_orca(
            mutations={"worker-start": [receipt]},
            request_show={request_id: {"state": "completed", "receipt": receipt}},
            worker_pages=[{"workers": [{
                "taskId": "orca-task-1", "dispatchId": "dispatch-1", "attempt": 1,
                "runId": RUN_ID, "workerState": "running"}]}],
        )
        intent = make_record(
            kind="intent", state="intent", action="worker-start",
            request_id=request_id, subject={"task_id": "orca-task-1", "attempt": 1},
            detail={"expected_effect": "dispatch",
                    "command": ["orchestration", "worker-start", "--task",
                                "orca-task-1"]})
        self.append(intent)
        started = self.orca("orchestration", "worker-start", "--task",
                            "orca-task-1", "--retry-request", request_id)
        self.assertEqual(started.returncode, 0, started.stderr)
        raw = json.loads(started.stdout)["result"]
        evidence_path = self.evidence / "worker-start-receipt.json"
        evidence_path.write_text(json.dumps(raw, sort_keys=True))
        broken = make_record(
            kind="result", state="completed", action="worker-start",
            request_id=request_id,
            subject={"task_id": "orca-task-1", "attempt": 1},
            detail={"raw_receipt": raw})
        broken["owner"] = "nobody"
        failed = self.run_cli("append", "--run-id", RUN_ID,
                              "--record", json.dumps(broken))
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(json.loads(evidence_path.read_text()), raw,
                         "a failed result write keeps the raw receipt")
        gap = make_record(
            kind="gap", state="failed", action="worker-start", request_id=request_id,
            subject={"task_id": "orca-task-1", "attempt": 1},
            evidence_ref=str(evidence_path),
            detail={"reason": "result write failed; raw receipt preserved"})
        self.append(gap)
        reconciled = self.reconcile()
        self.assertEqual(reconciled.returncode, 0, reconciled.stderr)
        report = json.loads(reconciled.stdout)
        self.assertEqual(report["counts"]["matched"], 1, report)
        self.assertEqual(len(fake.mutations()), 1,
                         "a failed result record never causes a replay")
        records = self.read()
        self.assertEqual([record["kind"] for record in records].count("gap"), 1)
        late = [record for record in records
                if record["kind"] == "result" and record["state"] == "completed"]
        self.assertEqual(len(late), 1)
        self.assertEqual(late[0]["detail"]["raw_receipt"], receipt)

    def test_a_progress_failure_records_a_gap_then_backfills_with_the_same_event_id(self):
        request_id = str(uuid.uuid4())
        receipt = {"state": "ready", "stage": "input_accepted",
                   "dispatchId": "dispatch-1"}
        self.fake_orca(
            mutations={"worker-start": [receipt]},
            request_show={request_id: {"state": "completed", "receipt": receipt}},
            worker_pages=[{"workers": [{
                "taskId": "orca-task-1", "dispatchId": "dispatch-1", "attempt": 1,
                "worktree": "wt-1", "runId": RUN_ID, "workerState": "running",
                "settled": True}]}],
            task_pages=[{"tasks": [{"taskId": "local-1",
                                    "orcaTaskId": "orca-task-1"}]}],
        )
        intent = make_record(
            kind="intent", state="intent", action="worker-start",
            request_id=request_id, subject={"task_id": "orca-task-1", "attempt": 1},
            detail={"expected_effect": "dispatch", "progress_event_id": "dispatch-w1",
                    "command": ["orchestration", "worker-start", "--task",
                                "orca-task-1"]})
        self.append(intent)
        started = self.orca("orchestration", "worker-start", "--task",
                            "orca-task-1", "--retry-request", request_id)
        self.assertEqual(started.returncode, 0, started.stderr)
        raw = json.loads(started.stdout)["result"]
        self.append(make_record(
            kind="result", state="completed", action="worker-start",
            request_id=request_id,
            subject={"task_id": "orca-task-1", "attempt": 1},
            detail={"raw_receipt": raw}))
        # The first progress write fails because the run was never initialized.
        failed = self.progress_process(
            "worker", {"id": "w1", "task_id": "local-1", "dispatch_id": "dispatch-1",
                       "attempt": 1, "worktree": "wt-1"}, event="dispatch-w1")
        self.assertEqual(failed.returncode, 1)
        self.assertIn("initialize", failed.stderr)
        self.append(make_record(
            kind="gap", state="failed", action="progress", request_id="",
            idempotency="none",
            subject={"task_id": "orca-task-1", "dispatch_id": "dispatch-1",
                     "attempt": 1},
            detail={"reason": "progress write failed", "event_id": "dispatch-w1"}))
        # Dispatch continues: the post-receipt observation is still recorded.
        self.append(make_record(
            kind="observation", state="observed", action="worker-start",
            request_id="", idempotency="none",
            subject={"task_id": "orca-task-1", "dispatch_id": "dispatch-1",
                     "attempt": 1},
            detail={"turn_start": "unobserved"}))
        self.assertEqual([record["kind"] for record in self.read()],
                         ["intent", "result", "gap", "observation"])
        # The next checkpoint backfills the row from the ledger and the authority.
        reconciled = self.reconcile()
        self.assertEqual(reconciled.returncode, 0, reconciled.stderr)
        projection = [record for record in self.read()
                      if record["kind"] == "observation"
                      and record["detail"].get("repair") == "projection"]
        self.assertEqual(len(projection), 1)
        fields = projection[0]["detail"]["projection"]
        self.assertEqual(fields, {"task_id": "orca-task-1",
                                  "dispatch_id": "dispatch-1",
                                  "attempt": 1, "worktree": "wt-1"})
        self.progress("init", {"id": "run-backfill", "target_repo": str(self.repo),
                               "mode": "REAL"})
        self.progress("task", {"id": "local-1", "provisional": True},
                      event="task-local")
        self.progress("task", {"id": "local-1", "provisional": False,
                               "orca_task_id": fields["task_id"]},
                      event="task-bound")
        backfill = {"id": "w1", "task_id": "local-1",
                    "dispatch_id": fields["dispatch_id"],
                    "attempt": fields["attempt"], "worktree": fields["worktree"]}
        self.progress("worker", backfill, event="dispatch-w1")
        before_retry = (self.progress_state / "progress.json").read_bytes()
        self.progress("worker", backfill, event="dispatch-w1")
        self.assertEqual((self.progress_state / "progress.json").read_bytes(),
                         before_retry, "the retry reuses the same event ID")
        conflict = {**backfill, "worktree": "different"}
        rejected = self.progress_process("worker", conflict, event="dispatch-w1")
        self.assertEqual(rejected.returncode, 1)
        self.assertEqual((self.progress_state / "progress.json").read_bytes(),
                         before_retry)
        worker = self.snapshot()["workers"]["w1"]
        self.assertEqual((worker["dispatch_id"], worker["attempt"],
                          worker["worktree"]), ("dispatch-1", 1, "wt-1"))


class DocumentationParityTests(unittest.TestCase):
    """Both language packs document the same reconciliation contract."""

    def documents(self):
        return [directory / "references" / "ledger.md" for directory in SKILL_DIRS]

    def assert_tokens(self, tokens):
        for path in self.documents():
            text = path.read_text(encoding="utf-8")
            for token in tokens:
                self.assertIn(token, text, str(path) + " is missing " + token)

    def test_both_references_document_windows_and_the_action_matrix(self):
        self.assert_tokens((
            "W1", "W2", "W3", "matched", "orphan_intent", "orphan_effect",
            "divergent", "unknown", "merge_replay", "tracker_close_replay",
            "unproven_retry", "unknown_outcome", "unsettled_release",
            "send_reply_no_resend", "stop_abandon_no_retry",
        ))

    def test_both_references_document_the_snapshot_and_report_fields(self):
        self.assert_tokens((
            "orca.request_show", "orca.workers", "orca.tasks", "orca.worker_show",
            "orca.messages", "pr_host", "tracker", "git", "authority_sources",
            "unreadable", "replays", "closeout", "counts", "repairs", "decisions",
            "reconcile --run-id", "--no-replay", "--human", "ORCA_CLI_COMMAND",
        ))

    def test_both_references_document_the_full_action_matrix_and_two_hard_rules(self):
        self.assert_tokens((
            "send", "reply", "ack", "stop", "abandon", "retain", "release", "merge",
            "pr-create", "tracker-close", "progress", "preflight",
            "request_id", "event_id", "joinable", "replay_safe", "readback_only",
            "lookup_then_create", "last_observed", "last_effective_progress",
            "turnStart", "bookkeeping",
        ))
        anchors = (
            (SKILL_DIRS[0] / "references" / "ledger.md",
             ("Idempotency", "Key before the action", "Read-back", "When uncertain",
              "Two hard rules", "Write the intent before the action",
              "read authority before deciding", "Fixed dispatch order")),
            (SKILL_DIRS[1] / "references" / "ledger.md",
             ("幂等", "动作前键", "回读", "不确定时", "两条硬规则",
              "动作前写 intent", "不确定时先读权威", "固定派发顺序")),
        )
        for path, tokens in anchors:
            text = path.read_text(encoding="utf-8")
            for token in tokens:
                self.assertIn(token, text, str(path) + " is missing " + token)


if __name__ == "__main__":
    unittest.main()
