#!/usr/bin/env python3
"""Chain-level resume and per-step exit checks.

The chain in this package is linear until it forks: ``grill-with-docs`` → ``to-spec`` →
``to-tickets`` → ``implement`` **or** ``implement-spec`` → ``code-review``. Each lap dispatches
one worker for one step, so the coordinator has to answer two mechanical questions between laps:

* **where does the chain continue** — which position already landed, and which one is next;
* **is this step's artifact shaped well enough to enter the next step** — a spec without a
  ``Test tier:`` line, or a ticket list where a ticket has no acceptance line, is a defect the
  next step would faithfully implement.

``resume`` answers the first from the ledger. ``exit-check`` answers the second from the
artifact itself. Neither reads the network, git or the PR host: the ledger is an index and the
artifact is a file, and anything that needs an authority fact stays with ``acceptance.py``
(``verify``) and ``closeout.py`` (``reconcile``).

The chain is modelled as **positions**, not steps, because ``implement`` and ``implement-spec``
share one gate: a single acceptance record cannot say which of the two ran, and pretending it
could would resume the chain into the wrong shape.

What this module deliberately does not do: judge whether the work is *good*. A spec can carry a
``Test tier:`` line and still specify the wrong thing. The check is a floor — it catches the
missing section, not the wrong decision — and the gate still belongs to the coordinator.

Public interface (deep module):

* ``landed_positions(records) -> list`` — pure: positions with a passed acceptance record.
* ``next_step(records) -> dict`` — pure: the resume answer, including why it is that position.
* ``check_artifact(step_id, text) -> list`` — pure: exit-check errors for one step.
* ``load_records(path) -> (records, problems)`` — tolerant JSONL reader with the ledger's rules:
  a trailing partial line is discarded, a corrupt complete line is reported, never dropped.

CLI: ``steps``, ``resume``, ``exit-check``. JSON out; exit ``0`` is success, ``1`` is a failed
check or invalid input, ``2`` is a usage error.

Only the Python standard library is used.
"""

import argparse
import json
import os
from pathlib import Path
import re
import sys

SCHEMA_VERSION = 1

#: Per-step metadata. ``exit`` names the check ``exit-check`` can apply; ``external`` means the
#: exit signal is a test run, a review or a PR state that this module deliberately does not read.
STEPS = (
    {
        "id": "grill-with-docs",
        "gate": "G-docs",
        "exit": "external",
        "artifact": "the settled change, its glossary terms and any ADR",
    },
    {
        "id": "to-spec",
        "gate": "G-spec",
        "exit": "spec",
        "artifact": "the spec published to the ticket tracker",
    },
    {
        "id": "to-tickets",
        "gate": "G-tickets",
        "exit": "tickets",
        "artifact": "the ticket map with blocking edges",
    },
    {
        "id": "implement",
        "gate": "G-impl",
        "exit": "external",
        "artifact": "one settled ticket's change, its tests and its review",
    },
    {
        "id": "implement-spec",
        "gate": "G-impl",
        "exit": "external",
        "artifact": "the integration branch, every ready ticket merged through it",
    },
    {
        "id": "code-review",
        "gate": "G-merge",
        "exit": "external",
        "artifact": "the two-axis review, then the merge",
    },
)

STEP_IDS = tuple(step["id"] for step in STEPS)

#: The chain as ordered positions. ``implement`` and ``implement-spec`` are one position: the
#: ticket map decides which one runs, and both close on ``G-impl``.
POSITIONS = (
    {"id": "grill", "gate": "G-docs", "steps": ("grill-with-docs",),
     "artifact": "the settled change, its glossary terms and any ADR"},
    {"id": "spec", "gate": "G-spec", "steps": ("to-spec",),
     "artifact": "the spec published to the ticket tracker"},
    {"id": "tickets", "gate": "G-tickets", "steps": ("to-tickets",),
     "artifact": "the ticket map with blocking edges"},
    {"id": "build", "gate": "G-impl", "steps": ("implement", "implement-spec"),
     "artifact": "one settled ticket, or the spec's integration branch"},
    {"id": "review", "gate": "G-merge", "steps": ("code-review",),
     "artifact": "the two-axis review, then the merge"},
)

POSITION_IDS = tuple(position["id"] for position in POSITIONS)

#: The line the spec contract requires; a spec without it cannot enter ``to-tickets``.
TEST_TIER = re.compile(r"^\s*(?:[-*]\s*)?(?:\*\*)?test tier(?:\*\*)?\s*:", re.IGNORECASE | re.MULTILINE)

#: A heading that opens an acceptance section, in either language.
ACCEPTANCE_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*.*(?:验收|acceptance)", re.IGNORECASE | re.MULTILINE)

#: One acceptance line. A ticket may phrase it as a done-when line.
ACCEPTANCE_LINE = re.compile(
    r"^\s*(?:[-*+]|\d+\.|>)\s+.*(?:验收|acceptance|done when|完成标准)",
    re.IGNORECASE | re.MULTILINE,
)

#: A bullet under a spec's acceptance heading is the testable statement itself.
BULLET_LINE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+\S", re.MULTILINE)

#: Any markdown heading, used to cut a section at its next sibling.
ANY_HEADING = re.compile(r"^#{1,6}\s", re.MULTILINE)

#: A level-two ticket heading, zero-width so the title survives the split.
TICKET_HEADING = re.compile(r"^##\s+(?=\S)", re.MULTILINE)

_KNOWN_GATES = {position["gate"] for position in POSITIONS}


def _detail(record):
    value = record.get("detail")
    return value if isinstance(value, dict) else {}


def _record_passed(record):
    """A gate passed when every recorded check passed and the review is a pass or a waiver.

    This mirrors ``acceptance.py``'s gate result without importing it: a record that carries no
    checks is a claim with no evidence, and is not a pass.
    """
    detail = _detail(record)
    checks = detail.get("checks")
    if not isinstance(checks, list) or not checks:
        return False
    if any(not isinstance(check, dict) or check.get("result") != "pass" for check in checks):
        return False
    review = detail.get("review")
    if isinstance(review, dict) and review.get("result") not in (None, "pass", "waiver"):
        return False
    return True


def landed_positions(records):
    """Return the ids of the positions whose gate has a passed acceptance record, in chain order.

    ``records`` is any iterable of ledger records; non-acceptance records are ignored. A position
    is landed at most once even when the ledger holds several records for it, because a later
    record never un-lands an earlier one — a revert is an ``observation`` and lowers the level
    without rewriting history.
    """
    passed_gates = set()
    for record in records:
        if not isinstance(record, dict) or record.get("kind") != "acceptance":
            continue
        if not _record_passed(record):
            continue
        gate = _detail(record).get("gate")
        if isinstance(gate, list):
            passed_gates.update(item for item in gate if isinstance(item, str) and item in _KNOWN_GATES)
    return [position["id"] for position in POSITIONS if position["gate"] in passed_gates]


def next_step(records):
    """Return the resume answer: the first position with no passed gate, and why it is that one."""
    landed = landed_positions(records)
    for position in POSITIONS:
        if position["id"] not in landed:
            return {
                "state": "continue",
                "landed": landed,
                "next": {
                    "position": position["id"],
                    "gate": position["gate"],
                    "steps": list(position["steps"]),
                    "artifact": position["artifact"],
                },
                "reason": "no passed acceptance record for gate " + position["gate"],
            }
    return {
        "state": "complete",
        "landed": landed,
        "next": None,
        "reason": "every position's gate has a passed acceptance record; the chain is done",
    }


def check_artifact(step_id, text):
    """Return the exit-check errors for one step's artifact text; an empty list is a pass.

    Only the two artifact-shaped steps are checkable here. ``external`` steps return a single
    error that names the real signal, because pretending to check a test run would be worse than
    refusing to.
    """
    if step_id not in STEP_IDS:
        return ["unknown step " + repr(step_id) + "; known steps: " + ", ".join(STEP_IDS)]
    step = next(item for item in STEPS if item["id"] == step_id)
    if not isinstance(text, str) or not text.strip():
        return [step_id + ": the artifact is empty"]
    if step["exit"] == "external":
        return [
            step_id + ": no artifact-shaped check exists for this step; "
            "its exit signal is the test run, the review or the PR state — "
            "record it with acceptance.py, not here"
        ]
    errors = []
    if step["exit"] == "spec":
        if not TEST_TIER.search(text):
            errors.append("spec: missing a 'Test tier:' line (narrow or wide, with the reason)")
        heading = ACCEPTANCE_HEADING.search(text)
        if not heading:
            errors.append("spec: missing an acceptance section")
        else:
            rest = text[heading.end():]
            sibling = ANY_HEADING.search(rest)
            body = rest[:sibling.start()] if sibling else rest
            if not BULLET_LINE.search(body):
                errors.append("spec: the acceptance section has no testable line")
    if step["exit"] == "tickets":
        blocks = TICKET_HEADING.split(text)[1:]
        if not blocks:
            errors.append("tickets: no '## ' ticket blocks found")
        for index, block in enumerate(blocks, start=1):
            title = block.strip().splitlines()[0] if block.strip() else ""
            body = "\n".join(block.splitlines()[1:])
            if not ACCEPTANCE_LINE.search(body):
                errors.append(f"tickets: ticket {index} ({title[:40]!r}) has no acceptance line")
    return errors


def load_records(path):
    """Read a ledger JSONL file tolerantly; return ``(records, problems)``.

    The rules are the ledger's own: a trailing partial line is discarded, and a corrupt complete
    line is reported rather than dropped, because a silently skipped record is a resume that
    claims the wrong step.
    """
    path = Path(path)
    if not path.exists():
        return [], ["no ledger at " + str(path)]
    raw = path.read_text(encoding="utf-8")
    lines = raw.splitlines()
    records, problems = [], []
    partial = bool(raw) and not raw.endswith("\n")
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        if partial and index == len(lines) - 1:
            problems.append(f"line {index + 1}: discarded a trailing partial line")
            break
        try:
            value = json.loads(line)
        except ValueError:
            problems.append(f"line {index + 1}: not JSON")
            continue
        if not isinstance(value, dict):
            problems.append(f"line {index + 1}: not an object")
            continue
        records.append(value)
    return records, problems


def ledger_path(run_id, home=None):
    """Resolve ``$ORCA_LEDGER_HOME/runs/<run_id>/ledger.jsonl`` exactly as the ledger does."""
    root = Path(home or os.environ.get("ORCA_LEDGER_HOME") or Path.home() / ".orca" / "ledger")
    return root / "runs" / run_id / "ledger.jsonl"


def _emit(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=1))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Chain resume and per-step exit checks.")
    parser.add_argument("--version", action="version", version="chain " + str(SCHEMA_VERSION))
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("steps", help="print the chain positions, their gates and their steps")

    resume_parser = subparsers.add_parser("resume", help="read the ledger and name the next position")
    resume_parser.add_argument("--run-id", required=True)
    resume_parser.add_argument("--ledger-home", default=None)
    resume_parser.add_argument("--ledger", default=None, help="read this file instead of a home")

    check_parser = subparsers.add_parser("exit-check", help="check one step's artifact")
    check_parser.add_argument("--step", required=True, choices=STEP_IDS)
    check_parser.add_argument("--artifact", required=True, help="path to the artifact text")

    args = parser.parse_args(argv)

    if args.command == "steps":
        _emit({"version": SCHEMA_VERSION, "positions": list(POSITIONS), "steps": list(STEPS)})
        return 0

    if args.command == "resume":
        path = Path(args.ledger) if args.ledger else ledger_path(args.run_id, args.ledger_home)
        records, problems = load_records(path)
        answer = next_step(records)
        answer.update({"version": SCHEMA_VERSION, "run_id": args.run_id, "ledger": str(path),
                       "problems": problems})
        _emit(answer)
        return 0

    if args.command == "exit-check":
        artifact = Path(args.artifact)
        if not artifact.exists():
            _emit({"version": SCHEMA_VERSION, "step": args.step, "passed": False,
                   "errors": ["artifact not found: " + str(artifact)]})
            return 1
        errors = check_artifact(args.step, artifact.read_text(encoding="utf-8"))
        _emit({"version": SCHEMA_VERSION, "step": args.step, "artifact": str(artifact),
               "passed": not errors, "errors": errors})
        return 0 if not errors else 1

    parser.print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
