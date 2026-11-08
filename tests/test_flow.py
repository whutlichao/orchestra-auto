#!/usr/bin/env python3
"""Behaviour tests for ``flow.py``, the explicit process record.

The seam is the module's public interface: ``validate_record``, ``read_records``, ``plan`` and
``append_record`` take data and files and return data, and the CLI is exercised end to end
because a coordinator resumes from its JSON. Nothing here reads Orca, git or a tracker.

Every fixture is a real temporary file, so "the old ledger is not rewritten" and "an invalid
record never lands" are properties of the filesystem, not of a mock.
"""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "orchestra-auto" / "scripts"
FLOW = SCRIPTS / "flow.py"


def load_flow():
    spec = importlib.util.spec_from_file_location("flow_under_test", FLOW)
    assert spec is not None and spec.loader is not None, "flow.py could not be loaded"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


flow = load_flow()


def design_only_flow(run_id="r1", flow_id="f1"):
    return {
        "schema_version": 1,
        "record_id": "flow-1",
        "kind": "flow",
        "flow_id": flow_id,
        "run_id": run_id,
        "goal": "settle the retry policy",
        "endpoint": "design artifact",
        "nodes": [
            {"id": "grill", "gate": "G-docs", "depends_on": []},
            {"id": "spec", "gate": "G-spec", "depends_on": ["grill"]},
        ],
    }


def accepted(node, *, task_id="t1", attempt=1, version="v1", evidence="answer.md"):
    return {
        "schema_version": 1,
        "record_id": f"state-{node}-{task_id}-{attempt}-{version}",
        "kind": "state",
        "flow_id": "f1",
        "node": node,
        "state": "accepted",
        "task_id": task_id,
        "attempt": attempt,
        "artifact_version": version,
        "evidence_ref": evidence,
    }


class ValidateRecordTests(unittest.TestCase):
    def test_a_design_only_flow_validates(self):
        self.assertEqual(flow.validate_record(design_only_flow()), [])

    def test_an_unknown_state_is_rejected(self):
        record = {"schema_version": 1, "record_id": "x", "flow_id": "f1", "kind": "state",
                  "node": "grill", "state": "done"}
        self.assertTrue(any("state:" in error for error in flow.validate_record(record)))

    def test_not_applicable_requires_entry_evidence(self):
        record = {"schema_version": 1, "record_id": "x", "flow_id": "f1", "kind": "state",
                  "node": "grill", "state": "not_applicable", "reason": "no design asked"}
        self.assertTrue(any("entry_evidence" in error for error in flow.validate_record(record)))
        record["entry_evidence"] = "the user asked only for a one-line fix"
        self.assertEqual(flow.validate_record(record), [])

    def test_accepted_requires_a_binding(self):
        record = {"schema_version": 1, "record_id": "x", "flow_id": "f1", "kind": "state",
                  "node": "grill", "state": "accepted"}
        errors = flow.validate_record(record)
        for field in ("evidence_ref", "artifact_version", "task_id", "attempt"):
            self.assertTrue(any(field in error for error in errors), (field, errors))

    def test_an_acceptance_from_another_run_must_be_an_import(self):
        record = accepted("grill")
        record["source_run"] = "r0"
        self.assertTrue(any("import record" in error for error in flow.validate_record(record)))

    def test_a_dependency_cycle_is_rejected(self):
        record = design_only_flow()
        record["nodes"][0]["depends_on"] = ["spec"]
        self.assertTrue(any("cycle" in error for error in flow.validate_record(record)))

    def test_a_flow_needs_a_unique_non_empty_node_list(self):
        record = design_only_flow()
        record["nodes"] = []
        self.assertTrue(any("nodes:" in error for error in flow.validate_record(record)))

    def test_an_unknown_kind_is_rejected(self):
        record = {"schema_version": 1, "record_id": "x", "flow_id": "f1", "kind": "log"}
        self.assertTrue(any("kind:" in error for error in flow.validate_record(record)))


class PlanTests(unittest.TestCase):
    def test_a_flow_with_no_states_is_ready_at_its_first_node(self):
        answer = flow.plan([design_only_flow()])
        self.assertEqual(answer["state"], "continue")
        self.assertEqual([item["node"] for item in answer["ready"]], ["grill"])
        self.assertEqual(answer["nodes"][1]["state"], "pending")

    def test_an_accepted_node_binds_its_task_attempt_and_artifact(self):
        answer = flow.plan([design_only_flow(), accepted("grill")])
        node = answer["nodes"][0]
        self.assertEqual(node["state"], "accepted")
        self.assertEqual(node["attempt"], 1)
        self.assertEqual(node["artifact_version"], "v1")
        self.assertEqual([item["node"] for item in answer["ready"]], ["spec"])

    def test_completion_covers_only_the_applicable_nodes(self):
        answer = flow.plan([design_only_flow(), accepted("grill"), accepted("spec")])
        self.assertEqual(answer["state"], "complete")
        self.assertIn("not merge authorization", answer["note"])

    def test_a_not_applicable_node_finishes_its_dependents(self):
        skip = {"schema_version": 1, "record_id": "skip", "kind": "state", "flow_id": "f1",
                "node": "spec", "state": "not_applicable", "reason": "design-only endpoint",
                "entry_evidence": "the user asked for design only"}
        answer = flow.plan([design_only_flow(), accepted("grill"), skip])
        self.assertEqual(answer["state"], "complete")

    def test_a_node_without_applies_is_reported_not_applicable(self):
        """The plan's own `applies: false` is the evidence; it must not read as pending work."""
        record = design_only_flow()
        record["nodes"][1]["applies"] = False
        record["nodes"][1]["entry_evidence"] = "the user asked for design only"
        answer = flow.plan([record, accepted("grill")])
        self.assertEqual(answer["state"], "complete")
        self.assertEqual(answer["nodes"][1]["state"], "not_applicable")
        self.assertEqual(answer["nodes"][1]["reason"], "the user asked for design only")

    def test_waiting_user_is_not_an_execution_failure(self):
        waiting = {"schema_version": 1, "record_id": "wait", "kind": "state", "flow_id": "f1",
                   "node": "grill", "state": "waiting_user", "reason": "decision Q3"}
        answer = flow.plan([design_only_flow(), waiting])
        self.assertEqual(answer["state"], "continue")
        self.assertEqual([item["node"] for item in answer["waiting_user"]], ["grill"])

    def test_a_newer_failure_overrides_an_older_success(self):
        failed = {"schema_version": 1, "record_id": "fail-2", "kind": "state",
                  "flow_id": "f1", "node": "spec", "state": "blocked",
                  "reason": "acceptance re-run failed"}
        first = accepted("grill")
        second = dict(failed)
        answer = flow.plan([design_only_flow(), first, second])
        self.assertEqual(answer["nodes"][1]["state"], "blocked")
        self.assertEqual(answer["state"], "continue")

    def test_a_stale_acceptance_does_not_override_a_newer_failure(self):
        failed = {"schema_version": 1, "record_id": "fail-2", "kind": "state",
                  "flow_id": "f1", "node": "grill", "state": "blocked", "attempt": 2,
                  "reason": "re-run failed"}
        stale = accepted("grill", attempt=1)
        answer = flow.plan([design_only_flow(), failed, stale])
        self.assertEqual(answer["nodes"][0]["state"], "blocked")
        self.assertTrue(any("older" in gap for gap in answer["evidence_gaps"]))
        self.assertEqual(answer["state"], "uncertain")

    def test_an_acceptance_for_a_different_task_is_divergent(self):
        other = accepted("grill", task_id="t9")
        answer = flow.plan([design_only_flow(), accepted("grill"), other])
        self.assertTrue(answer["nodes"][0]["divergent"])
        self.assertEqual(answer["state"], "uncertain")

    def test_invalidation_propagates_downstream(self):
        revoke = {"schema_version": 1, "record_id": "rev", "kind": "invalidate",
                  "flow_id": "f1", "nodes": ["grill"], "cause": "spec-change",
                  "reason": "the user changed the scope"}
        answer = flow.plan([design_only_flow(), accepted("grill"), accepted("spec"), revoke])
        self.assertEqual([node["state"] for node in answer["nodes"]],
                         ["invalidated", "invalidated"])
        self.assertEqual(answer["invalidated"][0]["cause"], "spec-change")
        self.assertEqual(answer["state"], "continue")

    def test_a_revoked_node_can_be_redone(self):
        revoke = {"schema_version": 1, "record_id": "rev", "kind": "invalidate",
                  "flow_id": "f1", "nodes": ["grill"], "cause": "revocation",
                  "reason": "superseded"}
        redo = accepted("grill", attempt=2, version="v2")
        answer = flow.plan([design_only_flow(), accepted("grill"), revoke, redo])
        self.assertEqual(answer["nodes"][0]["state"], "accepted")
        self.assertEqual(answer["nodes"][0]["artifact_version"], "v2")
        self.assertEqual(answer["nodes"][1]["state"], "invalidated")
        self.assertIn("spec", [item["node"] for item in answer["ready"]])

    def test_an_import_carries_its_source_and_verification(self):
        record = {"schema_version": 1, "record_id": "imp", "kind": "import", "flow_id": "f1",
                  "node": "grill", "source_run": "r0", "artifact": "spec.md",
                  "artifact_version": "v3", "verification": "read and matched all 4 decisions",
                  "attempt": 1}
        answer = flow.plan([design_only_flow(), record])
        node = answer["nodes"][0]
        self.assertEqual(node["state"], "accepted")
        self.assertEqual(node["imported_from"]["source_run"], "r0")
        self.assertEqual(node["artifact_version"], "v3")

    def test_a_second_import_for_the_same_node_is_a_gap(self):
        first = {"schema_version": 1, "record_id": "imp-1", "kind": "import", "flow_id": "f1",
                 "node": "grill", "source_run": "r0", "artifact": "a.md",
                 "artifact_version": "v1", "verification": "matched"}
        second = dict(first, record_id="imp-2", source_run="r9")
        answer = flow.plan([design_only_flow(), first, second])
        self.assertTrue(any("imported more than once" in gap for gap in answer["evidence_gaps"]))

    def test_an_empty_file_is_uncertain_not_complete(self):
        answer = flow.plan([])
        self.assertEqual(answer["state"], "uncertain")
        self.assertEqual(answer["ready"], [])

    def test_an_unknown_node_reference_is_a_gap(self):
        record = accepted("ghost")
        answer = flow.plan([design_only_flow(), record])
        self.assertEqual(answer["state"], "uncertain")
        self.assertTrue(any("unknown node" in gap for gap in answer["evidence_gaps"]))


class ReadRecordsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "flow.jsonl"

    def tearDown(self):
        self.directory.cleanup()

    def test_a_missing_file_is_reported_not_raised(self):
        records, problems = flow.read_records(self.path)
        self.assertEqual(records, [])
        self.assertTrue(problems)

    def test_a_trailing_partial_line_is_discarded(self):
        self.path.write_text(json.dumps(design_only_flow()) + "\n" + '{"kind": "sta',
                             encoding="utf-8")
        records, problems = flow.read_records(self.path)
        self.assertEqual(len(records), 1)
        self.assertTrue(any("partial" in problem for problem in problems))

    def test_a_corrupt_complete_line_is_reported(self):
        self.path.write_text(json.dumps(design_only_flow()) + "\nnot json\n",
                             encoding="utf-8")
        records, problems = flow.read_records(self.path)
        self.assertEqual(len(records), 1)
        self.assertTrue(any("not JSON" in problem for problem in problems))

    def test_an_invalid_record_is_reported_not_used(self):
        bad = accepted("grill")
        bad["state"] = "done"
        self.path.write_text(json.dumps(design_only_flow()) + "\n" + json.dumps(bad) + "\n",
                             encoding="utf-8")
        records, problems = flow.read_records(self.path)
        self.assertEqual(len(records), 1)
        self.assertTrue(any("state:" in problem for problem in problems))

    def test_a_replayed_record_is_collapsed_and_a_conflict_is_reported(self):
        first = accepted("grill")
        self.path.write_text("\n".join(json.dumps(record) for record in
                                       (design_only_flow(), first, first)) + "\n",
                             encoding="utf-8")
        records, problems = flow.read_records(self.path)
        self.assertEqual(len(records), 2)
        self.assertEqual(problems, [])
        changed = dict(first, artifact_version="v9")
        self.path.write_text("\n".join(json.dumps(record) for record in
                                       (design_only_flow(), first, changed)) + "\n",
                             encoding="utf-8")
        records, problems = flow.read_records(self.path)
        self.assertTrue(any("conflicts" in problem for problem in problems))


class AppendRecordTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "runs" / "r1" / "flow.jsonl"

    def tearDown(self):
        self.directory.cleanup()

    def test_a_flow_lands_once_and_states_follow(self):
        flow.append_record(self.path, design_only_flow())
        flow.append_record(self.path, accepted("grill"))
        records, problems = flow.read_records(self.path)
        self.assertEqual(len(records), 2)
        self.assertEqual(problems, [])
        with self.assertRaises(flow.FlowError):
            flow.append_record(self.path, design_only_flow())

    def test_a_state_before_the_flow_is_refused(self):
        with self.assertRaises(flow.FlowError):
            flow.append_record(self.path, accepted("grill"))
        self.assertFalse(self.path.exists())

    def test_an_unknown_node_is_refused_before_writing(self):
        flow.append_record(self.path, design_only_flow())
        with self.assertRaises(flow.FlowError) as caught:
            flow.append_record(self.path, accepted("ghost"))
        self.assertIn("unknown node", str(caught.exception))
        records, _ = flow.read_records(self.path)
        self.assertEqual(len(records), 1)

    def test_a_mismatched_flow_id_is_refused(self):
        flow.append_record(self.path, design_only_flow())
        record = accepted("grill")
        record["flow_id"] = "other"
        with self.assertRaises(flow.FlowError):
            flow.append_record(self.path, record)

    def test_an_invalid_record_never_lands(self):
        with self.assertRaises(flow.FlowError):
            flow.append_record(self.path, {"schema_version": 1})


class CliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def run_cli(self, *argv, stdin=None):
        completed = subprocess.run([sys.executable, str(FLOW), *argv],
                                   capture_output=True, text=True, timeout=60, input=stdin)
        return completed.returncode, completed.stdout, completed.stderr

    def write(self, name, payload):
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_validate_reports_valid_and_invalid(self):
        good = self.write("flow.json", design_only_flow())
        bad = self.write("bad.json", {"schema_version": 1, "kind": "state"})
        code, out, _ = self.run_cli("validate", "--file", str(good))
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["valid"])
        code, out, _ = self.run_cli("validate", "--file", str(bad))
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)["valid"])

    def test_append_then_plan_round_trips(self):
        home = self.root / "home"
        record = self.write("flow.json", design_only_flow())
        code, out, _ = self.run_cli("append", "--run-id", "r1", "--flow-home", str(home),
                                    "--record", str(record))
        self.assertEqual(code, 0, out)
        state = self.write("state.json", accepted("grill"))
        code, out, _ = self.run_cli("append", "--run-id", "r1", "--flow-home", str(home),
                                    "--record", str(state))
        self.assertEqual(code, 0, out)
        code, out, _ = self.run_cli("plan", "--run-id", "r1", "--flow-home", str(home))
        self.assertEqual(code, 0, out)
        payload = json.loads(out)
        self.assertEqual(payload["state"], "continue")
        self.assertEqual([item["node"] for item in payload["ready"]], ["spec"])

    def test_plan_without_a_flow_is_uncertain(self):
        code, out, _ = self.run_cli("plan", "--run-id", "missing",
                                    "--flow-home", str(self.root))
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertEqual(payload["state"], "uncertain")
        self.assertEqual(payload["ready"], [])

    def test_plan_on_an_invalid_file_is_uncertain_with_problems(self):
        home = self.root / "home"
        run_dir = home / "runs" / "r1"
        run_dir.mkdir(parents=True)
        (run_dir / "flow.jsonl").write_text(
            json.dumps(design_only_flow()) + "\nnot json\n", encoding="utf-8")
        code, out, _ = self.run_cli("plan", "--run-id", "r1", "--flow-home", str(home))
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertEqual(payload["state"], "uncertain")
        self.assertTrue(any("not JSON" in item for item in payload["problems"]))

    def test_append_accepts_stdin(self):
        home = self.root / "home"
        code, out, _ = self.run_cli("append", "--run-id", "r1", "--flow-home", str(home),
                                    "--record", "-", stdin=json.dumps(design_only_flow()))
        self.assertEqual(code, 0, out)

    def test_an_invalid_append_exits_one_and_writes_nothing(self):
        home = self.root / "home"
        code, out, _ = self.run_cli("append", "--run-id", "r1", "--flow-home", str(home),
                                    "--record", "-", stdin='{"schema_version": 1}')
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)["appended"])
        self.assertFalse((home / "runs" / "r1" / "flow.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
