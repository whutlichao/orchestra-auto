#!/usr/bin/env python3
"""Behaviour tests for ``preflight.py``, the dispatch preflight receipt.

The declared seams are deliberate and match the ticket:

* **Module seam** for the receipt — ``check(ticket, context)`` builds the receipt, runs the
  mechanical items through an injected command runner, records judgment items and returns
  the blocking decision. Tests inject a minimal runner and never execute a live Orca or a
  live settings command.
* **Ledger seam** for durability — a checking run appends exactly one ``kind: preflight``
  record to a temporary ``$ORCA_LEDGER_HOME`` outside every repository, and the receipt and
  the record agree.
* **CLI seam** for the coordinator surface — ``preflight.py check`` and
  ``preflight.py post-dispatch`` run as subprocesses. The Orca executable is the shared
  ``tests/fake_orca.py`` shim installed through ``ORCA_CLI_COMMAND``; the settings database
  is a temporary SQLite fixture. No real Orca and no network.

The live-settings schema in the fixtures is version-scoped, not a stable contract. The
verified source and timestamp are asserted to be recorded in the ledger; the fixture is a
fixture, and no other Orca version's field is claimed to be stable.
"""

import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIRS = (
    REPO_ROOT / "skills" / "orchestra-auto",
    REPO_ROOT / "skills" / "orchestra-auto-zh",
)
PREFLIGHT_RELATIVE = Path("scripts") / "preflight.py"
LEDGER_RELATIVE = Path("scripts") / "ledger.py"
RUN_ID = "run_alpha"
BASE_COMMIT = "44a427a9662c38a76419589e4149193b8adf1688"
ROUND_START = "2026-10-29T10:00:00Z"
CHECKED_AT = "2026-10-29T10:05:00Z"


def load_script(skill_dir, relative, module_name):
    """Import a skill script by path, without touching ``sys.path``."""
    path = skill_dir / relative
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot build an import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_preflight(skill_dir):
    return load_script(
        skill_dir, PREFLIGHT_RELATIVE, "preflight_" + skill_dir.name.replace("-", "_")
    )


def load_ledger(skill_dir):
    return load_script(
        skill_dir, LEDGER_RELATIVE, "ledger_" + skill_dir.name.replace("-", "_")
    )


def load_fake_orca():
    """Import the shared fake harness under either import name.

    ``python3 -m unittest tests.test_preflight`` imports this module as
    ``tests.test_preflight`` while ``discover -s tests`` imports it as ``test_preflight``;
    loading the harness by path keeps both commands working without a package shim.
    """
    path = Path(__file__).resolve().parent / "fake_orca.py"
    spec = importlib.util.spec_from_file_location("fake_orca", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot build an import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ok(payload):
    return {"returncode": 0, "stdout": json.dumps(payload), "stderr": ""}


def failed(reason="command failed", returncode=1):
    return {"returncode": returncode, "stdout": "", "stderr": reason}


class FakeRunner:
    """A minimal injected command runner: matchers in, recorded calls out.

    This is deliberately not a second Orca mock. It answers only the commands the module
    runs, records every argument list so a test can prove no fallback or mutation ran, and
    has no behaviour beyond the scripted responses.
    """

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def run(self, argv, timeout=None):
        self.calls.append(list(argv))
        for matcher, response in self.responses:
            if matcher(list(argv)):
                return dict(response)
        return failed("unscripted command: " + " ".join(argv), returncode=127)


def make_state_database(profile_dir, rows=None, create_table=True, name="state.db",
                        payload=None):
    """Write a temporary settings fixture; never a live Orca database.

    The table and column names are the version-scoped schema recorded in the ticket's
    verification notes (Orca CLI 1.4.244, 2026-10-29). A test may deliberately break any
    of them to prove that an unreadable database fails instead of falling back.
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    database = profile_dir / name
    connection = sqlite3.connect(database)
    try:
        if create_table:
            connection.execute(
                "CREATE TABLE profile_state_documents (domain TEXT NOT NULL, payload TEXT NOT NULL)"
            )
            if rows is None:
                rows = [("settings", json.dumps(
                    {"defaultTuiAgent": "claude", "disabledTuiAgents": []}
                ))]
            for domain, text in rows:
                connection.execute(
                    "INSERT INTO profile_state_documents (domain, payload) VALUES (?, ?)",
                    (domain, payload if payload is not None else text),
                )
        connection.commit()
    finally:
        connection.close()
    return database


def settings_runner(profile_dir, status=None, command_surface=None):
    """The happy-path runner for the three mechanical probes."""
    return FakeRunner([
        (lambda argv: argv[:2] == ["orca", "status"],
         status if status is not None else ok({"app": "up"})),
        (lambda argv: argv[:3] == ["orca", "agent", "hooks"],
         ok({"result": {"settingsPath": str(profile_dir / "settings.json")}})),
        (lambda argv: argv[:3] == ["orca", "orchestration", "worker-list"],
         command_surface if command_surface is not None else ok(
             {"workers": [], "page": {"hasMore": False, "nextCursor": None}}
         )),
    ])


JUDGMENT_IDS = (
    "channel-decided",
    "ticket-type",
    "spec-elements",
    "worktree-parameters",
    "long-horizon-map",
    "main-worktree-rule",
    "ticket-canonical-copy",
    "gates-decidable",
    "formatter-prevention",
)


def make_ticket(**overrides):
    ticket = {
        "id": "36",
        "type": "implementation",
        "gate": "G-impl",
        "origin": "github:whutlichao/orchestra-auto#36",
    }
    ticket.update(overrides)
    return ticket


class PreflightTestCase(unittest.TestCase):
    """A temporary ledger home, profile, guide and fake runner for every test."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "ledger"
        self.profile = self.root / "profile"
        self.database = make_state_database(self.profile)
        self.guide = self.root / "guide.md"
        self.guide.write_text("# orchestration guide\n")
        self.preflight = load_preflight(SKILL_DIRS[0])
        self.ledger = load_ledger(SKILL_DIRS[0])

    def runner(self, **overrides):
        return settings_runner(self.profile, **overrides)

    def context(self, runner=None, **overrides):
        context = {
            "run_id": RUN_ID,
            "ledger_home": str(self.home),
            "round_started_at": ROUND_START,
            "checked_at": CHECKED_AT,
            "base_commit": BASE_COMMIT,
            "guide": {
                "fetched_at": "2026-10-29T10:01:00Z",
                "path": str(self.guide),
                "version": "1.4.244",
                "command": "skills get orchestration",
            },
            "judgments": {
                item_id: {"result": "pass", "note": "recorded by the coordinator"}
                for item_id in JUDGMENT_IDS
            },
            "overrides": [],
            "baselines": [],
        }
        context.update(overrides)
        return context, runner or self.runner()

    def check(self, ticket=None, runner=None, **overrides):
        context, runner = self.context(runner=runner, **overrides)
        return self.preflight.check(ticket or make_ticket(), context, runner=runner)

    def ledger_records(self, kind=None):
        records = self.ledger.read(RUN_ID, home=self.home)
        if kind is None:
            return records
        return [record for record in records if record["kind"] == kind]

    def item(self, receipt, item_id):
        return next(item for item in receipt["items"] if item["id"] == item_id)


class ChecklistTests(PreflightTestCase):
    """The twelve tick-sheet lines keep their identity and gain a class."""

    def test_the_twelve_checklist_lines_keep_their_ids_and_gain_a_class(self):
        checklist = self.preflight.CHECKLIST
        self.assertEqual(len(checklist), 12)
        self.assertEqual(
            [line["id"] for line in checklist],
            [
                "cli-alive",
                "guide-freshness",
                "channel-decided",
                "ticket-type",
                "spec-elements",
                "worktree-parameters",
                "long-horizon-map",
                "main-worktree-rule",
                "ticket-canonical-copy",
                "gates-decidable",
                "baseline-source-commit",
                "formatter-prevention",
            ],
        )
        for line in checklist:
            self.assertIn(line["class"], ("mechanical", "judgment"))
        receipt = self.check()
        ids = [item["id"] for item in receipt["items"]]
        expected = ["live-settings"]
        for line in checklist:
            expected.extend(line["items"])
        self.assertEqual(ids, expected)
        self.assertNotIn("live-settings", [line["id"] for line in checklist])
        for item in receipt["items"]:
            self.assertIn(item["class"], ("mechanical", "judgment"))
            self.assertIn(item["result"], ("pass", "fail", "waived"))

    def test_both_references_document_every_checklist_item_and_class(self):
        """The sheet and the receipt cannot disagree: same ids, same order, same class."""
        expected = [item for line in self.preflight.CHECKLIST for item in line["items"]]
        known = set(expected)
        for skill_dir in SKILL_DIRS:
            text = (skill_dir / "references" / "dispatch-preflight.md").read_text()
            blocks = []
            current = None
            for line in text.splitlines():
                if line.startswith("- [ ]"):
                    current = [line]
                    blocks.append(current)
                elif current is not None and line.strip() and line.startswith((" ", "\t")):
                    current.append(line)
                else:
                    current = None
            bullets = ["\n".join(block) for block in blocks]
            self.assertEqual(len(bullets), 12, skill_dir.name)
            parsed = [
                token
                for line in bullets
                for token in re.findall(r"`([^`]+)`", line)
                if token in known
            ]
            self.assertEqual(parsed, expected, skill_dir.name)
            for line, checklist_line in zip(bullets, self.preflight.CHECKLIST):
                self.assertIn(
                    checklist_line["class"], line,
                    f"{skill_dir.name}: {checklist_line['id']} has no class marker",
                )
            self.assertNotIn("`live-settings`", "\n".join(bullets))
            # The independent item is described in its own section, with its class and
            # blocking role, not as a thirteenth tick line.
            before_4_0_1 = text.split("### 4.0.1", 1)[0]
            self.assertIn("`live-settings`", before_4_0_1, skill_dir.name)
            self.assertIn("mechanical", before_4_0_1, skill_dir.name)

    def test_post_receipt_observations_are_not_pre_dispatch_gates(self):
        for skill_dir in SKILL_DIRS:
            text = (skill_dir / "references" / "dispatch-preflight.md").read_text()
            before = text.split("### 4.0.1", 1)[1].split("### 4.0.2", 1)[0]
            after = text.split("### 4.0.2", 1)[1].split("### 4.0.3", 1)[0]
            for token in ("`review-exemption-recheck`", "`dashboard-backfill`",
                          "`inbox-health`"):
                self.assertIn(token, after, skill_dir.name)
                self.assertNotIn(token, before, skill_dir.name)


class ReceiptTests(PreflightTestCase):
    """The receipt, its ledger record and the blocking decision."""

    def test_receipt_records_ticket_identity_items_and_a_ledger_record(self):
        receipt = self.check()
        self.assertEqual(receipt["ticket"], make_ticket())
        self.assertEqual(receipt["checked_at"], CHECKED_AT)
        self.assertTrue(receipt["blocking"]["allowed"])
        self.assertFalse(receipt["blocking"]["blocked"])
        self.assertTrue(receipt["record"]["written"])
        self.assertEqual(receipt["post_dispatch"]["review_exemption"]["status"], "none")
        self.assertEqual(self.item(receipt, "live-settings")["class"], "mechanical")
        self.assertEqual(self.item(receipt, "channel-decided")["class"], "judgment")
        self.assertEqual(self.item(receipt, "channel-decided")["result"], "pass")

        records = self.ledger_records(kind="preflight")
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["run_id"], RUN_ID)
        self.assertEqual(record["action"], "preflight")
        self.assertEqual(record["subject"]["ticket"], "36")
        self.assertEqual(record["state"], "observed")
        self.assertEqual(record["observed_at"], CHECKED_AT)
        detail = record["detail"]
        self.assertEqual(detail["ticket"], make_ticket())
        self.assertEqual(detail["checked_at"], CHECKED_AT)
        self.assertEqual(detail["items"], receipt["items"])
        self.assertEqual(detail["blocking"], receipt["blocking"])
        self.assertEqual(detail["overrides"], receipt["overrides"])
        self.assertEqual(detail["post_dispatch"], receipt["post_dispatch"])
        self.assertEqual(record["record_id"], receipt["record"]["record_id"])

    def test_blocking_failure_rejects_dispatch_and_an_override_requires_by_and_reason(self):
        judgments = {
            item_id: {"result": "pass", "note": "recorded"}
            for item_id in JUDGMENT_IDS
        }
        judgments["channel-decided"] = {"result": "fail", "note": "channel not re-judged"}
        receipt = self.check(judgments=judgments)
        self.assertFalse(receipt["blocking"]["allowed"])
        self.assertTrue(receipt["blocking"]["blocked"])
        self.assertIn("channel-decided", receipt["blocking"]["failed_items"])
        self.assertEqual(self.item(receipt, "channel-decided")["result"], "fail")
        self.assertEqual(self.ledger_records(kind="preflight")[0]["state"], "failed")

        waived = self.check(
            judgments=judgments,
            overrides=[{"item": "channel-decided", "by": "human", "reason": "settled live"}],
        )
        self.assertTrue(waived["blocking"]["allowed"])
        self.assertEqual(self.item(waived, "channel-decided")["result"], "waived")
        self.assertEqual(waived["overrides"][0]["applied"], True)
        self.assertIn("channel-decided", waived["blocking"]["waived_items"])

        incomplete = self.check(
            judgments=judgments,
            overrides=[{"item": "channel-decided", "by": "human"}],
        )
        self.assertFalse(incomplete["blocking"]["allowed"])
        self.assertEqual(self.item(incomplete, "channel-decided")["result"], "fail")
        self.assertEqual(incomplete["overrides"][0]["applied"], False)

    def test_a_receipt_write_failure_rejects_dispatch(self):
        runner = self.runner()
        receipt = self.check(runner=runner, ledger_home=str(REPO_ROOT / "forbidden-ledger"))
        self.assertFalse(receipt["record"]["written"])
        self.assertFalse(receipt["blocking"]["allowed"])
        self.assertTrue(receipt["blocking"]["blocked"])
        self.assertIn("receipt", receipt["blocking"]["reason"].lower())
        self.assertFalse(
            any(call[:2] == ["orca", "worker-start"] for call in runner.calls),
            "a failed receipt must never be followed by worker-start",
        )

    def test_a_supplied_record_id_makes_the_receipt_append_idempotent(self):
        record_id = str(uuid.uuid4())
        first = self.check(record_id=record_id)
        second = self.check(record_id=record_id)
        self.assertTrue(first["record"]["written"])
        self.assertEqual(first["record"]["record_id"], second["record"]["record_id"])
        self.assertEqual(len(self.ledger_records(kind="preflight")), 1)

        with self.assertRaises(ValueError):
            self.check(record_id="not-a-uuid")

    def test_an_unknown_judgment_item_is_not_silently_ignored(self):
        judgments = {item_id: {"result": "pass", "note": "recorded"}
                     for item_id in JUDGMENT_IDS}
        judgments["not-a-check"] = {"result": "pass", "note": "typo"}
        with self.assertRaises(ValueError):
            self.check(judgments=judgments)

    def test_ticket_identity_is_required(self):
        for missing in ("id", "type", "gate", "origin"):
            with self.subTest(field=missing):
                ticket = make_ticket()
                ticket.pop(missing)
                with self.assertRaises(ValueError):
                    self.check(ticket=ticket)


class MechanicalItemTests(PreflightTestCase):
    """The mechanical items and the command runner they use."""

    def test_cli_liveness_is_two_mechanical_evidence_items(self):
        receipt = self.check()
        app = self.item(receipt, "cli-app-up")
        surface = self.item(receipt, "cli-command-surface")
        self.assertEqual((app["class"], app["result"]), ("mechanical", "pass"))
        self.assertEqual((surface["class"], surface["result"]), ("mechanical", "pass"))
        self.assertEqual(app["command"], "orca status --json")
        self.assertIn("worker-list", surface["command"])

        broken = self.check(runner=self.runner(status=failed("app down")))
        self.assertEqual(self.item(broken, "cli-app-up")["result"], "fail")
        self.assertEqual(self.item(broken, "cli-command-surface")["result"], "pass")
        self.assertFalse(broken["blocking"]["allowed"])

        no_surface = self.check(runner=self.runner(command_surface=failed("no CLI")))
        self.assertEqual(self.item(no_surface, "cli-app-up")["result"], "pass")
        self.assertEqual(self.item(no_surface, "cli-command-surface")["result"], "fail")
        self.assertFalse(no_surface["blocking"]["allowed"])

    def test_guide_freshness_accepts_only_a_fetch_from_this_round(self):
        fresh = self.check()
        self.assertEqual(self.item(fresh, "guide-freshness")["result"], "pass")
        observed = self.item(fresh, "guide-freshness")["observed"]
        self.assertEqual(observed["fetched_at"], "2026-10-29T10:01:00Z")
        self.assertTrue(observed["path_exists"])

        on_disk = self.check(guide={"path": str(self.guide), "version": "1.4.244"})
        self.assertEqual(self.item(on_disk, "guide-freshness")["result"], "fail")
        self.assertFalse(on_disk["blocking"]["allowed"])

        stale = self.check(guide={
            "fetched_at": "2026-10-29T09:00:00Z",
            "path": str(self.guide),
        })
        self.assertEqual(self.item(stale, "guide-freshness")["result"], "fail")
        self.assertIn("round", self.item(stale, "guide-freshness")["note"].lower())

    def test_a_baseline_from_a_different_commit_must_be_labelled(self):
        same = self.check(baselines=[
            {"label": "tests", "source_commit": BASE_COMMIT},
        ])
        self.assertEqual(self.item(same, "baseline-source-commit")["result"], "pass")

        different = self.check(baselines=[
            {"label": "tests", "source_commit": "0" * 40},
        ])
        self.assertEqual(self.item(different, "baseline-source-commit")["result"], "fail")
        self.assertFalse(different["blocking"]["allowed"])

        labelled = self.check(baselines=[
            {"label": "tests", "source_commit": "0" * 40, "order_of_magnitude": True},
        ])
        self.assertEqual(self.item(labelled, "baseline-source-commit")["result"], "pass")

        missing = self.check(baselines=[{"label": "tests"}])
        self.assertEqual(self.item(missing, "baseline-source-commit")["result"], "fail")


class LiveSettingsTests(PreflightTestCase):
    """The live-settings item is blocking, version-scoped and has no fallback source."""

    def test_a_backup_database_is_not_a_candidate(self):
        (self.profile / "state.backup.1.db").write_bytes(b"not a database")
        receipt = self.check()
        item = self.item(receipt, "live-settings")
        self.assertEqual(item["result"], "pass", item["note"])
        self.assertEqual(item["observed"]["database"], "state.db")
        self.assertEqual(item["observed"]["defaultTuiAgent"], "claude")
        self.assertEqual(item["observed"]["disabledTuiAgents"], [])

    def test_zero_or_two_databases_stop_the_dispatch(self):
        empty_profile = self.root / "empty-profile"
        empty_profile.mkdir()
        runner = settings_runner(empty_profile)
        receipt = self.check(runner=runner)
        item = self.item(receipt, "live-settings")
        self.assertEqual(item["result"], "fail")
        self.assertIn("exactly one non-backup state database", item["note"])
        self.assertIn("no fallback", item["note"])
        self.assertFalse(receipt["blocking"]["allowed"])

        two_profile = self.root / "two-profile"
        make_state_database(two_profile, name="first.db")
        make_state_database(two_profile, name="second.db")
        receipt = self.check(runner=settings_runner(two_profile))
        item = self.item(receipt, "live-settings")
        self.assertEqual(item["result"], "fail")
        self.assertIn("found 2", item["note"])
        self.assertFalse(receipt["blocking"]["allowed"])

    def test_an_unreadable_database_never_falls_back_to_orca_data_json(self):
        profile = self.root / "unreadable-profile"
        make_state_database(profile, create_table=False, name="state.db")
        decoy = profile / "orca-data.json"
        decoy.write_text(json.dumps({
            "defaultTuiAgent": "decoy-agent",
            "disabledTuiAgents": ["decoy"],
        }))
        runner = settings_runner(profile)
        receipt = self.check(runner=runner)
        item = self.item(receipt, "live-settings")
        self.assertEqual(item["result"], "fail")
        self.assertIn("no fallback", item["note"])
        self.assertNotEqual(item["observed"].get("defaultTuiAgent"), "decoy-agent")
        self.assertNotIn("decoy", json.dumps(item["observed"]))
        self.assertFalse(receipt["blocking"]["allowed"])

    def test_live_settings_schema_failures_stop_the_dispatch(self):
        cases = {
            "missing-table": dict(create_table=False),
            "empty-table": dict(rows=[]),
            "two-settings-rows": dict(rows=[
                ("settings", json.dumps({"defaultTuiAgent": "a", "disabledTuiAgents": []})),
                ("settings", json.dumps({"defaultTuiAgent": "b", "disabledTuiAgents": []})),
            ]),
            "invalid-json": dict(payload="{not json"),
            "missing-disabled-list": dict(payload=json.dumps({"defaultTuiAgent": "a"})),
            "empty-default-agent": dict(payload=json.dumps({
                "defaultTuiAgent": "", "disabledTuiAgents": [],
            })),
            "disabled-list-not-strings": dict(payload=json.dumps({
                "defaultTuiAgent": "a", "disabledTuiAgents": [1],
            })),
        }
        for label, options in cases.items():
            with self.subTest(label=label):
                profile = self.root / ("profile-" + label)
                make_state_database(profile, name="state.db", **options)
                receipt = self.check(runner=settings_runner(profile))
                item = self.item(receipt, "live-settings")
                self.assertEqual(item["result"], "fail", label)
                self.assertIn("no fallback", item["note"])
                self.assertFalse(receipt["blocking"]["allowed"])

    def test_the_verification_source_and_timestamp_are_recorded_in_the_ledger(self):
        receipt = self.check()
        self.assertEqual(self.item(receipt, "live-settings")["result"], "pass")
        record = self.ledger_records(kind="preflight")[0]
        item = next(
            item for item in record["detail"]["items"]
            if item["id"] == "live-settings"
        )
        verification = item["observed"]["schema_verification"]
        self.assertIn("1.4.244", verification["source"])
        self.assertEqual(verification["verified_at"], "2026-10-29")
        self.assertIn("version-scoped", verification["scope"])
        self.assertIn("version-scoped", item["note"])

    def test_a_failed_profile_lookup_runs_no_settings_command_and_no_fallback(self):
        runner = settings_runner(self.profile)
        runner.responses[1] = (
            lambda argv: argv[:3] == ["orca", "agent", "hooks"],
            failed("hooks status unavailable"),
        )
        receipt = self.check(runner=runner)
        item = self.item(receipt, "live-settings")
        self.assertEqual(item["result"], "fail")
        self.assertIn("no fallback", item["note"])
        self.assertFalse(receipt["blocking"]["allowed"])
        self.assertFalse(
            any("sqlite3" in call or "orca-data" in " ".join(call) for call in runner.calls)
        )
        self.assertEqual(
            [call for call in runner.calls if call[:3] == ["orca", "agent", "hooks"]],
            [["orca", "agent", "hooks", "status", "--json"]],
        )

    def test_a_receipt_records_no_more_than_the_two_verified_settings_fields(self):
        receipt = self.check()
        observed = self.item(receipt, "live-settings")["observed"]
        self.assertEqual(
            sorted(key for key in observed if key in
                   ("defaultTuiAgent", "disabledTuiAgents")),
            ["defaultTuiAgent", "disabledTuiAgents"],
        )
        self.assertNotIn("payload", observed)


class ReviewExemptionTests(PreflightTestCase):
    """A review exemption is provisional before dispatch and rechecked after it."""

    def exemption(self, **overrides):
        value = {
            "reason": "single bounded ticket; no shared interface",
            "interface_impact": "none: a new script and its tests",
            "expected_changed_files": ["skills/orchestra-auto/scripts/preflight.py"],
            "max_changed_lines": 100,
        }
        value.update(overrides)
        return value

    def post_context(self, diff=None, **overrides):
        context, _runner = self.context(review_exemption=self.exemption())
        context.update({
            "worktree": str(self.root / "worktree"),
            "dashboard": {"written": True, "reason": ""},
            "inbox": {"installed": True, "healthy": True, "reason": ""},
        })
        context.update(overrides)
        runner = FakeRunner([
            (lambda argv: argv[:2] == ["orca", "status"], ok({"app": "up"})),
            (lambda argv: argv[:3] == ["orca", "agent", "hooks"],
             ok({"result": {"settingsPath": str(self.profile / "settings.json")}})),
            (lambda argv: argv[:3] == ["orca", "orchestration", "worker-list"],
             ok({"workers": [], "page": {"hasMore": False, "nextCursor": None}})),
            (lambda argv: argv[:3] == ["git", "-C", "diff"]
             or (argv[:1] == ["git"] and "--numstat" in argv),
             diff if diff is not None else ok({"numstat": ""})),
        ])
        # The runner returns raw stdout, so a diff fixture is expressed as text.
        if diff is not None:
            runner.responses[3] = (
                lambda argv: argv[:1] == ["git"],
                {"returncode": diff.get("returncode", 0),
                 "stdout": diff.get("stdout", ""),
                 "stderr": diff.get("stderr", "")},
            )
        return context, runner

    def test_a_review_exemption_is_provisional_before_dispatch(self):
        receipt = self.check(review_exemption=self.exemption())
        exemption = receipt["post_dispatch"]["review_exemption"]
        self.assertEqual(exemption["status"], "provisional")
        self.assertEqual(
            exemption["expected_changed_files"],
            ["skills/orchestra-auto/scripts/preflight.py"],
        )
        self.assertEqual(exemption["interface_impact"], "none: a new script and its tests")
        self.assertEqual(exemption["proposed_at"], CHECKED_AT)

    def test_recheck_confirms_when_the_real_diff_matches_the_exemption(self):
        self.check(review_exemption=self.exemption())
        before = self.ledger_records(kind="preflight")[0]
        context, runner = self.post_context(diff={
            "stdout": "40\t2\tskills/orchestra-auto/scripts/preflight.py\n",
        })
        report = self.preflight.post_dispatch(make_ticket(), context, runner=runner)
        self.assertEqual(report["review_exemption"]["status"], "confirmed")
        self.assertEqual(
            report["review_exemption"]["observed"]["changed_files"],
            ["skills/orchestra-auto/scripts/preflight.py"],
        )
        self.assertEqual(report["review_exemption"]["observed"]["added"], 40)
        self.assertEqual(report["review_exemption"]["observed"]["deleted"], 2)
        records = self.ledger_records()
        observation = next(
            record for record in records
            if record["kind"] == "observation"
            and record["detail"].get("observation") == "review-exemption-recheck"
        )
        self.assertEqual(observation["detail"]["status"], "confirmed")
        after = self.ledger_records(kind="preflight")[0]
        self.assertEqual(before["detail"]["blocking"], after["detail"]["blocking"])
        self.assertTrue(after["detail"]["blocking"]["allowed"])

    def test_recheck_revokes_on_an_unexpected_file_or_an_oversize_diff(self):
        self.check(review_exemption=self.exemption())
        unexpected, runner = self.post_context(diff={
            "stdout": "1\t0\tskills/orchestra-auto/scripts/other.py\n",
        })
        report = self.preflight.post_dispatch(make_ticket(), unexpected, runner=runner)
        self.assertEqual(report["review_exemption"]["status"], "revoked")
        self.assertIn("unexpected", report["review_exemption"]["reason"])

        oversize, runner = self.post_context(diff={
            "stdout": "120\t0\tskills/orchestra-auto/scripts/preflight.py\n",
        })
        report = self.preflight.post_dispatch(make_ticket(), oversize, runner=runner)
        self.assertEqual(report["review_exemption"]["status"], "revoked")
        self.assertIn("lines", report["review_exemption"]["reason"])

        unreadable, runner = self.post_context(diff={
            "returncode": 128, "stderr": "not a git worktree",
        })
        report = self.preflight.post_dispatch(make_ticket(), unreadable, runner=runner)
        self.assertEqual(report["review_exemption"]["status"], "revoked")
        self.assertIn("could not be read", report["review_exemption"]["reason"])

    def test_recheck_keeps_a_leading_dot_directory_intact(self):
        exemption = self.exemption(expected_changed_files=[".github/workflows/ci.yml"])
        self.check(review_exemption=exemption)
        context, runner = self.post_context(diff={
            "stdout": "1\t0\t.github/workflows/ci.yml\n",
        })
        context["review_exemption"] = exemption
        report = self.preflight.post_dispatch(make_ticket(), context, runner=runner)
        self.assertEqual(report["review_exemption"]["status"], "confirmed")

    def test_a_revoked_exemption_never_rewrites_the_preflight_receipt(self):
        self.check(review_exemption=self.exemption())
        before = self.ledger_records(kind="preflight")
        context, runner = self.post_context(diff={
            "stdout": "1\t0\tskills/orchestra-auto/scripts/other.py\n",
        })
        self.preflight.post_dispatch(make_ticket(), context, runner=runner)
        after = self.ledger_records(kind="preflight")
        self.assertEqual(len(after), len(before))
        self.assertEqual(before[0]["detail"], after[0]["detail"])
        self.assertTrue(after[0]["detail"]["blocking"]["allowed"])

    def test_no_exemption_means_no_diff_recheck(self):
        context, _runner = self.context()
        context.update({
            "worktree": str(self.root / "worktree"),
            "dashboard": {"written": True, "reason": ""},
            "inbox": {"installed": True, "healthy": True, "reason": ""},
        })
        runner = self.runner()
        report = self.preflight.post_dispatch(make_ticket(), context, runner=runner)
        self.assertEqual(report["review_exemption"]["status"], "none")
        self.assertFalse(any(call[:1] == ["git"] for call in runner.calls))


class PostReceiptObservationTests(PreflightTestCase):
    """Dashboard and inbox are ledger observations, never pre-dispatch gates."""

    def post_context(self, dashboard=None, inbox=None, **overrides):
        context, _runner = self.context()
        context.update({
            "dashboard": dashboard if dashboard is not None else {
                "written": True, "reason": "",
            },
            "inbox": inbox if inbox is not None else {
                "installed": True, "healthy": True, "reason": "",
            },
        })
        context.update(overrides)
        return context, self.runner()

    def test_dashboard_failure_records_a_gap_and_never_changes_the_preflight_result(self):
        receipt = self.check()
        self.assertTrue(receipt["blocking"]["allowed"])
        context, runner = self.post_context(
            dashboard={"written": False, "reason": "dashboard server down"},
        )
        report = self.preflight.post_dispatch(make_ticket(), context, runner=runner)
        dashboard = next(
            observation for observation in report["observations"]
            if observation["id"] == "dashboard-backfill"
        )
        self.assertEqual(dashboard["outcome"], "gap")
        self.assertIn("dashboard server down", dashboard["note"])
        records = self.ledger_records()
        self.assertTrue(any(record["kind"] == "gap" for record in records))
        preflight = self.ledger_records(kind="preflight")[0]
        self.assertTrue(preflight["detail"]["blocking"]["allowed"])
        self.assertEqual(preflight["detail"]["items"], receipt["items"])

    def test_inbox_health_is_an_observation_not_a_gate(self):
        self.check()
        context, runner = self.post_context(
            inbox={"installed": True, "healthy": False, "reason": "loop missing"},
        )
        report = self.preflight.post_dispatch(make_ticket(), context, runner=runner)
        inbox = next(
            observation for observation in report["observations"]
            if observation["id"] == "inbox-health"
        )
        self.assertEqual(inbox["outcome"], "unhealthy")
        self.assertIn("loop missing", inbox["note"])
        record = next(
            record for record in self.ledger_records()
            if record["detail"].get("observation") == "inbox-health"
        )
        self.assertEqual(record["kind"], "observation")
        self.assertTrue(self.ledger_records(kind="preflight")[0]["detail"]["blocking"]["allowed"])

    def test_both_post_receipt_observations_are_named_and_not_pre_dispatch_items(self):
        self.check()
        context, runner = self.post_context()
        report = self.preflight.post_dispatch(make_ticket(), context, runner=runner)
        self.assertEqual(
            [observation["id"] for observation in report["observations"]],
            list(self.preflight.OBSERVATIONS),
        )
        self.assertEqual(report["observations_expected"],
                         list(self.preflight.OBSERVATIONS))
        receipt = self.ledger_records(kind="preflight")[0]
        item_ids = [item["id"] for item in receipt["detail"]["items"]]
        self.assertNotIn("dashboard-backfill", item_ids)
        self.assertNotIn("inbox-health", item_ids)

    def test_a_post_dispatch_record_failure_is_reported_without_rewriting_the_gate(self):
        self.check()
        context, runner = self.post_context()
        context["ledger_home"] = str(REPO_ROOT / "forbidden-ledger")
        report = self.preflight.post_dispatch(make_ticket(), context, runner=runner)
        self.assertFalse(report["records"]["written"])
        self.assertIn("ledger", report["records"]["error"].lower())
        self.assertTrue(self.ledger_records(kind="preflight")[0]["detail"]["blocking"]["allowed"])


class CommandLineTests(PreflightTestCase):
    """The coordinator-facing CLI, driven as a subprocess against the shared fake."""

    def setUp(self):
        super().setUp()
        self.fake_orca = load_fake_orca()

    def write_json(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value))
        return path

    def cli_env(self, fake=None):
        env = {
            **os.environ,
            "HOME": str(self.root / "home"),
            "ORCA_LEDGER_HOME": str(self.home),
        }
        if fake is not None:
            env["ORCA_CLI_COMMAND"] = str(fake.path)
        return env

    def run_cli(self, *args, env=None):
        return subprocess.run(
            [sys.executable, str(SKILL_DIRS[0] / PREFLIGHT_RELATIVE), *args],
            env=env or self.cli_env(), cwd=str(self.root), text=True,
            capture_output=True,
        )

    def prepare_check_files(self, **context_overrides):
        ticket = self.write_json("ticket.json", make_ticket())
        context, _runner = self.context()
        context.pop("ledger_home", None)
        context.update(context_overrides)
        context_path = self.write_json("context.json", context)
        return ticket, context_path

    def test_check_cli_allows_a_clean_preflight_and_runs_no_mutation(self):
        fake = self.fake_orca.FakeOrca(
            self.root / "fake",
            status={"app": "up"},
            agent_hooks_status={"settingsPath": str(self.profile / "settings.json")},
            worker_pages=[{"workers": []}],
        ).write()
        ticket, context_path = self.prepare_check_files()
        result = self.run_cli(
            "check", "--run-id", RUN_ID, "--ticket", str(ticket),
            "--context", str(context_path), env=self.cli_env(fake),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertTrue(receipt["blocking"]["allowed"])
        self.assertTrue(receipt["record"]["written"])
        self.assertEqual(fake.mutations(), [])
        calls = "\n".join(fake.calls())
        self.assertIn("status --json", calls)
        self.assertIn("agent hooks status --json", calls)
        self.assertIn("orchestration worker-list", calls)
        self.assertNotIn("worker-start", calls)
        records = self.ledger_records(kind="preflight")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["state"], "observed")

    def test_check_cli_blocks_and_exits_nonzero_when_live_settings_are_unreadable(self):
        fake = self.fake_orca.FakeOrca(
            self.root / "fake",
            status={"app": "up"},
            agent_hooks_status={"fail": "hooks unavailable"},
            worker_pages=[{"workers": []}],
        ).write()
        ticket, context_path = self.prepare_check_files()
        result = self.run_cli(
            "check", "--run-id", RUN_ID, "--ticket", str(ticket),
            "--context", str(context_path), env=self.cli_env(fake),
        )
        self.assertEqual(result.returncode, 1)
        receipt = json.loads(result.stdout)
        self.assertFalse(receipt["blocking"]["allowed"])
        self.assertIn("live-settings", receipt["blocking"]["failed_items"])
        self.assertEqual(self.ledger_records(kind="preflight")[0]["state"], "failed")
        self.assertEqual(fake.mutations(), [])

    def test_check_cli_blocks_when_the_receipt_cannot_be_written(self):
        fake = self.fake_orca.FakeOrca(
            self.root / "fake",
            status={"app": "up"},
            agent_hooks_status={"settingsPath": str(self.profile / "settings.json")},
            worker_pages=[{"workers": []}],
        ).write()
        ticket, context_path = self.prepare_check_files()
        env = self.cli_env(fake)
        env["ORCA_LEDGER_HOME"] = str(REPO_ROOT / "forbidden-ledger")
        result = self.run_cli(
            "check", "--run-id", RUN_ID, "--ticket", str(ticket),
            "--context", str(context_path), env=env,
        )
        self.assertEqual(result.returncode, 1)
        receipt = json.loads(result.stdout)
        self.assertFalse(receipt["record"]["written"])
        self.assertFalse(receipt["blocking"]["allowed"])
        self.assertIn("receipt", receipt["blocking"]["reason"].lower())
        self.assertEqual(fake.mutations(), [])

    def test_post_dispatch_cli_records_observations_against_a_real_worktree(self):
        worktree = self.root / "worktree"
        worktree.mkdir()
        subprocess.run(["git", "init", "-q", str(worktree)], check=True)
        subprocess.run(["git", "-C", str(worktree), "config", "user.email",
                        "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(worktree), "config", "user.name", "test"],
                       check=True)
        target = worktree / "preflight.py"
        target.write_text("line one\n")
        subprocess.run(["git", "-C", str(worktree), "add", "preflight.py"], check=True)
        subprocess.run(["git", "-C", str(worktree), "commit", "-q", "-m", "base"],
                       check=True)
        base = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        target.write_text("line one\nline two\n")

        ticket = self.write_json("ticket.json", make_ticket())
        context, _runner = self.context()
        context.pop("ledger_home", None)
        context.update({
            "worktree": str(worktree),
            "base_commit": base,
            "review_exemption": {
                "reason": "bounded single-file change",
                "interface_impact": "none",
                "expected_changed_files": ["preflight.py"],
                "max_changed_lines": 10,
            },
            "dashboard": {"written": True, "reason": ""},
            "inbox": {"installed": True, "healthy": True, "reason": ""},
        })
        context_path = self.write_json("context-post.json", context)
        result = self.run_cli(
            "post-dispatch", "--run-id", RUN_ID, "--ticket", str(ticket),
            "--context", str(context_path), env=self.cli_env(),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["review_exemption"]["status"], "confirmed")
        self.assertEqual(report["review_exemption"]["observed"]["changed_files"],
                         ["preflight.py"])
        kinds = [record["kind"] for record in self.ledger_records()]
        self.assertIn("observation", kinds)
        self.assertEqual(self.ledger_records(kind="preflight"), [])

        unexpected = worktree / "extra.py"
        unexpected.write_text("unexpected\n")
        subprocess.run(["git", "-C", str(worktree), "add", "extra.py"], check=True)
        result = self.run_cli(
            "post-dispatch", "--run-id", RUN_ID, "--ticket", str(ticket),
            "--context", str(context_path), env=self.cli_env(),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["review_exemption"]["status"], "revoked")
        self.assertIn("unexpected", report["review_exemption"]["reason"])


if __name__ == "__main__":
    unittest.main()
