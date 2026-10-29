#!/usr/bin/env python3
"""Behaviour tests for ``health.py``, the pure run-health state machine.

The seams are the ones the spec confirms:

* **Module import** for the pure state machine — ``evaluate(signals, config)`` and
  ``transition(...)`` are loaded by path with the same ``importlib`` loader style the ledger
  tests use. A health evaluator that could not be exercised without a live Run would not be
  testable at all, so this is the primary seam.
* **CLI end to end** for the coordinator surface — ``health evaluate`` runs as a subprocess
  against a temporary ``$ORCA_LEDGER_HOME`` and prints the machine result. The record it writes
  is read back with the ledger CLI, which proves it survived a process boundary.
* **No Orca** — the CLI reads a signals snapshot; it never shells out. One test points
  ``ORCA_CLI_COMMAND`` at the shared fake and asserts it was never invoked, so the absence of an
  Orca dependency is asserted instead of assumed.

Fixture values are deliberately *not* claims about a live Orca. ``agentWait`` and
``lastHeartbeatAt`` are fixture fields the ticket says to use defensively: a missing field is
``unknown`` and only an explicit observation is evidence. Every age is derived from a
caller-supplied ``observed_at``, so no test depends on the wall clock.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIRS = (
    REPO_ROOT / "skills" / "orchestra-auto",
    REPO_ROOT / "skills" / "orchestra-auto-zh",
)
SCRIPT_RELATIVE = Path("scripts") / "health.py"
LEDGER_RELATIVE = Path("scripts") / "ledger.py"
RUN_ID = "run_health"

NOW = "2026-10-29T12:00:00Z"


def at(seconds_ago=0):
    """An RFC3339 UTC instant that many seconds before the fixture's fixed ``NOW``."""
    moment = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc) - timedelta(
        seconds=seconds_ago
    )
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def load_health(skill_dir=SKILL_DIRS[0]):
    """Import a skill's ``health.py`` by path, without touching ``sys.path``."""
    path = Path(skill_dir) / SCRIPT_RELATIVE
    spec = importlib.util.spec_from_file_location(
        "health_" + Path(skill_dir).name.replace("-", "_"), path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot build an import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SnapshotMixin:
    """Builds signal snapshots with one overridable entry at a time."""

    def healthy_signals(self, **overrides):
        snapshot = {
            "observed_at": NOW,
            "watch": {"enabled": True},
            "heartbeat": {
                "cadence_seconds": 90,
                "last_heartbeat_at": at(30),
                "liveness": "positive",
                "evidence": "ledger:" + RUN_ID + ":heartbeat",
            },
            "inbox": {"phase": "waiting", "updated_at": at(60)},
            "unacked": {"batches": [], "unacked": 0},
            "worker_list": {"observed_at": at(60), "active_workers": 1},
            "worker_show": {
                "observed_at": at(60),
                "status": "running",
                "worker_done": None,
                "agent_wait": None,
            },
            "turnStart": {
                "status": "observed",
                "observed_at": at(30),
                "dispatched_at": at(90),
            },
            "progress": {"updated_at": at(60)},
            "last_effective_progress": {"rounds_without_progress": 0},
            "reclaimable": {"terminals": []},
            "waiter_slot": {"leaked": False},
            "nested_roster": {"applicable": False, "reason": "single-ticket run"},
        }
        snapshot.update(overrides)
        return snapshot


class AggregationTests(SnapshotMixin, unittest.TestCase):
    """Worst-of precedence and the "absent is unknown, never healthy" rule."""

    @classmethod
    def setUpClass(cls):
        cls.health = load_health()

    def test_evaluate_returns_exactly_the_contract_keys(self):
        result = self.health.evaluate(self.healthy_signals())
        self.assertEqual(set(result), {"state", "reasons", "next_action"})

    def test_all_expected_signals_fresh_is_healthy(self):
        result = self.health.evaluate(self.healthy_signals())
        self.assertEqual(result["state"], "healthy")
        self.assertEqual(result["next_action"], "none")
        self.assertEqual(result["reasons"], [])

    def test_no_signal_at_all_is_unknown_never_healthy(self):
        result = self.health.evaluate({"observed_at": NOW})
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(result["next_action"], "collect_evidence")

    def test_one_unobserved_signal_is_unknown_never_healthy(self):
        snapshot = self.healthy_signals()
        del snapshot["heartbeat"]
        result = self.health.evaluate(snapshot)
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(
            result["reasons"][0]["reason"], "not_observed",
            "an absent signal is reported with its own reason, not folded into healthy",
        )

    def test_a_non_critical_stale_signal_only_degrades(self):
        result = self.health.evaluate(
            self.healthy_signals(inbox={"phase": "waiting", "updated_at": at(3600)})
        )
        self.assertEqual(result["state"], "degraded")
        self.assertEqual(result["next_action"], "continue")
        self.assertEqual(result["reasons"][0]["reason"], "inbox_stale")

    def test_a_leaked_waiter_slot_degrades_and_names_the_repair(self):
        result = self.health.evaluate(
            self.healthy_signals(waiter_slot={"leaked": True, "evidence": "lock:waiters"})
        )
        self.assertEqual(result["state"], "degraded")
        self.assertEqual(result["next_action"], "clear_waiter_slot")
        self.assertEqual(result["reasons"][0]["reason"], "waiter_slot_leak")

    def test_worst_of_precedence_lost_beats_blocked_beats_unknown_beats_degraded(self):
        cases = (
            ("healthy", {}, "healthy"),
            (
                "degraded",
                {"waiter_slot": {"leaked": True}},
                "degraded",
            ),
            (
                "unknown",
                {"heartbeat": {"cadence_seconds": None, "liveness": "none"}},
                "unknown",
            ),
            (
                "blocked",
                {"unacked": {"unacked": 1}},
                "blocked",
            ),
            (
                "lost",
                {
                    "worker_show": {
                        "observed_at": at(60),
                        "status": "exited",
                        "worker_done": False,
                    }
                },
                "lost",
            ),
        )
        for expected, override, label in cases:
            with self.subTest(expected=label):
                result = self.health.evaluate(self.healthy_signals(**override))
                self.assertEqual(result["state"], expected)

        mixed = self.health.evaluate(
            self.healthy_signals(
                waiter_slot={"leaked": True},
                heartbeat={"cadence_seconds": None},
            )
        )
        self.assertEqual(mixed["state"], "unknown", "unknown outranks degraded")

        mixed = self.health.evaluate(
            self.healthy_signals(
                unacked={"unacked": 1},
                heartbeat={"cadence_seconds": None},
            )
        )
        self.assertEqual(mixed["state"], "blocked", "blocked outranks unknown")

        mixed = self.health.evaluate(
            self.healthy_signals(
                worker_show={
                    "observed_at": at(60),
                    "status": "exited",
                    "worker_done": False,
                },
                unacked={"unacked": 1},
            )
        )
        self.assertEqual(mixed["state"], "lost", "lost outranks blocked")

    def test_unknown_signal_keys_are_rejected(self):
        with self.assertRaises(ValueError):
            self.health.evaluate(self.healthy_signals(heartbeats={"cadence_seconds": 90}))

    def test_unknown_config_keys_are_rejected(self):
        with self.assertRaises(ValueError):
            self.health.evaluate(self.healthy_signals(), {"invented_window": 5})


class TransitionTests(unittest.TestCase):
    """The spec's transition table, including the two authorization edges."""

    @classmethod
    def setUpClass(cls):
        cls.health = load_health()

    def evaluation(self, state, reason="test"):
        return {"state": state, "reasons": [], "next_action": "continue"}

    def test_healthy_to_degraded_is_a_rule(self):
        result = self.health.transition("healthy", self.evaluation("degraded"))
        self.assertTrue(result["allowed"])
        self.assertEqual(result["authorization"], "rule")
        self.assertEqual((result["from"], result["to"]), ("healthy", "degraded"))

    def test_degraded_to_healthy_is_a_rule(self):
        result = self.health.transition("degraded", self.evaluation("healthy"))
        self.assertTrue(result["allowed"])
        self.assertEqual(result["authorization"], "rule")

    def test_any_to_unknown_is_a_rule(self):
        for previous in ("healthy", "degraded", "blocked", "lost"):
            with self.subTest(previous=previous):
                result = self.health.transition(previous, self.evaluation("unknown"))
                self.assertTrue(result["allowed"])
                self.assertEqual(result["authorization"], "rule")

    def test_any_to_blocked_needs_a_recorded_pending_or_decision(self):
        unrecorded = self.health.transition("healthy", self.evaluation("blocked"))
        self.assertFalse(unrecorded["allowed"])
        self.assertEqual(unrecorded["authorization"], "record_pending")
        recorded = self.health.transition(
            "healthy", self.evaluation("blocked"), pending_record={"class": "approval"}
        )
        self.assertTrue(recorded["allowed"])
        self.assertEqual(recorded["authorization"], "record_pending")

    def test_blocked_to_healthy_needs_a_human_or_coordinator(self):
        rule = self.health.transition("blocked", self.evaluation("healthy"))
        self.assertFalse(rule["allowed"], "a rule may not clear a blocked Run")
        self.assertEqual(rule["authorization"], "human_or_coordinator")
        for decider in ("human", "coordinator"):
            with self.subTest(decided_by=decider):
                allowed = self.health.transition(
                    "blocked", self.evaluation("healthy"), decided_by=decider
                )
                self.assertTrue(allowed["allowed"])
                self.assertEqual(allowed["decided_by"], decider)

    def test_lost_needs_positive_termination_evidence_and_is_usually_reached_from_unknown(self):
        without = self.health.transition("unknown", self.evaluation("lost"))
        self.assertFalse(without["allowed"])
        with_evidence = self.health.transition(
            "unknown", self.evaluation("lost"),
            evidence="orca:worker-show:exit-without-worker-done",
        )
        self.assertTrue(with_evidence["allowed"])
        self.assertEqual(with_evidence["authorization"], "rule_with_evidence")

    def test_a_proven_termination_is_lost_from_any_state(self):
        """Refusing the record would hide a proven termination, which is a false green."""
        for previous in ("healthy", "degraded", "blocked", "unknown"):
            with self.subTest(previous=previous):
                result = self.health.transition(
                    previous, self.evaluation("lost"),
                    evidence="orca:worker-show:exit-without-worker-done",
                )
                self.assertTrue(result["allowed"])
                self.assertEqual(result["authorization"], "rule_with_evidence")

    def test_a_transition_the_table_does_not_authorize_is_flagged_not_recorded_silently(self):
        result = self.health.transition(
            "lost", self.evaluation("healthy"), decided_by="coordinator"
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["authorization"], "none")

    def test_lost_to_retry_is_a_coordinator_decision_inside_the_human_boundary(self):
        rule = self.health.authorize_retry("lost", decided_by="rule")
        self.assertFalse(rule["allowed"], "retry is never a rule")
        for decider in ("coordinator", "human"):
            with self.subTest(decided_by=decider):
                result = self.health.authorize_retry("lost", decided_by=decider)
                self.assertTrue(result["allowed"])
                self.assertEqual(result["boundary"], "human_boundary")
                self.assertEqual(result["decided_by"], decider)
        not_lost = self.health.authorize_retry("degraded", decided_by="coordinator")
        self.assertFalse(not_lost["allowed"])
        self.assertEqual(not_lost["from"], "degraded")

    def test_release_is_not_a_health_state_and_health_alone_never_authorizes_it(self):
        with self.assertRaises(ValueError):
            self.health.transition("healthy", self.evaluation("release"))
        healthy_alone = self.health.authorize_release("healthy")
        self.assertFalse(healthy_alone["allowed"])
        self.assertFalse(healthy_alone["health_authorized"])
        self.assertEqual(
            healthy_alone["decided_by"], "human",
            "with no settlement fact the release decision is uncertain and goes to the human",
        )
        known_not_settled = self.health.authorize_release(
            "healthy", settled=False, closeout_record="ledger:closeout:1"
        )
        self.assertFalse(known_not_settled["allowed"])
        self.assertFalse(known_not_settled["health_authorized"])

    def test_release_needs_settlement_and_a_closeout_record(self):
        no_settlement = self.health.authorize_release(
            "healthy", settled=False, closeout_record="ledger:closeout:1"
        )
        self.assertFalse(no_settlement["allowed"])
        no_record = self.health.authorize_release(
            "healthy", settled=True, closeout_record=""
        )
        self.assertFalse(no_record["allowed"])
        ready = self.health.authorize_release(
            "lost", settled=True, closeout_record="ledger:closeout:1",
            decided_by="coordinator",
        )
        self.assertTrue(ready["allowed"])
        self.assertEqual(ready["decided_by"], "coordinator")
        uncertain = self.health.authorize_release(
            "healthy", settled=None, closeout_record="ledger:closeout:1"
        )
        self.assertFalse(uncertain["allowed"])
        self.assertEqual(uncertain["decided_by"], "human")

    def test_a_same_state_transition_is_a_noop(self):
        result = self.health.transition("degraded", self.evaluation("degraded"))
        self.assertTrue(result["allowed"])
        self.assertEqual(result["reason"], "no transition")


class SignalTableTests(SnapshotMixin, unittest.TestCase):
    """The eleven signal/TTL rows, each exercised or recorded as unknown with evidence."""

    @classmethod
    def setUpClass(cls):
        cls.health = load_health()

    def finding(self, result, signal):
        for item in result["reasons"]:
            if item["signal"] == signal:
                return item
        return None

    def test_the_table_has_eleven_signals_and_each_rule_exists(self):
        self.assertEqual(len(self.health.SIGNALS), 11)
        self.assertEqual(set(self.health.SIGNAL_RULES), set(self.health.SIGNALS) | {"watch"})

    def test_heartbeat_uses_cadence_times_two_plus_grace(self):
        # Cadence 90 s and grace 60 s gives a 240 s window.
        inside = self.health.evaluate(
            self.healthy_signals(
                heartbeat={"cadence_seconds": 90, "last_heartbeat_at": at(239),
                           "liveness": "positive"}
            )
        )
        self.assertEqual(inside["state"], "healthy")
        outside = self.health.evaluate(
            self.healthy_signals(
                heartbeat={"cadence_seconds": 90, "last_heartbeat_at": at(241),
                           "liveness": "positive"}
            )
        )
        self.assertEqual(outside["state"], "degraded")
        self.assertEqual(self.finding(outside, "heartbeat")["reason"], "heartbeat_stale")

    def test_heartbeat_without_a_cadence_is_unknown_and_never_five_minutes(self):
        for cadence in (None, 0, "90"):
            with self.subTest(cadence=cadence):
                result = self.health.evaluate(
                    self.healthy_signals(
                        heartbeat={"cadence_seconds": cadence,
                                   "last_heartbeat_at": at(1), "liveness": "positive"}
                    )
                )
                self.assertEqual(result["state"], "unknown")
                reason = self.finding(result, "heartbeat")
                self.assertEqual(reason["reason"], "no_preamble_cadence")
                self.assertIn("not evaluated", reason["evidence"])

    def test_a_stale_heartbeat_without_positive_liveness_is_unknown(self):
        result = self.health.evaluate(
            self.healthy_signals(
                heartbeat={"cadence_seconds": 90, "last_heartbeat_at": at(1_000)}
            )
        )
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(
            self.finding(result, "heartbeat")["reason"], "heartbeat_stale_without_liveness"
        )

    def test_inbox_phases_stale_and_error(self):
        for phase in ("waiting", "checking", "backoff"):
            with self.subTest(phase=phase):
                fresh = self.health.evaluate(
                    self.healthy_signals(inbox={"phase": phase, "updated_at": at(60)})
                )
                self.assertEqual(fresh["state"], "healthy")
                stale = self.health.evaluate(
                    self.healthy_signals(inbox={"phase": phase, "updated_at": at(901)})
                )
                self.assertEqual(stale["state"], "degraded")
                self.assertEqual(self.finding(stale, "inbox")["reason"], "inbox_stale")
        error = self.health.evaluate(
            self.healthy_signals(inbox={"phase": "error", "error": "check exited 1"})
        )
        self.assertEqual(error["state"], "blocked")
        self.assertEqual(self.finding(error, "inbox")["reason"], "inbox_error")

    def test_an_unknown_inbox_phase_is_unknown_not_healthy(self):
        result = self.health.evaluate(
            self.healthy_signals(inbox={"phase": "resting", "updated_at": at(1)})
        )
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(self.finding(result, "inbox")["reason"], "unknown_inbox_phase")

    def test_an_unacked_batch_blocks(self):
        result = self.health.evaluate(
            self.healthy_signals(unacked={"unacked": 2, "batches": ["d1", "d2"]})
        )
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(self.finding(result, "unacked")["reason"], "unacked_batch")

    def test_worker_list_matters_only_while_workers_are_active(self):
        idle = self.health.evaluate(
            self.healthy_signals(
                worker_list={"observed_at": at(99_999), "active_workers": 0}
            )
        )
        self.assertEqual(idle["state"], "healthy")
        active = self.health.evaluate(
            self.healthy_signals(
                worker_list={"observed_at": at(301), "active_workers": 1}
            )
        )
        self.assertEqual(active["state"], "degraded")
        self.assertEqual(
            self.finding(active, "worker_list")["reason"], "stale_observation"
        )

    def test_worker_show_ttl_and_termination(self):
        stale = self.health.evaluate(
            self.healthy_signals(
                worker_show={"observed_at": at(301), "status": "running",
                             "agent_wait": None}
            )
        )
        self.assertEqual(stale["state"], "degraded")
        done = self.health.evaluate(
            self.healthy_signals(
                worker_show={"observed_at": at(60), "status": "exited",
                             "worker_done": True, "agent_wait": None}
            )
        )
        self.assertEqual(done["state"], "healthy")
        lost = self.health.evaluate(
            self.healthy_signals(
                worker_show={"observed_at": at(60), "status": "exited",
                             "worker_done": False, "agent_wait": None}
            )
        )
        self.assertEqual(lost["state"], "lost")
        self.assertEqual(
            self.finding(lost, "worker_show")["reason"], "agent_exited_without_worker_done"
        )

    def test_turn_start_observed_is_evidence_and_everything_else_is_unknown(self):
        observed = self.health.evaluate(
            self.healthy_signals(
                turnStart={"status": "observed", "observed_at": at(30),
                           "dispatched_at": at(90)}
            )
        )
        self.assertEqual(observed["state"], "healthy")
        for status in ("unsupported", "unobserved", None, "ready"):
            with self.subTest(status=status):
                result = self.health.evaluate(
                    self.healthy_signals(
                        turnStart={"status": status, "dispatched_at": at(90)}
                    )
                )
                self.assertEqual(result["state"], "unknown")
                self.assertEqual(
                    self.finding(result, "turnStart")["reason"], "turn_start_unproven"
                )

    def test_turn_start_absence_is_unknown(self):
        snapshot = self.healthy_signals()
        del snapshot["turnStart"]
        result = self.health.evaluate(snapshot)
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(self.finding(result, "turnStart")["reason"], "not_observed")

    def test_progress_freshness_is_a_gap_and_never_raises_the_state(self):
        result = self.health.evaluate(
            self.healthy_signals(progress={"updated_at": at(301)})
        )
        self.assertEqual(result["state"], "healthy", "progress.json is a projection")
        self.assertEqual(self.finding(result, "progress")["state"], "gap")
        self.assertEqual(self.finding(result, "progress")["reason"], "progress_stale")

    def test_three_rounds_without_effective_progress_is_degraded(self):
        two = self.health.evaluate(
            self.healthy_signals(
                last_effective_progress={"rounds_without_progress": 2}
            )
        )
        self.assertEqual(two["state"], "healthy")
        three = self.health.evaluate(
            self.healthy_signals(
                last_effective_progress={"rounds_without_progress": 3}
            )
        )
        self.assertEqual(three["state"], "degraded")
        self.assertEqual(
            self.finding(three, "last_effective_progress")["reason"],
            "no_effective_progress",
        )

    def test_reclaimable_terminals_older_than_ten_minutes_degrade(self):
        fresh = self.health.evaluate(
            self.healthy_signals(
                reclaimable={"terminals": [{"dispatch_id": "d1", "settled_at": at(60)}]}
            )
        )
        self.assertEqual(fresh["state"], "healthy")
        stale = self.health.evaluate(
            self.healthy_signals(
                reclaimable={"terminals": [{"dispatch_id": "d1", "settled_at": at(601)}]}
            )
        )
        self.assertEqual(stale["state"], "degraded")
        self.assertEqual(
            self.finding(stale, "reclaimable")["reason"], "reclaimable_terminal_stale"
        )

    def test_a_waiter_slot_leak_degrades_or_blocks(self):
        degraded = self.health.evaluate(
            self.healthy_signals(waiter_slot={"leaked": True})
        )
        self.assertEqual(degraded["state"], "degraded")
        blocked = self.health.evaluate(
            self.healthy_signals(waiter_slot={"leaked": True, "blocks_install": True})
        )
        self.assertEqual(blocked["state"], "blocked")

    def test_a_nested_roster_is_unknown_until_reported(self):
        unknown = self.health.evaluate(
            self.healthy_signals(nested_roster={"applicable": True})
        )
        self.assertEqual(unknown["state"], "unknown")
        self.assertEqual(
            self.finding(unknown, "nested_roster")["reason"], "roster_unreported"
        )
        reported = self.health.evaluate(
            self.healthy_signals(
                nested_roster={"applicable": True, "report": {"workers": 2}}
            )
        )
        self.assertEqual(reported["state"], "healthy")
        not_applicable = self.health.evaluate(
            self.healthy_signals(nested_roster={"applicable": False})
        )
        self.assertEqual(not_applicable["state"], "healthy")

    def test_every_signal_can_be_recorded_unknown_with_its_evidence(self):
        """The table's other half: every row, when absent, is unknown with evidence."""
        for name in self.health.SIGNALS:
            with self.subTest(signal=name):
                snapshot = self.healthy_signals()
                del snapshot[name]
                result = self.health.evaluate(snapshot)
                self.assertEqual(result["state"], "unknown")
                reason = self.finding(result, name)
                self.assertIsNotNone(reason)
                self.assertEqual(reason["reason"], "not_observed")
                self.assertTrue(reason["evidence"], "an absent signal names its evidence")

    def test_every_signal_can_be_unreadable_without_crashing(self):
        for name in self.health.SIGNALS:
            with self.subTest(signal=name):
                result = self.health.evaluate(self.healthy_signals(**{name: {"garbage": 1}}))
                self.assertIn(result["state"], ("unknown", "degraded", "blocked", "lost"))

    def test_agent_wait_null_means_orca_looked_and_found_no_wait(self):
        blocked = self.health.evaluate(
            self.healthy_signals(
                worker_show={"observed_at": at(60), "status": "running",
                             "agent_wait": {"prompt": "approve?"}}
            )
        )
        self.assertEqual(blocked["state"], "blocked")
        self.assertEqual(self.finding(blocked, "worker_show")["reason"], "agent_waiting")
        no_wait = self.health.evaluate(
            self.healthy_signals(
                worker_show={"observed_at": at(60), "status": "running", "agent_wait": None}
            )
        )
        self.assertEqual(no_wait["state"], "healthy")
        absent = self.health.evaluate(
            self.healthy_signals(worker_show={"observed_at": at(60), "status": "running"})
        )
        self.assertEqual(absent["state"], "unknown")
        self.assertEqual(
            self.finding(absent, "worker_show")["reason"], "agent_wait_unobserved"
        )

    def test_a_waiting_worker_is_healthy_not_failed(self):
        for status in ("waiting", "idle"):
            with self.subTest(status=status):
                result = self.health.evaluate(
                    self.healthy_signals(
                        worker_show={"observed_at": at(60), "status": status,
                                     "agent_wait": None}
                    )
                )
                self.assertEqual(result["state"], "healthy")

    def test_watch_disabled_is_degraded_with_watch_disabled_and_polling(self):
        result = self.health.evaluate(self.healthy_signals(watch={"enabled": False}))
        self.assertEqual(result["state"], "degraded")
        reason = self.finding(result, "watch")
        self.assertEqual(reason["reason"], "watch_disabled")
        self.assertEqual(result["next_action"], "poll_worker_list")
        self.assertIn("worker-list", reason["evidence"])
        self.assertNotEqual(result["state"], "healthy")

    def test_an_unreadable_watch_switch_is_unknown(self):
        result = self.health.evaluate(self.healthy_signals(watch={"enabled": "no"}))
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(self.finding(result, "watch")["reason"], "unreadable_watch")

    def test_a_recorded_pending_item_blocks(self):
        result = self.health.evaluate(
            self.healthy_signals(pending={"items": [{"class": "one_way_door"}]})
        )
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(self.finding(result, "pending")["reason"], "pending_decision")
        with self.assertRaises(ValueError):
            self.health.evaluate(self.healthy_signals(pending={"items": [{"class": "x"}]}))


class ConfigTests(SnapshotMixin, unittest.TestCase):
    """Thresholds are configured, not hardcoded: inject a one-second window."""

    @classmethod
    def setUpClass(cls):
        cls.health = load_health()

    def test_injected_windows_change_the_verdict(self):
        snapshot = self.healthy_signals(
            worker_list={"observed_at": at(5), "active_workers": 1},
            inbox={"phase": "waiting", "updated_at": at(2)},
        )
        default = self.health.evaluate(snapshot)
        self.assertEqual(default["state"], "healthy")
        tight = self.health.evaluate(
            snapshot,
            {
                "observation_ttl_seconds": 1,
                "inbox_stale_seconds": 1,
                "turn_start_window_seconds": 2,
                "turn_start_escalate_seconds": 2,
            },
        )
        self.assertEqual(tight["state"], "degraded")
        reasons = {item["signal"] for item in tight["reasons"]}
        self.assertIn("worker_list", reasons)
        self.assertIn("inbox", reasons)

    def test_the_stall_limit_is_configurable(self):
        snapshot = self.healthy_signals(
            last_effective_progress={"rounds_without_progress": 1}
        )
        self.assertEqual(self.health.evaluate(snapshot)["state"], "healthy")
        tight = self.health.evaluate(snapshot, {"stall_rounds": 1})
        self.assertEqual(tight["state"], "degraded")

    def test_the_turn_start_window_and_escalation_are_configurable(self):
        # Inside the 120 s window an unproven start is unknown but is not escalated yet.
        inside = self.health.evaluate(
            self.healthy_signals(
                turnStart={"status": "unobserved", "dispatched_at": at(30)}
            )
        )
        self.assertEqual(inside["state"], "unknown")
        self.assertEqual(inside["next_action"], "collect_evidence")

        # Past the 10 min escalation threshold it still is not a failure, but it escalates.
        old = self.health.evaluate(
            self.healthy_signals(
                turnStart={"status": "unobserved", "dispatched_at": at(601)}
            )
        )
        self.assertEqual(old["state"], "unknown")
        self.assertEqual(old["next_action"], "escalate")

        # A 1 s window with a 2 s escalation changes both verdicts; nothing is hardcoded.
        config = {"turn_start_window_seconds": 1, "turn_start_escalate_seconds": 2}
        at_two = self.health.evaluate(
            self.healthy_signals(
                turnStart={"status": "unobserved", "dispatched_at": at(2)}
            ),
            config,
        )
        self.assertEqual(at_two["next_action"], "escalate")
        at_one = self.health.evaluate(
            self.healthy_signals(
                turnStart={"status": "unobserved", "dispatched_at": at(1)}
            ),
            config,
        )
        self.assertEqual(at_one["next_action"], "collect_evidence")

    def test_the_escalation_threshold_cannot_precede_the_window(self):
        with self.assertRaises(ValueError):
            self.health.evaluate(
                self.healthy_signals(),
                {"turn_start_window_seconds": 10, "turn_start_escalate_seconds": 5},
            )

    def test_config_values_must_be_positive_integers(self):
        for bad in (0, -1, 1.5, "5", True):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    self.health.evaluate(self.healthy_signals(), {"stall_rounds": bad})


class CliTests(SnapshotMixin, unittest.TestCase):
    """``health evaluate`` as the coordinator surface, with a real ledger home."""

    @classmethod
    def setUpClass(cls):
        cls.health = load_health()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="health-cli-")
        self.addCleanup(self._tmp.cleanup)
        # The ledger refuses a symlinked home, and macOS puts temporary directories under
        # /var, which is a symlink to /private/var. Resolve once, then use the real path.
        root = Path(os.path.realpath(self._tmp.name))
        self.home = root / "ledger-home"
        self.work = root / "work"
        self.work.mkdir()

    def env(self, **overrides):
        environment = {
            key: value for key, value in os.environ.items()
            if key not in ("ORCA_LEDGER_HOME", "ORCA_CLI_COMMAND")
        }
        environment["ORCA_LEDGER_HOME"] = str(self.home)
        environment.update(overrides)
        return environment

    def write_snapshot(self, snapshot, name="signals.json"):
        path = self.work / name
        path.write_text(json.dumps(snapshot))
        return path

    def cli(self, argv, env=None, script=None):
        script = script or (SKILL_DIRS[0] / SCRIPT_RELATIVE)
        return subprocess.run(
            [sys.executable, str(script), *argv],
            capture_output=True, text=True, cwd=self.work, env=env or self.env(),
        )

    def ledger_records(self, run_id=RUN_ID, kind="health"):
        ledger = SKILL_DIRS[0] / LEDGER_RELATIVE
        result = subprocess.run(
            [sys.executable, str(ledger), "show", "--run-id", run_id, "--kind", kind],
            capture_output=True, text=True, cwd=self.work, env=self.env(),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_evaluate_prints_the_machine_result_and_records_health(self):
        snapshot = self.healthy_signals(run_id=RUN_ID)
        result = self.cli(
            ["evaluate", "--run-id", RUN_ID, "--signals", str(self.write_snapshot(snapshot))]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        printed = json.loads(result.stdout)
        self.assertEqual(printed["state"], "healthy")
        self.assertEqual(printed["run_id"], RUN_ID)
        self.assertEqual(printed["next_action"], "none")
        self.assertTrue(printed["record_id"])

        records = self.ledger_records()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["kind"], "health")
        self.assertEqual(record["action"], "health")
        self.assertEqual(record["actor"], "coordinator")
        self.assertEqual(record["observed_at"], NOW)
        detail = record["detail"]
        self.assertEqual(detail["health_state"], "healthy")
        self.assertEqual(detail["entered_at"], NOW)
        self.assertEqual(detail["decided_by"], "coordinator")
        self.assertEqual(detail["next_action"], "none")
        self.assertIn("signals", detail)
        self.assertIn("evidence_refs", detail)

    def test_a_degraded_evaluation_records_its_reason_and_next_action(self):
        snapshot = self.healthy_signals(run_id=RUN_ID, watch={"enabled": False})
        result = self.cli(
            ["evaluate", "--run-id", RUN_ID, "--signals", str(self.write_snapshot(snapshot))],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        printed = json.loads(result.stdout)
        self.assertEqual(printed["state"], "degraded")
        self.assertEqual(printed["next_action"], "poll_worker_list")
        record = self.ledger_records()[0]
        self.assertIn("watch_disabled", [r["reason"] for r in record["detail"]["reason"]])
        self.assertEqual(record["detail"]["next_action"], "poll_worker_list")

    def test_an_unknown_state_exits_zero_and_is_not_a_failure(self):
        snapshot = self.healthy_signals(run_id=RUN_ID)
        del snapshot["heartbeat"]
        result = self.cli(
            ["evaluate", "--run-id", RUN_ID, "--signals", str(self.write_snapshot(snapshot))],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["state"], "unknown")

    def test_signals_come_from_stdin_too(self):
        snapshot = self.healthy_signals(run_id=RUN_ID)
        result = subprocess.run(
            [sys.executable, str(SKILL_DIRS[0] / SCRIPT_RELATIVE),
             "evaluate", "--run-id", RUN_ID, "--signals", "-"],
            input=json.dumps(snapshot), capture_output=True, text=True,
            cwd=self.work, env=self.env(),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["state"], "healthy")

    def test_invalid_input_exits_one_and_writes_no_record(self):
        bad = self.write_snapshot({"observed_at": NOW, "heartbeat": "nope"})
        result = self.cli(["evaluate", "--run-id", RUN_ID, "--signals", str(bad)])
        self.assertEqual(result.returncode, 1)
        self.assertIn("Health error", result.stderr)
        self.assertEqual(self.ledger_records(), [])

    def test_a_signals_run_id_mismatch_is_refused(self):
        snapshot = self.healthy_signals(run_id="run_other")
        result = self.cli(
            ["evaluate", "--run-id", RUN_ID, "--signals", str(self.write_snapshot(snapshot))]
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.ledger_records(), [])

    def test_the_record_persists_across_a_restart(self):
        snapshot = self.healthy_signals(run_id=RUN_ID)
        first = self.cli(
            ["evaluate", "--run-id", RUN_ID, "--signals", str(self.write_snapshot(snapshot))]
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        # A second process (a fresh interpreter, a fresh coordinator turn) reads the record
        # back from disk; nothing is held in memory between them.
        records = self.ledger_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["detail"]["health_state"], "healthy")

    def test_the_cli_never_calls_orca(self):
        fake_dir = Path(os.path.realpath(self._tmp.name)) / "fake-orca"
        fake_dir.mkdir()
        fake = fake_dir / "orca"
        fake.write_text("#!/bin/sh\ntouch " + str(fake_dir / "called") + "\nexit 1\n")
        fake.chmod(0o755)
        snapshot = self.healthy_signals(run_id=RUN_ID)
        result = self.cli(
            ["evaluate", "--run-id", RUN_ID, "--signals", str(self.write_snapshot(snapshot))],
            env=self.env(ORCA_CLI_COMMAND=str(fake)),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((fake_dir / "called").exists(), "health evaluated by shelling out")

    def test_a_later_evaluation_reads_the_previous_state_from_the_ledger(self):
        first = self.cli(
            ["evaluate", "--run-id", RUN_ID, "--signals",
             str(self.write_snapshot(self.healthy_signals(run_id=RUN_ID), "one.json"))]
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        degraded = self.healthy_signals(run_id=RUN_ID, watch={"enabled": False})
        second = self.cli(
            ["evaluate", "--run-id", RUN_ID, "--signals",
             str(self.write_snapshot(degraded, "two.json"))]
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        printed = json.loads(second.stdout)
        self.assertEqual(printed["previous_state"], "healthy")
        self.assertEqual(printed["transition"]["allowed"], True)
        self.assertEqual(printed["transition"]["authorization"], "rule")
        records = self.ledger_records()
        self.assertEqual(len(records), 2)
        self.assertEqual(records[-1]["detail"]["health_state"], "degraded")
        self.assertEqual(records[-1]["detail"]["previous_state"], "healthy")

    def test_entered_at_is_stable_while_the_state_is(self):
        degraded_a = self.healthy_signals(
            run_id=RUN_ID, observed_at=at(60), watch={"enabled": False})
        degraded_b = self.healthy_signals(run_id=RUN_ID, observed_at=NOW,
                                          watch={"enabled": False})
        self.cli(["evaluate", "--run-id", RUN_ID, "--signals",
                  str(self.write_snapshot(degraded_a, "a.json"))])
        second = self.cli(["evaluate", "--run-id", RUN_ID, "--signals",
                           str(self.write_snapshot(degraded_b, "b.json"))])
        self.assertEqual(second.returncode, 0, second.stderr)
        printed = json.loads(second.stdout)
        self.assertEqual(printed["state"], "degraded")
        self.assertEqual(printed["entered_at"], at(60),
                         "the state was entered earlier and the record must say so")

    def test_a_failed_health_record_write_keeps_the_evaluation_and_retries(self):
        snapshot = self.healthy_signals(run_id=RUN_ID)
        path = self.write_snapshot(snapshot)
        blocked_home = self.work / "not-a-home"
        blocked_home.write_text("a file where the ledger home should be")
        result = self.cli(
            ["evaluate", "--run-id", RUN_ID, "--signals", str(path)],
            env=self.env(ORCA_LEDGER_HOME=str(blocked_home)),
        )
        self.assertEqual(
            result.returncode, 0,
            "a health-record write failure must not block orchestration: " + result.stderr,
        )
        printed = json.loads(result.stdout)
        self.assertEqual(printed["state"], "healthy")
        self.assertFalse(printed["recorded"])
        self.assertTrue(printed["record_id"], "the record id is kept for a later retry")
        self.assertIn("record_error", printed)
        self.assertTrue(printed["record_error"])

        retry = self.cli(
            ["evaluate", "--run-id", RUN_ID, "--signals", str(path),
             "--record-id", printed["record_id"]],
            env=self.env(),
        )
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertTrue(json.loads(retry.stdout)["recorded"])
        records = self.ledger_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["record_id"], printed["record_id"])

    def test_repeating_the_same_evaluation_is_idempotent(self):
        snapshot = self.healthy_signals(run_id=RUN_ID)
        path = self.write_snapshot(snapshot)
        first = self.cli(["evaluate", "--run-id", RUN_ID, "--signals", str(path)])
        second = self.cli(["evaluate", "--run-id", RUN_ID, "--signals", str(path)])
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(
            json.loads(first.stdout)["record_id"],
            json.loads(second.stdout)["record_id"],
            "the same evaluation over the same snapshot is the same record",
        )
        self.assertEqual(len(self.ledger_records()), 1)

    def test_the_optional_progress_projection_is_written_but_never_authority(self):
        snapshot = self.healthy_signals(run_id=RUN_ID)
        result = self.cli(
            ["evaluate", "--run-id", RUN_ID, "--signals", str(self.write_snapshot(snapshot))],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("projection", json.loads(result.stdout))
        self.assertNotIn(
            "authority",
            json.loads(result.stdout)["projection"],
            "progress.json remains a projection; health authority is the ledger record",
        )

    def test_both_bundles_run_the_same_cli(self):
        snapshot = self.healthy_signals(run_id=RUN_ID)
        path = self.write_snapshot(snapshot)
        for skill_dir in SKILL_DIRS:
            with self.subTest(bundle=skill_dir.name):
                result = self.cli(
                    ["evaluate", "--run-id", RUN_ID, "--signals", str(path)],
                    script=skill_dir / SCRIPT_RELATIVE,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["state"], "healthy")


class RecordingTests(SnapshotMixin, unittest.TestCase):
    """The record the evaluator writes, and the in-memory retry after a failed write."""

    @classmethod
    def setUpClass(cls):
        cls.health = load_health()

    class FakeLedger:
        def __init__(self, failures=0):
            self.failures = failures
            self.appended = []
            self.attempts = 0

        def append(self, record, home=None):
            self.attempts += 1
            if self.attempts <= self.failures:
                raise OSError("disk full")
            self.appended.append(record)
            return record["record_id"]

        def read(self, run_id, filters=None, home=None):
            kind = (filters or {}).get("kind")
            return [record for record in self.appended
                    if record["run_id"] == run_id and (kind is None or record["kind"] == kind)]

    def setUp(self):
        self.health._PENDING_RECORDS.clear()
        self.addCleanup(self.health._PENDING_RECORDS.clear)

    def test_the_record_carries_the_spec_fields(self):
        snapshot = self.healthy_signals(run_id=RUN_ID)
        result = self.health.evaluate(snapshot)
        record = self.health.health_record(RUN_ID, snapshot, result)
        self.assertEqual(record["kind"], "health")
        self.assertEqual(record["action"], "health")
        detail = record["detail"]
        for key in ("health_state", "entered_at", "reason", "signals", "evidence_refs",
                    "decided_by", "next_action"):
            self.assertIn(key, detail)
        self.assertEqual(detail["signals"], snapshot)
        self.assertEqual(detail["decided_by"], "coordinator")
        self.assertEqual(record["observed_at"], NOW)

    def test_a_failed_write_keeps_the_evaluation_and_a_later_retry_records_it(self):
        ledger = self.FakeLedger(failures=1)
        snapshot = self.healthy_signals(run_id=RUN_ID)
        payload = self.health.evaluate_to_ledger(
            RUN_ID, snapshot, ledger_module=ledger
        )
        self.assertEqual(payload["state"], "healthy", "the evaluation survives the write failure")
        self.assertFalse(payload["recorded"])
        self.assertIn("disk full", payload["record_error"])
        self.assertEqual(ledger.appended, [])
        self.assertEqual(len(self.health._PENDING_RECORDS), 1)

        retried = self.health.retry_pending(ledger_module=ledger)
        self.assertEqual(retried["retried"], [payload["record_id"]])
        self.assertEqual(retried["still_pending"], [])
        self.assertEqual(len(ledger.appended), 1)
        self.assertEqual(ledger.appended[0]["record_id"], payload["record_id"])
        self.assertEqual(self.health._PENDING_RECORDS, [])

    def test_a_retry_that_fails_again_stays_pending_without_raising(self):
        ledger = self.FakeLedger(failures=99)
        snapshot = self.healthy_signals(run_id=RUN_ID)
        first = self.health.evaluate_to_ledger(RUN_ID, snapshot, ledger_module=ledger)
        second = self.health.retry_pending(ledger_module=ledger)
        self.assertEqual(second["still_pending"], [first["record_id"]])

    def test_the_same_snapshot_yields_the_same_record_id(self):
        snapshot = self.healthy_signals(run_id=RUN_ID)
        first = self.health.evaluate_to_ledger(
            RUN_ID, snapshot, ledger_module=self.FakeLedger())
        second = self.health.evaluate_to_ledger(
            RUN_ID, snapshot, ledger_module=self.FakeLedger())
        self.assertEqual(first["record_id"], second["record_id"])

    def test_a_repeated_evaluation_keeps_the_original_entered_at(self):
        ledger = self.FakeLedger()
        first_snapshot = self.healthy_signals(run_id=RUN_ID, observed_at=at(60))
        first = self.health.evaluate_to_ledger(
            RUN_ID, first_snapshot, ledger_module=ledger)
        self.assertEqual(first["entered_at"], at(60))
        self.assertIsNone(first["previous_state"])

        second_snapshot = self.healthy_signals(run_id=RUN_ID, observed_at=NOW)
        second = self.health.evaluate_to_ledger(
            RUN_ID, second_snapshot, ledger_module=ledger)
        self.assertEqual(second["previous_state"], "healthy")
        self.assertEqual(second["entered_at"], at(60),
                         "a state that has not changed keeps the instant it was entered")

        degraded_snapshot = self.healthy_signals(
            run_id=RUN_ID, observed_at=NOW,
            inbox={"phase": "waiting", "updated_at": at(4000)},
        )
        third = self.health.evaluate_to_ledger(
            RUN_ID, degraded_snapshot, ledger_module=ledger)
        self.assertEqual(third["entered_at"], NOW)
        self.assertEqual(third["transition"]["from"], "healthy")
        self.assertEqual(third["transition"]["to"], "degraded")
        self.assertTrue(third["transition"]["allowed"])

        record = ledger.appended[-1]
        self.assertEqual(record["detail"]["entered_at"], NOW)
        self.assertEqual(record["detail"]["previous_state"], "healthy")

    def test_a_snapshot_from_another_run_is_refused(self):
        snapshot = self.healthy_signals(run_id="run_other")
        with self.assertRaises(ValueError):
            self.health.evaluate_to_ledger(
                RUN_ID, snapshot, ledger_module=self.FakeLedger())


if __name__ == "__main__":
    unittest.main()
