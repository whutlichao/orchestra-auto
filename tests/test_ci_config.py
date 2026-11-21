#!/usr/bin/env python3
"""Guard the CI contract, not the CI output.

The workflow exists to run one command. If someone renames that command, drops an operating
system the skill claims to support, or lets the required check name drift away from what
CONTRIBUTING tells a contributor to expect, the suite should say so here rather than in a red
pull request nobody can explain.

These assertions are deliberately loose about formatting — YAML layout and action versions will
change — and strict about the three things that would make CI a lie: the command, the platforms,
and the check name. The same reasoning covers the quick loop CONTRIBUTING hands a contributor
directly, so that list is held to the modules on disk too.
"""

from pathlib import Path
import re
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests.yml"
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"

#: The one module macOS deliberately skips: its readiness budget is tuned for a laptop.
MACOS_EXCLUDED = "test_progress_dashboard"


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(WORKFLOW.exists(), "no .github/workflows/tests.yml")
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def test_it_runs_the_documented_command(self):
        """CI must run the same command CONTRIBUTING gives a contributor, not a variant."""
        self.assertIn("python -m unittest discover -s tests", self.text)

    def test_it_covers_both_supported_platforms(self):
        """The skill is documented as macOS/Linux/WSL only; both supported systems get a runner."""
        for runner in ("ubuntu-latest", "macos-latest"):
            self.assertIn(runner, self.text)
        self.assertNotIn("windows-latest", self.text.lower())

    def test_it_pins_the_floor_python(self):
        """3.10 is the floor because the package tests use zip(..., strict=True)."""
        self.assertIn('"3.10"', self.text)

    def test_it_has_a_timeout_and_a_job_name(self):
        """A run that hangs must fail rather than occupy a runner for an hour."""
        self.assertIn("timeout-minutes:", self.text)
        self.assertIn("name: test", self.text)

    def test_the_macos_list_covers_every_module_except_the_socket_bound_one(self):
        """A new test module must not silently never run on macOS."""
        modules = {path.stem for path in (REPO_ROOT / "tests").glob("test_*.py")}
        self.assertIn(MACOS_EXCLUDED, modules)
        expected = modules - {MACOS_EXCLUDED}
        blocks = self.text.split("matrix.os == 'macos-latest'", 1)
        self.assertEqual(len(blocks), 2, "the workflow lost its macOS step")
        listed = set(re.findall(r"\btests\.([a-z0-9_]+)\b", blocks[1]))
        self.assertEqual(listed, expected,
                         "the macOS list drifted from tests/test_*.py")


class ContributingTests(unittest.TestCase):
    def test_contributing_states_the_required_check(self):
        """A contributor has to know which check gates the merge, and that CI runs the suite."""
        text = CONTRIBUTING.read_text(encoding="utf-8")
        self.assertIn("tests", text)
        self.assertIn("GitHub Actions", text)

    def test_the_quick_loop_lists_every_test_module(self):
        """A contributor works from the short loop, so a module missing there is skipped until CI."""
        modules = {path.stem for path in (REPO_ROOT / "tests").glob("test_*.py")}
        text = CONTRIBUTING.read_text(encoding="utf-8")
        blocks = text.split("If you only want a quick loop", 1)
        self.assertEqual(len(blocks), 2, "CONTRIBUTING lost its quick-loop paragraph")
        # The first command block after that paragraph is the quick loop itself.
        block = re.search(r"```bash\n(.*?)```", blocks[1], re.DOTALL)
        self.assertIsNotNone(block, "CONTRIBUTING lost the quick-loop command block")
        # Match module names only in command position, so a prose mention cannot satisfy the check.
        listed = set(re.findall(r"^python3 -m unittest tests\.([a-z0-9_]+)",
                                block.group(1), re.MULTILINE))
        self.assertEqual(listed, modules,
                         "the quick-loop list drifted from tests/test_*.py")


if __name__ == "__main__":
    unittest.main()
