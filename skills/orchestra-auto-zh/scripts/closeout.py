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
     "authority": "git merge-base --is-ancestor", "repeatable": True,
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


def _section(facts, name):
    """Return one facts section; a missing or malformed section is unknown, not empty."""
    value = facts.get(name)
    if not isinstance(value, dict):
        return _unknown_section(name + " not provided")
    state = value.get("state")
    if state not in SECTION_STATES:
        _invalid("facts." + name + ".state must be 'ok' or 'unknown'")
    if not isinstance(value.get("reason", ""), str):
        _invalid("facts." + name + ".reason must be a string")
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


def _level(payload, name):
    levels = payload.get("levels") if isinstance(payload, dict) else None
    level = levels.get(name) if isinstance(levels, dict) else None
    return level if isinstance(level, dict) else {}


def _merge_sha(payload):
    return _level(payload, "merged").get("merge_sha")


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


def _row_one(run_id, facts):
    state, reason, accepted = _accepted_records(facts)
    if state != "ok":
        return _row(1, "unknown", {"reason": reason}), accepted
    problems, entries, evidence = [], [], []
    for item in accepted:
        payload = item["payload"]
        entries.append({
            "record_id": item["record_id"],
            "ticket": payload.get("ticket"),
            "pr": _pr_number(payload),
            "attempt": payload.get("attempt"),
            "accepted_sha": payload.get("accepted_sha"),
            "answer_location": payload.get("answer_location"),
            "valid": not item["errors"],
        })
        evidence.append(_evidence(
            "ledger", "ledger:" + run_id + ":" + str(item["record_id"]),
            "ok" if not item["errors"] else "invalid"))
        problems.extend(
            str(item["record_id"]) + ": " + error for error in item["errors"])
    if not accepted:
        problems.append(
            "no acceptance record is persisted; the accepted SHA must be captured at "
            "acceptance and cannot be reconstructed later")
    status = "blocked" if problems else "ok"
    return _row(1, status, {"entries": entries, "problems": problems}, evidence), accepted


def _row_two(facts, accepted):
    section = _section(facts, "pr_host")
    if section.get("state") != "ok":
        return _row(2, "unknown", {"reason": section.get("reason", "")})
    rows = _rows(section, "pull_requests")
    problems, evidence = [], []
    if not accepted:
        problems.append("no accepted PR to reconcile against the PR host")
    for item in accepted:
        payload = item["payload"]
        number = _pr_number(payload)
        accepted_sha = payload.get("accepted_sha")
        merge_sha = _merge_sha(payload)
        if not ACCEPTANCE.is_sha(merge_sha):
            problems.append(
                "PR " + str(number) + ": levels.merged.merge_sha must be the merge "
                "commit SHA")
        if not ACCEPTANCE.is_sha(accepted_sha):
            problems.append("PR " + str(number) + ": the accepted SHA is missing")
        row = next((candidate for candidate in rows
                    if ACCEPTANCE.as_int(candidate.get("pr")) == number), None)
        if row is None:
            problems.append("PR host has no record for PR " + str(number))
            continue
        state = str(row.get("state", "")).upper()
        evidence.append(_evidence(
            "pr_host", "pr_host:pr:" + str(number), state or "unknown"))
        if state != "MERGED":
            problems.append(
                "PR host reports " + repr(row.get("state")) + " for PR " + str(number)
                + "; a closeout needs MERGED")
        if row.get("merge_sha") != merge_sha:
            problems.append(
                "PR " + str(number) + ": PR host merge_sha does not match the accepted "
                "merge_sha")
        base = payload.get("pr", {}).get("base") if isinstance(payload.get("pr"), dict) else None
        if row.get("base") != base:
            problems.append(
                "PR " + str(number) + ": PR host base " + repr(row.get("base"))
                + " is not the accepted base " + repr(base))
    status = "blocked" if problems else "ok"
    return _row(2, status, {"problems": problems}, evidence)


def _row_three(facts, accepted):
    section = _section(facts, "git")
    if section.get("state") != "ok":
        return _row(3, "unknown", {"reason": section.get("reason", "")})
    ancestry = _rows(section, "ancestry")
    problems, evidence = [], []
    for item in accepted:
        payload = item["payload"]
        accepted_sha = payload.get("accepted_sha")
        merge_sha = _merge_sha(payload)
        if not ACCEPTANCE.is_sha(accepted_sha) or not ACCEPTANCE.is_sha(merge_sha):
            problems.append(
                "PR " + str(_pr_number(payload)) + ": the accepted SHA and merge SHA "
                "are needed before ancestry can be checked")
            continue
        ref = "git:ancestry:" + accepted_sha + ".." + merge_sha
        covered = any(
            row.get("ancestor") == accepted_sha
            and row.get("descendant") == merge_sha
            and row.get("result") is True
            for row in ancestry)
        evidence.append(_evidence("git", ref, "merged" if covered else "not_merged"))
        if not covered:
            problems.append(
                "git ancestry does not prove the accepted head " + accepted_sha[:12]
                + " is an ancestor of the merge " + merge_sha[:12])
    if not accepted:
        problems.append("no accepted PR to check ancestry for")
    status = "blocked" if problems else "ok"
    return _row(3, status, {"problems": problems, "escalate": bool(problems)}, evidence)


def _row_four(facts, accepted):
    section = _section(facts, "git")
    if section.get("state") != "ok":
        return _row(4, "unknown", {"reason": section.get("reason", "")})
    ancestry = _rows(section, "ancestry")
    main = section.get("main") if isinstance(section.get("main"), dict) else {}
    head = main.get("head")
    problems, evidence = [], []
    if not ACCEPTANCE.is_sha(head):
        problems.append("git.main.head is missing, so main cannot be checked")
    for item in accepted:
        payload = item["payload"]
        merge_sha = _merge_sha(payload)
        if not ACCEPTANCE.is_sha(merge_sha) or not ACCEPTANCE.is_sha(head):
            problems.append(
                "PR " + str(_pr_number(payload)) + ": the merge SHA and main head are "
                "needed before containment can be checked")
            continue
        ref = "git:ancestry:" + merge_sha + ".." + head
        contained = any(
            row.get("ancestor") == merge_sha
            and row.get("descendant") == head
            and row.get("result") is True
            for row in ancestry)
        evidence.append(_evidence("git", ref, "contained" if contained else "missing"))
        if not contained:
            problems.append(
                "main does not contain the merge " + merge_sha[:12] + " up to "
                + head[:12])
    if not accepted:
        problems.append("no accepted PR to check containment for")
    status = "blocked" if problems else "ok"
    return _row(4, status, {"problems": problems, "escalate": bool(problems)}, evidence)


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
    gap = section.get("gap")
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
    return {
        "pr_merged": merged,
        "merge_sha": merge_sha,
        "main_synced": synced,
        "tracker_closed": closed,
    }


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


def _classification(rows, pending_open, applied):
    blocking = [item for item in rows if item["status"] in BLOCKING_STATUSES
                and item["id"] != 9]
    if not blocking:
        return "ready"
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
        _invalid("facts.release.verification is required when the release section is ok")
    unknown = sorted(set(verification) - set(RELEASE_VERIFICATION_KEYS))
    if unknown:
        _invalid("facts.release.verification has unknown fields: " + ", ".join(unknown))
    if not ACCEPTANCE.is_nonempty(verification.get("source")):
        _invalid("facts.release.verification.source must name the verification source")
    if not ACCEPTANCE.is_utc_instant(verification.get("at")):
        _invalid("facts.release.verification.at must be an RFC3339 UTC instant")
    if not isinstance(verification.get("version_scope"), str):
        _invalid("facts.release.verification.version_scope must be a string")
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
    unknown = sorted(set(facts) - {"schema_version", "run_id", "observed_at",
                                   "answer_location", *FACTS_SECTIONS})
    if unknown:
        _invalid("unknown facts fields: " + ", ".join(unknown))
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

    rows = []
    row_one, accepted = _row_one(run_id, normalized)
    rows.append(row_one)
    rows.append(_row_two(normalized, accepted))
    rows.append(_row_three(normalized, accepted))
    rows.append(_row_four(normalized, accepted))
    rows.append(_row_five(normalized))
    rows.append(_row_six(normalized, accepted))
    rows.append(_row_seven(normalized, accepted))
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
        "classification": _classification(rows, pending_open, applied),
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


def _settled_from(read_back):
    if not isinstance(read_back, dict) or read_back.get("state") != "ok":
        return None
    worker = read_back.get("worker")
    if not isinstance(worker, dict):
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
                   "classification", "merge_blocked", "non_repeatable", "answer_location")
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
    records = LEDGER.read(run_id, home=home)
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
        records = LEDGER.read(run_id, home=home)
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
                 + "; unknown: " + ", ".join(unknown))
    result = reconcile(args.run_id, RecordedFacts(recorded), home=home,
                       answer_location=args.answer_location)
    _emit(result)
    return 0 if result["recorded"] else 1


def command_show(args, home):
    records = [record for record in LEDGER.read(args.run_id, home=home)
               if record.get("kind") == "closeout"]
    if not records:
        _emit({"found": False, "run_id": args.run_id})
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
    })
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="closeout.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

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

    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            return command_plan(args)
        # One cross-module call point: the acceptance CLI already resolves the ledger
        # home under ledger.py's path policy plus the target-repository guard.
        home = ACCEPTANCE.home()
        if args.command == "reconcile":
            return command_reconcile(args, home)
        return command_show(args, home)
    except (OSError, TypeError, ValueError) as error:
        print("Closeout error: " + str(error), file=sys.stderr)
        _emit({"recorded": False, "ready": False, "errors": [str(error)]})
        return 1


if __name__ == "__main__":
    sys.exit(main())
