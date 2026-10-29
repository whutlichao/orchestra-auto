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
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid

SCHEMA_VERSION = 1
LEDGER_KIND = "preflight"
LEDGER_ACTION = "preflight"
#: The two item classes and the three item results the receipt may carry.
ITEM_CLASSES = ("mechanical", "judgment")
ITEM_RESULTS = ("pass", "fail", "waived")
TICKET_FIELDS = ("id", "type", "gate", "origin")
CLI_TIMEOUT_SECONDS = 30

#: The Orca CLI resolves through the same variable the rest of the skill uses.
CLI_ENV = "ORCA_CLI_COMMAND"
DEFAULT_CLI = "orca"

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
INDEPENDENT_ITEMS = ("live-settings",)

ITEM_CLASS = {
    item_id: line["class"] for line in CHECKLIST for item_id in line["items"]
}
ITEM_CLASS["live-settings"] = "mechanical"

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
    "source": "Orca CLI 1.4.244 on macOS",
    "verified_at": "2026-10-29",
    "scope": "version-scoped; verify against the current CLI and guide before reuse",
}
LIVE_SETTINGS_TABLE = "profile_state_documents"
LIVE_SETTINGS_DOMAIN = "settings"
LIVE_SETTINGS_FIELDS = ("defaultTuiAgent", "disabledTuiAgents")
NO_FALLBACK_NOTE = (
    "no fallback source (orca-data.json, backups, exports or skill text) is used"
)


def _invalid(message):
    raise ValueError(message)


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_timestamp(value):
    """Return an aware UTC ``datetime`` for an RFC3339 timestamp, or ``None``."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(timezone.utc)


def _timestamp_text(value):
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _json_object(text):
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


# ---------------------------------------------------------------------------
# The ledger seam
# ---------------------------------------------------------------------------


_LEDGER_MODULE = None


def ledger_module():
    """Load ``ledger.py`` from this script's directory, once.

    Loading by path keeps the two bundles byte-identical and avoids turning the scripts
    into a package. A missing or broken ledger is a receipt-write failure, which blocks
    the dispatch.
    """
    global _LEDGER_MODULE
    if _LEDGER_MODULE is None:
        path = Path(__file__).resolve().with_name("ledger.py")
        spec = importlib.util.spec_from_file_location("preflight_ledger", path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load the ledger module from {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _LEDGER_MODULE = module
    return _LEDGER_MODULE


# ---------------------------------------------------------------------------
# Command runners
# ---------------------------------------------------------------------------


class LocalRunner:
    """Run the mechanical probes through the resolved Orca CLI.

    The only Orca command this module ever runs is a read: ``status``, ``agent hooks
    status`` or ``orchestration worker-list``. It never starts, stops or messages a worker.
    """

    def __init__(self, orca=None):
        self.orca = orca or os.environ.get(CLI_ENV) or DEFAULT_CLI

    def run(self, argv, timeout=CLI_TIMEOUT_SECONDS):
        command = list(argv)
        if command and command[0] == "orca":
            command[0] = self.orca
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=timeout
            )
        except (OSError, subprocess.SubprocessError) as error:
            return {"returncode": 127, "stdout": "", "stderr": str(error)}
        return {
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }


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
    argv = ["orca", "orchestration", "worker-list", "--run", run_id, "--limit", "1"]
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
                    datetime.fromtimestamp(candidate.stat().st_mtime, timezone.utc)
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
    command = guide.get("command") if isinstance(guide.get("command"), str) else ""
    evidence = path if path_exists else ""
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
        connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as error:
        return None, f"cannot open the state database read-only: {error}"
    try:
        rows = connection.execute(
            f"SELECT payload FROM {LIVE_SETTINGS_TABLE} WHERE domain = ?",
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
    observed["databases"] = [path.name for path in candidates]
    if len(candidates) != 1:
        return _item(
            "live-settings", "fail", command, observed,
            f"expected exactly one non-backup state database, found {len(candidates)}; "
            + NO_FALLBACK_NOTE,
        )
    database = candidates[0]
    observed["database"] = database.name
    values, problem = _read_live_settings(database)
    evidence = str(database)[:2048]
    if problem:
        return _item(
            "live-settings", "fail", command, observed,
            problem + "; " + NO_FALLBACK_NOTE, evidence,
        )
    observed.update(values)
    return _item(
        "live-settings", "pass", command, observed,
        "read defaultTuiAgent and disabledTuiAgents from the current profile's single "
        "non-backup state database; the schema verification is version-scoped",
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
    note = entry.get("note") if isinstance(entry.get("note"), str) else ""
    evidence = entry.get("evidence_ref") if isinstance(entry.get("evidence_ref"), str) else ""
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


def check(ticket, context, runner=None):
    """Build the preflight receipt, write it to the ledger and return the decision.

    ``runner`` is injectable; the default is ``LocalRunner``. The receipt is written even
    when a blocking item fails, because the failed check is itself evidence. A write
    failure is different: it turns the decision into a block, so a caller can never read
    "allowed" from a record that is not durable.
    """
    ticket = _validated_ticket(ticket)
    context = _validated_context(context)
    checked_at = context.get("checked_at") or _now()
    parsed_checked_at = parse_timestamp(checked_at)
    if parsed_checked_at is None:
        _invalid("context.checked_at must be an RFC3339 timestamp")
    checked_at = _timestamp_text(parsed_checked_at)
    context = {**context, "checked_at": checked_at}
    runner = runner or LocalRunner(context.get("orca_command"))

    items = [
        _check_live_settings(runner),
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
    limit = provisional.get("max_changed_lines")
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
    healthy = bool(inbox.get("healthy"))
    reason = inbox.get("reason") if isinstance(inbox.get("reason"), str) else ""
    healthy_now = installed and healthy
    note = (
        "the inbox consumer is installed and healthy"
        if healthy_now else "the inbox consumer is not installed and healthy"
    )
    if reason:
        note += ": " + reason
    detail = {
        "observation": "inbox-health",
        "installed": installed,
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
        context.get("worktree") if isinstance(context.get("worktree"), str) else "",
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
            "inbox installed and healthy"
            if inbox_detail["outcome"] == "healthy"
            else "inbox not installed and healthy: " + inbox_detail["reason"]
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


def main(argv=None):
    parser = argparse.ArgumentParser(prog="preflight.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("check", "run the preflight, write the receipt, print the decision"),
        ("post-dispatch", "record the post-receipt checklist observations"),
    ):
        command_parser = sub.add_parser(name, help=help_text)
        command_parser.add_argument("--run-id", required=True)
        command_parser.add_argument(
            "--ticket", required=True, help="ticket identity JSON file")
        command_parser.add_argument(
            "--context", required=True, help="preflight context JSON file")
        command_parser.add_argument(
            "--orca", help="Orca executable; defaults to ORCA_CLI_COMMAND or 'orca'")
    args = parser.parse_args(argv)

    try:
        ticket = _load_json_file(args.ticket)
        context = _load_json_file(args.context)
        if not isinstance(context, dict):
            _invalid("context must be a JSON object")
        context["run_id"] = args.run_id
        if args.orca:
            context["orca_command"] = args.orca
        if args.command == "check":
            receipt = check(ticket, context)
            print(json.dumps(receipt, ensure_ascii=False))
            return 0 if (
                receipt["blocking"]["allowed"] and receipt["record"]["written"]
            ) else 1
        report = post_dispatch(ticket, context)
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["records"]["written"] else 1
    except (OSError, TypeError, ValueError) as error:
        print("Preflight error: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
