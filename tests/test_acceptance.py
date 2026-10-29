#!/usr/bin/env python3
"""Behaviour tests for ``acceptance.py``, the acceptance record and its four levels.

The declared seams are deliberate and narrow:

* **Module import** for the pure parts — ``validate(record)`` and its evidence rules,
  ``verify(record, authority)`` against an injected authority snapshot, and
  ``aggregate(ticket, records)`` over ledger records. These take data, return data and
  touch no disk, clock or network.
* **CLI end to end** for the product surface — ``record``, ``show``, ``observe`` and
  ``open-questions`` run as a subprocess against a temporary ``$ORCA_LEDGER_HOME`` outside
  every repository, so the assertions are about the ledger file that came out, the JSON
  printed and the exit code. Storage is the existing ``ledger.py``; this module does not
  re-implement it.
* **The shared fake** in ``tests/fake_orca.py`` supplies the PR-host, tracker and git
  authority adapters. Nothing here calls a live Orca, a PR host, a tracker, the network or
  a credential. An Orca receipt shape is never asserted as a stable contract: the start
  evidence accepted here is ``turnStart: observed`` or the attempt's own artifact /
  status-projection reference, never a receipt field the design has not verified.

The acceptance schema is local. It is recorded in ``references/gate-acceptance.md``.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIRS = (
    REPO_ROOT / "skills" / "orchestra-auto",
    REPO_ROOT / "skills" / "orchestra-auto-zh",
)
SCRIPT_RELATIVE = Path("scripts") / "acceptance.py"
RUN_ID = "run_alpha"

LEVELS = ("implemented", "integrated", "merged", "tracker_closed")


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha(character="a"):
    return character * 40


def acceptance(**overrides):
    """A complete acceptance record; overrides replace one field at a time."""
    at = utc_now()
    record = {
        "ticket": "39",
        "gate": ["G-impl"],
        "attempt": 1,
        "accepted_sha": sha("a"),
        "accepted_at": at,
        "pr": {"number": 12, "head_at_acceptance": sha("a"), "base": "main"},
        "checks": [
            {
                "name": "acceptance tests",
                "command": "python3 -m unittest tests.test_acceptance",
                "result": "pass",
                "evidence_ref": "evidence/acceptance-tests.log",
            },
        ],
        "review": {
            "axis": ["standards", "spec"],
            "result": "pass",
            "evidence_ref": "evidence/code-review.md",
        },
        "levels": {
            "implemented": {
                "value": True,
                "evidence_ref": "evidence/answer.md#implemented",
                "at": at,
                "start": {"turn_start": "observed"},
            },
            "integrated": {
                "value": True,
                "evidence_ref": "evidence/integration-ancestry.txt",
                "at": at,
            },
            "merged": {
                "value": True,
                "evidence_ref": "evidence/pr-12.json",
                "at": at,
                "merge_sha": sha("b"),
                "pr_state": "MERGED",
                "ancestry": {"ancestor": sha("a"), "descendant": sha("b"), "result": True},
            },
            "tracker_closed": {
                "value": True,
                "evidence_ref": "evidence/tracker-39.json",
                "at": at,
                "read_back": {"state": "closed", "at": at},
            },
        },
        "decided_by": "coordinator",
        "decided_at": at,
        "answer_location": "tickets/task-39-answer.md",
    }
    record.update(overrides)
    return record


def open_levels(**values):
    """All four levels false with no evidence; used for rejection-style records."""
    return {
        level: {"value": values.get(level, False), "at": utc_now()}
        for level in LEVELS
    }


def observation_record(**overrides):
    record = {
        "ticket": "39",
        "pr": 12,
        "level": "merged",
        "observation": "revert",
        "reason": "the merge was reverted on main",
        "evidence_ref": "evidence/revert-12.txt",
        "observed_at": utc_now(),
    }
    record.update(overrides)
    return record


def load_module(skill_dir, filename):
    """Import a script by path without turning the scripts directory into a package."""
    path = skill_dir / "scripts" / filename
    name = path.stem + "_" + skill_dir.name.replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot build an import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_fake_orca():
    """Import the shared fake harness under whichever name the runner used."""
    path = Path(__file__).resolve().parent / "fake_orca.py"
    spec = importlib.util.spec_from_file_location("fake_orca", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot build an import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FAKE = load_fake_orca()


def authority_snapshot(pr_host=None, tracker=None, git=None):
    """A complete authority snapshot; every missing section is unreadable, never empty."""
    snapshot = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "observed_at": utc_now(),
    }
    if pr_host is not None:
        snapshot["pr_host"] = pr_host
    if tracker is not None:
        snapshot["tracker"] = tracker
    if git is not None:
        snapshot["git"] = git
    return snapshot


class ValidationTests(unittest.TestCase):
    """``validate(record)`` is pure and returns a list of errors."""

    @classmethod
    def setUpClass(cls):
        cls.module = load_module(SKILL_DIRS[0], "acceptance.py")

    def assert_error(self, needle, record=None, **overrides):
        payload = acceptance() if record is None else record
        if overrides:
            payload.update(overrides)
        errors = self.module.validate(payload)
        self.assertTrue(
            any(needle in error for error in errors),
            f"expected an error containing {needle!r}, got {errors}",
        )

    def test_a_complete_record_is_accepted(self):
        self.assertEqual(self.module.validate(acceptance()), [])

    def test_a_true_level_without_an_evidence_reference_is_rejected(self):
        for level in LEVELS:
            with self.subTest(level=level):
                record = acceptance()
                record["levels"][level].pop("evidence_ref")
                errors = self.module.validate(record)
                self.assertTrue(
                    any(level in error and "evidence_ref" in error for error in errors),
                    f"{level}: {errors}",
                )

    def test_a_false_level_needs_no_evidence(self):
        record = acceptance(levels=open_levels())
        self.assertEqual(self.module.validate(record), [])

    def test_required_fields_are_required(self):
        for field in acceptance():
            with self.subTest(field=field):
                record = acceptance()
                del record[field]
                self.assertNotEqual(self.module.validate(record), [],
                                    f"{field}: a missing field must be an error")

    def test_unknown_fields_are_rejected(self):
        self.assert_error("unknown", acceptance(approval="yes"))
        record = acceptance()
        record["levels"]["merged"]["looks_good"] = True
        self.assert_error("unknown", record)

    def test_ticket_and_attempt_are_typed(self):
        self.assert_error("ticket", acceptance(ticket=""))
        self.assert_error("ticket", acceptance(ticket=39))
        for attempt in (0, -1, "1", True, None):
            with self.subTest(attempt=attempt):
                self.assert_error("attempt", acceptance(attempt=attempt))

    def test_gate_is_a_non_empty_list_of_known_gates(self):
        for gate in ([], "G-impl", ["G-unknown"], [""]):
            with self.subTest(gate=gate):
                self.assert_error("gate", acceptance(gate=gate))

    def test_timestamps_are_rfc3339_utc(self):
        for value in ("2026-10-29", "2026-10-29T12:00:00", "today", 1):
            with self.subTest(value=value):
                self.assert_error("accepted_at", acceptance(accepted_at=value))
        record = acceptance()
        record["levels"]["implemented"]["at"] = "yesterday"
        self.assert_error("at", record)

    def test_pr_needs_a_number_a_head_and_a_base(self):
        for pr in ({"number": 12, "base": "main"},
                   {"head_at_acceptance": sha(), "base": "main"},
                   {"number": "12", "head_at_acceptance": sha(), "base": "main"},
                   {"number": 0, "head_at_acceptance": sha(), "base": "main"}):
            with self.subTest(pr=pr):
                self.assert_error("pr", acceptance(pr=pr))

    def test_a_decision_belongs_to_the_coordinator_or_a_human(self):
        for decider in ("worker", "rule", "", None):
            with self.subTest(decider=decider):
                self.assert_error("decided_by", acceptance(decided_by=decider))

    def test_the_answer_location_is_required(self):
        self.assert_error("answer_location", acceptance(answer_location=""))

    def test_both_language_variants_validate_identically(self):
        chinese = load_module(SKILL_DIRS[1], "acceptance.py")
        for record in (acceptance(), acceptance(levels=open_levels())):
            self.assertEqual(chinese.validate(record), self.module.validate(record))


class MinimumEvidenceTests(unittest.TestCase):
    """A claim is only accepted with the evidence the spec names for it."""

    @classmethod
    def setUpClass(cls):
        cls.module = load_module(SKILL_DIRS[0], "acceptance.py")

    def errors(self, record):
        return self.module.validate(record)

    def assert_error(self, needle, record):
        errors = self.errors(record)
        self.assertTrue(any(needle in error for error in errors),
                        f"expected an error containing {needle!r}, got {errors}")

    def test_a_merge_without_the_pr_host_state_is_rejected(self):
        record = acceptance()
        record["levels"]["merged"].pop("pr_state")
        self.assert_error("MERGED", record)
        record = acceptance()
        record["levels"]["merged"]["pr_state"] = "OPEN"
        self.assert_error("MERGED", record)

    def test_a_merge_without_a_merge_sha_is_rejected(self):
        record = acceptance()
        record["levels"]["merged"].pop("merge_sha")
        self.assert_error("merge_sha", record)

    def test_a_merge_without_ancestry_is_rejected(self):
        record = acceptance()
        record["levels"]["merged"].pop("ancestry")
        self.assert_error("ancestry", record)
        record = acceptance()
        record["levels"]["merged"]["ancestry"]["result"] = False
        self.assert_error("ancestry", record)

    def test_ancestry_must_cover_the_accepted_head_up_to_the_merge(self):
        record = acceptance()
        record["levels"]["merged"]["ancestry"]["ancestor"] = sha("c")
        self.assert_error("ancestry", record)
        record = acceptance()
        record["levels"]["merged"]["ancestry"]["descendant"] = sha("d")
        self.assert_error("ancestry", record)

    def test_a_tracker_close_without_a_read_back_is_rejected(self):
        record = acceptance()
        record["levels"]["tracker_closed"].pop("read_back")
        self.assert_error("read_back", record)
        record = acceptance()
        record["levels"]["tracker_closed"]["read_back"] = {"state": "open", "at": utc_now()}
        self.assert_error("read_back", record)

    def test_a_start_needs_an_observed_turn_start_or_the_attempt_evidence(self):
        for start in (None, {}, {"turn_start": "unobserved"},
                      {"source": "worker_done", "evidence_ref": "evidence/done.txt"}):
            with self.subTest(start=start):
                record = acceptance()
                if start is None:
                    record["levels"]["implemented"].pop("start")
                else:
                    record["levels"]["implemented"]["start"] = start
                self.assert_error("start", record)

    def test_artifact_or_projection_evidence_proves_a_start(self):
        for start in ({"turn_start": "observed"},
                      {"source": "artifact", "evidence_ref": "evidence/commit.txt"},
                      {"source": "status_projection", "evidence_ref": "progress.json"}):
            with self.subTest(start=start):
                record = acceptance()
                record["levels"]["implemented"]["start"] = start
                self.assertEqual(self.errors(record), [], f"{start}")

    def test_a_test_check_needs_a_command_a_result_and_a_log(self):
        for broken in (
            {"name": "tests", "result": "pass", "evidence_ref": "evidence/t.log"},
            {"name": "tests", "command": "python3 -m unittest", "evidence_ref": "evidence/t.log"},
            {"name": "tests", "command": "python3 -m unittest", "result": "pass"},
            {"name": "", "command": "python3 -m unittest", "result": "pass",
             "evidence_ref": "evidence/t.log"},
            {"name": "tests", "command": "python3 -m unittest", "result": "maybe",
             "evidence_ref": "evidence/t.log"},
        ):
            with self.subTest(broken=broken):
                self.assert_error("check", acceptance(checks=[broken]))

    def test_a_grep_is_not_proof_that_a_test_ran(self):
        for command in ("grep -rn 'OK' .", "rg 'passed' build.log",
                        "git grep 'test result: ok'"):
            with self.subTest(command=command):
                record = acceptance(checks=[{
                    "name": "tests", "command": command, "result": "pass",
                    "evidence_ref": "evidence/grep.log",
                }])
                self.assert_error("grep", record)

    def test_a_review_needs_both_axes_and_a_reference(self):
        for review in (
            {"axis": ["standards"], "result": "pass", "evidence_ref": "evidence/review.md"},
            {"axis": ["standards", "spec"], "result": "pass"},
            {"axis": ["standards", "standards"], "result": "pass",
             "evidence_ref": "evidence/review.md"},
        ):
            with self.subTest(review=review):
                self.assert_error("review", acceptance(review=review))

    def test_a_waiver_records_its_reason_in_the_spec_and_the_answer(self):
        waived = {
            "axis": ["standards", "spec"],
            "result": "waiver",
            "waiver": {
                "reason": "copy-only change; the spec records the exemption",
                "spec_evidence_ref": "spec:section-3",
                "answer_evidence_ref": "tickets/task-39-answer.md#review",
            },
        }
        self.assertEqual(self.errors(acceptance(review=waived)), [])
        for key in ("spec_evidence_ref", "answer_evidence_ref"):
            with self.subTest(key=key):
                broken = json.loads(json.dumps(waived))
                broken["waiver"].pop(key)
                self.assert_error("waiver", acceptance(review=broken))
        broken = json.loads(json.dumps(waived))
        broken["waiver"] = {"spec_evidence_ref": "spec", "answer_evidence_ref": "answer"}
        self.assert_error("waiver", acceptance(review=broken))

    def test_worker_done_cannot_produce_an_acceptance(self):
        """A settlement receipt is not a start proof, so a level true on it is refused."""
        record = acceptance()
        record["levels"]["implemented"]["start"] = {
            "source": "worker_done", "evidence_ref": "evidence/worker-done.txt"}
        self.assert_error("settlement", record)

    def test_never_merge_is_a_human_terminal_state(self):
        record = acceptance(levels=open_levels())
        record["levels"]["merged"] = {
            "value": False,
            "terminal": "never_merge",
            "decided_by": "human",
            "reason": "spike: the work is deliberately not merged",
            "evidence_ref": "spec:never-merge",
            "at": utc_now(),
        }
        self.assertEqual(self.errors(record), [])
        for broken in (
            {**record["levels"]["merged"], "decided_by": "coordinator"},
            {**record["levels"]["merged"], "value": True, "merge_sha": sha("b"),
             "pr_state": "MERGED"},
            {**record["levels"]["merged"], "reason": ""},
            {**record["levels"]["merged"], "terminal": "maybe"},
        ):
            with self.subTest(broken=broken):
                candidate = acceptance(levels=open_levels())
                candidate["levels"]["merged"] = broken
                self.assert_error("terminal", candidate)

    def test_an_unknown_terminal_level_is_rejected(self):
        record = acceptance(levels=open_levels())
        record["levels"]["integrated"]["terminal"] = "never_merge"
        self.assert_error("terminal", record)


class AuthorityVerificationTests(unittest.TestCase):
    """``verify`` cross-checks the record's merge and tracker facts against authority."""

    @classmethod
    def setUpClass(cls):
        cls.module = load_module(SKILL_DIRS[0], "acceptance.py")

    def pr_host(self, **overrides):
        row = {"pr": 12, "state": "MERGED", "merge_sha": sha("b"),
               "head_sha": sha("a"), "base": "main"}
        row.update(overrides)
        return {"state": "ok", "reason": "", "pull_requests": [row]}

    def tracker(self, **overrides):
        row = {"ticket": "39", "state": "closed"}
        row.update(overrides)
        return {"state": "ok", "reason": "", "tickets": [row]}

    def git(self, **overrides):
        row = {"ancestor": sha("a"), "descendant": sha("b"), "result": True}
        row.update(overrides)
        return {"state": "ok", "reason": "", "ancestry": [row]}

    def snapshot(self, **overrides):
        sections = {"pr_host": self.pr_host(), "tracker": self.tracker(), "git": self.git()}
        sections.update(overrides)
        return authority_snapshot(**sections)

    def test_a_record_matching_authority_verifies(self):
        self.assertEqual(self.module.verify(acceptance(), self.snapshot()), [])

    def test_the_pr_host_must_report_merged_with_the_recorded_merge_sha(self):
        errors = self.module.verify(acceptance(), self.snapshot(
            pr_host=self.pr_host(state="OPEN")))
        self.assertTrue(any("MERGED" in error for error in errors), errors)
        errors = self.module.verify(acceptance(), self.snapshot(
            pr_host=self.pr_host(merge_sha=sha("c"))))
        self.assertTrue(any("merge_sha" in error for error in errors), errors)

    def test_the_ancestry_check_must_hold(self):
        errors = self.module.verify(acceptance(), self.snapshot(
            git=self.git(result=False)))
        self.assertTrue(any("ancestry" in error for error in errors), errors)
        errors = self.module.verify(acceptance(), self.snapshot(
            git={"state": "ok", "reason": "", "ancestry": []}))
        self.assertTrue(any("ancestry" in error for error in errors), errors)

    def test_the_tracker_close_must_be_read_back(self):
        errors = self.module.verify(acceptance(), self.snapshot(
            tracker=self.tracker(state="open")))
        self.assertTrue(any("tracker" in error for error in errors), errors)

    def test_an_unreadable_source_cannot_prove_a_level(self):
        errors = self.module.verify(acceptance(), self.snapshot(
            pr_host={"state": "unknown", "reason": "PR host unreachable"}))
        self.assertTrue(any("PR host" in error for error in errors), errors)

    def test_verification_is_skipped_for_levels_that_are_not_claimed(self):
        record = acceptance(levels=open_levels())
        self.assertEqual(self.module.verify(record, self.snapshot(
            pr_host={"state": "unknown", "reason": "unreachable"})), [])

    def test_the_fake_host_adapter_serves_the_snapshot(self):
        """The shared fake is the seam; nothing here reaches a live PR host."""
        host = FAKE.FakeHost(self.pr_host())
        section = host.snapshot(RUN_ID, [])
        self.assertEqual(section["pull_requests"][0]["state"], "MERGED")


class AggregationTests(unittest.TestCase):
    """The ticket-level levels aggregate per-PR records with the documented rule."""

    @classmethod
    def setUpClass(cls):
        cls.module = load_module(SKILL_DIRS[0], "acceptance.py")

    def ledger_record(self, payload, kind="acceptance", sequence=0):
        return {
            "schema_version": 1,
            "record_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"fixture-{sequence}")),
            "kind": kind,
            "run_id": RUN_ID,
            "subject": {"ticket": payload.get("ticket", "39"),
                        "pr": payload.get("pr") if isinstance(payload.get("pr"), int) else 12},
            "action": "acceptance",
            "request_id": "",
            "idempotency": "none",
            "state": "completed" if kind == "acceptance" else "observed",
            "evidence_ref": "",
            "observed_at": utc_now(),
            "actor": "coordinator",
            "detail": payload,
        }

    def pr_record(self, number, accepted_sha=None, levels=None):
        record = acceptance(pr={"number": number, "head_at_acceptance": sha("a"),
                                "base": "main"})
        if accepted_sha:
            record["accepted_sha"] = accepted_sha
        if levels is not None:
            record["levels"] = levels
        return record

    def observed(self, level, pr=12, observation="revert"):
        payload = observation_record(level=level, pr=pr, observation=observation)
        return self.ledger_record(payload, kind="observation", sequence=99)

    def test_one_pr_supports_the_ticket_levels(self):
        ticket = self.module.aggregate("39", [self.ledger_record(acceptance())])
        for level in LEVELS:
            self.assertTrue(ticket["levels"][level]["value"], level)
        self.assertFalse(ticket["divergent"])
        self.assertIsNone(ticket["terminal"])

    def test_every_linked_pr_must_support_the_level(self):
        first = self.ledger_record(self.pr_record(12), sequence=1)
        weak_levels = dict(acceptance()["levels"])
        weak_levels["merged"] = {"value": False, "at": utc_now()}
        second = self.ledger_record(self.pr_record(13, levels=weak_levels), sequence=2)
        ticket = self.module.aggregate("39", [first, second])
        self.assertFalse(ticket["levels"]["merged"]["value"])
        self.assertTrue(ticket["levels"]["implemented"]["value"])
        self.assertIn("multi_pr", ticket["provisional"])
        self.assertEqual([pr["number"] for pr in ticket["prs"]], [12, 13])

    def test_a_revert_appends_and_never_rewrites_the_acceptance(self):
        accepted = self.ledger_record(acceptance(), sequence=1)
        reverted = self.observed("merged")
        ticket = self.module.aggregate("39", [accepted, reverted])
        merged = ticket["prs"][0]["levels"]["merged"]
        self.assertFalse(merged["value"])
        self.assertEqual(merged["evidence_ref"], "evidence/pr-12.json")
        self.assertEqual(merged["last_observation"]["observation"], "revert")
        self.assertTrue(ticket["levels"]["implemented"]["value"])
        self.assertFalse(ticket["levels"]["merged"]["value"])
        self.assertIn("revert", ticket["provisional"])

    def test_a_later_attempt_supersedes_an_earlier_one(self):
        """A re-acceptance at a new head is a new attempt, not a conflict."""
        first = self.ledger_record(self.pr_record(12, accepted_sha=sha("a")), sequence=1)
        later_levels = dict(acceptance()["levels"])
        later_levels["merged"] = {
            "value": True, "evidence_ref": "evidence/pr-12-attempt2.json",
            "merge_sha": sha("d"), "pr_state": "MERGED",
            "ancestry": {"ancestor": sha("c"), "descendant": sha("d"), "result": True},
            "at": utc_now(),
        }
        second = self.ledger_record(
            self.pr_record(12, accepted_sha=sha("c"), levels=later_levels), sequence=2)
        second["subject"]["attempt"] = 2
        second["detail"]["attempt"] = 2
        ticket = self.module.aggregate("39", [first, second])
        self.assertFalse(ticket["prs"][0]["divergent"])
        self.assertEqual(ticket["prs"][0]["attempt"], 2)
        self.assertEqual(ticket["prs"][0]["accepted_sha"], sha("c"))
        self.assertEqual(ticket["levels"]["merged"]["value"], True)
        self.assertEqual(len(ticket["prs"][0]["records"]), 2)

    def test_conflicting_records_at_the_same_attempt_are_divergent(self):
        levels = open_levels()
        levels["implemented"] = {
            "value": True, "evidence_ref": "evidence/a.md", "at": utc_now(),
            "start": {"turn_start": "observed"},
        }
        first = self.ledger_record(self.pr_record(12, accepted_sha=sha("a"), levels=levels),
                                   sequence=1)
        second = self.ledger_record(self.pr_record(12, accepted_sha=sha("c"), levels=levels),
                                    sequence=2)
        ticket = self.module.aggregate("39", [first, second])
        self.assertTrue(ticket["divergent"])
        self.assertTrue(ticket["prs"][0]["divergent"])
        self.assertFalse(ticket["levels"]["implemented"]["value"])

    def test_never_merge_is_terminal_and_human(self):
        levels = open_levels()
        levels["merged"] = {
            "value": False, "terminal": "never_merge", "decided_by": "human",
            "reason": "spike", "evidence_ref": "spec:never-merge", "at": utc_now(),
        }
        ticket = self.module.aggregate("39", [
            self.ledger_record(acceptance(levels=levels), sequence=1)])
        self.assertEqual(ticket["terminal"], "never_merge")
        self.assertEqual(ticket["prs"][0]["levels"]["merged"]["decided_by"], "human")
        self.assertIn("never_merge", ticket["provisional"])

    def test_an_unrelated_ticket_is_not_aggregated(self):
        other = acceptance(ticket="40")
        ticket = self.module.aggregate("39", [self.ledger_record(other)])
        self.assertEqual(ticket["levels"]["implemented"]["value"], False)
        self.assertEqual(ticket["prs"], [])

    def test_the_bundle_copy_aggregates_identically(self):
        chinese = load_module(SKILL_DIRS[1], "acceptance.py")
        records = [self.ledger_record(acceptance(), sequence=1), self.observed("merged")]
        self.assertEqual(chinese.aggregate("39", records),
                         self.module.aggregate("39", records))


class CliTests(unittest.TestCase):
    """The product surface: the real script against a temporary ledger home."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "ledger-home"
        self.env = {
            **os.environ,
            "HOME": str(self.root / "home"),
            "ORCA_LEDGER_HOME": str(self.home),
        }
        self.env.pop("ORCA_LEDGER_RETENTION_DAYS", None)
        self.env.pop("ORCA_LEDGER_RETENTION_BYTES", None)

    def run_cli(self, *args, stdin=None):
        return subprocess.run(
            [sys.executable, str(SKILL_DIRS[0] / SCRIPT_RELATIVE), *args],
            env=self.env, cwd=str(self.root), input=stdin, text=True,
            capture_output=True,
        )

    def record(self, payload=None, *extra, ok=True):
        result = self.run_cli(
            "record", "--run-id", RUN_ID, "--record",
            json.dumps(payload or acceptance()), *extra)
        self.assertEqual(result.returncode == 0, ok, result.stderr + result.stdout)
        return json.loads(result.stdout)

    def show(self, ticket="39"):
        result = self.run_cli("show", "--run-id", RUN_ID, "--ticket", ticket)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def observe(self, payload, ok=True):
        result = self.run_cli("observe", "--run-id", RUN_ID,
                              "--record", json.dumps(payload))
        self.assertEqual(result.returncode == 0, ok, result.stderr + result.stdout)
        return json.loads(result.stdout)

    def ledger_records(self):
        path = self.home / "runs" / RUN_ID / "ledger.jsonl"
        if not path.exists() or path.is_symlink():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line]

    def test_a_record_is_written_shown_and_survives_a_restart(self):
        written = self.record()
        self.assertTrue(written["recorded"])
        self.assertTrue(written["gate_passed"])
        self.assertEqual(written["answer_location"], "tickets/task-39-answer.md")

        shown = self.show()
        ticket = shown["tickets"]["39"]
        for level in LEVELS:
            self.assertTrue(ticket["levels"][level]["value"], level)
        self.assertEqual(ticket["prs"][0]["levels"]["merged"]["merge_sha"], sha("b"))
        self.assertEqual(ticket["levels"]["tracker_closed"]["evidence_refs"],
                         ["evidence/tracker-39.json"])
        self.assertEqual(shown["records"][0]["kind"], "acceptance")
        self.assertEqual(shown["records"][0]["detail"]["answer_location"],
                         "tickets/task-39-answer.md")
        self.assertEqual(self.ledger_records()[0]["kind"], "acceptance")

    def test_worker_done_alone_is_refused_by_the_cli(self):
        record = acceptance()
        record["levels"]["implemented"]["start"] = {
            "source": "worker_done", "evidence_ref": "evidence/worker-done.txt"}
        result = self.record(record, ok=False)
        self.assertFalse(result["recorded"])
        self.assertFalse(result["gate_passed"])
        self.assertEqual(self.ledger_records(), [])

    def test_a_waiver_without_both_locations_is_refused_by_the_cli(self):
        record = acceptance()
        record["review"] = {
            "axis": ["standards", "spec"],
            "result": "waiver",
            "waiver": {"reason": "copy only", "spec_evidence_ref": "spec:3"},
        }
        result = self.record(record, ok=False)
        self.assertFalse(result["recorded"])
        self.assertTrue(any("waiver" in error for error in result["errors"]))

    def test_the_ledger_record_is_an_index_not_a_copy_of_the_claim(self):
        self.record()
        stored = self.ledger_records()[0]
        self.assertEqual(stored["kind"], "acceptance")
        self.assertEqual(stored["action"], "acceptance")
        self.assertEqual(stored["state"], "completed")
        self.assertEqual(stored["subject"],
                         {"ticket": "39", "pr": 12, "attempt": 1})
        self.assertEqual(stored["evidence_ref"], "tickets/task-39-answer.md")
        self.assertEqual(stored["detail"]["answer_location"],
                         "tickets/task-39-answer.md")

    def test_a_later_process_finds_the_answer_location(self):
        self.record()
        shown = self.show()
        self.assertEqual(shown["records"][0]["detail"]["answer_location"],
                         "tickets/task-39-answer.md")

    def test_an_invalid_record_writes_nothing_and_never_passes_the_gate(self):
        record = acceptance()
        record["levels"]["merged"].pop("evidence_ref")
        result = self.record(record, ok=False)
        self.assertFalse(result["recorded"])
        self.assertFalse(result["gate_passed"])
        self.assertTrue(result["errors"])
        self.assertEqual(self.ledger_records(), [])

    def test_an_authority_mismatch_is_refused(self):
        authority_file = self.root / "authority.json"
        authority_file.write_text(json.dumps(authority_snapshot(
            pr_host={"state": "ok", "reason": "", "pull_requests": [
                {"pr": 12, "state": "OPEN", "head_sha": sha("a"), "base": "main"}]},
            tracker={"state": "ok", "reason": "", "tickets": [
                {"ticket": "39", "state": "closed"}]},
            git={"state": "ok", "reason": "", "ancestry": [
                {"ancestor": sha("a"), "descendant": sha("b"), "result": True}]},
        )))
        result = self.record(acceptance(), "--authority", str(authority_file), ok=False)
        self.assertFalse(result["recorded"])
        self.assertFalse(result["gate_passed"])
        self.assertEqual(self.ledger_records(), [])

    def test_a_write_failure_is_recorded_and_retried(self):
        run_dir = self.home / "runs" / RUN_ID
        run_dir.mkdir(parents=True)
        outside = self.root / "elsewhere.jsonl"
        outside.write_text("")
        (run_dir / "ledger.jsonl").symlink_to(outside)

        failed = self.record(ok=False)
        self.assertFalse(failed["recorded"])
        self.assertFalse(failed["gate_passed"])
        self.assertIn("acceptance-pending", failed["retry_path"])
        self.assertTrue(Path(failed["retry_path"]).is_file())

        (run_dir / "ledger.jsonl").unlink()
        retried = self.run_cli("retry", "--run-id", RUN_ID)
        self.assertEqual(retried.returncode, 0, retried.stderr)
        outcome = json.loads(retried.stdout)
        self.assertEqual(outcome["pending"], 0)
        self.assertTrue(outcome["retried"][0]["recorded"])
        records = self.ledger_records()
        self.assertEqual([record["kind"] for record in records], ["acceptance"])
        self.assertEqual(records[0]["record_id"], failed["record_id"])

    def test_a_retry_of_the_same_record_is_idempotent(self):
        first = self.record()
        second = self.record()
        self.assertEqual(first["record_id"], second["record_id"])
        self.assertEqual(len(self.ledger_records()), 1)

    def test_multi_pr_revert_and_never_merge_are_shown(self):
        self.record(acceptance())
        second = acceptance(pr={"number": 13, "head_at_acceptance": sha("a"),
                                "base": "main"})
        second["levels"]["merged"] = {
            "value": False, "terminal": "never_merge", "decided_by": "human",
            "reason": "spike", "evidence_ref": "spec:never-merge", "at": utc_now(),
        }
        second["levels"]["tracker_closed"] = {"value": False, "at": utc_now()}
        self.record(second)

        reverted = self.observe(observation_record(level="merged", pr=12))
        self.assertTrue(reverted["recorded"])

        ticket = self.show()["tickets"]["39"]
        self.assertEqual([pr["number"] for pr in ticket["prs"]], [12, 13])
        self.assertIn("multi_pr", ticket["provisional"])
        self.assertEqual(ticket["terminal"], "never_merge")
        self.assertFalse(ticket["levels"]["merged"]["value"])
        self.assertEqual(ticket["prs"][0]["levels"]["merged"]["last_observation"]["observation"],
                         "revert")
        self.assertEqual(ticket["prs"][0]["levels"]["merged"]["evidence_ref"],
                         "evidence/pr-12.json")
        self.assertEqual(len([record for record in self.ledger_records()
                              if record["kind"] == "acceptance"]), 2)
        self.assertEqual(len([record for record in self.ledger_records()
                              if record["kind"] == "observation"]), 1)

    def test_open_questions_are_pending_with_evidence_and_need_no_human_reply(self):
        first = self.run_cli("open-questions", "--run-id", RUN_ID)
        self.assertEqual(first.returncode, 0, first.stderr)
        items = json.loads(first.stdout)["items"]
        self.assertEqual(len(items), 4)
        self.assertEqual({item["status"] for item in items}, {"pending"})
        for item in items:
            self.assertTrue(item["provisional"])
            self.assertTrue(item["evidence_ref"])

        second = self.run_cli("open-questions", "--run-id", RUN_ID)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout)["items"], items)
        pending = [record for record in self.ledger_records()
                   if record["kind"] == "pending"]
        self.assertEqual(len(pending), 4)
        self.assertTrue(all(record["state"] == "pending" for record in pending))

        resolved = self.run_cli(
            "resolve-question", "--run-id", RUN_ID,
            "--question-id", items[0]["question_id"],
            "--reason", "the human confirmed the multi-PR rule",
            "--evidence-ref", "issue:20#comment-1")
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        self.assertEqual(json.loads(resolved.stdout)["status"], "resolved")
        kinds = [record["kind"] for record in self.ledger_records()]
        self.assertEqual(kinds.count("pending"), 4)
        self.assertEqual(kinds.count("observation"), 1)


if __name__ == "__main__":
    unittest.main()
