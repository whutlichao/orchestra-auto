#!/usr/bin/env python3
"""Behaviour tests for ``ledger.py``, the durable coordination ledger.

The declared seams are deliberate:

* **CLI end to end** for storage behaviour — append, read, filters, summary, retention,
  permissions, path refusal, exit codes and concurrency. These run the real script as a
  subprocess against a temporary ledger home outside every repository, so the assertions
  are about the JSON that went in, the files that came out and the process exit code.
* **Module import** for pure validation — the schema-v1 checks, normalisation, and the path
  policy that needs an explicit target repository. The module is loaded by path with the
  same ``importlib`` loader style the inbox tests use; the script is never turned into a
  package and no CLI exists only for tests.

Nothing here touches the network, real Orca, credentials, or ``~/.orca``: every fixture
gets a temporary home directory and a temporary ``ORCA_LEDGER_HOME``.
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


if __name__ == "__main__":
    unittest.main()
