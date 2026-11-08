#!/usr/bin/env python3
"""Behaviour tests for ``chain.py``: chain-level resume and per-step exit checks.

The seam is narrow on purpose:

* **Module import** for the pure parts — ``landed_positions(records)``, ``next_step(records)``,
  ``check_artifact(step, text)`` and ``load_records(path)`` take data and return data.
* **CLI end to end** for the product surface — ``steps``, ``resume`` and ``exit-check`` run as a
  subprocess, so the assertions are about the JSON printed and the exit code.

Nothing here calls a live Orca, a PR host, a tracker or the network: this module deliberately
refuses to read authority, and a test that needed authority would be testing another module.
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
CHAIN = SCRIPTS / "chain.py"


def load_chain():
    spec = importlib.util.spec_from_file_location("chain_under_test", CHAIN)
    assert spec is not None and spec.loader is not None, "chain.py could not be loaded"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


chain = load_chain()


def acceptance(gate, *, checks=None, review=None):
    detail = {"gate": [gate] if isinstance(gate, str) else list(gate)}
    if checks is not None:
        detail["checks"] = checks
    if review is not None:
        detail["review"] = review
    return {"kind": "acceptance", "detail": detail}


PASSED_CHECKS = [{"name": "tests", "command": "python3 -m unittest",
                  "result": "pass", "evidence_ref": "t.log"}]


def ledger_record(detail, record_id="11111111-1111-4111-8111-111111111111"):
    """One schema-valid ledger record: the shape ``ledger.py`` accepts and validates."""
    return {
        "schema_version": 1,
        "record_id": record_id,
        "kind": "acceptance",
        "run_id": "r1",
        "subject": {"ticket": "39", "pr": 12, "attempt": 1},
        "action": "acceptance",
        "request_id": "",
        "idempotency": "none",
        "state": "completed",
        "evidence_ref": "tickets/task-39-answer.md",
        "observed_at": "2026-01-01T00:00:00Z",
        "actor": "coordinator",
        "detail": detail,
    }


class PositionTableTests(unittest.TestCase):
    def test_positions_follow_the_published_chain_order(self):
        self.assertEqual(chain.POSITION_IDS, ("grill", "spec", "tickets", "build", "review"))

    def test_the_build_position_holds_both_implement_steps(self):
        build = next(p for p in chain.POSITIONS if p["id"] == "build")
        self.assertEqual(build["steps"], ("implement", "implement-spec"))
        self.assertEqual(build["gate"], "G-impl")

    def test_every_position_names_a_known_gate(self):
        gates = {"G-docs", "G-spec", "G-tickets", "G-impl", "G-merge"}
        for position in chain.POSITIONS:
            self.assertIn(position["gate"], gates, position["id"])
            self.assertTrue(position["artifact"].strip(), position["id"])

    def test_only_the_artifact_shaped_steps_are_checkable(self):
        checkable = [s["id"] for s in chain.STEPS if s["exit"] != "external"]
        self.assertEqual(checkable, ["to-spec", "to-tickets"])


class LandedPositionsTests(unittest.TestCase):
    def test_a_record_with_no_checks_is_not_a_pass(self):
        self.assertEqual(chain.landed_positions([acceptance("G-docs")]), [])

    def test_a_failed_check_keeps_the_position_unlanded(self):
        record = acceptance("G-docs", checks=[{"name": "x", "command": "y",
                                               "result": "fail", "evidence_ref": "z"}])
        self.assertEqual(chain.landed_positions([record]), [])

    def test_a_waived_review_still_lands(self):
        record = acceptance("G-spec", checks=PASSED_CHECKS, review={"result": "waiver"})
        self.assertEqual(chain.landed_positions([record]), ["spec"])

    def test_a_failed_review_does_not_land(self):
        record = acceptance("G-spec", checks=PASSED_CHECKS, review={"result": "fail"})
        self.assertEqual(chain.landed_positions([record]), [])

    def test_non_acceptance_records_are_ignored(self):
        records = [{"kind": "intent", "detail": {"gate": ["G-docs"]}}, {"kind": "health"}]
        self.assertEqual(chain.landed_positions(records), [])

    def test_unknown_gates_land_nothing(self):
        self.assertEqual(chain.landed_positions([acceptance("G-made-up", checks=PASSED_CHECKS)]), [])

    def test_positions_stay_in_chain_order_not_ledger_order(self):
        records = [acceptance("G-impl", checks=PASSED_CHECKS),
                   acceptance("G-docs", checks=PASSED_CHECKS)]
        self.assertEqual(chain.landed_positions(records), ["grill", "build"])

    def test_one_gate_lands_one_position_only(self):
        """``implement`` and ``implement-spec`` share G-impl; the record must not land both."""
        landed = chain.landed_positions([acceptance("G-impl", checks=PASSED_CHECKS)])
        self.assertEqual(landed, ["build"])


class NextStepTests(unittest.TestCase):
    def test_an_empty_ledger_starts_at_the_first_position(self):
        answer = chain.next_step([])
        self.assertEqual(answer["state"], "continue")
        self.assertEqual(answer["next"]["position"], "grill")
        self.assertEqual(answer["landed"], [])

    def test_resume_points_at_the_first_unpassed_gate(self):
        answer = chain.next_step([acceptance("G-docs", checks=PASSED_CHECKS)])
        self.assertEqual(answer["next"]["position"], "spec")
        self.assertEqual(answer["next"]["steps"], ["to-spec"])
        self.assertIn("G-spec", answer["reason"])

    def test_a_gap_earlier_in_the_chain_wins(self):
        answer = chain.next_step([acceptance("G-docs", checks=PASSED_CHECKS),
                                  acceptance("G-tickets", checks=PASSED_CHECKS),
                                  acceptance("G-impl", checks=PASSED_CHECKS)])
        self.assertEqual(answer["next"]["position"], "spec")

    def test_the_build_position_offers_both_steps(self):
        answer = chain.next_step([acceptance("G-docs", checks=PASSED_CHECKS),
                                  acceptance("G-spec", checks=PASSED_CHECKS),
                                  acceptance("G-tickets", checks=PASSED_CHECKS)])
        self.assertEqual(answer["next"]["position"], "build")
        self.assertEqual(answer["next"]["steps"], ["implement", "implement-spec"])

    def test_every_gate_passed_is_complete(self):
        records = [acceptance(g, checks=PASSED_CHECKS)
                   for g in ("G-docs", "G-spec", "G-tickets", "G-impl", "G-merge")]
        answer = chain.next_step(records)
        self.assertEqual(answer["state"], "complete")
        self.assertIsNone(answer["next"])


class CheckArtifactTests(unittest.TestCase):
    SPEC_OK = (
        "# 设置页文案\n\n"
        "- **Test tier:** narrow — one component, one string\n\n"
        "## 验收\n\n"
        "- 打开设置页，开关文案是 Auto sync\n"
        "- 关掉再打开，状态保持\n"
    )

    def test_a_well_formed_spec_passes(self):
        self.assertEqual(chain.check_artifact("to-spec", self.SPEC_OK), [])

    def test_a_spec_without_a_test_tier_line_fails(self):
        errors = chain.check_artifact("to-spec", "# Spec\n\n## 验收\n\n- 文案正确\n")
        self.assertTrue(any("Test tier" in e for e in errors), errors)

    def test_a_spec_without_an_acceptance_section_fails(self):
        errors = chain.check_artifact("to-spec", "# Spec\n\n- **Test tier:** wide — router\n")
        self.assertTrue(any("acceptance section" in e for e in errors), errors)

    def test_an_acceptance_section_without_a_bullet_fails(self):
        text = "# Spec\n\n- **Test tier:** narrow — one file\n\n## 验收\n\n待定。\n"
        self.assertTrue(any("no testable line" in e
                            for e in chain.check_artifact("to-spec", text)))

    def test_the_section_is_cut_at_its_next_heading(self):
        text = "# Spec\n\n- **Test tier:** wide — two files\n\n## 验收\n\n## 其他\n\n- 无关内容\n"
        self.assertTrue(any("no testable line" in e
                            for e in chain.check_artifact("to-spec", text)))

    def test_tickets_pass_when_each_block_carries_an_acceptance_line(self):
        text = ("## 票 1\n\n- 改文案\n- 验收：设置页显示 Auto sync\n\n"
                "## 票 2\n\n- 改样式\n- Acceptance: the toggle keeps its state\n")
        self.assertEqual(chain.check_artifact("to-tickets", text), [])

    def test_tickets_name_the_block_that_is_missing_one(self):
        text = "## 票 1\n\n- 改文案\n- 验收：设置页显示 Auto sync\n\n## 票 2\n\n- 改样式\n"
        errors = chain.check_artifact("to-tickets", text)
        self.assertEqual(len(errors), 1)
        self.assertIn("票 2", errors[0])

    def test_a_ticket_map_without_blocks_fails(self):
        """Explicit ``--source list`` compatibility mode still demands ticket blocks."""
        errors = chain.check_artifact("to-tickets", "- 只列了一堆要点\n", source="list")
        self.assertTrue(any("no '## ' ticket blocks" in e for e in errors), errors)

    def test_unrecognisable_text_asks_for_the_format(self):
        errors = chain.check_artifact("to-tickets", "- 只列了一堆要点\n")
        self.assertTrue(any("--source" in e for e in errors), errors)

    def test_a_done_when_line_counts_as_acceptance(self):
        text = "## 票 1\n\n- 改文案\n- Done when: 设置页显示 Auto sync\n"
        self.assertEqual(chain.check_artifact("to-tickets", text), [])

    def test_external_steps_refuse_rather_than_pretend(self):
        errors = chain.check_artifact("implement", self.SPEC_OK)
        self.assertEqual(len(errors), 1)
        self.assertIn("acceptance.py", errors[0])

    def test_an_unknown_step_is_named(self):
        self.assertIn("unknown step", chain.check_artifact("ship-it", "x")[0])

    def test_an_empty_artifact_fails(self):
        self.assertTrue(chain.check_artifact("to-spec", "   "))


class LoadRecordsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "ledger.jsonl"

    def tearDown(self):
        self.directory.cleanup()

    def test_a_missing_ledger_is_reported_not_raised(self):
        records, problems = chain.load_records(self.path)
        self.assertEqual(records, [])
        self.assertTrue(problems and "no ledger" in problems[0])

    def test_a_trailing_partial_line_is_discarded(self):
        self.path.write_text('{"kind":"acceptance"}\n{"kind":"acc', encoding="utf-8")
        records, problems = chain.load_records(self.path)
        self.assertEqual(len(records), 1)
        self.assertTrue(any("partial" in p for p in problems))

    def test_a_corrupt_complete_line_is_reported(self):
        self.path.write_text('{"kind":"acceptance"}\nnot json\n', encoding="utf-8")
        records, problems = chain.load_records(self.path)
        self.assertEqual(len(records), 1)
        self.assertTrue(any("not JSON" in p for p in problems))

    def test_a_non_object_line_is_reported(self):
        self.path.write_text("[1,2]\n", encoding="utf-8")
        records, problems = chain.load_records(self.path)
        self.assertEqual(records, [])
        self.assertTrue(any("not an object" in p for p in problems))

    def test_a_valid_ledger_reads_clean(self):
        self.path.write_text('{"kind":"acceptance"}\n\n', encoding="utf-8")
        records, problems = chain.load_records(self.path)
        self.assertEqual(len(records), 1)
        self.assertEqual(problems, [])


class LedgerPathTests(unittest.TestCase):
    def test_the_home_argument_wins(self):
        self.assertEqual(chain.ledger_path("r1", "/tmp/ledger-home"),
                         Path("/tmp/ledger-home/runs/r1/ledger.jsonl"))


class TicketBoundaryTests(unittest.TestCase):
    """Real upstream ticket shapes must be recognised by their real boundaries.

    ``to-tickets`` may land a directory with one Markdown file per ticket, a single issue
    body, or a combined list. The old check split only on ``## `` and therefore invented
    tickets from an issue's ordinary sections and accepted a file boundary as "no ticket".
    """

    FILE_TICKET = (
        "# Add the retry cap\n\n"
        "## What to build\n\n"
        "Exponential backoff with a 30 s cap.\n\n"
        "## Acceptance criteria\n\n"
        "- The delay is capped at 30 s\n"
    )

    ISSUE_BODY = (
        "## What to build\n\n"
        "Exponential backoff with a 30 s cap.\n\n"
        "## Acceptance criteria\n\n"
        "- The delay is capped at 30 s\n\n"
        "## Out of scope\n\n"
        "- Jitter\n"
    )

    def test_one_file_per_ticket_is_a_ticket(self):
        self.assertEqual(
            chain.check_artifact("to-tickets", self.FILE_TICKET, source="file"), [])

    def test_an_issue_body_is_one_ticket_not_one_per_section(self):
        self.assertEqual(
            chain.check_artifact("to-tickets", self.ISSUE_BODY, source="issue"), [])

    def test_a_combined_ticket_list_still_checks_each_block(self):
        text = ("## 票 1\n\n- 改文案\n- 验收：设置页显示 Auto sync\n\n"
                "## 票 2\n\n- 改样式\n")
        errors = chain.check_artifact("to-tickets", text, source="list")
        self.assertEqual(len(errors), 1)
        self.assertIn("票 2", errors[0])

    def test_ambiguous_input_asks_for_the_format_instead_of_guessing(self):
        """A map title plus a ticket-like `##` heading fits both shapes; do not guess."""
        ambiguous = ("# The map\n\n## Add the retry cap\n\n- Acceptance: the delay is capped\n")
        with self.assertRaises(chain.AmbiguousArtifact) as caught:
            chain.check_artifact("to-tickets", ambiguous)
        self.assertIn("--source", str(caught.exception))

    def test_an_empty_acceptance_line_is_named_at_the_real_ticket(self):
        text = ("# Ticket two\n\n## What to build\n\nThing.\n\n## Acceptance criteria\n\n")
        errors = chain.check_artifact("to-tickets", text, source="file")
        self.assertEqual(len(errors), 1)
        self.assertIn("Ticket two", errors[0])

    def test_unknown_blockers_and_cycles_are_reported_separately(self):
        text = (
            "## 票 1\n\n- Blocked by: 票 9\n- 验收：a\n\n"
            "## 票 2\n\n- Blocked by: 票 3\n- 验收：b\n\n"
            "## 票 3\n\n- Blocked by: 票 2\n- 验收：c\n"
        )
        errors = chain.check_artifact("to-tickets", text, source="list")
        self.assertTrue(any("票 9" in e and "unknown" in e for e in errors), errors)
        self.assertTrue(any("cycle" in e for e in errors), errors)

    def test_a_native_check_and_the_dispatch_additions_are_reported_apart(self):
        body = "## What to build\n\nThing.\n\n## Acceptance criteria\n\n- it works\n"
        result = chain.inspect_spec(body)
        self.assertTrue(result["native"]["passed"])
        self.assertFalse(result["dispatch"]["passed"])
        self.assertTrue(any("Test tier" in e for e in result["dispatch"]["errors"]))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def run_cli(self, *argv):
        completed = subprocess.run([sys.executable, str(CHAIN), *argv],
                                   capture_output=True, text=True, timeout=60)
        return completed.returncode, completed.stdout, completed.stderr
    def test_steps_prints_the_positions_and_the_steps(self):
        code, out, _ = self.run_cli("steps")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual([p["id"] for p in payload["positions"]], list(chain.POSITION_IDS))
        self.assertEqual([s["id"] for s in payload["steps"]], list(chain.STEP_IDS))

    def test_resume_reads_a_ledger_file(self):
        ledger = self.root / "ledger.jsonl"
        ledger.write_text(
            json.dumps(ledger_record({"gate": ["G-docs"], "checks": PASSED_CHECKS})) + "\n",
            encoding="utf-8")
        code, out, _ = self.run_cli("resume", "--run-id", "r1", "--ledger", str(ledger))
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["next"]["position"], "spec")
        self.assertEqual(payload["problems"], [])
        self.assertEqual(payload["legacy"]["state"], "legacy")

    def test_a_flow_record_takes_precedence_over_the_five_position_chain(self):
        """The flow is the plan; the ledger is only an index beside it."""
        flow_file = self.root / "flow.jsonl"
        flow_file.write_text(json.dumps({
            "schema_version": 1, "record_id": "flow-1", "kind": "flow", "flow_id": "f1",
            "run_id": "r1", "goal": "design only", "endpoint": "design artifact",
            "nodes": [
                {"id": "grill", "gate": "G-docs", "depends_on": []},
                {"id": "spec", "gate": "G-spec", "depends_on": ["grill"],
                 "applies": False, "entry_evidence": "the user asked for design only"},
            ],
        }) + "\n" + json.dumps({
            "schema_version": 1, "record_id": "state-1", "kind": "state", "flow_id": "f1",
            "node": "grill", "state": "accepted", "task_id": "t1", "attempt": 1,
            "artifact_version": "v1", "evidence_ref": "answer.md",
        }) + "\n", encoding="utf-8")
        code, out, _ = self.run_cli("resume", "--run-id", "r1", "--flow", str(flow_file))
        self.assertEqual(code, 0, out)
        payload = json.loads(out)
        self.assertEqual(payload["state"], "complete")
        self.assertNotIn("next", payload)
        self.assertIn("not merge authorization", payload["note"])

    def test_an_uncertain_flow_resume_exits_one(self):
        flow_file = self.root / "flow.jsonl"
        flow_file.write_text(json.dumps({
            "schema_version": 1, "record_id": "flow-1", "kind": "flow", "flow_id": "f1",
            "run_id": "r1", "goal": "x", "endpoint": "design artifact",
            "nodes": [{"id": "grill", "gate": "G-docs", "depends_on": []}],
        }) + "\n" + json.dumps({
            "schema_version": 1, "record_id": "state-1", "kind": "state", "flow_id": "f1",
            "node": "ghost", "state": "blocked", "reason": "x",
        }) + "\n", encoding="utf-8")
        code, out, _ = self.run_cli("resume", "--run-id", "r1", "--flow", str(flow_file))
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertEqual(payload["state"], "uncertain")
        self.assertEqual(payload["ready"], [])

    def test_resume_degrades_to_unknown_when_the_archive_is_schema_invalid(self):
        """Archive history is read through the ledger's validator, not a JSON-object reader."""
        run_dir = self.root / "runs" / "r1"
        archive = run_dir / "archive"
        archive.mkdir(parents=True)
        active = run_dir / "ledger.jsonl"
        active.write_text("", encoding="utf-8")
        (archive / "2026-01-01T00-00-00Z-ledger.jsonl").write_text(
            '{"kind": "acceptance"}\n', encoding="utf-8")
        for argv in (("--ledger-home", str(self.root)), ("--ledger", str(active))):
            with self.subTest(entry=argv[0]):
                code, out, _ = self.run_cli("resume", "--run-id", "r1", *argv)
                self.assertEqual(code, 0)
                payload = json.loads(out)
                self.assertEqual(payload["state"], "unknown")
                self.assertIsNone(payload["next"])
                self.assertEqual(payload["history"]["state"], "unknown")

    def test_resume_without_a_ledger_still_answers(self):
        code, out, _ = self.run_cli("resume", "--run-id", "missing", "--ledger-home", str(self.root))
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["next"]["position"], "grill")
        self.assertTrue(payload["problems"])

    def test_exit_check_exit_codes(self):
        good = self.root / "spec.md"
        good.write_text(CheckArtifactTests.SPEC_OK, encoding="utf-8")
        bad = self.root / "bad.md"
        bad.write_text("# nothing\n", encoding="utf-8")
        self.assertEqual(self.run_cli("exit-check", "--step", "to-spec", "--artifact", str(good))[0], 0)
        self.assertEqual(self.run_cli("exit-check", "--step", "to-spec", "--artifact", str(bad))[0], 1)

    def test_a_missing_artifact_fails_with_a_message(self):
        code, out, _ = self.run_cli("exit-check", "--step", "to-spec",
                                    "--artifact", str(self.root / "nope.md"))
        self.assertEqual(code, 1)
        self.assertIn("artifact not found", json.loads(out)["errors"][0])

    def test_a_collection_checks_every_file_as_one_ticket_each(self):
        first = self.root / "01-first.md"
        first.write_text("# 01: First\n\n**Blocked by:** None (can start immediately).\n\n"
                         "- [ ] the behaviour works\n", encoding="utf-8")
        second = self.root / "02-second.md"
        second.write_text("# 02: Second\n\n**Blocked by:** 01: First\n\n- [ ] it works\n",
                          encoding="utf-8")
        code, out, _ = self.run_cli("exit-check", "--step", "to-tickets",
                                    "--artifact", str(first), "--artifact", str(second))
        self.assertEqual(code, 0, out)
        payload = json.loads(out)
        self.assertTrue(payload["passed"])
        self.assertEqual(len(payload["artifacts"]), 2)

    def test_a_collection_names_the_file_that_fails(self):
        good = self.root / "01-good.md"
        good.write_text("# 01: Good\n\n- [ ] works\n", encoding="utf-8")
        bad = self.root / "02-bad.md"
        bad.write_text("# 02: Bad\n\nNo criteria here.\n", encoding="utf-8")
        code, out, _ = self.run_cli("exit-check", "--step", "to-tickets",
                                    "--artifact", str(good), "--artifact", str(bad))
        self.assertEqual(code, 1)
        errors = json.loads(out)["errors"]
        self.assertTrue(any("02-bad.md" in error and "02: Bad" in error for error in errors),
                        errors)

    def test_an_ambiguous_artifact_exits_two_and_asks_for_the_source(self):
        ambiguous = self.root / "map.md"
        ambiguous.write_text("# Map\n\n## Add the cap\n\n- Acceptance: capped\n", encoding="utf-8")
        code, out, _ = self.run_cli("exit-check", "--step", "to-tickets",
                                    "--artifact", str(ambiguous))
        self.assertEqual(code, 2)
        self.assertIn("--source", json.loads(out)["errors"][0])

    def test_the_explicit_source_mode_resolves_the_ambiguity(self):
        ambiguous = self.root / "map.md"
        ambiguous.write_text("# Map\n\n## Add the cap\n\n- Acceptance: capped\n", encoding="utf-8")
        code, out, _ = self.run_cli("exit-check", "--step", "to-tickets",
                                    "--artifact", str(ambiguous), "--source", "list")
        self.assertEqual(code, 0, out)

    def test_an_issue_body_checks_as_one_ticket(self):
        issue = self.root / "issue.md"
        issue.write_text(
            "## What to build\n\nExponential backoff.\n\n"
            "## Acceptance criteria\n\n- [ ] capped at 30 s\n", encoding="utf-8")
        code, out, _ = self.run_cli("exit-check", "--step", "to-tickets",
                                    "--artifact", str(issue), "--source", "issue")
        self.assertEqual(code, 0, out)

    def test_the_spec_report_separates_native_from_dispatch(self):
        spec = self.root / "spec.md"
        spec.write_text("## Problem Statement\n\nx\n\n## Acceptance criteria\n\n- works\n",
                        encoding="utf-8")
        code, out, _ = self.run_cli("exit-check", "--step", "to-spec",
                                    "--artifact", str(spec))
        payload = json.loads(out)
        self.assertEqual(code, 1)
        self.assertTrue(payload["native"]["passed"])
        self.assertFalse(payload["dispatch"]["passed"])
        self.assertTrue(any("Test tier" in error for error in payload["dispatch"]["errors"]))


if __name__ == "__main__":
    unittest.main()
