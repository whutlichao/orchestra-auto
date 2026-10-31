"""Public CLI, snapshot, registry and HTTP contracts for the progress sidecar."""
import fcntl
import time
import concurrent.futures
import http.client
import importlib.util
import json
import os
import socket
import stat
import threading
import unittest.mock
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
import subprocess
import sys
import tempfile
import re
import signal
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'skills/orchestra-auto/scripts/progress.py'


def terminate(pid):
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def wait_dead(pid, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.02)
    return False


def page_script(asset):
    """The single inline script body: the self-healing rules live there, not in the prose."""
    start = asset.index('<script>') + len('<script>')
    return asset[start:asset.index('</script>')]


def prose(language, relative):
    """A shipped document as words on single spaces: prose wraps, its promises do not."""
    return ' '.join((ROOT / 'skills' / language / relative).read_text().split())


def braced_block(script, marker):
    """The balanced ``{...}`` body that follows ``marker``.

    Asserting a token is inside this block proves it belongs to the function under test,
    not merely that the page mentions the word somewhere.
    """
    opening = script.index('{', script.index(marker))
    depth = 0
    for index in range(opening, len(script)):
        if script[index] == '{':
            depth += 1
        elif script[index] == '}':
            depth -= 1
            if depth == 0:
                return script[opening:index + 1]
    raise AssertionError(f'unbalanced block after {marker!r}')


class LegacyDashboardHandler(BaseHTTPRequestHandler):
    """A pre-singleton dashboard: answers GET without a ``/health`` endpoint."""

    def do_GET(self):
        body = b'<html>legacy dashboard</html>'
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class IncompatibleDashboardHandler(BaseHTTPRequestHandler):
    """Speaks ``/health`` but reports a protocol older than the client minimum."""

    health = {'ok': True, 'protocol_version': 0, 'pid': 0,
              'started_at': '2026-01-01T00:00:00+00:00', 'runs': []}

    def do_GET(self):
        body = json.dumps(self.health).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


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
        # The singleton home and the ledger home are per-test temporary trees outside
        # every repository; no test may ever touch the real ~/.orca directories.
        self.home = self.root / 'progress-home'
        self.ledger_home = self.root / 'ledger-home'
        (self.root / 'sitecustomize.py').write_text(
            'import webbrowser\nwebbrowser.open = lambda url: True\n')

    def environment(self, **extra):
        return {**os.environ, 'PYTHONPATH': str(self.root),
                'ORCA_PROGRESS_HOME': str(self.home),
                'ORCA_LEDGER_HOME': str(self.ledger_home), **extra}

    def progress_module(self):
        spec = importlib.util.spec_from_file_location('progress_under_test', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def cli(self, command, data=None, event='e1', source='coordinator', ok=True,
            global_flags=(), flags=(), state=None):
        args = [sys.executable, str(SCRIPT), '--state', str(state or self.state),
                *global_flags, command]
        if command != 'show':
            args += ['--event-id', event, '--data', json.dumps(data or {}),
                     '--source', source, *flags]
        result = subprocess.run(args, text=True, capture_output=True,
                                env=self.environment())
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


    def origin_of(self, info):
        parts = urlsplit(info['url'])
        return parts.scheme + '://' + parts.netloc

    def snapshot_of(self, info, run_id='run1'):
        with urllib.request.urlopen(self.origin_of(info) + '/snapshot?run=' + run_id,
                                    timeout=5) as response:
            return json.load(response)

    def health_of(self, info):
        with urllib.request.urlopen(self.origin_of(info) + '/health', timeout=5) as response:
            return json.load(response)

    def init_run(self, name, run_id, **extra):
        state = self.root / 'audit' / name
        self.cli('init', {'id': run_id, 'target_repo': str(self.repo), 'mode': 'REAL',
                          'phase': 'implementation', 'status': 'active', **extra},
                 event='init', state=state)
        return state

    def make_run(self, name, run_id, status=None, **extra):
        """One complete Run whose terminal status is written through ``end``."""
        state = self.root / 'audit' / name
        data = {'id': run_id, 'target_repo': str(self.repo), 'mode': 'REAL', **extra}
        if status is not None:
            data['status'] = status
        self.cli('init', data, event='init-' + name, state=state)
        if status in {'completed', 'cancelled', 'failed'}:
            self.cli('end', {'status': status}, event='end-' + name, state=state)
        elif status not in (None, 'active'):
            self.cli('run', {'status': status}, event='status-' + name, state=state)
        return state

    def set_run_times(self, state, **values):
        path = state / 'progress.json'
        data = json.loads(path.read_text())
        data['run'].update(values)
        path.write_text(json.dumps(data))

    def register_with_time(self, state, run_id, registered_at, label=''):
        module = self.progress_module()
        module.register({'run_id': run_id, 'state_path': str(state), 'label': label},
                        home=self.home)
        path = self.home / 'runs' / (run_id + '.json')
        descriptor = json.loads(path.read_text())
        descriptor['registered_at'] = registered_at
        path.write_text(json.dumps(descriptor))
        return descriptor

    def prune_cli(self, *flags, home=None):
        return subprocess.run(
            [sys.executable, str(SCRIPT), '--registry', str(home or self.home),
             'registry', 'prune', *flags],
            env=self.environment(), capture_output=True, text=True)

    def open_run(self, state):
        return subprocess.run([sys.executable, str(SCRIPT), '--state', str(state), 'open'],
                              env=self.environment(), capture_output=True, text=True, timeout=25)

    def test_open_starts_viewer_and_reports_browser_failure(self):
        self.init()
        (self.root / 'sitecustomize.py').write_text('import webbrowser\nwebbrowser.open = lambda url: False\n')
        result = subprocess.run([sys.executable, str(SCRIPT), '--state', str(self.state), 'open'],
                                env=self.environment(), capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        info = json.loads(result.stdout)
        self.addCleanup(terminate, info['pid'])
        self.assertFalse(info['browser_opened'])
        self.assertIn('manually', result.stderr)
        self.assertEqual(self.snapshot_of(info)['run']['id'], 'run1')

    def background_viewer(self, script=SCRIPT, state=None):
        """Run ``open`` with a stubbed browser so the test never launches a real window."""
        (self.root / 'sitecustomize.py').write_text(
            'import webbrowser\nwebbrowser.open = lambda url: True\n')
        return subprocess.run([sys.executable, str(script), '--state', str(state or self.state),
                               'open'],
                              env=self.environment(), capture_output=True, text=True, timeout=25)

    def test_open_reports_url_and_a_background_pid_that_outlives_it(self):
        self.init()
        result = self.background_viewer()
        self.assertEqual(result.returncode, 0, result.stderr)
        info = json.loads(result.stdout)
        self.addCleanup(terminate, info['pid'])
        self.assertTrue(info['browser_opened'])
        self.assertEqual(result.stderr, '')
        self.assertTrue(info['url'].startswith('http://127.0.0.1:'))
        self.assertIn('?run=run1', info['url'])
        # The parent has exited, so reading here proves the viewer was detached.
        self.assertEqual(self.snapshot_of(info)['run']['id'], 'run1')

    def test_concurrent_open_yields_one_service_two_runs_and_no_second_server(self):
        # Replaces the per-Run free-port test: the migration makes the dashboard one
        # service per user, so two Runs share a port and register two descriptors.
        self.init()
        second = self.init_run('second', 'run2')
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            opened = list(pool.map(self.open_run, [self.state, second]))
        for result in opened:
            self.assertEqual(result.returncode, 0, result.stderr)
        infos = [json.loads(result.stdout) for result in opened]
        self.addCleanup(terminate, infos[0]['pid'])
        for info in infos:
            self.assertEqual(set(info), {'url', 'pid', 'degraded', 'reason',
                                         'browser_opened'})
            self.assertFalse(info['degraded'])
            self.assertEqual(info['reason'], '')
            self.assertEqual(self.origin_of(info), self.origin_of(infos[0]))
        self.assertEqual(infos[0]['pid'], infos[1]['pid'])
        self.assertEqual(sorted(info['url'] for info in infos),
                         sorted(self.origin_of(infos[0]) + '/?run=' + run_id
                                for run_id in ('run1', 'run2')))
        server = json.loads((self.home / 'server.json').read_text())
        self.assertEqual(server['pid'], infos[0]['pid'])
        self.assertEqual(server['port'], urlsplit(infos[0]['url']).port)
        descriptors = sorted(path.name for path in (self.home / 'runs').iterdir())
        self.assertEqual(descriptors, ['run1.json', 'run2.json'])
        self.assertFalse((self.home / 'starting.json').exists())
        lock = self.home / 'startup.lock'
        self.assertTrue(lock.is_file())
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)
        health = self.health_of(infos[0])
        self.assertTrue(health['ok'])
        self.assertEqual(health['protocol_version'], 1)
        self.assertEqual(health['pid'], infos[0]['pid'])
        self.assertTrue(health['started_at'])
        self.assertEqual(sorted(health['runs']), ['run1', 'run2'])
        # One service, one process: both descriptors point at the same live pid.
        for run_id in ('run1', 'run2'):
            descriptor = json.loads((self.home / 'runs' / (run_id + '.json')).read_text())
            self.assertEqual(descriptor['run_id'], run_id)
            self.assertEqual(descriptor['state_path'],
                             str(self.state if run_id == 'run1' else second))
            self.assertTrue(descriptor['registered_at'])
            self.assertEqual(descriptor['label'], '')
        self.assertEqual(self.snapshot_of(infos[0], 'run1')['run']['id'], 'run1')
        self.assertEqual(self.snapshot_of(infos[0], 'run2')['run']['id'], 'run2')

    def test_open_uses_the_run_title_as_default_label_and_label_flag_overrides_it(self):
        self.cli('init', {'id': 'run1', 'target_repo': str(self.repo), 'mode': 'REAL',
                          'phase': 'implementation', 'status': 'active',
                          'title': '  Dashboard labels from titles  '})
        first = self.background_viewer()
        self.assertEqual(first.returncode, 0, first.stderr)
        info = json.loads(first.stdout)
        self.addCleanup(terminate, info['pid'])
        descriptor = json.loads((self.home / 'runs' / 'run1.json').read_text())
        self.assertEqual(descriptor['label'], 'Dashboard labels from titles')
        again = subprocess.run(
            [sys.executable, str(SCRIPT), '--state', str(self.state),
             '--registry', str(self.home), '--label', 'explicit label', 'open'],
            env=self.environment(), capture_output=True, text=True, timeout=25)
        self.assertEqual(again.returncode, 0, again.stderr)
        descriptor = json.loads((self.home / 'runs' / 'run1.json').read_text())
        self.assertEqual(descriptor['label'], 'explicit label')

    def read_json(self, path):
        return json.loads(path.read_text())

    def registry_files(self):
        if not self.home.exists():
            return []
        return sorted(path.name for path in self.home.iterdir())

    def gaps(self, run_id='run1'):
        path = self.ledger_home / 'runs' / run_id / 'ledger.jsonl'
        if not path.exists():
            return []
        return [record for record in map(json.loads, path.read_text().splitlines())
                if record.get('kind') == 'gap']

    def free_port(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            return sock.getsockname()[1]

    def fake_service(self, handler, pid=None):
        """Serve a fake dashboard on localhost; never bind a non-local address."""
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def cleanup():
            server.shutdown()
            server.server_close()

        self.addCleanup(cleanup)
        if pid is None:
            sleeper = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
            self.addCleanup(sleeper.wait)
            self.addCleanup(terminate, sleeper.pid)
            pid = sleeper.pid
        return server.server_port, pid

    def write_server(self, port, pid, protocol_version=1):
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {'pid': pid, 'port': port, 'protocol_version': protocol_version,
                   'started_at': '2026-01-01T00:00:00+00:00', 'binary_version': 'test'}
        (self.home / 'server.json').write_text(json.dumps(payload))
        return payload

    def test_registry_module_exposes_register_discover_probe_and_private_home(self):
        module = self.progress_module()
        for name in ('register', 'discover', 'probe', 'open'):
            self.assertTrue(callable(getattr(module, name, None)), name)
        descriptor = module.register({'run_id': 'run1', 'state_path': str(self.state),
                                      'label': 'ticket-41'}, home=self.home)
        self.assertEqual(set(descriptor),
                         {'run_id', 'state_path', 'registered_at', 'label'})
        self.assertEqual(module.discover(home=self.home), [descriptor])
        self.assertEqual(module.probe(home=self.home)['status'], 'absent')
        self.assertEqual(stat.S_IMODE(self.home.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.home / 'runs').stat().st_mode), 0o700)

    def test_healthy_compatible_service_is_adopted_and_the_run_registered(self):
        self.init()
        first = self.background_viewer()
        self.assertEqual(first.returncode, 0, first.stderr)
        info = json.loads(first.stdout)
        self.addCleanup(terminate, info['pid'])
        second = self.init_run('second', 'run2')
        adopted = self.open_run(second)
        self.assertEqual(adopted.returncode, 0, adopted.stderr)
        again = json.loads(adopted.stdout)
        self.assertFalse(again['degraded'])
        self.assertEqual(again['reason'], '')
        self.assertEqual(again['pid'], info['pid'])
        self.assertEqual(self.origin_of(again), self.origin_of(info))
        self.assertEqual(sorted(self.health_of(info)['runs']), ['run1', 'run2'])
        self.assertEqual(self.snapshot_of(info, 'run2')['run']['id'], 'run2')

    def test_incompatible_legacy_and_hung_services_are_never_killed_or_deleted(self):
        self.init()
        cases = (('legacy', LegacyDashboardHandler, {}, 'service_legacy'),
                 ('incompatible', IncompatibleDashboardHandler,
                  {'protocol_version': 0}, 'service_incompatible'))
        for name, handler, health_overrides, reason in cases:
            with self.subTest(name):
                port, pid = self.fake_service(handler)
                if health_overrides:
                    handler.health = {**handler.health, **health_overrides,
                                      'pid': pid}
                payload = self.write_server(port, pid)
                before = (self.home / 'server.json').read_bytes()
                result = self.open_run(self.state)
                self.assertEqual(result.returncode, 0, result.stderr)
                info = json.loads(result.stdout)
                self.addCleanup(terminate, info['pid'])
                self.assertTrue(info['degraded'])
                self.assertEqual(info['reason'], reason)
                self.assertEqual((self.home / 'server.json').read_bytes(), before)
                os.kill(pid, 0)  # the pre-existing service was not killed
                self.assertIn(reason, [gap['detail']['reason'] for gap in self.gaps()])
                self.assertEqual(self.snapshot_of(info)['run']['id'], 'run1')
                (self.home / 'server.json').unlink()

    def test_a_hung_service_degrades_quickly_and_is_not_deleted(self):
        self.init()

        class Hung(LegacyDashboardHandler):
            def do_GET(self):
                time.sleep(2)

        port, pid = self.fake_service(Hung)
        payload = self.write_server(port, pid)
        before = (self.home / 'server.json').read_bytes()
        started = time.monotonic()
        result = self.open_run(self.state)
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(result.returncode, 0, result.stderr)
        info = json.loads(result.stdout)
        self.addCleanup(terminate, info['pid'])
        self.assertTrue(info['degraded'])
        self.assertEqual(info['reason'], 'service_unresponsive')
        self.assertEqual((self.home / 'server.json').read_bytes(), before)
        os.kill(pid, 0)
        self.assertIn('service_unresponsive',
                      [gap['detail']['reason'] for gap in self.gaps()])

    def test_connection_refused_stale_server_is_cleaned_and_restarted(self):
        self.init()
        dead = subprocess.Popen([sys.executable, '-c', 'pass'])
        dead.wait()
        self.write_server(self.free_port(), dead.pid)
        result = self.open_run(self.state)
        self.assertEqual(result.returncode, 0, result.stderr)
        info = json.loads(result.stdout)
        self.addCleanup(terminate, info['pid'])
        self.assertFalse(info['degraded'])
        self.assertNotEqual(info['pid'], dead.pid)
        server = self.read_json(self.home / 'server.json')
        self.assertEqual(server['pid'], info['pid'])
        self.assertEqual(server['port'], urlsplit(info['url']).port)
        self.assertTrue(self.health_of(info)['ok'])
        self.assertEqual(self.read_json(self.home / 'runs' / 'run1.json')['run_id'], 'run1')

    def test_crashed_service_restarts_and_reregisters_on_the_next_open(self):
        self.init()
        first = self.background_viewer()
        self.assertEqual(first.returncode, 0, first.stderr)
        info = json.loads(first.stdout)
        terminate(info['pid'])
        self.assertTrue(wait_dead(info['pid']))
        result = self.open_run(self.state)
        self.assertEqual(result.returncode, 0, result.stderr)
        restarted = json.loads(result.stdout)
        self.addCleanup(terminate, restarted['pid'])
        self.assertFalse(restarted['degraded'])
        self.assertNotEqual(restarted['pid'], info['pid'])
        self.assertEqual(self.read_json(self.home / 'server.json')['pid'],
                         restarted['pid'])
        self.assertEqual(self.health_of(restarted)['runs'], ['run1'])
        self.assertEqual(self.snapshot_of(restarted)['run']['id'], 'run1')

    def spawn_registry_child(self):
        port = self.free_port()
        child = subprocess.Popen(
            [sys.executable, str(SCRIPT), 'serve', '--registry', str(self.home),
             '--port', str(port)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.addCleanup(terminate, child.pid)
        self.addCleanup(child.stdout.close)
        line = child.stdout.readline().strip()
        self.assertTrue(line.startswith('Read-only progress:'), line)
        self.assertEqual(urlsplit(line.split()[-1]).port, port)
        return child, port

    def registry_service(self):
        """One real ``serve --registry`` service over the temporary registry home."""
        _child, port = self.spawn_registry_child()
        return 'http://127.0.0.1:' + str(port)

    def http_get(self, url, host=None):
        """One raw HTTP GET so the test observes the status line, headers and body."""
        parts = urlsplit(url)
        connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
        try:
            target = parts.path + ('?' + parts.query if parts.query else '')
            connection.putrequest('GET', target, skip_host=True, skip_accept_encoding=True)
            connection.putheader('Host', parts.netloc if host is None else host)
            connection.endheaders()
            response = connection.getresponse()
            body = response.read()
            return response.status, dict(response.getheaders()), body
        finally:
            connection.close()

    def http_request(self, url, method, body=b''):
        parts = urlsplit(url)
        connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
        try:
            target = parts.path + ('?' + parts.query if parts.query else '')
            connection.request(method, target, body=body)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def snapshot_over_http(self, base, run_id):
        status, _headers, body = self.http_get(base + '/snapshot?run=' + run_id)
        self.assertEqual(status, 200, body)
        return json.loads(body)

    def register_run_descriptor(self, state, run_id):
        module = self.progress_module()
        return module.register({'run_id': run_id, 'state_path': str(state)},
                               home=self.home)

    def test_one_service_serves_two_runs_with_isolated_snapshots(self):
        self.init()
        self.task(status='running')
        self.worker()
        second = self.init_run('second', 'run2')
        self.cli('task', {'id': 'only-in-run2', 'title': 'Second', 'status': 'ready'},
                 event='task-run2', state=second)
        self.register_run_descriptor(self.state, 'run1')
        self.register_run_descriptor(second, 'run2')
        base = self.registry_service()
        first = self.snapshot_over_http(base, 'run1')
        self.assertEqual(first['run']['id'], 'run1')
        self.assertEqual(sorted(first['tasks']), ['t1'])
        isolated = self.snapshot_over_http(base, 'run2')
        self.assertEqual(isolated['run']['id'], 'run2')
        self.assertEqual(sorted(isolated['tasks']), ['only-in-run2'])
        # Neither snapshot can name the other Run's facts or state directory.
        self.assertNotIn('only-in-run2', json.dumps(first))
        self.assertNotIn('"t1"', json.dumps(isolated))
        self.assertNotIn(str(second), json.dumps(first))
        self.assertNotIn(str(self.state), json.dumps(isolated))
        # The page lists every registered Run from /health and selects one by query.
        status, headers, page = self.http_get(base + '/?run=run2')
        self.assertEqual(status, 200)
        self.assertTrue(headers['Content-Type'].startswith('text/html'))
        self.assertIn(b'id="runs"', page)
        # The Run list is read through the same bounded reader as the snapshot.
        self.assertIn(b'startRead("/health")', page)
        self.assertIn(b'encodeURIComponent(runId)', page)
        self.assertIn(b'"/?run="', page)
        health = json.loads(self.http_get(base + '/health')[2])
        self.assertEqual(sorted(health['runs']), ['run1', 'run2'])
        self.assertEqual(self.http_get(base + '/')[0], 200)

    def test_snapshot_answers_only_a_registered_run_and_never_leaks_paths(self):
        self.init()
        second = self.init_run('second', 'run2')
        self.register_run_descriptor(self.state, 'run1')
        self.register_run_descriptor(second, 'run2')
        base = self.registry_service()
        for query in ('run=missing', 'run=', 'run=..', 'run=run1.json', 'run=/etc/passwd',
                      'run=%2e%2e', 'run=..%2F..%2Fetc', ''):
            status, headers, body = self.http_get(base + '/snapshot?' + query)
            self.assertIn(status, (400, 404, 503), query)
            self.assertEqual(headers['Cache-Control'], 'no-store')
            self.assertNotIn(str(self.state).encode(), body)
            self.assertNotIn(str(second).encode(), body)
            self.assertNotIn(b'progress.json', body)
        for run_id in ('run1', 'run2'):
            status, _headers, body = self.http_get(base + '/snapshot?run=' + run_id)
            self.assertEqual(status, 200)
            self.assertNotIn(str(self.state).encode(), body)
            self.assertNotIn(str(second).encode(), body)
            self.assertNotIn(b'state_path', body)
            # The documented snapshot contract is unchanged: no receipts side channel.
            self.assertNotIn(b'receipts', body)

    def test_http_surface_is_get_only_localhost_with_security_headers_and_host_check(self):
        self.init()
        self.register_run_descriptor(self.state, 'run1')
        base = self.registry_service()
        self.assertTrue(base.startswith('http://127.0.0.1:'))
        port = urlsplit(base).port
        for path in ('/', '/health', '/snapshot?run=run1'):
            status, headers, body = self.http_get(base + path)
            self.assertEqual(status, 200, path)
            self.assertIn("default-src 'none'", headers['Content-Security-Policy'])
            self.assertEqual(headers['Cache-Control'], 'no-store')
            self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
            self.assertEqual(headers['X-Frame-Options'], 'DENY')
            self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
            # No external asset can be referenced from the page.
            if path == '/':
                self.assertNotIn(b'https://', body)
                self.assertNotIn(b'src="http', body)
        for method in ('POST', 'PUT', 'DELETE', 'PATCH'):
            status, headers, _body = self.http_request(base + '/snapshot?run=run1', method,
                                                        body=b'{}')
            self.assertEqual(status, 405, method)
            self.assertEqual(headers['Cache-Control'], 'no-store')
        for method in ('POST', 'PUT'):
            self.assertEqual(self.http_request(base + '/', method, body=b'x')[0], 405, method)
        self.assertEqual(self.http_request(base + '/health', 'HEAD')[0], 405)
        for host, expected in (('evil.example.com', 403),
                               ('127.0.0.1.evil.example.com', 403),
                               ('localhost:1', 403),
                               ('[::1]:' + str(port), 403),
                               ('localhost', 200),
                               ('127.0.0.1:' + str(port), 200)):
            status, headers, _body = self.http_get(base + '/health', host=host)
            self.assertEqual(status, expected, host)
            self.assertEqual(headers['Cache-Control'], 'no-store')
            self.assertIn("default-src 'none'", headers['Content-Security-Policy'])
        if socket.has_ipv6:
            try:
                with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as ipv6:
                    ipv6.settimeout(1)
                    listening = ipv6.connect_ex(('::1', port)) == 0
            except OSError:
                listening = False
            self.assertFalse(listening, 'the service must not listen on IPv6 localhost')

    def test_unreadable_descriptor_shows_unavailable_and_is_never_deleted(self):
        self.init()
        second = self.init_run('second', 'run2')
        self.register_run_descriptor(self.state, 'run1')
        self.register_run_descriptor(second, 'run2')
        base = self.registry_service()
        descriptor = self.home / 'runs' / 'run2.json'
        stored = json.loads(descriptor.read_text())
        stored['state_path'] = str(self.root / 'gone')
        descriptor.write_text(json.dumps(stored))
        before_descriptor = descriptor.read_bytes()
        status, _headers, body = self.http_get(base + '/snapshot?run=run2')
        self.assertEqual(status, 503)
        self.assertIn(b'unavailable', body)
        self.assertNotIn(str(self.root).encode(), body)
        self.assertTrue(descriptor.exists())
        self.assertEqual(descriptor.read_bytes(), before_descriptor)
        # The healthy Run is unaffected by the unreadable one.
        self.assertEqual(self.snapshot_over_http(base, 'run1')['run']['id'], 'run1')
        # A descriptor that is not JSON at all is kept and skipped, never repaired.
        broken = self.home / 'runs' / 'run3.json'
        broken.write_text('{broken')
        status, _headers, body = self.http_get(base + '/health')
        self.assertEqual(status, 200)
        self.assertEqual(sorted(json.loads(body)['runs']), ['run1', 'run2'])
        self.assertEqual(broken.read_text(), '{broken')
        # A state file removed after registration is unavailable, not another Run's data.
        (second / 'progress.json').unlink()
        status, _headers, body = self.http_get(base + '/snapshot?run=run2')
        self.assertEqual(status, 503)
        self.assertIn(b'unavailable', body)
        self.assertTrue(descriptor.exists())

    def test_health_reports_readable_run_labels_and_never_leaks_state_paths(self):
        explicit = self.init_run('explicit', 'run1', title='Title under a descriptor label')
        historical = self.init_run('historical', 'run2', title='  Historical title  ')
        long_title = self.init_run('long', 'run3', title='L' * 200)
        broken = self.init_run('broken', 'run4', title='Unreadable state')
        untitled = self.init_run('untitled', 'run5')
        whitespace = self.init_run('whitespace', 'run6', title='Whitespace label fallback')
        module = self.progress_module()
        registrations = (
            ('run1', explicit, 'Descriptor label'),
            ('run2', historical, None),
            ('run3', long_title, None),
            ('run4', broken, None),
            ('run5', untitled, None),
            ('run6', whitespace, '   '),
        )
        for run_id, state, label in registrations:
            descriptor = {'run_id': run_id, 'state_path': str(state)}
            if label is not None:
                descriptor['label'] = label
            module.register(descriptor, home=self.home)
        (broken / 'progress.json').write_text('{broken')
        base = self.registry_service()
        status, _headers, body = self.http_get(base + '/health')
        self.assertEqual(status, 200)
        health = json.loads(body)
        self.assertTrue(health['ok'])
        # The existing contract is unchanged: runs is an ordered array of Run ID strings.
        self.assertEqual(health['runs'], ['run1', 'run2', 'run3', 'run4', 'run5', 'run6'])
        self.assertEqual(health['run_labels'], {
            'run1': 'Descriptor label',
            'run2': 'Historical title',
            'run3': 'L' * 120,
            'run6': 'Whitespace label fallback',
        })
        for path in (explicit, historical, long_title, broken, untitled, whitespace):
            self.assertNotIn(str(path), body.decode())
        self.assertNotIn(b'state_path', body)
        self.assertNotIn(b'progress.json', body)

    def test_health_reports_run_states_from_the_same_state_read_as_labels(self):
        statuses = {
            'run1': ('active', 'Active label'),
            'run2': ('completed', ''),
            'run3': ('waiting_user', ''),
            'run4': ('paused', 'Paused label'),
            'run5': ('failed', ''),
            'run6': ('unknown', ''),
        }
        states = {}
        for run_id, (status, label) in statuses.items():
            state = self.make_run(run_id, run_id, status)
            states[run_id] = state
            descriptor = {'run_id': run_id, 'state_path': str(state)}
            if label:
                descriptor['label'] = label
            self.progress_module().register(descriptor, home=self.home)
        broken_state = self.make_run('run7', 'run7', 'active')
        (broken_state / 'progress.json').write_text('{broken')
        self.progress_module().register(
            {'run_id': 'run7', 'state_path': str(broken_state)}, home=self.home)
        (self.home / 'runs' / 'run8.json').write_text('{broken descriptor')
        base = self.registry_service()
        status, _headers, body = self.http_get(base + '/health')
        self.assertEqual(status, 200)
        health = json.loads(body)
        self.assertEqual(health['protocol_version'], 1)
        self.assertEqual(health['runs'], ['run1', 'run2', 'run3', 'run4', 'run5', 'run6',
                                          'run7'])
        self.assertEqual(health['run_labels'], {
            'run1': 'Active label',
            'run4': 'Paused label',
        })
        self.assertEqual(health['run_states'], {
            'run1': 'active',
            'run2': 'completed',
            'run3': 'waiting_user',
            'run4': 'paused',
            'run5': 'failed',
            'run6': 'unknown',
        })
        for state in states.values():
            self.assertNotIn(str(state), body.decode())
        self.assertNotIn(b'state_path', body)
        self.assertNotIn(b'progress.json', body)

        # The label and the status are produced from one read per descriptor: a
        # descriptor label does not skip the read needed for run_states.
        module = self.progress_module()
        calls = []
        original = module.read_state

        def counting(directory):
            calls.append(str(directory))
            return original(directory)

        module.read_state = counting
        try:
            descriptors = module.discover(home=self.home)
            labels, projections = module.readable_run_views(descriptors)
        finally:
            module.read_state = original
        self.assertEqual(labels, health['run_labels'])
        self.assertEqual(projections, health['run_states'])
        self.assertEqual(len(calls), len(descriptors), calls)
        self.assertEqual(len(set(calls)), len(calls), 'a state was parsed twice')

    def test_registry_prune_only_archives_terminal_runs(self):
        terminal = {
            'run-completed': self.make_run('completed', 'run-completed', 'completed'),
            'run-cancelled': self.make_run('cancelled', 'run-cancelled', 'cancelled'),
            'run-failed': self.make_run('failed', 'run-failed', 'failed'),
        }
        nonterminal = {
            'run-active': self.make_run('active', 'run-active', 'active'),
            'run-waiting': self.make_run('waiting', 'run-waiting', 'waiting_user'),
            'run-paused': self.make_run('paused', 'run-paused', 'paused'),
            'run-unknown': self.make_run('unknown', 'run-unknown'),
        }
        old = '2020-01-01T00:00:00+00:00'
        for run_id, state in {**terminal, **nonterminal}.items():
            self.set_run_times(state, ended_at=old, updated_at=old, last_observed=old)
            self.register_with_time(state, run_id, '2026-01-01T00:00:00+00:00')
        result = self.prune_cli('--keep', '0', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['pruned'], sorted(terminal))
        self.assertEqual(report['archived'], sorted(terminal))
        self.assertEqual(report['kept'], sorted(nonterminal))
        self.assertEqual(report['skipped'], [])
        for run_id, state in terminal.items():
            self.assertFalse((self.home / 'runs' / (run_id + '.json')).exists())
            self.assertFalse(state.exists())
            self.assertTrue((self.home / 'archive' / run_id / state.name / 'progress.json')
                            .exists())
        for run_id, state in nonterminal.items():
            self.assertTrue((self.home / 'runs' / (run_id + '.json')).exists())
            self.assertTrue((state / 'progress.json').exists())

    def test_registry_prune_respects_the_age_boundary(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        cutoff = now.timestamp() - 7 * 86400
        runs = {
            'run-early': cutoff - 1,
            'run-exact': cutoff,
            'run-late': cutoff + 1,
        }
        for run_id, stamp in runs.items():
            state = self.make_run(run_id, run_id, 'completed')
            self.set_run_times(state, ended_at=datetime.fromtimestamp(
                stamp, timezone.utc).isoformat())
            self.register_with_time(state, run_id, '2026-01-01T00:00:00+00:00')
        module = self.progress_module()
        report, failed = module.prune(home=self.home, older_than_days=7, keep=0,
                                      now_timestamp=now.timestamp())
        self.assertFalse(failed)
        self.assertEqual(report['pruned'], ['run-early'])
        self.assertEqual(sorted(report['kept']), ['run-exact', 'run-late'])

    def test_registry_prune_keeps_the_newest_ten_descriptors_regardless_of_status(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        keep_runs = {}
        for index in range(12):
            run_id = 'keep-' + str(index).zfill(2)
            state = self.make_run(run_id, run_id, 'completed')
            self.set_run_times(state, ended_at='2020-01-01T00:00:00+00:00')
            self.register_with_time(
                state, run_id, '2026-01-01T00:00:' + str(index).zfill(2) + '+00:00')
            keep_runs[run_id] = state
        # A non-terminal Run inside the keep set stays kept as well.
        active = self.make_run('keep-active', 'keep-active', 'active')
        self.set_run_times(active, updated_at='2020-01-01T00:00:00+00:00')
        self.register_with_time(active, 'keep-active', '2025-12-31T23:59:59+00:00')
        module = self.progress_module()
        report, failed = module.prune(home=self.home, older_than_days=7, keep=10,
                                      now_timestamp=now.timestamp())
        self.assertFalse(failed)
        self.assertEqual(report['pruned'], ['keep-00', 'keep-01'])
        self.assertEqual(sorted(report['kept']),
                         ['keep-' + str(index).zfill(2) for index in range(2, 12)]
                         + ['keep-active'])
        for run_id in ('keep-00', 'keep-01'):
            self.assertFalse(keep_runs[run_id].exists())
        for run_id in keep_runs:
            if run_id not in ('keep-00', 'keep-01'):
                self.assertTrue(keep_runs[run_id].exists())
        # active was outside the ten newest descriptors but is not terminal.
        self.assertTrue((active / 'progress.json').exists())

    def test_registry_prune_dry_run_changes_nothing(self):
        state = self.make_run('completed', 'run1', 'completed')
        self.set_run_times(state, ended_at='2020-01-01T00:00:00+00:00')
        active = self.make_run('active', 'run2', 'active')
        self.register_with_time(state, 'run1', '2026-01-01T00:00:00+00:00')
        self.register_with_time(active, 'run2', '2026-01-01T00:00:00+00:00')
        before = sorted(str(path.relative_to(self.root))
                        for path in self.home.rglob('*'))
        descriptor_before = (self.home / 'runs' / 'run1.json').read_bytes()
        state_before = (state / 'progress.json').read_bytes()
        result = self.prune_cli('--keep', '0', '--dry-run', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['pruned'], ['run1'])
        self.assertEqual(report['archived'], [])
        self.assertEqual(report['kept'], ['run2'])
        self.assertEqual(sorted(str(path.relative_to(self.root))
                                for path in self.home.rglob('*')), before)
        self.assertFalse((self.home / 'archive').exists())
        self.assertEqual((self.home / 'runs' / 'run1.json').read_bytes(),
                         descriptor_before)
        self.assertEqual((state / 'progress.json').read_bytes(), state_before)

    def test_registry_prune_skips_unreadable_descriptors_and_states(self):
        runs = self.home / 'runs'
        runs.mkdir(parents=True, exist_ok=True, mode=0o700)
        (runs / 'broken.json').write_text('{broken descriptor')
        missing = self.root / 'audit' / 'missing'
        corrupt = self.root / 'audit' / 'corrupt'
        empty = self.root / 'audit' / 'empty'
        corrupt.mkdir(parents=True)
        (corrupt / 'progress.json').write_text('{broken')
        empty.mkdir(parents=True)
        for run_id, state in (('run-missing', missing), ('run-corrupt', corrupt),
                              ('run-empty', empty)):
            (runs / (run_id + '.json')).write_text(json.dumps({
                'run_id': run_id, 'state_path': str(state), 'label': '',
                'registered_at': '2026-01-01T00:00:00+00:00'}))
        good = self.make_run('good', 'run-good', 'completed')
        self.set_run_times(good, ended_at='2020-01-01T00:00:00+00:00')
        self.register_with_time(good, 'run-good', '2026-01-01T00:00:00+00:00')
        result = self.prune_cli('--keep', '0', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['pruned'], ['run-good'])
        self.assertEqual(report['archived'], ['run-good'])
        skipped = {item['run_id']: item['reason'] for item in report['skipped']}
        self.assertEqual(sorted(skipped), ['broken', 'run-corrupt', 'run-empty',
                                           'run-missing'])
        self.assertTrue(all(skipped.values()))
        self.assertEqual((runs / 'broken.json').read_text(), '{broken descriptor')
        for run_id, state in (('run-missing', missing), ('run-corrupt', corrupt),
                              ('run-empty', empty)):
            self.assertTrue((runs / (run_id + '.json')).exists())
            if state.exists():
                self.assertTrue(state.is_dir())
        self.assertTrue((corrupt / 'progress.json').exists())
        self.assertTrue((self.home / 'archive' / 'run-good' / good.name /
                         'progress.json').exists())

    def test_registry_prune_skips_an_existing_archive_directory(self):
        state = self.make_run('completed', 'run1', 'completed')
        self.set_run_times(state, ended_at='2020-01-01T00:00:00+00:00')
        self.register_with_time(state, 'run1', '2026-01-01T00:00:00+00:00')
        archive = self.home / 'archive' / 'run1'
        archive.mkdir(parents=True, mode=0o700)
        marker = archive / 'keep-me'
        marker.write_text('original archive')
        result = self.prune_cli('--keep', '0', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['pruned'], [])
        self.assertEqual(report['archived'], [])
        self.assertEqual([item['reason'] for item in report['skipped']],
                         ['archive_conflict'])
        self.assertEqual(marker.read_text(), 'original archive')
        self.assertTrue((state / 'progress.json').exists())
        self.assertTrue((self.home / 'runs' / 'run1.json').exists())

    def test_registry_prune_archives_the_state_and_the_original_descriptor(self):
        state = self.make_run('completed', 'run1', 'completed')
        self.set_run_times(state, ended_at='2020-01-01T00:00:00+00:00')
        self.register_with_time(state, 'run1', '2026-01-01T00:00:00+00:00')
        descriptor_path = self.home / 'runs' / 'run1.json'
        descriptor_before = descriptor_path.read_bytes()
        result = self.prune_cli('--keep', '0', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['pruned'], ['run1'])
        self.assertEqual(report['archived'], ['run1'])
        archive = self.home / 'archive' / 'run1'
        self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o700)
        self.assertFalse(descriptor_path.exists())
        self.assertFalse(state.exists())
        self.assertTrue((archive / state.name / 'progress.json').exists())
        self.assertEqual((archive / 'descriptor.json').read_bytes(), descriptor_before)
        self.assertEqual(stat.S_IMODE((archive / 'descriptor.json').stat().st_mode),
                         0o600)

    def test_registry_prune_failure_preserves_the_descriptor_and_state(self):
        state = self.make_run('completed', 'run1', 'completed')
        self.set_run_times(state, ended_at='2020-01-01T00:00:00+00:00')
        self.register_with_time(state, 'run1', '2026-01-01T00:00:00+00:00')
        # An archive path that is a file cannot be made into a directory.
        archive_file = self.home / 'archive'
        archive_file.write_text('not a directory')
        result = self.prune_cli('--keep', '0', '--json')
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['pruned'], [])
        self.assertEqual(report['archived'], [])
        self.assertEqual([item['reason'] for item in report['skipped']],
                         ['archive_failed'])
        self.assertEqual(archive_file.read_text(), 'not a directory')
        self.assertTrue((state / 'progress.json').exists())
        self.assertTrue((self.home / 'runs' / 'run1.json').exists())

    def test_registry_prune_rolls_back_when_the_descriptor_write_fails(self):
        state = self.make_run('completed', 'run1', 'completed')
        self.set_run_times(state, ended_at='2020-01-01T00:00:00+00:00')
        self.register_with_time(state, 'run1', '2026-01-01T00:00:00+00:00')
        module = self.progress_module()

        def fail(*_args, **_kwargs):
            raise OSError('disk full')

        with unittest.mock.patch.object(module, 'atomic_write_bytes', fail):
            report, failed = module.prune(home=self.home, older_than_days=7, keep=0,
                                          now_timestamp=datetime(2026, 1, 1,
                                                                 tzinfo=timezone.utc)
                                          .timestamp())
        self.assertTrue(failed)
        self.assertEqual(report['pruned'], [])
        self.assertEqual(report['archived'], [])
        self.assertEqual([item['reason'] for item in report['skipped']],
                         ['archive_failed'])
        self.assertTrue((state / 'progress.json').exists())
        self.assertTrue((self.home / 'runs' / 'run1.json').exists())
        self.assertFalse((self.home / 'archive' / 'run1').exists())

    def test_registry_prune_rejects_negative_and_non_numeric_options(self):
        for flag, value in (('--older-than-days', '-1'), ('--older-than-days', 'seven'),
                            ('--keep', '-1'), ('--keep', 'ten')):
            with self.subTest(flag=flag, value=value):
                result = self.prune_cli(flag, value)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn('nonnegative integer', result.stderr)

    def test_registry_service_refuses_symlinks_repository_ancestors_and_arbitrary_paths(self):
        module = self.progress_module()
        self.init()
        link = self.root / 'link'
        link.symlink_to(self.repo, target_is_directory=True)
        (self.home / 'runs').mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in (link / 'audit', self.repo / 'audit', self.repo):
            with self.assertRaises(ValueError):
                module.validated_descriptor({'run_id': 'bad', 'state_path': str(path)})
            # A descriptor written past the validator is refused when the registry reads it.
            (self.home / 'runs' / 'bad.json').write_text(
                json.dumps({'run_id': 'bad', 'state_path': str(path), 'label': ''}))
            self.assertEqual(module.discover(home=self.home), [])
            (self.home / 'runs' / 'bad.json').unlink()
        # A symlinked state directory swapped in after registration is refused at request time.
        self.register_run_descriptor(self.state, 'run1')
        link2 = self.root / 'swapped'
        link2.symlink_to(self.state, target_is_directory=True)
        (self.home / 'runs' / 'run1.json').write_text(
            json.dumps({'run_id': 'run1', 'state_path': str(link2), 'label': ''}))
        base = self.registry_service()
        status, _headers, body = self.http_get(base + '/snapshot?run=run1')
        self.assertEqual(status, 503)
        self.assertIn(b'unavailable', body)
        for path in ('/etc/passwd', '/progress.json', '/../etc/passwd', '/runs/run1.json',
                     '/snapshot/../health', '/?run=/etc/passwd', '/files?path=/etc/passwd'):
            status, _headers, body = self.http_get(base + path)
            self.assertIn(status, (200, 403, 404), path)
            self.assertNotIn(b'root:', body)
            self.assertNotIn(b'/etc/passwd', body)

    def register_run_descriptor(self, state, run_id):
        module = self.progress_module()
        return module.register({'run_id': run_id, 'state_path': str(state)},
                               home=self.home)

    def test_page_and_asset_ship_the_warning_and_unavailable_notices(self):
        for language in ('orchestra-auto', 'orchestra-auto-zh'):
            asset = (ROOT / 'skills' / language / 'scripts/progress.html').read_text()
            self.assertIn('warning is not a start failure', asset, language)
            self.assertIn('警告不是启动失败', asset, language)
            # An unreadable Run stays listed and marked; it is never dropped or replaced.
            self.assertIn('unavailableRuns', asset, language)
            self.assertIn('· 不可用', asset, language)
            self.assertIn('· unavailable', asset, language)
            self.assertIn('startRead("/health")', asset, language)
        status, _headers, body = self.http_get(self.registry_service() + '/')
        self.assertEqual(status, 200)
        text = body.decode()
        self.assertIn('warning is not a start failure', text)
        self.assertIn('警告不是启动失败', text)

    def test_page_reads_are_bounded_and_a_stuck_read_cannot_stop_the_poll(self):
        """Every read carries a timeout, and no outcome can leave a read marked in flight."""
        for language in ('orchestra-auto', 'orchestra-auto-zh'):
            asset = (ROOT / 'skills' / language / 'scripts/progress.html').read_text()
            script = page_script(asset)
            # A named timeout constant, an AbortController and an aborting timer.
            self.assertIn('const READ_TIMEOUT_MS = 8000;', script, language)
            self.assertIn('new AbortController()', script, language)
            self.assertIn('setTimeout(() => controller.abort(), READ_TIMEOUT_MS)', script,
                          language)
            self.assertIn('clearTimeout(timer)', script, language)
            # The one bounded reader is what both endpoints go through; no bare fetch of
            # the snapshot survives outside it, and the timeout is reported as a timeout.
            self.assertEqual(script.count('fetch('), 1, language)
            reader = braced_block(script, 'function startRead(path)')
            self.assertIn('signal: controller.signal', reader, language)
            message = braced_block(script, 'function readMessage(read, error)')
            self.assertIn('read.controller.signal.aborted', message, language)
            self.assertIn('读取超时', message, language)
            self.assertIn('Read timed out after', message, language)
            snapshot = braced_block(script, 'async function refresh(options)')
            self.assertIn('startRead(', snapshot, language)
            self.assertNotIn('await fetch(', snapshot, language)
            self.assertIn('inFlight.snapshot.abort()', snapshot, language)
            self.assertIn('readMessage(read, error)', snapshot, language)
            # The in-flight read *is* the serial flag, and `finally` clears it on success,
            # failure, timeout and abort alike — so the next poll, and the refresh button,
            # can send a real request instead of returning early.
            finally_block = braced_block(snapshot, 'finally')
            self.assertIn('inFlight.snapshot = null', finally_block, language)
            self.assertIn('generation === readGeneration', finally_block, language)
            self.assertNotIn('busy', script, language)
            # A replaced read cannot write over the newer one.
            self.assertIn('let readGeneration = 0;', script, language)
            self.assertIn('generation !== readGeneration', snapshot, language)
            # /health shares the timeout and stays silent on failure.
            health = braced_block(script, 'async function refreshRuns(options)')
            self.assertIn('startRead("/health")', health, language)
            self.assertNotIn('healthState = {', health, language)
            self.assertIn('if (inFlight.health === read.controller) inFlight.health = null;',
                          health, language)
        status, _headers, body = self.http_get(self.registry_service() + '/')
        self.assertEqual(status, 200)
        served = page_script(body.decode())
        self.assertIn('const READ_TIMEOUT_MS = 8000;', served)
        self.assertIn('new AbortController()', served)

    def test_manual_refresh_and_foreground_return_force_a_real_read(self):
        """The button and the foreground events replace a waiting read instead of queueing."""
        for language in ('orchestra-auto', 'orchestra-auto-zh'):
            asset = (ROOT / 'skills' / language / 'scripts/progress.html').read_text()
            script = page_script(asset)
            click = braced_block(
                script, 'refreshButton.addEventListener("click", () =>')
            self.assertIn('refreshRuns({ force: true });', click, language)
            self.assertIn('refresh({ force: true });', click, language)
            # Without `force` the click would return on the in-flight read — the exact bug
            # the issue reports — so the gate itself is asserted, not just the call site.
            refresh = braced_block(script, 'async function refresh(options)')
            self.assertIn('const force = Boolean(options && options.force);', refresh,
                          language)
            self.assertIn('if (inFlight.snapshot && !force) return;', refresh, language)
            # Visibility, bfcache restore and focus all refresh at once, debounced.
            self.assertIn('document.addEventListener("visibilitychange"', script, language)
            self.assertIn('document.visibilityState === "visible"', script, language)
            self.assertIn('window.addEventListener("pageshow",', script, language)
            self.assertIn('window.addEventListener("focus", refreshForeground)', script,
                          language)
            self.assertIn('let foregroundTimer = null;', script, language)
            self.assertIn('const FOREGROUND_DEBOUNCE_MS = 50;', script, language)
            foreground = braced_block(script, 'function refreshForeground()')
            self.assertIn('if (foregroundTimer) return;', foreground, language)
            self.assertIn('FOREGROUND_DEBOUNCE_MS', foreground, language)
            self.assertIn('refreshRuns({ force: true });', foreground, language)
            self.assertIn('refresh({ force: true });', foreground, language)
            # A Run switch must not let the previous Run's read land on the new selection.
            chip = braced_block(script, 'chip.addEventListener("click", (event) =>')
            self.assertIn('refresh({ force: true });', chip, language)
        status, _headers, body = self.http_get(self.registry_service() + '/')
        self.assertEqual(status, 200)
        served = page_script(body.decode())
        self.assertIn('refresh({ force: true });', served)
        self.assertIn('window.addEventListener("pageshow",', served)

    def test_page_places_runs_in_the_sidebar_and_collapses_ended_runs(self):
        for language in ('orchestra-auto', 'orchestra-auto-zh'):
            asset = (ROOT / 'skills' / language / 'scripts' / 'progress.html').read_text()
            sidebar = asset[asset.index('<aside class="sidebar">'):
                            asset.index('</aside>')]
            self.assertIn('id="runs"', sidebar, language)
            self.assertIn('id="runs-label"', sidebar, language)
            self.assertIn('id="run-toggle"', sidebar, language)
            self.assertIn('aria-controls="run-chips"', sidebar, language)
            self.assertLess(sidebar.index('class="brand"'), sidebar.index('id="runs"'),
                            language)
            self.assertLess(sidebar.index('id="runs"'), sidebar.index('id="nav"'),
                            language)
            # The old top strip is gone: there is exactly one Run list on the page.
            self.assertEqual(asset.count('id="runs"'), 1, language)
            script = page_script(asset)
            self.assertIn('const runsBar = document.getElementById("runs");', script,
                          language)
            self.assertIn('const runToggle = document.getElementById("run-toggle");',
                          script, language)
            self.assertIn('let runStates = {};', script, language)
            self.assertIn('let runsExpanded = false;', script, language)
            self.assertIn('const TERMINAL_RUN_STATES', script, language)
            self.assertIn('health.run_states', script, language)
            self.assertIn('显示全部 (', script, language)
            self.assertIn('Show all (', script, language)
            self.assertIn('收起', script, language)
            self.assertIn('Collapse', script, language)
            render = braced_block(script, 'function renderRuns()')
            self.assertIn('runState(runId)', render, language)
            self.assertIn('Object.hasOwn(runStates, runId)', script, language)
            self.assertIn('TERMINAL_RUN_STATES.has', render, language)
            self.assertIn('selectedRun', render, language)
            self.assertIn('runToggle', render, language)
            self.assertIn('run-dot', render, language)
            self.assertIn('aria-expanded', render, language)
            self.assertIn('history.replaceState', render, language)
            self.assertIn('runStates = {};', braced_block(
                script, 'async function refreshRuns(options)'), language)
            # The sidebar keeps the list usable on narrow screens.
            container = asset[asset.index('.runs {'):asset.index('.run-chip {')]
            self.assertIn('flex-direction: column', container, language)
            chips = asset[asset.index('.run-chips {'):asset.index('.run-chip {')]
            self.assertIn('flex-wrap: wrap', chips, language)
            mobile = asset[asset.index('@media (max-width: 900px)'):]
            self.assertIn('.sidebar {', mobile, language)
        status, _headers, body = self.http_get(self.registry_service() + '/')
        self.assertEqual(status, 200)
        served = page_script(body.decode())
        self.assertIn('let runStates = {};', served)
        self.assertIn('TERMINAL_RUN_STATES', served)
        self.assertIn('id="run-toggle"', body.decode())

    def test_both_bundles_ship_the_same_self_healing_page(self):
        """The two packages ship one page, and that page is the self-healing one.

        Byte parity itself is asserted by ``test_package_content`` (``progress.html`` is in
        ``SCRIPT_FILES``); this test asserts that the shared page carries the self-healing
        rules, in both copies, so a package that silently lost the fix cannot pass.
        """
        self.assertEqual(
            (ROOT / 'skills/orchestra-auto/scripts/progress.html').read_bytes(),
            (ROOT / 'skills/orchestra-auto-zh/scripts/progress.html').read_bytes(),
        )
        for language in ('orchestra-auto', 'orchestra-auto-zh'):
            page = (ROOT / 'skills' / language / 'scripts/progress.html').read_bytes()
            self.assertIn(b'const READ_TIMEOUT_MS = 8000;', page, language)
            self.assertIn(b'new AbortController()', page, language)
            self.assertIn(b'window.addEventListener("pageshow",', page, language)
            self.assertIn(b'window.addEventListener("focus", refreshForeground)', page, language)

    def test_optional_browser_script_targets_snapshots_with_two_runs_and_is_not_a_gate(self):
        script = (ROOT / 'tests/progress_dashboard_browser.cjs').read_text()
        self.assertIn('**/snapshot*', script)
        self.assertIn('**/health', script)
        self.assertIn('searchParams.get("second")', script)
        self.assertIn('SECOND', script)
        self.assertIn('SELECTED', script)
        self.assertIn('NOT the mandatory gate', script)
        self.assertIn('run-chip', script)
        self.assertIn('run-toggle', script)
        self.assertIn('.sidebar #runs', script)
        self.assertIn('run_states', script)
        for language, tokens in (('orchestra-auto',
                                  ('**/snapshot*', 'not the mandatory gate',
                                   'two registered Runs')),
                                 ('orchestra-auto-zh',
                                  ('**/snapshot*', '不是强制门禁', '两个 Run'))):
            text = (ROOT / 'skills' / language /
                    'references/progress-dashboard.md').read_text()
            for token in tokens:
                self.assertIn(token, text, f'{language}: browser check not documented')
        # It is optional: no other test module imports Playwright or runs Node.
        for path in (ROOT / 'tests').glob('*.py'):
            if path.name == 'test_progress_dashboard.py':
                continue
            text = path.read_text()
            self.assertNotIn('playwright', text.lower(), path.name)
            self.assertNotIn('progress_dashboard_browser.cjs', text, path.name)

    def test_startup_crash_window_adopts_a_healthy_child(self):
        self.init()
        child, port = self.spawn_registry_child()
        starting = {'pid': child.pid, 'port': port}
        (self.home / 'starting.json').write_text(json.dumps(starting))
        result = self.open_run(self.state)
        self.assertEqual(result.returncode, 0, result.stderr)
        info = json.loads(result.stdout)
        self.assertFalse(info['degraded'])
        self.assertEqual(info['pid'], child.pid)
        self.assertEqual(self.read_json(self.home / 'server.json')['pid'], child.pid)
        self.assertFalse((self.home / 'starting.json').exists())
        self.assertEqual(self.read_json(self.home / 'runs' / 'run1.json')['run_id'], 'run1')
        self.assertEqual(self.snapshot_of(info)['run']['id'], 'run1')

    def test_startup_crash_window_cleans_only_a_definitely_dead_child(self):
        self.init()
        dead = subprocess.Popen([sys.executable, '-c', 'pass'])
        dead.wait()
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.home / 'starting.json').write_text(
            json.dumps({'pid': dead.pid, 'port': self.free_port()}))
        result = self.open_run(self.state)
        self.assertEqual(result.returncode, 0, result.stderr)
        info = json.loads(result.stdout)
        self.addCleanup(terminate, info['pid'])
        self.assertFalse(info['degraded'])
        self.assertNotEqual(info['pid'], dead.pid)
        self.assertFalse((self.home / 'starting.json').exists())
        self.assertTrue(self.health_of(info)['ok'])

    def test_startup_lock_timeout_degrades_without_a_second_server(self):
        self.init()
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.home / 'runs').mkdir(mode=0o700, exist_ok=True)
        lock_path = self.home / 'startup.lock'
        with lock_path.open('a') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            started = time.monotonic()
            result = subprocess.run(
                [sys.executable, str(SCRIPT), '--state', str(self.state),
                 '--startup-timeout', '0.3', 'open'],
                env=self.environment(), capture_output=True, text=True, timeout=20)
            self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(result.returncode, 0, result.stderr)
        info = json.loads(result.stdout)
        self.addCleanup(terminate, info['pid'])
        self.assertTrue(info['degraded'])
        self.assertEqual(info['reason'], 'startup_lock_timeout')
        self.assertFalse((self.home / 'server.json').exists())
        self.assertEqual(self.snapshot_of(info)['run']['id'], 'run1')
        gaps = self.gaps()
        self.assertIn('startup_lock_timeout', [gap['detail']['reason'] for gap in gaps])
        self.assertTrue(gaps[-1]['detail']['repair'] == 'gap')

    def test_server_and_descriptor_writes_are_atomic_and_crash_safe(self):
        module = self.progress_module()
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self.home / 'server.json'
        synced, replaced = [], []
        real_fsync, real_replace = module.os.fsync, module.os.replace

        def record_fsync(fd):
            synced.append(stat.S_ISDIR(os.fstat(fd).st_mode))
            return real_fsync(fd)

        def record_replace(source, destination):
            replaced.append((Path(source).parent, Path(destination)))
            return real_replace(source, destination)

        with unittest.mock.patch.object(module.os, 'fsync', record_fsync), \
                unittest.mock.patch.object(module.os, 'replace', record_replace):
            module.atomic_write_json(target, {'pid': 1, 'port': 2},
                                     directory=self.home)
        self.assertEqual(sorted(synced), [False, True])
        self.assertEqual(len(replaced), 1)
        self.assertEqual(replaced[0][0], target.parent)
        self.assertEqual(replaced[0][1], target)
        self.assertEqual(self.read_json(target), {'pid': 1, 'port': 2})
        self.assertEqual([path.name for path in self.home.iterdir()], ['server.json'])
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_registry_home_must_be_private_and_outside_every_repository(self):
        self.init()
        inside_repo = self.repo / 'progress-home'
        result = subprocess.run(
            [sys.executable, str(SCRIPT), '--state', str(self.state), 'open'],
            env=self.environment(ORCA_PROGRESS_HOME=str(inside_repo)),
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        info = json.loads(result.stdout)
        self.addCleanup(terminate, info['pid'])
        self.assertTrue(info['degraded'])
        self.assertEqual(info['reason'], 'registry_home_invalid')
        self.assertFalse(inside_repo.exists())
        self.assertIn('registry_home_invalid',
                      [gap['detail']['reason'] for gap in self.gaps()])
        self.assertEqual(self.snapshot_of(info)['run']['id'], 'run1')
        # The default home lives under the per-user directory, never in a project tree.
        module = self.progress_module()
        self.assertEqual(module.registry_home(None, environ={}),
                         Path('~/.orca/progress').expanduser().resolve())

    def test_descriptors_and_server_json_carry_no_credentials(self):
        self.init()
        result = self.background_viewer()
        self.assertEqual(result.returncode, 0, result.stderr)
        info = json.loads(result.stdout)
        self.addCleanup(terminate, info['pid'])
        descriptor = self.read_json(self.home / 'runs' / 'run1.json')
        self.assertEqual(set(descriptor),
                         {'run_id', 'state_path', 'registered_at', 'label'})
        server = self.read_json(self.home / 'server.json')
        self.assertEqual(set(server),
                         {'pid', 'port', 'protocol_version', 'started_at',
                          'binary_version'})
        for value in list(descriptor.values()) + list(server.values()):
            self.assertNotIn('token', str(value).lower())
            self.assertNotIn('secret', str(value).lower())
            self.assertNotIn('authorization', str(value).lower())

    def test_recorder_and_registry_share_one_state_path_validator(self):
        module = self.progress_module()
        link = self.root / 'link'
        link.symlink_to(self.repo, target_is_directory=True)
        bad_paths = (link / 'audit', self.repo / 'audit', self.repo)
        for path in bad_paths:
            with self.assertRaises(ValueError):
                module.state_path(str(path))
            with self.assertRaises(ValueError):
                module.register({'run_id': 'bad', 'state_path': str(path)},
                                home=self.home)
            result = subprocess.run(
                [sys.executable, str(SCRIPT), '--state', str(path), 'init',
                 '--event-id', 'bad', '--data',
                 json.dumps({'id': 'bad', 'target_repo': str(self.repo),
                             'mode': 'REAL'})],
                env=self.environment(), capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / 'runs' / 'bad.json').exists())
        # Linkage: register and the recorder both go through the module's validator.
        calls = []
        original = module.state_path

        def counting(raw):
            calls.append(str(raw))
            return original(raw)

        module.state_path = counting
        try:
            module.register({'run_id': 'run1', 'state_path': str(self.state)},
                            home=self.home)
            module.mutate(self.state, 'init',
                          {'id': 'run1', 'target_repo': str(self.repo), 'mode': 'REAL'},
                          'init', 'coordinator')
        finally:
            module.state_path = original
        self.assertEqual(len(calls), 2, calls)

    def test_open_json_degrades_exit_zero_while_the_legacy_path_exits_one(self):
        self.init()
        incomplete = self.root / 'incomplete-skill' / 'progress.py'
        incomplete.parent.mkdir()
        incomplete.write_bytes(SCRIPT.read_bytes())
        legacy = self.background_viewer(incomplete)
        self.assertEqual(legacy.returncode, 1)
        self.assertEqual(legacy.stdout, '')
        self.assertIn('dashboard server failed to start', legacy.stderr)
        degraded = subprocess.run(
            [sys.executable, str(incomplete), '--state', str(self.state), '--json',
             'open'], env=self.environment(), capture_output=True, text=True, timeout=20)
        self.assertEqual(degraded.returncode, 0, degraded.stderr)
        payload = json.loads(degraded.stdout)
        self.assertTrue(payload['degraded'])
        self.assertEqual(payload['reason'], 'viewer_unavailable')
        self.assertFalse((self.home / 'server.json').exists())

    def test_serve_state_and_demo_never_register_in_the_singleton(self):
        self.init()
        url = self.serve()
        self.assertEqual(json.load(urllib.request.urlopen(url + 'snapshot'))['run']['id'],
                         'run1')
        self.assertFalse((self.home / 'server.json').exists())
        self.assertFalse((self.home / 'runs').exists())
        demo = self.root / 'demo'
        result = subprocess.run(
            [sys.executable, str(SCRIPT.with_name('progress-demo.py')), '--state',
             str(demo), '--target-repo', str(self.repo)],
            env=self.environment(), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        opened = subprocess.run([sys.executable, str(SCRIPT), '--state', str(demo),
                                 '--json', 'open'],
                                env=self.environment(), capture_output=True, text=True,
                                timeout=20)
        self.assertEqual(opened.returncode, 0, opened.stderr)
        demo_result = json.loads(opened.stdout)
        self.assertTrue(demo_result['degraded'])
        self.assertEqual(demo_result['reason'], 'demo_not_registered')
        self.assertFalse((self.home / 'server.json').exists())
        self.assertFalse((self.home / 'runs').exists())

    def test_open_never_calls_orca_and_stays_inside_its_budget(self):
        self.init()
        forbidden = self.root / 'orca'
        trace = self.root / 'orca.trace'
        forbidden.write_text('#!/bin/sh\nprintf called >> "$ORCA_TRACE"\nexit 91\n')
        forbidden.chmod(0o700)
        started = time.monotonic()
        result = subprocess.run(
            [sys.executable, str(SCRIPT), '--state', str(self.state), 'open'],
            env=self.environment(ORCA_CLI_COMMAND=str(forbidden),
                                 ORCA_TRACE=str(trace), PATH=str(self.root) + os.pathsep +
                                 os.environ['PATH']),
            capture_output=True, text=True, timeout=20)
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(result.returncode, 0, result.stderr)
        info = json.loads(result.stdout)
        self.addCleanup(terminate, info['pid'])
        self.assertFalse(info['degraded'])
        self.assertFalse(trace.exists(), 'open and the registry never call Orca')

    def test_second_open_for_a_registered_run_updates_its_descriptor(self):
        self.init()
        first = self.background_viewer()
        self.assertEqual(first.returncode, 0, first.stderr)
        info = json.loads(first.stdout)
        self.addCleanup(terminate, info['pid'])
        before = json.loads((self.home / 'runs' / 'run1.json').read_text())
        again = subprocess.run(
            [sys.executable, str(SCRIPT), '--state', str(self.state),
             '--registry', str(self.home), '--label', 'renamed', 'open'],
            env=self.environment(), capture_output=True, text=True, timeout=25)
        self.assertEqual(again.returncode, 0, again.stderr)
        after = json.loads((self.home / 'runs' / 'run1.json').read_text())
        self.assertEqual(sorted(path.name for path in (self.home / 'runs').iterdir()),
                         ['run1.json'])
        self.assertEqual(after['label'], 'renamed')
        self.assertEqual(after['registered_at'], before['registered_at'])
        self.assertEqual(json.loads(again.stdout)['pid'], info['pid'])

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
        # Every non-GET is refused with the same explicit read-only answer.
        self.assertEqual(caught.exception.code, 405)
        self.assertEqual(caught.exception.headers['Allow'], 'GET')
        self.assertEqual(caught.exception.headers['Cache-Control'], 'no-store')
        self.assertEqual(caught.exception.headers['X-Content-Type-Options'], 'nosniff')
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

    def test_both_references_document_the_http_surface_isolation_and_security_identically(self):
        neutral = ('/health', '/?run=<id>', '/snapshot?run=<id>', 'Host', 'localhost',
                   '127.0.0.1', 'Content-Security-Policy', 'Cache-Control: no-store',
                   'X-Content-Type-Options: nosniff', 'X-Frame-Options: DENY',
                   '405', '403', '404', '503', 'unavailable', 'Allow: GET',
                   'DNS')
        verified = ('one service serves two Runs', 'run isolation', 'security boundary')
        english = ('**HTTP surface and Run isolation.**', '**Security boundary.**',
                   'never their state paths', 'never deleted', 'restartable')
        chinese = ('**HTTP 面与 Run 隔离。**', '**安全边界。**', '绝不出现状态路径',
                   '绝不删除', '可重启')
        for language, extra in (('orchestra-auto', english),
                                ('orchestra-auto-zh', chinese)):
            text = (ROOT / 'skills' / language /
                    'references/progress-dashboard.md').read_text()
            for token in (*neutral, *extra):
                self.assertIn(token, text, f'{language}: HTTP surface docs miss {token!r}')
            self.assertNotIn('http://127.0.0.1:8766/?run=', text)
        # Both bundles describe it identically: same routes, same headers, same refusals.
        for token in (*neutral, 'Cache-Control', 'Referrer-Policy'):
            for language in ('orchestra-auto', 'orchestra-auto-zh'):
                self.assertIn(token, (ROOT / 'skills' / language /
                                      'references/progress-dashboard.md').read_text())
        self.assertTrue(verified[0])

    def test_both_progress_references_document_the_singleton_registry_identically(self):
        anchors = ('$ORCA_PROGRESS_HOME', 'server.json', 'startup.lock', 'starting.json',
                   'runs/', 'archive/', 'descriptor.json', 'protocol_version',
                   'run_states', 'register(run)', 'discover()', 'probe()',
                   'registry prune', '--older-than-days', '--keep', '--dry-run',
                   'serve --state', 'legacy', '10', '500', 'degraded', 'gap',
                   '0720-not-used')
        anchors = tuple(anchor for anchor in anchors if not anchor.endswith('not-used'))
        english = ('/health', 'startup crash window', 'directory fsync', '5 s')
        chinese = ('启动崩溃窗口', '目录 fsync', '5 秒', '启动锁')
        for language, extra in (('orchestra-auto', english),
                                ('orchestra-auto-zh', chinese)):
            text = (ROOT / 'skills' / language /
                    'references/progress-dashboard.md').read_text()
            for token in (*anchors, *extra):
                self.assertIn(token, text, f'{language}: progress-dashboard.md '
                                           f'is missing the registry anchor {token!r}')

    def test_both_progress_references_document_explicit_prune_and_sidebar_runs(self):
        common = ('run_states', 'archive/', 'registry prune', '--older-than-days',
                  '--keep', '--dry-run', 'descriptor.json', '"pruned"', '"archived"',
                  '"kept"', '"skipped"', 'completed', 'cancelled', 'failed')
        english = ('7 days', '10 newest', 'left sidebar', 'Show all', 'Collapse',
                   'never runs it automatically', 'same filesystem',
                   'copies and verifies', 'exit 1')
        chinese = ('7 天', '最近 10', '侧边栏', '显示全部', '收起',
                   '绝不自动调用', '同一文件系统', '复制并校验', '退出码 1')
        for language, extra in (('orchestra-auto', english),
                                ('orchestra-auto-zh', chinese)):
            text = (ROOT / 'skills' / language /
                    'references/progress-dashboard.md').read_text()
            for token in (*common, *extra):
                self.assertIn(token, text, f'{language}: prune docs miss {token!r}')

    def test_design_doc_documents_run_states_and_the_only_explicit_prune_entry(self):
        text = (ROOT / 'docs/design/orchestration-stability.md').read_text()
        for token in ('run_states', 'registry prune', 'archive/', '--dry-run',
                      '不自动删除', '终态', '显式'):
            self.assertIn(token, text, f'design doc misses {token!r}')

    def test_readmes_document_run_states_and_explicit_prune_retention(self):
        claims = (
            ('README.md', ('run_states', 'registry prune', 'never deleted by the service',
                           'archives ended Runs')),
            ('README.zh-CN.md', ('run_states', 'registry prune',
                                 '服务与 `open` 绝不删除', '归档已结束 Run')),
        )
        for relative, tokens in claims:
            text = (ROOT / relative).read_text()
            for token in tokens:
                self.assertIn(token, text, f'{relative}: retention docs miss {token!r}')

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

    def test_both_references_document_the_self_healing_page(self):
        """Both languages promise the same reads, timeouts, retries and foreground refresh."""
        english = prose('orchestra-auto', 'references/progress-dashboard.md')
        chinese = prose('orchestra-auto-zh', 'references/progress-dashboard.md')
        # Each promise is anchored on wording that only the new paragraph carries; "every
        # three seconds" alone predates this change and would prove nothing.
        for token in ('8-second timeout', 'keeps the last', 'last valid view',
                      'The in-page refresh button re-reads',
                      'returning the tab to the foreground'):
            self.assertIn(token, english, f'English reference misses {token!r}')
        for token in ('8 秒超时', '最后有效视图', '页面内「刷新」按钮立即重读', '回到前台'):
            self.assertIn(token, chinese, f'Chinese reference misses {token!r}')


if __name__ == '__main__':
    unittest.main()
