#!/usr/bin/env python3
"""Read-only probe for the repository bootstrap gate.

SKILL.md §1.1 and ``references/dispatch-loop.md`` §4.3.0 describe the same criteria in prose, so
every round re-derived them by hand and left no artifact behind. This module is that artifact: one
command, four checks, a status and an exit code.

The criteria are reported as four checks because the tracker probe only means something once the
governance documents exist, and a coordinator deciding what to fix wants to see them apart:

* ``repository`` — ``git rev-parse --show-toplevel`` resolves the given path's root;
* ``agent-skills`` — root ``CLAUDE.md`` when present, else ``AGENTS.md``, carries
  ``^##[ \\t]+Agent skills[ \\t]*$`` (setup's own selection rule, not a second copy of it);
* ``docs-agents`` — ``docs/agents/issue-tracker.md`` and ``docs/agents/domain.md`` exist and are
  non-empty, plus ``docs/agents/triage-labels.md`` when the ``triage`` skill is installed;
* ``tracker`` — ``gh repo view --json nameWithOwner`` (GitHub) or ``glab repo view`` (GitLab)
  reports a project from inside the target; a local-markdown tracker is ``skipped``.

Results are ``pass``, ``fail``, ``unknown`` or ``skipped``. ``unknown`` is not failure: a missing
``git``, a path that is not a repository root, an absent or unauthenticated tracker CLI, or a
tracker document that names no known tracker means the probe could not decide. Collapsing that into
"uninitialized" is the mistake this module exists to prevent.

Exit codes: ``0`` initialized (no ``fail`` and no ``unknown``), ``1`` uninitialized (at least one
``fail``), ``2`` unknown (no ``fail``, at least one ``unknown``) or a usage error.

What it deliberately does not do: write a file, read ``orca-data.json`` or any profile state, or
call the network except for the read-only tracker probe. It does not judge whether the bootstrap
content is *good* — only whether the criteria hold.

Public interface (deep module):

* ``check(repo=".", *, runner=None, triage_candidates=None) -> dict`` — the report
  ``{"status", "repo", "checks"}`` with the four checks in their fixed order;
* ``status_for(checks) -> str`` — pure: ``fail`` beats ``unknown`` beats ``pass``;
* ``render_human(report) -> str`` — the tab-separated lines plus the final ``status:`` line;
* ``run_command(argv, cwd)`` — the default runner, raising ``CommandMissing`` when the executable
  is not on PATH so tests can inject a fake instead of needing a logged-in tracker CLI.

Only the Python standard library is used.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

EXIT_INITIALIZED = 0
EXIT_UNINITIALIZED = 1
EXIT_UNKNOWN = 2

STATUS_TO_EXIT = {
    "initialized": EXIT_INITIALIZED,
    "uninitialized": EXIT_UNINITIALIZED,
    "unknown": EXIT_UNKNOWN,
}

#: The report's fixed check order; the JSON consumer may rely on it.
CHECK_IDS = ("repository", "agent-skills", "docs-agents", "tracker")

#: Setup's own selection rule, in order: the file that exists wins, and never both.
BOOTSTRAP_FILES = ("CLAUDE.md", "AGENTS.md")

#: The heading the bootstrap file must carry. Trailing whitespace is tolerated; a heading with a
#: suffix (``## Agent skills (draft)``) is not the heading setup lands.
AGENT_SKILLS_HEADING = re.compile(r"^##[ \t]+Agent skills[ \t]*$", re.MULTILINE)

TRACKER_DOCUMENT = "docs/agents/issue-tracker.md"
REQUIRED_DOCUMENTS = (TRACKER_DOCUMENT, "docs/agents/domain.md")
TRIAGE_LABELS = "docs/agents/triage-labels.md"

#: The tracker heading setup lands. Only the known trackers get a probe; anything else is a
#: tracker this probe has no way to verify.
TRACKER_HEADING = re.compile(
    r"^#\s*Issue tracker:\s*(?P<name>.*?)\s*$", re.IGNORECASE | re.MULTILINE
)
LOCAL_MARKDOWN_TRACKER = "local markdown"

#: One entry per documented remote tracker. ``field`` is the JSON key carrying the project for a
#: CLI that can answer in JSON; ``None`` means the CLI prints the project as text.
TRACKER_PROBES = (
    {
        "name": "github",
        "cli": "gh",
        "argv": ("repo", "view", "--json", "nameWithOwner"),
        "field": "nameWithOwner",
    },
    {
        "name": "gitlab",
        "cli": "glab",
        "argv": ("repo", "view"),
        "field": None,
    },
)

#: A probe command is read-only but still a network round trip; cap it so a hung CLI becomes
#: ``unknown`` instead of a stuck coordinator.
COMMAND_TIMEOUT = 60


class CommandMissing(Exception):
    """``argv[0]`` is not on PATH, so the probe has no answer to give."""


def run_command(argv, cwd):
    """Run one read-only probe command and return its ``CompletedProcess``.

    ``shutil.which`` is what turns "no ``gh`` on this machine" into ``CommandMissing`` instead of
    an ``OSError`` the caller would have to guess at. No shell is involved and no environment is
    added: the probe inherits the caller's PATH and credentials exactly, so its answer is the
    answer the coordinator would get typing the command.
    """
    executable = shutil.which(argv[0])
    if executable is None:
        raise CommandMissing(argv[0])
    return subprocess.run(
        [executable, *argv[1:]],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT,
        check=False,
    )


def _result(check_id, result, detail):
    """One check's record. ``detail`` is evidence, so it stays on a single line."""
    return {"id": check_id, "result": result, "detail": " ".join(str(detail).split())}


def _first_line(text):
    for line in str(text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _read_text(path):
    """Return ``(text, error)``; a missing or unreadable file is an error, not an exception."""
    try:
        return path.read_text(encoding="utf-8", errors="replace"), None
    except OSError as error:
        return None, error


def _default_triage_candidates():
    """Where the ``triage`` skill can be installed, in the resolution order this module defines.

    The first candidate is this bundle's own skills root — ``scripts/`` → bundle → ``skills/`` —
    so a checkout that vendors the skill uses it. A bundle published alone will not have it, which
    is why the per-user installs follow: the probe needs to know whether ``triage`` *is installed
    here*, and that is a property of the machine, not of the repository being probed.
    """
    return (
        Path(__file__).resolve().parents[2] / "triage" / "SKILL.md",
        Path.home() / ".agents" / "skills" / "triage" / "SKILL.md",
        Path.home() / ".claude" / "skills" / "triage" / "SKILL.md",
        Path.home() / ".pi" / "agent" / "skills" / "triage" / "SKILL.md",
    )


def triage_skill_path(triage_candidates=None):
    """Return the installed ``triage`` skill's path, or ``None`` when it is not installed."""
    candidates = _default_triage_candidates() if triage_candidates is None else triage_candidates
    for candidate in candidates:
        candidate = Path(candidate)
        if candidate.is_file():
            return candidate
    return None


def check_repository(repo, runner=None):
    """``repository``: the given path is a git repository root that still exists."""
    runner = runner or run_command
    argv = ["git", "-C", str(repo), "rev-parse", "--show-toplevel"]
    try:
        completed = runner(argv, repo)
    except CommandMissing:
        return _result("repository", "unknown", "git is not on PATH; the repository probe cannot run")
    except (OSError, subprocess.SubprocessError) as error:
        return _result("repository", "unknown", f"git could not run: {error}")
    if completed.returncode != 0:
        reason = _first_line(completed.stderr) or f"git exited {completed.returncode}"
        return _result("repository", "unknown", f"not a git repository: {reason}")
    root = Path(completed.stdout.strip())
    if not root.is_dir():
        return _result("repository", "unknown", f"git reports a root that does not exist: {root}")
    if os.path.realpath(root) != os.path.realpath(repo):
        return _result(
            "repository", "unknown",
            f"{repo} is not a repository root; git reports {root}",
        )
    return _result("repository", "pass", f"git root {root}")


def bootstrap_document(repo):
    """Return ``(path, filename)`` for setup's chosen bootstrap file, or ``(None, None)``.

    This is setup's own selection rule expressed once: when ``CLAUDE.md`` exists it is the only
    file consulted, and ``AGENTS.md`` is never a fallback for it — a second copy of the rule would
    let the two disagree.
    """
    for filename in BOOTSTRAP_FILES:
        path = Path(repo) / filename
        if path.is_file():
            return path, filename
    return None, None


def check_agent_skills(repo):
    """``agent-skills``: the chosen bootstrap file carries the ``## Agent skills`` heading."""
    path, filename = bootstrap_document(repo)
    if path is None:
        return _result(
            "agent-skills", "fail",
            "neither CLAUDE.md nor AGENTS.md exists at the repository root",
        )
    text, error = _read_text(path)
    if text is None:
        return _result("agent-skills", "unknown", f"{filename} is unreadable: {error}")
    if AGENT_SKILLS_HEADING.search(text):
        return _result("agent-skills", "pass", f"{filename} carries a '## Agent skills' heading")
    return _result("agent-skills", "fail", f"{filename} carries no '## Agent skills' heading")


def check_docs_agents(repo, triage_candidates=None):
    """``docs-agents``: the tracker and domain documents are present and non-empty.

    ``triage-labels.md`` is required only when ``triage`` is installed: a repository that never
    installed the skill has no label vocabulary to publish, and failing it for a file no setup
    step would land is a false negative. The detail always says which of the two cases applied.
    """
    problems = []
    for relative in REQUIRED_DOCUMENTS:
        text, error = _read_text(Path(repo) / relative)
        if text is None and isinstance(error, FileNotFoundError):
            problems.append(f"{relative} is missing")
        elif text is None:
            problems.append(f"{relative} is unreadable: {error}")
        elif not text.strip():
            problems.append(f"{relative} is empty")
    triage = triage_skill_path(triage_candidates)
    if triage is None:
        evidence = "triage not installed"
    else:
        text, error = _read_text(Path(repo) / TRIAGE_LABELS)
        if text is None and isinstance(error, FileNotFoundError):
            problems.append(f"{TRIAGE_LABELS} is missing; triage installed at {triage}")
        elif text is None:
            problems.append(f"{TRIAGE_LABELS} is unreadable: {error}")
        elif not text.strip():
            problems.append(f"{TRIAGE_LABELS} is empty; triage installed at {triage}")
        evidence = f"triage-labels.md present; triage installed at {triage}"
    if problems:
        return _result("docs-agents", "fail", "; ".join(problems) + "; " + evidence)
    return _result(
        "docs-agents", "pass",
        "issue-tracker.md and domain.md present; " + evidence,
    )


def read_tracker_heading(repo):
    """Return ``(state, name)`` for the tracker document's ``Issue tracker:`` heading.

    ``state`` is ``"missing"`` when the document itself is absent, ``"absent"`` when it carries
    no such heading, and ``"named"`` when it does — an empty name included, because a documented
    heading with no tracker is unreadable evidence, not a repository that skipped the document.
    """
    text, _error = _read_text(Path(repo) / TRACKER_DOCUMENT)
    if text is None:
        return "missing", ""
    match = TRACKER_HEADING.search(text)
    if match is None:
        return "absent", ""
    return "named", match.group("name").strip()


def _project_from_probe(probe, stdout):
    """Return the project the tracker CLI printed, or ``""`` when it printed no usable project.

    A GitHub probe must answer in JSON: the flag asked for a field, so text means the CLI did not
    understand the request and the probe is unknown rather than failed. A GitLab probe has no
    field flag, so its first non-empty line is the project.
    """
    text = stdout.strip()
    if not text:
        return ""
    if probe["field"] is None:
        return text.splitlines()[0].strip()
    try:
        payload = json.loads(text)
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    value = payload.get(probe["field"])
    return value.strip() if isinstance(value, str) else ""


def check_tracker(repo, runner=None):
    """``tracker``: a documented tracker resolves a project from inside the target repository."""
    runner = runner or run_command
    state, name = read_tracker_heading(repo)
    if state == "missing":
        return _result("tracker", "skipped", f"n/a (no {TRACKER_DOCUMENT})")
    lowered = name.lower()
    if state == "absent" or lowered == LOCAL_MARKDOWN_TRACKER:
        return _result("tracker", "skipped", "n/a (local markdown tracker)")
    probe = next((entry for entry in TRACKER_PROBES if entry["name"] == lowered), None)
    if probe is None:
        described = name or "the heading names no tracker"
        return _result("tracker", "unknown", f"undocumented tracker: {described}")
    command = " ".join((probe["cli"], *probe["argv"]))
    try:
        completed = runner([probe["cli"], *probe["argv"]], Path(repo))
    except CommandMissing:
        return _result(
            "tracker", "unknown",
            f"{probe['cli']} is not on PATH; the tracker probe cannot run",
        )
    except (OSError, subprocess.SubprocessError) as error:
        return _result("tracker", "unknown", f"{command} could not run: {error}")
    if completed.returncode != 0:
        reason = _first_line(completed.stderr) or f"exit {completed.returncode}"
        return _result("tracker", "unknown", f"{command} failed: {reason}")
    project = _project_from_probe(probe, completed.stdout)
    if not project:
        return _result("tracker", "unknown", f"{command} printed no project")
    return _result("tracker", "pass", f"{command} reports {project}")


def status_for(checks):
    """Pure status rule: a ``fail`` is uninitialized, an ``unknown`` is undecided, else initialized.

    ``skipped`` votes for nothing. It means the check does not apply to this repository, which is
    neither evidence of initialization nor evidence against it.
    """
    results = {item["result"] for item in checks}
    if "fail" in results:
        return "uninitialized"
    if "unknown" in results:
        return "unknown"
    return "initialized"


def check(repo=".", *, runner=None, triage_candidates=None):
    """Probe one repository and return the report ``{status, repo, checks}``.

    ``runner`` and ``triage_candidates`` are the two seams tests need: the tracker CLI is never
    invoked for real, and whether ``triage`` is installed is injected instead of read from the
    machine the test happens to run on.
    """
    path = Path(repo).expanduser()
    runner = runner or run_command
    checks_by_id = {
        "repository": check_repository(path, runner),
        "agent-skills": check_agent_skills(path),
        "docs-agents": check_docs_agents(path, triage_candidates),
        "tracker": check_tracker(path, runner),
    }
    checks = [checks_by_id[check_id] for check_id in CHECK_IDS]
    return {"status": status_for(checks), "repo": str(path.resolve()), "checks": checks}


def render_human(report):
    """The tab-separated human surface: one line per check, then the aggregate status."""
    lines = [
        f"{item['id']}\t{item['result']}\t{item['detail']}" for item in report["checks"]
    ]
    lines.append("status: " + report["status"])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="init-check.py",
        description="Read-only probe for the repository bootstrap gate.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    check_parser = subparsers.add_parser(
        "check", help="probe one repository and report the four bootstrap checks",
    )
    check_parser.add_argument(
        "--repo", default=".",
        help="repository to probe (default: the current directory)",
    )
    check_parser.add_argument(
        "--json", action="store_true", help="print the report as JSON instead of lines",
    )
    args = parser.parse_args(argv)

    report = check(args.repo)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print(render_human(report))
    return STATUS_TO_EXIT[report["status"]]


if __name__ == "__main__":
    sys.exit(main())
