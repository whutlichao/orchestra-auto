#!/usr/bin/env python3
"""Explicit, versioned process record: the work plan outside the ledger.

The ledger is an append-only index of what was observed. It cannot say which stages this Run
actually chose, which gate applies to a node, or where the authorized delivery endpoint is —
so a resume that reads only the ledger replays a fixed chain even when the Run never chose it.
This module is the missing work plan: a versioned set of nodes with their dependencies, gates,
states and evidence, stored append-only beside the ledger (never inside a repository).

Contract (schema v1):

* one JSONL file per Run at ``$ORCA_FLOW_HOME/runs/<run_id>/flow.jsonl``, default
  ``~/.orca/flows``;
* record kinds: ``flow`` (the plan, once), ``state`` (a node's state change), ``invalidate``
  (a revocation, spec change or artifact update, which invalidates the named nodes and every
  downstream node), and ``import`` (an external or previous-Run artifact, named explicitly with
  its source, version and verification);
* node states: ``pending``, ``in_progress``, ``waiting_user``, ``blocked``, ``accepted``,
  ``not_applicable``, ``invalidated``;
* ``accepted`` binds a node to the task/attempt, the artifact version and the acceptance
  evidence; an ``import`` carries the same binding plus the external source;
* ``not_applicable`` carries the entry evidence that made it inapplicable; ``waiting_user`` is
  not a failure;
* the file is append-only and order is the authority for one node: a later state overrides an
  earlier one, so a current failure or block is never hidden by an older success. An acceptance
  whose attempt or task is older than what the node has already seen is reported as a gap
  instead of overriding the newer state.

What this module deliberately does not do: decide whether a gate *should* pass, read the
tracker or Orca, or convert an old ledger. An old ledger keeps its meaning and is reported as
``legacy`` by the resume path; the flow file is only ever written explicitly.

Public interface (deep module):

* ``validate_record(record) -> list[str]`` — pure: schema errors for one record.
* ``read_records(path) -> (records, problems)`` — tolerant JSONL reader; a trailing partial
  line is discarded and a corrupt complete line is reported, never dropped.
* ``plan(records) -> dict`` — pure: effective node states, the ready set, blockers, waiting
  items, invalidation and evidence gaps; ``state`` is ``continue``, ``complete`` or
  ``uncertain``.
* ``append_record(path, record) -> record`` — validate against the file's existing plan, then
  append; an invalid, unknown-node or mismatched-run record is never written.

CLI: ``validate``, ``plan`` and ``append``. JSON out; exit ``0`` success or a plan that can
continue, ``1`` invalid input or an uncertain plan, ``2`` usage error. Only the Python standard
library is used.
"""

import argparse
import json
import os
from pathlib import Path
import sys

SCHEMA_VERSION = 1

KINDS = ("flow", "state", "invalidate", "import")
NODE_STATES = (
    "pending", "in_progress", "waiting_user", "blocked", "accepted",
    "not_applicable", "invalidated",
)
INVALIDATION_CAUSES = ("revocation", "spec-change", "artifact-update")

#: States that finish a node for resume purposes.
SATISFIED = ("accepted", "not_applicable")
#: States that are unfinished work, each explained in the plan.
UNFINISHED = ("pending", "in_progress", "waiting_user", "blocked", "invalidated")

FLOW_HOME_ENV = "ORCA_FLOW_HOME"
DEFAULT_HOME = "~/.orca/flows"
RUNS_DIR = "runs"
FLOW_NAME = "flow.jsonl"

MAX_RECORD_BYTES = 64 * 1024


class FlowError(ValueError):
    """A record does not satisfy the flow contract or does not belong in this file."""


def _text(value, field, errors, *, required=True):
    if value is None or value == "":
        if required:
            errors.append(field + ": required")
        return ""
    if not isinstance(value, str):
        errors.append(field + ": must be a string")
        return ""
    return value.strip()


def _check_state(state, errors):
    if state not in NODE_STATES:
        errors.append("state: must be one of " + ", ".join(NODE_STATES))


def _positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def validate_record(record):
    """Return the schema errors for one flow record; an empty list is valid."""
    if not isinstance(record, dict):
        return ["record: must be an object"]
    errors = []
    if record.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version: must be " + str(SCHEMA_VERSION))
    _text(record.get("record_id"), "record_id", errors)
    _text(record.get("flow_id"), "flow_id", errors)
    kind = record.get("kind")
    if kind not in KINDS:
        return errors + ["kind: must be one of " + ", ".join(KINDS)]
    if kind == "flow":
        return errors + _validate_flow(record)
    if kind == "state":
        return errors + _validate_state(record)
    if kind == "invalidate":
        nodes = record.get("nodes")
        if not isinstance(nodes, list) or not nodes or any(
                not isinstance(node, str) or not node.strip() for node in nodes):
            errors.append("nodes: a non-empty list of node ids is required")
        if record.get("cause") not in INVALIDATION_CAUSES:
            errors.append("cause: must be one of " + ", ".join(INVALIDATION_CAUSES))
        _text(record.get("reason"), "reason", errors)
        return errors
    _text(record.get("node"), "node", errors)
    _text(record.get("source_run"), "source_run", errors)
    _text(record.get("artifact"), "artifact", errors)
    _text(record.get("artifact_version"), "artifact_version", errors)
    _text(record.get("verification"), "verification", errors)
    if record.get("attempt") is not None and not _positive_int(record["attempt"]):
        errors.append("attempt: must be a positive integer")
    return errors


def _validate_flow(record):
    errors = []
    _text(record.get("run_id"), "run_id", errors)
    _text(record.get("goal"), "goal", errors)
    _text(record.get("endpoint"), "endpoint", errors)
    nodes = record.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return errors + ["nodes: at least one node is required"]
    identifiers = []
    for index, node in enumerate(nodes):
        where = f"nodes[{index}]"
        if not isinstance(node, dict):
            errors.append(where + ": must be an object")
            continue
        identifiers.append(_text(node.get("id"), where + ".id", errors))
        _text(node.get("gate"), where + ".gate", errors)
        depends = node.get("depends_on", [])
        if not isinstance(depends, list) or any(
                not isinstance(item, str) or not item.strip() for item in depends):
            errors.append(where + ".depends_on: must be a list of node ids")
        applies = node.get("applies", True)
        if not isinstance(applies, bool):
            errors.append(where + ".applies: must be true or false")
        elif not applies:
            _text(node.get("entry_evidence"), where + ".entry_evidence", errors)
    if len(identifiers) != len(set(identifiers)):
        errors.append("nodes: ids must be unique")
    known = set(identifiers)
    edges = {}
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        identifier = node.get("id")
        depends = node.get("depends_on", [])
        if not isinstance(depends, list):
            continue
        edges[identifier] = [item for item in depends if isinstance(item, str)]
        for dependency in edges[identifier]:
            if dependency not in known:
                errors.append(f"nodes[{index}].depends_on: unknown node {dependency!r}")
            elif dependency == identifier:
                errors.append(f"nodes[{index}].depends_on: a node cannot depend on itself")
    if not errors:
        for cycle in _cycles(edges):
            errors.append("nodes: dependency cycle " + " -> ".join(cycle))
    return errors


def _validate_state(record):
    errors = []
    _text(record.get("node"), "node", errors)
    state = record.get("state")
    _check_state(state, errors)
    if state == "accepted":
        for field in ("evidence_ref", "artifact_version", "task_id"):
            _text(record.get(field), field, errors)
        if not _positive_int(record.get("attempt")):
            errors.append("attempt: an accepted node binds a positive attempt number")
        if "source_run" in record:
            errors.append("source_run: a prior-Run artifact must use an import record, "
                          "not an accepted state")
    elif state in ("not_applicable", "waiting_user", "blocked"):
        _text(record.get("reason"), "reason", errors)
    if state == "not_applicable":
        _text(record.get("entry_evidence"), "entry_evidence", errors)
    if record.get("attempt") is not None and not _positive_int(record["attempt"]):
        errors.append("attempt: must be a positive integer")
    return errors


def read_records(path):
    """Read a flow JSONL file tolerantly; return ``(records, problems)``.

    The rules match the ledger's reader: a trailing partial line was never a complete append
    and is discarded with a note, while a corrupt complete line is reported by line number.
    A repeated ``record_id`` with an identical payload is the same append replayed and is
    collapsed; the same id with a different payload is a conflict and is reported.
    """
    path = Path(path)
    if not path.exists():
        return [], ["no flow record at " + str(path)]
    raw = path.read_text(encoding="utf-8")
    lines = raw.splitlines()
    records, problems = [], []
    by_id = {}
    partial = bool(raw) and not raw.endswith("\n")
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        if partial and index == len(lines) - 1:
            problems.append(f"line {index + 1}: discarded a trailing partial line")
            break
        if len(line.encode("utf-8")) > MAX_RECORD_BYTES:
            problems.append(f"line {index + 1}: record exceeds {MAX_RECORD_BYTES} bytes")
            continue
        try:
            value = json.loads(line)
        except ValueError:
            problems.append(f"line {index + 1}: not JSON")
            continue
        errors = validate_record(value)
        if errors:
            problems.append(f"line {index + 1}: " + "; ".join(errors))
            continue
        record_id = value["record_id"]
        fingerprint = json.dumps(value, sort_keys=True, ensure_ascii=False)
        if record_id in by_id:
            if by_id[record_id] != fingerprint:
                problems.append(f"line {index + 1}: record_id {record_id!r} conflicts with "
                                "an earlier record")
            continue
        by_id[record_id] = fingerprint
        records.append(value)
    return records, problems


def _cycles(edges):
    """Every dependency cycle, named by the path that closes it."""
    state, found = {}, []

    def visit(node, path):
        state[node] = "visiting"
        path = path + [node]
        for target in edges.get(node, ()):
            if target not in edges:
                continue
            if state.get(target) == "visiting":
                found.append(path[path.index(target):] + [target])
            elif target not in state:
                visit(target, path)
        state[node] = "done"

    for node in edges:
        if node not in state:
            visit(node, [])
    return found


def _downstream(nodes, targets):
    """The targets plus every node that transitively depends on one of them."""
    affected = set(targets)
    changed = True
    while changed:
        changed = False
        for node in nodes:
            identifier = node["id"]
            if identifier in affected:
                continue
            if any(dependency in affected for dependency in node.get("depends_on", [])):
                affected.add(identifier)
                changed = True
    return affected


def _new_node(node):
    return {
        "id": node["id"],
        "gate": node["gate"],
        "depends_on": list(node.get("depends_on", [])),
        "applies": node.get("applies", True),
        "entry_evidence": node.get("entry_evidence", ""),
        "state": "pending",
        "reason": "",
        "evidence_ref": "",
        "task_id": "",
        "dispatch_id": "",
        "attempt": None,
        "max_attempt": None,
        "artifact_version": "",
        "imported_from": None,
        "invalidated_by": None,
        "divergent": False,
    }


def _apply_state(node, record, gaps):
    """Apply one state record; a stale acceptance is a gap, not an override."""
    state = record["state"]
    attempt = record.get("attempt")
    if isinstance(attempt, int):
        node["max_attempt"] = max(node["max_attempt"] or 0, attempt)
    if state == "accepted":
        if node["max_attempt"] and attempt < node["max_attempt"]:
            gaps.append(f"node {node['id']!r}: acceptance for attempt {attempt} is older "
                        f"than the attempt {node['max_attempt']} already recorded; it does "
                        "not override the newer state")
            return
        if node["task_id"] and record["task_id"] != node["task_id"]:
            node["divergent"] = True
            gaps.append(f"node {node['id']!r}: accepted by task {record['task_id']!r} while "
                        f"it is already bound to task {node['task_id']!r}")
            return
        node.update({
            "state": "accepted",
            "reason": record.get("reason", ""),
            "evidence_ref": record["evidence_ref"],
            "task_id": record["task_id"],
            "dispatch_id": record.get("dispatch_id", ""),
            "attempt": attempt,
            "artifact_version": record["artifact_version"],
            "imported_from": None,
            "invalidated_by": None,
        })
        return
    node["state"] = state
    node["reason"] = record.get("reason", "")
    node["evidence_ref"] = record.get("evidence_ref", "")
    node["invalidated_by"] = "revocation" if state == "invalidated" else None


def _apply_import(node, record, gaps):
    attempt = record.get("attempt")
    if isinstance(attempt, int):
        node["max_attempt"] = max(node["max_attempt"] or 0, attempt)
    if node["imported_from"] is not None:
        gaps.append(f"node {node['id']!r}: imported more than once; the first import stands")
        return
    node.update({
        "state": "accepted",
        "reason": "",
        "evidence_ref": record["verification"],
        "artifact_version": record["artifact_version"],
        "attempt": attempt,
        "imported_from": {
            "source_run": record["source_run"],
            "artifact": record["artifact"],
            "artifact_version": record["artifact_version"],
        },
        "invalidated_by": None,
    })


def plan(records):
    """Return the effective process plan for one Run's records.

    ``state`` is ``continue`` while work remains, ``complete`` only when every applicable node
    is satisfied, and ``uncertain`` when evidence conflicts, cannot be attributed, or the file
    itself is invalid. Completion covers the authorized endpoint only: it is not merge
    authorization, and the repository gates still decide that.
    """
    problems = []
    flows = [record for record in records if record["kind"] == "flow"]
    base = {
        "version": SCHEMA_VERSION,
        "mode": "flow",
        "flow_id": None,
        "run_id": None,
        "goal": "",
        "endpoint": "",
        "ready": [],
        "nodes": [],
        "waiting_user": [],
        "blocked": [],
        "invalidated": [],
        "evidence_gaps": [],
        "problems": problems,
        "note": "completion covers the authorized endpoint only; it is not merge authorization",
    }
    if not flows:
        base.update({"state": "uncertain",
                     "evidence_gaps": ["no flow record for this Run"]})
        return base
    if len({record["flow_id"] for record in flows}) > 1:
        problems.append("more than one flow_id in one file; the first flow record stands")
    flow = flows[0]
    declared = flow["nodes"]
    nodes = {node["id"]: _new_node(node) for node in declared}
    gaps = []
    for record in records:
        if record["kind"] == "flow" or record["flow_id"] != flow["flow_id"]:
            continue
        if record["kind"] == "invalidate":
            unknown = [target for target in record["nodes"] if target not in nodes]
            for target in unknown:
                gaps.append(f"invalidate names unknown node {target!r}")
            affected = _downstream(declared, [t for t in record["nodes"] if t in nodes])
            for target in affected:
                entry = nodes[target]
                entry["state"] = "invalidated"
                entry["reason"] = record["reason"]
                entry["invalidated_by"] = record["cause"]
                entry["evidence_ref"] = ""
            continue
        node = nodes.get(record.get("node"))
        if node is None:
            gaps.append(f"{record['kind']} names unknown node {record.get('node')!r}")
            continue
        if record["kind"] == "import":
            _apply_import(node, record, gaps)
        else:
            _apply_state(node, record, gaps)

    ordered = [nodes[node["id"]] for node in declared]
    for entry in ordered:
        if not entry["applies"] and entry["state"] == "pending":
            entry["state"] = "not_applicable"
            entry["reason"] = entry["entry_evidence"]
    ready, waiting_user, blocked, invalidated = [], [], [], []
    for entry in ordered:
        if not entry["applies"]:
            continue
        if entry["state"] == "invalidated":
            invalidated.append({"node": entry["id"], "reason": entry["reason"],
                                "cause": entry["invalidated_by"]})
            missing = [dependency for dependency in entry["depends_on"]
                       if nodes[dependency]["applies"]
                       and nodes[dependency]["state"] not in SATISFIED]
            if missing:
                blocked.append({"node": entry["id"], "state": "invalidated",
                                "reason": "waiting on " + ", ".join(missing)})
            else:
                ready.append({"node": entry["id"], "gate": entry["gate"],
                              "state": "invalidated"})
        elif entry["state"] == "pending":
            missing = [dependency for dependency in entry["depends_on"]
                       if nodes[dependency]["applies"]
                       and nodes[dependency]["state"] not in SATISFIED]
            if missing:
                blocked.append({"node": entry["id"], "state": "pending",
                                "reason": "waiting on " + ", ".join(missing)})
            else:
                ready.append({"node": entry["id"], "gate": entry["gate"],
                              "state": "pending"})
        elif entry["state"] == "waiting_user":
            waiting_user.append({"node": entry["id"], "reason": entry["reason"]})
        elif entry["state"] == "blocked":
            blocked.append({"node": entry["id"], "state": entry["state"],
                            "reason": entry["reason"]})

    uncertain = bool(gaps) or any(entry["divergent"] for entry in ordered)
    unfinished = [entry for entry in ordered
                  if entry["applies"] and entry["state"] in UNFINISHED]
    any_applicable = any(entry["applies"] for entry in ordered)
    if uncertain:
        state = "uncertain"
    elif unfinished or not any_applicable:
        state = "continue"
    else:
        state = "complete"
    base.update({
        "state": state,
        "flow_id": flow["flow_id"],
        "run_id": flow["run_id"],
        "goal": flow["goal"],
        "endpoint": flow["endpoint"],
        "ready": ready if not uncertain else [],
        "nodes": ordered,
        "waiting_user": waiting_user,
        "blocked": blocked,
        "invalidated": invalidated,
        "evidence_gaps": gaps,
    })
    if not any_applicable:
        base["evidence_gaps"] = gaps + [
            "no applicable node: the authorized endpoint has no work in this flow"]
    return base


def append_record(path, record):
    """Validate against the file's existing plan, then append; return the record.

    The check is deliberately more than schema: a ``flow`` may appear once, every other record
    must name a node of that flow, and its ``flow_id`` must match. Appending a record that
    contradicts the file would leave a plan nobody can resume from.
    """
    errors = validate_record(record)
    if errors:
        raise FlowError("; ".join(errors))
    path = Path(path)
    existing, problems = read_records(path)
    if problems and any("conflicts" in problem or "not JSON" in problem for problem in problems):
        raise FlowError("the existing flow file is invalid; fix it before appending")
    flows = [item for item in existing if item["kind"] == "flow"]
    if record["kind"] == "flow":
        if flows:
            raise FlowError("a flow file carries exactly one flow record")
    else:
        if not flows:
            raise FlowError("append the flow record before any state, invalidate or import")
        flow = flows[0]
        if record["flow_id"] != flow["flow_id"]:
            raise FlowError(f"flow_id {record['flow_id']!r} does not match this file's "
                            f"flow {flow['flow_id']!r}")
        known = {node["id"] for node in flow["nodes"]}
        targets = record.get("nodes", []) if record["kind"] == "invalidate" else [record["node"]]
        unknown = [target for target in targets if target not in known]
        if unknown:
            raise FlowError("unknown node(s): " + ", ".join(repr(item) for item in unknown))
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n"
    if len(line.encode("utf-8")) > MAX_RECORD_BYTES:
        raise FlowError(f"record exceeds {MAX_RECORD_BYTES} bytes")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    return record


def flow_path(run_id, home=None):
    """Resolve ``$ORCA_FLOW_HOME/runs/<run_id>/flow.jsonl``; the explicit home wins."""
    root = Path(home or os.environ.get(FLOW_HOME_ENV) or Path(DEFAULT_HOME).expanduser())
    return root / RUNS_DIR / run_id / FLOW_NAME


def _emit(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=1))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Explicit process records for one Run.")
    parser.add_argument("--version", action="version", version="flow " + str(SCHEMA_VERSION))
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="validate one JSON record")
    validate_parser.add_argument("--file", required=True, help="path to a JSON record")

    plan_parser = subparsers.add_parser("plan", help="print the effective plan")
    plan_parser.add_argument("--run-id", required=True)
    plan_parser.add_argument("--flow-home", default=None)
    plan_parser.add_argument("--flow", default=None, help="read this file instead of a home")

    append_parser = subparsers.add_parser("append", help="append a validated record")
    append_parser.add_argument("--run-id", required=True)
    append_parser.add_argument("--flow-home", default=None)
    append_parser.add_argument("--flow", default=None, help="write this file instead of a home")
    append_parser.add_argument("--record", required=True, help="path to a JSON record, or -")

    args = parser.parse_args(argv)

    if args.command == "validate":
        path = Path(args.file)
        if not path.exists():
            _emit({"version": SCHEMA_VERSION, "valid": False,
                   "errors": ["file not found: " + str(path)]})
            return 1
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            _emit({"version": SCHEMA_VERSION, "valid": False, "errors": [str(exc)]})
            return 1
        errors = validate_record(record)
        _emit({"version": SCHEMA_VERSION, "valid": not errors, "errors": errors})
        return 0 if not errors else 1

    path = Path(args.flow) if args.flow else flow_path(args.run_id, args.flow_home)

    if args.command == "plan":
        records, problems = read_records(path)
        answer = plan(records)
        answer["problems"] = problems + answer["problems"]
        if problems and answer["state"] != "uncertain":
            answer["state"] = "uncertain"
            answer["ready"] = []
            answer["evidence_gaps"] = answer["evidence_gaps"] + [
                "the flow file has invalid records; resolve them before resuming"]
        _emit(answer)
        return 0 if answer["state"] != "uncertain" else 1

    if args.record == "-":
        raw = sys.stdin.read()
    else:
        record_path = Path(args.record)
        if not record_path.exists():
            _emit({"version": SCHEMA_VERSION, "appended": False,
                   "errors": ["record not found: " + str(record_path)]})
            return 1
        raw = record_path.read_text(encoding="utf-8")
    try:
        record = json.loads(raw)
        append_record(path, record)
    except (ValueError, FlowError) as exc:
        _emit({"version": SCHEMA_VERSION, "appended": False, "errors": [str(exc)]})
        return 1
    _emit({"version": SCHEMA_VERSION, "appended": True, "flow": str(path),
           "record_id": record.get("record_id"), "kind": record.get("kind")})
    return 0


if __name__ == "__main__":
    sys.exit(main())
