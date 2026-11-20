#!/usr/bin/env python3
"""Dispatch preflight receipts: one ledger record per dispatch, with blocking checks.

The preflight is the machine-readable form of the dispatch tick sheet in
``references/dispatch-preflight.md``. The sheet stays the source of truth: every line
there has an item id and a class here, and the receipt cannot silently drop one.

Public interface (module seam)::

    check(ticket, context, runner=None) -> receipt

``ticket`` is ``{id, type, gate, origin}``. ``context`` carries the round start, the guide
fetch, the coordinator's judgment results, baseline declarations, optional overrides, the
real worktree and the ledger home. Mechanical items run through an injectable command
runner; tests inject a fake and never execute a live Orca or a live settings command.

The receipt is appended as a ledger ``kind: preflight`` record **before** the dispatch.
Exit/decision semantics: a failed blocking item rejects the dispatch unless a complete
override (``item``, ``by``, ``reason``) waives it, and a receipt that cannot be written
durably also rejects the dispatch. The command never runs ``worker-start``.

Only the Python standard library is used; ``ledger.py`` is loaded from the same directory
so both bundles share one schema implementation.
"""

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import NoReturn
import uuid

_COMMON_PATH = Path(__file__).resolve().with_name("_common.py")
import importlib.util  # noqa: E402

_COMMON_SPEC = importlib.util.spec_from_file_location("orca_preflight_common", _COMMON_PATH)
assert _COMMON_SPEC is not None and _COMMON_SPEC.loader is not None
_COMMON = importlib.util.module_from_spec(_COMMON_SPEC)
_COMMON_SPEC.loader.exec_module(_COMMON)

#: Python 3.10 is the supported floor and has no ``datetime.UTC`` alias, so the zone is
#: named once here instead of being spelled out at every timestamp.
UTC = _COMMON.UTC

SCHEMA_VERSION = 1
LEDGER_KIND = "preflight"
LEDGER_ACTION = "preflight"
#: The two item classes and the three item results the receipt may carry.
ITEM_CLASSES = ("mechanical", "judgment")
ITEM_RESULTS = ("pass", "fail", "waived")
TICKET_FIELDS = ("id", "type", "gate", "origin")
CLI_TIMEOUT_SECONDS = _COMMON.CLI_TIMEOUT_SECONDS

#: The Orca CLI resolves through the same variable the rest of the skill uses.
CLI_ENV = _COMMON.CLI_ENV
DEFAULT_CLI = _COMMON.DEFAULT_CLI
resolve_orca_cli = _COMMON.resolve_orca_cli


#: The twelve §4.0.1 tick-sheet lines, in order. ``items`` are the receipt items each line
#: produces; the CLI-liveness line is deliberately split into two evidence items. The
#: ``live-settings`` lookup is an independent blocking item, not a thirteenth line.
CHECKLIST = (
    {"id": "cli-alive", "class": "mechanical",
     "title": "The CLI is alive",
     "items": ("cli-app-up", "cli-command-surface")},
    {"id": "guide-freshness", "class": "mechanical",
     "title": "The guide was fetched this round",
     "items": ("guide-freshness",)},
    {"id": "channel-decided", "class": "judgment",
     "title": "The channel is decided and was re-checked if exploration happened",
     "items": ("channel-decided",)},
    {"id": "ticket-type", "class": "judgment",
     "title": "The ticket type is decided and appears as a spec line",
     "items": ("ticket-type",)},
    {"id": "spec-elements", "class": "judgment",
     "title": "The spec has all five elements",
     "items": ("spec-elements",)},
    {"id": "worktree-parameters", "class": "judgment",
     "title": "The worktree parameters are right",
     "items": ("worktree-parameters",)},
    {"id": "long-horizon-map", "class": "judgment",
     "title": "A long-horizon map is really ready",
     "items": ("long-horizon-map",)},
    {"id": "main-worktree-rule", "class": "judgment",
     "title": "The main hard-rule self-check really ran",
     "items": ("main-worktree-rule",)},
    {"id": "ticket-canonical-copy", "class": "judgment",
     "title": "The ticket's canonical copy is located",
     "items": ("ticket-canonical-copy",)},
    {"id": "gates-decidable", "class": "judgment",
     "title": "The gates are named and decidable",
     "items": ("gates-decidable",)},
    {"id": "baseline-source-commit", "class": "mechanical",
     "title": "Every baseline number was verified against the base commit",
     "items": ("baseline-source-commit",)},
    {"id": "formatter-prevention", "class": "judgment",
     "title": "The formatter hazard prevention is written into the spec",
     "items": ("formatter-prevention",)},
)

#: The independent blocking lookup that runs before the sheet: the agent choice depends
#: on it, so it is not one of the twelve tick-sheet lines.
INDEPENDENT_ITEMS = ("live-settings", "message-capability")

ITEM_CLASS = {
    item_id: line["class"] for line in CHECKLIST for item_id in line["items"]
}
ITEM_CLASS["live-settings"] = "mechanical"
ITEM_CLASS["message-capability"] = "mechanical"

JUDGMENT_ITEMS = tuple(
    item_id
    for line in CHECKLIST if line["class"] == "judgment"
    for item_id in line["items"]
)

ITEM_ORDER = INDEPENDENT_ITEMS + tuple(
    item_id for line in CHECKLIST for item_id in line["items"]
)

#: The three post-receipt checklist observations. All are ledger observations, never
#: pre-dispatch gates; a failed dashboard write records a gap and never blocks a dispatch.
OBSERVATIONS = ("review-exemption-recheck", "dashboard-backfill", "inbox-health")

#: The live-settings schema is version-scoped. These names were verified against the CLI
#: and guide recorded here; a different Orca version may differ, so a read that does not
#: match fails and reports instead of guessing or falling back.
LIVE_SETTINGS_VERIFICATION = {
    "source": "Orca CLI 1.4.265 on macOS",
    "verified_at": "2026-11-20",
    "scope": "version-scoped; verify against the current CLI and guide before reuse",
}
#: The documented settings table. The read below spells it literally so no identifier is
#: ever interpolated into SQL; a test keeps the literal and this name in step.
LIVE_SETTINGS_TABLE = "profile_state_documents"
LIVE_SETTINGS_DOMAIN = "settings"
LIVE_SETTINGS_FIELDS = ("defaultTuiAgent", "disabledTuiAgents")
NO_FALLBACK_NOTE = (
    "no fallback source (orca-data.json, backups, exports or skill text) is used"
)


def _invalid(message) -> NoReturn:
    raise ValueError(message)


_now = _COMMON.utc_now
parse_timestamp = _COMMON.parse_utc
_timestamp_text = _COMMON.timestamp_text
_json_object = _COMMON.json_object


# ---------------------------------------------------------------------------
# The ledger seam
# ---------------------------------------------------------------------------


def ledger_module():
    """Load ``ledger.py`` from this script's directory, once.

    Loading by path keeps the two bundles byte-identical and avoids turning the scripts
    into a package. A missing or broken ledger is a receipt-write failure, which blocks
    the dispatch.
    """
    return _COMMON.load_sibling(__file__, "ledger.py", alias="preflight_ledger")


# ---------------------------------------------------------------------------
# Command runners
# ---------------------------------------------------------------------------


class LocalRunner:
    """Run the mechanical probes through the resolved Orca CLI.

    The only Orca command this module ever runs is a read: ``status``, ``agent hooks
    status`` or ``orchestration worker-list``, the version-matched guide and command help. It never starts, stops or messages a worker.
    """

    def __init__(self, orca=None):
        self.orca = orca or resolve_orca_cli()

    def run(self, argv, timeout=CLI_TIMEOUT_SECONDS):
        command = list(argv)
        if command and command[0] == "orca":
            command[0] = self.orca
        return _COMMON.run_command(command, timeout=timeout)


def _run(runner, argv):
    """Run one probe, normalising an injected runner's reply and failures."""
    try:
        result = runner.run(list(argv), timeout=CLI_TIMEOUT_SECONDS)
    except Exception as error:  # a runner failure is evidence, never a pass
        return {"returncode": 127, "stdout": "", "stderr": str(error)}
    if not isinstance(result, dict):
        return {"returncode": 127, "stdout": "", "stderr": "runner returned no result object"}
    return {
        "returncode": result.get("returncode", 127),
        "stdout": result.get("stdout", "") or "",
        "stderr": result.get("stderr", "") or "",
    }


def _item(item_id, result, command="", observed=None, note="", evidence_ref=""):
    if result not in ITEM_RESULTS:
        _invalid(f"unknown item result for {item_id}: {result}")
    return {
        "id": item_id,
        "class": ITEM_CLASS[item_id],
        "result": result,
        "command": command,
        "observed": {} if observed is None else observed,
        "note": note,
        "evidence_ref": evidence_ref,
    }


def _failure_note(prefix, result):
    detail = (result.get("stderr") or "").strip()
    if detail:
        return f"{prefix}: {detail}"
    return prefix


# ---------------------------------------------------------------------------
# Mechanical items
# ---------------------------------------------------------------------------


def _command_reply(result, what):
    """Return ``(reply, problem)`` for a read command that must answer JSON."""
    if result["returncode"] != 0:
        return None, _failure_note(f"{what} failed", result)
    reply = _json_object(result["stdout"])
    if reply is None:
        return None, f"{what} did not return a JSON object"
    if reply.get("ok") is False:
        return None, f"{what} reported ok: false"
    return reply, ""


#: The ledger caps a record at 64 KiB, so a chatty reply is summarised rather than
#: copied. The receipt must still be writable, or a passing preflight would be blocked by
#: an oversized observation.
MAX_OBSERVED_REPLY = 4096


def _bounded_reply(reply):
    """Return ``reply`` when small, else a summary that names its size and digest."""
    text = json.dumps(reply, sort_keys=True, ensure_ascii=False)
    if len(text) <= MAX_OBSERVED_REPLY:
        return reply
    return {
        "truncated": True,
        "bytes": len(text.encode("utf-8")),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def _check_status(runner):
    argv = ["orca", "status", "--json"]
    result = _run(runner, argv)
    reply, problem = _command_reply(result, "orca status --json")
    observed = {"returncode": result["returncode"]}
    if reply is not None:
        observed["reply"] = _bounded_reply(reply)
    if problem:
        return _item("cli-app-up", "fail", "orca status --json", observed, problem)
    return _item(
        "cli-app-up", "pass", "orca status --json", observed,
        "the app answered a JSON status reply",
    )


def _check_command_surface(runner, run_id):
    argv = ["orca", "orchestration", "worker-list", "--run", run_id, "--limit", "1",
            "--json"]
    command = " ".join(argv)
    result = _run(runner, argv)
    reply, problem = _command_reply(result, "orca orchestration worker-list")
    observed = {"returncode": result["returncode"]}
    if reply is not None:
        observed["reply"] = _bounded_reply(reply)
    if problem:
        return _item("cli-command-surface", "fail", command, observed, problem)
    return _item(
        "cli-command-surface", "pass", command, observed,
        "the command surface answered a minimal worker-list probe",
    )


#: The guide sentence that separates the two delivery shapes: a chat receives each
#: message whole, a terminal is woken with a check command it must run itself.
TERMINAL_WAKE_MARKERS = (
    "a wake that names a",
    "(a terminal, or one message too large for a chat message)",
    "means run that command",
)


def _collapse(text):
    """Collapse whitespace so a wrapped guide line still matches its sentence.

    The guide is markdown, so a sentence it publishes is wrapped across lines. Comparing
    wrapped text literally reports a line break as changed wording, which is neither.
    """
    return " ".join(text.split())


def _terminal_binding(runner, guide):
    """Probe whether this pane's runtime terminal handle is this agent's mailbox.

    This is the TUI-terminal counterpart of the structured-session probe: identity comes
    from Orca's own terminal inventory (the handle it injected beside the pane it injected
    into), addressability from the mailbox that handle owns, and the wake shape from the
    guide. Like the structured probe it establishes identity and the documented contract,
    never an end-to-end wake test. No message content is recorded, only a digest.
    """
    handle = os.environ.get("ORCA_TERMINAL_HANDLE", "").strip()
    pane = os.environ.get("ORCA_AGENT_PANE", "").strip()
    tab, _, leaf = pane.partition(":")
    probe = {
        "handle": handle,
        "pane": pane,
        "contract": all(marker in guide for marker in TERMINAL_WAKE_MARKERS),
        "bound": False,
        "mailbox_ok": False,
        "reason": "",
    }
    if not handle or not tab or not leaf:
        probe["reason"] = "no runtime terminal handle and pane in this environment"
        return probe
    listing = _run(runner, ["orca", "terminal", "list", "--json"])
    reply, problem = _command_reply(listing, "orca terminal list --json")
    container = reply.get("result") if isinstance(reply, dict) else None
    rows = container.get("terminals") if isinstance(container, dict) else None
    rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    matches = [row for row in rows if row.get("handle") == handle]
    probe["terminal_count"] = len(rows)
    if problem or len(matches) != 1:
        probe["reason"] = problem or (
            "the runtime terminal handle is not one row of the terminal inventory"
            if not matches else
            "the runtime terminal handle is ambiguous in the terminal inventory"
        )
        return probe
    row = matches[0]
    if (row.get("tabId"), row.get("leafId")) != (tab, leaf):
        probe["reason"] = "the runtime terminal handle belongs to another pane"
        return probe
    if row.get("connected") is not True or row.get("orphaned") is not False:
        probe["reason"] = "the bound terminal is not connected or is orphaned"
        return probe
    probe["bound"] = True
    inbox = _run(runner, ["orca", "orchestration", "inbox", "--terminal", handle,
                          "--limit", "1", "--json"])
    inbox_problem = _command_reply(
        inbox, "orca orchestration inbox --terminal <handle> --json")[1]
    probe["mailbox"] = {
        "returncode": inbox["returncode"],
        "sha256": hashlib.sha256(inbox["stdout"].encode("utf-8")).hexdigest(),
    }
    probe["mailbox_ok"] = not inbox_problem
    if inbox_problem:
        probe["reason"] = inbox_problem
    return probe


_HANDLED_NEGATIONS = (
    # A negation only counts when it lands on the act of marking or consuming a batch
    # as handled, inside one clause. Scanning the whole help for a bare token is wrong:
    # the same page documents other modes, and ``--peek`` says it returns unread
    # messages "without marking them read", which says nothing about the consuming
    # check's handled semantics. Binding the negation to the handled verb keeps the gate
    # fail-closed (a genuine "does not mark it handled" still matches) while ignoring
    # negations aimed at an unrelated object.
    re.compile(
        r"(?:\b(?:not|never|no\s+longer|without)\b|n't\b)"
        r"[^.;]{0,80}?"
        r"\b(?:mark(?:s|ed|ing)?|consume[sd]?|consuming)\b"
        r"[^.;]{0,40}?"
        r"\b(?:handled|consumed)\b"
    ),
    re.compile(r"\bunhandled\b"),
    re.compile(r"\bdiscard(?:s|ed|ing)?\s+(?:the|this|any|a)\s+batch\b"),
)


def _handled_negation(text):
    """Return the clause that negates the handled contract, or ``''``.

    The help text documents every mode at once, so context decides whether a negation
    is a contract change. A match here means the selected CLI says out loud that a check
    does not mark (or no longer consumes) its batch as handled.
    """
    for pattern in _HANDLED_NEGATIONS:
        match = pattern.search(text)
        if match is not None:
            return match.group(0)
    return ""


def _messaging_contract_known(guide, check):
    """Verify wake, FIFO handled read, --retry-request replay, and inbox recovery."""
    if not guide or not check:
        return False
    if _handled_negation(check):
        return False
    wake_ok = ("orca wakes you" in guide) or ("orca" in guide and "wakes you" in guide)
    fifo_handled_ok = (
        "fifo" in check
        and "batch" in check
        and "handled" in check
        and ("mark" in check or "marks" in check or "marked" in check)
    )
    retry_ok = "--retry-request" in check and (
        "same batch" in check or "same response" in check or "replay" in check
    )
    inbox_ok = "inbox" in check and "--terminal" in check and "--full" in check
    return wake_ok and fifo_handled_ok and retry_ok and inbox_ok


def _check_messaging(context, runner):
    """Fail closed unless this session and its selected CLI support recovery.

    Orca delivers to two shapes and this gate accepts both: a structured chat, whose
    session the CLI resolves, and a TUI agent terminal, whose handle the CLI can prove is
    this pane's and whose mailbox it can read. Neither path proves an end-to-end wake, and
    both still require the legacy-consumer migration attestation.
    """
    messaging = context.get("messaging")
    if not isinstance(messaging, dict):
        messaging = {}
    evidence = messaging.get("evidence_ref")
    evidence = evidence if isinstance(evidence, str) else ""
    confirmed = (messaging.get("native_wake_confirmed") is True
                 and messaging.get("source") in ("live_preamble", "verified_session")
                 and isinstance(evidence, str) and bool(evidence.strip()))
    migration_ref = messaging.get("migration_evidence_ref")
    migration_ready = (messaging.get("legacy_migration_complete") is True
                       and isinstance(migration_ref, str) and bool(migration_ref.strip()))
    probes = (
        (["orca", "--version"], ()),
        (["orca", "skills", "get", "orchestration"], ()),
        (["orca", "orchestration", "check", "--help"], ("--retry-request",)),
        (["orca", "orchestration", "inbox", "--help"], ("--terminal", "--full")),
    )
    observations = []
    outputs = {}
    for argv, required in probes:
        result = _run(runner, argv)
        output = result["stdout"]
        key = "/".join(argv[1:])
        digest = hashlib.sha256(output.encode("utf-8")).hexdigest()
        outputs[key] = output
        observations.append({"command": " ".join(argv), "returncode": result["returncode"],
                             "markers_present": all(token in output.lower() for token in required),
                             "sha256": digest})
    version = outputs["--version"].strip()
    available = bool(version) and all(outputs["/".join(argv[1:])].strip()
                                      for argv, _required in probes) and all(
                                      row["returncode"] == 0 and row["markers_present"]
                                      for row in observations)
    # Match the documented wake/FIFO/recovery semantics without tying verification
    # to brittle punctuation or filler words, while still failing on negations or missing
    # recovery capabilities unless human_verification explicitly attests the new CLI.
    guide = _collapse(outputs["skills/get/orchestration"]).lower()
    check = _collapse(outputs["orchestration/check/--help"]).lower()
    status_observation = {}
    terminal_probe = {}
    session_source = messaging.get("source")
    if not confirmed and os.environ.get("ORCA_STRUCTURED_SESSION") == "1" and os.environ.get("ORCA_AGENT_SESSION_ID"):
        status_result = _run(runner, ["orca", "status", "--json"])
        status, problem = _command_reply(status_result, "orca status --json")
        result = status.get("result") if isinstance(status, dict) else None
        caller = result.get("caller") if isinstance(result, dict) else None
        caller = caller if isinstance(caller, dict) else {}
        session_id = caller.get("orcaSessionId")
        chat_contract = (
            ("orca wakes you" in guide or ("orca" in guide and "wakes you" in guide))
            and ("each message arrives whole" in guide or ("chat" in guide and "whole" in guide))
        )
        confirmed = (not problem and status is not None and status.get("ok") is True
                     and caller.get("live") is True
                     and isinstance(session_id, str) and bool(session_id.strip()) and chat_contract)
        status_observation = {"returncode": status_result["returncode"],
                              "sha256": hashlib.sha256(status_result["stdout"].encode("utf-8")).hexdigest(),
                              "caller_live": caller.get("live") is True,
                              "session_id": session_id if isinstance(session_id, str) else "",
                              "chat_contract": chat_contract}
        if confirmed:
            session_source = "live_structured_chat"
            evidence = "orca status --json + version-matched orchestration guide"
    if not confirmed:
        terminal_probe = _terminal_binding(runner, guide)
        if terminal_probe["bound"] and terminal_probe["mailbox_ok"] and terminal_probe["contract"]:
            confirmed = True
            session_source = "live_terminal_binding"
            evidence = ("orca terminal list --json bound "
                        f"{terminal_probe['handle']} to this pane; "
                        "orca orchestration inbox --terminal "
                        f"{terminal_probe['handle']} --json reached its mailbox")
    handled_negation = _handled_negation(check)
    known = _messaging_contract_known(guide, check)
    review = messaging.get("human_verification")
    review = review if isinstance(review, dict) else {}
    hashes = {"/".join(argv[1:]): row["sha256"]
              for (argv, _required), row in zip(probes, observations, strict=True)}
    reviewer = review.get("reviewed_by")
    review_ref = review.get("evidence_ref")
    reviewed = (isinstance(reviewer, str) and bool(reviewer.strip())
                and isinstance(review_ref, str) and bool(review_ref.strip())
                and review.get("version") == version
                and review.get("probe_sha256") == hashes
                and all(review.get(key) is True for key in
                        ("native_wake", "handled_check", "retry_recovery", "inbox_recovery")))
    passed = confirmed and migration_ready and available and (known or reviewed)
    return _item("message-capability", "pass" if passed else "fail",
                 "read selected CLI version, orchestration guide, check/inbox help and the "
                 "terminal inventory",
                 {"session_confirmed": confirmed, "session_source": session_source,
                  "session_probe": status_observation,
                  "terminal_probe": terminal_probe,
                  "migration_confirmed": migration_ready,
                  "migration_evidence_ref": migration_ref if isinstance(migration_ref, str) else "",
                  "version": version, "probes": observations,
                  "handled_negation": handled_negation,
                  "semantic_basis": "documented_contract" if known else "human_review" if reviewed else "unknown",
                  "reviewed_by": reviewer if reviewed else "",
                  "review_evidence_ref": review_ref if reviewed else ""},
                 "native delivery and recovery confirmed" if passed else
                 "native delivery or recovery unverified; do not dispatch",
                 evidence)


def _check_guide(context):
    guide = context.get("guide") or {}
    checked_at = parse_timestamp(context.get("checked_at")) or parse_timestamp(_now())
    round_start = parse_timestamp(context.get("round_started_at"))
    fetched_at = parse_timestamp(guide.get("fetched_at"))
    path = guide.get("path") if isinstance(guide.get("path"), str) else ""
    path_exists = False
    mtime = None
    if path:
        try:
            candidate = Path(path)
            path_exists = candidate.is_file()
            if path_exists:
                mtime = _timestamp_text(
                    datetime.fromtimestamp(candidate.stat().st_mtime, UTC)
                )
        except OSError:
            path_exists = False
    observed = {
        "fetched_at": guide.get("fetched_at", ""),
        "round_started_at": context.get("round_started_at", ""),
        "checked_at": context.get("checked_at", ""),
        "path": path,
        "path_exists": path_exists,
        "mtime": mtime,
        "version": guide.get("version", ""),
        "fetch_command": guide.get("command", ""),
    }
    command = str(guide.get("command") or "")
    evidence = str(path) if path_exists else ""
    if fetched_at is None:
        return _item(
            "guide-freshness", "fail", command, observed,
            "no fetch timestamp this round; a file on disk alone does not pass",
            evidence,
        )
    if round_start is None:
        return _item(
            "guide-freshness", "fail", command, observed,
            "the round start is not recorded, so 'this round' cannot be established",
            evidence,
        )
    if fetched_at < round_start:
        return _item(
            "guide-freshness", "fail", command, observed,
            "the guide was fetched before this round started; the on-disk file is not fresh",
            evidence,
        )
    if checked_at is not None and fetched_at > checked_at:
        return _item(
            "guide-freshness", "fail", command, observed,
            "the fetch timestamp is later than the check; the record is not usable",
            evidence,
        )
    return _item(
        "guide-freshness", "pass", command, observed,
        "the guide was fetched during this round",
        evidence,
    )


def _check_baselines(context):
    baselines = context.get("baselines") or []
    if not isinstance(baselines, list):
        _invalid("context.baselines must be a list")
    base_commit = context.get("base_commit") or ""
    entries = []
    problems = []
    for index, entry in enumerate(baselines):
        if not isinstance(entry, dict):
            _invalid("each baseline must be an object")
        label = entry.get("label") or f"baseline-{index + 1}"
        if entry.get("used", True) is False:
            entries.append({"label": label, "status": "unused"})
            continue
        source = entry.get("source_commit")
        if not isinstance(source, str) or not source:
            entries.append({"label": label, "source_commit": "", "status": "missing"})
            problems.append(f"{label} has no source commit")
        elif source == base_commit:
            entries.append({
                "label": label, "source_commit": source, "status": "same-base",
            })
        elif entry.get("order_of_magnitude") is True:
            entries.append({
                "label": label, "source_commit": source,
                "status": "order-of-magnitude",
            })
        else:
            entries.append({
                "label": label, "source_commit": source, "status": "different-base",
            })
            problems.append(
                f"{label} names commit {source[:12]}, not this ticket's base "
                f"{base_commit[:12] or '(unrecorded)'}"
            )
    observed = {"base_commit": base_commit, "baselines": entries}
    if problems:
        return _item(
            "baseline-source-commit", "fail", "",
            observed, "; ".join(problems),
        )
    return _item(
        "baseline-source-commit", "pass", "", observed,
        "every used baseline names this ticket's base commit or is labelled "
        "order-of-magnitude only",
    )


def _read_live_settings(database):
    """Read the two verified fields from one settings database.

    Returns ``(values, problem)``. The whole payload is never copied: it may carry
    unrelated settings, and the ledger carries no credentials.
    """
    try:
        uri = database.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
    except (OSError, sqlite3.Error) as error:
        return None, f"cannot open the state database read-only: {error}"
    try:
        rows = connection.execute(
            "SELECT payload FROM profile_state_documents WHERE domain = ?",
            (LIVE_SETTINGS_DOMAIN,),
        ).fetchall()
    except sqlite3.Error as error:
        return None, f"cannot read {LIVE_SETTINGS_TABLE}.payload: {error}"
    finally:
        connection.close()
    if len(rows) != 1:
        return None, (
            f"expected exactly one {LIVE_SETTINGS_DOMAIN!r} row in "
            f"{LIVE_SETTINGS_TABLE}, found {len(rows)}"
        )
    payload = rows[0][0]
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            return None, "the settings payload is not UTF-8 text"
    if not isinstance(payload, str):
        return None, "the settings payload is not JSON text"
    try:
        document = json.loads(payload)
    except ValueError as error:
        return None, f"the settings payload is not JSON: {error}"
    if not isinstance(document, dict):
        return None, "the settings payload is not a JSON object"
    missing = [field for field in LIVE_SETTINGS_FIELDS if field not in document]
    if missing:
        return None, "the settings payload is missing: " + ", ".join(missing)
    default_agent = document["defaultTuiAgent"]
    disabled = document["disabledTuiAgents"]
    if not isinstance(default_agent, str) or not default_agent.strip():
        return None, "defaultTuiAgent is not a non-empty string"
    if not isinstance(disabled, list) or any(
        not isinstance(agent, str) for agent in disabled
    ):
        return None, "disabledTuiAgents is not an array of strings"
    return {"defaultTuiAgent": default_agent, "disabledTuiAgents": list(disabled)}, ""


def _check_live_settings(runner):
    argv = ["orca", "agent", "hooks", "status", "--json"]
    command = " ".join(argv)
    result = _run(runner, argv)
    observed = {
        "returncode": result["returncode"],
        "schema_verification": dict(LIVE_SETTINGS_VERIFICATION),
        "schema": {
            "table": LIVE_SETTINGS_TABLE,
            "domain": LIVE_SETTINGS_DOMAIN,
            "fields": ["payload." + field for field in LIVE_SETTINGS_FIELDS],
        },
    }
    if result["returncode"] != 0:
        observed["stderr"] = (result["stderr"] or "").strip()
        return _item(
            "live-settings", "fail", command, observed,
            _failure_note("live settings profile lookup failed", result)
            + "; " + NO_FALLBACK_NOTE,
        )
    reply = _json_object(result["stdout"])
    if reply is None:
        return _item(
            "live-settings", "fail", command, observed,
            "orca agent hooks status did not return a JSON object; " + NO_FALLBACK_NOTE,
        )
    settings_path = None
    result_section = reply.get("result")
    if isinstance(result_section, dict):
        settings_path = result_section.get("settingsPath")
    if not isinstance(settings_path, str) or not settings_path:
        return _item(
            "live-settings", "fail", command, observed,
            "the profile lookup did not name result.settingsPath; " + NO_FALLBACK_NOTE,
        )
    observed["settings_path"] = settings_path
    if not Path(settings_path).expanduser().is_absolute():
        return _item(
            "live-settings", "fail", command, observed,
            "result.settingsPath is not an absolute path; " + NO_FALLBACK_NOTE,
        )
    profile_dir = Path(settings_path).expanduser().parent
    try:
        candidates = sorted(
            path for path in profile_dir.iterdir()
            if path.is_file() and path.name.endswith(".db") and ".backup." not in path.name
        )
    except OSError as error:
        return _item(
            "live-settings", "fail", command, observed,
            f"cannot list the profile directory: {error}; " + NO_FALLBACK_NOTE,
        )
    observed["profile_directory"] = str(profile_dir)
    # A profile directory holds unrelated SQLite files (caches, a not-yet-created store),
    # so the file count proves nothing. Probe each non-backup candidate read-only and keep
    # only the one whose schema answers with exactly one usable settings document. The
    # filename is never a rule; a candidate that cannot be opened is simply not usable.
    probes = []
    usable = []
    for candidate in candidates:
        values, problem = _read_live_settings(candidate)
        if problem:
            probes.append({"database": candidate.name, "usable": False, "reason": problem})
            continue
        probes.append({
            "database": candidate.name,
            "usable": True,
            "reason": "one settings document with a defaultTuiAgent and disabledTuiAgents",
        })
        usable.append((candidate, values))
    observed["database_probes"] = probes
    if len(usable) != 1:
        detail = "; ".join(
            f"{probe['database']}: {probe['reason']}" for probe in probes
        ) or "no non-backup .db file in the profile directory"
        return _item(
            "live-settings", "fail", command, observed,
            f"expected exactly one usable state database, found {len(usable)} among "
            f"{len(candidates)} non-backup candidates; {detail}; " + NO_FALLBACK_NOTE,
        )
    database, values = usable[0]
    observed["database"] = database.name
    evidence = str(database)[:2048]
    observed.update(values)
    others = len(candidates) - 1
    rejected = (
        f"{others} other non-backup candidate(s) were probed and rejected"
        if others else "no other non-backup candidate was present"
    )
    return _item(
        "live-settings", "pass", command, observed,
        "read defaultTuiAgent and disabledTuiAgents from the single non-backup state "
        "database whose schema exposes one settings document; " + rejected
        + "; the schema verification is version-scoped",
        evidence,
    )


def _check_judgment(item_id, context):
    judgments = context.get("judgments") or {}
    entry = judgments.get(item_id) if isinstance(judgments, dict) else None
    if not isinstance(entry, dict):
        return _item(
            item_id, "fail", "",
            {"recorded_result": None},
            "the judgment item was not recorded",
        )
    result = entry.get("result")
    observed = {"recorded_result": result}
    note = str(entry.get("note") or "")
    evidence = str(entry.get("evidence_ref") or "")
    if result not in ("pass", "fail"):
        return _item(
            item_id, "fail", "", observed,
            f"the judgment result must be pass or fail, got {result!r}", evidence,
        )
    return _item(item_id, result, "", observed, note, evidence)


# ---------------------------------------------------------------------------
# Blocking decision, overrides and the receipt
# ---------------------------------------------------------------------------


def _apply_overrides(items, raw_overrides):
    by_id = {item["id"]: item for item in items}
    records = []
    for entry in raw_overrides:
        if not isinstance(entry, dict):
            _invalid("each override must be an object")
        item_id = entry.get("item")
        by = entry.get("by")
        reason = entry.get("reason")
        record = {
            "item": item_id if isinstance(item_id, str) else "",
            "by": by if isinstance(by, str) else "",
            "reason": reason if isinstance(reason, str) else "",
            "applied": False,
        }
        if not (isinstance(item_id, str) and item_id.strip()
                and isinstance(by, str) and by.strip()
                and isinstance(reason, str) and reason.strip()):
            record["error"] = "an override requires item, by and reason"
        elif item_id not in by_id:
            record["error"] = f"unknown item: {item_id}"
        elif item_id == "message-capability":
            record["error"] = "message capability cannot be waived"
        elif by_id[item_id]["result"] != "fail":
            record["error"] = "the item did not fail; nothing to waive"
        else:
            by_id[item_id]["result"] = "waived"
            record["applied"] = True
        records.append(record)
    return records


def _blocking_decision(items, overrides):
    failed = [
        item["id"] for item in items
        if item["result"] == "fail"
    ]
    waived = [
        item["id"] for item in items
        if item["result"] == "waived"
    ]
    reason = "; ".join(
        f"{item['id']}: {item['note'] or 'failed'}"
        for item in items if item["id"] in failed
    )
    return {
        "blocked": bool(failed),
        "allowed": not failed,
        "failed_items": failed,
        "waived_items": waived,
        "reason": reason,
    }


def _provisional_exemption(context, checked_at):
    value = context.get("review_exemption")
    if not value:
        return {"status": "none"}
    if not isinstance(value, dict):
        _invalid("context.review_exemption must be an object")
    reason = value.get("reason")
    impact = value.get("interface_impact")
    expected = value.get("expected_changed_files")
    if not isinstance(reason, str) or not reason.strip():
        _invalid("a review exemption requires a reason")
    if not isinstance(impact, str) or not impact.strip():
        _invalid("a review exemption requires the expected interface impact")
    if not isinstance(expected, list) or not expected or any(
        not isinstance(path, str) or not path.strip() for path in expected
    ):
        _invalid("a review exemption requires a non-empty expected_changed_files list")
    exemption = {
        "status": "provisional",
        "reason": reason,
        "interface_impact": impact,
        "expected_changed_files": [path.strip() for path in expected],
        "proposed_at": checked_at,
    }
    max_lines = value.get("max_changed_lines")
    if max_lines is not None:
        if type(max_lines) is not int or max_lines <= 0:
            _invalid("max_changed_lines must be a positive integer")
        exemption["max_changed_lines"] = max_lines
    return exemption


def _validated_ticket(ticket):
    if not isinstance(ticket, dict):
        _invalid("ticket must be a JSON object")
    missing = [
        field for field in TICKET_FIELDS
        if not isinstance(ticket.get(field), str) or not ticket[field].strip()
    ]
    if missing:
        _invalid("ticket requires non-empty " + ", ".join(TICKET_FIELDS))
    return {field: ticket[field] for field in TICKET_FIELDS}


def _validated_context(context):
    if not isinstance(context, dict):
        _invalid("context must be a JSON object")
    if not isinstance(context.get("run_id"), str) or not context["run_id"].strip():
        _invalid("context.run_id is required")
    for field in ("judgments", "guide", "review_exemption"):
        value = context.get(field)
        if value is not None and not isinstance(value, dict):
            _invalid(f"context.{field} must be an object")
    judgments = context.get("judgments") or {}
    unknown = sorted(set(judgments) - set(JUDGMENT_ITEMS))
    if unknown:
        _invalid("unknown judgment items: " + ", ".join(unknown))
    for field in ("overrides", "baselines"):
        value = context.get(field)
        if value is not None and not isinstance(value, list):
            _invalid(f"context.{field} must be a list")
    if context.get("base_commit") is not None and not isinstance(
        context.get("base_commit"), str
    ):
        _invalid("context.base_commit must be a string")
    record_id = context.get("record_id")
    if record_id is not None:
        try:
            uuid.UUID(str(record_id))
        except (TypeError, ValueError):
            _invalid("context.record_id must be a UUID")
    return context


def _append_receipt(detail, ticket, context, checked_at, blocking):
    record = {
        "schema_version": 1,
        "record_id": context.get("record_id") or str(uuid.uuid4()),
        "kind": LEDGER_KIND,
        "run_id": context["run_id"],
        "subject": {"ticket": ticket["id"]},
        "action": LEDGER_ACTION,
        "request_id": "",
        "idempotency": "none",
        "state": "failed" if blocking["blocked"] else "observed",
        "evidence_ref": ticket["origin"][:2048],
        "observed_at": checked_at,
        "actor": "coordinator",
        "detail": detail,
    }
    try:
        record_id = ledger_module().append(record, home=context.get("ledger_home"))
    except Exception as error:  # any failure to make the record durable blocks
        return {
            "written": False,
            "record_id": record["record_id"],
            "error": str(error),
        }
    return {"written": True, "record_id": record_id, "error": ""}


_SPEC_FIELD_PATTERNS = {
    "target": re.compile(r"^\s*(?:[-*]\s*)?target\s*[:：]\s*(.*)$", re.IGNORECASE),
    "change": re.compile(r"^\s*(?:[-*]\s*)?change\s*[:：]\s*(.*)$", re.IGNORECASE),
    "constraints": re.compile(r"^\s*(?:[-*]\s*)?constraints\s*[:：]\s*(.*)$", re.IGNORECASE),
    "ownership": re.compile(r"^\s*(?:[-*]\s*)?ownership\s*[:：]\s*(.*)$", re.IGNORECASE),
    "acceptance": re.compile(
        r"^\s*(?:[-*]\s*)?observable acceptance\s*[:：]\s*(.*)$", re.IGNORECASE
    ),
    "type": re.compile(
        r"^\s*(?:[-*]\s*)?(?:ticket\s*type|type|票型|票据类型)\s*[:：]\s*(.*)$",
        re.IGNORECASE,
    ),
    "gates": re.compile(r"^\s*(?:[-*]\s*)?(?:gates|门禁)\s*[:：]\s*(.*)$", re.IGNORECASE),
    "test_tier": re.compile(
        r"^\s*(?:[-*]\s*)?(?:test\s*tier|测试档)\s*[:：]\s*(.*)$", re.IGNORECASE
    ),
    "skill_location": re.compile(
        r"^\s*(?:[-*]\s*)?(?:file\s*location|文件位置)\s*[:：]\s*(.*)$", re.IGNORECASE
    ),
    "question_channel": re.compile(
        r"^\s*(?:[-*]\s*)?(?:question\s*channel|提问通道)\s*[:：]\s*(.*)$",
        re.IGNORECASE,
    ),
}
_FIVE_ELEMENT_KEYS = ("target", "change", "constraints", "ownership", "acceptance")
_PLACEHOLDER_ONLY_RE = re.compile(r"^<[^<>]+>$")
_IMPL_TYPE_WORDS = ("impl", "implementation", "bugfix", "实施", "code")


def _clean_field_value(raw):
    value = raw.strip()
    if len(value) >= 2 and value[0] == "`" and value[-1] == "`":
        value = value[1:-1].strip()
    return value


def _looks_placeholder(value):
    cleaned = _clean_field_value(value)
    if not cleaned:
        return True
    if _PLACEHOLDER_ONLY_RE.match(cleaned):
        return True
    if "<actual" in cleaned.lower() or "<实际" in cleaned:
        return True
    if "研究票 | 设计票" in cleaned or "research | design" in cleaned.lower():
        return True
    return False


def inspect_spec_text(
    spec_text,
    *,
    ticket_type="",
    ticket_gate="",
    review_exempt=False,
    check_paths=True,
    spec_ref="inline-spec",
):
    """Mechanically check the worker spec contract, fields, and pre-skill file paths."""
    if not isinstance(spec_text, str) or not spec_text.strip():
        return {
            "ok": False,
            "spec_ref": spec_ref,
            "errors": ["spec text is empty"],
            "fields": {},
            "skill_paths": [],
            "derived_judgments": {
                item_id: {
                    "result": "fail",
                    "note": "spec text is empty",
                    "evidence_ref": spec_ref,
                }
                for item_id in (
                    "spec-elements",
                    "ticket-type",
                    "gates-decidable",
                    "formatter-prevention",
                )
            },
        }

    lines = spec_text.splitlines()
    fields = {}
    skill_locations = []
    for idx, line in enumerate(lines):
        for key, pattern in _SPEC_FIELD_PATTERNS.items():
            match = pattern.match(line)
            if not match:
                continue
            val = match.group(1).strip()
            if not val and idx + 1 < len(lines):
                nxt = lines[idx + 1].strip()
                if nxt and not any(p.match(nxt) for p in _SPEC_FIELD_PATTERNS.values()):
                    val = nxt
            if key == "skill_location":
                if val:
                    skill_locations.append(_clean_field_value(val))
            elif key not in fields:
                fields[key] = val

    errors = []
    missing_elements = [k for k in _FIVE_ELEMENT_KEYS if not fields.get(k)]
    placeholder_elements = [
        k for k in _FIVE_ELEMENT_KEYS
        if fields.get(k) and _looks_placeholder(fields[k])
    ]
    behavior_rule_present = "BEHAVIOR RULE" in spec_text
    if missing_elements:
        errors.append("missing spec element(s): " + ", ".join(missing_elements))
    if placeholder_elements:
        errors.append("unfilled spec placeholder(s): " + ", ".join(placeholder_elements))
    if not behavior_rule_present:
        errors.append("missing BEHAVIOR RULE block")

    skill_paths = []
    path_errors = []
    for raw_loc in skill_locations:
        candidate = raw_loc.strip().rstrip(".,;")
        entry = {"path": candidate, "exists": False}
        if _looks_placeholder(candidate):
            entry["error"] = "unfilled skill path placeholder"
            path_errors.append(f"{candidate}: unfilled placeholder")
        elif check_paths:
            exists = Path(candidate).expanduser().is_file()
            entry["exists"] = exists
            if not exists:
                entry["error"] = "file not readable on disk"
                path_errors.append(f"{candidate}: not readable")
        else:
            entry["exists"] = True
        skill_paths.append(entry)
    if path_errors:
        errors.append("pre-skill path failure: " + "; ".join(path_errors))

    parsed_type = fields.get("type", "")
    type_ok = bool(parsed_type) and not _looks_placeholder(parsed_type)
    if not parsed_type:
        errors.append("missing Type / 票型 line")
    elif _looks_placeholder(parsed_type):
        errors.append("unfilled Type / 票型 placeholder")

    effective_type = (ticket_type or parsed_type).lower()
    is_impl = any(word in effective_type for word in _IMPL_TYPE_WORDS) or (
        ticket_gate in ("G-impl", "G-merge")
    )
    parsed_gates = fields.get("gates", "")
    gates_present = bool(parsed_gates) and not _looks_placeholder(parsed_gates)
    if not gates_present and not ticket_gate:
        errors.append("missing Gates / 门禁 line")
    elif parsed_gates and _looks_placeholder(parsed_gates):
        errors.append("unfilled Gates / 门禁 placeholder")
    code_review_ok = (
        not is_impl
        or review_exempt
        or ("code-review" in spec_text.lower())
    )
    if not code_review_ok:
        errors.append("implementation spec requires code-review unless review_exempt is set")

    formatter_ok = any(
        token in spec_text.lower()
        for token in ("formatter", "git status", "重排", "格式化")
    )
    if not formatter_ok:
        errors.append("missing formatter / git status safeguard")

    spec_elements_ok = (
        not missing_elements
        and not placeholder_elements
        and behavior_rule_present
        and not path_errors
    )
    gates_ok = (gates_present or bool(ticket_gate)) and not (
        parsed_gates and _looks_placeholder(parsed_gates)
    ) and code_review_ok

    elem_reasons = []
    if missing_elements:
        elem_reasons.append("missing: " + ", ".join(missing_elements))
    if placeholder_elements:
        elem_reasons.append("placeholder: " + ", ".join(placeholder_elements))
    if not behavior_rule_present:
        elem_reasons.append("missing BEHAVIOR RULE")
    if path_errors:
        elem_reasons.extend(path_errors)

    derived = {
        "spec-elements": {
            "result": "pass" if spec_elements_ok else "fail",
            "note": (
                "verified 5 elements, BEHAVIOR RULE, and pre-skill paths"
                if spec_elements_ok
                else "; ".join(elem_reasons)
            ),
            "evidence_ref": spec_ref,
        },
        "ticket-type": {
            "result": "pass" if type_ok else "fail",
            "note": f"type line: {parsed_type}" if type_ok else "missing or unfilled Type / 票型 line",
            "evidence_ref": spec_ref,
        },
        "gates-decidable": {
            "result": "pass" if gates_ok else "fail",
            "note": (
                f"gates: {parsed_gates or ticket_gate}"
                if gates_ok
                else (
                    "missing/unfilled Gates line or missing code-review"
                )
            ),
            "evidence_ref": spec_ref,
        },
        "formatter-prevention": {
            "result": "pass" if formatter_ok else "fail",
            "note": (
                "formatter / git status safeguard present"
                if formatter_ok
                else "missing formatter / git status safeguard"
            ),
            "evidence_ref": spec_ref,
        },
    }

    return {
        "ok": not errors,
        "spec_ref": spec_ref,
        "errors": errors,
        "fields": fields,
        "behavior_rule": behavior_rule_present,
        "skill_paths": skill_paths,
        "formatter_guard": formatter_ok,
        "code_review_present": code_review_ok,
        "derived_judgments": derived,
    }


def _apply_spec_to_context(ticket, context):
    """If ``context`` includes ``spec_file`` or ``spec_text``, derive spec judgments."""
    spec_file = context.get("spec_file")
    spec_text = context.get("spec_text")
    if not spec_file and not spec_text:
        return context, None
    spec_ref = str(spec_file or "inline-spec")
    if spec_file and not spec_text:
        spec_text = Path(str(spec_file)).read_text(encoding="utf-8")
    inspection = inspect_spec_text(
        spec_text or "",
        ticket_type=ticket.get("type", ""),
        ticket_gate=ticket.get("gate", ""),
        review_exempt=bool(context.get("review_exemption")),
        check_paths=bool(context.get("check_spec_paths", True)),
        spec_ref=spec_ref,
    )
    judgments = dict(context.get("judgments") or {})
    for item_id, entry in inspection["derived_judgments"].items():
        if item_id not in judgments:
            judgments[item_id] = entry
    return {**context, "judgments": judgments}, inspection


def check(ticket, context, runner=None):
    """Build the preflight receipt, write it to the ledger and return the decision.

    ``runner`` is injectable; the default is ``LocalRunner``. The receipt is written even
    when a blocking item fails, because the failed check is itself evidence. A write
    failure is different: it turns the decision into a block, so a caller can never read
    "allowed" from a record that is not durable.
    """
    ticket = _validated_ticket(ticket)
    context = _validated_context(context)
    context, spec_inspection = _apply_spec_to_context(ticket, context)
    checked_at = context.get("checked_at") or _now()
    parsed_checked_at = parse_timestamp(checked_at)
    if parsed_checked_at is None:
        _invalid("context.checked_at must be an RFC3339 timestamp")
    checked_at = _timestamp_text(parsed_checked_at)
    context = {**context, "checked_at": checked_at}
    runner = runner or LocalRunner(context.get("orca_command"))

    items = [
        _check_live_settings(runner),
        _check_messaging(context, runner),
        _check_status(runner),
        _check_command_surface(runner, context["run_id"]),
        _check_guide(context),
        *(_check_judgment(item_id, context) for item_id in JUDGMENT_ITEMS),
        _check_baselines(context),
    ]
    order = {item_id: index for index, item_id in enumerate(ITEM_ORDER)}
    items.sort(key=lambda item: order[item["id"]])

    raw_overrides = context.get("overrides") or []
    overrides = _apply_overrides(items, raw_overrides)
    blocking = _blocking_decision(items, overrides)
    detail = {
        "schema_version": SCHEMA_VERSION,
        "ticket": ticket,
        "checked_at": checked_at,
        "items": items,
        "blocking": blocking,
        "overrides": overrides,
        "post_dispatch": {
            "review_exemption": _provisional_exemption(context, checked_at),
            "observations": [],
        },
    }
    if spec_inspection is not None:
        detail["spec_inspection"] = {
            "ok": spec_inspection["ok"],
            "spec_ref": spec_inspection["spec_ref"],
            "errors": spec_inspection["errors"],
            "skill_paths": spec_inspection["skill_paths"],
        }
    record = _append_receipt(detail, ticket, context, checked_at, blocking)
    receipt = dict(detail)
    receipt["record"] = record
    if not record["written"]:
        receipt["blocking"] = {
            "blocked": True,
            "allowed": False,
            "failed_items": list(blocking["failed_items"]),
            "waived_items": list(blocking["waived_items"]),
            "reason": "the preflight receipt was not written durably: "
                      + record["error"],
        }
    return receipt


# ---------------------------------------------------------------------------
# Post-dispatch observations
# ---------------------------------------------------------------------------


def _normalize_relative_path(value):
    """Strip a leading ``./`` without eating meaningful leading dots (``.github/...``)."""
    value = value.strip()
    while value.startswith("./"):
        value = value[2:]
    return value


def _numstat_entries(text):
    """Parse ``git diff --numstat`` output into entries and malformed lines."""
    entries = []
    problems = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            problems.append("malformed numstat line: " + line)
            continue
        added, deleted, path = parts[0], parts[1], "\t".join(parts[2:])
        if not (added == "-" or added.isdigit()) or not (deleted == "-" or deleted.isdigit()):
            problems.append("malformed numstat counts: " + line)
            continue
        entries.append({"path": path, "added": added, "deleted": deleted})
    return entries, problems


def _review_recheck(ticket, context, runner, checked_at):
    """Recheck a provisional review exemption against the real worktree diff.

    This is a post-dispatch observation: it can revoke the exemption and call for a
    review or a send-back, but it never edits the preflight receipt and never blocks a
    dispatch that already happened. A run with no exemption still records the observation
    as ``none``, so the post-receipt checklist and the ledger always name the same three
    observations.
    """
    value = context.get("review_exemption")
    if not value:
        return {
            "observation": "review-exemption-recheck",
            "status": "none",
            "command": "",
            "observed": {},
            "reason": "no review exemption was declared",
        }
    provisional = _provisional_exemption({"review_exemption": value}, checked_at)
    worktree = context.get("worktree")
    if not isinstance(worktree, str) or not worktree.strip():
        _invalid("context.worktree is required to recheck a review exemption")
    base_commit = context.get("base_commit")
    argv = ["git", "-C", worktree, "diff", "--numstat"]
    if isinstance(base_commit, str) and base_commit.strip():
        argv.append(base_commit)
    command = " ".join(argv)
    result = _run(runner, argv)
    observed = {
        "returncode": result["returncode"],
        "worktree": worktree,
        "base_commit": base_commit if isinstance(base_commit, str) else "",
    }
    detail = {
        "observation": "review-exemption-recheck",
        "status": "revoked",
        "command": command,
        "observed": observed,
        "expected_changed_files": provisional["expected_changed_files"],
        "interface_impact": provisional["interface_impact"],
        "reason": "",
    }
    if result["returncode"] != 0:
        detail["reason"] = _failure_note(
            "the real worktree diff could not be read", result)
        detail["observed"]["stderr"] = (result["stderr"] or "").strip()
        return detail
    entries, problems = _numstat_entries(result["stdout"])
    total_added = sum(int(entry["added"]) for entry in entries if entry["added"] != "-")
    total_deleted = sum(int(entry["deleted"]) for entry in entries if entry["deleted"] != "-")
    changed = [entry["path"] for entry in entries]
    expected = {
        _normalize_relative_path(path)
        for path in provisional["expected_changed_files"]
    }
    normalized = [_normalize_relative_path(path) for path in changed]
    unexpected = sorted(path for path in normalized if path not in expected)
    observed.update({
        "entries": entries,
        "changed_files": changed,
        "added": total_added,
        "deleted": total_deleted,
        "total": total_added + total_deleted,
        "malformed_lines": problems,
    })
    if problems:
        detail["reason"] = "the real worktree diff contained unreadable lines"
        return detail
    if unexpected:
        detail["reason"] = (
            "the real diff changed unexpected files: " + ", ".join(unexpected)
        )
        return detail
    raw_limit = provisional.get("max_changed_lines")
    limit = int(raw_limit) if isinstance(raw_limit, int) else None
    if limit is not None and total_added + total_deleted > limit:
        detail["reason"] = (
            f"the real diff is {total_added + total_deleted} changed lines, above the "
            f"{limit}-line bound"
        )
        return detail
    detail["status"] = "confirmed"
    detail["reason"] = "the real worktree diff stays inside the provisional exemption"
    return detail


def _dashboard_observation(context):
    dashboard = context.get("dashboard")
    if not isinstance(dashboard, dict):
        _invalid("context.dashboard must be an object recording the dashboard write")
    written = bool(dashboard.get("written"))
    reason = dashboard.get("reason") if isinstance(dashboard.get("reason"), str) else ""
    if written:
        outcome = "backfilled"
        note = "the dashboard record was backfilled"
    else:
        outcome = "gap"
        note = "the dashboard write failed; a gap was recorded"
        if reason:
            note += ": " + reason
    detail = {
        "observation": "dashboard-backfill",
        "outcome": outcome,
        "written": written,
        "reason": reason,
    }
    return detail


def _inbox_observation(context):
    inbox = context.get("inbox")
    if not isinstance(inbox, dict):
        _invalid("context.inbox must be an object recording the inbox state")
    installed = bool(inbox.get("installed"))
    healthy = inbox.get("healthy") is True
    reason = inbox.get("reason") if isinstance(inbox.get("reason"), str) else ""
    healthy_now = inbox.get("native_wake") is True and healthy
    detail = {
        "observation": "inbox-health",
        "installed": installed,
        "native_wake": inbox.get("native_wake") is True,
        "healthy": healthy,
        "outcome": "healthy" if healthy_now else "unhealthy",
        "reason": reason,
    }
    return detail


def _append_observation(kind, ticket, context, checked_at, detail, evidence_ref=""):
    record = {
        "schema_version": 1,
        "record_id": str(uuid.uuid4()),
        "kind": kind,
        "run_id": context["run_id"],
        "subject": {"ticket": ticket["id"]},
        "action": LEDGER_ACTION,
        "request_id": "",
        "idempotency": "none",
        "state": "observed",
        "evidence_ref": (evidence_ref or "")[:2048],
        "observed_at": checked_at,
        "actor": "coordinator",
        "detail": detail,
    }
    try:
        return {"written": True, "record_id": ledger_module().append(
            record, home=context.get("ledger_home")), "error": ""}
    except Exception as error:
        return {"written": False, "record_id": record["record_id"], "error": str(error)}


def post_dispatch(ticket, context, runner=None):
    """Record the post-receipt checklist observations.

    The review-exemption recheck, the dashboard backfill/gap and the inbox health are
    ledger observations. None of them is a pre-dispatch gate, and none of them rewrites
    the preflight receipt: a revoked exemption or a failed dashboard write is evidence for
    the coordinator, not a retroactive change to the dispatch decision.
    """
    ticket = _validated_ticket(ticket)
    context = _validated_context(context)
    checked_at = context.get("checked_at") or _now()
    parsed_checked_at = parse_timestamp(checked_at)
    if parsed_checked_at is None:
        _invalid("context.checked_at must be an RFC3339 timestamp")
    checked_at = _timestamp_text(parsed_checked_at)
    runner = runner or LocalRunner(context.get("orca_command"))

    observations = []
    writes = []
    review_detail = _review_recheck(ticket, context, runner, checked_at)
    review_record = _append_observation(
        "observation", ticket, context, checked_at, review_detail,
        str(context.get("worktree") or ""),
    )
    writes.append(review_record)
    observations.append({
        "id": "review-exemption-recheck",
        "outcome": review_detail["status"],
        "note": review_detail["reason"],
        "detail": review_detail,
        "record": review_record,
    })

    dashboard_detail = _dashboard_observation(context)
    dashboard_record = _append_observation(
        "observation", ticket, context, checked_at, dashboard_detail)
    writes.append(dashboard_record)
    observations.append({
        "id": "dashboard-backfill",
        "outcome": dashboard_detail["outcome"],
        "note": (
            "dashboard record backfilled"
            if dashboard_detail["outcome"] == "backfilled"
            else "dashboard write gap recorded: "
                 + (dashboard_detail["reason"] or "no reason recorded")
        ),
        "detail": dashboard_detail,
        "record": dashboard_record,
    })
    if dashboard_detail["outcome"] == "gap":
        gap_detail = {
            "observation": "dashboard-backfill",
            "reason": dashboard_detail["reason"] or "dashboard write failed",
        }
        writes.append(_append_observation(
            "gap", ticket, context, checked_at, gap_detail))

    inbox_detail = _inbox_observation(context)
    inbox_record = _append_observation(
        "observation", ticket, context, checked_at, inbox_detail)
    writes.append(inbox_record)
    observations.append({
        "id": "inbox-health",
        "outcome": inbox_detail["outcome"],
        "note": (
            "native wake confirmed and healthy"
            if inbox_detail["outcome"] == "healthy"
            else "native wake not confirmed and healthy: " + inbox_detail["reason"]
        ),
        "detail": inbox_detail,
        "record": inbox_record,
    })

    errors = [write["error"] for write in writes if not write["written"]]
    return {
        "schema_version": SCHEMA_VERSION,
        "ticket": ticket,
        "checked_at": checked_at,
        "observations_expected": list(OBSERVATIONS),
        "review_exemption": review_detail or {"status": "none"},
        "observations": observations,
        "records": {
            "written": not errors,
            "record_ids": [
                write["record_id"] for write in writes if write["written"]
            ],
            "error": errors[0] if errors else "",
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_json_file(path):
    raw = Path(path).read_text()
    try:
        return json.loads(raw)
    except ValueError as error:
        _invalid(f"{path} is not valid JSON: {error}")


def _compact_receipt(receipt):
    failed = [
        {"id": item["id"], "note": item.get("note", "")}
        for item in receipt.get("items", [])
        if item.get("result") == "fail"
    ]
    payload = {
        "schema_version": receipt["schema_version"],
        "ticket": receipt["ticket"],
        "checked_at": receipt["checked_at"],
        "allowed": receipt["blocking"]["allowed"],
        "blocked": receipt["blocking"]["blocked"],
        "failed_items": receipt["blocking"]["failed_items"],
        "waived_items": receipt["blocking"]["waived_items"],
        "failed_details": failed,
        "record": receipt.get("record", {}),
    }
    if "spec_inspection" in receipt:
        payload["spec_inspection"] = receipt["spec_inspection"]
    return payload


def _compact_post_dispatch(report):
    return {
        "schema_version": report["schema_version"],
        "ticket": report["ticket"],
        "checked_at": report["checked_at"],
        "observations": [
            {"id": row["id"], "outcome": row["outcome"], "note": row["note"]}
            for row in report.get("observations", [])
        ],
        "records": report.get("records", {}),
    }


def _merge_cli_inputs(args):
    ticket = _load_json_file(args.ticket) if args.ticket else {}
    if not isinstance(ticket, dict):
        _invalid("ticket must be a JSON object")
    for arg_name, field in (
        ("ticket_id", "id"),
        ("ticket_type", "type"),
        ("ticket_gate", "gate"),
        ("ticket_origin", "origin"),
    ):
        value = getattr(args, arg_name, None)
        if value:
            ticket[field] = value

    context = _load_json_file(args.context) if args.context else {}
    if not isinstance(context, dict):
        _invalid("context must be a JSON object")
    context["run_id"] = args.run_id
    if args.orca:
        context["orca_command"] = args.orca

    if args.command != "check":
        return ticket, context

    if getattr(args, "spec", None):
        context["spec_file"] = args.spec
    if getattr(args, "no_spec_path_check", False):
        context["check_spec_paths"] = False
    if getattr(args, "round_started_at", None):
        context["round_started_at"] = args.round_started_at
    if getattr(args, "base_commit", None):
        context["base_commit"] = args.base_commit
    if getattr(args, "worktree", None):
        context["worktree"] = args.worktree

    if getattr(args, "guide", None) or getattr(args, "guide_fetched_at", None):
        guide = dict(context.get("guide") or {})
        if args.guide:
            guide["path"] = args.guide
        if args.guide_fetched_at:
            guide["fetched_at"] = args.guide_fetched_at
        if getattr(args, "guide_command", None):
            guide["command"] = args.guide_command
        context["guide"] = guide

    if any(
        getattr(args, name, None)
        for name in (
            "legacy_migrated",
            "migration_ref",
            "native_wake",
            "wake_source",
            "wake_evidence",
        )
    ):
        messaging = dict(context.get("messaging") or {})
        if args.legacy_migrated:
            messaging["legacy_migration_complete"] = True
        if args.migration_ref:
            messaging["migration_evidence_ref"] = args.migration_ref
        if args.native_wake:
            messaging["native_wake_confirmed"] = True
        if args.wake_source:
            messaging["source"] = args.wake_source
        if args.wake_evidence:
            messaging["evidence_ref"] = args.wake_evidence
        context["messaging"] = messaging

    judgment_notes = (
        ("channel_note", "channel-decided"),
        ("worktree_note", "worktree-parameters"),
        ("long_horizon_note", "long-horizon-map"),
        ("main_rule_note", "main-worktree-rule"),
        ("canonical_note", "ticket-canonical-copy"),
    )
    if any(getattr(args, attr, None) for attr, _ in judgment_notes):
        judgments = dict(context.get("judgments") or {})
        for attr, item_id in judgment_notes:
            note = getattr(args, attr, None)
            if note:
                judgments[item_id] = {
                    "result": "pass",
                    "note": note,
                    "evidence_ref": args.spec or note,
                }
        context["judgments"] = judgments
    return ticket, context


def main(argv=None):
    parser = argparse.ArgumentParser(prog="preflight.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("check", "run the preflight, write the receipt, print the decision"),
        ("post-dispatch", "record the post-receipt checklist observations"),
    ):
        command_parser = sub.add_parser(name, help=help_text)
        command_parser.add_argument("--run-id", required=True)
        command_parser.add_argument("--ticket", help="ticket identity JSON file")
        command_parser.add_argument("--context", help="preflight context JSON file")
        command_parser.add_argument("--ticket-id", help="inline ticket identifier")
        command_parser.add_argument("--type", dest="ticket_type", help="inline ticket type")
        command_parser.add_argument("--gate", dest="ticket_gate", help="inline target gate")
        command_parser.add_argument("--origin", dest="ticket_origin", help="inline ticket origin")
        command_parser.add_argument(
            "--orca", help="Orca executable; defaults to the resolved Orca executable")
        command_parser.add_argument("--compact", action="store_true", help="emit compact JSON summary")
        if name == "check":
            command_parser.add_argument("--spec", help="worker spec markdown/text file to verify")
            command_parser.add_argument("--no-spec-path-check", action="store_true",
                                        help="skip checking pre-skill file existence on disk")
            command_parser.add_argument("--guide", help="cached guide file path")
            command_parser.add_argument("--guide-fetched-at", help="RFC3339 fetch timestamp")
            command_parser.add_argument("--guide-command", help="command used to fetch guide")
            command_parser.add_argument("--round-started-at", help="RFC3339 round start timestamp")
            command_parser.add_argument("--base-commit", help="base commit SHA for baseline checks")
            command_parser.add_argument("--worktree", help="worktree path")
            command_parser.add_argument("--channel-note", help="judgment note for channel-decided")
            command_parser.add_argument("--worktree-note", help="judgment note for worktree-parameters")
            command_parser.add_argument("--long-horizon-note", help="judgment note for long-horizon-map")
            command_parser.add_argument("--main-rule-note", help="judgment note for main-worktree-rule")
            command_parser.add_argument("--canonical-note", help="judgment note for ticket-canonical-copy")
            command_parser.add_argument("--legacy-migrated", action="store_true",
                                        help="mark legacy consumer migration complete")
            command_parser.add_argument("--migration-ref", help="evidence ref for legacy migration")
            command_parser.add_argument("--native-wake", action="store_true",
                                        help="mark native wake confirmed (when not auto-probed)")
            command_parser.add_argument("--wake-source", help="native wake source identifier")
            command_parser.add_argument("--wake-evidence", help="native wake evidence reference")

    inspect_parser = sub.add_parser(
        "inspect-spec", help="mechanically validate a worker spec markdown/text file"
    )
    inspect_parser.add_argument("--spec", required=True, help="worker spec file path")
    inspect_parser.add_argument("--type", dest="ticket_type", default="", help="ticket type")
    inspect_parser.add_argument("--gate", dest="ticket_gate", default="", help="target gate")
    inspect_parser.add_argument("--review-exempt", action="store_true")
    inspect_parser.add_argument("--no-path-check", action="store_true")
    inspect_parser.add_argument("--compact", action="store_true")

    args = parser.parse_args(argv)

    try:
        if args.command == "inspect-spec":
            text = Path(args.spec).read_text(encoding="utf-8")
            result = inspect_spec_text(
                text,
                ticket_type=args.ticket_type,
                ticket_gate=args.ticket_gate,
                review_exempt=args.review_exempt,
                check_paths=not args.no_path_check,
                spec_ref=args.spec,
            )
            _COMMON.emit_json(result, compact=args.compact)
            return 0 if result["ok"] else 1

        if not args.ticket and not args.ticket_id:
            _invalid("--ticket FILE or --ticket-id is required")
        if not args.context and not args.ticket_id and not getattr(args, "spec", None):
            _invalid("--context FILE is required unless direct check flags are given")
        ticket, context = _merge_cli_inputs(args)
        if args.command == "check":
            receipt = check(ticket, context)
            payload = _compact_receipt(receipt) if args.compact else receipt
            _COMMON.emit_json(payload, compact=args.compact)
            return 0 if (
                receipt["blocking"]["allowed"] and receipt["record"]["written"]
            ) else 1
        report = post_dispatch(ticket, context)
        payload = _compact_post_dispatch(report) if args.compact else report
        _COMMON.emit_json(payload, compact=args.compact)
        return 0 if report["records"]["written"] else 1
    except (OSError, TypeError, ValueError) as error:
        print("Preflight error: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
