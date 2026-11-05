#!/usr/bin/env python3
"""The retired consumer must not steal native wakeups or damage old audit data.

FakeOrca models the documented 1.4.248 check contract; it is not an end-to-end
Orca runtime. No behavior of an older server is assumed by these tests.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("orchestra-auto", "orchestra-auto-zh")
spec = importlib.util.spec_from_file_location("fake_orca", ROOT / "tests/fake_orca.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class NativeMailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fake = module.FakeOrca(self.root / "fake", check=[
            {"deliveryId": "d1", "messages": [{"id": "m1", "type": "worker_done"}]},
            {"deliveryId": "d2", "messages": [{"id": "m2", "type": "question"}]},
        ]).write()

    def call(self, *args):
        result = subprocess.run([str(self.fake.path), "orchestration", *args, "--json"],
                                text=True, capture_output=True, check=True)
        return json.loads(result.stdout)["result"]

    def test_check_marks_handled_peek_does_not_and_retry_recovers_lost_response(self):
        self.assertEqual(self.call("check", "--peek")["deliveryId"], "d1")
        first = self.call("check", "--wait")
        self.assertEqual(first["deliveryId"], "d1")
        self.assertEqual(self.call("check", "--retry-request", first["requestId"]), first)
        self.assertEqual(self.call("check")["deliveryId"], "d2")
        self.assertEqual(self.call("check")["messages"], [])
        self.assertEqual([m["id"] for m in self.call("inbox", "--full")["messages"]],
                         ["m1", "m2"])

    def test_old_entrypoints_fail_closed_preserve_state_and_do_not_steal_wake(self):
        state = self.root / "old-state"
        state.mkdir()
        (state / "delivery.json").write_text('{"historical": true}')
        before = (state / "delivery.json").read_bytes()
        env = dict(os.environ, ORCA_CLI_COMMAND=str(self.fake.path))
        for package in PACKAGES:
            for script in ("inbox-loop.py", "inbox-loop.sh"):
                for args in (("run:test", str(state), "run:test"),
                             ("--ack", "d1", str(state)), ("--status", str(state))):
                    with self.subTest(package=package, script=script, args=args):
                        entry = ROOT / "skills" / package / "scripts" / script
                        command = ([sys.executable, str(entry)] if script.endswith(".py")
                                   else [str(entry)])
                        result = subprocess.run([*command, *args], env=env,
                                                capture_output=True, text=True, timeout=10)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("native", result.stderr.lower())
        self.assertEqual((state / "delivery.json").read_bytes(), before)
        self.assertEqual(self.fake.call_argv(), [])
        # The native wake's message is still available to the agent itself.
        self.assertEqual(self.call("check")["messages"][0]["id"], "m1")

    def test_duplicate_recovery_is_deduplicated_by_message_id(self):
        first = self.call("check")
        duplicate = self.call("check", "--retry-request", first["requestId"])
        handled = set()
        actions = []
        for batch in (first, duplicate):
            for message in batch["messages"]:
                if message["id"] not in handled:
                    handled.add(message["id"])
                    actions.append(message["id"])
        self.assertEqual(actions, ["m1"])
        self.assertEqual(self.call("check")["deliveryId"], "d2")
