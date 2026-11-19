#!/usr/bin/env python3
"""Run health: a pure state machine over observed signals, plus the coordinator CLI.

Health answers one question — "what can this Run prove right now?" — and refuses to
answer a second one. Its five states are ``healthy``, ``degraded``, ``blocked``,
``lost`` and ``unknown``:

* ``healthy``  every expected signal is fresh within its window and no action is required;
* ``degraded`` a non-critical signal is stale, watch is off, or the projection is behind;
  work may continue and the reason is recorded;
* ``blocked``  a human or coordinator decision is pending, so nothing advances;
* ``lost``     positive proof the agent exited without a ``worker_done``;
* ``unknown``  evidence is absent. It is neither healthy nor failed; the next step is
  evidence collection.

Aggregation is worst-of with the precedence ``lost > blocked > unknown > degraded >
healthy``. An unobserved signal is ``unknown``, never ``healthy``. Release is not a health
state: health alone never authorizes it.

The module is importable and pure at its core: ``evaluate``, ``transition``,
``authorize_release`` and ``authorize_retry`` do no I/O and read no clock (every age is
computed from the snapshot's own ``observed_at``). The CLI is the coordinator's surface,
not a test hook: ``health evaluate`` reads a signals snapshot (or one inbox observation,
``--observation``), writes the ``kind: health`` ledger record through the ledger module's
append interface, and prints the machine result.

``observation_signals`` translates historical inbox observations for audit. The old
consumer is retired; current signals must come from independently verified evidence,
and missing evidence aggregates as ``unknown``.

Defaults are configurable, never product constants. In particular the heartbeat window is
the live preamble cadence × 2 plus a grace; without a cadence the heartbeat signal is
``unknown`` rather than a guessed five minutes.
"""

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys
import uuid

_COMMON_PATH = Path(__file__).resolve().with_name("_common.py")
_COMMON_SPEC = importlib.util.spec_from_file_location("orca_health_common", _COMMON_PATH)
assert _COMMON_SPEC is not None and _COMMON_SPEC.loader is not None
_COMMON = importlib.util.module_from_spec(_COMMON_SPEC)
_COMMON_SPEC.loader.exec_module(_COMMON)

SCHEMA_VERSION = 1
CLI_NAME = "health.py"

#: The signal/TTL table. Every row is evaluated by ``evaluate``; an absent row is
#: ``unknown`` with its own evidence, never ``healthy``.
SIGNALS = (
    "heartbeat",
    "inbox",
    "unacked",
    "worker_list",
    "worker_show",
    "turnStart",
    "progress",
    "last_effective_progress",
    "reclaimable",
    "waiter_slot",
    "nested_roster",
)

#: Signals required in strict/legacy consumer mode vs active native-wake mode.
LEGACY_CONSUMER_SIGNALS = ("inbox", "unacked", "waiter_slot")
NATIVE_REQUIRED_SIGNALS = (
    "worker_list", "worker_show", "turnStart", "last_effective_progress", "reclaimable",
)
HEALTH_MODES = ("strict", "native")
PENDING_DIRNAME = "health-pending"
#: ``watch`` is a local switch rather than an Orca observation, but its absence is still
#: evidence absent: the evaluator must not assume watching is on.
REQUIRED_INPUTS = SIGNALS + ("watch",)

#: ``pending`` is optional input: a recorded decision item is the only source of the
#: ``any -> blocked`` transition for a decision that leaves no other signal.
OPTIONAL_INPUTS = ("pending", "binding")

#: What counts as **effective** progress: a non-heartbeat message, an artifact change, a
#: commit or a stage change. A heartbeat is liveness, never progress. The historical inbox loop and this
#: module share the vocabulary so the observation it writes means the same thing here.
PROGRESS_MARKERS = ("message", "artifact", "commit", "stage")

#: The subset a consumer can observe on its own: a delivered batch and a changed worker
#: stage. Artifact and commit changes belong to the coordinator's ledger, not to the loop.
CONSUMER_MARKERS = ("message", "stage")

#: The two binding losses the specification names. They stop consumption and lifecycle
#: operations for that identity; they never authorize a retry by themselves.
BINDING_OUTCOMES = ("consumer_fenced", "run_binding_lost")

#: What a binding loss refuses, whatever the recovery path: borrowing another handle and
#: creating a Run without proving the old one ended. Both are published in the finding.
BINDING_REFUSALS = ("impersonate_handle", "create_new_run")

#: ``ownership`` is the fact that decides whether the loss is blocked or unknown. A caller
#: that recorded the loss but never verified who really owns the binding has not observed it.
BINDING_OWNERSHIP = ("verified", "unproven")

STATES = ("healthy", "degraded", "blocked", "lost", "unknown")
STATE_RANK = {"healthy": 0, "degraded": 1, "unknown": 2, "blocked": 3, "lost": 4}

#: A finding that changes no state: progress projection staleness is recorded, not graded.
GAP = "gap"

#: When several findings name an action, the most specific one wins.
ACTION_PRECEDENCE = (
    "escalate",
    "coordinator_retry_decision",
    "record_pending",
    "clear_waiter_slot",
    "poll_worker_list",
    "collect_evidence",
    "continue",
    "none",
)

STATE_ACTION = {
    "healthy": "none",
    "degraded": "continue",
    "blocked": "record_pending",
    "lost": "coordinator_retry_decision",
    "unknown": "collect_evidence",
}

#: Configurable defaults from the spec's signal table. Every value may be overridden
#: through ``evaluate``'s config argument; a test can inject a one-second window.
DEFAULT_CONFIG = {
    "heartbeat_grace_seconds": 60,
    "inbox_stale_seconds": 900,
    "observation_ttl_seconds": 300,
    "turn_start_window_seconds": 120,
    "turn_start_escalate_seconds": 600,
    "progress_freshness_seconds": 300,
    "stall_rounds": 3,
    "reclaimable_stale_seconds": 600,
}

#: Terminal worker statuses that prove the agent is gone, not merely quiet.
#: ``succeeded``/``stopped``/``abandoned`` are the live Orca task outcomes of a settle:
#: the worker stopped, so the only question left is whether ``worker_done`` was seen.
#: ``released``/``retained``/``reclaimable``/``release_pending``/``release_unknown`` are
#: terminal *resource* states that outlive the process and are never read as termination.
TERMINATED_STATUSES = (
    "exited", "terminated", "dead", "succeeded", "stopped", "abandoned",
)
#: Statuses that prove the worker is alive; a waiting worker is alive, not failed.
LIVE_STATUSES = ("running", "ready", "waiting", "idle", "working", "active", "started")
#: A worker whose own process failed. It keeps the failure direction (:func:`_worker_show`
#: reports ``lost``) instead of becoming healthy or silently ``unknown``.
FAILED_STATUSES = ("failed",)
#: Process states the live projection reports in ``state`` for a superseded lifecycle.
#: A ``cancelled`` worker did not succeed, so it is graded like a failure, not a settle.
CANCELLED_STATUSES = ("cancelled", "canceled")

#: Inbox phases that are expected to keep moving; ``error`` is immediate.
INBOX_LIVE_PHASES = ("waiting", "checking", "backoff")

#: Decision classes that put the Run in ``blocked`` until a human or coordinator settles
#: them (one-way door, escalation, approval, head-of-line batch, recorded pending item).
PENDING_CLASSES = (
    "decision", "one_way_door", "escalation", "approval", "head_of_line", "pending",
)

#: The ledger caps one record at 64 KiB; the health record embeds the snapshot, so the
#: snapshot is bounded well below that to leave room for the findings.
MAX_SNAPSHOT_BYTES = 32 * 1024

RFC3339_UTC = "%Y-%m-%dT%H:%M:%SZ"

#: Health records are retried from memory when the ledger write fails; the coordinator
#: never blocks orchestration on a health-record write.
_PENDING_RECORDS = []


def _invalid(message):
    raise ValueError(message)


def _parse(value, field):
    """Parse an RFC3339 UTC instant; an unreadable value is evidence absent, not a crash."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.strptime(value, RFC3339_UTC)
    except ValueError:
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _age(now, value):
    """Seconds between the observation instant and a signal's own timestamp."""
    if now is None:
        return None
    moment = _parse(value, "signal timestamp")
    if moment is None:
        return None
    return max(0.0, (now - moment).total_seconds())


def _finding(signal, state, reason, evidence="", detail=None, next_action=None):
    item = {
        "signal": signal,
        "state": state,
        "reason": reason,
        "evidence": evidence,
    }
    if detail:
        item["detail"] = detail
    if next_action:
        item["next_action"] = next_action
    return item


def _absent(signal, evidence="", reason="not_observed"):
    return _finding(signal, "unknown", reason, evidence or "signals." + signal + " missing")


def _block(snapshot, name):
    """Return a signal block, or ``None`` when it was not observed."""
    value = snapshot.get(name)
    if not isinstance(value, dict) or not value:
        return None
    return value


def _observed_at(value, key="observed_at"):
    return value.get(key)


def _ttl_finding(signal, snapshot, now, config, age=None):
    """Shared shape for a signal whose only failure mode is a stale observation."""
    block = _block(snapshot, signal)
    if block is None:
        return _absent(signal)
    ttl = config["observation_ttl_seconds"]
    measured = _age(now, _observed_at(block)) if age is None else age
    if measured is None:
        return _finding(
            signal, "unknown", "no_observation_time",
            "signals." + signal + ".observed_at is missing or unreadable",
        )
    if measured > ttl:
        return _finding(
            signal, "degraded", "stale_observation",
            "signals." + signal + " is " + str(int(measured)) + "s old, TTL " + str(ttl) + "s",
            detail={"age_seconds": int(measured), "ttl_seconds": ttl},
        )
    return None


def _heartbeat(snapshot, config, now):
    block = _block(snapshot, "heartbeat")
    if block is None:
        return _absent("heartbeat")
    cadence = block.get("cadence_seconds")
    if type(cadence) is not int or cadence <= 0:
        # The live preamble supplied no cadence. Never invent a threshold.
        return _finding(
            "heartbeat", "unknown", "no_preamble_cadence",
            "signal preamble cadence absent; the heartbeat is not evaluated",
        )
    note = _observed_at(block, "last_heartbeat_at") or _observed_at(block)
    measured = _age(now, note)
    if measured is None:
        return _finding(
            "heartbeat", "unknown", "no_heartbeat_time",
            "signals.heartbeat.last_heartbeat_at is missing or unreadable",
        )
    window = cadence * 2 + config["heartbeat_grace_seconds"]
    if measured > window:
        liveness = block.get("liveness")
        if liveness == "positive":
            return _finding(
                "heartbeat", "degraded", "heartbeat_stale",
                "last heartbeat " + str(int(measured)) + "s ago, window " + str(window) + "s",
                detail={"age_seconds": int(measured), "window_seconds": window},
            )
        return _finding(
            "heartbeat", "unknown", "heartbeat_stale_without_liveness",
            "last heartbeat " + str(int(measured)) + "s ago and liveness is unproven",
            detail={"age_seconds": int(measured), "window_seconds": window},
        )
    return None


def _inbox(snapshot, config, now):
    block = _block(snapshot, "inbox")
    if block is None:
        return _absent("inbox")
    phase = block.get("phase")
    if phase == "error":
        return _finding(
            "inbox", "blocked", "inbox_error",
            "signals.inbox.phase is error" + (
                ": " + str(block["error"]) if block.get("error") else ""),
        )
    if phase not in INBOX_LIVE_PHASES:
        return _finding(
            "inbox", "unknown", "unknown_inbox_phase",
            "signals.inbox.phase " + repr(phase) + " is not a known phase",
        )
    measured = _age(now, _observed_at(block, "updated_at"))
    if measured is None:
        return _finding(
            "inbox", "unknown", "no_observation_time",
            "signals.inbox.updated_at is missing or unreadable",
        )
    ttl = config["inbox_stale_seconds"]
    if measured > ttl:
        return _finding(
            "inbox", "degraded", "inbox_stale",
            "inbox phase " + phase + " has not advanced for " + str(int(measured)) + "s",
            detail={"age_seconds": int(measured), "ttl_seconds": ttl},
        )
    return None


def _unacked(snapshot, config, now):
    block = _block(snapshot, "unacked")
    if block is None:
        return _absent("unacked")
    count = block.get("unacked")
    if type(count) is not int or count < 0:
        return _finding(
            "unacked", "unknown", "unreadable_count",
            "signals.unacked.unacked is not a count",
        )
    if count > 0:
        return _finding(
            "unacked", "blocked", "unacked_batch",
            str(count) + " delivery batch(es) saved but not acknowledged",
            detail={"unacked": count},
        )
    return None


def _worker_list(snapshot, config, now):
    """A stale worker-list matters only while workers are expected to be active."""
    block = _block(snapshot, "worker_list")
    if block is None:
        return _absent("worker_list")
    active = block.get("active_workers")
    if type(active) is not int or active < 0:
        return _finding(
            "worker_list", "unknown", "unreadable_count",
            "signals.worker_list.active_workers is not a count",
        )
    if active == 0:
        return None
    return _ttl_finding("worker_list", snapshot, now, config)


def _worker_show(snapshot, config, now):
    block = _block(snapshot, "worker_show")
    if block is None:
        return _absent("worker_show")
    status = block.get("status")
    if status in FAILED_STATUSES or status in CANCELLED_STATUSES:
        # A failed worker keeps the failure direction: the agent is gone and its own run
        # did not complete, so this is never healthy. It is not reported as a settle.
        return _finding(
            "worker_show", "lost", "agent_failed_without_worker_done",
            "status " + status + " with no worker_done",
            detail={"status": status},
        )
    if status in TERMINATED_STATUSES:
        if block.get("worker_done") is True:
            return None
        return _finding(
            "worker_show", "lost", "agent_exited_without_worker_done",
            "status " + status + " with no worker_done",
            detail={"status": status},
        )
    if status not in LIVE_STATUSES:
        return _finding(
            "worker_show", "unknown", "unknown_worker_status",
            "signals.worker_show.status " + repr(status) + " is not a known status",
        )
    stale = _ttl_finding("worker_show", snapshot, now, config)
    if stale is not None:
        return stale
    # ``agentWait`` is read defensively. A present ``null`` is a positive observation:
    # Orca looked and found no wait. A record means the worker is waiting on a decision,
    # which blocks. An absent field means Orca never looked, so it is unknown.
    if "agent_wait" not in block:
        return _finding(
            "worker_show", "unknown", "agent_wait_unobserved",
            "worker-show returned no agentWait field; whether the worker waits is unknown",
        )
    wait = block["agent_wait"]
    if wait is None:
        return None
    if isinstance(wait, dict):
        return _finding(
            "worker_show", "blocked", "agent_waiting",
            str(wait.get("prompt") or "the worker is waiting on a decision"),
            detail={"agent_wait": wait},
        )
    return _finding(
        "worker_show", "unknown", "unreadable_agent_wait",
        "signals.worker_show.agent_wait is neither null nor an object",
    )


def _turn_start(snapshot, config, now):
    block = _block(snapshot, "turnStart")
    if block is None:
        return _absent("turnStart")
    status = block.get("status")
    if status == "observed":
        # ``observed`` is evidence the turn began; the timestamp only records when.
        return None
    # An unproven start is not a failure inside the window: gather the evidence the agent
    # supports. Past the escalation threshold it is still unknown, but the next step becomes
    # a human or coordinator escalation instead of more quiet collection.
    dispatched = _age(now, _observed_at(block, "dispatched_at"))
    escalate = config["turn_start_escalate_seconds"]
    if dispatched is not None and dispatched >= escalate:
        return _finding(
            "turnStart", "unknown", "turn_start_unproven",
            "turnStart status " + repr(status) + " for " + str(int(dispatched))
            + "s; the dispatch is past the escalation window",
            detail={"age_seconds": int(dispatched), "escalate_seconds": escalate},
            next_action="escalate",
        )
    return _finding(
        "turnStart", "unknown", "turn_start_unproven",
        "turnStart status " + repr(status) + "; a ready Dispatch is not a started turn",
        detail={"age_seconds": int(dispatched) if dispatched is not None else None,
                "window_seconds": config["turn_start_window_seconds"]},
    )


def _progress(snapshot, config, now):
    block = _block(snapshot, "progress")
    if block is None:
        return _absent("progress")
    measured = _age(now, _observed_at(block, "updated_at"))
    if measured is None:
        return _finding(
            "progress", "unknown", "no_observation_time",
            "signals.progress.updated_at is missing or unreadable",
        )
    ttl = config["progress_freshness_seconds"]
    if measured > ttl:
        # The projection is not authority: a stale projection is an observability gap.
        return _finding(
            "progress", GAP, "progress_stale",
            "progress.json is " + str(int(measured))
            + "s old; this is a projection, not authority",
            detail={"age_seconds": int(measured), "ttl_seconds": ttl},
        )
    return None


def _last_effective_progress(snapshot, config, now):
    """Stall detection: non-heartbeat progress only, plus the consumer's unacked count.

    ``rounds_without_progress`` is the counter the coordinator or the inbox observation
    advances. ``markers`` names what progressed since the previous round; a heartbeat is
    liveness, never progress, so it is listed in ``ignored_markers`` and changes nothing. A
    ``mode: consumer`` block also has to carry the ``--status --json`` ``unacked`` count:
    empty waits are invisible to a consumer, so the count is part of the stall signal, and a
    block that omits it is evidence absent rather than a healthy scan.
    """
    block = _block(snapshot, "last_effective_progress")
    if block is None:
        return _absent("last_effective_progress")
    rounds = block.get("rounds_without_progress")
    if type(rounds) is not int or rounds < 0:
        return _finding(
            "last_effective_progress", "unknown", "unreadable_count",
            "signals.last_effective_progress.rounds_without_progress is not a count",
        )
    mode = block.get("mode", "coordinator")
    if mode not in ("consumer", "coordinator"):
        return _finding(
            "last_effective_progress", "unknown", "unknown_progress_mode",
            "signals.last_effective_progress.mode " + repr(mode) + " is not a known mode",
        )
    detail = {"rounds_without_progress": rounds, "limit": config["stall_rounds"]}
    markers = block.get("markers", [])
    if not isinstance(markers, list) or any(not isinstance(item, str) for item in markers):
        return _finding(
            "last_effective_progress", "unknown", "unreadable_markers",
            "signals.last_effective_progress.markers is not a list of marker names",
        )
    detail["markers"] = [item for item in markers if item in PROGRESS_MARKERS]
    ignored = [item for item in markers if item not in PROGRESS_MARKERS]
    if ignored:
        detail["ignored_markers"] = ignored
    if "unacked" in block:
        unacked = block["unacked"]
        if type(unacked) is not int or unacked < 0:
            return _finding(
                "last_effective_progress", "unknown", "unreadable_count",
                "signals.last_effective_progress.unacked is not a count",
            )
        detail["unacked"] = unacked
    if "empty_waits" in block:
        empty_waits = block["empty_waits"]
        if type(empty_waits) is not int or empty_waits < 0:
            return _finding(
                "last_effective_progress", "unknown", "unreadable_count",
                "signals.last_effective_progress.empty_waits is not a count",
            )
        detail["empty_waits"] = empty_waits
    if mode == "consumer" and "unacked" not in detail:
        return _finding(
            "last_effective_progress", "unknown", "consumer_unacked_missing",
            "a consumer-mode stall block must carry the --status --json unacked count; "
            "empty waits are invisible to a consumer",
            detail=detail,
        )
    limit = config["stall_rounds"]
    if rounds >= limit:
        return _finding(
            "last_effective_progress", "degraded", "no_effective_progress",
            str(rounds) + " rounds without non-heartbeat progress",
            detail=detail,
        )
    return None


def _reclaimable(snapshot, config, now):
    block = _block(snapshot, "reclaimable")
    if block is None:
        return _absent("reclaimable")
    terminals = block.get("terminals")
    if not isinstance(terminals, list):
        return _finding(
            "reclaimable", "unknown", "unreadable_terminals",
            "signals.reclaimable.terminals is not a list",
        )
    limit = config["reclaimable_stale_seconds"]
    stale = []
    for entry in terminals:
        if not isinstance(entry, dict):
            return _finding(
                "reclaimable", "unknown", "unreadable_terminals",
                "signals.reclaimable.terminals holds a value that is not an object",
            )
        measured = _age(now, entry.get("settled_at") or entry.get("observed_at"))
        if measured is None:
            return _finding(
                "reclaimable", "unknown", "no_observation_time",
                "a reclaimable terminal has no readable age",
            )
        if measured > limit:
            stale.append(int(measured))
    if stale:
        return _finding(
            "reclaimable", "degraded", "reclaimable_terminal_stale",
            str(len(stale)) + " reclaimable terminal(s) older than " + str(limit) + "s",
            detail={"stale": stale, "limit_seconds": limit},
        )
    return None


def _waiter_slot(snapshot, config, now):
    block = _block(snapshot, "waiter_slot")
    if block is None:
        return _absent("waiter_slot")
    leaked = block.get("leaked")
    if not isinstance(leaked, bool):
        return _finding(
            "waiter_slot", "unknown", "unreadable_leak",
            "signals.waiter_slot.leaked is not a boolean",
        )
    if not leaked:
        return None
    if block.get("blocks_install") is True:
        return _finding(
            "waiter_slot", "blocked", "waiter_slot_leak",
            "a leaked CLI waiter keeps the slot and blocks reinstall",
            next_action="clear_waiter_slot",
        )
    return _finding(
        "waiter_slot", "degraded", "waiter_slot_leak",
        "a leaked CLI waiter slot was observed",
        next_action="clear_waiter_slot",
    )


def _nested_roster(snapshot, config, now):
    block = _block(snapshot, "nested_roster")
    if block is None:
        return _absent("nested_roster")
    applicable = block.get("applicable")
    if not isinstance(applicable, bool):
        return _finding(
            "nested_roster", "unknown", "unreadable_applicability",
            "signals.nested_roster.applicable is not a boolean",
        )
    if not applicable:
        return None
    report = block.get("report")
    if not isinstance(report, dict) or not report:
        return _finding(
            "nested_roster", "unknown", "roster_unreported",
            "a long-horizon ticket has not reported its nested roster yet",
        )
    return None


def _watch(snapshot, config, now):
    block = _block(snapshot, "watch")
    if block is None:
        return _absent("watch")
    enabled = block.get("enabled")
    if not isinstance(enabled, bool):
        return _finding(
            "watch", "unknown", "unreadable_watch",
            "signals.watch.enabled is not a boolean",
        )
    if not enabled:
        return _finding(
            "watch", "degraded", "watch_disabled",
            "WORKER_WATCH=0: wakes are not observed; poll worker-list periodically",
            next_action="poll_worker_list",
        )
    return None


def _binding(snapshot, config, now):
    """The binding-loss protocol, as an optional recorded input.

    ``consumer_fenced`` or a lost Run binding stops consumption and lifecycle operations for
    that identity, so it is never healthy. What decides between ``blocked`` and ``unknown``
    is ownership: a fenced consumer is a positive stop signal (Orca itself refused the
    identity), while a bare ``run_binding_lost`` without a verified owner leaves the question
    of whose binding this is unobserved. Either way the finding publishes the two refusals —
    never impersonate a handle, never create a Run unconditionally — and an ownership that
    cannot be established escalates. An absent block is not an observation, so it adds nothing.
    """
    block = snapshot.get("binding")
    if block is None:
        return None
    if not isinstance(block, dict):
        _invalid("binding must be an object")
    outcome = block.get("outcome", "none")
    if outcome == "none":
        return None
    detail = {"outcome": outcome, "refused": list(BINDING_REFUSALS)}
    if not isinstance(outcome, str) or outcome not in BINDING_OUTCOMES:
        return _finding(
            "binding", "unknown", "unknown_binding_outcome",
            "binding.outcome " + repr(outcome) + " is not a recorded binding loss",
            detail=detail, next_action="escalate",
        )
    ownership = block.get("ownership")
    detail["ownership"] = ownership
    if "evidence" in block:
        detail["evidence"] = block["evidence"]
    if not isinstance(ownership, str) or ownership not in BINDING_OWNERSHIP:
        return _finding(
            "binding", "unknown", "ownership_unobserved",
            "the binding loss was recorded without verifying the real caller and Run/Dispatch "
            "ownership, so the next step is evidence collection and escalation",
            detail=detail, next_action="escalate",
        )
    if outcome == "run_binding_lost" and ownership != "verified":
        return _finding(
            "binding", "unknown", "ownership_unproven",
            "ownership of the binding was not verified; do not reclaim it from a guess",
            detail=detail, next_action="escalate",
        )
    escalate = outcome == "consumer_fenced" and ownership != "verified"
    return _finding(
        "binding", "blocked", "binding_loss",
        "binding outcome " + outcome + ": stop consuming and stop lifecycle operations for "
        "this identity, preserve the evidence, and verify the real caller and ownership",
        detail=detail,
        next_action="escalate" if escalate else "record_pending",
    )


def _pending(snapshot, config, now):
    """An optional recorded decision item; its absence is not an observation."""
    block = snapshot.get("pending")
    if block is None:
        return None
    if not isinstance(block, dict):
        _invalid("pending must be an object")
    items = block.get("items", [])
    if not isinstance(items, list):
        _invalid("pending.items must be a list")
    blocked = []
    for item in items:
        if not isinstance(item, dict) or item.get("class") not in PENDING_CLASSES:
            _invalid("pending items need one of the classes: " + ", ".join(PENDING_CLASSES))
        blocked.append(str(item["class"]))
    if blocked:
        return _finding(
            "pending", "blocked", "pending_decision",
            "pending decision item(s): " + ", ".join(sorted(blocked)),
            detail={"classes": sorted(blocked)},
        )
    return None


SIGNAL_RULES = {
    "heartbeat": _heartbeat,
    "inbox": _inbox,
    "unacked": _unacked,
    "worker_list": _worker_list,
    "worker_show": _worker_show,
    "turnStart": _turn_start,
    "progress": _progress,
    "last_effective_progress": _last_effective_progress,
    "reclaimable": _reclaimable,
    "waiter_slot": _waiter_slot,
    "nested_roster": _nested_roster,
    "watch": _watch,
}


def _config(overrides=None):
    if overrides is None:
        return dict(DEFAULT_CONFIG)
    if not isinstance(overrides, dict):
        _invalid("config must be an object")
    unknown = sorted(set(overrides) - set(DEFAULT_CONFIG))
    if unknown:
        _invalid("unknown config fields: " + ", ".join(unknown))
    merged = dict(DEFAULT_CONFIG)
    for key, value in overrides.items():
        if type(value) is not int or value < 1:
            _invalid("config." + key + " must be a positive integer")
        merged[key] = value
    if merged["turn_start_escalate_seconds"] < merged["turn_start_window_seconds"]:
        _invalid("turn_start_escalate_seconds cannot precede turn_start_window_seconds")
    return merged


def _snapshot(snapshot):
    if not isinstance(snapshot, dict):
        _invalid("signals snapshot must be an object")
    allowed = {
        "schema_version", "run_id", "observed_at", "mode", "long_horizon",
        *REQUIRED_INPUTS, *OPTIONAL_INPUTS,
    }
    unknown = sorted(set(snapshot) - allowed)
    if unknown:
        _invalid("unknown signal fields: " + ", ".join(unknown))
    mode = snapshot.get("mode")
    if mode is not None and mode not in HEALTH_MODES:
        _invalid("signals.mode must be one of " + ", ".join(HEALTH_MODES))
    if "long_horizon" in snapshot and not isinstance(snapshot["long_horizon"], bool):
        _invalid("signals.long_horizon must be a boolean")
    for name in SIGNALS:
        value = snapshot.get(name)
        if value is not None and not isinstance(value, dict):
            _invalid("signals." + name + " must be an object")
    for name in ("watch", "pending", "binding"):
        value = snapshot.get(name)
        if value is not None and not isinstance(value, dict):
            _invalid("signals." + name + " must be an object")
    return snapshot


def _next_action(state, findings):
    candidates = [STATE_ACTION[state]]
    for item in findings:
        if item.get("next_action"):
            candidates.append(item["next_action"])
    for action in ACTION_PRECEDENCE:
        if action in candidates:
            return action
    return STATE_ACTION[state]


def _state(value, field):
    if not isinstance(value, str) or value not in STATES:
        _invalid("unknown " + field + ": " + repr(value))
    return value


def _evaluation(value):
    if not isinstance(value, dict) or value.get("state") not in STATES:
        _invalid("an evaluation must carry one of the states: " + ", ".join(STATES))
    return value


def _transition_result(previous, following, allowed, authorization, reason,
                       decided_by=None):
    return {
        "from": previous,
        "to": following,
        "allowed": allowed,
        "authorization": authorization,
        "reason": reason,
        "decided_by": decided_by,
    }


def transition(previous, evaluation, pending_record=None, decided_by=None, evidence=""):
    """Decide whether the evaluation may replace ``previous``; a pure function.

    The trigger and authorization are the spec's transition table, not a guess:
    ``healthy -> degraded``, ``degraded -> healthy`` and ``any -> unknown`` are rules;
    ``any -> blocked`` needs a recorded decision or pending item; ``blocked -> healthy``
    needs a human or coordinator; ``unknown -> lost`` needs positive termination
    evidence. Anything else is refused with the reason it was refused.
    """
    _state(previous, "health state")
    _evaluation(evaluation)
    following = _state(evaluation["state"], "health state")
    decider = decided_by if isinstance(decided_by, str) and decided_by else None

    if previous == following:
        return _transition_result(previous, following, True, "rule", "no transition")

    if following == "unknown":
        return _transition_result(
            previous, following, True, "rule",
            "evidence is absent, so the state is unknown and the next step is collection",
        )
    if following == "blocked":
        recorded = isinstance(pending_record, dict) and pending_record.get("class")
        if recorded:
            return _transition_result(
                previous, following, True, "record_pending",
                "a decision or pending item is recorded", decider or "coordinator",
            )
        return _transition_result(
            previous, following, False, "record_pending",
            "blocked needs a recorded decision or pending item",
        )
    if previous == "blocked" and following == "healthy":
        if decider in ("human", "coordinator"):
            return _transition_result(
                previous, following, True, "human_or_coordinator",
                "the pending decision was resolved", decider,
            )
        return _transition_result(
            previous, following, False, "human_or_coordinator",
            "only a human or coordinator may clear a blocked Run",
        )
    if following == "lost":
        # The table names ``unknown -> lost``; that is the common path (TTL expiry, then
        # evidence). The authorization is the evidence, not the previous state, so a proven
        # termination is never hidden behind an earlier green state.
        if isinstance(evidence, str) and evidence:
            return _transition_result(
                previous, following, True, "rule_with_evidence",
                "positive termination evidence", "rule",
            )
        return _transition_result(
            previous, following, False, "rule_with_evidence",
            "lost needs positive termination evidence",
        )
    if (previous, following) in (("healthy", "degraded"), ("degraded", "healthy")):
        return _transition_result(
            previous, following, True, "rule",
            "a non-critical signal changed or recovered", "rule",
        )
    return _transition_result(
        previous, following, False, "none",
        "the transition table has no edge from " + previous + " to " + following,
    )


def authorize_retry(state, decided_by=None):
    """Retry after ``lost`` is a coordinator decision inside the human boundary."""
    _state(state, "health state")
    decider = decided_by if isinstance(decided_by, str) and decided_by else None
    result = {
        "from": state,
        "action": "retry",
        "allowed": False,
        "boundary": "human_boundary",
        "decided_by": decider,
        "reason": "",
    }
    if decider in ("coordinator", "human"):
        if state != "lost":
            result["reason"] = "retry applies to a lost Run, not a " + state + " one"
            return result
        result["allowed"] = True
        result["reason"] = "an explicit retry decision was made"
        return result
    result["reason"] = "retry is never a rule; it needs a coordinator or human decision"
    return result


def authorize_release(health_state, settled=None, closeout_record="", decided_by=None):
    """Release needs settlement and a closeout record; health never authorizes it.

    Release is an action, not a health state. A healthy Run releases nothing by itself,
    so ``health_authorized`` is always false and the caller must supply the two
    authority facts. An uncertain settlement escalates to the human.
    """
    _state(health_state, "health state")
    decider = decided_by if isinstance(decided_by, str) and decided_by else None
    record = closeout_record if isinstance(closeout_record, str) else ""
    result = {
        "from": health_state,
        "action": "release",
        "allowed": False,
        "health_authorized": False,
        "settled": settled,
        "closeout_record": record,
        "decided_by": decider or "coordinator",
        "reason": "",
    }
    if settled is None:
        result["decided_by"] = "human"
        result["reason"] = "settlement is unknown; the release decision is the human's"
        return result
    if settled is not True:
        result["reason"] = "the worker is not proven settled; release waits for settlement"
        return result
    if not record:
        result["reason"] = "release needs a durable closeout record"
        return result
    if decider == "human":
        result["reason"] = "the human authorized the release"
    else:
        result["reason"] = "settlement and the closeout record were proven"
    result["allowed"] = True
    return result


def _native_signal_names(snapshot):
    """Signals evaluated in ``native`` mode: active Orca wake contracts only.

    Retired consumer signals (``inbox``, ``unacked``, ``waiter_slot``) are checked only
    when explicitly supplied. ``nested_roster`` is required only when ``long_horizon``
    is true. ``heartbeat`` is checked only when a positive preamble cadence exists.
    ``progress`` and ``watch`` do not turn an active Run ``unknown`` when omitted.
    """
    names = list(NATIVE_REQUIRED_SIGNALS)
    for retired in LEGACY_CONSUMER_SIGNALS:
        if _block(snapshot, retired) is not None:
            names.append(retired)
    if snapshot.get("long_horizon") or _block(snapshot, "nested_roster") is not None:
        names.append("nested_roster")
    heartbeat = _block(snapshot, "heartbeat")
    if heartbeat is not None and type(heartbeat.get("cadence_seconds")) is int and heartbeat["cadence_seconds"] > 0:
        names.append("heartbeat")
    if _block(snapshot, "progress") is not None:
        names.append("progress")
    if _block(snapshot, "watch") is not None:
        names.append("watch")
    return names


def evaluate(signals, config=None, *, mode=None):
    """Evaluate one signals snapshot; a pure function.

    Returns ``{"state", "reasons", "next_action"}``. ``reasons`` holds one entry per
    finding: the signal, the state it contributes, a stable reason code, the evidence and
    any detail. Healthy signals are omitted. Aggregation is worst-of with the precedence
    ``lost > blocked > unknown > degraded > healthy``; a projection gap is recorded but
    never raises the state, because ``progress.json`` is not authority.

    ``mode="native"`` (or ``signals["mode"] == "native"``) evaluates the native-wake
    signal set instead of demanding retired consumer signals.
    """
    snapshot = _snapshot(signals)
    settings = _config(config)
    active_mode = mode or snapshot.get("mode") or "strict"
    if active_mode not in HEALTH_MODES:
        _invalid("mode must be one of " + ", ".join(HEALTH_MODES))
    now = _parse(snapshot.get("observed_at"), "observed_at")
    findings = []
    names = _native_signal_names(snapshot) if active_mode == "native" else (SIGNALS + ("watch",))
    for name in names:
        finding = SIGNAL_RULES[name](snapshot, settings, now)
        if finding is not None:
            findings.append(finding)
    pending = _pending(snapshot, settings, now)
    if pending is not None:
        findings.append(pending)
    binding = _binding(snapshot, settings, now)
    if binding is not None:
        findings.append(binding)

    state = "healthy"
    for item in findings:
        if item["state"] == GAP:
            continue
        if STATE_RANK[item["state"]] > STATE_RANK[state]:
            state = item["state"]
    return {
        "state": state,
        "reasons": findings,
        "next_action": _next_action(state, findings),
    }


# ---------------------------------------------------------------------------
# The inbox observation → signals snapshot seam
# ---------------------------------------------------------------------------
#
# Translate historical consumer observations for audit. The retired loop no longer emits
# them; absent signals remain ``unknown`` and historical state is not current evidence.


def observation_signals(observation):
    """Turn one inbox observation into a signals snapshot; a pure function.

    Observed by the consumer and copied: the observation instant, the ``--status --json``
    unacked arithmetic, the watch switch, the inbox phase when it has one, the recorded
    binding-loss outcome, and the stall counters. Everything else — heartbeat, worker-show,
    worker-list, turnStart, the projection, reclaimable terminals, waiter slots, nested
    roster — is left out so it aggregates as ``unknown`` rather than as a pass.
    """
    if not isinstance(observation, dict):
        _invalid("an inbox observation must be an object")
    observed_at = observation.get("observed_at")
    if _parse(observed_at, "observed_at") is None:
        _invalid("an inbox observation needs a readable observed_at instant")
    signals = {"observed_at": observed_at}
    run_id = observation.get("run")
    if isinstance(run_id, str) and run_id:
        signals["run_id"] = run_id

    unacked = observation.get("unacked")
    if type(unacked) is int and unacked >= 0:
        ids = observation.get("unacked_delivery_ids")
        signals["unacked"] = {
            "unacked": unacked,
            "batches": [item for item in ids if isinstance(item, str)]
            if isinstance(ids, list) else [],
        }

    watch = observation.get("watch")
    if watch in ("on", "off"):
        signals["watch"] = {"enabled": watch == "on"}

    phase = observation.get("phase")
    if phase == "error":
        block = {"phase": "error", "updated_at": observed_at}
        error = observation.get("error")
        if isinstance(error, str) and error:
            block["error"] = error
        signals["inbox"] = block
    elif phase in INBOX_LIVE_PHASES:
        signals["inbox"] = {"phase": phase, "updated_at": observed_at}

    for name in ("last_effective_progress", "binding"):
        value = observation.get(name)
        if isinstance(value, dict):
            signals[name] = value
    return signals


def evaluate_observation(observation, config=None, run_id=None):
    """Evaluate one inbox observation directly; a convenience over the two steps above."""
    signals = observation_signals(observation)
    if run_id is not None and signals.get("run_id") not in (None, run_id):
        _invalid("the inbox observation belongs to another run")
    return evaluate(signals, config)


# ---------------------------------------------------------------------------
# The coordinator surface
# ---------------------------------------------------------------------------
#
# ``health evaluate`` reads one signals snapshot, records the outcome as a ``kind: health``
# ledger record and prints the machine result. The record is written through the ledger
# module's own append interface — the storage rules (fsync, flock, idempotency, retention)
# live there and are not copied here. A write failure keeps the in-memory evaluation and
# prints the record id so the coordinator can retry with ``--record-id``; it never blocks
# orchestration.


def load_ledger(script_dir=None):
    """Import the sibling ``ledger.py`` by path, without touching ``sys.path``."""
    base = Path(script_dir) / "health.py" if script_dir else __file__
    return _COMMON.load_sibling(base, "ledger.py", "orca_health_ledger")


def _stable_record_id(run_id, snapshot, result):
    """A deterministic id: the same observation and verdict is the same record."""
    payload = {
        "run_id": run_id,
        "observed_at": snapshot.get("observed_at"),
        "state": result["state"],
        "next_action": result["next_action"],
        "reasons": result["reasons"],
        "signals": snapshot,
    }
    key = "orca-health:" + json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


def _evidence_refs(result):
    refs = []
    for item in result["reasons"]:
        evidence = item.get("evidence")
        if isinstance(evidence, str) and evidence and evidence not in refs:
            refs.append(evidence)
    return refs


def health_record(run_id, snapshot, result, record_id=None, decided_by="coordinator",
                  entered_at=None, previous_state=None, transition_result=None):
    """Build the ``kind: health`` schema-v1 record for one evaluation.

    ``observed_at`` is when this evaluation ran; ``detail.entered_at`` is when the recorded
    state began, which stays put while the state does not change.
    """
    observed_at = _parse(snapshot.get("observed_at"), "observed_at")
    if observed_at is None:
        _invalid("the signals snapshot needs an observed_at instant")
    moment = observed_at.strftime(RFC3339_UTC)
    instant = entered_at or moment
    decider = decided_by if decided_by in ("human", "coordinator") else "coordinator"
    refs = _evidence_refs(result)
    detail = {
        "health_state": result["state"],
        "entered_at": instant,
        "reason": result["reasons"],
        "signals": snapshot,
        "evidence_refs": refs,
        "decided_by": decider,
        "next_action": result["next_action"],
    }
    if previous_state is not None:
        detail["previous_state"] = previous_state
    if transition_result is not None:
        detail["transition"] = transition_result
    return {
        "schema_version": 1,
        "record_id": record_id or _stable_record_id(run_id, snapshot, result),
        "kind": "health",
        "run_id": run_id,
        "subject": {},
        "action": "health",
        "request_id": "",
        "idempotency": "none",
        "state": "observed",
        "evidence_ref": refs[0] if refs else "",
        "observed_at": moment,
        "actor": decider,
        "detail": detail,
    }


def _projection(run_id, result):
    """The read-only projection a dashboard may render; never the authority."""
    return {
        "run_id": run_id,
        "run": {"health": result["state"], "next_action": result["next_action"]},
    }


def _read_snapshot(path):
    try:
        raw = sys.stdin.read() if path == "-" else Path(path).read_text()
    except OSError as error:
        _invalid("cannot read the signals snapshot: " + str(error))
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as error:
        _invalid("the signals snapshot is not valid JSON: " + str(error))
    return data


def _previous_health(ledger, run_id, home):
    """The last ``kind: health`` record for this Run, or ``None`` when there is none.

    The ledger read is best-effort: an unreadable ledger is treated as "no earlier record"
    rather than failing the evaluation, because the evaluation is the product and the record
    is what gets retried.
    """
    try:
        records = ledger.read(run_id, {"kind": "health"}, home=home)
    except (OSError, TypeError, ValueError):
        return None
    return records[-1] if records else None


def _pending_dir(ledger, ledger_home, run_id):
    home_fn = getattr(ledger, "home_path", None)
    if not callable(home_fn):
        return None
    try:
        home = Path(home_fn(ledger_home))
    except (OSError, TypeError, ValueError):
        return None
    return home / "runs" / run_id / PENDING_DIRNAME


def _persist_pending(ledger, ledger_home, record):
    directory = _pending_dir(ledger, ledger_home, record["run_id"])
    if directory is None:
        return ""
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / f"{record['record_id']}.json"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        return str(path)
    except OSError:
        return ""


def evaluate_to_ledger(run_id, snapshot, config=None, record_id=None,
                       ledger_home=None, ledger_module=None,
                       decided_by=None):
    """Evaluate, then record through the ledger; a write failure is reported, not raised.

    Returns the machine result plus ``record_id``, ``recorded``, the ``previous_state`` this
    evaluation compared against and the ``transition`` it decided. When the state did not
    change the earlier ``entered_at`` is kept, so a run in one state has one entry instant.
    The evaluation itself always survives a failed write: the caller can retry the same
    ``record_id`` later without re-reading anything.
    """
    if snapshot.get("run_id") is not None and snapshot["run_id"] != run_id:
        _invalid("the signals snapshot belongs to another run")
    result = evaluate(snapshot, config)
    if len(json.dumps(snapshot, ensure_ascii=False)) > MAX_SNAPSHOT_BYTES:
        _invalid("the signals snapshot is too large to record")
    ledger = ledger_module or load_ledger()
    previous = _previous_health(ledger, run_id, ledger_home)
    previous_state = previous["detail"].get("health_state") if previous else None
    if previous_state not in STATES:
        # A health record from another producer may not carry a readable state. It is not
        # evidence of a transition, so this evaluation starts a fresh chain instead of
        # failing on an unreadable predecessor.
        previous = None
        previous_state = None
    entered_at = None
    transition_result = None
    if previous is not None:
        # Every blocked finding is itself a recorded observation (a pending item, an unacked
        # batch, an inbox error, a waiting agent or a slot that blocks the install), so the
        # snapshot supplies the record the transition requires.
        blocked_reason = next(
            (item for item in result["reasons"] if item["state"] == "blocked"), None)
        refs = _evidence_refs(result)
        transition_result = transition(
            previous_state, result,
            pending_record=({"class": "pending", "reason": blocked_reason["reason"]}
                            if blocked_reason else None),
            decided_by=decided_by,
            evidence=refs[0] if refs else "",
        )
        if result["state"] == previous_state:
            entered_at = previous["detail"].get("entered_at")
    record = health_record(
        run_id, snapshot, result, record_id=record_id, decided_by=decided_by,
        entered_at=entered_at, previous_state=previous_state,
        transition_result=transition_result,
    )
    recorded, error, retry_path = False, "", ""
    try:
        ledger.append(record, home=ledger_home)
        recorded = True
    except (OSError, ValueError) as failure:
        error = str(failure)
        _PENDING_RECORDS.append(record)
        retry_path = _persist_pending(ledger, ledger_home, record)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "record_id": record["record_id"],
        "recorded": recorded,
        "state": result["state"],
        "reasons": result["reasons"],
        "next_action": result["next_action"],
        "entered_at": record["detail"]["entered_at"],
        "previous_state": previous_state,
        "transition": transition_result,
        "projection": _projection(run_id, result),
    }
    if error:
        payload["record_error"] = error
    if retry_path:
        payload["retry_path"] = retry_path
    return payload


def retry_pending(ledger_home=None, ledger_module=None, run_id=None):
    """Retry every failed health record in memory and any persisted ``health-pending`` files."""
    ledger = ledger_module or load_ledger()
    retried, still = [], []
    for record in list(_PENDING_RECORDS):
        try:
            ledger.append(record, home=ledger_home)
        except (OSError, ValueError):
            still.append(record["record_id"])
            continue
        retried.append(record["record_id"])
        _PENDING_RECORDS.remove(record)
    if run_id:
        directory = _pending_dir(ledger, ledger_home, run_id)
        if directory is not None and directory.is_dir() and not directory.is_symlink():
            already_retried = set(retried)
            for path in sorted(directory.glob("*.json")):
                if path.stem in already_retried:
                    try:
                        path.unlink()
                    except OSError:
                        pass
                    continue
                try:
                    record = json.loads(path.read_text())
                    record_id = ledger.append(record, home=ledger_home)
                except (OSError, ValueError, TypeError):
                    still.append(path.stem)
                    continue
                path.unlink()
                if record_id not in already_retried:
                    retried.append(record_id)
                    already_retried.add(record_id)
    return {"retried": retried, "still_pending": still}


def _orca_json(orca_cli, argv, runner=None):
    cmd = [orca_cli, *argv]
    result = runner(cmd) if runner else _COMMON.run_command(cmd)
    if result.get("returncode", 1) != 0:
        return None
    envelope = _COMMON.json_object(result.get("stdout", ""))
    if envelope is None or envelope.get("ok") is False:
        return None
    inner = envelope.get("result", envelope)
    return inner if isinstance(inner, dict) else None


#: Marker for "the source never told us": distinct from ``None``, which is a positive
#: observation that Orca looked and found no wait.
_UNOBSERVED = object()

#: The two fields every live ``worker-show`` record carries regardless of state. Their
#: presence identifies a host whose absent ``waitingOnHuman`` is a positive no-wait; a
#: record without them is treated as an older host, where absence stays unobserved.
_LIVE_SHAPE_FIELDS = frozenset(("stage", "liveness"))


def _first_text(row, *keys):
    """The first key carrying a non-empty string; the documented field-read priority."""
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return ""


def _process_state(row):
    """The worker's own process/task state, read from the live projection first.

    Orca 1.4.263 publishes the task outcome as ``state`` and keeps terminal/process
    accounting in a separate ``terminalState``. ``terminalState`` proves nothing about
    whether the worker is still running (a live worker was measured ``retained``), so the
    process state is read from ``state`` first and the pre-1.4.263 keys stay as fallbacks
    for older hosts.
    """
    return _first_text(row, "state", "status", "workerState", "worker_state")


def _terminal_state(row):
    """The terminal resource state, which is accounting rather than a process signal."""
    return _first_text(row, "terminalState", "worker_state", "workerState")


def _worker_status(worker):
    """The worker-show status, preferring a process signal over terminal accounting.

    A live row is ``state=ready, terminalState=retained``; reading ``terminalState`` as
    the status would misreport a live worker, so terminal accounting is consulted only
    when no process signal exists at all.
    """
    return _process_state(worker) or _terminal_state(worker) or "unknown"


def _worker_done(worker):
    """Whether a completion signal was observed; an absent field stays unknown.

    The live projection has no ``worker_done``/``settled`` field at all: ``state:
    succeeded`` *is* the completion record, so the collector translates it into the
    signal vocabulary the evaluator already understands. Without that translation every
    real worker would read as terminated-without-completion, which is the same silently
    dead signal the status enums caused, only inverted. ``stopped``/``abandoned`` and
    ``failed`` deliberately do not map to a completion.
    """
    for key in ("workerDone", "worker_done"):
        if isinstance(worker.get(key), bool):
            return worker[key]
    if isinstance(worker.get("settled"), bool):
        return worker["settled"]
    if _process_state(worker) == "succeeded":
        return True
    return None


def _waiting_on_human(worker):
    """The three-way human-wait observation, or ``_UNOBSERVED`` when Orca never looked.

    The concise ``--json`` view carries ``waitingOnHuman``: a string names the wait,
    ``"unknown"`` means Orca never evaluated it, and an absent field means Orca evaluated
    it and found no wait. ``observation.agentWait`` (``--full`` only) and the legacy
    ``agentWait``/``agent_wait`` remain fallbacks for older hosts. A present ``null`` is a
    positive no-wait observation; a present string other than ``"unknown"`` is a wait.
    Only those two types are recognized: any other value is a present-but-unreadable
    field and stays unobserved rather than reading as "no wait".
    """
    if "waitingOnHuman" in worker:
        value = worker["waitingOnHuman"]
        if isinstance(value, str):
            if not value.strip() or value.strip().lower() == "unknown":
                return _UNOBSERVED
            return {"waiting": True, "waiting_on_human": value}
        if value is None:
            return None
        # Only a string or null is a recognized observation. Any other type is a field
        # that exists but cannot be read, so it proves nothing and must not fall through
        # to the live-shape branch and read as a positive no-wait.
        return _UNOBSERVED
    observation = worker.get("observation")
    if isinstance(observation, dict) and "agentWait" in observation:
        return _agent_wait(observation["agentWait"])
    for key in ("agentWait", "agent_wait"):
        if key in worker:
            return _agent_wait(worker[key])
    # No wait field at all. On the live projection the concise view omits
    # ``waitingOnHuman`` only after evaluating it, so absence there is a positive no-wait.
    # A record without the live markers is an older host, where an absent ``agentWait``
    # stays unobserved rather than being read as "not waiting".
    if _LIVE_SHAPE_FIELDS.intersection(worker):
        return None
    return _UNOBSERVED


def _agent_wait(value):
    """Normalize an ``agentWait`` value: ``null`` is no wait, an object is a wait."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if value is True:
        return {"waiting": True}
    return _UNOBSERVED


def collect_signals(run_id, *, dispatch_id=None, progress_dir=None, long_horizon=False,
                    rounds_without_progress=0, ledger_home=None, ledger_module=None,
                    orca_cli=None, runner=None, observed_at=None):
    """Collect a native-wake signals snapshot from Orca CLI, ledger, and optional progress.

    Unreachable sources are left omitted rather than fabricated so ``evaluate`` reports
    them honestly as ``unknown``.
    """
    now = observed_at or _COMMON.utc_now()
    cli = orca_cli or _COMMON.resolve_orca_cli()
    ledger = ledger_module or load_ledger()
    signals = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "observed_at": now,
        "mode": "native",
        "long_horizon": bool(long_horizon),
        "watch": {"enabled": os.environ.get("WORKER_WATCH", "1") != "0"},
    }

    try:
        records, _unknown = ledger.effective_records(run_id, home=ledger_home)
    except (OSError, ValueError):
        records = []

    worker_list_result = _orca_json(
        cli, ["orchestration", "worker-list", "--run", run_id, "--include-remote", "--json"],
        runner=runner,
    )
    workers = []
    if worker_list_result is not None and isinstance(worker_list_result.get("workers"), list):
        workers = [row for row in worker_list_result["workers"] if isinstance(row, dict)]
        active = [row for row in workers if _process_state(row) in LIVE_STATUSES]
        signals["worker_list"] = {"observed_at": now, "active_workers": len(active)}
        reclaimable = []
        for row in workers:
            # Terminal accounting, not the process state: ``reclaimable`` describes the
            # terminal resource, which outlives the worker that used it.
            if _terminal_state(row) == "reclaimable":
                reclaimable.append({"observed_at": row.get("settledAt") or now})
        signals["reclaimable"] = {"terminals": reclaimable}

    target_dispatch = dispatch_id
    if not target_dispatch and workers:
        # ``worker-list`` is newest first, so the first row is the current Dispatch.
        target_dispatch = workers[0].get("dispatchId") or workers[0].get("dispatch_id")
    if target_dispatch:
        wshow = _orca_json(
            cli, ["orchestration", "worker-show", "--dispatch", target_dispatch, "--json"],
            runner=runner,
        )
        if wshow is not None:
            worker = wshow.get("worker", wshow)
            if isinstance(worker, dict) and isinstance(worker.get("worker"), dict):
                worker = worker["worker"]
            if isinstance(worker, dict):
                signals["worker_show"] = {
                    "observed_at": now,
                    "status": _worker_status(worker),
                    "worker_done": _worker_done(worker),
                }
                waiting = _waiting_on_human(worker)
                if waiting is not _UNOBSERVED:
                    signals["worker_show"]["agent_wait"] = waiting
                if worker.get("turnStart") == "observed" or worker.get("turn_start") == "observed":
                    signals["turnStart"] = {"status": "observed", "observed_at": now}

    if "turnStart" not in signals:
        last_dispatch = None
        for record in reversed(records):
            if record.get("action") == "worker-start" and record.get("kind") in ("result", "intent"):
                detail = record.get("detail") or {}
                raw_receipt = detail.get("raw_receipt") or {}
                if raw_receipt.get("turnStart") == "observed" or detail.get("turn_start") == "observed":
                    signals["turnStart"] = {"status": "observed", "observed_at": record["observed_at"]}
                    break
                if last_dispatch is None:
                    last_dispatch = record["observed_at"]
        if "turnStart" not in signals and last_dispatch is not None:
            signals["turnStart"] = {"status": "unobserved", "dispatched_at": last_dispatch}

    markers = []
    for record in records[-5:]:
        if record.get("kind") in ("result", "acceptance", "closeout"):
            markers.append("message")
            break
    signals["last_effective_progress"] = {
        "mode": "coordinator",
        "rounds_without_progress": rounds_without_progress,
        "markers": markers,
    }

    pending_items = []
    resolved_ids = {
        (rec.get("detail") or {}).get("pending_record_id")
        for rec in records
        if rec.get("kind") == "observation" and (rec.get("detail") or {}).get("status") == "resolved"
    }
    for record in records:
        if record.get("kind") == "pending" and record["record_id"] not in resolved_ids:
            detail = record.get("detail") or {}
            cls = detail.get("decision_class") or detail.get("pending_type") or "pending"
            pending_items.append({"class": cls if cls in PENDING_CLASSES else "pending"})
    if pending_items:
        signals["pending"] = {"items": pending_items}

    if progress_dir:
        pfile = Path(progress_dir).expanduser() / "progress.json"
        try:
            pdata = json.loads(pfile.read_text(encoding="utf-8"))
            updated = ((pdata.get("run") or {}).get("last_observed")
                       or (pdata.get("run") or {}).get("updated_at"))
            if isinstance(updated, str) and updated:
                signals["progress"] = {"updated_at": updated}
        except (OSError, ValueError, TypeError):
            pass

    return signals


def _compact_health(payload):
    return {
        "run_id": payload["run_id"],
        "state": payload["state"],
        "next_action": payload["next_action"],
        "recorded": payload["recorded"],
        "record_id": payload["record_id"],
        "reasons": [{"signal": r["signal"], "state": r["state"], "reason": r["reason"]}
                    for r in payload["reasons"]],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(prog=CLI_NAME, description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    evaluate_parser = sub.add_parser(
        "evaluate", help="evaluate one signals snapshot and record the health state")
    evaluate_parser.add_argument("--run-id", required=True)
    source = evaluate_parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--signals", metavar="FILE",
        help="signals snapshot JSON, or - for stdin")
    source.add_argument(
        "--observation", metavar="FILE",
        help="one inbox observation as the loop publishes it in the wake payload "
             "(or - for stdin); the signals this consumer cannot see read as unknown")
    evaluate_parser.add_argument(
        "--config", metavar="FILE",
        help="threshold overrides as a JSON object; defaults are documented")
    evaluate_parser.add_argument(
        "--record-id", metavar="UUID",
        help="retry an earlier health record with the same id")
    evaluate_parser.add_argument(
        "--decided-by", choices=("coordinator", "human"), default="coordinator")
    evaluate_parser.add_argument("--compact", action="store_true", help="compact JSON output")

    collect_parser = sub.add_parser(
        "collect", help="collect native-wake signals from Orca/ledger and evaluate health")
    collect_parser.add_argument("--run-id", required=True)
    collect_parser.add_argument("--dispatch", default=None, help="target dispatch id for worker-show")
    collect_parser.add_argument("--progress", default=None, help="optional progress state directory")
    collect_parser.add_argument("--long-horizon", action="store_true")
    collect_parser.add_argument("--rounds-without-progress", type=int, default=0)
    collect_parser.add_argument("--config", metavar="FILE")
    collect_parser.add_argument("--no-record", action="store_true", help="do not append to ledger")
    collect_parser.add_argument("--compact", action="store_true", help="compact JSON output")

    retry_parser = sub.add_parser("retry", help="retry persisted health-pending records")
    retry_parser.add_argument("--run-id", required=True)

    args = parser.parse_args(argv)

    try:
        if args.command == "retry":
            _COMMON.emit_json(retry_pending(run_id=args.run_id))
            return 0
        if args.command == "collect":
            config = _read_snapshot(args.config) if args.config else None
            snapshot = collect_signals(
                args.run_id,
                dispatch_id=args.dispatch,
                progress_dir=args.progress,
                long_horizon=args.long_horizon,
                rounds_without_progress=args.rounds_without_progress,
            )
            if args.no_record:
                verdict = evaluate(snapshot, config)
                payload = {"run_id": args.run_id, "signals": snapshot, **verdict, "recorded": False}
                _COMMON.emit_json(
                    {"run_id": args.run_id, **verdict, "recorded": False} if args.compact else payload,
                    compact=args.compact,
                )
                return 0
            payload = evaluate_to_ledger(args.run_id, snapshot, config)
            _COMMON.emit_json(_compact_health(payload) if args.compact else payload, compact=args.compact)
            return 0
        config = None
        if args.config:
            config = _read_snapshot(args.config)
        if args.observation:
            snapshot = observation_signals(_read_snapshot(args.observation))
        else:
            snapshot = _read_snapshot(args.signals)
        payload = evaluate_to_ledger(
            args.run_id, snapshot, config, record_id=args.record_id,
            decided_by=args.decided_by,
        )
        _COMMON.emit_json(_compact_health(payload) if args.compact else payload, compact=args.compact)
    except (OSError, TypeError, ValueError) as error:
        print("Health error: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
