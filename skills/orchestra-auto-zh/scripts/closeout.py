#!/usr/bin/env python3
"""Closeout: the ten-row checklist, one-way-door pending items and the release gate.

A closeout is a reconciliation, not a claim. ``plan(facts)`` is a pure planner over
authority facts and returns the ten documented rows; ``reconcile(run_id, authority)`` reads
authority through injected adapters, consumes the revert/reopen observations T09 appends,
resolves the pending items it can prove, and persists one ``kind: closeout`` record that
carries the Answer location. ``release_decision`` gates a release on a settled Dispatch and
a durable closeout record.

The authority map is the spec's: Orca lifecycle, the PR host/git and the tracker are
authoritative; the ledger is an index; ``progress.json`` is a projection and never evidence.
Row 9 therefore records a gap and never raises the checklist, and row 10 keeps a one-way
door visible without a coordinator turn.

Storage, locking, fsync and retention belong to ``ledger.py``, which also owns the shared
JSON encoding; the acceptance schema, aggregate, value predicates and pending-write seam
belong to ``acceptance.py``; the release decision consumes ``health.authorize_release`` and
never restates it. Each is imported from its owning module, not copied or reimplemented here.

Only the Python standard library is used. The checklist is documented in
``references/gate-acceptance.md`` §6.7.
"""

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import uuid

SCHEMA_VERSION = 1

#: The ten documented rows, in order. ``repeatable`` is whether authority can prove the
#: fact again later; row 1 is the one fact only the acceptance record can carry.
ROWS = (
    {"id": 1, "fact": "the accepted SHA per PR",
     "authority": "ledger acceptance record", "repeatable": False,
     "action": "must be persisted at acceptance; it cannot be reconstructed later"},
    {"id": 2, "fact": "PR MERGED, base and merge SHA",
     "authority": "PR host API", "repeatable": True,
     "action": "read back and write an observation"},
    {"id": 3, "fact": "the merge covers the accepted head",
     "authority": "git merge-base --is-ancestor / git merges (equivalence)",
     "repeatable": True,
     "action": "on failure mark divergent and escalate"},
    {"id": 4, "fact": "main contains the merge",
     "authority": "git rev-parse / ancestry", "repeatable": True,
     "action": "record the ancestry result"},
    {"id": 5, "fact": "local main is synced",
     "authority": "git status / rev-parse / upstream, ff-only", "repeatable": True,
     "action": "blocked -> pause later merges"},
    {"id": 6, "fact": "every ticket is integrated",
     "authority": "integration branch ancestry / PR Closes", "repeatable": True,
     "action": "record the result per ticket"},
    {"id": 7, "fact": "tracker state",
     "authority": "tracker read-back", "repeatable": True,
     "action": "record the close or reopen; an exception escalates instead of an auto-close"},
    {"id": 8, "fact": "worker lifecycle",
     "authority": "worker-show / worker-list / release receipt", "repeatable": True,
     "action": "enumerate reclaimable workers and settle or explain each"},
    {"id": 9, "fact": "dashboard projection",
     "authority": "progress.json (non-authoritative)", "repeatable": True,
     "action": "record the gap only; never treat the projection as evidence"},
    {"id": 10, "fact": "one-way-door pending",
     "authority": "ledger pending", "repeatable": True,
     "action": "check again when due"},
)
NON_REPEATABLE = tuple(item["id"] for item in ROWS if not item["repeatable"])

#: The statuses a row can take. ``gap`` never blocks: it is how row 9 records a
#: non-authoritative projection.
BLOCKING_STATUSES = ("blocked", "pending", "unknown")

#: A pending item explains a closeout that is not ready while only these rows block.
PENDING_EXPLAINED_ROWS = (2, 3, 4, 5, 7, 10)

#: The worker-list terminal states that still need an action before the Run is clean.
#: ``worker-list --terminal-state`` is a verified command surface; its row shape is a
#: fixture until a live receipt proves it, so a missing state is never invented.
#: ``retained`` is a decision already made (the user asked to keep the worker), so it is
#: reported but never treated as unfinished work.
RECLAIMABLE_STATES = ("reclaimable", "release_pending", "release_unknown")
RETAINED_STATES = ("retained",)

#: The pending types that are a one-way door with four reconcilable checks. Every other
#: pending record stays visible in the ledger but does not block a declaration: the open
#: four-level questions in issue #20 are explicitly closable without a human reply.
ONE_WAY_DOOR_TYPES = ("human_merge",)

LEVEL_ROWS = {"merged": (2, 3, 4), "tracker_closed": (7,)}
OBSERVATIONS = ("revert", "reopen")

CLOSEOUT_OUTCOMES = ("success", "failure", "pending")
PENDING_DIRNAME = "closeout-pending"

#: The failure classes of ``references/failed-dispatch-recovery.md``. A failure closeout
#: names exactly one; a classification outside this set is not a recovery classification.
FAILURE_CLASSES = (
    "transient_infrastructure",
    "setup_fault",
    "isolation_fault",
    "artifact_present",
    "authority_fault",
)
#: The script-reported next actions of the recovery sequence. ``authorize-release`` is the
#: step ``failure-record`` hands to ``failure-plan``; only ``failure-plan`` emits ``release``.
FAILURE_NEXT_ACTIONS = (
    "read-settled", "record-closeout", "authorize-release", "escalate", "release",
)
FAILURE_VERIFICATION_SOURCE = "worker-release --help"
FAILURE_VERSION_SCOPE = (
    "worker-release CLI help; replay beyond the help stays an assumption")

FACTS_SECTIONS = (
    "acceptance", "observations", "pending",
    "pr_host", "git", "tickets", "tracker", "workers", "dashboard", "release",
)
AUTHORITY_SECTIONS = (
    "pr_host", "git", "tickets", "tracker", "workers", "dashboard", "release",
)
SECTION_STATES = ("ok", "unknown")
RELEASE_VERIFICATION_KEYS = ("source", "at", "version_scope")
MAPPING_KEYS = ("dispatch_id", "task_id", "attempt", "worktree")

#: The row shape each authority section's row list holds, as read by the planner
#: (``_row_two`` through ``_row_ten``). ``--print-schema`` prints these so a reader is told
#: the same shape the planner reads.
SECTION_ROWS = {
    "pr_host": ("pull_requests", ("pr", "state", "merge_sha", "base")),
    "git": ("ancestry", ("ancestor", "descendant", "result")),
    "tickets": ("tickets", ("ticket", "integrated", "evidence_ref")),
    "tracker": ("tickets", ("ticket", "state", "closed_at")),
    "workers": ("rows", ("dispatch_id", "task_id", "attempt", "worktree", "worker_state")),
}
#: ``git`` also carries the local-main object ``_row_five`` checks.
MAIN_KEYS = ("branch", "head", "local_branch", "local_head", "upstream", "clean", "synced")
#: ``dashboard`` carries a gap and never evidence; ``release`` carries one verification.
DASHBOARD_KEYS = ("gap",)
PENDING_ITEM_KEYS = (
    "record_id", "pending_type", "pr", "ticket", "accepted_sha", "owner", "condition",
    "check_after", "resolved", "status", "checks",
)
PENDING_CHECKS = ("pr_merged", "merge_sha", "main_synced", "tracker_closed")
#: Every top-level key ``validated_facts`` accepts, and the required ones among them.
FACTS_TOP_KEYS = ("schema_version", "run_id", "observed_at", "answer_location",
                  *FACTS_SECTIONS)
FACTS_REQUIRED = ("schema_version", "run_id", "observed_at", "answer_location")
#: A section always carries these two; the row list is additionally required when state is ok.
SECTION_KEYS = ("state", "reason")


def _load(name, module_name):
    """Import a sibling script by path: the shared modules are reused, not copied."""
    path = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot build an import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LEDGER = _load("ledger.py", "orca_closeout_ledger")
ACCEPTANCE = _load("acceptance.py", "orca_closeout_acceptance")
HEALTH = _load("health.py", "orca_closeout_health")


def _invalid(message):
    raise ValueError(message)


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z")


def _unknown_section(reason):
    return {"state": "unknown", "reason": reason}


def schema():
    """The ``--print-schema`` document, derived from the module's own constants.

    Pure: it reads module constants only, so it performs no I/O and never touches the
    ledger, a PR host, a tracker or git.
    """
    acceptance_schema = ACCEPTANCE.schema()["record"]
    sections = {}
    for name in FACTS_SECTIONS:
        section = {
            "fields": list(SECTION_KEYS),
            "required": ["state"],
            "optional": ["reason"],
            "state_enum": list(SECTION_STATES),
            "reason": "string; the reason an unknown section gives",
        }
        rows = SECTION_ROWS.get(name)
        if rows is not None:
            section["rows_key"] = rows[0]
            section["row_fields"] = list(rows[1])
            section["rows_note"] = "read when state is ok"
        if name == "git":
            section["main_fields"] = list(MAIN_KEYS)
        elif name == "dashboard":
            section["gap_fields"] = list(DASHBOARD_KEYS)
        elif name == "release":
            section["verification_fields"] = list(RELEASE_VERIFICATION_KEYS)
        elif name == "pending":
            section["rows_key"] = "items"
            section["row_fields"] = list(PENDING_ITEM_KEYS)
            section["checks_fields"] = list(PENDING_CHECKS)
            section["one_way_door_types"] = list(ONE_WAY_DOOR_TYPES)
        elif name == "acceptance":
            section["rows_key"] = "records"
            section["row_fields"] = ["record_id", *acceptance_schema["fields"]]
            section["row_note"] = "each record is validated by acceptance.py"
            section["levels_fields"] = list(acceptance_schema["level_names"])
            section["level_object_fields"] = list(acceptance_schema["level_fields"])
            section["level_shapes"] = {
                key: list(value)
                for key, value in acceptance_schema["level_shapes"].items()}
        elif name == "observations":
            section["rows_key"] = "records"
            section["row_fields"] = ["record_id", *ACCEPTANCE.OBSERVATION_KEYS]
            section["observation_enum"] = list(OBSERVATIONS)
        sections[name] = section
    return {
        "schema_version": SCHEMA_VERSION,
        "facts": {
            "top_level_fields": list(FACTS_TOP_KEYS),
            "required": list(FACTS_REQUIRED),
            "schema_version_value": SCHEMA_VERSION,
            "timestamp_fields": ["observed_at"],
            "timestamp_format": "RFC3339 UTC instant, e.g. 2026-11-02T09:00:00Z",
            "sections": sections,
        },
        "authority_file": {
            "commands": ["reconcile"],
            "fields": list(AUTHORITY_SECTIONS),
            "note": "reconcile --authority takes a JSON object with only these keys",
        },
        "mapping": {
            "fields": list(MAPPING_KEYS),
            "required_for": "a success closeout before release",
        },
        "failure_recovery": {
            "commands": ["failure-record", "failure-plan"],
            "sequence": ["read-settled", "record-closeout", "authorize-release", "release"],
            "classifications": list(FAILURE_CLASSES),
            "next_actions": list(FAILURE_NEXT_ACTIONS),
            "mapping_fields": list(MAPPING_KEYS),
            "note": "failure-record refuses without a settled read-back; only failure-plan "
                    "emits the worker-release command, and only when authorize_release "
                    "allows it",
        },
        "enums": {
            "section_states": list(SECTION_STATES),
            "blocking_statuses": list(BLOCKING_STATUSES),
            "row_statuses": ["ok", *BLOCKING_STATUSES, "gap"],
            "closeout_outcomes": list(CLOSEOUT_OUTCOMES),
            "observations": list(OBSERVATIONS),
            "one_way_door_types": list(ONE_WAY_DOOR_TYPES),
            "reclaimable_states": list(RECLAIMABLE_STATES),
            "retained_states": list(RETAINED_STATES),
            "level_rows": {key: list(value) for key, value in LEVEL_ROWS.items()},
        },
        "rows": [dict(item) for item in ROWS],
    }


SCHEMA_HELP = ("Run with the top-level --print-schema flag to print the complete "
               "facts/authority schema: every top-level field, every section and row "
               "shape, which are required, and the known enum values.")


def _state_choices():
    return "{" + ", ".join(SECTION_STATES) + "}"


def _section(facts, name):
    """Return one facts section; a missing or malformed section is unknown, not empty."""
    value = facts.get(name)
    if not isinstance(value, dict):
        return _unknown_section(name + " not provided")
    state_key, reason_key = SECTION_KEYS
    state = value.get(state_key)
    if state not in SECTION_STATES:
        _invalid("facts." + name + "." + state_key + " must be one of "
                 + _state_choices())
    if not isinstance(value.get(reason_key, ""), str):
        _invalid("facts." + name + "." + reason_key + " must be a string")
    return value


def _rows(section, key):
    rows = section.get(key, [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        _invalid("a facts section row list must be a list of objects")
    return rows


def _evidence(source, ref, state):
    return {"source": source, "ref": ref, "state": state}


# ---------------------------------------------------------------------------
# plan: the pure checklist
# ---------------------------------------------------------------------------


def _acceptance_payload(entry):
    return {key: value for key, value in entry.items() if key != "record_id"}


def _accepted_records(facts):
    """The acceptance entries with their validation errors, in ledger order."""
    section = _section(facts, "acceptance")
    if section.get("state") != "ok":
        return section.get("state", "unknown"), section.get("reason", ""), []
    found = []
    for entry in _rows(section, "records"):
        if not isinstance(entry, dict):
            _invalid("facts.acceptance.records must hold objects")
        payload = _acceptance_payload(entry)
        errors = ACCEPTANCE.validate(payload)
        found.append({
            "record_id": entry.get("record_id", ""),
            "payload": payload,
            "errors": errors,
        })
    return "ok", "", found


def _ledger_record(item):
    """One facts acceptance entry in the ledger-record shape ``aggregate`` reads."""
    payload = item["payload"]
    return {
        "record_id": item["record_id"],
        "kind": "acceptance",
        "subject": {
            "ticket": payload.get("ticket"),
            "pr": _pr_number(payload),
            "attempt": payload.get("attempt"),
        },
        "detail": payload,
    }


def _current_claims(accepted):
    """The per-PR claims in force, exactly as ``acceptance.aggregate`` selects them.

    ``accepted`` is row 1's full audit list. A record without a ticket or a positive
    integer attempt is not a claim: row 1 already blocks on it, and promoting it here
    would invent an attempt. A same-attempt disagreement is returned separately so
    rows 2-7 never use it and row 1 can block on it; the superseded attempts stay in
    the row-1 audit and are never rewritten.
    """
    groups, order = {}, []
    for item in accepted:
        payload = item["payload"]
        ticket = payload.get("ticket")
        if not ACCEPTANCE.is_nonempty(ticket):
            continue
        if type(payload.get("attempt")) is not int or payload["attempt"] < 1:
            continue
        if ticket not in groups:
            groups[ticket] = []
            order.append(ticket)
        groups[ticket].append(item)

    claims, divergent = [], []
    for ticket in order:
        group = groups[ticket]
        aggregated = ACCEPTANCE.aggregate(
            ticket, [_ledger_record(item) for item in group])
        by_id = {item["record_id"]: item for item in group}
        for pr in aggregated["prs"]:
            current = by_id.get(pr.get("record_id"))
            claim = {
                "record_id": pr.get("record_id", ""),
                "current_record_ids": list(pr.get("current_record_ids", [])),
                "ticket": ticket,
                "pr": pr.get("number"),
                "attempt": pr.get("attempt"),
                "divergent": pr.get("divergent") is True,
                "payload": current["payload"] if current else {},
                "errors": current["errors"] if current else [],
            }
            (divergent if claim["divergent"] else claims).append(claim)
    return claims, divergent


def _level(payload, name):
    levels = payload.get("levels") if isinstance(payload, dict) else None
    level = levels.get(name) if isinstance(levels, dict) else None
    return level if isinstance(level, dict) else {}


def _recorded_merge_claim(payload):
    """The record's own merge claim, kept as recorded evidence only (SP-05).

    Only the accepted head is non-repeatable (row 1); the merge is an authority fact
    read back later. A claim is still worth keeping: when it contradicts the host it
    means the record no longer describes the merge that happened.
    """
    level = _level(payload, "merged")
    if level.get("value") is not True:
        return None
    if isinstance(level.get("equivalence"), dict):
        shape = "equivalence"
    elif isinstance(level.get("ancestry"), dict):
        shape = "ancestry"
    else:
        shape = None
    return {"merge_sha": level.get("merge_sha"), "shape": shape}


def _merge_facts(facts, claims):
    """Resolve each PR's merge facts from its own PR host row (SP-05).

    Row 2's authority is the PR host, so the state, merge SHA and base come from its
    row: a closeout re-run after a crash repairs the merge from authority instead of
    demanding that the pre-merge acceptance record have predicted its SHA. The record's
    own claim rides along as ``recorded`` evidence and is ``divergent`` only when the
    host affirmatively reports a different merge SHA or base; a missing or not-yet-
    merged host row is unproven, never a conflict.

    Rows 3 and 4 read ``merge_sha`` from here too. When the host row is absent or
    unreadable, the recorded claim is the only merge evidence left and git must still
    prove it; row 2 already blocks on the missing host row, so no proof is relaxed.
    """
    section = _section(facts, "pr_host")
    rows = _rows(section, "pull_requests") if section.get("state") == "ok" else []
    resolved = []
    for claim in claims:
        payload = claim["payload"]
        number = _pr_number(payload)
        host = next((candidate for candidate in rows
                     if ACCEPTANCE.as_int(candidate.get("pr")) == number), None)
        state = str(host.get("state", "")).upper() if host is not None else ""
        host_merge_sha = host.get("merge_sha") if host is not None else None
        if not ACCEPTANCE.is_sha(host_merge_sha):
            host_merge_sha = None
        base = host.get("base") if host is not None else None
        if not ACCEPTANCE.is_nonempty(base):
            base = None
        recorded = _recorded_merge_claim(payload)
        pr = payload.get("pr") if isinstance(payload.get("pr"), dict) else {}
        recorded_base = pr.get("base") if ACCEPTANCE.is_nonempty(pr.get("base")) else None
        recorded_sha = None
        if recorded is not None:
            recorded_sha = recorded.get("merge_sha")
            if not ACCEPTANCE.is_sha(recorded_sha):
                recorded_sha = None
        divergence = []
        if host is not None and state == "MERGED":
            for field, recorded_value, authority_value in (
                    ("merge_sha", recorded_sha, host_merge_sha),
                    ("base", recorded_base, base)):
                if recorded_value is not None and authority_value is not None \
                        and recorded_value != authority_value:
                    divergence.append({
                        "field": field,
                        "recorded": recorded_value,
                        "authority": authority_value,
                    })
        merge_sha = host_merge_sha if state == "MERGED" else None
        merge_source = "pr_host" if merge_sha is not None else ""
        if merge_sha is None and recorded_sha is not None:
            merge_sha = recorded_sha
            merge_source = "recorded"
        resolved.append({
            "record_id": claim.get("record_id", ""),
            "pr": number,
            "accepted_sha": payload.get("accepted_sha"),
            "host": host,
            "state": state,
            "host_merge_sha": host_merge_sha,
            "base": base,
            "recorded": recorded,
            "merge_sha": merge_sha,
            "merge_source": merge_source,
            "divergence": divergence,
            "divergent": bool(divergence),
        })
    return resolved


def _merge_detail(entry):
    """One resolved merge fact as rows 2-4 report it."""
    recorded = entry["recorded"] or {}
    return {
        "pr": entry["pr"],
        "accepted_sha": entry["accepted_sha"],
        "state": entry["state"],
        "merge_sha": entry["merge_sha"],
        "merge_source": entry["merge_source"],
        "base": entry["base"],
        "recorded_merge_sha": recorded.get("merge_sha"),
        "divergence": entry["divergence"],
    }


def _pr_number(payload):
    pr = payload.get("pr") if isinstance(payload, dict) else None
    return ACCEPTANCE.as_int(pr.get("number")) if isinstance(pr, dict) else None


def _row(index, status, detail=None, evidence=None):
    template = ROWS[index - 1]
    return {
        "id": template["id"],
        "fact": template["fact"],
        "authority": template["authority"],
        "repeatable": template["repeatable"],
        "action": template["action"],
        "status": status,
        "detail": detail if isinstance(detail, dict) else {},
        "evidence": list(evidence or []),
    }


def _divergence_detail(claim):
    return {
        "ticket": claim["ticket"],
        "pr": claim["pr"],
        "attempt": claim["attempt"],
        "record_ids": claim["current_record_ids"],
    }


def _row_one(run_id, state, reason, accepted, divergent, merge_divergence=(),
             current_record_ids=()):
    if state != "ok":
        return _row(1, "unknown", {"reason": reason})
    problems, entries, evidence = [], [], []
    for item in accepted:
        payload = item["payload"]
        gate_passed = ACCEPTANCE.all_checks_passed(payload.get("checks"))
        entries.append({
            "record_id": item["record_id"],
            "ticket": payload.get("ticket"),
            "pr": _pr_number(payload),
            "attempt": payload.get("attempt"),
            "accepted_sha": payload.get("accepted_sha"),
            "answer_location": payload.get("answer_location"),
            "gate_passed": gate_passed,
            "valid": not item["errors"],
        })
        evidence.append(_evidence(
            "ledger", "ledger:" + run_id + ":" + str(item["record_id"]),
            "ok" if not item["errors"] else "invalid"))
        problems.extend(
            str(item["record_id"]) + ": " + error for error in item["errors"])
        # A superseded attempt stays in ``entries`` as audit evidence; only the claim in
        # force (aggregate's ``current_record_ids``) can block the declaration.
        if not gate_passed and item["record_id"] in current_record_ids:
            problems.append(
                str(item["record_id"]) + ": a recorded check did not pass; the gate "
                "did not pass and blocks the declaration")
    for claim in divergent:
        problems.append(
            "PR " + str(claim["pr"]) + ": the acceptance records at attempt "
            + str(claim["attempt"]) + " disagree about the accepted SHA or the level "
            "values; the divergent claim is not a single claim in force")
    for entry in merge_divergence:
        fields = ", ".join(item["field"] for item in entry["divergence"])
        problems.append(
            "PR " + str(entry["pr"]) + ": the recorded merge claim is divergent from "
            "the PR host (" + fields + "); the claim in force does not describe the "
            "merge that happened")
    if not accepted:
        problems.append(
            "no acceptance record is persisted; the accepted SHA must be captured at "
            "acceptance and cannot be reconstructed later")
    status = "blocked" if problems else "ok"
    detail = {
        "entries": entries,
        "problems": problems,
        "divergent": [_divergence_detail(claim) for claim in divergent],
        "merge_divergent": [
            {"pr": entry["pr"], "accepted_sha": entry["accepted_sha"],
             "divergence": entry["divergence"]}
            for entry in merge_divergence],
    }
    return _row(1, status, detail, evidence)


def _row_two(facts, merges):
    """The PR host's MERGED state, base and merge SHA, read from each PR's row.

    The merge SHA is the host's, not the record's forecast (SP-05): the record only
    has to persist the accepted head. A recorded claim that contradicts the host is
    reported as a divergence, never averaged.
    """
    section = _section(facts, "pr_host")
    if section.get("state") != "ok":
        return _row(2, "unknown", {"reason": section.get("reason", "")})
    problems, evidence = [], []
    if not merges:
        problems.append("no accepted PR to reconcile against the PR host")
    for entry in merges:
        number = entry["pr"]
        accepted_sha = entry["accepted_sha"]
        if not ACCEPTANCE.is_sha(accepted_sha):
            problems.append("PR " + str(number) + ": the accepted SHA is missing")
        if entry["host"] is None:
            problems.append("PR host has no record for PR " + str(number))
            continue
        state = entry["state"]
        evidence.append(_evidence(
            "pr_host", "pr_host:pr:" + str(number), state or "unknown"))
        if state != "MERGED":
            problems.append(
                "PR host reports " + repr(state or None) + " for PR " + str(number)
                + "; a closeout needs MERGED")
        if not ACCEPTANCE.is_sha(entry["host_merge_sha"]):
            problems.append(
                "PR " + str(number) + ": the PR host reports no merge SHA, so the "
                "merge is not proven")
        if not ACCEPTANCE.is_nonempty(entry["base"]):
            problems.append(
                "PR " + str(number) + ": the PR host reports no base, so the merge "
                "target is not proven")
        for divergence in entry["divergence"]:
            problems.append(
                "PR " + str(number) + ": the recorded " + divergence["field"]
                + " " + repr(divergence["recorded"]) + " contradicts the PR host "
                + repr(divergence["authority"]) + "; divergent")
    status = "blocked" if problems else "ok"
    detail = {
        "problems": problems,
        "merges": [_merge_detail(entry) for entry in merges],
        "divergence": [item for entry in merges for item in entry["divergence"]],
    }
    return _row(2, status, detail, evidence)


def _ancestry_covers(ancestry, ancestor, descendant):
    return any(
        row.get("ancestor") == ancestor
        and row.get("descendant") == descendant
        and row.get("result") is True
        for row in ancestry)


def _equivalence_covers(merges, accepted_sha, merge_sha):
    return any(
        row.get("accepted_sha") == accepted_sha
        and row.get("merge_sha") == merge_sha
        and row.get("diff_empty") is True
        for row in merges)


def _row_three(facts, merges):
    """The merge covers the accepted head: by ancestry, or by content equivalence.

    A merge commit makes the accepted head an ancestor of the merge SHA. A squash or
    rebase merge rewrites it, so row 3 accepts the record's ``equivalence`` shape and
    demands git's own ``merges`` row, never the record's word for it. The merge SHA is
    the host's when it has one, so a pre-merge acceptance recovers here (SP-05).
    """
    section = _section(facts, "git")
    if section.get("state") != "ok":
        return _row(3, "unknown", {"reason": section.get("reason", "")})
    ancestry = _rows(section, "ancestry")
    record_merges = _rows(section, "merges")
    problems, evidence = [], []
    for entry in merges:
        accepted_sha = entry["accepted_sha"]
        merge_sha = entry["merge_sha"]
        if entry["divergent"]:
            problems.append(
                "PR " + str(entry["pr"]) + ": the merge facts are divergent, so no "
                "single merge can be shown to cover the accepted head")
            continue
        if not ACCEPTANCE.is_sha(accepted_sha) or not ACCEPTANCE.is_sha(merge_sha):
            problems.append(
                "PR " + str(entry["pr"]) + ": the accepted SHA and merge SHA "
                "are needed before the merge can be shown to cover the accepted head")
            continue
        recorded = entry["recorded"] or {}
        if recorded.get("shape") == "equivalence":
            ref = "git:merges:" + accepted_sha + ".." + merge_sha
            covered = _equivalence_covers(record_merges, accepted_sha, merge_sha)
            evidence.append(_evidence("git", ref, "equivalent" if covered else "unknown"))
            if not covered:
                problems.append(
                    "git has no equivalence proof for the accepted head "
                    + accepted_sha[:12] + " and the merge " + merge_sha[:12]
                    + ": the accepted record uses the equivalence shape, so a "
                    "git.merges row with diff_empty true is required")
            continue
        ref = "git:ancestry:" + accepted_sha + ".." + merge_sha
        covered = _ancestry_covers(ancestry, accepted_sha, merge_sha)
        evidence.append(_evidence("git", ref, "merged" if covered else "not_merged"))
        if not covered:
            problems.append(
                "git ancestry does not prove the accepted head " + accepted_sha[:12]
                + " is an ancestor of the merge " + merge_sha[:12])
    if not merges:
        problems.append("no accepted PR to check the merge coverage for")
    status = "blocked" if problems else "ok"
    detail = {
        "problems": problems,
        "escalate": bool(problems),
        "merge_divergent": any(entry["divergent"] for entry in merges),
    }
    return _row(3, status, detail, evidence)


def _row_four(facts, merges):
    section = _section(facts, "git")
    if section.get("state") != "ok":
        return _row(4, "unknown", {"reason": section.get("reason", "")})
    ancestry = _rows(section, "ancestry")
    main = section.get("main") if isinstance(section.get("main"), dict) else {}
    head = main.get("head")
    problems, evidence = [], []
    if not ACCEPTANCE.is_sha(head):
        problems.append("git.main.head is missing, so main cannot be checked")
    for entry in merges:
        merge_sha = entry["merge_sha"]
        if entry["divergent"]:
            problems.append(
                "PR " + str(entry["pr"]) + ": the merge facts are divergent, so main "
                "cannot be shown to contain the merge")
            continue
        if not ACCEPTANCE.is_sha(merge_sha) or not ACCEPTANCE.is_sha(head):
            problems.append(
                "PR " + str(entry["pr"]) + ": the merge SHA and main head are "
                "needed before containment can be checked")
            continue
        ref = "git:ancestry:" + merge_sha + ".." + head
        contained = _ancestry_covers(ancestry, merge_sha, head)
        evidence.append(_evidence("git", ref, "contained" if contained else "missing"))
        if not contained:
            problems.append(
                "main does not contain the merge " + merge_sha[:12] + " up to "
                + head[:12])
    if not merges:
        problems.append("no accepted PR to check containment for")
    status = "blocked" if problems else "ok"
    detail = {
        "problems": problems,
        "escalate": bool(problems),
        "merge_divergent": any(entry["divergent"] for entry in merges),
    }
    return _row(4, status, detail, evidence)


def _row_five(facts):
    section = _section(facts, "git")
    if section.get("state") != "ok":
        return _row(5, "unknown", {"reason": section.get("reason", "")})
    main = section.get("main")
    if not isinstance(main, dict):
        return _row(5, "blocked", {
            "problems": ["local main state was not provided"],
            "action": "pause later merges until local main can be proven synced",
        })
    problems = []
    if main.get("local_branch") != main.get("branch"):
        problems.append(
            "the local working copy sits on " + repr(main.get("local_branch"))
            + ", not on " + repr(main.get("branch")))
    if main.get("clean") is not True:
        problems.append("the local main working tree or index is not clean")
    if not ACCEPTANCE.is_nonempty(main.get("upstream")):
        problems.append("the upstream is not explicit")
    if main.get("synced") is not True or main.get("local_head") != main.get("head"):
        problems.append(
            "local main is not synced to the remote: local "
            + str(main.get("local_head"))[:12] + " vs remote "
            + str(main.get("head"))[:12])
    evidence = [_evidence(
        "git", "git:main:" + str(main.get("head", "")),
        "synced" if not problems else "unsynced")]
    detail = {
        "problems": problems,
        "action": "pause later merges until local main is synced",
    }
    return _row(5, "blocked" if problems else "ok", detail, evidence)


def _row_six(facts, accepted):
    section = _section(facts, "tickets")
    if section.get("state") != "ok":
        return _row(6, "unknown", {"reason": section.get("reason", "")})
    rows = _rows(section, "tickets")
    tickets = sorted({item["payload"].get("ticket") for item in accepted
                      if ACCEPTANCE.is_nonempty(item["payload"].get("ticket"))})
    problems, evidence, entries = [], [], []
    if not tickets:
        problems.append("no accepted ticket to reconcile against the integration branch")
    for ticket in tickets:
        row = next((candidate for candidate in rows
                    if candidate.get("ticket") == ticket), None)
        integrated = bool(row and row.get("integrated") is True)
        entries.append({"ticket": ticket, "integrated": integrated,
                        "evidence_ref": row.get("evidence_ref", "") if row else ""})
        evidence.append(_evidence(
            "git", "git:integration:" + ticket,
            "integrated" if integrated else "not_integrated"))
        if not integrated:
            problems.append("ticket " + ticket + " is not integrated")
    status = "blocked" if problems else "ok"
    return _row(6, status, {"tickets": entries, "problems": problems}, evidence)


def _row_seven(facts, accepted):
    section = _section(facts, "tracker")
    if section.get("state") != "ok":
        return _row(7, "unknown", {"reason": section.get("reason", "")})
    rows = _rows(section, "tickets")
    tickets = sorted({item["payload"].get("ticket") for item in accepted
                      if ACCEPTANCE.is_nonempty(item["payload"].get("ticket"))})
    problems, evidence, entries = [], [], []
    if not tickets:
        problems.append("no accepted ticket to read back from the tracker")
    for ticket in tickets:
        row = next((candidate for candidate in rows
                    if candidate.get("ticket") == ticket), None)
        state = row.get("state") if row else "absent"
        entries.append({"ticket": ticket, "state": state,
                        "closed_at": row.get("closed_at") if row else None})
        evidence.append(_evidence(
            "tracker", "tracker:ticket:" + ticket, str(state)))
        if state != "closed":
            problems.append(
                "tracker reports " + repr(state) + " for ticket " + ticket
                + "; an exception escalates instead of an auto-close")
    status = "blocked" if problems else "ok"
    detail = {"tickets": entries, "problems": problems}
    if problems:
        detail["escalate"] = True
    return _row(7, status, detail, evidence)


def _row_eight(facts):
    section = _section(facts, "workers")
    if section.get("state") != "ok":
        return _row(8, "unknown", {"reason": section.get("reason", "")})
    rows = _rows(section, "rows")
    reclaimable, retained, read_back, evidence = [], [], [], []
    for row in rows:
        state = row.get("worker_state")
        if state in RETAINED_STATES:
            retained.append({"dispatch_id": row.get("dispatch_id", ""),
                             "worker_state": state})
            continue
        if state not in RECLAIMABLE_STATES:
            continue
        entry = {"dispatch_id": row.get("dispatch_id", ""), "worker_state": state}
        reclaimable.append(entry)
        evidence.append(_evidence(
            "orca", "orca:worker:" + str(row.get("dispatch_id", "")), str(state)))
        if state == "release_unknown" and row.get("dispatch_id"):
            read_back.append(row["dispatch_id"])
    detail = {
        "rows": len(rows),
        "reclaimable": reclaimable,
        "retained": retained,
        "read_back_required": read_back,
    }
    return _row(8, "blocked" if reclaimable else "ok", detail, evidence)


def _row_nine(facts):
    section = _section(facts, "dashboard")
    gap = section.get(DASHBOARD_KEYS[0])
    detail = {
        "authoritative": False,
        "gap": gap if isinstance(gap, dict) else None,
        "reason": section.get("reason", ""),
        "note": "the dashboard projection is a gap only and is never evidence",
    }
    return _row(9, "gap", detail, [])


def _ticket_for_pr(accepted, pr):
    for item in accepted:
        if _pr_number(item["payload"]) == pr:
            return item["payload"].get("ticket")
    return None


def _pending_checks(facts, item):
    """The four checks a pending one-way door is reconciled against."""
    pr = ACCEPTANCE.as_int(item.get("pr"))
    accepted_sha = item.get("accepted_sha")
    pr_section = _section(facts, "pr_host")
    git_section = _section(facts, "git")
    tracker_section = _section(facts, "tracker")
    pr_row = None
    if pr_section.get("state") == "ok":
        pr_row = next((row for row in _rows(pr_section, "pull_requests")
                       if ACCEPTANCE.as_int(row.get("pr")) == pr), None)
    merged = bool(pr_row and str(pr_row.get("state", "")).upper() == "MERGED")
    merge_sha = bool(pr_row and ACCEPTANCE.is_sha(pr_row.get("merge_sha")))
    main = git_section.get("main") if isinstance(git_section.get("main"), dict) else {}
    synced = bool(
        git_section.get("state") == "ok" and main.get("synced") is True
        and main.get("head") == main.get("local_head"))
    ticket = item.get("ticket")
    closed = False
    if tracker_section.get("state") == "ok" and ACCEPTANCE.is_nonempty(ticket):
        tracker_row = next((row for row in _rows(tracker_section, "tickets")
                            if row.get("ticket") == ticket), None)
        closed = bool(tracker_row and tracker_row.get("state") == "closed")
    checks = {
        "pr_merged": merged,
        "merge_sha": merge_sha,
        "main_synced": synced,
        "tracker_closed": closed,
    }
    return {name: checks[name] for name in PENDING_CHECKS}


def _row_ten(facts, accepted, observed_at):
    section = _section(facts, "pending")
    if section.get("state") != "ok":
        return _row(10, "unknown", {"reason": section.get("reason", "")})
    entries, evidence, open_items = [], [], []
    for item in _rows(section, "items"):
        record_id = item.get("record_id", "")
        pr = ACCEPTANCE.as_int(item.get("pr"))
        pending_type = item.get("pending_type") or "pending"
        resolved = bool(item.get("resolved")) or item.get("status") == "resolved"
        check_after = item.get("check_after")
        due = True
        if ACCEPTANCE.is_utc_instant(check_after) and ACCEPTANCE.is_utc_instant(observed_at):
            due = check_after <= observed_at
        entry = {
            "record_id": record_id,
            "pending_type": pending_type,
            "pr": pr,
            "owner": item.get("owner", "human"),
            "condition": item.get("condition", ""),
            "due": due,
            "one_way_door": pending_type in ONE_WAY_DOOR_TYPES,
            "resolved": resolved,
        }
        if entry["one_way_door"]:
            checks = item.get("checks") if isinstance(item.get("checks"), dict) else None
            if checks is None:
                checks = _pending_checks(facts, {
                    "ticket": item.get("ticket") or _ticket_for_pr(accepted, pr),
                    "pr": pr, "accepted_sha": item.get("accepted_sha"),
                })
            entry["checks"] = {key: checks.get(key) is True
                               for key in ("pr_merged", "merge_sha", "main_synced",
                                           "tracker_closed")}
            entry["reconcilable"] = all(entry["checks"].values())
        entries.append(entry)
        evidence.append(_evidence(
            "ledger", "ledger:" + facts.get("run_id", "") + ":" + str(record_id),
            "pending"))
        if entry["one_way_door"] and not resolved:
            open_items.append(entry)
    detail = {"items": entries}
    return _row(10, "pending" if open_items else "ok", detail, evidence)


def _apply_observations(rows, accepted, observations):
    """Consume the revert/reopen observations T09 appended; never append a new one."""
    applied = {"revert": None, "reopen": None}
    accepted_prs = {_pr_number(item["payload"]) for item in accepted}
    for observation in observations:
        level = observation.get("level")
        pr = ACCEPTANCE.as_int(observation.get("pr"))
        tag = observation.get("observation")
        if tag not in OBSERVATIONS or level not in LEVEL_ROWS:
            continue
        if accepted_prs and pr not in accepted_prs and pr is not None:
            continue
        for index in LEVEL_ROWS[level]:
            item = rows[index - 1]
            if item["status"] == "unknown":
                continue
            item["status"] = "blocked"
            item["detail"].setdefault("problems", []).append(
                "a " + str(tag) + " observation lowers the " + str(level)
                + " level: " + str(observation.get("reason", "")))
            item["detail"]["escalate"] = True
            if tag == "revert":
                item["detail"]["reverted"] = observation
            else:
                item["detail"]["reopened"] = observation
            applied[tag] = observation
    return applied


def _classification(rows, pending_open, applied, divergent=False):
    blocking = [item for item in rows if item["status"] in BLOCKING_STATUSES
                and item["id"] != 9]
    if not blocking:
        return "ready"
    if divergent:
        return "acceptance_divergent"
    if applied.get("revert"):
        return "merge_reverted"
    if applied.get("reopen"):
        return "tracker_reopened"
    ids = [item["id"] for item in blocking]
    if pending_open and set(ids) <= set(PENDING_EXPLAINED_ROWS):
        return "pending_one_way_door"
    first = blocking[0]
    if first["status"] == "unknown":
        return "authority_unknown"
    return {
        1: "acceptance_missing",
        2: "pr_not_merged",
        3: "ancestry_divergent",
        4: "main_missing_merge",
        5: "local_main_unsynced",
        6: "ticket_not_integrated",
        7: "tracker_unclosed",
        8: "workers_reclaimable",
        10: "pending_one_way_door",
    }[first["id"]]


def _release_verification(facts):
    section = _section(facts, "release")
    if section.get("state") != "ok":
        return None
    verification = section.get("verification")
    if not isinstance(verification, dict):
        _invalid("facts.release.verification is required when the release section is ok; "
                 "it must be {" + ", ".join(RELEASE_VERIFICATION_KEYS) + "}, e.g. "
                 "{source: <where it was verified>, at: <RFC3339 UTC instant>, "
                 "version_scope: <what version range it covers>}")
    unknown = sorted(set(verification) - set(RELEASE_VERIFICATION_KEYS))
    if unknown:
        _invalid("facts.release.verification has unknown fields: " + ", ".join(unknown)
                 + "; it must be {" + ", ".join(RELEASE_VERIFICATION_KEYS) + "}")
    if not ACCEPTANCE.is_nonempty(verification.get("source")):
        _invalid("facts.release.verification.source must name the verification source, "
                 "e.g. worker-release --help")
    if not ACCEPTANCE.is_utc_instant(verification.get("at")):
        _invalid("facts.release.verification.at must be an RFC3339 UTC instant, "
                 "e.g. 2026-11-02T09:00:00Z")
    if not isinstance(verification.get("version_scope"), str):
        _invalid("facts.release.verification.version_scope must be a string, e.g. "
                 "\"orchestra-auto <version>\"")
    return {
        "source": verification["source"],
        "at": verification["at"],
        "version_scope": verification["version_scope"],
    }


def validated_facts(facts):
    """Return a shallow normalized copy or raise ``ValueError``.

    The facts object is a local contract: every top-level key must be documented, every
    section carries ``state`` and ``reason``, and a non-repeatable fact (row 1) can only
    come from the ledger. Nothing here reads a clock or touches authority.
    """
    if not isinstance(facts, dict):
        _invalid("facts must be a JSON object")
    unknown = sorted(set(facts) - set(FACTS_TOP_KEYS))
    if unknown:
        _invalid("unknown facts fields: " + ", ".join(unknown) + "; facts take {"
                 + ", ".join(FACTS_TOP_KEYS) + "}; run --print-schema for the field table")
    if facts.get("schema_version") != SCHEMA_VERSION:
        _invalid("facts.schema_version must be 1")
    if not ACCEPTANCE.is_nonempty(facts.get("run_id")):
        _invalid("facts.run_id must be a non-empty string")
    if not ACCEPTANCE.is_utc_instant(facts.get("observed_at")):
        _invalid("facts.observed_at must be an RFC3339 UTC instant")
    if not ACCEPTANCE.is_nonempty(facts.get("answer_location")):
        _invalid("facts.answer_location must name where the Answer lives")
    normalized = dict(facts)
    for name in FACTS_SECTIONS:
        normalized[name] = copy.deepcopy(_section(facts, name))
    _release_verification(normalized)
    return normalized


def plan(facts):
    """Return the ten-row checklist for one facts object; a pure function.

    Every row names its authority, whether it is repeatable and the action it calls for.
    A fact that cannot be read is ``unknown``, never a silent pass; row 9 can only ever be
    a gap, because ``progress.json`` is a projection and not evidence.
    """
    normalized = validated_facts(facts)
    run_id = normalized["run_id"]
    observed_at = normalized["observed_at"]

    state, reason, accepted = _accepted_records(normalized)
    claims, divergent = _current_claims(accepted)
    current_record_ids = {record_id for claim in claims + divergent
                          for record_id in claim["current_record_ids"]}
    merges = _merge_facts(normalized, claims)
    merge_divergent = [entry for entry in merges if entry["divergent"]]
    rows = [_row_one(run_id, state, reason, accepted, divergent, merge_divergent,
                     current_record_ids)]
    rows.append(_row_two(normalized, merges))
    rows.append(_row_three(normalized, merges))
    rows.append(_row_four(normalized, merges))
    rows.append(_row_five(normalized))
    rows.append(_row_six(normalized, claims))
    rows.append(_row_seven(normalized, claims))
    rows.append(_row_eight(normalized))
    rows.append(_row_nine(normalized))

    observations_section = _section(normalized, "observations")
    observations = (_rows(observations_section, "records")
                    if observations_section.get("state") == "ok" else [])
    applied = _apply_observations(rows, accepted, observations)

    ten = _row_ten(normalized, accepted, observed_at)
    rows.append(ten)
    pending_open = any(
        entry["one_way_door"] and not entry["resolved"]
        for entry in ten["detail"].get("items", []))

    blocking = [item["id"] for item in rows
                if item["status"] in BLOCKING_STATUSES and item["id"] != 9]
    checklist = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "observed_at": observed_at,
        "answer_location": normalized["answer_location"],
        "rows": rows,
        "ready": not blocking,
        "blocking": blocking,
        "divergent": bool(divergent) or bool(merge_divergent),
        "classification": _classification(
            rows, pending_open, applied,
            bool(divergent) or bool(merge_divergent)),
        # Row 5 pauses later merges on a failed *or* unreadable local-main proof:
        # §6.5.1 preserves the scene when a sync condition cannot be shown to hold.
        "merge_blocked": rows[4]["status"] in ("blocked", "unknown"),
        "non_repeatable": list(NON_REPEATABLE),
        "release_verification": _release_verification(normalized),
    }
    return checklist


def release_decision(closeout, health_state, settled=None, read_back=None,
                     release_state="", decided_by=None):
    """Decide whether a worker may be released; health never decides it alone.

    Release needs a settled Dispatch and a durable closeout record: on success at least
    the acceptance records and the dispatch -> task -> attempt -> worktree mapping, on
    failure at least a classification and evidence references. ``release_unknown`` reads
    ``worker-show``/``worker-list`` first and never releases an unsettled worker. The
    verification source and its version scope are reported, never asserted as a stable
    contract: release replay semantics beyond the CLI help are an assumption.
    """
    if release_state not in ("", "release_unknown"):
        _invalid("release_state must be '' or 'release_unknown'")
    record = closeout if isinstance(closeout, dict) else None
    record_id = record.get("record_id", "") if record else ""
    health = HEALTH.authorize_release(
        health_state, settled=settled, closeout_record=record_id, decided_by=decided_by)
    decision = {
        "action": "release",
        "allowed": False,
        "health_authorized": False,
        "settled": settled,
        "closeout_record": record_id,
        "decided_by": health["decided_by"],
        "reason": "",
        "required_reads": [],
        "version_scope": "",
        "provisional": True,
        "read_back": (dict(read_back, settled=settled)
                      if isinstance(read_back, dict) else None),
        "health": health,
    }

    if release_state == "release_unknown":
        decision["required_reads"] = ["worker-show", "worker-list"]
        if not isinstance(read_back, dict):
            decision["reason"] = (
                "release_unknown: read worker-show and worker-list before releasing; "
                "an unsettled worker is never released")
            decision["decided_by"] = "coordinator"
            return decision
        settled = _settled_from(read_back)
        decision["settled"] = settled
        decision["read_back"] = dict(read_back, settled=settled)
        if settled is not True:
            decision["reason"] = (
                "the worker is not proven settled; release waits for settlement")
            return decision
        health = HEALTH.authorize_release(
            health_state, settled=True, closeout_record=record_id,
            decided_by=decided_by)
        decision["health"] = health
        decision["decided_by"] = health["decided_by"]

    if settled is None:
        decision["reason"] = health["reason"]
        decision["decided_by"] = "human"
        return decision
    if settled is not True:
        decision["reason"] = "the worker is not proven settled; release waits for settlement"
        return decision

    if record is None:
        decision["reason"] = "release needs a durable closeout record"
        return decision
    if record.get("durable") is not True:
        decision["reason"] = (
            "the closeout record is not durable; release and the declaration wait until "
            "the write is repaired")
        return decision

    verification = record.get("release_verification")
    if not isinstance(verification, dict) or not ACCEPTANCE.is_nonempty(verification.get("source")) \
            or not ACCEPTANCE.is_utc_instant(verification.get("at")):
        decision["reason"] = (
            "the closeout record carries no release verification source and timestamp")
        return decision
    decision["version_scope"] = verification.get("version_scope", "")

    outcome = record.get("outcome")
    if outcome not in CLOSEOUT_OUTCOMES:
        _invalid("closeout.outcome must be one of " + ", ".join(CLOSEOUT_OUTCOMES))
    if outcome == "pending":
        decision["decided_by"] = "human"
        decision["reason"] = (
            "the closeout is pending a one-way door; the human decides the next turn")
        return decision
    if outcome == "success":
        if not record.get("acceptance_record_ids"):
            decision["reason"] = (
                "a success closeout needs the acceptance records and the "
                "dispatch -> task -> attempt -> worktree mapping")
            return decision
        if not _valid_mapping(record.get("mapping")):
            decision["reason"] = (
                "a success closeout needs the complete dispatch -> task -> attempt -> "
                "worktree mapping before release")
            return decision
    else:
        if not ACCEPTANCE.is_nonempty(record.get("classification")):
            decision["reason"] = (
                "a failure closeout needs its classification and evidence references")
            return decision
        if not record.get("evidence_refs"):
            decision["reason"] = (
                "a failure closeout needs its evidence references before release")
            return decision

    decision["allowed"] = bool(health["allowed"])
    decision["reason"] = health["reason"] if decision["allowed"] else (
        "settlement and the closeout record do not yet authorize the release")
    return decision


def _settled_from(read_back, dispatch_id=None):
    """The settled flag of a ``worker-show`` read-back, bound to one Dispatch when asked.

    A settled worker proves only its own Dispatch settled. When ``dispatch_id`` is given,
    a read-back that names another Dispatch (or names none) proves nothing about this
    one, so the recovery cannot borrow a healthy worker's settlement for a failed
    Dispatch's release.
    """
    if not isinstance(read_back, dict) or read_back.get("state") != "ok":
        return None
    worker = read_back.get("worker")
    if not isinstance(worker, dict):
        return None
    if dispatch_id is not None:
        recorded = worker.get("dispatch_id")
        if not ACCEPTANCE.is_nonempty(recorded) or recorded != dispatch_id:
            return None
    settled = worker.get("settled")
    return settled if isinstance(settled, bool) else None


def _valid_mapping(mapping):
    if not isinstance(mapping, list) or not mapping:
        return False
    for entry in mapping:
        if not isinstance(entry, dict):
            return False
        if any(key not in entry for key in MAPPING_KEYS):
            return False
        if not ACCEPTANCE.is_nonempty(entry.get("dispatch_id")) or not ACCEPTANCE.is_nonempty(entry.get("task_id")):
            return False
        if type(entry.get("attempt")) is not int or entry["attempt"] < 1:
            return False
        if not ACCEPTANCE.is_nonempty(entry.get("worktree")):
            return False
    return True


# ---------------------------------------------------------------------------
# Failed-dispatch recovery: settled proof -> durable closeout -> authorize -> release
# ---------------------------------------------------------------------------


def _failure_mapping(dispatch):
    """Validate the dispatch -> task -> attempt -> worktree mapping of one failure."""
    if not isinstance(dispatch, dict):
        _invalid("failure recovery needs a dispatch mapping object with "
                 + ", ".join(MAPPING_KEYS))
    unknown = sorted(set(dispatch) - set(MAPPING_KEYS))
    if unknown:
        _invalid("unknown dispatch mapping fields: " + ", ".join(unknown)
                 + "; the mapping takes {" + ", ".join(MAPPING_KEYS) + "}")
    mapping = {key: dispatch.get(key) for key in MAPPING_KEYS}
    if not _valid_mapping([mapping]):
        _invalid("the dispatch mapping needs a non-empty dispatch_id, task_id and "
                 "worktree, and an attempt >= 1")
    return mapping


def _failure_evidence(evidence_refs):
    """One or more evidence references, deduplicated and ordered, or ``ValueError``."""
    if not isinstance(evidence_refs, (list, tuple)) or not evidence_refs:
        _invalid("a failure closeout needs at least one evidence reference")
    refs = []
    for ref in evidence_refs:
        if not ACCEPTANCE.is_nonempty(ref):
            _invalid("every failure evidence reference must be a non-empty string")
        refs.append(ref)
    return sorted(set(refs))


def _failure_verification(verification):
    if verification is None:
        verification = {}
    if not isinstance(verification, dict):
        _invalid("release_verification must be an object")
    unknown = sorted(set(verification) - set(RELEASE_VERIFICATION_KEYS))
    if unknown:
        _invalid("release_verification has unknown fields: " + ", ".join(unknown))
    source = verification.get("source") or FAILURE_VERIFICATION_SOURCE
    at = verification.get("at") or _now()
    version_scope = verification.get("version_scope") or FAILURE_VERSION_SCOPE
    if not ACCEPTANCE.is_nonempty(source):
        _invalid("release_verification.source must name the verification source")
    if not ACCEPTANCE.is_utc_instant(at):
        _invalid("release_verification.at must be an RFC3339 UTC instant")
    if not isinstance(version_scope, str):
        _invalid("release_verification.version_scope must be a string")
    return {"source": source, "at": at, "version_scope": version_scope}


def _quoted(value):
    return '"' + str(value).replace('"', '\\"') + '"'


def _settled_commands(dispatch_id):
    return [
        "<ORCA> orchestration worker-show --dispatch " + _quoted(dispatch_id) + " --json",
        "<ORCA> orchestration worker-list --json",
    ]


def _failure_plan_command(run_id, dispatch_id):
    return ("python3 scripts/closeout.py failure-plan --run-id " + _quoted(run_id)
            + " --dispatch-id " + _quoted(dispatch_id)
            + " --read-back <worker-show.json>")


def _failure_record_command(run_id, mapping, classification, evidence_refs,
                            answer_location, read_back_path):
    parts = [
        "python3", "scripts/closeout.py", "failure-record",
        "--run-id", _quoted(run_id),
        "--dispatch-id", _quoted(mapping["dispatch_id"]),
        "--task-id", _quoted(mapping["task_id"]),
        "--attempt", str(mapping["attempt"]),
        "--worktree", _quoted(mapping["worktree"]),
        "--classification", classification,
        "--answer-location", _quoted(answer_location),
        "--read-back", _quoted(read_back_path or "<worker-show.json>"),
    ]
    for ref in evidence_refs:
        parts.extend(["--evidence-ref", _quoted(ref)])
    return " ".join(parts)


def _failure_record_template(run_id, dispatch_id):
    return ("python3 scripts/closeout.py failure-record --run-id " + _quoted(run_id)
            + " --dispatch-id " + _quoted(dispatch_id)
            + " --task-id <task_id> --attempt <attempt> --worktree <worktree>"
            + " --classification <classification> --evidence-ref <evidence_ref>"
            + " --answer-location <answer_location> --read-back <worker-show.json>")


def _planned_record_command(run_id, dispatch_id, dispatch, classification,
                            evidence_refs, answer_location, read_back_path):
    """The next ``failure-record`` command: exact when the inputs are known, else a template."""
    try:
        mapping = _failure_mapping(dispatch)
        evidence = _failure_evidence(evidence_refs)
    except ValueError:
        return _failure_record_template(run_id, dispatch_id)
    if classification not in FAILURE_CLASSES or not ACCEPTANCE.is_nonempty(answer_location):
        return _failure_record_template(run_id, dispatch_id)
    return _failure_record_command(
        run_id, mapping, classification, evidence, answer_location, read_back_path)


def _failure_closeout(records, dispatch_id):
    """The latest durable failure closeout that names this Dispatch, or ``None``.

    Matching on ``failure_recovery.dispatch_id``, not merely on the worker mapping, keeps
    a checklist closeout for the same Run from authorizing a release the recovery never
    classified.
    """
    for record in reversed(records):
        if record.get("kind") != "closeout":
            continue
        detail = record.get("detail")
        if not isinstance(detail, dict) or detail.get("outcome") != "failure":
            continue
        recovery = detail.get("failure_recovery")
        if not isinstance(recovery, dict):
            continue
        if recovery.get("dispatch_id") != dispatch_id:
            continue
        if recovery.get("settled") is not True:
            continue
        mapping = detail.get("mapping")
        if not isinstance(mapping, list) or not any(
                isinstance(entry, dict) and entry.get("dispatch_id") == dispatch_id
                for entry in mapping):
            continue
        return record
    return None


def failure_record(run_id, read_back, dispatch, classification, evidence_refs,
                   home=None, answer_location="", release_verification=None):
    """Persist one durable failure closeout, but only after settlement is proven.

    The order is enforced here, not in prose: ``read_back`` (the ``worker-show`` output)
    must prove ``settled: true`` before anything is written, and the record carries the
    failure classification, its evidence references and the dispatch -> task -> attempt ->
    worktree mapping. The returned ``next_action`` is ``authorize-release``; only
    ``failure-plan`` may emit the release command. An undurable write is reported as
    ``recorded: false, durable: false`` and never as a closeout.
    """
    if not ACCEPTANCE.is_nonempty(run_id):
        _invalid("failure_record needs a non-empty run_id")
    if not ACCEPTANCE.is_nonempty(answer_location):
        _invalid("failure_record needs the Answer location for the closeout record")
    if classification not in FAILURE_CLASSES:
        _invalid("failure classification must be one of " + ", ".join(FAILURE_CLASSES))
    mapping = _failure_mapping(dispatch)
    evidence = _failure_evidence(evidence_refs)
    verification = _failure_verification(release_verification)
    settled = _settled_from(read_back, mapping["dispatch_id"])
    readable = isinstance(read_back, dict) and read_back.get("state") == "ok"
    result = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "dispatch_id": mapping["dispatch_id"],
        "record_id": "",
        "settled": settled,
        "recorded": False,
        "appended": False,
        "durable": False,
        "next_action": "",
        "reason": "",
        "closeout": None,
        "retry_path": "",
        "commands": [],
    }
    if settled is not True:
        if readable:
            result["next_action"] = "escalate"
            result["reason"] = (
                "the read-back does not prove the Dispatch settled; release never turns "
                "an unsettled Dispatch into a settled one")
        else:
            result["next_action"] = "read-settled"
            result["reason"] = (
                "read the Dispatch back with worker-show and worker-list first; the "
                "settlement proof is the first recovery step")
            result["commands"] = _settled_commands(mapping["dispatch_id"])
        return result

    observed_at = _now()
    identity = {
        "run_id": run_id,
        "dispatch": mapping,
        "classification": classification,
        "evidence_refs": evidence,
        "answer_location": answer_location,
        "release_verification": {
            "source": verification["source"],
            "version_scope": verification["version_scope"],
        },
    }
    record_id = str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        "orca-closeout-failure:" + LEDGER.canonical_json(identity)))
    result["record_id"] = record_id
    records, unknown = LEDGER.effective_records(run_id, home=home)
    if unknown is not None:
        result["next_action"] = "escalate"
        result["reason"] = "the ledger history is incomplete: " + unknown["reason"]
        return result
    existing = next(
        (item for item in records if item.get("record_id") == record_id), None)
    if existing is not None:
        result["recorded"] = True
        result["durable"] = True
        result["next_action"] = "authorize-release"
        result["reason"] = "the same failure closeout is already durable"
        result["closeout"] = _summary(existing, False, True)
        result["commands"] = [_failure_plan_command(run_id, mapping["dispatch_id"])]
        return result

    worker = read_back.get("worker") if isinstance(read_back.get("worker"), dict) else {}
    detail = {
        "outcome": "failure",
        "classification": classification,
        "answer_location": answer_location,
        "acceptance_record_ids": [],
        "mapping": [mapping],
        "evidence_refs": evidence,
        "release_verification": verification,
        "failure_recovery": {
            "dispatch_id": mapping["dispatch_id"],
            "settled": True,
            "read_back": {
                "source": "worker-show",
                "state": read_back.get("state"),
                "worker_state": worker.get("worker_state", ""),
                "settled": True,
            },
        },
        "observed_at": observed_at,
    }
    record = {
        "schema_version": LEDGER.SCHEMA_VERSION,
        "record_id": record_id,
        "kind": "closeout",
        "run_id": run_id,
        "subject": {
            "dispatch_id": mapping["dispatch_id"],
            "task_id": mapping["task_id"],
            "attempt": mapping["attempt"],
        },
        "action": "closeout",
        "request_id": "",
        "idempotency": "none",
        "state": "failed",
        "evidence_ref": answer_location[:2048],
        "observed_at": observed_at,
        "actor": "coordinator",
        "detail": detail,
    }
    try:
        LEDGER.append(record, home=home)
    except (OSError, TypeError, ValueError) as error:
        path = ACCEPTANCE.pending_path(
            LEDGER.home_path(home), run_id, record_id, PENDING_DIRNAME)
        ACCEPTANCE.write_pending(path, record)
        result["next_action"] = "record-closeout"
        result["reason"] = (
            "the failure closeout could not be persisted; release stays refused until "
            "the durable write is repaired")
        result["retry_path"] = str(path)
        result["record_error"] = str(error)
        result["closeout"] = _summary(record, False, False, str(path))
        return result
    result["recorded"] = True
    result["appended"] = True
    result["durable"] = True
    result["next_action"] = "authorize-release"
    result["reason"] = (
        "the failure closeout is durable; ask failure-plan to authorize the release")
    result["closeout"] = _summary(record, True, True)
    result["commands"] = [_failure_plan_command(run_id, mapping["dispatch_id"])]
    return result


def failure_plan(run_id, dispatch_id, read_back, home=None, dispatch=None,
                 classification="", evidence_refs=None, answer_location="",
                 read_back_path=""):
    """Return the next command of the failure recovery sequence as JSON.

    The order is fixed: settlement proof, a durable failure closeout, then
    ``authorize_release``. Only an allowed decision emits the ``worker-release`` command.
    An unsettled or unknown settlement escalates; it is never released into a settled
    state, and a closeout for another Dispatch never authorizes this one.
    """
    if not ACCEPTANCE.is_nonempty(run_id):
        _invalid("failure_plan needs a non-empty run_id")
    if not ACCEPTANCE.is_nonempty(dispatch_id):
        _invalid("failure_plan needs the failed dispatch id")
    readable = isinstance(read_back, dict) and read_back.get("state") == "ok"
    settled = _settled_from(read_back, dispatch_id)
    worker = (read_back.get("worker")
              if readable and isinstance(read_back.get("worker"), dict) else {})
    result = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "dispatch_id": dispatch_id,
        "settled": settled,
        "settled_proof": {
            "source": "worker-show",
            "state": read_back.get("state") if isinstance(read_back, dict) else "absent",
            "worker_state": worker.get("worker_state", ""),
            "settled": settled,
        },
        "closeout_record_id": "",
        "next_action": "",
        "allowed": False,
        "reason": "",
        "authorize_release": None,
        "commands": [],
    }
    if not readable:
        result["next_action"] = "read-settled"
        result["reason"] = (
            "read the Dispatch back with worker-show and worker-list before any release "
            "decision")
        result["commands"] = _settled_commands(dispatch_id)
        return result
    if settled is not True:
        result["next_action"] = "escalate"
        result["reason"] = (
            "the read-back does not prove the Dispatch settled; release never turns an "
            "unsettled Dispatch into a settled one")
        return result
    records, unknown = LEDGER.effective_records(run_id, home=home)
    if unknown is not None:
        result["next_action"] = "escalate"
        result["reason"] = "the ledger history is incomplete: " + unknown["reason"]
        return result
    record = _failure_closeout(records, dispatch_id)
    if record is None:
        result["next_action"] = "record-closeout"
        result["reason"] = (
            "release needs a durable failure closeout with this Dispatch's "
            "classification, evidence references and mapping")
        command = _planned_record_command(
            run_id, dispatch_id, dispatch, classification, evidence_refs,
            answer_location, read_back_path)
        result["commands"] = [command]
        return result
    decision = release_decision(
        _summary(record, False, True), health_state="unknown", settled=True,
        decided_by="coordinator")
    result["closeout_record_id"] = record["record_id"]
    result["authorize_release"] = decision
    if decision["allowed"]:
        result["allowed"] = True
        result["next_action"] = "release"
        result["reason"] = decision["reason"]
        result["commands"] = [
            "<ORCA> orchestration worker-release --dispatch "
            + _quoted(dispatch_id) + " --json"]
        return result
    result["next_action"] = "record-closeout"
    result["reason"] = decision["reason"] + (
        "; record a failure closeout the release contract accepts")
    command = _planned_record_command(
        run_id, dispatch_id, dispatch, classification, evidence_refs,
        answer_location, read_back_path)
    result["commands"] = [command]
    return result


# ---------------------------------------------------------------------------
# reconcile: read authority, consume observations, resolve pending, persist
# ---------------------------------------------------------------------------


def _acceptance_section(records):
    items = []
    for record in records:
        if record.get("kind") != "acceptance":
            continue
        detail = record.get("detail")
        if not isinstance(detail, dict):
            continue
        items.append({"record_id": record["record_id"], **detail})
    return {"state": "ok", "reason": "", "records": items}


def _observations_section(records):
    items = []
    for record in records:
        if record.get("kind") != "observation":
            continue
        detail = record.get("detail")
        if not isinstance(detail, dict) or detail.get("observation") not in OBSERVATIONS:
            continue
        items.append({"record_id": record["record_id"], **detail})
    return {"state": "ok", "reason": "", "records": items}


def _resolution_index(records):
    resolved = {}
    for record in records:
        if record.get("kind") != "observation":
            continue
        detail = record.get("detail")
        if not isinstance(detail, dict) or detail.get("status") != "resolved":
            continue
        pending_id = detail.get("pending_record_id")
        if ACCEPTANCE.is_nonempty(pending_id):
            resolved[pending_id] = detail
    return resolved


def _pending_section(records, acceptance):
    resolved = _resolution_index(records)
    items = []
    for record in records:
        if record.get("kind") != "pending" or record.get("state") != "pending":
            continue
        detail = record.get("detail") if isinstance(record.get("detail"), dict) else {}
        subject = record.get("subject") if isinstance(record.get("subject"), dict) else {}
        pr = ACCEPTANCE.as_int(detail.get("pr", subject.get("pr")))
        acceptance_detail = next(
            (item for item in acceptance
             if _pr_number(item) == pr), {})
        item = {
            "record_id": record["record_id"],
            "pending_type": detail.get("pending_type")
            or detail.get("decision_class") or "pending",
            "pr": pr,
            "accepted_sha": detail.get("accepted_sha")
            or acceptance_detail.get("accepted_sha"),
            "ticket": detail.get("ticket") or acceptance_detail.get("ticket"),
            "owner": detail.get("owner") or record.get("actor", "human"),
            "condition": detail.get("condition") or detail.get("reason", ""),
            "check_after": detail.get("check_after") or record["observed_at"],
            "status": "pending",
        }
        if record["record_id"] in resolved:
            resolution = resolved[record["record_id"]]
            item["resolved"] = True
            item["status"] = "resolved"
            if isinstance(resolution.get("checks"), dict):
                item["checks"] = resolution["checks"]
        items.append(item)
    return {"state": "ok", "reason": "", "items": items}


def _authority_sections(authority, run_id, records):
    """Read the external sections through an injected adapter; never the network."""
    if hasattr(authority, "facts"):
        sections = authority.facts(run_id, records)
    elif hasattr(authority, "snapshot"):
        sections = authority.snapshot(run_id, records)
    else:
        sections = copy.deepcopy(authority)
    if not isinstance(sections, dict):
        _invalid("the authority adapter returned no object")
    unknown = sorted(set(sections) - set(AUTHORITY_SECTIONS)
                     - {"schema_version", "run_id", "observed_at"})
    if unknown:
        _invalid("unknown authority fields: " + ", ".join(unknown))
    return sections


def _facts_for(run_id, records, sections, answer_location, observed_at):
    acceptance = _acceptance_section(records)
    facts = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "observed_at": observed_at,
        "answer_location": answer_location,
        "acceptance": acceptance,
        "observations": _observations_section(records),
        "pending": _pending_section(records, acceptance["records"]),
    }
    for name in AUTHORITY_SECTIONS:
        section = sections.get(name)
        facts[name] = copy.deepcopy(section) if isinstance(section, dict) \
            else _unknown_section(name + " adapter not provided")
    return facts


def _mapping(workers):
    if workers.get("state") != "ok":
        return []
    mapping = []
    for row in _rows(workers, "rows"):
        if not ACCEPTANCE.is_nonempty(row.get("dispatch_id")) or not ACCEPTANCE.is_nonempty(row.get("task_id")):
            continue
        attempt = row.get("attempt")
        mapping.append({
            "dispatch_id": row["dispatch_id"],
            "task_id": row["task_id"],
            "attempt": attempt if type(attempt) is int else 0,
            "worktree": row.get("worktree", "") if isinstance(row.get("worktree"), str) else "",
        })
    return mapping


def _resolution_record(run_id, item, checks, observed_at):
    payload = {
        "pending_record_id": item["record_id"],
        "status": "resolved",
        "checks": checks,
        "reason": "the one-way door is reconciled: " + str(item.get("condition", "")),
        "evidence_ref": "ledger:" + run_id + ":" + item["record_id"],
        "observed_at": observed_at,
    }
    key = "orca-closeout-resolution:" + "|".join(
        (run_id, item["record_id"], LEDGER.canonical_json(checks)))
    record = {
        "schema_version": LEDGER.SCHEMA_VERSION,
        "record_id": str(uuid.uuid5(uuid.NAMESPACE_URL, key)),
        "kind": "observation",
        "run_id": run_id,
        "subject": {"ticket": item.get("ticket")} if ACCEPTANCE.is_nonempty(item.get("ticket")) else {},
        "action": "closeout",
        "request_id": "",
        "idempotency": "none",
        "state": "observed",
        "evidence_ref": payload["evidence_ref"],
        "observed_at": observed_at,
        "actor": "rule",
        "detail": payload,
    }
    return record


def _digest(facts, records, answer_location, verification):
    """Identity of one closeout: authority facts plus the source ledger, minus closeouts."""
    source = sorted(
        (record for record in records if record.get("kind") != "closeout"),
        key=lambda record: record["record_id"])
    payload = {
        "facts": {key: value for key, value in facts.items() if key != "observed_at"},
        "records": source,
        "answer_location": answer_location,
        "release_verification": verification,
    }
    return hashlib.sha256(LEDGER.canonical_json(payload).encode("utf-8")).hexdigest()


def _bounded_detail(detail):
    """Keep the record inside the ledger's 64 KiB limit without losing the identity.

    The full checklist is the useful part, so it is the last thing trimmed: the counts,
    the classification and the evidence references survive a truncation, and the record
    says it was truncated rather than failing to write at all.
    """
    if len(LEDGER.canonical_json(detail)) <= LEDGER.MAX_RECORD_BYTES - 4096:
        return detail
    trimmed = dict(detail)
    trimmed["checklist"] = {
        key: value for key, value in detail["checklist"].items()
        if key in ("schema_version", "run_id", "observed_at", "ready", "blocking",
                   "divergent", "classification", "merge_blocked", "non_repeatable",
                   "answer_location")
    }
    trimmed["truncated"] = True
    if len(LEDGER.canonical_json(trimmed)) <= LEDGER.MAX_RECORD_BYTES - 4096:
        return trimmed
    trimmed["evidence_refs"] = trimmed["evidence_refs"][:64]
    trimmed["mapping"] = trimmed["mapping"][:64]
    return trimmed


def _closeout_record(run_id, digest, checklist, facts, verification, observed_at):
    outcome = _outcome(checklist)
    evidence_refs = sorted({
        entry["ref"] for item in checklist["rows"] for entry in item["evidence"]})
    acceptance_ids = [item["record_id"] for item in checklist["rows"][0]
                      ["detail"].get("entries", []) if item.get("record_id")]
    mapping = _mapping(_section(facts, "workers"))
    detail = {
        "outcome": outcome,
        "classification": checklist["classification"],
        "answer_location": checklist["answer_location"],
        "acceptance_record_ids": acceptance_ids,
        "mapping": mapping,
        "evidence_refs": evidence_refs,
        "release_verification": verification,
        "checklist": checklist,
        "observed_at": observed_at,
    }
    detail = _bounded_detail(detail)
    state = {"success": "completed", "failure": "failed", "pending": "pending"}[outcome]
    return {
        "schema_version": LEDGER.SCHEMA_VERSION,
        "record_id": str(uuid.uuid5(
            uuid.NAMESPACE_URL, "orca-closeout:" + run_id + ":" + digest)),
        "kind": "closeout",
        "run_id": run_id,
        "subject": {},
        "action": "closeout",
        "request_id": "",
        "idempotency": "none",
        "state": state,
        "evidence_ref": checklist["answer_location"][:2048],
        "observed_at": observed_at,
        "actor": "coordinator",
        "detail": detail,
    }


def _outcome(checklist):
    if checklist["ready"]:
        return "success"
    if checklist["classification"] == "pending_one_way_door":
        return "pending"
    return "failure"


def _summary(record, appended, durable, retry_path=""):
    detail = record.get("detail", {})
    return {
        "record_id": record["record_id"],
        "appended": appended,
        "durable": durable,
        "outcome": detail.get("outcome"),
        "classification": detail.get("classification"),
        "acceptance_record_ids": detail.get("acceptance_record_ids", []),
        "mapping": detail.get("mapping", []),
        "evidence_refs": detail.get("evidence_refs", []),
        "answer_location": detail.get("answer_location", ""),
        "release_verification": detail.get("release_verification"),
        "retry_path": retry_path,
    }


def _unknown_history_result(run_id, unknown):
    """A closeout over an unreadable history is unknown, not a declaration."""
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "record_id": "",
        "recorded": False,
        "appended": False,
        "retry_path": "",
        "declared": False,
        "checklist": None,
        "pending_resolved": [],
        "history": unknown,
        "error": "the ledger history is incomplete: " + unknown["reason"],
    }


def reconcile(run_id, authority, home=None, answer_location=""):
    """Read authority through adapters, reconcile and persist one closeout record.

    The ledger is read once as a consistent prefix; no Orca, PR-host or tracker call runs
    while the ledger lock is held (the shared append takes it briefly). This function
    never merges, closes a ticket or releases a worker: it records what authority proves
    and resolves the pending items whose own checks are all true. A revert/reopen
    observation is consumed, never appended again.
    """
    if not ACCEPTANCE.is_nonempty(answer_location):
        _invalid("reconcile needs the Answer location for the closeout record")
    observed_at = _now()
    records, unknown = LEDGER.effective_records(run_id, home=home)
    if unknown is not None:
        return _unknown_history_result(run_id, unknown)
    sections = _authority_sections(authority, run_id, records)
    facts = _facts_for(run_id, records, sections, answer_location, observed_at)
    checklist = plan(facts)

    resolved_ids = []
    for item in checklist["rows"][9]["detail"].get("items", []):
        if not item["one_way_door"] or item["resolved"] or not item["reconcilable"]:
            continue
        record = _resolution_record(run_id, item, item["checks"], observed_at)
        LEDGER.append(record, home=home)
        resolved_ids.append(item["record_id"])
    if resolved_ids:
        records, unknown = LEDGER.effective_records(run_id, home=home)
        if unknown is not None:
            result = _unknown_history_result(run_id, unknown)
            result["pending_resolved"] = resolved_ids
            return result
        facts = _facts_for(run_id, records, sections, answer_location, observed_at)
        checklist = plan(facts)

    verification = checklist["release_verification"]
    digest = _digest(facts, records, answer_location, verification)
    record = _closeout_record(
        run_id, digest, checklist, facts, verification, observed_at)
    existing = {item["record_id"]: item for item in records
                if item.get("kind") == "closeout"}
    result = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "record_id": record["record_id"],
        "recorded": False,
        "appended": False,
        "retry_path": "",
        "checklist": checklist,
        "pending_resolved": resolved_ids,
    }
    if record["record_id"] in existing:
        result["recorded"] = True
        result["declared"] = checklist["ready"]
        result["closeout"] = _summary(existing[record["record_id"]], False, True)
        return result
    try:
        LEDGER.append(record, home=home)
    except (OSError, TypeError, ValueError) as error:
        path = ACCEPTANCE.pending_path(
            LEDGER.home_path(home), run_id, record["record_id"], PENDING_DIRNAME)
        ACCEPTANCE.write_pending(path, record)
        result["closeout"] = _summary(
            record, False, False, str(path))
        result["retry_path"] = str(path)
        result["record_error"] = str(error)
        result["declared"] = False
        return result
    result["recorded"] = True
    result["appended"] = True
    result["declared"] = checklist["ready"]
    result["closeout"] = _summary(record, True, True)
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _read_json(path):
    raw = sys.stdin.read() if path == "-" else Path(path).read_text()
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as error:
        _invalid(str(path) + " is not valid JSON: " + str(error))


def _emit(payload):
    print(json.dumps(payload, ensure_ascii=False))


class RecordedFacts:
    """An adapter serving the authority sections recorded in one JSON file."""

    def __init__(self, sections):
        self.sections = sections

    def facts(self, run_id, records):
        return copy.deepcopy(self.sections)


def command_plan(args):
    recorded = _read_json(args.facts)
    if not isinstance(recorded, dict) or recorded.get("run_id") != args.run_id:
        _invalid("the facts file belongs to another run")
    checklist = plan(recorded)
    _emit(checklist)
    return 0


def command_reconcile(args, home):
    recorded = _read_json(args.authority) if args.authority else {}
    if not isinstance(recorded, dict):
        _invalid("the authority file must hold a JSON object")
    unknown = sorted(set(recorded) - set(AUTHORITY_SECTIONS))
    if unknown:
        _invalid("the authority file may only supply " + ", ".join(AUTHORITY_SECTIONS)
                 + "; unknown: " + ", ".join(unknown)
                 + "; run --print-schema for each section's row shape")
    result = reconcile(args.run_id, RecordedFacts(recorded), home=home,
                       answer_location=args.answer_location)
    _emit(result)
    return 0 if result["recorded"] else 1


def command_show(args, home):
    records, unknown = LEDGER.effective_records(args.run_id, home=home)
    history = unknown if unknown is not None else {"state": "ok", "reason": ""}
    records = [record for record in records if record.get("kind") == "closeout"]
    if not records:
        _emit({"found": False, "run_id": args.run_id, "history": history})
        return 1
    record = records[-1]
    detail = record.get("detail") if isinstance(record.get("detail"), dict) else {}
    _emit({
        "found": True,
        "run_id": args.run_id,
        "record_id": record["record_id"],
        "observed_at": record["observed_at"],
        "checklist": detail.get("checklist"),
        "closeout": detail,
        "history": history,
    })
    return 0


def command_failure_record(args, home):
    read_back = _read_json(args.read_back)
    result = failure_record(
        args.run_id, read_back,
        {"dispatch_id": args.dispatch_id, "task_id": args.task_id,
         "attempt": args.attempt, "worktree": args.worktree},
        args.classification, args.evidence_refs, home=home,
        answer_location=args.answer_location,
        release_verification={
            "source": args.release_source,
            "at": args.release_at,
            "version_scope": args.version_scope,
        })
    _emit(result)
    return 0 if result["recorded"] else 1


def command_failure_plan(args, home):
    read_back = _read_json(args.read_back)
    dispatch = None
    if args.task_id is not None and args.attempt is not None and args.worktree is not None:
        dispatch = {"dispatch_id": args.dispatch_id, "task_id": args.task_id,
                    "attempt": args.attempt, "worktree": args.worktree}
    result = failure_plan(
        args.run_id, args.dispatch_id, read_back, home=home, dispatch=dispatch,
        classification=args.classification or "",
        evidence_refs=args.evidence_refs or [],
        answer_location=args.answer_location or "",
        read_back_path=args.read_back if args.read_back != "-" else "")
    _emit(result)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="closeout.py", description=__doc__, epilog=SCHEMA_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--print-schema", action="store_true",
                        help="print the facts/authority JSON schema and exit")
    sub = parser.add_subparsers(dest="command", required=False)

    plan_parser = sub.add_parser(
        "plan", help="print the ten-row closeout checklist from a facts file")
    plan_parser.add_argument("--run-id", required=True)
    plan_parser.add_argument("--facts", required=True, metavar="FILE",
                             help="facts JSON, or - for stdin")

    reconcile_parser = sub.add_parser(
        "reconcile", help="read authority, reconcile and persist one closeout record")
    reconcile_parser.add_argument("--run-id", required=True)
    reconcile_parser.add_argument(
        "--authority", metavar="FILE",
        help="recorded pr_host/git/tickets/tracker/workers/dashboard/release sections")
    reconcile_parser.add_argument(
        "--answer-location", required=True,
        help="where this ticket's Answer lives; stored on the closeout record")

    show_parser = sub.add_parser("show", help="print the last persisted checklist")
    show_parser.add_argument("--run-id", required=True)

    failure_record_parser = sub.add_parser(
        "failure-record",
        help="persist one failure closeout after a settled read-back; refuses to release")
    failure_record_parser.add_argument("--run-id", required=True)
    failure_record_parser.add_argument("--dispatch-id", required=True)
    failure_record_parser.add_argument("--task-id", required=True)
    failure_record_parser.add_argument("--attempt", type=int, required=True)
    failure_record_parser.add_argument("--worktree", required=True)
    failure_record_parser.add_argument("--classification", required=True,
                                       choices=FAILURE_CLASSES)
    failure_record_parser.add_argument("--evidence-ref", action="append",
                                       dest="evidence_refs", required=True,
                                       metavar="REF",
                                       help="repeat one per evidence reference")
    failure_record_parser.add_argument("--answer-location", required=True)
    failure_record_parser.add_argument(
        "--read-back", required=True, metavar="FILE",
        help="worker-show JSON proving settled:true, or - for stdin")
    failure_record_parser.add_argument("--release-source",
                                       default=FAILURE_VERIFICATION_SOURCE)
    failure_record_parser.add_argument("--release-at", default="")
    failure_record_parser.add_argument("--version-scope",
                                       default=FAILURE_VERSION_SCOPE)

    failure_plan_parser = sub.add_parser(
        "failure-plan",
        help="print the next failure recovery command; emits release only when allowed")
    failure_plan_parser.add_argument("--run-id", required=True)
    failure_plan_parser.add_argument("--dispatch-id", required=True)
    failure_plan_parser.add_argument(
        "--read-back", required=True, metavar="FILE",
        help="worker-show JSON proving settled:true, or - for stdin")
    failure_plan_parser.add_argument("--task-id")
    failure_plan_parser.add_argument("--attempt", type=int)
    failure_plan_parser.add_argument("--worktree")
    failure_plan_parser.add_argument("--classification", choices=FAILURE_CLASSES)
    failure_plan_parser.add_argument("--evidence-ref", action="append",
                                     dest="evidence_refs", metavar="REF")
    failure_plan_parser.add_argument("--answer-location")

    args = parser.parse_args(argv)
    if args.print_schema:
        _emit(schema())
        return 0
    if args.command is None:
        parser.error("a command is required (or --print-schema)")
    try:
        if args.command == "plan":
            return command_plan(args)
        # One cross-module call point: the acceptance CLI already resolves the ledger
        # home under ledger.py's path policy plus the target-repository guard.
        home = ACCEPTANCE.home()
        if args.command == "reconcile":
            return command_reconcile(args, home)
        if args.command == "failure-record":
            return command_failure_record(args, home)
        if args.command == "failure-plan":
            return command_failure_plan(args, home)
        return command_show(args, home)
    except (OSError, TypeError, ValueError) as error:
        print("Closeout error: " + str(error), file=sys.stderr)
        _emit({"recorded": False, "ready": False, "errors": [str(error)]})
        return 1


if __name__ == "__main__":
    sys.exit(main())
