#!/usr/bin/env python3
"""Guard the CI contract, not the CI output.

The workflow exists to run one command. If someone renames that command, drops an operating
system the skill claims to support, or lets the required check name drift away from what
CONTRIBUTING tells a contributor to expect, the suite should say so here rather than in a red
pull request nobody can explain.

These assertions are deliberately loose about formatting — YAML layout and action versions will
change — and strict about the three things that would make CI a lie: the command, the platforms,
and the check name.
"""

from pathlib import Path
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests.yml"
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"


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


class ContributingTests(unittest.TestCase):
    def test_contributing_states_the_required_check(self):
        """A contributor has to know which check gates the merge, and that CI runs the suite."""
        text = CONTRIBUTING.read_text(encoding="utf-8")
        self.assertIn("tests", text)
        self.assertIn("GitHub Actions", text)


if __name__ == "__main__":
    unittest.main()
