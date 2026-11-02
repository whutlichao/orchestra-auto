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


if __name__ == "__main__":
    unittest.main()
