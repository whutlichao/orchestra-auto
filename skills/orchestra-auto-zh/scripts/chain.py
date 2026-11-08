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
* ``check_artifact(step_id, text, source=None, base_dir=None) -> list`` — pure: exit-check
  errors for one step. ``source`` declares the artifact shape (``list``, ``file`` or ``issue``)
  for a ticket artifact; leaving it out detects the shape and raises ``AmbiguousArtifact`` when
  the text fits more than one. ``base_dir`` resolves relative links inside a ticket.
* ``inspect_spec(text) -> dict`` — pure: the upstream-native checks and the project's dispatch
  additions, reported apart so a native spec is never called invalid for missing an addition.
* ``load_records(path) -> (records, problems)`` — tolerant JSONL reader with the ledger's rules:
  a trailing partial line is discarded, a corrupt complete line is reported, never dropped.
* ``load_effective_records(path) -> (records, problems, unknown)`` — the active ledger plus
  its ``archive/``, read through ``ledger.effective_records_for_path`` so retention never
  hides a record from resume and invalid history degrades to ``unknown``.

CLI: ``steps``, ``resume``, ``exit-check``. JSON out; exit ``0`` is success, ``1`` is a failed
check or invalid input, ``2`` is a usage error.

Ticket artifacts come in three real shapes and the check must use each one's own boundary:
a combined ``## <ticket>`` list, a single file with ``# <title>`` and an acceptance section,
and a single issue body whose sections are *not* tickets. ``--artifact`` repeated is a
collection: every file is one ticket. ``--source`` is the explicit compatibility mode for a
list whose blocks carry no ticket label.

Only the Python standard library is used.
"""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import sys

SCHEMA_VERSION = 1


def _load_ledger():
    """Import the sibling ``ledger.py`` by path: effective history is reused, not copied."""
    path = Path(__file__).with_name("ledger.py")
    spec = importlib.util.spec_from_file_location("orca_chain_ledger", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot build an import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_flow():
    """Import the sibling ``flow.py`` by path: the flow contract stays in one module."""
    path = Path(__file__).with_name("flow.py")
    spec = importlib.util.spec_from_file_location("orca_chain_flow", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot build an import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LEDGER = _load_ledger()
FLOW = _load_flow()

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

#: A single ticket's own title: the upstream local file template opens with ``# <NN>: <title>``.
H1_HEADING = re.compile(r"^#\s+(?=\S)", re.MULTILINE)

#: Headings that name an ordinary section of one artifact, never a ticket in a combined list.
#: This is what stops an issue body's ``## What to build`` and ``## Acceptance criteria`` from
#: being read as two tickets.
ORDINARY_SECTION = re.compile(
    r"^(?:"
    r"what to build|acceptance criteria|blocked by|parent|out of scope|"
    r"problem statement|solution|user stories|implementation decisions|"
    r"testing decisions|further notes|notes|acceptance|"
    r"destination|decisions so far|not yet specified|question|"
    r"验收|验收标准|阻塞|阻塞于|依赖|范围外|问题|方案|备注|任务|目的地|已定决定|未指定"
    r")\s*[:：]?\s*$",
    re.IGNORECASE,
)

#: ``Blocked by:`` as a line or a bold label, in either language.
BLOCKED_BY = re.compile(
    r"^\s*(?:[-*+]\s*)?(?:\*\*)?(?:blocked by|阻塞于|依赖)(?:\*\*)?\s*[:：]\s*(?P<value>.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

#: A task-list checkbox is acceptance evidence in the upstream local ticket template, which has
#: no acceptance heading: the checkboxes at the end of the file are the criteria.
CHECKBOX_LINE = re.compile(r"^\s*[-*+]\s+\[[ xX]\]\s+\S", re.MULTILINE)

#: Values that mean "no blockers".
NO_BLOCKERS = re.compile(
    r"^(?:none|n/?a|no|无|没有)(?:\s*\(.*\))?$", re.IGNORECASE)

#: The explicit ticket formats ``--source`` accepts, and the two single-ticket ones.
TICKET_SOURCES = ("list", "file", "issue")
SINGLE_TICKET_SOURCES = ("file", "issue")


class AmbiguousArtifact(ValueError):
    """The text fits more than one ticket shape, so the caller must name the format."""

    def __init__(self, shapes):
        self.shapes = tuple(shapes)
        super().__init__(
            "the ticket artifact fits more than one shape ("
            + " or ".join(self.shapes)
            + "); pass --source " + "|".join(TICKET_SOURCES) + " instead of guessing"
        )

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


def check_artifact(step_id, text, source=None, base_dir=None):
    """Return the exit-check errors for one step's artifact text; an empty list is a pass.

    Only the two artifact-shaped steps are checkable here. ``external`` steps return a single
    error that names the real signal, because pretending to check a test run would be worse than
    refusing to.

    A ticket artifact is checked against its real boundary. ``source="file"`` reads one ticket
    whose title is the first ``# `` heading; ``source="issue"`` reads one ticket whose acceptance
    section is a heading; ``source="list"`` reads the combined ``## <ticket>`` format this
    project uses. ``source=None`` detects the shape and raises ``AmbiguousArtifact`` when more
    than one fits — an issue body with ordinary ``##`` sections must never be read as several
    tickets, and a single file must never be read as one ticket per section.
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
    if step["exit"] == "spec":
        report = inspect_spec(text)
        return report["native"]["errors"] + report["dispatch"]["errors"]
    if source is not None and source not in TICKET_SOURCES:
        return ["tickets: unknown source " + repr(source) + "; use " + "|".join(TICKET_SOURCES)]
    if source is None:
        shape = detect_ticket_source(text)
        if shape is None:
            return ["tickets: no '## ' ticket blocks found and the text is not a single "
                    "ticket; name the format with --source " + "|".join(TICKET_SOURCES)]
        source = shape
    if source == "list":
        return _check_ticket_list(text)
    return _check_single_ticket(text, base_dir)


def inspect_spec(text):
    """Report the upstream-native spec checks and this project's dispatch additions apart.

    The upstream spec template has no ``Test tier:`` line; that line is this project's
    addition, needed before the spec enters implementation here. Reporting them separately
    keeps "this is not an upstream spec" from being confused with "this spec is invalid
    upstream", and neither result claims the requirement itself is correct.
    """
    native, dispatch = [], []
    heading = ACCEPTANCE_HEADING.search(text)
    if not heading:
        native.append("spec: missing an acceptance section")
    else:
        rest = text[heading.end():]
        sibling = ANY_HEADING.search(rest)
        body = rest[:sibling.start()] if sibling else rest
        if not BULLET_LINE.search(body) and not CHECKBOX_LINE.search(body):
            native.append("spec: the acceptance section has no testable line")
    if not TEST_TIER.search(text):
        dispatch.append("spec: missing a 'Test tier:' line (narrow or wide, with the reason) "
                        "(project addition, not an upstream requirement)")
    return {
        "native": {"passed": not native, "errors": native},
        "dispatch": {"passed": not dispatch, "errors": dispatch},
    }


def detect_ticket_source(text):
    """Return ``"list"``, ``"file"``, ``"issue"`` or ``None`` for one artifact's shape.

    ``list`` is the project's combined format: at least one ``## <ticket>`` heading whose title
    is not an ordinary section label. Anything else that can stand as one ticket — a ``# ``
    title, an acceptance heading, or the upstream local template's trailing checkboxes — is
    ``file``; ``issue`` is the same single-ticket shape and shares its check. A text that fits no
    shape is ``None``: it is not a ticket artifact at all. A single-ticket text that also carries
    a ticket-like ``##`` title fits both and genuinely is ambiguous — a map with its own title
    and one ticket with ticket-like sections cannot be told apart — so that raises rather than
    guesses.
    """
    blocks = [title for title in TICKET_HEADING.split(text)[1:]
              if not ORDINARY_SECTION.match(title.strip().splitlines()[0].strip())]
    single_evidence = (H1_HEADING.search(text) is not None
                       or ACCEPTANCE_HEADING.search(text) is not None
                       or CHECKBOX_LINE.search(text) is not None)
    shapes = []
    if blocks:
        shapes.append("list")
    if single_evidence:
        shapes.append("file")
    if not shapes:
        return None
    if len(shapes) > 1:
        raise AmbiguousArtifact(shapes)
    return shapes[0]


def _ticket_title(text):
    """The ticket's own name: its first ``# `` heading, else empty."""
    match = H1_HEADING.search(text)
    if not match:
        return ""
    return text[match.end():].splitlines()[0].strip()


def _blockers(body):
    """Every ticket this block says blocks it, in either language."""
    for match in BLOCKED_BY.finditer(body):
        value = match.group("value").strip()
        if not value or NO_BLOCKERS.match(value):
            continue
        for part in re.split(r"[,，、]", value):
            part = part.strip()
            if part:
                yield part


def _check_single_ticket(text, base_dir=None):
    """One ticket: its own title, one acceptance section, real blockers and links."""
    errors = []
    title = _ticket_title(text)
    label = title[:40] if title else "(untitled)"
    heading = ACCEPTANCE_HEADING.search(text)
    if heading:
        rest = text[heading.end():]
        sibling = ANY_HEADING.search(rest)
        body = rest[:sibling.start()] if sibling else rest
        if not BULLET_LINE.search(body) and not CHECKBOX_LINE.search(body):
            errors.append(f"tickets: ticket ({label!r}) has no acceptance criteria")
    elif not CHECKBOX_LINE.search(text):
        errors.append(f"tickets: ticket ({label!r}) has no acceptance section "
                      "(a heading or the local template's checkboxes)")
    for reference in _blockers(text):
        if title and reference == title:
            errors.append(f"tickets: ticket ({label!r}) blocks itself")
    errors.extend(_check_references(text, base_dir, label))
    return errors


def _check_ticket_list(text):
    """A combined ``## <ticket>`` list: each block on its own, then the edges as a graph."""
    blocks = TICKET_HEADING.split(text)[1:]
    if not blocks:
        return ["tickets: no '## ' ticket blocks found"]
    titles = [block.strip().splitlines()[0].strip() if block.strip() else ""
              for block in blocks]
    known = set(titles)
    edges = {}
    errors = []
    for block, title in zip(blocks, titles):
        body = "\n".join(block.splitlines()[1:])
        label = title[:40]
        if not ACCEPTANCE_LINE.search(body) and not CHECKBOX_LINE.search(body):
            errors.append(f"tickets: ticket ({label!r}) has no acceptance line")
        references = list(_blockers(body))
        edges[title] = references
        for reference in references:
            if reference == title:
                errors.append(f"tickets: ticket ({label!r}) blocks itself")
            elif reference not in known:
                errors.append(f"tickets: ticket ({label!r}) is blocked by unknown ticket "
                              f"({reference[:40]!r})")
    for cycle in _cycles(edges, known):
        errors.append("tickets: blocking cycle " + " -> ".join(cycle))
    return errors


def _cycles(edges, known):
    """Every blocking cycle reachable through tickets that exist, each reported once.

    A cycle is a defect of the graph, not of one ticket, so it is named by the path that closes
    it; the check does not attempt to say which edge is "wrong".
    """
    state = {}
    found = []

    def visit(node, path):
        state[node] = "visiting"
        path = path + [node]
        for target in edges.get(node, ()):
            if target not in known:
                continue
            if state.get(target) == "visiting":
                start = path.index(target)
                found.append(path[start:] + [target])
            elif target not in state:
                visit(target, path)
        state[node] = "done"

    for node in edges:
        if node not in state:
            visit(node, [])
    return found


def _check_references(text, base_dir, label):
    """A ticket that links a local file must link one that exists; a missing file is named."""
    if not base_dir:
        return []
    errors = []
    root = Path(base_dir)
    for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", text):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        if not (root / target).exists():
            errors.append(f"tickets: ticket ({label!r}) links a missing file ({target})")
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


def load_effective_records(path):
    """Read the active ledger and its archive; return ``(records, problems, unknown)``.

    ``resume`` reads history, not the active view: retention moves complete records to
    ``archive/`` and forgetting them would restart the chain at an earlier position.
    The read is ``ledger.effective_records_for_path`` — the same schema-validating
    interface ``ledger.effective_records`` exposes — so a JSON object that is not a
    ledger record degrades to ``unknown`` here exactly as it does there. ``problems``
    only names a missing active file; it does not make the history unknown.
    """
    path = Path(path)
    records, unknown = LEDGER.effective_records_for_path(path)
    problems = [] if path.exists() else ["no ledger at " + str(path)]
    return records, problems, unknown


def ledger_path(run_id, home=None):
    """Resolve ``$ORCA_LEDGER_HOME/runs/<run_id>/ledger.jsonl`` exactly as the ledger does."""
    root = Path(home or os.environ.get("ORCA_LEDGER_HOME") or Path.home() / ".orca" / "ledger")
    return root / "runs" / run_id / "ledger.jsonl"


def _resolve_flow_path(args):
    """The explicit flow file wins, then the flow home, then the ledger's own Run directory.

    Placing ``flow.jsonl`` beside ``ledger.jsonl`` by default means a coordinator who already
    points the ledger somewhere does not have to point the flow somewhere else.
    """
    if getattr(args, "flow", None):
        return Path(args.flow)
    home = getattr(args, "flow_home", None)
    if home:
        return FLOW.flow_path(args.run_id, home)
    ledger_home = getattr(args, "ledger_home", None)
    if ledger_home:
        return Path(ledger_home) / "runs" / args.run_id / "flow.jsonl"
    return FLOW.flow_path(args.run_id)


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
    resume_parser.add_argument("--flow", default=None,
                              help="explicit process record; defaults beside the ledger")
    resume_parser.add_argument("--flow-home", default=None,
                              help="home holding runs/<id>/flow.jsonl")

    check_parser = subparsers.add_parser("exit-check", help="check one step's artifact")
    check_parser.add_argument("--step", required=True, choices=STEP_IDS)
    check_parser.add_argument("--artifact", required=True, action="append",
                              help="path to the artifact text; repeat for a ticket collection")
    check_parser.add_argument("--source", default=None, choices=TICKET_SOURCES,
                              help="ticket format: list (combined ## blocks), file (one local "
                                   "ticket) or issue (one issue body); detected when omitted")

    args = parser.parse_args(argv)

    if args.command == "steps":
        _emit({"version": SCHEMA_VERSION, "positions": list(POSITIONS), "steps": list(STEPS)})
        return 0

    if args.command == "resume":
        path = Path(args.ledger) if args.ledger else ledger_path(args.run_id, args.ledger_home)
        flow_path = _resolve_flow_path(args)
        flow_records, flow_problems = FLOW.read_records(flow_path)
        if flow_records or flow_path.exists():
            answer = FLOW.plan(flow_records)
            answer["problems"] = flow_problems + answer["problems"]
            answer["ledger"] = str(path)
            answer["ledger_mode"] = "index only"
            if flow_problems and answer["state"] != "uncertain":
                answer["state"] = "uncertain"
                answer["ready"] = []
                answer["evidence_gaps"] = answer["evidence_gaps"] + [
                    "the flow file has invalid records; resolve them before resuming"]
            _emit(answer)
            return 0 if answer["state"] != "uncertain" else 1
        records, problems, unknown = load_effective_records(path)
        legacy = {"state": "legacy",
                  "reason": "no explicit flow record; this is a five-position chain suggestion "
                            "from an old ledger and cannot prove the current stage, gates or "
                            "endpoint, nor that new work is authorized",
                  "flow": str(flow_path)}
        if unknown is not None:
            _emit({"version": SCHEMA_VERSION, "run_id": args.run_id, "ledger": str(path),
                   "state": "unknown", "landed": [], "next": None,
                   "reason": unknown["reason"], "history": unknown, "legacy": legacy,
                   "problems": problems})
            return 0
        answer = next_step(records)
        answer.update({"version": SCHEMA_VERSION, "run_id": args.run_id, "ledger": str(path),
                       "history": {"state": "ok", "reason": ""}, "problems": problems,
                       "legacy": legacy})
        _emit(answer)
        return 0

    if args.command == "exit-check":
        artifacts = [Path(path) for path in args.artifact]
        missing = [str(path) for path in artifacts if not path.exists()]
        if missing:
            _emit({"version": SCHEMA_VERSION, "step": args.step, "passed": False,
                   "errors": ["artifact not found: " + path for path in missing]})
            return 1
        errors = []
        report = None
        try:
            if len(artifacts) == 1:
                artifact = artifacts[0]
                text = artifact.read_text(encoding="utf-8")
                if args.step == "to-spec":
                    report = inspect_spec(text)
                    errors = report["native"]["errors"] + report["dispatch"]["errors"]
                else:
                    errors = check_artifact(args.step, text,
                                            source=args.source, base_dir=artifact.parent)
            else:
                if args.step != "to-tickets":
                    errors = ["--artifact may be repeated only for to-tickets (a collection)"]
                for artifact in artifacts:
                    errors.extend(
                        f"{artifact.name}: {error}" for error in check_artifact(
                            args.step, artifact.read_text(encoding="utf-8"),
                            source=args.source or "file", base_dir=artifact.parent))
        except AmbiguousArtifact as exc:
            _emit({"version": SCHEMA_VERSION, "step": args.step, "passed": False,
                   "errors": [str(exc)]})
            return 2
        payload = {"version": SCHEMA_VERSION, "step": args.step,
                   "artifacts": [str(path) for path in artifacts],
                   "passed": not errors, "errors": errors}
        if report is not None:
            payload["native"] = report["native"]
            payload["dispatch"] = report["dispatch"]
        if len(artifacts) == 1:
            payload["artifact"] = str(artifacts[0])
        _emit(payload)
        return 0 if not errors else 1

    parser.print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
