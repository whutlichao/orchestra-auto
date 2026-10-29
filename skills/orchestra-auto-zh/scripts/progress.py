#!/usr/bin/env python3
"""Explicit progress recorder and localhost viewer; Python standard library only.

No Orca commands, inbox access, scheduler actions, or file-content service.
Protocol v2 is documented in references/progress-dashboard.md; schema-v1 snapshots
remain readable and the next write migrates them to v2.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import tempfile
import time
import uuid
import webbrowser
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

VERSION = 2
READABLE_VERSIONS = {1, 2}
ASSET = Path(__file__).with_name('progress.html')
BANNER = 'Read-only progress: '
LOCK_TIMEOUT_SECONDS = 2.0

#: The singleton registry protocol. ``protocol_version`` starts at 1; a service that
#: does not answer ``/health`` with at least this version is legacy or incompatible.
PROTOCOL_VERSION = 1
REGISTRY_HOME_ENV = 'ORCA_PROGRESS_HOME'
DEFAULT_REGISTRY_HOME = '~/.orca/progress'
REGISTRY_DIRECTORY_MODE = 0o700
STARTUP_LOCK_TIMEOUT_SECONDS = 10.0
HEALTH_TIMEOUT_SECONDS = 0.5
READINESS_TIMEOUT_SECONDS = 5.0
#: ``open`` is never on the dispatch critical path; this is its whole budget.
OPEN_BUDGET_SECONDS = 15.0
SERVER_NAME = 'server.json'
STARTING_NAME = 'starting.json'
STARTUP_LOCK_NAME = 'startup.lock'
REGISTRY_RUNS_DIR = 'runs'
DESCRIPTOR_FIELDS = {'run_id', 'state_path', 'registered_at', 'label'}
SERVER_FIELDS = {'pid', 'port', 'protocol_version', 'started_at', 'binary_version'}
#: The HTTP surface is read-only and local to this machine. Only these Host names are
#: accepted (an optional ``:port`` suffix must name the serving port); anything else is a
#: DNS-rebinding attempt and is refused before routing.
LOCALHOST_NAMES = frozenset({'localhost', '127.0.0.1'})
#: No external assets, no framing, no form posts. The page is one inline-script document
#: that fetches only same-origin JSON.
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'")
SECURITY_HEADERS = (
    ('Cache-Control', 'no-store'),
    ('X-Content-Type-Options', 'nosniff'),
    ('X-Frame-Options', 'DENY'),
    ('Referrer-Policy', 'no-referrer'),
    ('Content-Security-Policy', CONTENT_SECURITY_POLICY),
)

FIELDS = {
    'init': {'id', 'target_repo', 'mode', 'phase', 'status', 'title', 'orca_run_id',
             'health'},
    'task': {'id', 'title', 'dependencies', 'status', 'blocked_reason', 'pending_decision',
             'provisional', 'orca_task_id'},
    'worker': {'id', 'task_id', 'dispatch_id', 'attempt', 'parent_worker_id', 'worktree',
               'start_confirmation', 'lifecycle', 'reported_lifecycle', 'reported_summary'},
    'event': {'type', 'task_id', 'worker_id', 'dispatch_id', 'attempt', 'summary', 'evidence'},
    'delivery': {'task_id', 'implemented', 'integrated', 'merged', 'tracker_closed', 'evidence'},
    'artifact': {'id', 'task_id', 'kind', 'label', 'reference'},
    'end': {'status', 'phase', 'summary'},
    'run': {'phase', 'status', 'title', 'orca_run_id', 'health'},
}
TASK_STATES = {'unknown', 'pending', 'ready', 'running', 'blocked', 'waiting_user', 'reported',
               'accepted', 'rejected', 'failed', 'paused', 'cancelled'}
RUN_STATES = {'unknown', 'active', 'waiting_user', 'failed', 'paused', 'completed', 'cancelled'}
RUN_HEALTH = {'healthy', 'degraded', 'blocked', 'lost', 'unknown'}
LIFECYCLES = {'unknown', 'dispatched', 'running', 'waiting_user', 'reported_done', 'failed',
              'lost', 'reclaimable', 'closed'}
DELIVERY = ('implemented', 'integrated', 'merged', 'tracker_closed')
PORTABLE_ID = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')


class Degraded(Exception):
    """An intentional, non-blocking degradation; ``--json`` reports it and exits 0."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def timestamp(value):
    return datetime.fromisoformat(value).timestamp()


def validate_data(command, data, source):
    if not isinstance(data, dict) or set(data) - FIELDS[command]:
        raise ValueError('input must be an object containing only documented summary fields')
    if source == 'worker':
        allowed = {'id', 'task_id', 'dispatch_id', 'attempt', 'reported_lifecycle', 'reported_summary'}
        if command not in {'worker', 'event'} or (command == 'worker' and set(data) - allowed):
            raise ValueError('worker reports cannot verify coordinator facts or delivery')
    for key, value in data.items():
        if key in DELIVERY or key == 'provisional':
            if type(value) is not bool:
                raise ValueError(key + ' must be boolean')
        elif key == 'attempt':
            if type(value) is not int or value < 1:
                raise ValueError('attempt must be a positive integer')
        elif key in {'dependencies', 'evidence'}:
            if not isinstance(value, list) or len(value) > 100:
                raise ValueError(key + ' must be a list of at most 100 references')
            if any(not isinstance(item, str) or len(item) > 2048 for item in value):
                raise ValueError('references must be short strings')
        elif key == 'orca_task_id' and value is None:
            pass
        elif not isinstance(value, str) or len(value) > 4096:
            raise ValueError(key + ' must be a short string')
        if value is None:
            continue
        if key == 'id' or key.endswith('_id'):
            if not PORTABLE_ID.fullmatch(value):
                raise ValueError(key + ' must be a portable identifier')
    if 'status' in data:
        choices = TASK_STATES if command == 'task' else RUN_STATES
        if data['status'] not in choices:
            raise ValueError('unknown status')
    if 'health' in data and data['health'] not in RUN_HEALTH:
        raise ValueError('unknown run health')
    if data.get('provisional') is True and data.get('orca_task_id') is not None:
        raise ValueError('a provisional task must not carry an Orca Task ID')
    for field in ('lifecycle', 'reported_lifecycle'):
        if field in data and data[field] not in LIFECYCLES:
            raise ValueError('unknown lifecycle')
    if 'start_confirmation' in data and data['start_confirmation'] not in {
            'unknown', 'observed', 'unobserved', 'unsupported'}:
        raise ValueError('unknown start confirmation')
    if command == 'artifact' and data.get('kind') not in {'report', 'test', 'commit', 'pr', 'other'}:
        raise ValueError('artifact kind required: report/test/commit/pr/other')


def repository_ancestor(path):
    return any((parent / '.git').exists() for parent in (path, *path.parents))


def state_path(raw):
    path = Path(raw).expanduser().absolute()
    # Do not allow a symlink to change the boundary checked here.
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('state path must not traverse symlinks')
    path = path.resolve()
    if repository_ancestor(path):
        raise ValueError('runtime data must be outside every repository')
    return path


def guard_target(directory, target):
    repo = Path(target).expanduser().resolve()
    if not repo.is_dir():
        raise ValueError('target_repo must be an existing directory')
    if directory == repo or repo in directory.parents or directory in repo.parents:
        raise ValueError('state and target repository must be separate directory trees')


@contextmanager
def locked(directory, timeout=LOCK_TIMEOUT_SECONDS):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory / '.progress.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as lock:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Degraded(
                        'lock_busy',
                        'progress lock busy; retry later with the same event-id')
                time.sleep(0.025)
        yield


def read_state(directory):
    path = directory / 'progress.json'
    if path.is_symlink():
        raise ValueError('snapshot must not be a symlink')
    state = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(state, dict) or state.get('schema_version') not in READABLE_VERSIONS:
        raise ValueError('unsupported snapshot schema_version')
    for key in ('run', 'tasks', 'workers', 'deliveries', 'artifacts', 'receipts'):
        if not isinstance(state.get(key), dict):
            raise ValueError('invalid snapshot object: ' + key)
    if not isinstance(state.get('events'), list):
        raise ValueError('invalid snapshot events')
    guard_target(directory, state['run']['target_repo'])
    run = state['run']
    orca_run_id = run.get('orca_run_id')
    if orca_run_id is not None and (
            not isinstance(orca_run_id, str) or not PORTABLE_ID.fullmatch(orca_run_id)):
        raise ValueError('invalid run.orca_run_id')
    if 'health' in run and run['health'] not in RUN_HEALTH:
        raise ValueError('invalid run.health')
    for task in state['tasks'].values():
        if 'provisional' in task and type(task['provisional']) is not bool:
            raise ValueError('invalid task.provisional')
        orca_task_id = task.get('orca_task_id')
        if orca_task_id is not None and (
                not isinstance(orca_task_id, str)
                or not PORTABLE_ID.fullmatch(orca_task_id)):
            raise ValueError('invalid task.orca_task_id')
    for event in state['events']:
        if 'bookkeeping' in event and type(event['bookkeeping']) is not bool:
            raise ValueError('invalid event.bookkeeping')
    return state


def migrate_state(state):
    """Upgrade a v1 snapshot in memory; the next successful write saves it as v2."""
    state['schema_version'] = VERSION
    state['run'].setdefault('orca_run_id', None)
    state['run'].setdefault('health', 'unknown')
    for task in state['tasks'].values():
        task.setdefault('provisional', False)
        task.setdefault('orca_task_id', None)
    return state


def save(directory, state):
    # One atomic writer for the snapshot, server.json and every descriptor: temp file,
    # fsync, rename, directory fsync.
    atomic_write_json(directory / 'progress.json', state, directory,
                      prefix='.progress-')


def required(data, *fields):
    if any(field not in data or data[field] == '' for field in fields):
        raise ValueError('required fields: ' + ', '.join(fields))


# --- singleton registry -----------------------------------------------------------
#
# One dashboard service per user. ``$ORCA_PROGRESS_HOME`` (default ``~/.orca/progress``,
# mode 0700, outside every repository) holds ``server.json``, ``startup.lock`` and one
# ``runs/<run_id>.json`` descriptor per registered Run. The startup lock serializes
# starts; a later caller registers with the winner and never starts a second service.
# The HTTP layer only renders read-only snapshots from these descriptors.

def registry_home(raw=None, environ=None):
    """Resolve ``$ORCA_PROGRESS_HOME`` (or the override); never create it here."""
    source = os.environ if environ is None else environ
    value = raw if raw is not None else source.get(REGISTRY_HOME_ENV, DEFAULT_REGISTRY_HOME)
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise Degraded('registry_home_invalid', 'progress home must not traverse a symlink')
    path = path.resolve()
    if repository_ancestor(path):
        raise Degraded('registry_home_invalid',
                       'the progress home must live outside every repository')
    return path


def ensure_private_directory(path):
    if path.is_symlink():
        raise Degraded('registry_home_invalid', 'progress home must not be a symlink')
    path.mkdir(parents=True, exist_ok=True, mode=REGISTRY_DIRECTORY_MODE)
    os.chmod(path, REGISTRY_DIRECTORY_MODE)


def atomic_write_json(path, payload, directory=None, prefix=None):
    """Write ``payload`` atomically: temp file, fsync, rename, directory fsync."""
    directory = Path(directory or path.parent)
    prefix = prefix or '.' + Path(path).name + '-'
    fd, name = tempfile.mkstemp(prefix=prefix, dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(payload, output, ensure_ascii=False, indent=2)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
        dfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json_object(path, reason):
    """Read one small JSON object; refuse symlinks and non-object payloads."""
    if path.is_symlink():
        raise Degraded(reason, str(path.name) + ' must not be a symlink')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise Degraded(reason, str(path.name) + ' is unreadable: ' + type(error).__name__)
    if not isinstance(data, dict):
        raise Degraded(reason, str(path.name) + ' must be an object')
    return data


def validated_descriptor(raw):
    if not isinstance(raw, dict):
        raise ValueError('a Run descriptor must be an object')
    unknown = sorted(set(raw) - DESCRIPTOR_FIELDS)
    if unknown:
        raise ValueError('unknown descriptor fields: ' + ', '.join(unknown))
    run_id = raw.get('run_id')
    if not isinstance(run_id, str) or not PORTABLE_ID.fullmatch(run_id):
        raise ValueError('run_id must be a portable identifier')
    label = raw.get('label', '')
    if not isinstance(label, str) or len(label) > 4096:
        raise ValueError('label must be a short string')
    descriptor = {'run_id': run_id, 'state_path': str(state_path(raw.get('state_path'))),
                  'label': label}
    registered_at = raw.get('registered_at')
    if registered_at is not None:
        if not isinstance(registered_at, str) or not registered_at:
            raise ValueError('registered_at must be a timestamp string')
        descriptor['registered_at'] = registered_at
    return descriptor


def register(run, home=None):
    """Register (or update) one Run descriptor atomically; return the descriptor."""
    home = registry_home(home)
    ensure_private_directory(home)
    runs = home / REGISTRY_RUNS_DIR
    ensure_private_directory(runs)
    descriptor = validated_descriptor(run)
    path = runs / (descriptor['run_id'] + '.json')
    registered_at = now()
    if path.exists():
        try:
            existing = validated_descriptor(read_json_object(path, 'descriptor_invalid'))
            registered_at = existing.get('registered_at') or registered_at
        except (Degraded, ValueError):
            pass
    descriptor['registered_at'] = registered_at
    stored = {key: descriptor[key] for key in
              ('run_id', 'state_path', 'registered_at', 'label')}
    atomic_write_json(path, stored, runs)
    return stored


def discover(home=None):
    """Every readable Run descriptor, sorted by Run ID; unreadable ones are kept, skipped."""
    home = registry_home(home)
    runs = home / REGISTRY_RUNS_DIR
    if runs.is_symlink() or not runs.is_dir():
        return []
    descriptors = []
    for path in sorted(runs.iterdir()):
        if path.suffix != '.json' or not path.is_file() or path.is_symlink():
            continue
        try:
            descriptors.append(validated_descriptor(read_json_object(path, 'descriptor_invalid')))
        except (Degraded, ValueError):
            continue
    return descriptors


def read_server(home):
    path = home / SERVER_NAME
    if path.is_symlink():
        raise Degraded('server_record_invalid', 'server.json must not be a symlink')
    if not path.exists():
        return None
    server = read_json_object(path, 'server_record_invalid')
    unknown = sorted(set(server) - SERVER_FIELDS)
    if unknown or type(server.get('pid')) is not int or type(server.get('port')) is not int:
        raise Degraded('server_record_invalid', 'server.json is not a server record')
    return server


def probe_port(port, expected_pid=None):
    """One 500 ms ``/health`` probe; returns a status, never kills or deletes anything."""
    url = 'http://127.0.0.1:' + str(port) + '/health'
    try:
        with urlopen(Request(url, headers={'Cache-Control': 'no-store'}),
                     timeout=HEALTH_TIMEOUT_SECONDS) as response:
            body = response.read(65536)
    except HTTPError as error:
        status = 'legacy' if error.code in (400, 404) else 'unresponsive'
        return {'status': status, 'port': port, 'pid': expected_pid, 'health': None}
    except URLError as error:
        refused = isinstance(getattr(error, 'reason', None), ConnectionRefusedError)
        return {'status': 'refused' if refused else 'unresponsive',
                'port': port, 'pid': expected_pid, 'health': None}
    except (TimeoutError, OSError):
        return {'status': 'unresponsive', 'port': port, 'pid': expected_pid, 'health': None}
    try:
        health = json.loads(body)
    except ValueError:
        return {'status': 'legacy', 'port': port, 'pid': expected_pid, 'health': None}
    if not isinstance(health, dict) or health.get('ok') is not True:
        return {'status': 'unresponsive', 'port': port, 'pid': expected_pid, 'health': None}
    version = health.get('protocol_version')
    if type(version) is not int or version < PROTOCOL_VERSION:
        return {'status': 'incompatible', 'port': port, 'pid': expected_pid, 'health': health}
    return {'status': 'healthy', 'port': port, 'pid': health.get('pid', expected_pid),
            'health': health}


def probe(home=None):
    """Probe the registered service: absent, healthy, legacy, incompatible, unresponsive."""
    home = registry_home(home)
    server = read_server(home)
    if server is None:
        return {'status': 'absent', 'port': None, 'pid': None, 'health': None}
    return probe_port(server['port'], server['pid'])


@contextmanager
def startup_lock(directory, timeout=STARTUP_LOCK_TIMEOUT_SECONDS):
    """The 10 s startup flock; holding it means one start path at a time per user."""
    if timeout < 0:
        raise ValueError('startup-timeout must be nonnegative')
    fd = os.open(directory / STARTUP_LOCK_NAME, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as lock:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Degraded(
                        'startup_lock_timeout',
                        'another dashboard start is in progress; using a local viewer')
                time.sleep(0.025)
        yield


def load_ledger(script_dir=None):
    """Import the sibling ``ledger.py`` by path; storage rules live there, not here."""
    directory = Path(script_dir) if script_dir else Path(__file__).resolve().parent
    path = directory / 'ledger.py'
    name = 'orchestra_progress_ledger_' + str(abs(hash(str(path))))
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError('cannot import the ledger module from ' + str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def record_gap(run_id, reason, ledger_home=None):
    """Best-effort ledger gap; a registry or startup failure never blocks dispatch."""
    try:
        if not isinstance(run_id, str) or not PORTABLE_ID.fullmatch(run_id):
            return ''
        record = {
            'schema_version': 1, 'record_id': str(uuid.uuid4()), 'kind': 'gap',
            'run_id': run_id, 'subject': {}, 'action': 'progress', 'request_id': '',
            'idempotency': 'none', 'state': 'observed',
            'evidence_ref': 'progress-dashboard', 'observed_at': now(), 'actor': 'rule',
            'detail': {'repair': 'gap', 'source': 'progress-dashboard', 'reason': reason},
        }
        return load_ledger().append(record, home=ledger_home)
    except Exception:
        return ''


def _alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _unlink(path):
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def read_starting(home):
    """The startup crash-window record; an unparseable one is cleaned, not adopted."""
    path = home / STARTING_NAME
    if not path.exists():
        return None
    try:
        starting = read_json_object(path, 'starting_record_invalid')
    except Degraded:
        _unlink(path)
        return None
    if type(starting.get('pid')) is not int or type(starting.get('port')) is not int:
        _unlink(path)
        return None
    return starting


def _server_payload(pid, port, health=None):
    return {'pid': pid, 'port': port, 'protocol_version': PROTOCOL_VERSION,
            'started_at': (health or {}).get('started_at') or now(),
            'binary_version': str(VERSION)}


def _info(port, pid, run_id, degraded=False, reason=''):
    return {'url': 'http://127.0.0.1:' + str(port) + '/?run=' + run_id,
            'pid': pid, 'degraded': degraded, 'reason': reason, 'browser_opened': False}


def _spawn_viewer(executable, arguments):
    """Start a detached viewer and read its printed address within 5 s."""
    child = subprocess.Popen(
        [sys.executable, str(executable), *arguments],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        start_new_session=True)
    ready, _, _ = select.select([child.stdout], [], [], READINESS_TIMEOUT_SECONDS)
    line = child.stdout.readline().strip() if ready else ''
    child.stdout.close()
    if not line.startswith(BANNER):
        _stop(child)
        return None
    port = urlsplit(line[len(BANNER):]).port
    return child, port


def _stop(child):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
    else:
        child.wait()


def _start_registry_server(home, executable, deadline=None):
    """Start ``serve --registry <home> --port 0`` and wait up to 5 s for ``/health``."""
    spawned = _spawn_viewer(executable, ['--registry', str(home), 'serve', '--port', '0'])
    if spawned is None:
        return None
    child, port = spawned
    # The crash-window record exists before readiness and before server.json is durable.
    atomic_write_json(home / STARTING_NAME, {'pid': child.pid, 'port': port}, home)
    readiness = READINESS_TIMEOUT_SECONDS
    if deadline is not None:
        readiness = max(0.0, min(readiness, deadline - time.monotonic()))
    deadline = time.monotonic() + readiness
    while time.monotonic() < deadline:
        if child.poll() is not None:
            break
        found = probe_port(port, child.pid)
        if found['status'] == 'healthy':
            return child, port, found['health']
        time.sleep(0.05)
    # This child is ours and never became ready; the pre-existing-service rule does not
    # apply, but no second registry server may be left behind.
    _stop(child)
    _unlink(home / STARTING_NAME)
    return None


def _fallback(directory, run_id, reason, executable, opener):
    """Unregistered ``serve --state`` fallback: degraded result, never writes server.json."""
    spawned = _spawn_viewer(executable, ['--state', str(directory), 'serve', '--port', '0'])
    if spawned is None:
        raise Degraded('viewer_unavailable', 'dashboard server failed to start')
    child, port = spawned
    info = _info(port, child.pid, run_id, degraded=True, reason=reason)
    info['browser_opened'] = _open_browser(info['url'], opener)
    return info


def _open_browser(url, opener=None):
    try:
        return bool((opener or webbrowser.open)(url))
    except Exception:
        return False


def _open_registered(run_id, directory, home, executable, startup_timeout, opener,
                     label='', deadline=None):
    try:
        home = registry_home(home)
    except Degraded as error:
        return _fallback(directory, run_id, error.reason, executable, opener)
    try:
        ensure_private_directory(home)
        ensure_private_directory(home / REGISTRY_RUNS_DIR)
    except OSError:
        return _fallback(directory, run_id, 'registry_unavailable', executable, opener)
    if deadline is not None:
        startup_timeout = max(0.0, min(startup_timeout, deadline - time.monotonic()))
    try:
        with startup_lock(home, startup_timeout):
            return _open_locked(run_id, directory, home, executable, opener, label,
                                deadline)
    except Degraded as error:
        # Every registry or startup failure degrades to an unregistered local viewer;
        # none of them may block dispatch, and none may kill a pre-existing service.
        return _fallback(directory, run_id, error.reason, executable, opener)


def _open_locked(run_id, directory, home, executable, opener, label='', deadline=None):
    found = probe(home)
    status = found['status']
    if status == 'healthy':
        register({'run_id': run_id, 'state_path': str(directory), 'label': label}, home=home)
        info = _info(found['port'], found['pid'], run_id)
        info['browser_opened'] = _open_browser(info['url'], opener)
        return info
    if status in {'legacy', 'incompatible'}:
        raise Degraded('service_' + status,
                       'an old dashboard service is running; stop it manually and retry')
    if status == 'unresponsive':
        raise Degraded('service_unresponsive',
                       'the dashboard service did not answer /health; inspect it manually')
    if status == 'refused':
        # Only a refused connection proves the recorded service is gone; never a timeout.
        _unlink(home / SERVER_NAME)
    starting = read_starting(home)
    if starting is not None:
        adopted = probe_port(starting['port'], starting['pid'])
        healthy_child = (adopted['status'] == 'healthy'
                         and adopted['pid'] in (None, starting['pid']))
        if healthy_child:
            atomic_write_json(home / SERVER_NAME,
                              _server_payload(starting['pid'], starting['port'],
                                              adopted['health']), home)
            _unlink(home / STARTING_NAME)
            register({'run_id': run_id, 'state_path': str(directory), 'label': label},
                     home=home)
            info = _info(starting['port'], starting['pid'], run_id)
            info['browser_opened'] = _open_browser(info['url'], opener)
            return info
        if not _alive(starting['pid']):
            _unlink(home / STARTING_NAME)
        else:
            raise Degraded('startup_child_unresponsive',
                           'a previous dashboard start is still running but not healthy')
    started = _start_registry_server(home, executable, deadline)
    if started is None:
        return _fallback(directory, run_id, 'viewer_unavailable', executable, opener)
    child, port, health = started
    atomic_write_json(home / SERVER_NAME, _server_payload(child.pid, port, health), home)
    _unlink(home / STARTING_NAME)
    register({'run_id': run_id, 'state_path': str(directory), 'label': label}, home=home)
    info = _info(port, child.pid, run_id)
    info['browser_opened'] = _open_browser(info['url'], opener)
    return info


def open(run, executable=None, home=None, startup_timeout=STARTUP_LOCK_TIMEOUT_SECONDS,
         opener=None):
    """Open (or adopt) the per-user dashboard for one Run.

    Returns ``{url, pid, degraded, reason, browser_opened}``. Registry and startup
    failures are degradations: they record a ledger gap, fall back to ``serve --state``
    and never block dispatch. ``open`` never calls Orca and has a 15 s budget.
    """
    if not isinstance(run, dict):
        raise ValueError('open needs a Run object')
    run_id = run.get('run_id')
    if not isinstance(run_id, str) or not PORTABLE_ID.fullmatch(run_id):
        raise ValueError('run_id must be a portable identifier')
    directory = state_path(run.get('state_path'))
    mode = run.get('mode')
    if mode == 'DEMO':
        raise Degraded('demo_not_registered',
                       'a DEMO run uses serve --state and never joins the singleton registry')
    executable = executable or Path(__file__).resolve()
    deadline = time.monotonic() + OPEN_BUDGET_SECONDS
    try:
        info = _open_registered(run_id, directory, home, executable, startup_timeout,
                                opener, run.get('label', ''), deadline)
    except Degraded as error:
        record_gap(run_id, error.reason)
        raise
    if info['degraded']:
        record_gap(run_id, info['reason'])
    return info


def mutate(directory, command, data, event_id, source, effective=False,
           bookkeeping=False, timeout=LOCK_TIMEOUT_SECONDS):
    directory = state_path(str(directory))
    validate_data(command, data, source)
    # The fingerprint is the caller's exact input, so an identical retry stays a
    # no-op across the v1 -> v2 migration; `bookkeeping` is added only when set so
    # a v1 receipt without it still matches.
    fingerprint = {'command': command, 'data': data, 'source': source,
                   'effective': effective}
    if bookkeeping:
        fingerprint['bookkeeping'] = True
    data = dict(data)
    if not PORTABLE_ID.fullmatch(event_id):
        raise ValueError('event-id must be a portable identifier')
    if effective and source != 'coordinator':
        raise ValueError('only coordinator verification can mark effective progress')
    if effective and bookkeeping:
        raise ValueError('bookkeeping writes cannot claim effective progress')
    # A receipt that cannot report turnStart (or omitted it) is unknown, never a
    # started turn; a legacy v1 `unsupported` value reads back as unknown too.
    if command == 'worker' and data.get('start_confirmation') == 'unsupported':
        data['start_confirmation'] = 'unknown'
    if command == 'init':
        required(data, 'id', 'target_repo', 'mode')
        if data['mode'] not in {'REAL', 'DEMO'}:
            raise ValueError('mode must be REAL or DEMO')
        guard_target(directory, data['target_repo'])
    with locked(directory, timeout):
        exists = (directory / 'progress.json').exists()
        state = read_state(directory) if exists else None
        if state is not None:
            migrate_state(state)
        if state and event_id in state['receipts']:
            if state['receipts'][event_id] != fingerprint:
                raise ValueError('event-id already used with a different payload')
            return state
        time = now()
        if command == 'init':
            if exists:
                raise ValueError('state already initialized; use a separate directory for each run/demo')
            state = {'schema_version': VERSION, 'run': {
                'phase': 'unknown', 'status': 'unknown', 'orca_run_id': None,
                'health': 'unknown', **data,
                'target_repo': str(Path(data['target_repo']).expanduser().resolve()),
                'observation_mode': 'explicit-coordinator', 'started_at': time,
                'last_effective_progress': time if effective else None},
                'tasks': {}, 'workers': {}, 'deliveries': {}, 'artifacts': {},
                'events': [], 'receipts': {}}
        elif state is None:
            raise ValueError('initialize this run first')
        elif command == 'task':
            required(data, 'id')
            task = state['tasks'].setdefault(data['id'], {
                'id': data['id'], 'title': data['id'], 'dependencies': [], 'status': 'unknown',
                'blocked_reason': '', 'pending_decision': '', 'provisional': False,
                'orca_task_id': None})
            if data['id'] in data.get('dependencies', []):
                raise ValueError('task cannot depend on itself')
            task.update(data)
            task['verified_at'] = time
            state['deliveries'].setdefault(data['id'], {
                'task_id': data['id'], **{key: None for key in DELIVERY}})
        elif command == 'worker':
            required(data, 'id')
            if data['id'] not in state['workers']:
                required(data, 'task_id', 'dispatch_id', 'attempt')
                if source != 'coordinator' or data['task_id'] not in state['tasks']:
                    raise ValueError('coordinator must register worker against an existing task')
                state['workers'][data['id']] = {
                    'id': data['id'], 'lifecycle': 'unknown', 'reported_lifecycle': 'unknown',
                    'start_confirmation': 'unknown', 'last_effective_progress': None,
                    'parent_worker_id': data.get('parent_worker_id')}
            worker = state['workers'][data['id']]
            if source == 'worker':
                required(data, 'task_id', 'dispatch_id', 'attempt')
            for field in ('task_id', 'dispatch_id', 'attempt', 'parent_worker_id'):
                if field in worker and field in data and worker[field] != data[field]:
                    raise ValueError('worker identity is immutable; register a new id for each attempt')
            if data.get('parent_worker_id') == data['id']:
                raise ValueError('worker cannot parent itself')
            if 'parent_worker_id' in data and data['parent_worker_id'] not in state['workers']:
                raise ValueError('parent worker must be registered first')
            worker.update(data)
            if not bookkeeping:
                worker['last_observed'] = time
            worker['reported_at' if source == 'worker' else 'verified_at'] = time
            if effective:
                worker['last_effective_progress'] = time
        elif command == 'delivery':
            required(data, 'task_id')
            if data['task_id'] not in state['tasks']:
                raise ValueError('delivery task must exist')
            item = state['deliveries'].setdefault(data['task_id'], {
                'task_id': data['task_id'], **{key: None for key in DELIVERY}})
            item.update(data)
            item['verified_at'] = time
        elif command == 'artifact':
            required(data, 'id', 'kind', 'label', 'reference')
            if 'task_id' in data and data['task_id'] not in state['tasks']:
                raise ValueError('artifact task must exist')
            state['artifacts'][data['id']] = {**data, 'recorded_at': time}
        elif command == 'event':
            required(data, 'type', 'summary')
            if source == 'worker':
                required(data, 'worker_id', 'task_id', 'dispatch_id', 'attempt')
                reported_worker = state['workers'].get(data['worker_id'], {})
                if any(data[key] != reported_worker.get(key) for key in ('task_id', 'dispatch_id', 'attempt')):
                    raise ValueError('report identity does not match registered worker attempt')
            if 'task_id' in data and data['task_id'] not in state['tasks']:
                raise ValueError('event task must exist')
            if 'worker_id' in data:
                if data['worker_id'] not in state['workers']:
                    raise ValueError('event worker must exist')
                if not bookkeeping:
                    state['workers'][data['worker_id']]['last_observed'] = time
                if effective:
                    state['workers'][data['worker_id']]['last_effective_progress'] = time
        elif command in {'run', 'end'}:
            if command == 'end':
                required(data, 'status')
                if data['status'] not in {'completed', 'failed', 'paused', 'cancelled'}:
                    raise ValueError('end status must be completed/failed/paused/cancelled')
                state['run']['ended_at'] = time
            state['run'].update(data)
        state['run']['updated_at'] = time
        if not bookkeeping:
            state['run']['last_observed'] = time
            if effective:
                state['run']['last_effective_progress'] = time
        event_context = {key: data[key] for key in ('task_id', 'worker_id', 'summary', 'evidence') if key in data}
        if command == 'task':
            event_context['task_id'] = data['id']
        elif command == 'worker':
            event_context.update(worker_id=data['id'], task_id=state['workers'][data['id']]['task_id'])
        event_context.setdefault('summary', command + ' recorded')
        event = {'id': event_id, 'at': time, 'type': command,
                 'source': source, **(data if command == 'event' else event_context),
                 'entity_id': data.get('id', data.get('task_id', state['run']['id']))}
        if bookkeeping:
            event['bookkeeping'] = True
        state['events'].append(event)
        state['receipts'][event_id] = fingerprint
        save(directory, state)
        return state


def view(directory, stale_seconds):
    state = read_state(state_path(str(directory)))
    current = datetime.now(timezone.utc).timestamp()
    for entity in [state['run'], *state['workers'].values()]:
        observed = entity.get('last_observed')
        entity['freshness'] = ('unknown' if not observed else
                               'stale' if current - timestamp(observed) >= stale_seconds else 'current')
    # Idempotency receipts are private recorder bookkeeping, not browser content.
    state.pop('receipts', None)
    return state


def descriptor_run_ids(home):
    """Run IDs named by the descriptor files, including ones that cannot be read.

    A descriptor that fails validation is still kept: it names a Run that exists and is
    merely unavailable, which is not the same answer as "unknown Run".
    """
    runs = home / REGISTRY_RUNS_DIR
    if runs.is_symlink() or not runs.is_dir():
        return []
    names = []
    for path in sorted(runs.iterdir()):
        if path.suffix != '.json' or not path.is_file() or path.is_symlink():
            continue
        if PORTABLE_ID.fullmatch(path.stem):
            names.append(path.stem)
    return names


def resolve_run(home, run_id):
    """Resolve one Run ID to its validated state directory.

    Raises ``LookupError`` for an ID that is not registered here and ``ValueError`` for a
    registered Run whose descriptor is unreadable. It never returns another Run's data,
    never accepts a path, and never deletes anything.
    """
    if not isinstance(run_id, str) or not PORTABLE_ID.fullmatch(run_id):
        raise LookupError('unknown or missing run')
    for descriptor in discover(home):
        if descriptor['run_id'] == run_id:
            return Path(descriptor['state_path'])
    if run_id in descriptor_run_ids(home):
        raise ValueError('registered Run is unavailable')
    raise LookupError('unknown or missing run')


def serve(directory, port, stale_seconds, registry=None):
    """Serve one Run (``serve --state``) or the registered Runs (``serve --registry``).

    The HTTP surface is read-only, IPv4-localhost-only and exposes three GET routes:
    ``/health`` (service plus registered Run IDs), ``/?run=<id>`` (the page, which lists
    every registered Run and renders the selected one) and ``/snapshot?run=<id>`` (that
    Run's read-only snapshot). It renders ``progress.json`` and the local registry
    descriptors only: it never reads or serves arbitrary files or file contents, never
    writes, and never names a state path in a response. Unreadable descriptors are
    reported as unavailable and are never deleted.
    """
    page = ASSET.read_bytes()
    started_at = now()
    binding = {'port': port}

    def health_payload():
        runs = []
        if registry is not None:
            try:
                runs = [descriptor['run_id'] for descriptor in discover(registry)]
            except (OSError, ValueError, Degraded):
                runs = []
        return {'ok': True, 'protocol_version': PROTOCOL_VERSION, 'pid': os.getpid(),
                'started_at': started_at, 'runs': runs}

    class Handler(BaseHTTPRequestHandler):
        def host_allowed(self):
            """Only the two local names may address the service (DNS-rebinding guard)."""
            host = (self.headers.get('Host') or '').strip().lower()
            if not host or host.startswith('['):
                return False
            name, separator, port_text = host.partition(':')
            if name not in LOCALHOST_NAMES:
                return False
            if not separator:
                return True
            return port_text.isdigit() and int(port_text) == binding['port']

        def respond(self, status, mime, body, allow=None):
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            for key, value in SECURITY_HEADERS:
                self.send_header(key, value)
            if allow:
                self.send_header('Allow', allow)
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)

        def do_GET(self):
            if not self.host_allowed():
                self.respond(403, 'text/plain; charset=utf-8', b'Forbidden', 'GET')
                return
            parts = urlsplit(self.path)
            route = parts.path
            if route == '/':
                self.respond(200, 'text/html; charset=utf-8', page)
            elif route == '/health':
                self.respond(200, 'application/json; charset=utf-8',
                             json.dumps(health_payload()).encode())
            elif route == '/snapshot':
                self.snapshot(parts.query)
            else:
                self.respond(404, 'text/plain; charset=utf-8', b'Not found', 'GET')

        def snapshot(self, query):
            mime = 'application/json; charset=utf-8'
            selected = directory
            if registry is not None:
                wanted = (parse_qs(query).get('run') or [''])[0]
                try:
                    selected = resolve_run(registry, wanted)
                except LookupError:
                    self.respond(404, mime, json.dumps({'error': 'Unknown Run'}).encode())
                    return
                except (OSError, ValueError, Degraded):
                    self.respond(503, mime, json.dumps(
                        {'error': 'Snapshot unavailable: this Run cannot be read'}).encode())
                    return
            try:
                body = json.dumps(view(selected, stale_seconds)).encode()
            except (OSError, ValueError, KeyError, TypeError):
                self.respond(503, mime,
                             json.dumps({'error': 'Snapshot unavailable'}).encode())
                return
            self.respond(200, mime, body)

        def reject_method(self):
            if not self.host_allowed():
                self.respond(403, 'text/plain; charset=utf-8', b'Forbidden', 'GET')
                return
            self.respond(405, 'text/plain; charset=utf-8', b'Method not allowed', 'GET')

        do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = reject_method

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    binding['port'] = server.server_port
    print(BANNER + 'http://127.0.0.1:' + str(server.server_port) + '/', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _state_required(value):
    return value is not None and value != ''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', help='external audit directory, separate per run')
    parser.add_argument('--registry', help='the singleton progress home (overrides $ORCA_PROGRESS_HOME)')
    parser.add_argument('--label', default='', help='optional human label for this Run descriptor')
    parser.add_argument('--startup-timeout', type=float, default=STARTUP_LOCK_TIMEOUT_SECONDS,
                        help='seconds to wait for the dashboard startup lock (default 10)')
    parser.add_argument('--json', action='store_true',
                        help='machine mode: degraded results exit 0 with degraded/reason')
    parser.add_argument('--lock-timeout', type=float, default=LOCK_TIMEOUT_SECONDS,
                        help='seconds to wait for the snapshot lock (default 2)')
    parser.add_argument('--bookkeeping', action='store_true',
                        help='reconciliation/backfill write; do not refresh freshness')
    sub = parser.add_subparsers(dest='command', required=True)
    for command in FIELDS:
        item = sub.add_parser(command)
        item.add_argument('--data', required=True, help='JSON object, or - to read JSON from stdin')
        item.add_argument('--event-id', required=True, help='stable idempotency key; reuse only for identical retries')
        item.add_argument('--source', choices=('coordinator', 'worker'), default='coordinator')
        item.add_argument('--effective', action='store_true', help='coordinator observed meaningful progress')
    sub.add_parser('show')
    sub.add_parser('open', help='start or adopt the per-user localhost viewer and open it in the browser')
    viewer = sub.add_parser('serve')
    viewer.add_argument('--registry', dest='serve_registry',
                        help='serve every registered Run from this progress home')
    viewer.add_argument('--port', type=int, default=8765)
    viewer.add_argument('--stale-seconds', type=int, default=300)
    args = parser.parse_args()
    # ``serve --registry DIR`` is the documented spelling; the global flag is equivalent.
    if getattr(args, 'serve_registry', None):
        if args.registry and args.registry != args.serve_registry:
            parser.error('--registry given twice with different values')
        args.registry = args.serve_registry
    try:
        if args.lock_timeout < 0:
            raise ValueError('lock-timeout must be nonnegative')
        if args.startup_timeout < 0:
            raise ValueError('startup-timeout must be nonnegative')
        if args.command == 'serve' and args.registry and args.state:
            raise ValueError('serve takes either --state or --registry, not both')
        if args.command == 'serve' and args.registry:
            if args.stale_seconds < 0:
                raise ValueError('stale-seconds must be nonnegative')
            registry = registry_home(args.registry)
            ensure_private_directory(registry)
            ensure_private_directory(registry / REGISTRY_RUNS_DIR)
            serve(None, args.port, args.stale_seconds, registry)
            return 0
        if not _state_required(args.state):
            raise ValueError('--state is required for this command')
        directory = state_path(args.state)
        if args.command == 'show':
            print(json.dumps(view(directory, 300), ensure_ascii=False))
        elif args.command == 'open':
            state = read_state(directory)
            run = {'run_id': state['run']['id'], 'state_path': str(directory),
                   'mode': state['run'].get('mode')}
            if args.label:
                run['label'] = args.label
            info = open(run, executable=Path(__file__).resolve(), home=args.registry,
                        startup_timeout=args.startup_timeout)
            print(json.dumps(info))
            if not info['browser_opened']:
                print('Progress warning: open the printed URL manually', file=sys.stderr)
        elif args.command == 'serve':
            if args.stale_seconds < 0:
                raise ValueError('stale-seconds must be nonnegative')
            serve(directory, args.port, args.stale_seconds)
        else:
            raw = sys.stdin.read(65537) if args.data == '-' else args.data
            if len(raw) > 65536:
                raise ValueError('input exceeds 64 KiB')
            state = mutate(directory, args.command, json.loads(raw), args.event_id,
                           args.source, args.effective, args.bookkeeping,
                           args.lock_timeout)
            print(json.dumps({'schema_version': VERSION, 'run_id': state['run']['id'],
                              'event_id': args.event_id, 'recorded': True}))
    except Degraded as error:
        print('Progress warning (continue orchestration): ' + str(error),
              file=sys.stderr)
        if args.json:
            print(json.dumps({'degraded': True, 'reason': error.reason,
                              'error': str(error)}))
            return 0
        return 1
    except (OSError, ValueError, KeyError, TypeError) as error:
        print('Progress warning (continue orchestration): ' + str(error), file=sys.stderr)
        if args.json:
            print(json.dumps({'degraded': False, 'error': str(error)}))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
