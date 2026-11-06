#!/usr/bin/env python3
"""Behaviour tests for ``init-check.py``, the repository bootstrap probe.

The declared seams are deliberate and match the ticket:

* **Module seam** for the report — ``check(repo, runner=..., triage_candidates=...)`` takes the
  command runner and the places ``triage`` may be installed as arguments, so every tracker answer
  is injected and no test needs a GitHub login, a network connection or this machine's ``gh``.
* **CLI seam** for the coordinator surface — the script runs as a subprocess. The tracker CLI is
  a stub on an isolated ``PATH``; the "no ``gh`` installed" case isolates ``PATH`` down to a
  single ``git`` link, so absence is real absence rather than a mock of ``shutil.which``.

Fixtures are temporary directories. The repository is initialized with a real ``git init`` because
``git`` is the one dependency the probe cannot work without, and it works offline.

The exit-code rule is tested at both seams: ``unknown`` must never degrade into ``uninitialized``,
which is the mistake the prose made and the script exists to prevent.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_NAMES = ("orchestra-auto", "orchestra-auto-zh")
SKILL_DIRS = tuple(REPO_ROOT / "skills" / name for name in SKILL_NAMES)
SCRIPT_RELATIVE = Path("scripts") / "init-check.py"
SCRIPT = SKILL_DIRS[0] / SCRIPT_RELATIVE

AGENTS_WITH_HEADING = "# Rules\n\n## Agent skills\n\n- `triage` carries the label vocabulary.\n"
AGENTS_WITHOUT_HEADING = "# Rules\n\n## Working agreements\n\n- No skills block here.\n"
DOMAIN_TEXT = "# Domain\n\nOne context, one glossary.\n"
TRIAGE_LABELS_TEXT = "# Triage labels\n\n- ready-for-agent\n"
GITHUB_TRACKER = "# Issue tracker: GitHub\n\nIssues live in GitHub.\n"
LOCAL_TRACKER = "# Issue tracker: local markdown\n\nDocuments in the repository.\n"


def load_module(skill_dir):
    """Import a bundle's ``init-check.py`` by path, without touching ``sys.path``."""
    path = skill_dir / SCRIPT_RELATIVE
    spec = importlib.util.spec_from_file_location(
        "init_check_" + skill_dir.name.replace("-", "_"), path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot build an import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULE = load_module(SKILL_DIRS[0])
MODULES = {
    name: load_module(directory)
    for name, directory in zip(SKILL_NAMES, SKILL_DIRS, strict=True)
}


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def git_init(root):
    """Make ``root`` a real repository root. No commit is needed for ``rev-parse``."""
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    return root


def build_repository(
    root,
    *,
    git=True,
    bootstrap: str | None = "AGENTS.md",
    agents_text=AGENTS_WITH_HEADING,
    extra_files=None,
    domain_text=DOMAIN_TEXT,
    tracker_text: str | None = LOCAL_TRACKER,
    triage_labels_text: str | None = TRIAGE_LABELS_TEXT,
):
    """Build one fixture repository and return its root.

    Every knob is a documented failure mode: a missing heading, an empty domain document, an
    absent tracker CLI (through the runner) and a local tracker each have a test.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if git:
        git_init(root)
    if bootstrap is not None:
        write(root / bootstrap, agents_text)
    for name, text in (extra_files or {}).items():
        write(root / name, text)
    write(root / "docs" / "agents" / "domain.md", domain_text)
    if tracker_text is not None:
        write(root / "docs" / "agents" / "issue-tracker.md", tracker_text)
    if triage_labels_text is not None:
        write(root / "docs" / "agents" / "triage-labels.md", triage_labels_text)
    return root


def triage_skill(root):
    """An installed ``triage`` skill candidate for the injected resolution order."""
    return write(Path(root) / "install" / "triage" / "SKILL.md", "---\nname: triage\n---\n")


def completed(returncode=0, stdout="", stderr=""):
    return {"returncode": returncode, "stdout": stdout, "stderr": stderr}


def git_answer(root):
    return {
        ("git", "-C", str(root), "rev-parse", "--show-toplevel"): completed(stdout=str(root) + "\n"),
    }


def gh_answer(project="owner/repo"):
    return {
        ("gh", "repo", "view", "--json", "nameWithOwner"):
            completed(stdout=json.dumps({"nameWithOwner": project}) + "\n"),
    }


class FakeRunner:
    """Answer probe commands from a table, and record every command it was asked to run.

    ``CommandMissing`` is raised from the answer table, because "no ``gh`` on PATH" is a
    distinct outcome from "``gh`` failed" and the probe must not conflate them.
    """

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, argv, cwd):
        self.calls.append((tuple(argv), str(cwd)))
        key = tuple(argv)
        if key not in self.answers:
            raise AssertionError(f"unexpected command: {list(argv)}")
        outcome = self.answers[key]
        if isinstance(outcome, Exception):
            raise outcome
        return subprocess.CompletedProcess(args=list(argv), **outcome)

    def commands(self):
        return [call[0] for call in self.calls]


def missing_command_runner(argv, cwd):
    """A runner for the one case a command table cannot express: the binary is not installed."""
    raise MODULE.CommandMissing(argv[0])


class ReportShapeTests(unittest.TestCase):
    def test_the_json_report_has_the_documented_keys_and_order(self):
        """A fixed order is what lets a caller diff two rounds; an extra key is a broken shape."""
        with tempfile.TemporaryDirectory() as temporary:
            root = build_repository(Path(temporary) / "repo")
            report = MODULE.check(root, runner=FakeRunner(git_answer(root)), triage_candidates=[])
        self.assertEqual(list(report), ["status", "repo", "checks"])
        self.assertEqual([check["id"] for check in report["checks"]], list(MODULE.CHECK_IDS))
        self.assertEqual(MODULE.CHECK_IDS, ("repository", "agent-skills", "docs-agents", "tracker"))
        for check in report["checks"]:
            self.assertEqual(list(check), ["id", "result", "detail"])
            self.assertIn(check["result"], {"pass", "fail", "unknown", "skipped"})
            self.assertEqual(check["detail"], check["detail"].strip())
            self.assertNotIn("\n", check["detail"])
        self.assertEqual(report["repo"], str(root.resolve()))

    def test_the_human_surface_is_one_tab_separated_line_per_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = build_repository(Path(temporary) / "repo")
            report = MODULE.check(root, runner=FakeRunner(git_answer(root)), triage_candidates=[])
        lines = MODULE.render_human(report).splitlines()
        self.assertEqual(len(lines), 5)
        for line, check in zip(lines[:4], report["checks"], strict=True):
            self.assertEqual(line.split("\t"), [check["id"], check["result"], check["detail"]])
        self.assertEqual(lines[4], "status: initialized")

    def test_both_bundles_report_the_same_probable_facts(self):
        """The copies are byte-identical; this pins the behaviour the parity test cannot see."""
        with tempfile.TemporaryDirectory() as temporary:
            root = build_repository(Path(temporary) / "repo", tracker_text=GITHUB_TRACKER)
            runner = FakeRunner({**git_answer(root), **gh_answer()})
            reports = {
                name: module.check(root, runner=runner, triage_candidates=[])
                for name, module in MODULES.items()
            }
        self.assertEqual(reports["orchestra-auto"], reports["orchestra-auto-zh"])


class CheckTests(unittest.TestCase):
    def report(self, root, answers=None, triage_candidates=()):
        return MODULE.check(
            root,
            runner=FakeRunner(answers if answers is not None else git_answer(root)),
            triage_candidates=list(triage_candidates),
        )

    def result_of(self, report, check_id):
        return next(check for check in report["checks"] if check["id"] == check_id)

    def test_a_fully_initialized_repository_passes_all_four_checks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = build_repository(Path(temporary) / "repo", tracker_text=GITHUB_TRACKER)
            report = self.report(
                root,
                {**git_answer(root), **gh_answer()},
                triage_candidates=[triage_skill(temporary)],
            )
        self.assertEqual(report["status"], "initialized")
        self.assertEqual([check["result"] for check in report["checks"]], ["pass"] * 4)
        self.assertEqual(MODULE.STATUS_TO_EXIT[report["status"]], 0)

    def test_a_missing_agent_skills_heading_is_a_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = build_repository(Path(temporary) / "repo", agents_text=AGENTS_WITHOUT_HEADING)
            report = self.report(root)
        self.assertEqual(self.result_of(report, "agent-skills")["result"], "fail")
        self.assertEqual(report["status"], "uninitialized")
        self.assertEqual(MODULE.STATUS_TO_EXIT[report["status"]], 1)

    def test_no_bootstrap_file_at_all_is_a_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = build_repository(Path(temporary) / "repo", bootstrap=None)
            report = self.report(root)
        self.assertEqual(self.result_of(report, "agent-skills")["result"], "fail")

    def test_an_empty_domain_document_is_a_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = build_repository(Path(temporary) / "repo", domain_text=" \n\t\n")
            report = self.report(root)
        detail = self.result_of(report, "docs-agents")["detail"]
        self.assertEqual(self.result_of(report, "docs-agents")["result"], "fail")
        self.assertIn("domain.md is empty", detail)

    def test_claude_md_wins_and_agents_md_is_not_a_fallback(self):
        """Setup edits ``CLAUDE.md`` when it exists, so the probe reads the same file setup edits."""
        with tempfile.TemporaryDirectory() as temporary:
            root = build_repository(
                Path(temporary) / "repo", bootstrap="CLAUDE.md",
                extra_files={"AGENTS.md": AGENTS_WITHOUT_HEADING},
            )
            report = self.report(root)
        self.assertEqual(self.result_of(report, "agent-skills")["result"], "pass")
        self.assertIn("CLAUDE.md", self.result_of(report, "agent-skills")["detail"])

    def test_a_claude_md_without_the_heading_fails_even_when_agents_md_has_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = build_repository(
                Path(temporary) / "repo", bootstrap="CLAUDE.md",
                agents_text=AGENTS_WITHOUT_HEADING,
                extra_files={"AGENTS.md": AGENTS_WITH_HEADING},
            )
            report = self.report(root)
        self.assertEqual(self.result_of(report, "agent-skills")["result"], "fail")
        self.assertIn("CLAUDE.md", self.result_of(report, "agent-skills")["detail"])

    def test_triage_labels_are_required_when_triage_is_installed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = build_repository(Path(temporary) / "repo", triage_labels_text=None)
            report = self.report(root, triage_candidates=[triage_skill(temporary)])
        self.assertEqual(self.result_of(report, "docs-agents")["result"], "fail")
        self.assertIn("triage-labels.md is missing", self.result_of(report, "docs-agents")["detail"])

    def test_triage_labels_are_not_required_when_triage_is_not_installed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = build_repository(Path(temporary) / "repo", triage_labels_text=None)
            report = self.report(root, triage_candidates=[])
        self.assertEqual(self.result_of(report, "docs-agents")["result"], "pass")
        self.assertIn("triage not installed", self.result_of(report, "docs-agents")["detail"])

    def test_a_local_markdown_tracker_is_skipped_and_still_initialized(self):
        """``skipped`` is not a missing criterion: it means the check does not apply."""
        with tempfile.TemporaryDirectory() as temporary:
            root = build_repository(Path(temporary) / "repo")  # LOCAL_TRACKER by default
            report = self.report(root)
        self.assertEqual(self.result_of(report, "tracker")["result"], "skipped")
        self.assertEqual(
            self.result_of(report, "tracker")["detail"], "n/a (local markdown tracker)"
        )
        self.assertEqual(report["status"], "initialized")
        self.assertEqual(MODULE.STATUS_TO_EXIT[report["status"]], 0)

    def test_an_issue_tracker_document_without_the_heading_is_skipped(self):
        """A tracker document with no heading is the local-markdown shape, not an unknown one."""
        with tempfile.TemporaryDirectory() as temporary:
            root = build_repository(
                Path(temporary) / "repo",
                tracker_text="# Where issues live\n\nLocal markdown files.\n",
            )
            report = self.report(root)
        self.assertEqual(self.result_of(report, "tracker")["result"], "skipped")
        self.assertEqual(
            self.result_of(report, "tracker")["detail"], "n/a (local markdown tracker)"
        )
        self.assertEqual(report["status"], "initialized")

    def test_a_heading_that_names_no_tracker_is_unknown(self):
        """An empty heading is documented but unreadable, which is not the same as absent."""
        with tempfile.TemporaryDirectory() as temporary:
            root = build_repository(Path(temporary) / "repo", tracker_text="# Issue tracker:\n")
            report = self.report(root)
        self.assertEqual(self.result_of(report, "tracker")["result"], "unknown")
        self.assertIn("undocumented tracker", self.result_of(report, "tracker")["detail"])
        self.assertEqual(report["status"], "unknown")

    def test_an_undocumented_tracker_is_unknown_not_failed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = build_repository(Path(temporary) / "repo", tracker_text="# Issue tracker: Jira\n")
            report = self.report(root)
        self.assertEqual(self.result_of(report, "tracker")["result"], "unknown")
        self.assertIn("undocumented tracker", self.result_of(report, "tracker")["detail"])
        self.assertEqual(report["status"], "unknown")
        self.assertEqual(MODULE.STATUS_TO_EXIT[report["status"]], 2)

    def test_a_missing_tracker_cli_is_unknown_not_a_failure(self):
        """The repository may be perfectly initialized; this machine just cannot prove it."""
        with tempfile.TemporaryDirectory() as temporary:
            root = build_repository(Path(temporary) / "repo", tracker_text=GITHUB_TRACKER)
            answers = git_answer(root)
            answers[("gh", "repo", "view", "--json", "nameWithOwner")] = (
                MODULE.CommandMissing("gh")
            )
            report = self.report(root, answers)
        self.assertEqual(self.result_of(report, "tracker")["result"], "unknown")
        self.assertEqual(report["status"], "unknown")
        self.assertNotIn("fail", [check["result"] for check in report["checks"]])

    def test_tracker_output_that_is_not_json_is_unknown(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = build_repository(Path(temporary) / "repo", tracker_text=GITHUB_TRACKER)
            answers = git_answer(root)
            answers[("gh", "repo", "view", "--json", "nameWithOwner")] = completed(
                stdout="owner/repo\n"
            )
            report = self.report(root, answers)
        self.assertEqual(self.result_of(report, "tracker")["result"], "unknown")

    def test_a_failed_tracker_probe_is_unknown(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = build_repository(Path(temporary) / "repo", tracker_text=GITHUB_TRACKER)
            answers = git_answer(root)
            answers[("gh", "repo", "view", "--json", "nameWithOwner")] = completed(
                returncode=4, stderr="gh: To get started with GitHub CLI, please run: gh auth login\n"
            )
            report = self.report(root, answers)
        self.assertEqual(self.result_of(report, "tracker")["result"], "unknown")
        self.assertIn("gh auth login", self.result_of(report, "tracker")["detail"])

    def test_a_missing_git_binary_is_unknown(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = build_repository(Path(temporary) / "repo")
            report = MODULE.check(
                root,
                runner=missing_command_runner,
                triage_candidates=[],
            )
        self.assertEqual(self.result_of(report, "repository")["result"], "unknown")
        self.assertEqual(report["status"], "unknown")

    def test_a_plain_directory_is_unknown_rather_than_uninitialized(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = build_repository(Path(temporary) / "repo", git=False)
            answer = completed(returncode=128, stderr="fatal: not a git repository\n")
            report = self.report(root, {("git", "-C", str(root), "rev-parse", "--show-toplevel"): answer})
        self.assertEqual(self.result_of(report, "repository")["result"], "unknown")
        self.assertIn("not a git repository", self.result_of(report, "repository")["detail"])

    def test_a_subdirectory_is_not_accepted_as_the_repository_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = build_repository(Path(temporary) / "repo")
            nested = root / "nested"
            nested.mkdir()
            answer = completed(stdout=str(root) + "\n")
            report = self.report(nested, {("git", "-C", str(nested), "rev-parse", "--show-toplevel"): answer})
        self.assertEqual(self.result_of(report, "repository")["result"], "unknown")

    def test_failure_beats_unknown(self):
        """A known missing criterion is actionable; an undecided probe must not mask it."""
        checks = [
            {"id": "repository", "result": "unknown", "detail": ""},
            {"id": "agent-skills", "result": "fail", "detail": ""},
        ]
        self.assertEqual(MODULE.status_for(checks), "uninitialized")

    def test_skipped_votes_for_nothing(self):
        checks = [
            {"id": "repository", "result": "pass", "detail": ""},
            {"id": "tracker", "result": "skipped", "detail": ""},
        ]
        self.assertEqual(MODULE.status_for(checks), "initialized")


def isolated_path(directory, *binaries):
    """A ``PATH`` holding only the named binaries, so any other CLI is genuinely absent.

    ``gh`` is written as a stub that answers the one probe command; every other binary is the
    real one, symlinked, because ``git`` must behave like ``git``.
    """
    bin_dir = Path(directory) / "bin"
    bin_dir.mkdir()
    for name in binaries:
        if name == "gh":
            stub = bin_dir / name
            write(stub, "#!/bin/sh\nprintf '%s\\n' '{\"nameWithOwner\": \"owner/repo\"}'\n")
            stub.chmod(0o755)
            continue
        resolved = shutil.which(name)
        if resolved is None:
            raise RuntimeError(f"test fixture needs {name} on PATH")
        (bin_dir / name).symlink_to(resolved)
    return bin_dir


def run_script(*arguments, cwd=None, path=None, home=None):
    environment = dict(os.environ)
    if path is not None:
        environment["PATH"] = str(path)
    if home is not None:
        # The probe resolves `triage` through `Path.home()`; pinning HOME keeps every CLI case
        # independent of which skills this machine has installed.
        environment["HOME"] = str(home)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        cwd=str(cwd or REPO_ROOT),
        capture_output=True,
        text=True,
        env=environment,
    )


class CommandLineTests(unittest.TestCase):
    def test_the_cli_reports_four_passes_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = build_repository(Path(temporary) / "repo", tracker_text=GITHUB_TRACKER)
            result = run_script(
                "check", "--repo", str(root), path=isolated_path(temporary, "git", "gh"),
                home=Path(temporary) / "home",
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual([line.split("\t")[0] for line in lines[:4]],
                         ["repository", "agent-skills", "docs-agents", "tracker"])
        self.assertEqual([line.split("\t")[1] for line in lines[:4]], ["pass"] * 4)
        self.assertEqual(lines[4], "status: initialized")

    def test_the_json_flag_prints_a_parseable_report_in_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = build_repository(Path(temporary) / "repo", tracker_text=GITHUB_TRACKER)
            result = run_script(
                "check", "--repo", str(root), "--json",
                path=isolated_path(temporary, "git", "gh"),
                home=Path(temporary) / "home",
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(list(report), ["status", "repo", "checks"])
        self.assertEqual(report["status"], "initialized")
        self.assertEqual(report["repo"], str(root.resolve()))
        self.assertEqual([check["id"] for check in report["checks"]],
                         ["repository", "agent-skills", "docs-agents", "tracker"])

    def test_the_repo_flag_defaults_to_the_current_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = build_repository(Path(temporary) / "repo")
            result = run_script(
                "check", "--json", cwd=root,
                path=isolated_path(temporary, "git"),
                home=Path(temporary) / "home",
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["repo"], str(root.resolve()))

    def test_a_missing_agent_skills_heading_exits_one(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = build_repository(Path(temporary) / "repo", agents_text=AGENTS_WITHOUT_HEADING)
            result = run_script(
                "check", "--repo", str(root), path=isolated_path(temporary, "git"),
                home=Path(temporary) / "home",
            )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("agent-skills\tfail", result.stdout)
        self.assertEqual(result.stdout.splitlines()[-1], "status: uninitialized")

    def test_a_missing_tracker_cli_exits_two(self):
        """The reverse evidence for ``unknown``: no ``gh`` on PATH, and the repo may be fine."""
        with tempfile.TemporaryDirectory() as temporary:
            root = build_repository(Path(temporary) / "repo", tracker_text=GITHUB_TRACKER)
            result = run_script(
                "check", "--repo", str(root), path=isolated_path(temporary, "git"),
                home=Path(temporary) / "home",
            )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("tracker\tunknown", result.stdout)
        self.assertEqual(result.stdout.splitlines()[-1], "status: unknown")

    def test_a_plain_directory_exits_two(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = build_repository(Path(temporary) / "repo", git=False)
            result = run_script(
                "check", "--repo", str(root), path=isolated_path(temporary, "git"),
                home=Path(temporary) / "home",
            )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("repository\tunknown", result.stdout)

    def test_a_usage_error_exits_two_and_explains_itself(self):
        result = run_script()
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage", result.stderr.lower())

    def test_checking_writes_nothing_into_the_repository(self):
        """Read-only is a promise: a probe that leaves files would change what it measures."""
        with tempfile.TemporaryDirectory() as temporary:
            root = build_repository(Path(temporary) / "repo")
            before = sorted(
                (str(path.relative_to(root)), path.stat().st_size)
                for path in root.rglob("*")
            )
            result = run_script(
                "check", "--repo", str(root), path=isolated_path(temporary, "git"),
                home=Path(temporary) / "home",
            )
            after = sorted(
                (str(path.relative_to(root)), path.stat().st_size)
                for path in root.rglob("*")
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
