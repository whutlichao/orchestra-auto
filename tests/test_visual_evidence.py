#!/usr/bin/env python3
"""Behaviour tests for ``visual-evidence.py``, the quiet screenshot batch.

The ticket behind this module: an agent gathering Orca-rendered screenshots minimized the
Orca window through Accessibility, shot a batch, then restored it with ``set frontmost``.
The user saw the window cycle and lost focus. The fix keeps the render and removes the
window interference, so the tests are about the *sequence* of window calls, not only about
a PNG existing:

* exactly one hide and one unhide per batch, with every shot between them;
* ``set frontmost`` only to restore a batch that started with Orca frontmost;
* a focus drift or a failed restore is reported and exits 3;
* a plan that needs no hidden window never touches visibility;
* an unobservable window refuses before hiding anything.

The seams are the module constant ``HOST_PLATFORM`` and the ``--orca`` / ``--osascript``
executables. Tests install stub executables that record their own invocations, so no test
reads or changes a real window and the suite runs on Linux CI.
"""

import contextlib
import importlib.util
import io
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_NAMES = ("orchestra-auto", "orchestra-auto-zh")
SCRIPT_RELATIVE = Path("scripts") / "visual-evidence.py"
REFERENCE_RELATIVE = Path("references") / "visual-evidence.md"

OSASCRIPT_STUB = '''#!{python}
import json, os, sys

base = os.path.dirname(os.path.abspath(__file__))
state_path = os.path.join(base, "window.json")
calls_path = os.path.join(base, "osascript-calls.txt")
state = json.load(open(state_path))
script = sys.argv[-1]
with open(calls_path, "a") as out:
    out.write(script.replace("\\n", " ") + "\\n")


def save():
    with open(state_path, "w") as out:
        json.dump(state, out)


if "set visible to false" in script:
    if state.get("fail_hide"):
        sys.stderr.write("hide denied\\n")
        sys.exit(1)
    state["visible"] = False
    if state["frontmost"] == "Orca":
        state["frontmost"] = state.get("after_hide_frontmost", "Terminal")
    save()
    sys.exit(0)
if "set visible to true" in script:
    if state.get("fail_show"):
        sys.stderr.write("show denied\\n")
        sys.exit(1)
    state["visible"] = True
    save()
    sys.exit(0)
if "set frontmost to true" in script:
    state["frontmost"] = "Orca"
    save()
    sys.exit(0)
if state.get("fail_probe"):
    sys.stderr.write("probe denied\\n")
    sys.exit(1)
state["probes"] = state.get("probes", 0) + 1
queue = state.get("frontmost_queue") or []
if state["probes"] > 1 and queue:
    state["frontmost"] = queue.pop(0)
print(state["frontmost"])
print("true" if state["visible"] else "false")
print("true" if state["minimized"] else "false")
print(str(state.get("windows", 1)))
save()
'''

ORCA_STUB = '''#!{python}
import json, os, sys

base = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[1:]
with open(os.path.join(base, "orca-calls.txt"), "a") as out:
    out.write(json.dumps(args) + "\\n")
out_path = args[args.index("--out") + 1]
parent = os.path.dirname(out_path)
if parent:
    os.makedirs(parent, exist_ok=True)
scenario = args[args.index("--scenario") + 1] if "--scenario" in args else ""
if "fail" in scenario or "fail" in os.path.basename(out_path):
    sys.stderr.write("scenario failed\\n")
    print(json.dumps({{"ok": False, "error": {{"code": "scenario_failed"}}}}))
    sys.exit(1)
with open(out_path, "wb") as image:
    image.write(b"\\x89PNG\\r\\n\\x1a\\n")
theme = args[args.index("--theme") + 1] if "--theme" in args else None
print(json.dumps({{"ok": True, "result": {{
    "path": out_path, "settled": True, "themeApplied": bool(theme),
    "errors": [], "width": 900, "height": 100, "theme": theme}}}}))
'''


def load_script(skill_name):
    path = REPO_ROOT / "skills" / skill_name / SCRIPT_RELATIVE
    spec = importlib.util.spec_from_file_location(
        "visual_evidence_" + skill_name.replace("-", "_"), path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot build an import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_executable(path, text):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class Sandbox:
    """A temporary directory with a window state, an orca stub and an osascript stub."""

    def __init__(self, frontmost="ego lite", visible=True, minimized=False, **extra):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.state = {
            "frontmost": frontmost,
            "frontmost_queue": [],
            "visible": visible,
            "minimized": minimized,
            "windows": 1,
        }
        self.state.update(extra)
        self.write_state()
        self.osascript_calls = self.root / "osascript-calls.txt"
        self.orca_calls = self.root / "orca-calls.txt"
        self.osascript = write_executable(
            self.root / "osascript", OSASCRIPT_STUB.format(python=sys.executable))
        self.orca = write_executable(
            self.root / "orca", ORCA_STUB.format(python=sys.executable))

    def write_state(self):
        (self.root / "window.json").write_text(json.dumps(self.state))

    def plan(self, shots, plugin=None):
        path = self.root / "plan.json"
        path.write_text(json.dumps({
            "plugin": str(plugin or self.root),
            "shots": [dict(shot, out=str(self.root / "shots" / shot["out"]))
                      for shot in shots],
        }))
        return path

    def calls(self, path):
        if not path.exists():
            return []
        return [line for line in path.read_text().splitlines() if line]

    def close(self):
        self.directory.cleanup()


class ModuleCase(unittest.TestCase):
    """Shared helper: run ``main`` in-process against a sandbox."""

    skill = "orchestra-auto"

    def setUp(self):
        self.module = load_script(self.skill)
        self.module.__dict__["HOST_PLATFORM"] = "darwin"

    def sandbox(self, **kwargs):
        box = Sandbox(**kwargs)
        self.addCleanup(box.close)
        return box

    def run_main(self, sandbox, extra):
        argv = ["run", "--orca", str(sandbox.orca),
                "--osascript", str(sandbox.osascript)] + extra
        output = io.StringIO()
        with contextlib.redirect_stdout(output), \
                contextlib.redirect_stderr(io.StringIO()):
            code = self.module.main(argv)
        return code, json.loads(output.getvalue())

    def assert_sequence(self, sandbox, report):
        calls = sandbox.calls(sandbox.osascript_calls)
        hides = [index for index, call in enumerate(calls) if "visible to false" in call]
        shows = [index for index, call in enumerate(calls) if "visible to true" in call]
        self.assertEqual(len(hides), 1, calls)
        self.assertEqual(len(shows), 1, calls)
        self.assertLess(hides[0], shows[0], calls)
        self.assertTrue(report["window"]["hiddenByScript"])
        self.assertTrue(report["window"]["restored"])
        return calls, hides[0], shows[0]


class PlanValidationTests(ModuleCase):
    def test_valid_plan_resolves_absolute_outputs(self):
        box = self.sandbox()
        plan = box.plan([{"scenario": "ready", "out": "a.png", "theme": "dark"}])
        plugin, shots = self.module.load_plan(plan)
        self.assertEqual(plugin, str(box.root))
        self.assertTrue(shots[0]["out"].startswith("/"))

    def test_unknown_keys_are_rejected(self):
        box = self.sandbox()
        plan = box.plan([{"scenario": "ready", "out": "a.png", "scenaro": "typo"}])
        with self.assertRaises(self.module.PlanError):
            self.module.load_plan(plan)

    def test_duplicate_outputs_are_rejected(self):
        box = self.sandbox()
        plan = box.plan([{"scenario": "a", "out": "same.png"},
                         {"scenario": "b", "out": "same.png"}])
        with self.assertRaises(self.module.PlanError):
            self.module.load_plan(plan)

    def test_scenario_and_act_cannot_combine(self):
        box = self.sandbox()
        plan = box.plan([{"scenario": "ready", "act": [{"press": {"text": "Go"}}],
                          "out": "a.png"}])
        with self.assertRaises(self.module.PlanError):
            self.module.load_plan(plan)

    def test_only_scenario_or_act_need_a_hidden_window(self):
        self.assertFalse(self.module.needs_hidden_window(
            [{"scenario": None, "act": None}]))
        self.assertTrue(self.module.needs_hidden_window(
            [{"scenario": "ready", "act": None}]))
        self.assertTrue(self.module.needs_hidden_window(
            [{"scenario": None, "act": [{"press": {"text": "Go"}}]}]))

    def test_window_decisions(self):
        self.assertEqual(self.module.decide_window(
            {"orca": "missing", "visible": False, "minimized": False}), "refuse-missing")
        self.assertEqual(self.module.decide_window(
            {"orca": "present", "visible": False, "minimized": False}), "already-hidden")
        self.assertEqual(self.module.decide_window(
            {"orca": "present", "visible": True, "minimized": True}), "already-minimized")
        self.assertEqual(self.module.decide_window(
            {"orca": "present", "visible": True, "minimized": False}), "hide")


class BatchTests(ModuleCase):
    def test_batch_hides_once_and_never_touches_frontmost(self):
        box = self.sandbox(frontmost="ego lite")
        plan = box.plan([
            {"scenario": "ready", "out": "ready-dark.png", "width": 900, "theme": "dark"},
            {"scenario": "empty", "out": "empty-dark.png", "theme": "dark"},
        ])
        code, report = self.run_main(box, ["--plan", str(plan)])
        self.assertEqual(code, 0, report)
        calls, hide_at, show_at = self.assert_sequence(box, report)
        self.assertFalse(
            [call for call in calls if "set frontmost" in call],
            "a batch that started behind another app must not activate Orca")
        self.assertEqual(report["window"]["frontmostBefore"], "ego lite")
        self.assertEqual(report["window"]["frontmostAfter"], "ego lite")
        self.assertTrue(report["window"]["focusRestored"])
        self.assertFalse(report["window"]["activatedOnRestore"])
        shots = box.calls(box.orca_calls)
        self.assertEqual(len(shots), 2)
        self.assertTrue(all("--scenario" in line for line in shots))
        self.assertTrue(all(report["shots"][index]["ok"] for index in range(2)))
        self.assertTrue(all(report["shots"][index]["settled"] for index in range(2)))
        self.assertTrue(Path(report["shots"][0]["out"]).exists())
        self.assertLess(hide_at, show_at)

    def test_frontmost_orca_is_restored_after_the_batch(self):
        box = self.sandbox(frontmost="Orca", after_hide_frontmost="Terminal")
        plan = box.plan([{"scenario": "ready", "out": "ready.png"}])
        code, report = self.run_main(box, ["--plan", str(plan)])
        self.assertEqual(code, 0, report)
        calls = box.calls(box.osascript_calls)
        activations = [index for index, call in enumerate(calls)
                       if "set frontmost to true" in call]
        show_at = [index for index, call in enumerate(calls)
                   if "visible to true" in call][0]
        self.assertEqual(len(activations), 1, calls)
        self.assertLess(show_at, activations[0],
                        "unhide first, then put the frontmost app back")
        self.assertTrue(report["window"]["activatedOnRestore"])
        self.assertTrue(report["window"]["focusRestored"])
        self.assertEqual(report["window"]["frontmostAfter"], "Orca")

    def test_a_user_switch_during_the_batch_is_not_overridden(self):
        """When the person starts using another app while Orca is hidden, leave them there."""
        box = self.sandbox(frontmost="Orca", after_hide_frontmost="Terminal",
                           frontmost_queue=["Terminal", "Safari"])
        plan = box.plan([{"scenario": "ready", "out": "ready.png"}])
        code, report = self.run_main(box, ["--plan", str(plan)])
        self.assertEqual(code, 0, report)
        calls = box.calls(box.osascript_calls)
        self.assertFalse([call for call in calls if "set frontmost" in call],
                         "restoring must not take focus back from a person using another app")
        self.assertTrue(report["window"]["userTookOver"])
        self.assertFalse(report["window"]["activatedOnRestore"])
        self.assertTrue(report["window"]["focusRestored"])
        self.assertEqual(report["window"]["frontmostBefore"], "Orca")
        self.assertEqual(report["window"]["frontmostWhileHidden"], "Terminal")
        self.assertEqual(report["window"]["frontmostAfter"], "Safari")

    def test_focus_drift_is_reported_and_fails_the_batch(self):
        box = self.sandbox(frontmost="ego lite", frontmost_queue=["ego lite", "Orca"])
        plan = box.plan([{"scenario": "ready", "out": "ready.png"}])
        code, report = self.run_main(box, ["--plan", str(plan)])
        self.assertEqual(code, self.module.EXIT_WINDOW_CHANGED, report)
        self.assertFalse(report["window"]["focusRestored"])
        self.assertTrue(report["window"]["restored"])
        self.assertIn("window_not_restored",
                      [error["code"] for error in report["errors"]])

    def test_failed_restore_is_reported_and_fails_the_batch(self):
        box = self.sandbox(fail_show=True)
        plan = box.plan([{"scenario": "ready", "out": "ready.png"}])
        code, report = self.run_main(box, ["--plan", str(plan)])
        self.assertEqual(code, self.module.EXIT_WINDOW_CHANGED, report)
        self.assertFalse(report["window"]["restored"])
        self.assertIn("window_not_restored",
                      [error["code"] for error in report["errors"]])

    def test_a_failed_shot_still_restores_the_window(self):
        box = self.sandbox()
        plan = box.plan([{"scenario": "fail", "out": "ready.png"}])
        code, report = self.run_main(box, ["--plan", str(plan)])
        self.assertEqual(code, self.module.EXIT_SHOT_FAILED, report)
        self.assertFalse(report["shots"][0]["ok"])
        self.assertTrue(report["window"]["restored"])
        self.assertTrue(report["window"]["focusRestored"])
        self.assertEqual(len([call for call in box.calls(box.osascript_calls)
                              if "visible to true" in call]), 1)

    def test_a_window_already_hidden_is_left_alone(self):
        box = self.sandbox(visible=False)
        plan = box.plan([{"scenario": "ready", "out": "ready.png"}])
        code, report = self.run_main(box, ["--plan", str(plan)])
        self.assertEqual(code, 0, report)
        self.assertFalse(report["window"]["hiddenByScript"])
        self.assertEqual(report["window"]["visibilityBefore"], "hidden")
        self.assertEqual(report["window"]["visibilityAfter"], "hidden")
        calls = box.calls(box.osascript_calls)
        self.assertFalse([call for call in calls if "visible to" in call])
        self.assertTrue(report["window"]["restored"])

    def test_a_plain_panel_plan_never_hides_the_window(self):
        box = self.sandbox()
        plan = box.plan([{"panel": "main", "out": "panel.png"}])
        code, report = self.run_main(box, ["--plan", str(plan)])
        self.assertEqual(code, 0, report)
        self.assertFalse(report["window"]["hiddenByScript"])
        calls = box.calls(box.osascript_calls)
        self.assertFalse([call for call in calls if "visible to" in call])
        self.assertEqual(len(box.calls(box.orca_calls)), 1)

    def test_dry_run_touches_nothing(self):
        box = self.sandbox()
        plan = box.plan([{"scenario": "ready", "out": "ready.png"}])
        code, report = self.run_main(box, ["--plan", str(plan), "--dry-run"])
        self.assertEqual(code, 0, report)
        self.assertTrue(report["dryRun"])
        self.assertTrue(report["window"]["hiddenByScript"])
        self.assertEqual(box.calls(box.orca_calls), [])
        calls = box.calls(box.osascript_calls)
        self.assertEqual(len(calls), 1, "dry run probes once, changes nothing")
        self.assertFalse([call for call in calls if "visible to" in call])

    def test_a_failed_hide_refuses_before_shooting(self):
        box = self.sandbox(fail_hide=True)
        plan = box.plan([{"scenario": "ready", "out": "ready.png"}])
        code, report = self.run_main(box, ["--plan", str(plan)])
        self.assertEqual(code, self.module.EXIT_USAGE, report)
        self.assertEqual(box.calls(box.orca_calls), [])
        self.assertIn("hide_failed", [error["code"] for error in report["errors"]])

    def test_an_unobservable_window_refuses_to_hide(self):
        box = self.sandbox(fail_probe=True)
        plan = box.plan([{"scenario": "ready", "out": "ready.png"}])
        code, report = self.run_main(box, ["--plan", str(plan)])
        self.assertEqual(code, self.module.EXIT_USAGE, report)
        self.assertEqual(box.calls(box.orca_calls), [])
        self.assertIn("probe_failed", [error["code"] for error in report["errors"]])
        self.assertFalse(report["window"]["hiddenByScript"])

    def test_budget_expiry_stops_before_a_shot_and_still_restores(self):
        box = self.sandbox()
        plan = box.plan([{"scenario": "ready", "out": "ready.png"}])
        code, report = self.run_main(
            box, ["--plan", str(plan), "--max-seconds", "-1"])
        self.assertEqual(code, self.module.EXIT_BUDGET, report)
        self.assertTrue(report["window"]["budgetExceeded"])
        self.assertEqual(box.calls(box.orca_calls), [])
        self.assertTrue(report["window"]["restored"])
        self.assertEqual(len([call for call in box.calls(box.osascript_calls)
                              if "visible to true" in call]), 1)

    def test_report_file_is_written(self):
        box = self.sandbox()
        plan = box.plan([{"scenario": "ready", "out": "ready.png"}])
        report_path = box.root / "report.json"
        code, report = self.run_main(
            box, ["--plan", str(plan), "--report", str(report_path)])
        self.assertEqual(code, 0, report)
        written = json.loads(report_path.read_text())
        self.assertEqual(written["window"], report["window"])

    def test_schema_documents_plan_and_report(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(self.module.main(["run", "--print-schema"]), 0)
        schema = json.loads(output.getvalue())
        self.assertEqual(schema["plan"]["required"], ["plugin", "shots"])
        self.assertIn("focusRestored", schema["report"]["window"])
        self.assertIn("restored", schema["report"]["window"])


class PlatformTests(ModuleCase):
    def test_non_macos_refuses_before_probing(self):
        box = self.sandbox()
        self.module.__dict__["HOST_PLATFORM"] = "linux"
        plan = box.plan([{"scenario": "ready", "out": "ready.png"}])
        code, report = self.run_main(box, ["--plan", str(plan)])
        self.assertEqual(code, self.module.EXIT_USAGE, report)
        self.assertEqual(report["errors"][0]["code"], "unsupported_platform")
        self.assertEqual(box.calls(box.osascript_calls), [])
        self.assertEqual(box.calls(box.orca_calls), [])


class ChineseBatchTests(BatchTests):
    """The zh bundle ships the same bytes; the sequence contract holds there too."""

    skill = "orchestra-auto-zh"


class DocumentContractTests(unittest.TestCase):
    def test_each_bundle_ships_the_reference_with_its_invariants(self):
        for name in SKILL_NAMES:
            with self.subTest(bundle=name):
                text = (REPO_ROOT / "skills" / name / REFERENCE_RELATIVE).read_text()
                for marker in ("visual-evidence.py", "restored", "focusRestored",
                               "hiddenSeconds", "AXMinimizeButton", "set frontmost"):
                    self.assertIn(marker, text, f"{name}: reference lost {marker}")

    def test_the_reference_is_routed_from_the_entrypoint(self):
        for name in SKILL_NAMES:
            with self.subTest(bundle=name):
                text = (REPO_ROOT / "skills" / name / "SKILL.md").read_text()
                self.assertIn("references/visual-evidence.md", text)

    def test_the_evidence_gate_points_at_the_protocol(self):
        for name in SKILL_NAMES:
            with self.subTest(bundle=name):
                text = (REPO_ROOT / "skills" / name / "references"
                        / "gate-acceptance.md").read_text()
                self.assertIn("visual-evidence.md", text)
                self.assertIn("focusRestored", text)


if __name__ == "__main__":
    unittest.main()
