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

class MessagingCapabilityTests(unittest.TestCase):
    def test_dispatch_is_blocked_when_wake_or_recovery_is_unverified(self):
        import importlib.util
        for package in PACKAGES:
            path = ROOT / 'skills' / package / 'scripts' / 'preflight.py'
            spec = importlib.util.spec_from_file_location('preflight_message_test', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            class Runner:
                def __init__(self, missing=None):
                    self.missing = missing
                def run(self, argv, timeout=None):
                    key = tuple(argv[1:])
                    outputs = {
                        ('--version',): '1.4.248\n',
                        ('skills', 'get', 'orchestration'): 'Orca wakes you.\nWhen Orca wakes you',
                        ('orchestration', 'check', '--help'): "default: return the bound Run's oldest unread FIFO batch and mark it handled. --retry-request",
                        ('orchestration', 'inbox', '--help'): '--terminal --full',
                    }
                    return {'returncode': 1 if key == self.missing else 0,
                            'stdout': outputs.get(key, ''), 'stderr': ''}
            good = {'messaging': {'native_wake_confirmed': True,
                                  'source': 'live_preamble', 'evidence_ref': 'session evidence',
                                  'legacy_migration_complete': True,
                                  'migration_evidence_ref': 'inventory evidence'}}
            with self.subTest(package=package):
                self.assertEqual(module._check_messaging(good, Runner())['result'], 'pass')
                self.assertEqual(module._check_messaging({}, Runner())['result'], 'fail')
                incomplete = {'messaging': dict(good['messaging'], legacy_migration_complete=False)}
                self.assertEqual(module._check_messaging(incomplete, Runner())['result'], 'fail')
                self.assertEqual(module._check_messaging(good, Runner(('orchestration', 'inbox', '--help')))['result'], 'fail')
                class NegativeRunner(Runner):
                    def run(self, argv, timeout=None):
                        result = super().run(argv, timeout)
                        if argv[1:] == ['orchestration', 'check', '--help']:
                            result['stdout'] = 'does not mark it handled --retry-request'
                        return result
                self.assertEqual(module._check_messaging(good, NegativeRunner())['result'], 'fail')
                class RewordedRunner(Runner):
                    def run(self, argv, timeout=None):
                        result = super().run(argv, timeout)
                        if argv[1:] == ['orchestration', 'check', '--help']:
                            result['stdout'] = 'returns the oldest unread FIFO batch and immediately marks it handled --retry-request'
                        return result
                self.assertEqual(module._check_messaging(good, RewordedRunner())['result'], 'fail')
                item = module._check_messaging({}, Runner())
                overrides = module._apply_overrides([item], [{'item': 'message-capability', 'by': 'human', 'reason': 'skip'}])
                self.assertFalse(overrides[0]['applied'])
                self.assertTrue(module._blocking_decision([item], overrides)['blocked'])

class MessagingVersionAdaptationTests(unittest.TestCase):
    def test_first_session_and_evidence_bound_human_review(self):
        import hashlib
        import importlib.util
        outputs = {
            ('--version',): '2.0.0\n',
            ('skills', 'get', 'orchestration'): 'Messages wake the coordinator in this session.',
            ('orchestration', 'check', '--help'): 'A read handles the batch. --retry-request',
            ('orchestration', 'inbox', '--help'): '--terminal --full',
        }
        class Runner:
            def __init__(self, data): self.data = data
            def run(self, argv, timeout=None):
                value = self.data.get(tuple(argv[1:]))
                return {'returncode': 0 if value is not None else 1,
                        'stdout': value or '', 'stderr': ''}
        hashes = {'/'.join(key): hashlib.sha256(value.encode()).hexdigest()
                  for key, value in outputs.items()}
        messaging = {'native_wake_confirmed': True, 'source': 'live_preamble',
                     'evidence_ref': 'live preamble promises wake for this session',
                     'legacy_migration_complete': True, 'migration_evidence_ref': 'first session inventory',
                     'human_verification': {'reviewed_by': 'human', 'evidence_ref': 'review evidence',
                                            'version': '2.0.0', 'probe_sha256': hashes,
                                            'native_wake': True, 'handled_check': True,
                                            'retry_recovery': True, 'inbox_recovery': True}}
        for package in PACKAGES:
            spec = importlib.util.spec_from_file_location('version_probe', ROOT / 'skills' / package / 'scripts/preflight.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with self.subTest(package=package):
                self.assertEqual(module._check_messaging({'messaging': messaging}, Runner(outputs))['result'], 'pass')
                incomplete_review = dict(messaging)
                incomplete_review['human_verification'] = dict(messaging['human_verification'], handled_check=False)
                self.assertEqual(module._check_messaging({'messaging': incomplete_review}, Runner(outputs))['result'], 'fail')
                wrong_version = dict(messaging)
                wrong_version['human_verification'] = dict(messaging['human_verification'], version='1.0.0')
                self.assertEqual(module._check_messaging({'messaging': wrong_version}, Runner(outputs))['result'], 'fail')
                no_review = dict(messaging); no_review.pop('human_verification')
                self.assertEqual(module._check_messaging({'messaging': no_review}, Runner(outputs))['result'], 'fail')
                changed = dict(outputs); changed[('orchestration', 'check', '--help')] = 'unsupported --retry-request'
                self.assertEqual(module._check_messaging({'messaging': messaging}, Runner(changed))['result'], 'fail')
                unsupported = dict(outputs); unsupported.pop(('orchestration', 'inbox', '--help'))
                self.assertEqual(module._check_messaging({'messaging': messaging}, Runner(unsupported))['result'], 'fail')
