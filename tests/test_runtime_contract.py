"""Regression checks for the Orca 1.4.248 native wake contract."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("orchestra-auto", "orchestra-auto-zh")


class NativeWakeContract(unittest.TestCase):
    def test_retired_consumer_never_calls_orca_or_changes_state(self):
        for package in PACKAGES:
            with self.subTest(package=package), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                marker = root / "called"
                fake = root / "orca"
                fake.write_text("#!/bin/sh\ntouch '$MARKER'\n".replace("'$MARKER'", '"$MARKER"'))
                fake.chmod(0o755)
                state = root / "state"
                env = dict(os.environ, ORCA_CLI_COMMAND=str(fake), MARKER=str(marker))
                script = ROOT / "skills" / package / "scripts" / "inbox-loop.py"
                result = subprocess.run([sys.executable, str(script), "run:test", str(state), "run:test"],
                                        env=env, capture_output=True, text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("native", result.stderr.lower())
                self.assertFalse(marker.exists())
                self.assertFalse(state.exists())

class ExecutableResolution(unittest.TestCase):
    def test_resolution_for_each_script_and_language(self):
        import importlib.util
        from unittest.mock import patch
        scenarios = [
            ({"ORCA_CLI_COMMAND": ""}, "linux", ""),
            ({"ORCA_CLI_COMMAND": "/tmp/custom", "ORCA_DEV_REPO_ROOT": "/dev"}, "linux", "/tmp/custom"),
            ({"ORCA_DEV_REPO_ROOT": "/dev"}, "linux", "orca-dev"),
            ({}, "linux", "orca-ide"),
            ({"ORCA_TERMINAL_HANDLE": "term"}, "linux", "orca"),
            ({"ORCA_AGENT_SESSION_ID": "session"}, "linux", "orca"),
            ({}, "darwin", "orca"),
        ]
        for package in PACKAGES:
            for script in ("preflight.py", "ledger.py", "visual-evidence.py"):
                path = ROOT / "skills" / package / "scripts" / script
                spec = importlib.util.spec_from_file_location("contract_test", path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                for environment, platform, expected in scenarios:
                    with self.subTest(package=package, script=script, environment=environment), \
                            patch.dict(os.environ, environment, clear=True), \
                            patch.object(sys, "platform", platform):
                        self.assertEqual(module.resolve_orca_cli(), expected)


class PreflightNativeWake(unittest.TestCase):
    def test_legacy_installation_cannot_claim_native_wake(self):
        import importlib.util
        for package in PACKAGES:
            path = ROOT / "skills" / package / "scripts" / "preflight.py"
            spec = importlib.util.spec_from_file_location("preflight_contract", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            legacy = module._inbox_observation({"inbox": {"installed": True, "healthy": True}})
            self.assertEqual(legacy["outcome"], "unhealthy")
            actual = module._inbox_observation({"inbox": {"native_wake": True, "healthy": True}})
            self.assertEqual(actual["outcome"], "healthy")
            malformed = module._inbox_observation({"inbox": {"native_wake": True, "healthy": "yes"}})
            self.assertEqual(malformed["outcome"], "unhealthy")
