#!/usr/bin/env python3
"""Behaviour tests for ``closeout.py``: the ten-row checklist and the release gate.

The declared seams follow ``tests/test_acceptance.py`` and the spec's hybrid seam:

* **Module import** for ``plan(facts)`` and ``release_decision(...)``. The planner takes a
  facts object, returns a checklist and touches no disk, clock or network; the release
  decision consumes ``health.authorize_release`` rather than restating its rules.
* **Module reconciliation** for ``reconcile(run_id, authority, home, answer_location)``
  with an injected facts adapter and a temporary ``$ORCA_LEDGER_HOME`` outside every
  repository. Storage is ``ledger.py`` and every acceptance record is one
  ``acceptance.py`` would write.
* **CLI end to end** for ``plan``, ``reconcile`` and ``show`` as a subprocess.

Nothing here calls a live Orca, a PR host, a tracker, the network or a credential. The
shared fake in ``tests/fake_orca.py`` serves the PR-host section; an Orca release receipt
shape is never asserted as a stable contract.
"""

import copy
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
SCRIPT_RELATIVE = Path("scripts") / "closeout.py"
RUN_ID = "run_alpha"
ACCEPTED = "a" * 40
MERGE = "b" * 40
MAIN_HEAD = "e" * 40
ACCEPTANCE_RECORD_ID = "11111111-1111-4111-8111-111111111111"
PENDING_RECORD_ID = "22222222-2222-4222-8222-222222222222"


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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
    path = Path(__file__).resolve().parent / "fake_orca.py"
    spec = importlib.util.spec_from_file_location("fake_orca", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot build an import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FAKE = load_fake_orca()


def acceptance_record(**overrides):
    """A complete acceptance record: the one shape ``acceptance.validate`` accepts."""
    at = utc_now()
    record = {
        "ticket": "39",
        "gate": ["G-impl"],
        "attempt": 1,
        "accepted_sha": ACCEPTED,
        "accepted_at": at,
        "pr": {"number": 12, "head_at_acceptance": ACCEPTED, "base": "main"},
        "checks": [{
            "name": "closeout tests",
            "command": "python3 -m unittest tests.test_closeout",
            "result": "pass",
            "evidence_ref": "evidence/closeout-tests.log",
        }],
        "review": {
            "axis": ["standards", "spec"],
            "result": "pass",
            "evidence_ref": "evidence/code-review.md",
        },
        "levels": {
            "implemented": {
                "value": True, "evidence_ref": "evidence/answer.md#implemented",
                "at": at, "start": {"turn_start": "observed"},
            },
            "integrated": {"value": True, "evidence_ref": "evidence/ancestry.txt", "at": at},
            "merged": {
                "value": True, "evidence_ref": "evidence/pr-12.json", "at": at,
                "merge_sha": MERGE, "pr_state": "MERGED",
                "ancestry": {"ancestor": ACCEPTED, "descendant": MERGE, "result": True},
            },
            "tracker_closed": {
                "value": True, "evidence_ref": "evidence/tracker-39.json", "at": at,
                "read_back": {"state": "closed", "at": at},
            },
        },
        "decided_by": "coordinator",
        "decided_at": at,
        "answer_location": "tickets/task-39-answer.md",
    }
    record.update(overrides)
    return record


def facts(**overrides):
    """A complete facts object: every row can be proven from it."""
    at = utc_now()
    data = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "observed_at": at,
        "answer_location": "tickets/task-40-answer.md",
        "acceptance": {
            "state": "ok", "reason": "",
            "records": [{"record_id": ACCEPTANCE_RECORD_ID, **acceptance_record()}],
        },
        "observations": {"state": "ok", "reason": "", "records": []},
        "pending": {"state": "ok", "reason": "", "items": []},
        "pr_host": {"state": "ok", "reason": "", "pull_requests": [{
            "pr": 12, "state": "MERGED", "base": "main",
            "head_sha": ACCEPTED, "merge_sha": MERGE,
        }]},
        "git": {
            "state": "ok", "reason": "",
            "ancestry": [
                {"ancestor": ACCEPTED, "descendant": MERGE, "result": True},
                {"ancestor": MERGE, "descendant": MAIN_HEAD, "result": True},
            ],
            "main": {
                "branch": "main", "local_branch": "main",
                "head": MAIN_HEAD, "local_head": MAIN_HEAD,
                "clean": True, "upstream": "origin/main", "synced": True,
            },
        },
        "tickets": {"state": "ok", "reason": "", "tickets": [
            {"ticket": "39", "integrated": True, "evidence_ref": "git:ancestry:39"},
        ]},
        "tracker": {"state": "ok", "reason": "", "tickets": [
            {"ticket": "39", "state": "closed", "closed_at": at},
        ]},
        "workers": {"state": "ok", "reason": "", "rows": [{
            "dispatch_id": "dispatch-1", "task_id": "task-39", "attempt": 1,
            "worktree": "worktrees/task-39", "worker_state": "released", "settled": True,
        }]},
        "dashboard": {"state": "ok", "reason": "", "gap": None},
        "release": {"state": "ok", "verification": {
            "source": "worker-release --help (fixture)",
            "at": at,
            "version_scope": "help text only; replay beyond the help stays an assumption",
        }},
    }
    data.update(overrides)
    return data


def pending_item(**overrides):
    item = {
        "record_id": PENDING_RECORD_ID,
        "pending_type": "human_merge",
        "pr": 12,
        "accepted_sha": ACCEPTED,
        "owner": "human",
        "condition": "PR merged",
        "check_after": utc_now(),
        "status": "pending",
    }
    item.update(overrides)
    return item


def row(checklist, index):
    return next(item for item in checklist["rows"] if item["id"] == index)


class FactsAuthority:
    """An injected facts adapter: it serves the recorded sections and no network."""

    def __init__(self, **sections):
        self.sections = sections

    def facts(self, run_id, records):
        data = copy.deepcopy(self.sections)
        data.setdefault("run_id", run_id)
        return data


class ChecklistShapeTests(unittest.TestCase):
    """``plan(facts)`` is a pure planner over authority facts."""

    @classmethod
    def setUpClass(cls):
        cls.module = load_module(SKILL_DIRS[0], "closeout.py")

    def test_the_checklist_has_the_ten_documented_rows(self):
        checklist = self.module.plan(facts())
        self.assertEqual([item["id"] for item in checklist["rows"]],
                         list(range(1, 11)))
        facts_named = [item["fact"] for item in checklist["rows"]]
        self.assertEqual(len(facts_named), 10)
        self.assertIn("accepted SHA", facts_named[0])
        self.assertIn("merge SHA", facts_named[1])
        self.assertIn("local main", facts_named[4])
        self.assertIn("worker", facts_named[7])
        self.assertIn("dashboard", facts_named[8])
        self.assertIn("one-way-door", facts_named[9])

    def test_every_row_names_authority_repeatability_and_action(self):
        checklist = self.module.plan(facts())
        for item in checklist["rows"]:
            with self.subTest(row=item["id"]):
                for key in ("fact", "authority", "action"):
                    self.assertIsInstance(item[key], str)
                    self.assertTrue(item[key].strip(), item["id"])
                self.assertIsInstance(item["repeatable"], bool)
                self.assertIn(item["status"],
                              ("ok", "blocked", "pending", "gap", "unknown"))

    def test_only_the_accepted_sha_row_is_non_repeatable(self):
        checklist = self.module.plan(facts())
        self.assertEqual(checklist["non_repeatable"], [1])
        self.assertFalse(row(checklist, 1)["repeatable"])
        for index in range(2, 11):
            self.assertTrue(row(checklist, index)["repeatable"], index)

    def test_the_planner_does_not_mutate_its_input(self):
        data = facts()
        snapshot = json.loads(json.dumps(data))
        self.module.plan(data)
        self.assertEqual(data, snapshot)

    def test_malformed_facts_are_rejected(self):
        missing = {key: value for key, value in facts().items() if key != "schema_version"}
        for broken in (missing, facts(schema_version=2), facts(bogus={"state": "ok"})):
            with self.subTest(broken=sorted(broken)[:3]):
                with self.assertRaises(ValueError):
                    self.module.plan(broken)
        self.module.plan(facts())  # the control still plans

    def test_a_release_verification_asserts_only_source_time_and_scope(self):
        checklist = self.module.plan(facts())
        verification = checklist["release_verification"]
        self.assertEqual(set(verification),
                         {"source", "at", "version_scope"})
        broken = facts()
        broken["release"]["verification"]["received"] = {"state": "completed"}
        with self.assertRaises(ValueError):
            self.module.plan(broken)

    def test_the_bundle_copy_plans_identically(self):
        chinese = load_module(SKILL_DIRS[1], "closeout.py")
        self.assertEqual(chinese.plan(facts()), self.module.plan(facts()))


class RowOneToFiveTests(unittest.TestCase):
    """Rows 1-5: acceptance, PR host, ancestry, main contains the merge, local sync."""

    @classmethod
    def setUpClass(cls):
        cls.module = load_module(SKILL_DIRS[0], "closeout.py")

    def test_without_a_persisted_acceptance_record_row_one_blocks(self):
        checklist = self.module.plan(facts(acceptance={
            "state": "ok", "reason": "", "records": []}))
        self.assertEqual(row(checklist, 1)["status"], "blocked")
        self.assertIn("persisted", " ".join(row(checklist, 1)["detail"]["problems"]))
        self.assertIn(1, checklist["blocking"])
        self.assertEqual(checklist["classification"], "acceptance_missing")
        self.assertFalse(checklist["ready"])

    def test_an_invalid_acceptance_record_is_not_accepted_as_evidence(self):
        record = acceptance_record()
        record["levels"]["merged"]["merge_sha"] = "not-a-sha"
        checklist = self.module.plan(facts(acceptance={
            "state": "ok", "reason": "",
            "records": [{"record_id": ACCEPTANCE_RECORD_ID, **record}]}))
        self.assertEqual(row(checklist, 1)["status"], "blocked")
        problems = " ".join(row(checklist, 1)["detail"]["problems"])
        self.assertIn("merge_sha", problems)
        self.assertIn("merge_sha", " ".join(row(checklist, 2)["detail"]["problems"]))

    def test_row_one_names_the_answer_location_of_each_accepted_record(self):
        checklist = self.module.plan(facts())
        entries = row(checklist, 1)["detail"]["entries"]
        self.assertEqual(entries[0]["answer_location"], "tickets/task-39-answer.md")
        self.assertEqual(entries[0]["record_id"], ACCEPTANCE_RECORD_ID)
        self.assertEqual(entries[0]["accepted_sha"], ACCEPTED)

    def test_row_two_needs_the_pr_host_merged_state_base_and_merge_sha(self):
        for broken in (
            {"state": "OPEN", "base": "main", "merge_sha": MERGE},
            {"state": "MERGED", "base": "release", "merge_sha": MERGE},
            {"state": "MERGED", "base": "main"},
        ):
            with self.subTest(broken=broken):
                row_fact = {"pr": 12, "head_sha": ACCEPTED, **broken}
                checklist = self.module.plan(facts(pr_host={
                    "state": "ok", "reason": "", "pull_requests": [row_fact]}))
                self.assertEqual(row(checklist, 2)["status"], "blocked")
                self.assertIn(2, checklist["blocking"])
        checklist = self.module.plan(facts(pr_host={
            "state": "ok", "reason": "", "pull_requests": []}))
        self.assertEqual(row(checklist, 2)["status"], "blocked")
        self.assertEqual(checklist["classification"], "pr_not_merged")

    def test_row_three_requires_ancestry_from_the_accepted_head_to_the_merge(self):
        git = facts()["git"]
        git["ancestry"] = [{"ancestor": ACCEPTED, "descendant": MERGE, "result": False}]
        checklist = self.module.plan(facts(git=git))
        self.assertEqual(row(checklist, 3)["status"], "blocked")
        self.assertTrue(row(checklist, 3)["detail"]["escalate"])
        self.assertEqual(checklist["classification"], "ancestry_divergent")

    def test_row_four_requires_main_to_contain_the_merge(self):
        git = facts()["git"]
        git["ancestry"] = [{"ancestor": ACCEPTED, "descendant": MERGE, "result": True}]
        checklist = self.module.plan(facts(git=git))
        self.assertEqual(row(checklist, 4)["status"], "blocked")
        self.assertIn("does not contain",
                      " ".join(row(checklist, 4)["detail"]["problems"]))
        self.assertEqual(checklist["classification"], "main_missing_merge")

    def test_row_five_blocks_later_merges_while_local_main_is_unsynced(self):
        for main in (
            {"branch": "main", "local_branch": "main", "head": MAIN_HEAD,
             "local_head": "c" * 40, "clean": True, "upstream": "origin/main",
             "synced": False},
            {"branch": "main", "local_branch": "main", "head": MAIN_HEAD,
             "local_head": MAIN_HEAD, "clean": False, "upstream": "origin/main",
             "synced": True},
            {"branch": "main", "local_branch": "feature", "head": MAIN_HEAD,
             "local_head": MAIN_HEAD, "clean": True, "upstream": "origin/main",
             "synced": True},
        ):
            with self.subTest(main=main):
                git = facts()["git"]
                git["main"] = main
                checklist = self.module.plan(facts(git=git))
                self.assertEqual(row(checklist, 5)["status"], "blocked")
                self.assertTrue(checklist["merge_blocked"])
                self.assertEqual(checklist["classification"], "local_main_unsynced")
        git = facts()["git"]
        git["main"] = {"branch": "main", "local_branch": "main", "head": MAIN_HEAD,
                       "local_head": MAIN_HEAD, "clean": True,
                       "upstream": "origin/main", "synced": False}
        checklist = self.module.plan(facts(git=git))
        self.assertIn("pause later merges", row(checklist, 5)["detail"]["action"])

    def test_an_unreadable_authority_is_unknown_not_a_failure(self):
        checklist = self.module.plan(facts(pr_host={
            "state": "unknown", "reason": "PR host unreachable"}))
        self.assertEqual(row(checklist, 2)["status"], "unknown")
        self.assertIn(2, checklist["blocking"])
        self.assertEqual(checklist["classification"], "authority_unknown")
        self.assertFalse(checklist["ready"])
        self.assertIn("PR host unreachable", row(checklist, 2)["detail"]["reason"])

    def test_an_unreadable_local_main_also_pauses_later_merges(self):
        git = facts()["git"]
        git.pop("main")
        checklist = self.module.plan(facts(git=git))
        self.assertEqual(row(checklist, 5)["status"], "blocked")
        self.assertTrue(checklist["merge_blocked"])

    def test_an_unsynced_local_main_also_blocks_all_later_merges(self):
        git = facts()["git"]
        git["main"]["synced"] = False
        checklist = self.module.plan(facts(git=git))
        self.assertTrue(checklist["merge_blocked"])
        self.assertIn(5, checklist["blocking"])


class RowSixToEightTests(unittest.TestCase):
    """Rows 6-8: every ticket integrated, tracker read back, worker lifecycle."""

    @classmethod
    def setUpClass(cls):
        cls.module = load_module(SKILL_DIRS[0], "closeout.py")

    def test_row_six_requires_every_acceptance_ticket_to_be_integrated(self):
        for tickets in (
            {"state": "ok", "reason": "", "tickets": []},
            {"state": "ok", "reason": "", "tickets": [
                {"ticket": "39", "integrated": False, "evidence_ref": "git:39"}]},
        ):
            with self.subTest(tickets=tickets):
                checklist = self.module.plan(facts(tickets=tickets))
                self.assertEqual(row(checklist, 6)["status"], "blocked")
                self.assertEqual(checklist["classification"], "ticket_not_integrated")
        checklist = self.module.plan(facts(tickets={
            "state": "unknown", "reason": "integration branch unreadable"}))
        self.assertEqual(row(checklist, 6)["status"], "unknown")

    def test_row_seven_reads_the_tracker_back_and_never_closes_it(self):
        checklist = self.module.plan(facts(tracker={
            "state": "ok", "reason": "",
            "tickets": [{"ticket": "39", "state": "open"}]}))
        self.assertEqual(row(checklist, 7)["status"], "blocked")
        self.assertEqual(checklist["classification"], "tracker_unclosed")
        self.assertIn("escalate", row(checklist, 7)["action"])
        # The planner reports; it does not write to the tracker.
        self.assertTrue(all("tracker" != action for item in checklist["rows"]
                            for action in item["evidence"]))

    def test_row_seven_records_a_reopen_observation(self):
        observations = {"state": "ok", "reason": "", "records": [{
            "record_id": str(uuid.uuid4()), "ticket": "39", "pr": 12,
            "level": "tracker_closed", "observation": "reopen",
            "reason": "the tracker was reopened", "evidence_ref": "evidence/reopen.txt",
            "observed_at": utc_now(),
        }]}
        checklist = self.module.plan(facts(observations=observations))
        self.assertEqual(row(checklist, 7)["status"], "blocked")
        self.assertEqual(checklist["classification"], "tracker_reopened")
        self.assertEqual(row(checklist, 7)["detail"]["reopened"]["evidence_ref"],
                         "evidence/reopen.txt")

    def test_row_eight_enumerates_reclaimable_workers(self):
        workers = {"state": "ok", "reason": "", "rows": [
            {"dispatch_id": "dispatch-1", "worker_state": "released", "settled": True},
            {"dispatch_id": "dispatch-2", "worker_state": "reclaimable"},
            {"dispatch_id": "dispatch-3", "worker_state": "release_unknown"},
            {"dispatch_id": "dispatch-4", "worker_state": "active"},
            {"dispatch_id": "dispatch-5", "worker_state": "retained"},
        ]}
        checklist = self.module.plan(facts(workers=workers))
        item = row(checklist, 8)
        self.assertEqual(item["status"], "blocked")
        self.assertEqual([entry["dispatch_id"] for entry in item["detail"]["reclaimable"]],
                         ["dispatch-2", "dispatch-3"])
        self.assertEqual([entry["dispatch_id"] for entry in item["detail"]["retained"]],
                         ["dispatch-5"])
        self.assertEqual(item["detail"]["read_back_required"], ["dispatch-3"])
        self.assertIn(8, checklist["blocking"])
        self.assertEqual(checklist["classification"], "workers_reclaimable")

    def test_a_retained_worker_is_a_decision_not_unfinished_work(self):
        workers = {"state": "ok", "reason": "", "rows": [
            {"dispatch_id": "dispatch-1", "worker_state": "released", "settled": True},
            {"dispatch_id": "dispatch-5", "worker_state": "retained"},
        ]}
        checklist = self.module.plan(facts(workers=workers))
        self.assertEqual(row(checklist, 8)["status"], "ok")
        self.assertTrue(checklist["ready"])

    def test_a_released_worker_settles_row_eight(self):
        checklist = self.module.plan(facts())
        self.assertEqual(row(checklist, 8)["status"], "ok")
        self.assertEqual(row(checklist, 8)["detail"]["reclaimable"], [])


class RowNineToTenTests(unittest.TestCase):
    """Rows 9-10: the dashboard projection is a gap, never evidence; pending stays visible."""

    @classmethod
    def setUpClass(cls):
        cls.module = load_module(SKILL_DIRS[0], "closeout.py")

    def test_row_nine_records_a_gap_only_and_never_blocks(self):
        for dashboard in (
            {"state": "ok", "reason": "", "gap": None},
            {"state": "ok", "reason": "", "gap": {"reason": "dashboard write failed"}},
            {"state": "unknown", "reason": "projection unreadable"},
        ):
            with self.subTest(dashboard=dashboard):
                checklist = self.module.plan(facts(dashboard=dashboard))
                item = row(checklist, 9)
                self.assertEqual(item["status"], "gap")
                self.assertEqual(item["evidence"], [])
                self.assertFalse(item["detail"]["authoritative"])
                self.assertTrue(checklist["ready"], checklist["blocking"])
                self.assertNotIn(9, checklist["blocking"])

    def test_a_pending_one_way_door_blocks_the_declaration_until_reconciled(self):
        pending = {"state": "ok", "reason": "", "items": [pending_item()]}
        checklist = self.module.plan(facts(pending=pending))
        self.assertEqual(row(checklist, 10)["status"], "pending")
        self.assertIn(10, checklist["blocking"])
        self.assertFalse(checklist["ready"])
        item = row(checklist, 10)["detail"]["items"][0]
        self.assertTrue(item["due"])
        self.assertTrue(item["reconcilable"])
        self.assertEqual(checklist["classification"], "pending_one_way_door")

    def test_an_unmet_pending_item_stays_visible_with_its_failed_check(self):
        pending = {"state": "ok", "reason": "", "items": [pending_item()]}
        checklist = self.module.plan(facts(
            pending=pending,
            pr_host={"state": "ok", "reason": "", "pull_requests": [
                {"pr": 12, "state": "OPEN", "base": "main", "head_sha": ACCEPTED}]}))
        item = row(checklist, 10)["detail"]["items"][0]
        self.assertFalse(item["reconcilable"])
        self.assertFalse(item["checks"]["pr_merged"])
        self.assertEqual(row(checklist, 10)["status"], "pending")

    def test_a_non_door_pending_item_stays_visible_without_blocking(self):
        pending = {"state": "ok", "reason": "", "items": [
            {"record_id": "33333333-3333-4333-8333-333333333333",
             "pending_type": "pending", "condition": "an open question",
             "check_after": utc_now(), "status": "pending"}]}
        checklist = self.module.plan(facts(pending=pending))
        self.assertEqual(row(checklist, 10)["status"], "ok")
        self.assertTrue(checklist["ready"])
        item = row(checklist, 10)["detail"]["items"][0]
        self.assertFalse(item["one_way_door"])
        self.assertNotIn("checks", item)

    def test_a_resolved_pending_item_no_longer_blocks(self):
        pending = {"state": "ok", "reason": "", "items": [
            pending_item(resolved=True, checks={
                "pr_merged": True, "merge_sha": True,
                "main_synced": True, "tracker_closed": True})]}
        checklist = self.module.plan(facts(pending=pending))
        self.assertEqual(row(checklist, 10)["status"], "ok")
        self.assertTrue(checklist["ready"])


class RevertReopenTests(unittest.TestCase):
    """A revert/reopen observation from T09 is consumed, never duplicated."""

    @classmethod
    def setUpClass(cls):
        cls.module = load_module(SKILL_DIRS[0], "closeout.py")

    def observation(self, level="merged", observation="revert"):
        return {"record_id": str(uuid.uuid4()), "ticket": "39", "pr": 12,
                "level": level, "observation": observation,
                "reason": "the merge was reverted on main",
                "evidence_ref": "evidence/revert-12.txt", "observed_at": utc_now()}

    def test_a_revert_lowers_the_merged_rows(self):
        observations = {"state": "ok", "reason": "", "records": [
            self.observation()]}
        checklist = self.module.plan(facts(observations=observations))
        for index in (2, 3, 4):
            with self.subTest(row=index):
                self.assertEqual(row(checklist, index)["status"], "blocked")
                self.assertTrue(row(checklist, index)["detail"]["escalate"])
        self.assertEqual(checklist["classification"], "merge_reverted")
        self.assertEqual(row(checklist, 2)["detail"]["reverted"]["evidence_ref"],
                         "evidence/revert-12.txt")

    def test_a_revert_for_another_pr_does_not_lower_this_one(self):
        other = dict(self.observation(observation="reopen"), pr=13)
        observations = {"state": "ok", "reason": "", "records": [other]}
        checklist = self.module.plan(facts(observations=observations))
        self.assertEqual(row(checklist, 2)["status"], "ok")


class ReleaseTests(unittest.TestCase):
    """Release needs settlement and a persisted, proven closeout record."""

    @classmethod
    def setUpClass(cls):
        cls.module = load_module(SKILL_DIRS[0], "closeout.py")

    def closeout(self, outcome="success", **overrides):
        data = {
            "record_id": str(uuid.uuid4()),
            "durable": True,
            "outcome": outcome,
            "classification": "ready" if outcome == "success" else "tracker_unclosed",
            "acceptance_record_ids": [ACCEPTANCE_RECORD_ID],
            "mapping": [{"dispatch_id": "dispatch-1", "task_id": "task-39",
                         "attempt": 1, "worktree": "worktrees/task-39"}],
            "evidence_refs": ["ledger:acceptance:" + ACCEPTANCE_RECORD_ID],
            "release_verification": {
                "source": "worker-release --help (fixture)",
                "at": utc_now(),
                "version_scope": "help text only",
            },
        }
        data.update(overrides)
        return data

    def read_back(self, **worker):
        return {"state": "ok", "reason": "", "worker": {
            "dispatch_id": "dispatch-1", "worker_state": "released",
            "settled": True, **worker}}

    def test_a_settled_dispatch_with_a_closeout_record_may_release(self):
        closeout = self.closeout()
        decision = self.module.release_decision(
            closeout, health_state="healthy", settled=True,
            read_back=self.read_back(), decided_by="coordinator")
        self.assertTrue(decision["allowed"], decision)
        self.assertFalse(decision["health_authorized"])
        self.assertEqual(decision["closeout_record"], closeout["record_id"])
        self.assertEqual(decision["version_scope"], "help text only")
        self.assertTrue(decision["provisional"])

    def test_an_unsettled_dispatch_is_never_released(self):
        for settled in (False, None):
            with self.subTest(settled=settled):
                decision = self.module.release_decision(
                    self.closeout(), health_state="healthy", settled=settled,
                    read_back=self.read_back(worker_state="active", settled=False),
                    decided_by="coordinator")
                self.assertFalse(decision["allowed"])
                self.assertIn("settl", decision["reason"])

    def test_a_missing_closeout_record_blocks_release(self):
        decision = self.module.release_decision(
            None, health_state="healthy", settled=True,
            read_back=self.read_back(), decided_by="coordinator")
        self.assertFalse(decision["allowed"])
        self.assertIn("closeout", decision["reason"])

    def test_a_failed_closeout_record_write_blocks_release_until_repaired(self):
        decision = self.module.release_decision(
            self.closeout(durable=False), health_state="healthy", settled=True,
            read_back=self.read_back(), decided_by="coordinator")
        self.assertFalse(decision["allowed"])
        self.assertIn("durable", decision["reason"])

    def test_release_unknown_reads_worker_show_and_worker_list_first(self):
        decision = self.module.release_decision(
            self.closeout(), health_state="healthy", settled=None, read_back=None,
            release_state="release_unknown", decided_by="coordinator")
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["required_reads"], ["worker-show", "worker-list"])
        self.assertIn("worker-show", decision["reason"])

    def test_release_unknown_with_an_unsettled_read_back_stays_refused(self):
        decision = self.module.release_decision(
            self.closeout(), health_state="healthy", settled=None,
            read_back=self.read_back(worker_state="active", settled=False),
            release_state="release_unknown", decided_by="coordinator")
        self.assertFalse(decision["allowed"])
        self.assertIn("settl", decision["reason"])

    def test_release_unknown_with_a_settled_read_back_may_release(self):
        decision = self.module.release_decision(
            self.closeout(), health_state="healthy", settled=None,
            read_back=self.read_back(), release_state="release_unknown",
            decided_by="coordinator")
        self.assertTrue(decision["allowed"], decision)
        self.assertTrue(decision["read_back"]["settled"])

    def test_a_success_closeout_must_prove_acceptance_and_the_mapping(self):
        for broken in (
            self.closeout(acceptance_record_ids=[]),
            self.closeout(mapping=[]),
            self.closeout(mapping=[{"dispatch_id": "dispatch-1", "task_id": "task-39"}]),
        ):
            with self.subTest(broken=sorted(broken)):
                decision = self.module.release_decision(
                    broken, health_state="healthy", settled=True,
                    read_back=self.read_back(), decided_by="coordinator")
                self.assertFalse(decision["allowed"])
                self.assertIn("mapping", decision["reason"])

    def test_a_failure_closeout_must_prove_classification_and_evidence(self):
        for broken in (
            self.closeout(outcome="failure", classification=""),
            self.closeout(outcome="failure", evidence_refs=[]),
        ):
            with self.subTest(broken=sorted(broken)):
                decision = self.module.release_decision(
                    broken, health_state="healthy", settled=True,
                    read_back=self.read_back(), decided_by="coordinator")
                self.assertFalse(decision["allowed"])
                self.assertIn("evidence", decision["reason"])

    def test_a_pending_closeout_waits_for_its_one_way_door(self):
        decision = self.module.release_decision(
            self.closeout(outcome="pending"), health_state="healthy", settled=True,
            read_back=self.read_back(), decided_by="coordinator")
        self.assertFalse(decision["allowed"])
        self.assertIn("pending", decision["reason"])
        self.assertEqual(decision["decided_by"], "human")

    def test_health_never_authorizes_release_by_itself(self):
        decision = self.module.release_decision(
            self.closeout(), health_state="lost", settled=True,
            read_back=self.read_back(), decided_by="coordinator")
        self.assertFalse(decision["health_authorized"])
        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["health"]["from"], "lost")


class ReconciliationTests(unittest.TestCase):
    """``reconcile`` reads authority through adapters and persists the closeout."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "ledger-home"
        self.module = load_module(SKILL_DIRS[0], "closeout.py")
        self.acceptance = load_module(SKILL_DIRS[0], "acceptance.py")
        self.ledger = self.module.LEDGER

    def record_acceptance(self, **overrides):
        payload = acceptance_record(**overrides)
        self.assertEqual(self.acceptance.validate(payload), [])
        record = self.acceptance._ledger_record(payload, RUN_ID)
        self.ledger.append(record, home=self.home)
        self.recorded_acceptance_id = record["record_id"]
        return record

    def append_pending(self, item=None):
        item = item or pending_item()
        record = {
            "schema_version": 1,
            "record_id": item["record_id"],
            "kind": "pending",
            "run_id": RUN_ID,
            "subject": {"ticket": "39", "pr": item["pr"]},
            "action": "merge",
            "request_id": "",
            "idempotency": "none",
            "state": "pending",
            "evidence_ref": "pr_host:pr:" + str(item["pr"]),
            "observed_at": item["check_after"],
            "actor": "human",
            "detail": {**item, "decision_class": item["pending_type"]},
        }
        self.ledger.append(record, home=self.home)
        return record

    def authority(self, **sections):
        """The sections reconcile reads from outside the ledger; missing ones are unknown."""
        defaults = {
            "pr_host": facts()["pr_host"],
            "git": facts()["git"],
            "tickets": facts()["tickets"],
            "tracker": facts()["tracker"],
            "workers": facts()["workers"],
            "dashboard": facts()["dashboard"],
            "release": facts()["release"],
        }
        defaults.update(sections)
        return FactsAuthority(**defaults)

    def reconcile(self, authority=None):
        return self.module.reconcile(
            RUN_ID, authority if authority is not None else self.authority(),
            home=self.home, answer_location="tickets/task-40-answer.md")

    def records(self, kind=None):
        filters = {"kind": kind} if kind else None
        return self.ledger.read(RUN_ID, filters, home=self.home)

    def test_a_ready_closeout_is_persisted_with_the_answer_location(self):
        self.record_acceptance()
        result = self.reconcile()
        self.assertTrue(result["recorded"])
        self.assertTrue(result["checklist"]["ready"])
        self.assertEqual(result["checklist"]["classification"], "ready")
        self.assertEqual(result["checklist"]["answer_location"],
                         "tickets/task-40-answer.md")
        closeouts = self.records("closeout")
        self.assertEqual(len(closeouts), 1)
        self.assertEqual(closeouts[0]["detail"]["answer_location"],
                         "tickets/task-40-answer.md")
        self.assertEqual(closeouts[0]["detail"]["outcome"], "success")
        self.assertEqual(closeouts[0]["detail"]["mapping"], [{
            "dispatch_id": "dispatch-1", "task_id": "task-39", "attempt": 1,
            "worktree": "worktrees/task-39"}])
        self.assertEqual(closeouts[0]["detail"]["acceptance_record_ids"],
                         [self.recorded_acceptance_id])

    def test_the_closeout_record_is_idempotent_for_the_same_authority(self):
        self.record_acceptance()
        first = self.reconcile()
        second = self.reconcile()
        self.assertEqual(first["record_id"], second["record_id"])
        self.assertFalse(second["appended"])
        self.assertEqual(len(self.records("closeout")), 1)

    def test_a_revert_observation_is_consumed_and_never_duplicated(self):
        self.record_acceptance()
        observation = {
            "ticket": "39", "pr": 12, "level": "merged", "observation": "revert",
            "reason": "the merge was reverted on main",
            "evidence_ref": "evidence/revert-12.txt", "observed_at": utc_now(),
        }
        self.assertEqual(self.acceptance.validate_observation(observation), [])
        self.ledger.append(
            self.acceptance._observation_record(observation, RUN_ID), home=self.home)
        before = len(self.records("observation"))
        result = self.reconcile()
        self.assertEqual(result["checklist"]["classification"], "merge_reverted")
        self.assertFalse(result["checklist"]["ready"])
        after = self.records("observation")
        self.assertEqual(len(after), before)
        self.assertEqual(
            [item["detail"].get("observation") for item in after].count("revert"), 1)

    def test_a_crash_between_merge_and_tracker_is_repaired_from_rows_two_three_four_seven(self):
        self.record_acceptance()
        first = self.reconcile(authority=self.authority(tracker={
            "state": "ok", "reason": "",
            "tickets": [{"ticket": "39", "state": "open"}]}))
        self.assertEqual(first["checklist"]["classification"], "tracker_unclosed")
        self.assertEqual(first["closeout"]["outcome"], "failure")
        self.assertTrue(first["closeout"]["evidence_refs"])
        self.assertEqual(first["closeout"]["classification"], "tracker_unclosed")

        second = self.reconcile()
        self.assertTrue(second["checklist"]["ready"])
        self.assertEqual(second["closeout"]["outcome"], "success")
        self.assertNotEqual(first["record_id"], second["record_id"])
        closeouts = self.records("closeout")
        self.assertEqual(len(closeouts), 2)
        outcomes = sorted(record["detail"]["outcome"] for record in closeouts)
        self.assertEqual(outcomes, ["failure", "success"])

    def test_a_human_merge_pending_item_is_visible_without_a_coordinator_turn(self):
        self.record_acceptance()
        pending = self.append_pending()
        # No closeout run yet: the ledger itself shows the one-way door.
        shown = self.records("pending")
        self.assertEqual([record["record_id"] for record in shown],
                         [pending["record_id"]])
        summary = self.ledger.summary(RUN_ID, home=self.home)
        self.assertIn(pending["record_id"], summary["pending_items"])

    def test_the_pending_item_reconciles_on_the_next_turn(self):
        self.record_acceptance()
        pending = self.append_pending()
        open_host = {"state": "ok", "reason": "", "pull_requests": [
            {"pr": 12, "state": "OPEN", "base": "main", "head_sha": ACCEPTED}]}
        waiting = self.reconcile(authority=self.authority(pr_host=open_host))
        self.assertEqual(waiting["checklist"]["classification"], "pending_one_way_door")
        # Row 2 is genuinely unproven while the human merge is outstanding; row 10 is the
        # pending one-way door that explains it, so the declaration waits.
        self.assertEqual(waiting["checklist"]["blocking"], [2, 10])
        self.assertEqual(waiting["closeout"]["outcome"], "pending")
        self.assertEqual(waiting["pending_resolved"], [])

        merged = self.reconcile()
        self.assertEqual(merged["pending_resolved"], [pending["record_id"]])
        self.assertTrue(merged["checklist"]["ready"])
        self.assertEqual(merged["checklist"]["classification"], "ready")
        resolutions = [record for record in self.records("observation")
                       if record["detail"].get("pending_record_id") == pending["record_id"]]
        self.assertEqual(len(resolutions), 1)
        self.assertEqual(resolutions[0]["detail"]["status"], "resolved")
        self.assertEqual(resolutions[0]["detail"]["checks"], {
            "pr_merged": True, "merge_sha": True,
            "main_synced": True, "tracker_closed": True})

        repeated = self.reconcile()
        self.assertEqual(repeated["pending_resolved"], [])
        resolutions = [record for record in self.records("observation")
                       if record["detail"].get("pending_record_id") == pending["record_id"]]
        self.assertEqual(len(resolutions), 1)

    def test_the_closeout_record_carries_the_release_verification(self):
        self.record_acceptance()
        result = self.reconcile()
        detail = self.records("closeout")[0]["detail"]
        self.assertEqual(detail["release_verification"]["source"],
                         "worker-release --help (fixture)")
        self.assertEqual(set(detail["release_verification"]),
                         {"source", "at", "version_scope"})
        decision = self.module.release_decision(
            result["closeout"], health_state="healthy", settled=True,
            read_back={"state": "ok", "worker": {"settled": True, "worker_state": "released"}},
            decided_by="coordinator")
        self.assertTrue(decision["allowed"], decision)

    def test_an_unwritable_ledger_returns_an_undurable_closeout(self):
        self.record_acceptance()
        ledger_file = self.home / "runs" / RUN_ID / "ledger.jsonl"
        os.chmod(ledger_file, 0o400)
        self.addCleanup(os.chmod, ledger_file, 0o600)
        result = self.reconcile()
        self.assertFalse(result["recorded"])
        self.assertFalse(result["closeout"]["durable"])
        self.assertIn("closeout-pending", result["retry_path"])
        decision = self.module.release_decision(
            result["closeout"], health_state="healthy", settled=True,
            read_back={"state": "ok", "worker": {"settled": True, "worker_state": "released"}},
            decided_by="coordinator")
        self.assertFalse(decision["allowed"])

    def test_reconcile_refuses_to_persist_without_an_answer_location(self):
        with self.assertRaises(ValueError):
            self.module.reconcile(RUN_ID, self.authority(), home=self.home,
                                  answer_location="")
        self.assertEqual(self.records("closeout"), [])

    def test_a_very_large_checklist_still_writes_a_valid_record(self):
        """The ledger caps a record at 64 KiB; a big map must not lose its closeout.

        Row 1 carries every accepted record's Answer location, so a map of long
        locations produces a checklist larger than one ledger record. The closeout keeps
        its outcome, its own Answer location and the acceptance ids, marks itself
        truncated, and still validates.
        """
        answer = "tickets/task-40-answer.md"
        long_answer = "tickets/" + "a" * 1800 + ".md"
        for number in range(100, 140):
            payload = acceptance_record(
                pr={"number": number, "head_at_acceptance": ACCEPTED, "base": "main"},
                answer_location=long_answer)
            self.assertEqual(self.acceptance.validate(payload), [])
            self.ledger.append(
                self.acceptance._ledger_record(payload, RUN_ID), home=self.home)
        self.assertGreater(
            len(self.ledger.canonical_json(facts(
                acceptance={"state": "ok", "reason": "", "records": [
                    {"record_id": str(uuid.uuid4()),
                     **acceptance_record(answer_location=long_answer)}
                    for _ in range(40)]}))),
            60 * 1024)
        authority = self.authority(pr_host={
            "state": "ok", "reason": "",
            "pull_requests": [
                {"pr": number, "state": "MERGED", "base": "main",
                 "head_sha": ACCEPTED, "merge_sha": MERGE}
                for number in range(100, 140)]})
        result = self.reconcile(authority=authority)
        self.assertTrue(result["recorded"], result)
        stored = self.records("closeout")[0]
        self.assertEqual(stored["detail"]["outcome"], "success")
        self.assertTrue(stored["detail"]["truncated"])
        self.assertEqual(stored["detail"]["answer_location"], answer)
        self.assertEqual(len(stored["detail"]["acceptance_record_ids"]), 40)
        self.ledger.validate_record(stored)

    def test_the_shared_fake_serves_the_pr_host_section(self):
        self.record_acceptance()
        authority = self.authority()
        section = FAKE.FakeHost(facts()["pr_host"]).snapshot(RUN_ID, [])
        authority.sections["pr_host"] = section
        result = self.reconcile(authority=authority)
        self.assertEqual(result["checklist"]["classification"], "ready")


class ReferenceParityTests(unittest.TestCase):
    """Both bundles describe the checklist, pending and release precondition identically.

    The anchors are the decisions a coordinator must not read differently in the two
    bundles: the ten-numbered rows, row 1 being non-repeatable, row 9 being a gap only,
    row 5 pausing later merges, the pending item staying visible without a turn, the two
    halves of the release precondition, the crash-recovery order and the version scope.
    This checks anchors, not full semantic equivalence; wording may differ per language.
    """

    ANCHORS = {
        "orchestra-auto": {
            "section": "§6.7",
            "script": "scripts/closeout.py",
            "non_repeatable": "Only row 1 is non-repeatable",
            "dashboard": "Row 9 records a gap and nothing else",
            "merge_blocked": "blocks later merges",
            "pending": "without a coordinator turn",
            "release": "Release requires a settled Dispatch",
            "release_unknown": "`release_unknown` first reads `worker-show`",
            "undurable": "release and the gate declaration wait",
            "crash": "repaired from rows 2, 3, 4 and 7",
            "answer": "`answer_location`",
            "version_scope": "version scope",
            "reclaimable": "reclaimable",
        },
        "orchestra-auto-zh": {
            "section": "§6.7",
            "script": "scripts/closeout.py",
            "non_repeatable": "只有第 1 行不可重复",
            "dashboard": "第 9 行只记 gap",
            "merge_blocked": "阻断后续合并",
            "pending": "无需 coordinator turn",
            "release": "release 需要已结算的 Dispatch",
            "release_unknown": "`release_unknown` 先读 `worker-show`",
            "undurable": "release 与门禁宣告都等写入修复",
            "crash": "第 2、3、4、7 行修复",
            "answer": "`answer_location`",
            "version_scope": "版本范围",
            "reclaimable": "reclaimable",
        },
    }
    ROW_ANCHORS = {
        "orchestra-auto": ("accepted SHA", "PR host API", "git merge-base --is-ancestor",
                           "local main is synced", "tracker read-back", "worker lifecycle",
                           "dashboard projection", "one-way-door pending"),
        "orchestra-auto-zh": ("accepted SHA", "PR host API", "git merge-base --is-ancestor",
                              "本地 main 已同步", "tracker 读回", "worker 生命周期",
                              "dashboard 投影", "单向门 pending"),
    }

    def flat(self, name):
        text = (REPO_ROOT / "skills" / name / "references" / "gate-acceptance.md").read_text()
        return "".join(text.split())

    def test_the_two_bundles_carry_the_same_closeout_contract(self):
        for name, expected in self.ANCHORS.items():
            flat = self.flat(name)
            for key, anchor in expected.items():
                self.assertIn("".join(anchor.split()), flat,
                              f"{name}: gate-acceptance.md lost the closeout anchor {key!r}")
            for anchor in self.ROW_ANCHORS[name]:
                self.assertIn("".join(anchor.split()), flat,
                              f"{name}: the ten-row table lost the {anchor!r} row")

    def test_long_horizon_documents_pending_and_release_in_both_bundles(self):
        expectations = {
            "orchestra-auto": ("### 4.1", "pending", "ledger.py show",
                               "Release comes after settlement", "worker-show",
                               "never released", "§6.7"),
            "orchestra-auto-zh": ("### 4.1", "pending", "ledger.py show",
                                  "release 在结算与持久收尾之后", "worker-show",
                                  "绝不 release", "§6.7"),
        }
        for name, anchors in expectations.items():
            text = (REPO_ROOT / "skills" / name / "references"
                    / "long-horizon-tickets.md").read_text()
            flat = "".join(text.split())
            for anchor in anchors:
                self.assertIn("".join(anchor.split()), flat,
                              f"{name}: long-horizon-tickets.md lost {anchor!r}")


class CliTests(unittest.TestCase):
    """The product surface: ``plan``, ``reconcile`` and ``show`` as a subprocess."""

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

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(SKILL_DIRS[0] / SCRIPT_RELATIVE), *args],
            env=self.env, cwd=str(self.root), text=True, capture_output=True)

    def write_json(self, name, payload):
        path = self.root / name
        path.write_text(json.dumps(payload))
        return path

    def seed_acceptance(self):
        payload = acceptance_record()
        result = subprocess.run(
            [sys.executable, str(SKILL_DIRS[0] / "scripts" / "acceptance.py"),
             "record", "--run-id", RUN_ID, "--record", json.dumps(payload)],
            env=self.env, cwd=str(self.root), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def authority_file(self):
        return self.write_json("authority.json", {
            key: value for key, value in facts().items()
            if key in ("pr_host", "git", "tickets", "tracker", "workers",
                       "dashboard", "release")
        })

    def test_plan_prints_the_checklist_from_a_facts_file(self):
        path = self.write_json("facts.json", facts())
        result = self.run_cli("plan", "--run-id", RUN_ID, "--facts", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        checklist = json.loads(result.stdout)
        self.assertEqual([item["id"] for item in checklist["rows"]],
                         list(range(1, 11)))
        self.assertTrue(checklist["ready"])

    def test_plan_refuses_malformed_facts(self):
        path = self.write_json("facts.json", facts(bogus={}))
        result = self.run_cli("plan", "--run-id", RUN_ID, "--facts", str(path))
        self.assertEqual(result.returncode, 1)
        self.assertIn("closeout", result.stderr.lower())

    def test_plan_refuses_another_runs_facts(self):
        path = self.write_json("facts.json", facts(run_id="run_other"))
        result = self.run_cli("plan", "--run-id", RUN_ID, "--facts", str(path))
        self.assertEqual(result.returncode, 1)
        self.assertIn("another run", result.stderr)

    def test_reconcile_then_show_round_trips(self):
        self.seed_acceptance()
        reconciled = self.run_cli(
            "reconcile", "--run-id", RUN_ID, "--authority", str(self.authority_file()),
            "--answer-location", "tickets/task-40-answer.md")
        self.assertEqual(reconciled.returncode, 0, reconciled.stderr + reconciled.stdout)
        outcome = json.loads(reconciled.stdout)
        self.assertTrue(outcome["recorded"])
        self.assertTrue(outcome["checklist"]["ready"])
        self.assertEqual(outcome["closeout"]["outcome"], "success")
        self.assertEqual(outcome["checklist"]["answer_location"],
                         "tickets/task-40-answer.md")

        shown = self.run_cli("show", "--run-id", RUN_ID)
        self.assertEqual(shown.returncode, 0, shown.stderr + shown.stdout)
        payload = json.loads(shown.stdout)
        self.assertTrue(payload["found"])
        self.assertEqual(payload["record_id"], outcome["record_id"])
        self.assertEqual(payload["checklist"]["classification"], "ready")
        self.assertEqual(payload["closeout"]["outcome"], "success")

    def test_show_without_a_closeout_exits_non_zero(self):
        result = self.run_cli("show", "--run-id", RUN_ID)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(json.loads(result.stdout)["found"])

    def test_the_closeout_record_is_a_ledger_record_with_an_answer_location(self):
        self.seed_acceptance()
        reconciled = self.run_cli(
            "reconcile", "--run-id", RUN_ID, "--authority", str(self.authority_file()),
            "--answer-location", "tickets/task-40-answer.md")
        self.assertEqual(reconciled.returncode, 0, reconciled.stderr)
        records = json.loads(subprocess.run(
            [sys.executable, str(SKILL_DIRS[0] / "scripts" / "ledger.py"),
             "show", "--run-id", RUN_ID, "--kind", "closeout"],
            env=self.env, cwd=str(self.root), text=True,
            capture_output=True).stdout)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["kind"], "closeout")
        self.assertEqual(records[0]["action"], "closeout")
        self.assertEqual(records[0]["detail"]["answer_location"],
                         "tickets/task-40-answer.md")
        self.assertEqual(records[0]["detail"]["release_verification"]["source"],
                         "worker-release --help (fixture)")


if __name__ == "__main__":
    unittest.main()
