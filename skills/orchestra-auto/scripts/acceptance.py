#!/usr/bin/env python3
"""Acceptance records: per-PR facts, minimum evidence and four levels.

The acceptance record is the coordinator's durable claim that a gate passed. It is a
ledger record (``kind: acceptance``) whose ``detail`` is this module's schema; storage,
locking, fsync and retention belong to ``ledger.py`` and are reused rather than copied.
The ledger stays an index: the PR host, git and the tracker remain the authorities, and
``verify`` reads those facts through an injected snapshot instead of trusting the record.

The schema is local and documented in ``references/gate-acceptance.md``. Nothing here is
a claim about a live Orca receipt shape. The start evidence this module accepts is
``turnStart: observed`` or the attempt's own artifact / status-projection reference; a
``worker_done`` receipt proves settlement only and is explicitly not a start proof.

Public interface (deep module):

* ``validate(record) -> errors`` — pure schema and minimum-evidence validation.
* ``verify(record, authority) -> errors`` — cross-check claimed levels against an
  injected authority snapshot (PR host, tracker, git ancestry).
* ``aggregate(ticket, records) -> ticket`` — pure four-level aggregation over ledger
  records: every linked PR must support a level, a later observation lowers a level
  without rewriting history, and conflicting records are ``divergent``.

CLI (product surface, not a test hook): ``record``, ``show``, ``observe``,
``open-questions``, ``resolve-question`` and ``retry``. JSON in and out; exit ``0`` is
success, ``1`` is invalid input or an I/O failure, ``2`` is a usage error.

Only the Python standard library is used.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import uuid

SCHEMA_VERSION = 1

LEVEL_NAMES = ("implemented", "integrated", "merged", "tracker_closed")

#: Documented gates a record may claim. An unknown gate is a typo, not a new gate.
GATES = ("G-docs", "G-wayfinder", "G-spec", "G-tickets", "G-impl", "G-merge")

DECIDERS = ("coordinator", "human")
REVIEW_AXES = ("standards", "spec")
CHECK_RESULTS = ("pass", "fail")
REVIEW_RESULTS = ("pass", "waiver")
START_SOURCES = ("turn_start", "artifact", "status_projection")
TERMINALS = ("never_merge",)
OBSERVATIONS = ("revert", "reopen")

LEVEL_KEYS = (
    "value", "evidence_ref", "at", "merge_sha", "pr_state", "ancestry", "read_back",
    "start", "terminal", "decided_by", "reason",
)
CHECK_KEYS = ("name", "command", "result", "evidence_ref")
REVIEW_KEYS = ("axis", "result", "evidence_ref", "waiver")
WAIVER_KEYS = ("reason", "spec_evidence_ref", "answer_evidence_ref")
PR_KEYS = ("number", "head_at_acceptance", "base")
RECORD_KEYS = (
    "ticket", "gate", "attempt", "accepted_sha", "accepted_at", "pr", "checks",
    "review", "levels", "decided_by", "decided_at", "answer_location",
)
OBSERVATION_KEYS = (
    "ticket", "pr", "level", "observation", "reason", "evidence_ref", "observed_at",
)

#: A bare ``grep`` proves a string exists in a file; it never proves a command ran.
_GREP_COMMANDS = ("grep", "egrep", "fgrep", "rg", "ag", "ack")
_SHA_TEXT = re.compile(r"[0-9a-fA-F]{7,64}\Z")

PENDING_DIRNAME = "acceptance-pending"
MAX_PAYLOAD_BYTES = 60 * 1024

#: Issue #20's open questions about the four levels. They are recorded as ledger
#: ``pending`` items so a long-horizon run can close without a human reply; the affected
#: semantics carry ``provisional`` so nothing here is frozen.
OPEN_QUESTIONS = (
    {
        "question_id": "four-levels-multi-pr",
        "question": "A ticket that spans two PRs: which PR owns 'merged'?",
        "affected": "multi_pr",
        "evidence_ref": "issue:20#question-1-multi-pr",
    },
    {
        "question_id": "four-levels-non-github-tracker",
        "question": "A tracker that closes itself from a merge keyword collapses "
                    "'merged' and 'tracker_closed'; is the split GitHub-shaped?",
        "affected": "tracker_close",
        "evidence_ref": "issue:20#question-2-tracker",
    },
    {
        "question_id": "four-levels-never-merge",
        "question": "Work that is deliberately never merged has no accepted terminal "
                    "state; is 'never_merge' that state?",
        "affected": "never_merge",
        "evidence_ref": "issue:20#question-3-never-merge",
    },
    {
        "question_id": "four-levels-revert",
        "question": "After a merge is reverted, does the level go back down, and who "
                    "says so?",
        "affected": "revert",
        "evidence_ref": "issue:20#question-4-revert",
    },
)


def _load_ledger():
    """Import the sibling ``ledger.py`` by path: storage is reused, not duplicated."""
    path = Path(__file__).with_name("ledger.py")
    spec = importlib.util.spec_from_file_location("orca_acceptance_ledger", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot build an import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LEDGER = _load_ledger()


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _utc(value):
    """True for an RFC3339 instant explicitly in UTC (``Z`` or ``+00:00``)."""
    if not isinstance(value, str):
        return False
    if not (value.endswith("Z") or value.endswith("+00:00")):
        return False
    try:
        datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return False
    return True


def _nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def _sha(value):
    return isinstance(value, str) and bool(_SHA_TEXT.fullmatch(value))


def _unknown_keys(value, allowed, path, errors):
    unexpected = sorted(set(value) - set(allowed))
    if unexpected:
        errors.append(path + ": unknown fields: " + ", ".join(unexpected))


# ---------------------------------------------------------------------------
# validate: schema and minimum evidence, pure
# ---------------------------------------------------------------------------


def _validate_pr(pr, errors):
    if not isinstance(pr, dict):
        errors.append("pr must be an object")
        return
    _unknown_keys(pr, PR_KEYS, "pr", errors)
    number = pr.get("number")
    if type(number) is not int or number < 1:
        errors.append("pr.number must be a positive integer")
    head = pr.get("head_at_acceptance")
    if not _sha(head):
        errors.append("pr.head_at_acceptance must be a commit SHA")
    if not _nonempty(pr.get("base")):
        errors.append("pr.base must be a non-empty branch name")


def _validate_check(index, check, errors):
    path = f"checks[{index}]"
    if not isinstance(check, dict):
        errors.append(path + " must be an object")
        return
    _unknown_keys(check, CHECK_KEYS, path, errors)
    if not _nonempty(check.get("name")):
        errors.append(path + ".name must be a non-empty string")
    command = check.get("command")
    if not _nonempty(command):
        errors.append(path + ".command must be a non-empty string")
    else:
        tokens = command.split()
        greps = tokens and (tokens[0] in _GREP_COMMANDS
                            or tokens[:2] == ["git", "grep"])
        if greps:
            errors.append(
                path + ".command is a grep: a grep only proves a string exists and "
                "does not prove the command ran")
    if check.get("result") not in CHECK_RESULTS:
        errors.append(path + ".result must be 'pass' or 'fail'")
    if not _nonempty(check.get("evidence_ref")):
        errors.append(path + ".evidence_ref must name the log that was captured")


def _validate_review(review, errors):
    path = "review"
    if not isinstance(review, dict):
        errors.append(path + " must be an object")
        return
    _unknown_keys(review, REVIEW_KEYS, path, errors)
    axis = review.get("axis")
    if not isinstance(axis, list) or sorted(axis) != sorted(REVIEW_AXES):
        errors.append(
            path + ".axis must name both axes exactly once: " + " and ".join(REVIEW_AXES))
    if not _nonempty(review.get("evidence_ref")) and review.get("result") != "waiver":
        errors.append(path + ".evidence_ref must name the review report")
    result = review.get("result")
    if result not in REVIEW_RESULTS:
        errors.append(path + ".result must be 'pass' or 'waiver'")
    if result == "waiver":
        waiver = review.get("waiver")
        if not isinstance(waiver, dict):
            errors.append(path + ".waiver must record why both axes were waived")
            return
        _unknown_keys(waiver, WAIVER_KEYS, path + ".waiver", errors)
        if not _nonempty(waiver.get("reason")):
            errors.append(path + ".waiver.reason must record the waiver reason")
        for key in ("spec_evidence_ref", "answer_evidence_ref"):
            if not _nonempty(waiver.get(key)):
                errors.append(
                    path + ".waiver." + key
                    + " must show the reason is in both the spec and the Answer")


def _validate_start(level, errors):
    path = "levels.implemented.start"
    start = level.get("start")
    if not isinstance(start, dict):
        errors.append(path + " is required: an observed turn start or the attempt evidence")
        return
    if start.get("turn_start") == "observed":
        return
    source = start.get("source")
    if source == "worker_done":
        errors.append(
            path + ": worker_done proves settlement only and never a start or acceptance")
        return
    if source in START_SOURCES[1:] and _nonempty(start.get("evidence_ref")):
        return
    errors.append(
        path + " must be turnStart: observed or the attempt's artifact / "
        "status-projection evidence")


def _validate_level(name, level, record, errors):
    path = "levels." + name
    if not isinstance(level, dict):
        errors.append(path + " must be an object")
        return
    _unknown_keys(level, LEVEL_KEYS, path, errors)
    value = level.get("value")
    if not isinstance(value, bool):
        errors.append(path + ".value must be a boolean")
        return
    if not _utc(level.get("at")):
        errors.append(path + ".at must be an RFC3339 UTC instant")

    terminal = level.get("terminal")
    if terminal is not None:
        if name != "merged" or terminal not in TERMINALS:
            errors.append(path + ".terminal is only defined for the merged level as "
                                + " / ".join(TERMINALS))
        if value:
            errors.append(path + ".terminal contradicts value: a terminal level is false")
        if level.get("decided_by") != "human":
            errors.append(path + ".terminal needs decided_by: human")
        if not _nonempty(level.get("reason")):
            errors.append(path + ".terminal must record the human's reason")
    elif level.get("decided_by") is not None and name != "merged":
        errors.append(path + ".decided_by is only defined for the merged level")

    if value or terminal is not None:
        if not _nonempty(level.get("evidence_ref")):
            errors.append(
                path + " is claimed without evidence_ref; a level needs an evidence "
                "reference")

    if not value:
        return
    if name == "implemented":
        _validate_start(level, errors)
    elif name == "merged":
        if not _sha(level.get("merge_sha")):
            errors.append("levels.merged.merge_sha must be the merge commit SHA")
        if level.get("pr_state") != "MERGED":
            errors.append("levels.merged.pr_state must be MERGED as reported by the PR host")
        ancestry = level.get("ancestry")
        if not isinstance(ancestry, dict):
            errors.append("levels.merged.ancestry must record the git ancestry check")
        else:
            if ancestry.get("ancestor") != record.get("accepted_sha"):
                errors.append(
                    "levels.merged.ancestry.ancestor must be the accepted_sha")
            expected = level.get("merge_sha")
            if ancestry.get("descendant") != expected:
                errors.append(
                    "levels.merged.ancestry.descendant must be the merge_sha")
            if ancestry.get("result") is not True:
                errors.append(
                    "levels.merged.ancestry.result must be true: git merge-base "
                    "--is-ancestor <accepted_sha> <merge_sha>")
    elif name == "tracker_closed":
        read_back = level.get("read_back")
        if not isinstance(read_back, dict):
            errors.append(
                "levels.tracker_closed.read_back must be a tracker read-back")
        else:
            if read_back.get("state") != "closed":
                errors.append(
                    "levels.tracker_closed.read_back.state must be 'closed' from the tracker")
            if not _utc(read_back.get("at")):
                errors.append(
                    "levels.tracker_closed.read_back.at must be the read-back time")


def validate(record):
    """Return a list of error strings; empty means the record may be recorded.

    Rejects a ``true`` level without an ``evidence_ref`` and every missing piece of the
    minimum evidence the spec names: a merge needs the PR host's ``MERGED`` state, the
    merge SHA and ancestry; a tracker close needs a read-back; a start needs
    ``turnStart: observed`` or the attempt's artifact / projection evidence; a test check
    needs command, result and a log (a grep is not a test run); a review needs both axes
    and a reference, or a waiver whose reason is in both the spec and the Answer.
    """
    if not isinstance(record, dict):
        return ["record must be a JSON object"]
    errors = []
    _unknown_keys(record, RECORD_KEYS, "record", errors)

    if not _nonempty(record.get("ticket")):
        errors.append("ticket must be a non-empty string")
    gate = record.get("gate")
    if not isinstance(gate, list) or not gate:
        errors.append("gate must be a non-empty list of gates")
    else:
        for item in gate:
            if item not in GATES:
                errors.append("gate: unknown gate " + repr(item))
    attempt = record.get("attempt")
    if type(attempt) is not int or attempt < 1:
        errors.append("attempt must be a positive integer")
    if not _sha(record.get("accepted_sha")):
        errors.append("accepted_sha must be the accepted commit SHA")
    if not _utc(record.get("accepted_at")):
        errors.append("accepted_at must be an RFC3339 UTC instant")

    _validate_pr(record.get("pr"), errors)

    checks = record.get("checks")
    if not isinstance(checks, list) or not checks:
        errors.append("checks must be a non-empty list")
    else:
        for index, check in enumerate(checks):
            _validate_check(index, check, errors)

    _validate_review(record.get("review"), errors)

    levels = record.get("levels")
    if not isinstance(levels, dict):
        errors.append("levels must be an object with the four levels")
    else:
        _unknown_keys(levels, LEVEL_NAMES, "levels", errors)
        for name in LEVEL_NAMES:
            if name not in levels:
                errors.append("levels." + name + " is required")
                continue
            _validate_level(name, levels[name], record, errors)

    if record.get("decided_by") not in DECIDERS:
        errors.append("decided_by must be 'coordinator' or 'human'")
    if not _utc(record.get("decided_at")):
        errors.append("decided_at must be an RFC3339 UTC instant")
    if not _nonempty(record.get("answer_location")):
        errors.append("answer_location must name where the Answer lives")

    return errors


def validate_observation(observation):
    """Return the errors for one revert / reopen observation."""
    if not isinstance(observation, dict):
        return ["observation must be a JSON object"]
    errors = []
    _unknown_keys(observation, OBSERVATION_KEYS, "observation", errors)
    if not _nonempty(observation.get("ticket")):
        errors.append("observation.ticket must be a non-empty string")
    pr = observation.get("pr")
    if type(pr) is not int or pr < 1:
        errors.append("observation.pr must be a positive PR number")
    if observation.get("level") not in LEVEL_NAMES:
        errors.append("observation.level must be one of " + ", ".join(LEVEL_NAMES))
    if observation.get("observation") not in OBSERVATIONS:
        errors.append("observation.observation must be 'revert' or 'reopen'")
    if not _nonempty(observation.get("reason")):
        errors.append("observation.reason must record why the level changed")
    if not _nonempty(observation.get("evidence_ref")):
        errors.append("observation.evidence_ref must name the read-back evidence")
    if not _utc(observation.get("observed_at")):
        errors.append("observation.observed_at must be an RFC3339 UTC instant")
    return errors


# ---------------------------------------------------------------------------
# verify: cross-check claimed levels against injected authority
# ---------------------------------------------------------------------------


def _section(snapshot, name, rows_key):
    """Return ``(state, reason, rows)`` for one authority section.

    A missing, unreadable or failed section is ``unknown`` with its rows withheld: an
    empty success is never invented, because "nothing there" is not "could not read".
    """
    section = snapshot.get(name) if isinstance(snapshot, dict) else None
    if not isinstance(section, dict):
        return "unknown", name + " authority not provided", []
    if section.get("state") != "ok":
        reason = section.get("reason")
        return "unknown", name + ": " + (reason if _nonempty(reason) else "unreadable"), []
    rows = section.get(rows_key)
    if not isinstance(rows, list):
        return "unknown", name + ": the section carries no rows", []
    return "ok", "", [row for row in rows if isinstance(row, dict)]


def _number(value):
    if type(value) is int:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def verify(record, authority):
    """Return the errors where a claimed level is not supported by authority.

    Only claimed levels are checked, so a record whose levels are still false needs no
    readable authority. The PR host must report ``MERGED`` with the recorded merge SHA,
    git ancestry must confirm the merge, and a tracker close needs a ``closed``
    read-back. ``authority`` is an injected snapshot; this function performs no I/O.
    """
    errors = []
    levels = record.get("levels") if isinstance(record, dict) else None
    if not isinstance(levels, dict):
        return ["levels must be an object before authority can be checked"]
    pr = record.get("pr") if isinstance(record.get("pr"), dict) else {}

    merged = levels.get("merged")
    if isinstance(merged, dict) and merged.get("value") is True:
        state, reason, rows = _section(authority, "pr_host", "pull_requests")
        if state != "ok":
            errors.append("PR host is unreadable, so the merge cannot be proven: " + reason)
        else:
            number = _number(pr.get("number"))
            row = next((item for item in rows if _number(item.get("pr")) == number), None)
            if row is None:
                errors.append(
                    "PR host has no record for PR " + str(pr.get("number")))
            else:
                if row.get("state") != "MERGED":
                    errors.append(
                        "PR host reports " + repr(row.get("state"))
                        + " for PR " + str(pr.get("number")) + "; a merge needs MERGED")
                if row.get("merge_sha") != merged.get("merge_sha"):
                    errors.append(
                        "PR host merge_sha does not match the recorded merge_sha")
        git_state, git_reason, ancestry_rows = _section(authority, "git", "ancestry")
        if git_state != "ok":
            errors.append("git ancestry is unreadable: " + git_reason)
        else:
            accepted = record.get("accepted_sha")
            merge_sha = merged.get("merge_sha")
            covered = any(
                item.get("ancestor") == accepted
                and item.get("descendant") == merge_sha
                and item.get("result") is True
                for item in ancestry_rows)
            if not covered:
                errors.append(
                    "git ancestry does not prove the accepted_sha is an ancestor of the "
                    "merge_sha")

    tracker = levels.get("tracker_closed")
    if isinstance(tracker, dict) and tracker.get("value") is True:
        state, reason, rows = _section(authority, "tracker", "tickets")
        if state != "ok":
            errors.append("tracker is unreadable, so the close cannot be proven: " + reason)
        else:
            ticket = record.get("ticket")
            row = next((item for item in rows if item.get("ticket") == ticket), None)
            if row is None:
                errors.append("tracker has no record for ticket " + str(ticket))
            elif row.get("state") != "closed":
                errors.append(
                    "tracker reports " + repr(row.get("state"))
                    + " for ticket " + str(ticket) + "; tracker_closed needs a closed "
                    "read-back")

    return errors


# ---------------------------------------------------------------------------
# aggregate: four levels per ticket, per PR, history preserved
# ---------------------------------------------------------------------------


def _detail(record):
    detail = record.get("detail") if isinstance(record, dict) else None
    return detail if isinstance(detail, dict) else {}


def _ticket_of(record):
    detail = _detail(record)
    if _nonempty(detail.get("ticket")):
        return detail["ticket"]
    subject = record.get("subject") if isinstance(record, dict) else None
    if isinstance(subject, dict) and _nonempty(subject.get("ticket")):
        return subject["ticket"]
    return None


def _pr_number(value):
    if isinstance(value, dict):
        return _number(value.get("number"))
    return _number(value)


def aggregate(ticket, records):
    """Return the ticket-level four levels over its ledger records.

    The documented rule: a ticket may link several PRs (a partial integration), and the
    ticket-level level is true only when **every** linked PR record supports it. A later
    ``observation`` (revert / reopen) sets the affected level false and is attached as
    ``last_observation`` while the original evidence reference is preserved — history is
    appended to, never overwritten. A later attempt re-accepting the same PR is the claim
    in force and leaves the earlier ones in ``records`` as history; records at the *same*
    attempt that disagree about the same PR are ``divergent``, and that PR's levels are
    neither averaged nor claimed.
    """
    accepted, observations = [], []
    for record in records or []:
        if not isinstance(record, dict) or _ticket_of(record) != ticket:
            continue
        kind = record.get("kind")
        if kind == "acceptance":
            accepted.append(record)
        elif kind == "observation":
            observations.append(record)

    by_pr, order = {}, []
    for record in accepted:
        number = _pr_number(_detail(record).get("pr"))
        if number is None:
            continue
        if number not in by_pr:
            by_pr[number] = []
            order.append(number)
        by_pr[number].append(record)

    provisional = ["four_levels"]
    prs = []
    terminals = set()
    for number in order:
        group = by_pr[number]
        # A later attempt re-accepted the same PR: the newest attempt is the claim in
        # force, and the earlier ones stay in ``records`` as history. Only records at the
        # same attempt can genuinely disagree.
        attempt = max((_detail(record).get("attempt") for record in group
                       if type(_detail(record).get("attempt")) is int), default=1)
        current = [record for record in group
                   if _detail(record).get("attempt") == attempt]
        signatures = {_canonical({
            "accepted_sha": _detail(record).get("accepted_sha"),
            "levels": [
                (_level_name, _detail(record).get("levels", {}).get(_level_name, {}).get(
                    "value"))
                for _level_name in LEVEL_NAMES
            ],
        }) for record in current}
        divergent = len(signatures) > 1
        latest = current[-1]
        detail = _detail(latest)
        levels = {}
        for name in LEVEL_NAMES:
            value = detail.get("levels", {}).get(name)
            levels[name] = dict(value) if isinstance(value, dict) else {"value": False}
            if divergent:
                levels[name]["value"] = False
        for record in observations:
            observed_detail = _detail(record)
            if _pr_number(observed_detail.get("pr")) != number:
                continue
            name = observed_detail.get("level")
            if name not in levels:
                continue
            levels[name]["value"] = False
            levels[name]["last_observation"] = {
                "observation": observed_detail.get("observation"),
                "reason": observed_detail.get("reason"),
                "evidence_ref": observed_detail.get("evidence_ref"),
                "observed_at": observed_detail.get("observed_at"),
                "record_id": record.get("record_id"),
            }
            tag = observed_detail.get("observation")
            if tag in ("revert", "reopen") and tag not in provisional:
                provisional.append(tag)
        terminal = levels["merged"].get("terminal")
        if terminal is not None:
            terminals.add(terminal)
        if divergent:
            provisional.append("divergent")
        prs.append({
            "number": number,
            "attempt": attempt,
            "divergent": divergent,
            "accepted_sha": detail.get("accepted_sha"),
            "levels": levels,
            "records": [record.get("record_id") for record in group],
        })

    if len(prs) > 1 and "multi_pr" not in provisional:
        provisional.append("multi_pr")

    ticket_levels = {}
    for name in LEVEL_NAMES:
        supported = bool(prs) and all(
            pr["levels"][name].get("value") is True for pr in prs)
        ticket_levels[name] = {
            "value": supported,
            "evidence_refs": [pr["levels"][name].get("evidence_ref")
                              for pr in prs if pr["levels"][name].get("evidence_ref")],
        }
    terminal = None
    if len(terminals) == 1:
        terminal = next(iter(terminals))
        if "never_merge" not in provisional:
            provisional.append("never_merge")
    elif len(terminals) > 1:
        terminal = "divergent"
        if "divergent" not in provisional:
            provisional.append("divergent")

    return {
        "ticket": ticket,
        "prs": prs,
        "levels": ticket_levels,
        "terminal": terminal,
        "divergent": bool(terminals and terminal == "divergent") or any(
            pr["divergent"] for pr in prs),
        "provisional": provisional,
        "records": [record.get("record_id") for record in accepted],
    }


# ---------------------------------------------------------------------------
# Ledger records and the CLI
# ---------------------------------------------------------------------------


def _record_id(payload):
    """A deterministic id: an identical record re-appended is a no-op, a changed one is new."""
    key = "orca-acceptance:" + hashlib.sha256(
        _canonical(payload).encode("utf-8")).hexdigest()
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


def _ledger_record(payload, run_id):
    return {
        "schema_version": LEDGER.SCHEMA_VERSION,
        "record_id": _record_id(payload),
        "kind": "acceptance",
        "run_id": run_id,
        "subject": {
            "ticket": payload["ticket"],
            "pr": payload["pr"]["number"],
            "attempt": payload["attempt"],
        },
        "action": "acceptance",
        "request_id": "",
        "idempotency": "none",
        "state": "completed",
        "evidence_ref": payload["answer_location"],
        "observed_at": payload["decided_at"],
        "actor": payload["decided_by"],
        "detail": payload,
    }


def _observation_record(observation, run_id):
    detail = dict(observation)
    return {
        "schema_version": LEDGER.SCHEMA_VERSION,
        "record_id": str(uuid.uuid4()),
        "kind": "observation",
        "run_id": run_id,
        "subject": {"ticket": observation["ticket"], "pr": observation["pr"]},
        "action": "acceptance",
        "request_id": "",
        "idempotency": "none",
        "state": "observed",
        "evidence_ref": observation["evidence_ref"],
        "observed_at": observation["observed_at"],
        "actor": "coordinator",
        "detail": detail,
    }


def _target_repository():
    """The repository containing the invoking shell, if any.

    The ledger's CLI does the same inference; this repeats only the lookup, so the path
    policy itself stays in ``ledger.home_path``.
    """
    try:
        candidate = Path.cwd().resolve()
    except OSError:
        return None
    for path in (candidate, *candidate.parents):
        if (path / ".git").exists():
            return path
    return None


def _home():
    """The ledger home, under ledger.py's own path policy and target-repository guard."""
    return LEDGER.home_path(target_repo=_target_repository())


def _pending_path(home, run_id, record_id):
    directory = home / "runs" / run_id / PENDING_DIRNAME
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory / (record_id + ".json")


def _write_pending(path, record):
    """Persist a record whose append failed, so the retry writes the same record id."""
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, (_canonical(record) + "\n").encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def _emit(payload):
    print(json.dumps(payload, ensure_ascii=False))


def command_record(args, home):
    try:
        payload = json.loads(args.record)
    except (TypeError, ValueError) as error:
        _emit({"recorded": False, "gate_passed": False,
               "errors": ["record is not valid JSON: " + str(error)]})
        return 1
    if len(args.record) > MAX_PAYLOAD_BYTES:
        _emit({"recorded": False, "gate_passed": False,
               "errors": ["record exceeds 60 KiB"]})
        return 1

    errors = validate(payload)
    if not errors and args.authority:
        try:
            authority = LEDGER.validated_authority(
                json.loads(Path(args.authority).read_text()))
        except (OSError, ValueError) as error:
            errors = ["cannot read the authority file: " + str(error)]
        else:
            errors = verify(payload, authority)
    if errors:
        _emit({"recorded": False, "gate_passed": False, "errors": errors})
        return 1

    record = _ledger_record(payload, args.run_id)
    try:
        record_id = LEDGER.append(record, home=home)
    except (OSError, ValueError, TypeError) as error:
        path = _pending_path(home, args.run_id, record["record_id"])
        _write_pending(path, record)
        _emit({
            "recorded": False,
            "gate_passed": False,
            "errors": ["the acceptance record is not durable: " + str(error)],
            "record_id": record["record_id"],
            "retry_path": str(path),
            "answer_location": payload["answer_location"],
        })
        return 1
    _emit({
        "recorded": True,
        "gate_passed": True,
        "record_id": record_id,
        "ticket": payload["ticket"],
        "pr": payload["pr"]["number"],
        "answer_location": payload["answer_location"],
    })
    return 0


def command_observe(args, home):
    try:
        payload = json.loads(args.record)
    except (TypeError, ValueError) as error:
        _emit({"recorded": False,
               "errors": ["observation is not valid JSON: " + str(error)]})
        return 1
    errors = validate_observation(payload)
    if errors:
        _emit({"recorded": False, "errors": errors})
        return 1
    record = _observation_record(payload, args.run_id)
    try:
        record_id = LEDGER.append(record, home=home)
    except (OSError, ValueError, TypeError) as error:
        _emit({"recorded": False, "errors": ["the observation is not durable: "
                                             + str(error)]})
        return 1
    _emit({"recorded": True, "record_id": record_id,
           "ticket": payload["ticket"], "pr": payload["pr"],
           "level": payload["level"], "observation": payload["observation"]})
    return 0


def command_show(args, home):
    records = LEDGER.read(args.run_id, home=home)
    ticket = aggregate(args.ticket, records)
    shown = [record for record in records
             if record.get("kind") in ("acceptance", "observation")
             and _ticket_of(record) == args.ticket]
    _emit({"ticket": args.ticket, "records": shown,
           "tickets": {args.ticket: ticket}})
    return 0


def _question_items(run_id, home):
    records = LEDGER.read(run_id, home=home)
    existing = {}
    resolutions = {}
    for record in records:
        detail = _detail(record)
        question_id = detail.get("question_id")
        if not _nonempty(question_id):
            continue
        if record.get("kind") == "pending":
            existing[question_id] = record
        elif record.get("kind") == "observation" and detail.get("status") == "resolved":
            resolutions[question_id] = record
    items, appended = [], []
    for question in OPEN_QUESTIONS:
        question_id = question["question_id"]
        record = existing.get(question_id)
        if record is None:
            detail = {
                **question,
                "ticket": "39",
                "issue": 20,
                "provisional": True,
                "status": "pending",
            }
            record = {
                "schema_version": LEDGER.SCHEMA_VERSION,
                "record_id": str(uuid.uuid4()),
                "kind": "pending",
                "run_id": run_id,
                "subject": {"ticket": "39"},
                "action": "acceptance",
                "request_id": "",
                "idempotency": "none",
                "state": "pending",
                "evidence_ref": question["evidence_ref"],
                "observed_at": _now(),
                "actor": "rule",
                "detail": detail,
            }
            appended.append(record)
        item = {
            "question_id": question_id,
            "question": question["question"],
            "affected": question["affected"],
            "provisional": True,
            "status": "pending",
            "evidence_ref": question["evidence_ref"],
            "record_id": record["record_id"],
        }
        resolution = resolutions.get(question_id)
        if resolution is not None:
            resolution_detail = _detail(resolution)
            item["status"] = "resolved"
            item["resolution"] = {
                "reason": resolution_detail.get("reason"),
                "evidence_ref": resolution_detail.get("evidence_ref"),
                "record_id": resolution.get("record_id"),
            }
        items.append(item)
    for record in appended:
        LEDGER.append(record, home=home)
    return items


def command_open_questions(args, home):
    items = _question_items(args.run_id, home)
    _emit({"items": items, "count": len(items)})
    return 0


def command_resolve_question(args, home):
    question = next((item for item in OPEN_QUESTIONS
                     if item["question_id"] == args.question_id), None)
    if question is None:
        _emit({"status": "refused", "errors": [
            "unknown question id: " + str(args.question_id)]})
        return 1
    if not _nonempty(args.reason) or not _nonempty(args.evidence_ref):
        _emit({"status": "refused", "errors": [
            "a resolution needs --reason and --evidence-ref"]})
        return 1
    record = {
        "schema_version": LEDGER.SCHEMA_VERSION,
        "record_id": str(uuid.uuid4()),
        "kind": "observation",
        "run_id": args.run_id,
        "subject": {"ticket": "39"},
        "action": "acceptance",
        "request_id": "",
        "idempotency": "none",
        "state": "observed",
        "evidence_ref": args.evidence_ref,
        "observed_at": _now(),
        "actor": "human" if args.human else "coordinator",
        "detail": {
            "question_id": question["question_id"],
            "affected": question["affected"],
            "ticket": "39",
            "issue": 20,
            "status": "resolved",
            "provisional": True,
            "reason": args.reason,
            "evidence_ref": args.evidence_ref,
        },
    }
    try:
        record_id = LEDGER.append(record, home=home)
    except (OSError, ValueError, TypeError) as error:
        _emit({"status": "refused", "errors": ["the resolution is not durable: "
                                               + str(error)]})
        return 1
    _emit({"question_id": question["question_id"], "status": "resolved",
           "record_id": record_id})
    return 0


def command_retry(args, home):
    directory = home / "runs" / args.run_id / PENDING_DIRNAME
    retried = []
    remaining = 0
    if directory.is_dir() and not directory.is_symlink():
        for path in sorted(directory.glob("*.json")):
            try:
                record = json.loads(path.read_text())
            except (OSError, ValueError) as error:
                retried.append({"path": str(path), "recorded": False,
                                "errors": ["unreadable pending record: " + str(error)]})
                remaining += 1
                continue
            try:
                record_id = LEDGER.append(record, home=home)
            except (OSError, ValueError, TypeError) as error:
                retried.append({"path": str(path), "recorded": False,
                                "record_id": record.get("record_id"),
                                "errors": [str(error)]})
                remaining += 1
                continue
            path.unlink()
            retried.append({"recorded": True, "record_id": record_id})
    _emit({"pending": remaining, "retried": retried})
    return 0 if remaining == 0 else 1


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="acceptance.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    record_parser = sub.add_parser(
        "record", help="validate, verify and append one acceptance record")
    record_parser.add_argument("--run-id", required=True)
    record_parser.add_argument("--record", required=True,
                               help="acceptance record JSON, or - for stdin")
    record_parser.add_argument(
        "--authority", metavar="FILE",
        help="recorded pr_host/tracker/git sections to verify the claimed levels")

    show_parser = sub.add_parser("show", help="aggregate and print one ticket's levels")
    show_parser.add_argument("--run-id", required=True)
    show_parser.add_argument("--ticket", required=True)

    observe_parser = sub.add_parser(
        "observe", help="append a revert/reopen observation that lowers a level")
    observe_parser.add_argument("--run-id", required=True)
    observe_parser.add_argument("--record", required=True, help="observation JSON")

    questions_parser = sub.add_parser(
        "open-questions",
        help="record issue #20's open four-level questions as pending items")
    questions_parser.add_argument("--run-id", required=True)

    resolve_parser = sub.add_parser(
        "resolve-question", help="append a later human decision as an observation")
    resolve_parser.add_argument("--run-id", required=True)
    resolve_parser.add_argument("--question-id", required=True)
    resolve_parser.add_argument("--reason", required=True)
    resolve_parser.add_argument("--evidence-ref", required=True)
    resolve_parser.add_argument("--human", action="store_true")

    retry_parser = sub.add_parser(
        "retry", help="retry every acceptance record whose append failed")
    retry_parser.add_argument("--run-id", required=True)

    args = parser.parse_args(argv)
    try:
        home = _home()
        if args.command == "record":
            if args.record == "-":
                args.record = sys.stdin.read(MAX_PAYLOAD_BYTES + 1)
            return command_record(args, home)
        if args.command == "observe":
            return command_observe(args, home)
        if args.command == "show":
            return command_show(args, home)
        if args.command == "open-questions":
            return command_open_questions(args, home)
        if args.command == "resolve-question":
            return command_resolve_question(args, home)
        return command_retry(args, home)
    except (OSError, TypeError, ValueError) as error:
        _emit({"recorded": False, "gate_passed": False, "errors": [str(error)]})
        print("Acceptance error: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
