#!/usr/bin/env python3
"""Document contracts: a reference cannot promise a command order it does not show.

The failed-dispatch recovery is the one place a coordinator is tempted to release from
memory. Both language bundles must therefore present the same executable order: read the
settlement back, persist the failure closeout, ask the script to authorize, and only then
run the ``worker-release`` command it printed. A release command that appears before the
closeout command is a bypass path.
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_NAMES = ("orchestra-auto", "orchestra-auto-zh")
REFERENCE = Path("references") / "failed-dispatch-recovery.md"
INBOX_REFERENCE = Path("references") / "inbox-and-waiting.md"

#: The delivery guarantee and the old promise it replaced. A document may state the
#: at-least-once wake contract or the no-duplicate claim, never both: a duplicate wake
#: after a crash is the accepted cost of never losing one.
AT_LEAST_ONCE = re.compile(r"at-least-once|至少一次", re.IGNORECASE)
NO_DUPLICATE_WAKE = re.compile(r"no\s+duplicate\s+wake|不重复\s*唤醒", re.IGNORECASE)

#: A closeout command, whether spelled through the bundle path or a ``$CLOSEOUT`` variable.
RECORD_COMMAND = re.compile(
    r'(?:closeout\.py"?|\$\{?CLOSEOUT\}?"?)\s+failure-record\b')
PLAN_COMMAND = re.compile(
    r'(?:closeout\.py"?|\$\{?CLOSEOUT\}?"?)\s+failure-plan\b')
#: The only release invocation shape: a command line, not a passing prose mention.
RELEASE_COMMAND = re.compile(
    r"^\s*(?:<ORCA>|orca)\s+orchestration\s+worker-release\b", re.M | re.I)


class DocContractTests(unittest.TestCase):
    """The two recovery references carry the script-enforced sequence."""

    def text(self, name):
        return (REPO_ROOT / "skills" / name / REFERENCE).read_text()

    def test_failed_dispatch_recovery_calls_closeout_before_release(self):
        for name in SKILL_NAMES:
            with self.subTest(bundle=name):
                text = self.text(name)
                record = [match.start() for match in RECORD_COMMAND.finditer(text)]
                plan = [match.start() for match in PLAN_COMMAND.finditer(text)]
                release = [match.start()
                           for match in RELEASE_COMMAND.finditer(text)]
                self.assertTrue(record, f"{name}: no failure-record command")
                self.assertTrue(plan, f"{name}: no failure-plan command")
                self.assertTrue(release, f"{name}: no worker-release command")
                self.assertLess(min(record), min(plan),
                                f"{name}: failure-plan must follow failure-record")
                self.assertLess(min(plan), min(release),
                                f"{name}: release must follow failure-plan")
                # No bypass path: every release command follows both closeout commands.
                for position in release:
                    self.assertTrue(
                        any(item < position for item in record),
                        f"{name}: a worker-release command bypasses failure-record")
                    self.assertTrue(
                        any(item < position for item in plan),
                        f"{name}: a worker-release command bypasses failure-plan")
                # The JSON the script prints is the contract the prose points at.
                for field in ('"next_action"', '"allowed"', '"recorded"',
                              '"durable"', '"classification"', '"evidence_refs"',
                              '"mapping"'):
                    self.assertIn(field, text,
                                  f"{name}: the recovery JSON contract lost {field}")
                # The old direct-release sentence must not survive as a bypass path.
                self.assertNotIn("Then release the failed Dispatch", text)
                self.assertNotIn("然后 release 掉那个失败 Dispatch", text)


class InboxWakeContractTests(unittest.TestCase):
    """The inbox reference cannot promise at-least-once and no-duplicate at once."""

    def test_a_document_that_claims_at_least_once_never_promises_no_duplicate_wake(self):
        for name in SKILL_NAMES:
            with self.subTest(bundle=name):
                text = (REPO_ROOT / "skills" / name / INBOX_REFERENCE).read_text()
                if not AT_LEAST_ONCE.search(text):
                    continue
                contradiction = NO_DUPLICATE_WAKE.search(text)
                self.assertIsNone(
                    contradiction,
                    f"{name}: at-least-once wake cannot also promise no duplicate wake")


#: The two identity classes the inbox reference must name, and the env variables that
#: resolve them. ``caller`` alone stalled a real coordinator: an Orca-launched TUI agent
#: terminal has ``ORCA_TERMINAL_HANDLE`` but never ``ORCA_AGENT_SESSION_ID``.
IDENTITY_MARKERS = (
    "ORCA_AGENT_SESSION_ID",
    "ORCA_TERMINAL_HANDLE",
    "ORCA_AGENT_PANE",
    "caller.orcaSessionId",
    "orca terminal list",
)
#: The old failure-table sentence that made ``orca status --json`` the only source.
SINGLE_SOURCE_CLAIMS = (
    "handle 取自真实调用方身份（`orca status --json`）",
    "Take handles from the real calling identity (`orca status --json`)",
)


class InboxIdentityTests(unittest.TestCase):
    """A missing caller is a missing session id, not a missing coordinator."""

    def test_each_bundle_names_both_identity_classes(self):
        for name in SKILL_NAMES:
            with self.subTest(bundle=name):
                text = (REPO_ROOT / "skills" / name / INBOX_REFERENCE).read_text()
                for marker in IDENTITY_MARKERS:
                    self.assertIn(marker, text,
                                  f"{name}: identity section lost {marker}")

    def test_status_is_no_longer_the_only_handle_source(self):
        for name in SKILL_NAMES:
            with self.subTest(bundle=name):
                text = (REPO_ROOT / "skills" / name / INBOX_REFERENCE).read_text()
                for claim in SINGLE_SOURCE_CLAIMS:
                    self.assertNotIn(
                        claim, text,
                        f"{name}: the failure table still makes status the only source")

    def test_the_handle_check_is_runnable_prose(self):
        """The documented check must quote both variables, not just describe them."""
        for name in SKILL_NAMES:
            with self.subTest(bundle=name):
                text = (REPO_ROOT / "skills" / name / INBOX_REFERENCE).read_text()
                self.assertIn('"$ORCA_TERMINAL_HANDLE"', text)
                self.assertIn('os.environ["ORCA_AGENT_PANE"]', text)


class RuntimeMigrationContractTests(unittest.TestCase):
    def test_pr_only_scope_never_grants_merge(self):
        cases = {
            'orchestra-auto': ('asking you to open a PR is not one',
                               'A PR-only request stops with the PR open'),
            'orchestra-auto-zh': ('项目改动默认自动创建并合并 PR',
                                  '只要求开 PR 时止于开放 PR'),
        }
        for name, (forbidden, required) in cases.items():
            with self.subTest(bundle=name):
                text = (REPO_ROOT / 'skills' / name / 'SKILL.md').read_text()
                self.assertNotIn(forbidden, text)
                self.assertIn(required, text)

    def test_existing_consumer_is_quiesced_before_local_archive_reconciliation(self):
        cases = {
            'orchestra-auto': ('## Legacy consumer migration', 'inbox.lock',
                               'verified process identity', 'stop its launcher',
                               'confirm exit and no surviving check',
                               'deliveries/*.json', 'receipts.jsonl',
                               'successful ack is not proof', 'message ID',
                               'block migration', 'failed or missing receipt',
                               'latest 50 inbox rows', 'Never automatically repeat'),
            'orchestra-auto-zh': ('## 旧收件器迁移', 'inbox.lock',
                                  '核实进程身份', '停止其启动器',
                                  '确认进程退出且没有遗留 check',
                                  'deliveries/*.json', 'receipts.jsonl',
                                  'ack 成功不证明', '消息 ID', '阻塞迁移',
                                  '回执失败或缺失', 'inbox 最近 50 条', '不自动重复'),
        }
        for name, markers in cases.items():
            with self.subTest(bundle=name):
                text = (REPO_ROOT / 'skills' / name / INBOX_REFERENCE).read_text()
                for marker in markers:
                    self.assertIn(marker, text)
                self.assertLess(text.index(markers[4]), text.index(markers[5]))


if __name__ == "__main__":
    unittest.main()
