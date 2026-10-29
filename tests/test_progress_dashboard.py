"""Public CLI, snapshot and HTTP contracts for the progress sidecar."""
import fcntl
import time
import concurrent.futures
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
import subprocess
import sys
import tempfile
import re
import signal
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'skills/orchestra-auto/scripts/progress.py'


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # macOS TMPDIR lives under /var, a symlink to /private/var; progress.py
        # rejects any state path with a symlink component, so fixtures must use
        # the resolved root. The symlink-boundary test below creates its own link.
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / 'audit' / 'real'
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        (self.repo / '.git').mkdir()

    def cli(self, command, data=None, event='e1', source='coordinator', ok=True,
            global_flags=(), flags=()):
        args = [sys.executable, str(SCRIPT), '--state', str(self.state),
                *global_flags, command]
        if command != 'show':
            args += ['--event-id', event, '--data', json.dumps(data or {}),
                     '--source', source, *flags]
        result = subprocess.run(args, text=True, capture_output=True)
        self.assertEqual(result.returncode == 0, ok, result.stderr)
        return result

    def init(self):
        self.cli('init', {'id': 'run1', 'target_repo': str(self.repo), 'mode': 'REAL',
                          'phase': 'implementation', 'status': 'active'})

    def snapshot(self):
        return json.loads(self.cli('show').stdout)

    def test_init_and_restart_read_versioned_external_snapshot(self):
        self.init()
        state = self.snapshot()
        self.assertEqual(state['schema_version'], 2)
        self.assertEqual(state['run']['id'], 'run1')
        self.assertEqual(state['run']['mode'], 'REAL')
        self.assertEqual(state['run']['observation_mode'], 'explicit-coordinator')
        self.assertTrue(state['run']['started_at'])
        self.assertFalse((self.repo / 'progress.json').exists())


    def test_open_starts_viewer_and_reports_browser_failure(self):
        self.init()
        (self.root / 'sitecustomize.py').write_text('import webbrowser\nwebbrowser.open = lambda url: False\n')
        env = {**os.environ, 'PYTHONPATH': str(self.root)}
        result = subprocess.run([sys.executable, str(SCRIPT), '--state', str(self.state), 'open'],
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        info = json.loads(result.stdout)
        self.addCleanup(os.kill, info['pid'], signal.SIGTERM)
        self.assertFalse(info['browser_opened'])
        self.assertIn('manually', result.stderr)
        with urllib.request.urlopen(info['url'] + 'snapshot', timeout=5) as response:
            self.assertEqual(json.load(response)['run']['id'], 'run1')

    def background_viewer(self, script=SCRIPT):
        """Run ``open`` with a stubbed browser so the test never launches a real window."""
        (self.root / 'sitecustomize.py').write_text(
            'import webbrowser\nwebbrowser.open = lambda url: True\n')
        return subprocess.run([sys.executable, str(script), '--state', str(self.state), 'open'],
                              env={**os.environ, 'PYTHONPATH': str(self.root)},
                              capture_output=True, text=True, timeout=10)

    def test_open_reports_url_and_a_background_pid_that_outlives_it(self):
        self.init()
        result = self.background_viewer()
        self.assertEqual(result.returncode, 0, result.stderr)
        info = json.loads(result.stdout)
        self.addCleanup(os.kill, info['pid'], signal.SIGTERM)
        self.assertTrue(info['browser_opened'])
        self.assertEqual(result.stderr, '')
        self.assertTrue(info['url'].startswith('http://127.0.0.1:'))
        # The parent has exited, so reading here proves the viewer was detached.
        with urllib.request.urlopen(info['url'] + 'snapshot', timeout=5) as response:
            self.assertEqual(json.load(response)['run']['id'], 'run1')

    def test_open_gives_each_run_its_own_free_port(self):
        self.init()
        opened = [self.background_viewer() for _ in range(2)]
        for result in opened:
            self.assertEqual(result.returncode, 0, result.stderr)
        infos = [json.loads(result.stdout) for result in opened]
        for info in infos:
            self.addCleanup(os.kill, info['pid'], signal.SIGTERM)
        self.assertNotEqual(infos[0]['url'], infos[1]['url'])
        for info in infos:
            with urllib.request.urlopen(info['url'] + 'snapshot', timeout=5) as response:
                self.assertEqual(json.load(response)['run']['id'], 'run1')

    def test_open_warns_instead_of_hanging_when_the_viewer_cannot_start(self):
        self.init()
        incomplete = self.root / 'incomplete-skill' / 'progress.py'
        incomplete.parent.mkdir()
        incomplete.write_bytes(SCRIPT.read_bytes())  # the page asset is absent
        result = self.background_viewer(incomplete)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        self.assertIn('dashboard server failed to start', result.stderr)
        self.assertIn('continue orchestration', result.stderr)

    def task(self, identity='t1', **extra):
        self.cli('task', {'id': identity, 'title': identity, **extra}, event='task-' + identity)

    def worker(self):
        self.cli('worker', {'id': 'w1', 'task_id': 't1', 'dispatch_id': 'd1', 'attempt': 1,
                            'start_confirmation': 'observed', 'lifecycle': 'running'}, event='start')

    def test_reports_do_not_accept_or_merge_and_delivery_fields_are_independent(self):
        self.init()
        self.task(status='running')
        self.worker()
        before = self.snapshot()['run']['last_effective_progress']
        self.cli('worker', {'id': 'w1', 'task_id': 't1', 'dispatch_id': 'd1', 'attempt': 1, 'reported_lifecycle': 'reported_done',
                            'reported_summary': 'Tests pass'}, event='done', source='worker')
        state = self.snapshot()
        self.assertEqual(state['workers']['w1']['lifecycle'], 'running')
        self.assertEqual(state['tasks']['t1']['status'], 'running')
        self.assertEqual({key: state['deliveries']['t1'][key] for key in
                          ('implemented', 'integrated', 'merged', 'tracker_closed')},
                         {'implemented': None, 'integrated': None, 'merged': None, 'tracker_closed': None})
        self.assertEqual(state['run']['last_effective_progress'], before)
        self.assertIsNotNone(state['workers']['w1']['last_observed'])
        self.cli('delivery', {'task_id': 't1', 'implemented': True}, event='accepted')
        self.cli('artifact', {'id': 'pr1', 'task_id': 't1', 'kind': 'pr', 'label': 'PR',
                              'reference': 'https://example.com/pull/1'}, event='pr')
        self.assertIsNone(self.snapshot()['deliveries']['t1']['merged'])
        self.cli('delivery', {'task_id': 't1', 'merged': True}, event='merge')
        self.assertIsNone(self.snapshot()['deliveries']['t1']['tracker_closed'])
        self.cli('delivery', {'task_id': 't1', 'implemented': True}, event='forged',
                 source='worker', ok=False)
        self.cli('worker', {'id': 'w1', 'lifecycle': 'closed'}, event='forged2',
                 source='worker', ok=False)

    def test_dependency_wait_failure_rejection_pause_and_nested_identity(self):
        self.init()
        self.task(status='rejected', blocked_reason='Review needs corrections')
        self.task('t2', dependencies=['t1'], status='blocked', blocked_reason='Dependency rejected')
        self.task('decision', status='waiting_user', pending_decision='Choose release date')
        self.worker()
        self.cli('worker', {'id': 'child', 'task_id': 't2', 'dispatch_id': 'child-d', 'attempt': 2,
                            'parent_worker_id': 'w1', 'lifecycle': 'lost'}, event='child')
        self.cli('worker', {'id': 'w1', 'dispatch_id': 'changed'}, event='identity', ok=False)
        self.cli('end', {'status': 'paused', 'summary': 'Awaiting decision'}, event='pause')
        state = self.snapshot()
        self.assertEqual(state['tasks']['t2']['dependencies'], ['t1'])
        self.assertEqual(state['tasks']['decision']['pending_decision'], 'Choose release date')
        self.assertEqual(state['workers']['child']['parent_worker_id'], 'w1')
        self.assertEqual(state['run']['status'], 'paused')
        self.assertTrue(state['run']['ended_at'])

    def test_stale_attempt_reports_are_rejected_and_effective_progress_is_explicit(self):
        self.init()
        self.task()
        self.worker()
        before = (self.state / 'progress.json').read_bytes()
        self.cli('worker', {'id': 'w1', 'task_id': 't1', 'dispatch_id': 'old', 'attempt': 1,
                            'reported_lifecycle': 'reported_done'}, event='stale', source='worker', ok=False)
        self.cli('event', {'type': 'worker_done', 'task_id': 't1', 'worker_id': 'w1',
                           'dispatch_id': 'd1', 'attempt': 2, 'summary': 'Stale attempt'},
                 event='stale-event', source='worker', ok=False)
        self.assertEqual((self.state / 'progress.json').read_bytes(), before)
        args = [sys.executable, str(SCRIPT), '--state', str(self.state), 'worker',
                '--event-id', 'effective', '--effective', '--data',
                json.dumps({'id': 'w1', 'lifecycle': 'running'})]
        result = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.snapshot()
        self.assertIsNotNone(state['run']['last_effective_progress'])
        self.assertEqual(state['workers']['w1']['last_effective_progress'],
                         state['run']['last_effective_progress'])

    def test_optional_recording_wrapper_continues_under_set_e_and_documents_all_checkpoints(self):
        for language in ('orchestra-auto', 'orchestra-auto-zh'):
            doc = (ROOT / 'skills' / language / 'references/progress-dashboard.md').read_text()
            wrapper = re.search(r'progress_record\(\) \{.*?\n\}', doc, re.S).group()
            result = subprocess.run(['bash', '-c', 'set -e\nPROGRESS=/nonexistent\nSTATE="$1"\n' +
                                     wrapper + '\nprogress_record show\nprintf continued', '_', str(self.state)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, 'continued')
            for token in ('task_id', 'dispatch_id', 'attempt', 'parent_worker_id', 'G-impl',
                          'G-merge', 'tracker_closed', 'implemented', 'integrated', 'merged',
                          'last_observed', 'last_effective_progress', 'pending_decision'):
                self.assertIn(token, doc)
        # No Orca/inbox dependency is needed even when the progress service is absent.
        self.init()
        self.task(status='running')
        self.assertEqual(self.snapshot()['tasks']['t1']['status'], 'running')

    def test_both_languages_document_automatic_open_and_the_demo_serve_difference(self):
        claims = (
            ('skills/orchestra-auto/SKILL.md',
             ('Before the first dispatch', 'open the dashboard automatically')),
            ('skills/orchestra-auto-zh/SKILL.md', ('首次派活前', '自动打开')),
            ('skills/orchestra-auto/references/progress-dashboard.md',
             ('before the first dispatch', 'manually', 'kill PID', 'does not open a browser')),
            ('skills/orchestra-auto-zh/references/progress-dashboard.md',
             ('首次派活前', '手动', 'kill PID', '不打开浏览器')),
            ('README.md', ('before the first dispatch', 'opened manually', '`serve`',
                           'never opens a browser')),
            ('README.zh-CN.md', ('首次派活前', '手动访问', '`serve`', '不打开浏览器')),
        )
        for relative, tokens in claims:
            text = (ROOT / relative).read_text()
            for token in tokens:
                self.assertIn(token, text, f'{relative}: does not document {token!r}')

    def test_stub_workflow_trace_is_identical_disabled_working_failing_and_viewer_down(self):
        # This fixture CLI represents normal coordinator actions, not a new Orca API.
        # A separate forbidden-orca executable catches any unexpected real CLI call.
        stub = self.root / 'action.py'
        stub.write_text("import sys\nwith open(sys.argv[1], 'a') as f: f.write(sys.argv[2] + '\\n')\n")
        forbidden = self.root / 'orca'
        forbidden.write_text('#!/bin/sh\nprintf unexpected-orca >> "$ORCA_TRACE"\nexit 91\n')
        forbidden.chmod(0o700)
        wrapper = re.search(r'progress_record\(\) \{.*?\n\}',
                            (ROOT / 'skills/orchestra-auto/references/progress-dashboard.md').read_text(),
                            re.S).group()
        actions = ['dispatch', 'start-confirmed', 'question', 'wait-user', 'answer', 'worker-done',
                   'reject', 'retry', 'accept', 'integrate', 'merge', 'tracker-close', 'end']
        traces = {}
        for mode in ('disabled', 'working', 'failing', 'viewer-down'):
            state = self.root / mode
            trace = self.root / (mode + '.trace')
            unexpected = self.root / (mode + '.orca')
            if mode == 'viewer-down':
                address, viewer = self.serve(state, with_process=True)
                # Starting and stopping the viewer does not create the state or own the recorder.
                with urllib.request.urlopen(address) as response:
                    self.assertEqual(response.status, 200)
                viewer.terminate()
                viewer.communicate(timeout=5)
            progress = str(SCRIPT) if mode != 'failing' else '/nonexistent-progress.py'
            shell = 'set -e\n' + wrapper + '\n'
            if mode == 'disabled':
                shell += 'progress_record() { :; }\n'
            shell += 'progress_record init --event-id init --data "$INIT"\n'
            for index, action in enumerate(actions):
                shell += '"$PYTHON" "$STUB" "$TRACE" ' + action + '\n'
                shell += ('progress_record event --event-id step-' + str(index) +
                          ' --data \'{"type":"' + action + '","summary":"stub action complete"}\'\n')
            result = subprocess.run(['bash', '-c', shell], capture_output=True, text=True,
                                    env={**os.environ, 'PROGRESS': progress, 'STATE': str(state),
                                         'PYTHON': sys.executable, 'STUB': str(stub), 'TRACE': str(trace),
                                         'INIT': json.dumps({'id': mode, 'target_repo': str(self.repo),
                                                             'mode': 'REAL'}),
                                         'PATH': str(self.root) + os.pathsep + os.environ['PATH'],
                                         'ORCA_TRACE': str(unexpected)})
            self.assertEqual(result.returncode, 0, result.stderr)
            traces[mode] = trace.read_text().splitlines()
            self.assertEqual(traces[mode], actions)
            self.assertFalse(unexpected.exists(), 'progress must never call Orca/check/ack')
            if mode == 'failing':
                self.assertIn('Progress warning; continue orchestration', result.stderr)
            if mode in ('working', 'viewer-down'):
                recorded = json.loads((state / 'progress.json').read_text())
                self.assertEqual([e['type'] for e in recorded['events'][1:]], actions)
        self.assertEqual(traces['disabled'], traces['working'])
        self.assertEqual(traces['working'], traces['failing'])
        self.assertEqual(traces['working'], traces['viewer-down'])

    def test_top_level_parent_identity_cannot_change_or_form_a_cycle(self):
        self.init()
        self.task()
        self.worker()
        self.cli('worker', {'id': 'child', 'task_id': 't1', 'dispatch_id': 'child-d', 'attempt': 1,
                            'parent_worker_id': 'w1'}, event='child')
        before = (self.state / 'progress.json').read_bytes()
        self.cli('worker', {'id': 'w1', 'parent_worker_id': 'child'}, event='cycle', ok=False)
        self.assertEqual((self.state / 'progress.json').read_bytes(), before)
        self.assertIsNone(self.snapshot()['workers']['w1']['parent_worker_id'])
        self.cli('worker', {'id': 'missing-parent', 'task_id': 't1', 'dispatch_id': 'missing-d',
                            'attempt': 1, 'parent_worker_id': 'absent'}, event='missing', ok=False)

    def test_duplicate_events_are_idempotent_and_payload_collisions_fail(self):
        self.init()
        data = {'type': 'observed', 'summary': 'No state transition'}
        self.cli('event', data, event='repeat')
        before = (self.state / 'progress.json').read_bytes()
        self.cli('event', data, event='repeat')
        self.assertEqual((self.state / 'progress.json').read_bytes(), before)
        self.cli('event', {**data, 'summary': 'Different'}, event='repeat', ok=False)
        self.assertEqual((self.state / 'progress.json').read_bytes(), before)

    def test_contended_lock_times_out_and_wrapper_can_continue_without_mutation(self):
        self.init()
        before = (self.state / 'progress.json').read_bytes()
        with (self.state / '.progress.lock').open('a') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            started = time.monotonic()
            result = subprocess.run([sys.executable, str(SCRIPT), '--state', str(self.state),
                                     'run', '--event-id', 'locked', '--data', '{"phase":"blocked"}'],
                                    capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('lock busy', result.stderr)
            self.assertLess(time.monotonic() - started, 4)
        self.assertEqual((self.state / 'progress.json').read_bytes(), before)
        self.cli('run', {'phase': 'recovered'}, event='unlocked')
        self.assertEqual(self.snapshot()['run']['phase'], 'recovered')

    def test_process_concurrency_and_abandoned_temporary_file_recovery(self):
        self.init()
        (self.state / '.progress-abandoned').write_text('{partial')
        def write(index):
            return self.cli('event', {'type': 'heartbeat', 'summary': str(index)},
                            event='concurrent-' + str(index))
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(24)))
        state = self.snapshot()
        self.assertEqual(len(state['events']), 25)
        self.assertEqual(len({e['id'] for e in state['events']}), 25)
        self.cli('run', {'phase': 'verification'}, event='restart')
        self.assertEqual(self.snapshot()['run']['phase'], 'verification')

    def test_unknown_fields_invalid_types_and_repository_boundaries_do_not_write(self):
        self.init()
        before = (self.state / 'progress.json').read_bytes()
        for data in ({'id': 't', 'raw_message': 'forbidden'}, {'id': 't', 'dependencies': 'bad'},
                     {'id': 't', 'status': 'invented'}):
            self.cli('task', data, event='bad', ok=False)
        self.assertEqual((self.state / 'progress.json').read_bytes(), before)
        original = self.state
        for path in (self.repo / 'audit', self.repo, self.repo.parent):
            self.state = path
            self.cli('init', {'id': 'bad', 'target_repo': str(self.repo), 'mode': 'REAL'}, ok=False)
            self.assertFalse((path / 'progress.json').exists())
        self.state = original
        link = self.root / 'link'
        link.symlink_to(self.repo, target_is_directory=True)
        self.state = link / 'audit'
        self.cli('init', {'id': 'bad', 'target_repo': str(self.repo), 'mode': 'REAL'}, ok=False)
        self.assertFalse((self.repo / 'audit').exists())

    def serve(self, state=None, stale=300, with_process=False):
        process = subprocess.Popen([sys.executable, str(SCRIPT), '--state', str(state or self.state),
                                    'serve', '--port', '0', '--stale-seconds', str(stale)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cleanup():
            process.terminate()
            process.communicate(timeout=5)
        self.addCleanup(cleanup)
        address = process.stdout.readline().strip().split()[-1]
        return (address, process) if with_process else address

    def test_http_is_read_only_localhost_stale_and_never_serves_file_paths(self):
        self.init()
        self.task()
        self.worker()
        url = self.serve(stale=0)
        self.assertTrue(url.startswith('http://127.0.0.1:'))
        state = json.load(urllib.request.urlopen(url + 'snapshot'))
        self.assertEqual(state['workers']['w1']['freshness'], 'stale')
        self.assertNotIn('receipts', state)
        before = (self.state / 'progress.json').read_bytes()
        for path in ('progress.json', '../progress.json', '%2e%2e/progress.json', 'files?path=/etc/passwd'):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(url + path)
            self.assertEqual(caught.exception.code, 404)
        request = urllib.request.Request(url + 'snapshot', data=b'{}', method='POST')
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        self.assertEqual(caught.exception.code, 501)
        self.assertEqual((self.state / 'progress.json').read_bytes(), before)
        (self.state / 'progress.json').write_text('{broken')
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(url + 'snapshot')
        self.assertEqual(caught.exception.code, 503)
        self.assertIn('Snapshot unavailable', caught.exception.read().decode())

    def test_empty_missing_snapshot_and_corruption_are_explicit_errors(self):
        self.cli('show', ok=False)
        url = self.serve()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(url + 'snapshot')
        self.assertEqual(caught.exception.code, 503)
        self.init()
        self.assertEqual(self.snapshot()['tasks'], {})
        (self.state / 'progress.json').write_text('{broken')
        self.cli('run', {'status': 'active'}, event='repair', ok=False)
        self.assertEqual((self.state / 'progress.json').read_text(), '{broken')

    def test_demo_cannot_overwrite_real_or_existing_demo_and_has_no_orca_dependency(self):
        demo = SCRIPT.with_name('progress-demo.py')
        self.init()
        before = (self.state / 'progress.json').read_bytes()
        result = subprocess.run([sys.executable, str(demo), '--state', str(self.state)],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.state / 'progress.json').read_bytes(), before)
        self.state = self.root / 'demo'
        result = subprocess.run([sys.executable, str(demo), '--state', str(self.state),
                                 '--target-repo', str(self.repo)], capture_output=True, text=True,
                                env={**os.environ, 'PATH': '/nonexistent'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.snapshot()['run']['mode'], 'DEMO')
        self.assertEqual(self.snapshot()['tasks']['retry']['status'], 'rejected')

    def test_protocol_v2_run_and_task_fields_and_undocumented_field_rejection(self):
        self.init()
        state = self.snapshot()
        self.assertEqual(state['schema_version'], 2)
        self.assertIsNone(state['run']['orca_run_id'])
        self.assertEqual(state['run']['health'], 'unknown')
        self.cli('run', {'orca_run_id': 'run-orca-1', 'health': 'degraded'}, event='binding')
        state = self.snapshot()
        self.assertEqual((state['run']['orca_run_id'], state['run']['health']),
                         ('run-orca-1', 'degraded'))
        self.task()
        self.assertFalse(self.snapshot()['tasks']['t1']['provisional'])
        self.assertIsNone(self.snapshot()['tasks']['t1']['orca_task_id'])
        # A provisional progress task is written before the Orca Task exists and is
        # bound after the receipt; before binding it carries no join key.
        self.cli('task', {'id': 'local-1', 'title': 'Dispatch', 'provisional': True},
                 event='provisional')
        task = self.snapshot()['tasks']['local-1']
        self.assertTrue(task['provisional'])
        self.assertIsNone(task['orca_task_id'])
        self.cli('task', {'id': 'local-1', 'provisional': False,
                          'orca_task_id': 'orca-task-7'}, event='bound')
        task = self.snapshot()['tasks']['local-1']
        self.assertFalse(task['provisional'])
        self.assertEqual(task['orca_task_id'], 'orca-task-7')
        before = (self.state / 'progress.json').read_bytes()
        for data in ({'id': 'bad', 'provisional': 'yes'},
                     {'id': 'bad', 'orca_task_id': 'not a portable id'},
                     {'id': 'bad', 'provisional': True, 'orca_task_id': 'orca-task-1'},
                     {'id': 'bad', 'owner': 'nobody'}):
            self.cli('task', data, event='bad-task', ok=False)
        self.cli('run', {'health': 'invented'}, event='bad-health', ok=False)
        self.cli('run', {'orca_run_id': 'not a portable id'}, event='bad-run', ok=False)
        self.cli('event', {'type': 'x', 'summary': 'y', 'raw_message': 'no'}, event='bad-event',
                 ok=False)
        self.assertEqual((self.state / 'progress.json').read_bytes(), before)

    def test_protocol_v2_reads_a_v1_snapshot_and_migrates_on_write(self):
        self.init()
        self.task(status='running')
        self.worker()
        path = self.state / 'progress.json'
        legacy = json.loads(path.read_text())
        legacy['schema_version'] = 1
        legacy['run'].pop('orca_run_id', None)
        legacy['run'].pop('health', None)
        for task in legacy['tasks'].values():
            task.pop('provisional', None)
            task.pop('orca_task_id', None)
        for event in legacy['events']:
            event.pop('bookkeeping', None)
        path.write_text(json.dumps(legacy))
        state = self.snapshot()
        self.assertEqual(state['schema_version'], 1)
        self.assertEqual(state['tasks']['t1']['status'], 'running')
        self.assertEqual(state['workers']['w1']['dispatch_id'], 'd1')
        self.cli('event', {'type': 'note', 'summary': 'v2 write'}, event='migrate')
        migrated = json.loads(path.read_text())
        self.assertEqual(migrated['schema_version'], 2)
        self.assertEqual(migrated['tasks']['t1']['status'], 'running')
        self.assertEqual(migrated['workers']['w1']['attempt'], 1)
        self.assertIn('provisional', migrated['tasks']['t1'])
        self.assertIn('orca_run_id', migrated['run'])

    def test_bookkeeping_writes_do_not_refresh_freshness(self):
        self.init()
        self.task()
        self.worker()
        before = self.snapshot()
        time.sleep(0.02)
        self.cli('event', {'type': 'reconcile', 'summary': 'projection backfill',
                           'task_id': 't1', 'worker_id': 'w1', 'dispatch_id': 'd1',
                           'attempt': 1}, event='bookkeeping-1',
                 global_flags=['--bookkeeping'])
        after = self.snapshot()
        self.assertEqual(after['run']['last_observed'], before['run']['last_observed'])
        self.assertEqual(after['run']['last_effective_progress'],
                         before['run']['last_effective_progress'])
        self.assertEqual(after['workers']['w1']['last_observed'],
                         before['workers']['w1']['last_observed'])
        self.assertTrue(after['events'][-1]['bookkeeping'])
        self.assertEqual(after['events'][-1]['type'], 'reconcile')
        # A backfill of a missing worker row is also bookkeeping, not a new fact.
        self.cli('worker', {'id': 'w1', 'worktree': 'wt-1'}, event='backfill-w1',
                 global_flags=['--bookkeeping'])
        self.assertEqual(self.snapshot()['workers']['w1']['last_observed'],
                         before['workers']['w1']['last_observed'])
        self.assertEqual(self.snapshot()['workers']['w1']['worktree'], 'wt-1')
        time.sleep(0.02)
        self.cli('event', {'type': 'note', 'summary': 'genuine fact', 'task_id': 't1',
                           'worker_id': 'w1', 'dispatch_id': 'd1', 'attempt': 1},
                 event='fact-1')
        fresh = self.snapshot()
        self.assertNotEqual(fresh['run']['last_observed'], before['run']['last_observed'])
        self.assertNotEqual(fresh['workers']['w1']['last_observed'],
                            before['workers']['w1']['last_observed'])
        self.cli('worker', {'id': 'w1', 'lifecycle': 'running'}, event='bk-effective',
                 ok=False, global_flags=['--bookkeeping'], flags=['--effective'])

    def test_json_exit_contract_degrades_on_lock_timeout_and_exits_one_on_input(self):
        self.init()
        before = (self.state / 'progress.json').read_bytes()
        with (self.state / '.progress.lock').open('a') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            started = time.monotonic()
            degraded = self.cli('run', {'phase': 'blocked'}, event='locked-json',
                                global_flags=['--json', '--lock-timeout', '0.2'])
            self.assertLess(time.monotonic() - started, 2)
            payload = json.loads(degraded.stdout)
            self.assertTrue(payload['degraded'])
            self.assertEqual(payload['reason'], 'lock_busy')
            legacy = self.cli('run', {'phase': 'blocked'}, event='locked-legacy', ok=False)
            self.assertIn('lock busy', legacy.stderr)
        self.assertEqual((self.state / 'progress.json').read_bytes(), before)
        rejected = self.cli('run', {'owner': 'nobody'}, event='bad-json', ok=False,
                            global_flags=['--json'])
        self.assertFalse(json.loads(rejected.stdout)['degraded'])
        self.assertIn('documented', json.loads(rejected.stdout)['error'])
        self.assertEqual((self.state / 'progress.json').read_bytes(), before)
        success = self.cli('run', {'phase': 'recovered'}, event='json-ok',
                           global_flags=['--json'])
        self.assertTrue(json.loads(success.stdout)['recorded'])
        (self.state / 'progress.json').write_text('{broken')
        corrupt = self.cli('run', {'phase': 'x'}, event='corrupt-json', ok=False,
                           global_flags=['--json'])
        self.assertIn('error', json.loads(corrupt.stdout))
        self.assertFalse(json.loads(corrupt.stdout)['degraded'])

    def test_turn_start_observed_once_and_unsupported_missing_unknown_unobserved_distinct(self):
        self.init()
        self.task(status='ready')
        self.cli('worker', {'id': 'w1', 'task_id': 't1', 'dispatch_id': 'd1',
                            'attempt': 1}, event='register')
        # A ready task is not a started turn and does not invent a start confirmation.
        self.assertEqual(self.snapshot()['tasks']['t1']['status'], 'ready')
        self.assertEqual(self.snapshot()['workers']['w1']['start_confirmation'], 'unknown')
        # An unsupported receipt field and a missing field both stay unknown.
        self.cli('worker', {'id': 'w1', 'start_confirmation': 'unsupported'},
                 event='unsupported')
        self.assertEqual(self.snapshot()['workers']['w1']['start_confirmation'], 'unknown')
        self.cli('worker', {'id': 'w1', 'start_confirmation': 'unobserved'},
                 event='unobserved')
        self.assertEqual(self.snapshot()['workers']['w1']['start_confirmation'],
                         'unobserved')
        self.assertNotEqual(self.snapshot()['workers']['w1']['start_confirmation'],
                            'unknown')
        # Observed is written once, and a retry of the same event is a no-op.
        self.cli('worker', {'id': 'w1', 'start_confirmation': 'observed',
                            'lifecycle': 'running'}, event='observed', flags=['--effective'])
        observed = self.snapshot()
        self.assertEqual(observed['workers']['w1']['start_confirmation'], 'observed')
        self.assertEqual(observed['tasks']['t1']['status'], 'ready')
        events = len(observed['events'])
        self.cli('worker', {'id': 'w1', 'start_confirmation': 'observed',
                            'lifecycle': 'running'}, event='observed', flags=['--effective'])
        self.assertEqual(len(self.snapshot()['events']), events)
        self.cli('worker', {'id': 'w1', 'start_confirmation': 'observed',
                            'lifecycle': 'failed'}, event='observed',
                 ok=False, flags=['--effective'])
        # The evidence the agent does support is gathered as a normal event.
        self.cli('event', {'type': 'start-evidence',
                           'summary': 'terminal title and worker-read',
                           'task_id': 't1', 'worker_id': 'w1', 'dispatch_id': 'd1',
                           'attempt': 1,
                           'evidence': ['terminal:title', 'worker-read']},
                 event='start-evidence')
        self.assertIn('start-evidence',
                      [event['type'] for event in self.snapshot()['events']])

    def test_both_progress_references_document_protocol_v2_exit_codes_and_dispatch_contract(self):
        neutral = ('schema_version: 2', 'orca_run_id', 'health', 'provisional',
                   'orca_task_id', 'bookkeeping', '--json', '--lock-timeout', 'degraded',
                   'lock_busy', 'last_observed', 'last_effective_progress',
                   'worker-start', '--retry-request', 'preflight', 'intent', 'result',
                   'observation', 'turnStart', 'unobserved', 'ready', 'request_id')
        english = ('Protocol v2', 'Fixed dispatch order', 'Record-failure semantics',
                   'Two hard rules', 'Write the intent before the action',
                   'read authority before deciding')
        chinese = ('协议 v2', '固定派发顺序', '记录失败语义', '两条硬规则',
                   '动作前写 intent', '不确定时先读权威')
        for language, anchors in (('orchestra-auto', english),
                                  ('orchestra-auto-zh', chinese)):
            text = (ROOT / 'skills' / language /
                    'references/progress-dashboard.md').read_text()
            for token in (*neutral, *anchors):
                self.assertIn(token, text, f'{language}: progress-dashboard.md '
                                          f'is missing {token!r}')


if __name__ == '__main__':
    unittest.main()
