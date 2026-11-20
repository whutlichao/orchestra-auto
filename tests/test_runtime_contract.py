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
                assert spec is not None and spec.loader is not None
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
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            legacy = module._inbox_observation({"inbox": {"installed": True, "healthy": True}})
            self.assertEqual(legacy["outcome"], "unhealthy")
            actual = module._inbox_observation({"inbox": {"native_wake": True, "healthy": True}})
            self.assertEqual(actual["outcome"], "healthy")
            malformed = module._inbox_observation({"inbox": {"native_wake": True, "healthy": "yes"}})
            self.assertEqual(malformed["outcome"], "unhealthy")

class MessagingCapabilityTests(unittest.TestCase):
    def test_live_terminal_binding_uses_current_runtime_contract(self):
        """The TUI agent terminal shape proves identity from Orca's own inventory."""
        import importlib.util
        import json
        from unittest.mock import patch

        handle, pane = 'term_1', 'tab-1:leaf-2'

        class Runner:
            def __init__(self, leaf='leaf-2', contract=True, mailbox=True):
                self.leaf, self.contract, self.mailbox = leaf, contract, mailbox

            def run(self, argv, timeout=None):
                guide = (
                    'Orca wakes you. A wake\n  that names a `check` command (a terminal, or one\n'
                    '  message too large for a chat message) means run that command.'
                )
                outputs = {
                    ('--version',): '1.4.258\n',
                    ('skills', 'get', 'orchestration'): guide if self.contract else 'Orca wakes you.',
                    ('orchestration', 'check', '--help'): "default: return the bound Run's oldest unread FIFO batch and mark it handled. --retry-request If a check response was lost, rerun it with the --retry-request it printed to get the same batch. To find mail after losing context, run orca orchestration inbox --terminal <mailbox> --limit <n> --full",
                    ('orchestration', 'inbox', '--help'): '--terminal --full',
                    ('terminal', 'list', '--json'): json.dumps({'ok': True, 'result': {'terminals': [
                        {'handle': handle, 'tabId': 'tab-1', 'leafId': self.leaf,
                         'connected': True, 'orphaned': False}]}}),
                    ('orchestration', 'inbox', '--terminal', handle, '--limit', '1', '--json'):
                        json.dumps({'ok': self.mailbox, 'result': {'messages': []}}),
                }
                return {'returncode': 0, 'stdout': outputs.get(tuple(argv[1:]), ''), 'stderr': ''}

        context = {'messaging': {'legacy_migration_complete': True,
                                 'migration_evidence_ref': 'inventory'}}
        environment = {'ORCA_TERMINAL_HANDLE': handle, 'ORCA_AGENT_PANE': pane,
                       'ORCA_STRUCTURED_SESSION': '', 'ORCA_AGENT_SESSION_ID': ''}
        for package in PACKAGES:
            spec = importlib.util.spec_from_file_location(
                'terminal_binding_preflight', ROOT / 'skills' / package / 'scripts' / 'preflight.py')
            assert spec is not None and spec.loader is not None
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with self.subTest(package=package), patch.dict(os.environ, environment):
                item = module._check_messaging(context, Runner())
                self.assertEqual(item['result'], 'pass')
                self.assertEqual(item['observed']['session_source'], 'live_terminal_binding')
                self.assertTrue(item['observed']['terminal_probe']['contract'])
                self.assertEqual(module._check_messaging(context, Runner(leaf='leaf-9'))['result'], 'fail')
                self.assertEqual(module._check_messaging(context, Runner(contract=False))['result'], 'fail')
                self.assertEqual(module._check_messaging(context, Runner(mailbox=False))['result'], 'fail')
                self.assertEqual(module._check_messaging({}, Runner())['result'], 'fail')

    def test_live_structured_chat_uses_current_runtime_contract(self):
        import importlib.util
        import json
        from unittest.mock import patch

        class Runner:
            def __init__(self, live=True, session='session-1', chat_contract=True):
                self.live, self.session, self.chat_contract = live, session, chat_contract

            def run(self, argv, timeout=None):
                outputs = {
                    ('--version',): '1.4.250',
                    ('status', '--json'): json.dumps({'ok': True, 'result': {'caller': {
                        'orcaSessionId': self.session, 'live': self.live}}}),
                    ('skills', 'get', 'orchestration'): ('Orca wakes you. In a chat, each message arrives whole as its own message' if self.chat_contract else 'Orca wakes you.'),
                    ('orchestration', 'check', '--help'): "default: return the bound Run's oldest unread FIFO batch and mark it handled. --retry-request If a check response was lost, rerun it with the --retry-request it printed to get the same batch. To find mail after losing context, run orca orchestration inbox --terminal <mailbox> --limit <n> --full",
                    ('orchestration', 'inbox', '--help'): '--terminal --full',
                }
                return {'returncode': 0, 'stdout': outputs.get(tuple(argv[1:]), ''), 'stderr': ''}

        context = {'messaging': {'legacy_migration_complete': True, 'migration_evidence_ref': 'inventory'}}
        for package in PACKAGES:
            spec = importlib.util.spec_from_file_location('live_chat_preflight', ROOT / 'skills' / package / 'scripts' / 'preflight.py')
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with self.subTest(package=package), patch.dict(os.environ, {'ORCA_STRUCTURED_SESSION': '1', 'ORCA_AGENT_SESSION_ID': 'session-1'}, clear=True):
                self.assertEqual(module._check_messaging(context, Runner())['result'], 'pass')
                self.assertEqual(module._check_messaging(context, Runner(live=False))['result'], 'fail')
                self.assertEqual(module._check_messaging(context, Runner(session=''))['result'], 'fail')
                self.assertEqual(module._check_messaging(context, Runner(chat_contract=False))['result'], 'fail')
                self.assertEqual(module._check_messaging({}, Runner())['result'], 'fail')
                with patch.dict(os.environ, {'ORCA_STRUCTURED_SESSION': '0'}):
                    self.assertEqual(module._check_messaging(context, Runner())['result'], 'fail')

    def test_future_version_with_matching_contract_passes_with_session_evidence(self):
        import importlib.util
        class Runner:
            def run(self, argv, timeout=None):
                outputs = {
                    ('--version',): '9.99.999\n',
                    ('skills', 'get', 'orchestration'): 'Orca wakes you.',
                    ('orchestration', 'check', '--help'): "default: return the bound Run's oldest unread FIFO batch and mark it handled. --retry-request If a check response was lost, rerun it with the --retry-request it printed to get the same batch. To find mail after losing context, run orca orchestration inbox --terminal <mailbox> --limit <n> --full",
                    ('orchestration', 'inbox', '--help'): '--terminal --full',
                }
                return {'returncode': 0, 'stdout': outputs[tuple(argv[1:])], 'stderr': ''}
        context = {'messaging': {'native_wake_confirmed': True, 'source': 'live_preamble',
                   'evidence_ref': 'fixture', 'legacy_migration_complete': True,
                   'migration_evidence_ref': 'fixture'}}
        for package in PACKAGES:
            spec = importlib.util.spec_from_file_location('preflight_14250', ROOT / 'skills' / package / 'scripts' / 'preflight.py')
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with self.subTest(package=package):
                self.assertEqual(module._check_messaging(context, Runner())['result'], 'pass')
                self.assertEqual(module._check_messaging({}, Runner())['result'], 'fail')
                class MissingRecovery(Runner):
                    def run(self, argv, timeout=None):
                        result = super().run(argv, timeout)
                        if argv[1:] == ['orchestration', 'check', '--help']:
                            result['stdout'] = result['stdout'].replace('get the same batch', 'discard the batch')
                        return result
                self.assertEqual(module._check_messaging(context, MissingRecovery())['result'], 'fail')

    def test_dispatch_is_blocked_when_wake_or_recovery_is_unverified(self):
        import importlib.util
        for package in PACKAGES:
            path = ROOT / 'skills' / package / 'scripts' / 'preflight.py'
            spec = importlib.util.spec_from_file_location('preflight_message_test', path)
            assert spec is not None and spec.loader is not None
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
                        ('orchestration', 'check', '--help'): "default: return the bound Run's oldest unread FIFO batch and mark it handled. --retry-request If a check response was lost, rerun it with the --retry-request it printed to get the same batch. To find mail after losing context, run orca orchestration inbox --terminal <mailbox> --limit <n> --full",
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
                class HarmlessParaphraseRunner(Runner):
                    def run(self, argv, timeout=None):
                        result = super().run(argv, timeout)
                        if argv[1:] == ['skills', 'get', 'orchestration']:
                            result['stdout'] = 'Orca wakes you reliably on delivery.'
                        elif argv[1:] == ['orchestration', 'check', '--help']:
                            result['stdout'] = (
                                'Returns the oldest unread FIFO batch and marks it handled; '
                                'pass --retry-request to recover the same batch or run '
                                'orca orchestration inbox --terminal <id> --full.'
                            )
                        return result
                self.assertEqual(module._check_messaging(good, HarmlessParaphraseRunner())['result'], 'pass')
                item = module._check_messaging({}, Runner())
                overrides = module._apply_overrides([item], [{'item': 'message-capability', 'by': 'human', 'reason': 'skip'}])
                self.assertFalse(overrides[0]['applied'])
                self.assertTrue(module._blocking_decision([item], overrides)['blocked'])

class MessagingNegationScopeTests(unittest.TestCase):
    """A negation fails the gate only when it lands on the handled contract.

    Orca documents every check mode on one help page, so a bare phrase scan reads an
    unrelated sentence as a contract change: ``--peek`` says it returns unread messages
    "without marking them read", and that is about a read cursor, not about the consuming
    check marking its FIFO batch handled. The gate must ignore it and still fail closed
    when the selected CLI really drops the handled semantics.
    """

    REAL_CHECK_HELP = (
        "Usage: orca orchestration check [--terminal <handle>] [--run <run_id>] "
        "[--ack <delivery_id>] [--unread | --peek | --all] [--retry-request <id>] [--json]\n"
        "  default: return the bound Run's oldest unread FIFO batch and mark it handled.\n"
        "  --ack: optional; acknowledges a batch from an older Orca that does not mark it.\n"
        "  --peek: return only unread messages without marking them read.\n"
        "  --all: return recent messages for the handle; does not mark read.\n"
        "Notes:\n"
        "  A consuming check marks the batch it returns as handled, so the next check "
        "returns newer mail; no --ack is needed.\n"
        "  A script that only watches the inbox must use --peek (including --peek --wait) "
        "and must never --ack, or it takes mail the agent never sees.\n"
        "  If a check response was lost, rerun it with the --retry-request it printed to "
        "get the same batch. To find mail after losing context, run orca orchestration "
        "inbox --terminal <mailbox> --limit <n> --full\n"
    )

    @staticmethod
    def _module(package):
        import importlib.util
        path = ROOT / "skills" / package / "scripts" / "preflight.py"
        spec = importlib.util.spec_from_file_location("negation_scope_preflight", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_unrelated_negations_do_not_fail_the_contract(self):
        for package in PACKAGES:
            module = self._module(package)
            # The gate probes exactly what _check_messaging probes: whitespace collapsed
            # and lower-cased, because the help page wraps and changes case freely.
            check = module._collapse(self.REAL_CHECK_HELP).lower()
            with self.subTest(package=package):
                self.assertEqual(module._handled_negation(check), "")
                self.assertTrue(module._messaging_contract_known("orca wakes you.", check))

    def test_negations_aimed_at_handled_still_fail_closed(self):
        negations = (
            "default: return the oldest unread FIFO batch; it does not mark it handled.",
            "default: return the oldest unread FIFO batch. It never marks it handled.",
            "default: return the FIFO batch without marking it handled.",
            "default: return the FIFO batch without marking the batch as handled.",
            "checks no longer mark the batch as handled; --retry-request replays the same batch.",
            "a check leaves its FIFO batch unhandled; --retry-request replays the same batch.",
            "a lost response discards the batch; --retry-request replays the same batch.",
        )
        for package in PACKAGES:
            module = self._module(package)
            for text in negations:
                with self.subTest(package=package, text=text):
                    self.assertTrue(module._handled_negation(text.lower()), text)
                    self.assertFalse(
                        module._messaging_contract_known("Orca wakes you.", text.lower()), text)

    def test_message_capability_passes_on_the_current_help_page(self):
        for package in PACKAGES:
            module = self._module(package)
            self_help = self.REAL_CHECK_HELP

            class Runner:
                def run(self, argv, timeout=None):
                    outputs = {
                        ("--version",): "1.4.248\n",
                        ("skills", "get", "orchestration"): "Orca wakes you.\n",
                        ("orchestration", "check", "--help"): self_help,
                        ("orchestration", "inbox", "--help"): "--terminal --full",
                    }
                    key = tuple(argv[1:])
                    return {"returncode": 0 if key in outputs else 1,
                            "stdout": outputs.get(key, ""), "stderr": ""}

            context = {"messaging": {
                "native_wake_confirmed": True, "source": "live_preamble",
                "evidence_ref": "session evidence", "legacy_migration_complete": True,
                "migration_evidence_ref": "inventory evidence"}}
            with self.subTest(package=package):
                item = module._check_messaging(context, Runner())
                self.assertEqual(item["result"], "pass", item["note"])
                self.assertEqual(item["observed"]["semantic_basis"], "documented_contract")
                self.assertEqual(item["observed"]["handled_negation"], "")
                self.assertEqual(item["evidence_ref"], "session evidence")

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
            assert spec is not None and spec.loader is not None
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

    def test_live_1_4_265_guide_and_help_wording_pass_without_human_override(self):
        """Captured Orca 1.4.265 guide/help phrasing must satisfy _check_messaging directly."""
        import importlib.util
        import json
        from unittest.mock import patch

        guide_1_4_265 = (
            "## When Orca wakes you\n"
            "- In a chat, each message arrives whole as its own message: act on it. A wake\n"
            "  that names a `check` command (a terminal, or one message too large for a chat\n"
            "  message) means run that command.\n"
        )
        check_help_1_4_265 = (
            "Usage: orca orchestration check [--terminal <handle>] [--run <run_id>] "
            "[--ack <delivery_id>] [--unread | --peek | --all] [--types <type,...>] "
            "[--format] [--wait] [--timeout-ms <n>] [--retry-request <id>] [--json]\n"
            "  default: return the bound Run's oldest unread FIFO batch and mark it handled.\n"
            "  --peek: return only unread messages without marking them read.\n"
            "  --retry-request <id>  Reissue an earlier request by its ID; replays its result instead of repeating it\n"
            "Notes:\n"
            "  A consuming check marks the batch it returns as handled, so the next check returns newer mail; no --ack is needed.\n"
            "  If a check response was lost, rerun it with the --retry-request it printed to get the same batch. "
            "To find mail after losing context, run orca orchestration inbox --terminal <mailbox> --limit <n> --full\n"
        )
        inbox_help_1_4_265 = (
            "Usage: orca orchestration inbox [--limit <n>] [--terminal <handle>] [--run <run_id>] [--full] [--json]\n"
            "  --terminal <handle>  Runtime-issued terminal handle\n"
            "  --full                 Print the full record Orca returned instead of the concise view\n"
        )
        handle, pane = "term_1", "tab-1:leaf-2"

        class Runner:
            def run(self, argv, timeout=None):
                outputs = {
                    ("--version",): "1.4.265\n",
                    ("skills", "get", "orchestration"): guide_1_4_265,
                    ("orchestration", "check", "--help"): check_help_1_4_265,
                    ("orchestration", "inbox", "--help"): inbox_help_1_4_265,
                    ("terminal", "list", "--json"): json.dumps({
                        "ok": True,
                        "result": {"terminals": [{
                            "handle": handle, "tabId": "tab-1", "leafId": "leaf-2",
                            "connected": True, "orphaned": False,
                        }]},
                    }),
                    ("orchestration", "inbox", "--terminal", handle, "--limit", "1", "--json"):
                        json.dumps({"ok": True, "result": {"messages": []}}),
                }
                value = outputs.get(tuple(argv[1:]))
                return {"returncode": 0 if value is not None else 1,
                        "stdout": value or "", "stderr": ""}

        migration = {"legacy_migration_complete": True, "migration_evidence_ref": "native"}
        for package in PACKAGES:
            spec = importlib.util.spec_from_file_location(
                "contract_1_4_265", ROOT / "skills" / package / "scripts" / "preflight.py"
            )
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with self.subTest(package=package), patch.dict(
                os.environ, {"ORCA_TERMINAL_HANDLE": handle, "ORCA_AGENT_PANE": pane}, clear=True
            ):
                item = module._check_messaging({"messaging": migration}, Runner())
                self.assertEqual(item["result"], "pass", item["note"])
                self.assertEqual(item["observed"]["semantic_basis"], "documented_contract")
                self.assertEqual(item["observed"]["session_source"], "live_terminal_binding")
