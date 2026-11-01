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
        errors = chain.check_artifact("to-tickets", "- 只列了一堆要点\n")
        self.assertTrue(any("no '## ' ticket blocks" in e for e in errors), errors)

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
        ledger.write_text(json.dumps(acceptance("G-docs", checks=PASSED_CHECKS)) + "\n",
                          encoding="utf-8")
        code, out, _ = self.run_cli("resume", "--run-id", "r1", "--ledger", str(ledger))
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["next"]["position"], "spec")
        self.assertEqual(payload["problems"], [])

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


if __name__ == "__main__":
    unittest.main()
