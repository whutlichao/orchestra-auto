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
import json
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import tempfile
import time
import webbrowser
from urllib.parse import urlsplit

VERSION = 2
READABLE_VERSIONS = {1, 2}
ASSET = Path(__file__).with_name('progress.html')
BANNER = 'Read-only progress: '
LOCK_TIMEOUT_SECONDS = 2.0
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
    fd, name = tempfile.mkstemp(prefix='.progress-', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(state, output, ensure_ascii=False, indent=2)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, directory / 'progress.json')
        dfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def required(data, *fields):
    if any(field not in data or data[field] == '' for field in fields):
        raise ValueError('required fields: ' + ', '.join(fields))


def mutate(directory, command, data, event_id, source, effective=False,
           bookkeeping=False, timeout=LOCK_TIMEOUT_SECONDS):
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
    state = read_state(directory)
    current = datetime.now(timezone.utc).timestamp()
    for entity in [state['run'], *state['workers'].values()]:
        observed = entity.get('last_observed')
        entity['freshness'] = ('unknown' if not observed else
                               'stale' if current - timestamp(observed) >= stale_seconds else 'current')
    # Idempotency receipts are private recorder bookkeeping, not browser content.
    state.pop('receipts', None)
    return state


def serve(directory, port, stale_seconds):
    page = ASSET.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            route = urlsplit(self.path).path
            status, mime = 200, 'text/html; charset=utf-8'
            if route == '/':
                body = page
            elif route == '/snapshot':
                mime = 'application/json; charset=utf-8'
                try:
                    body = json.dumps(view(directory, stale_seconds)).encode()
                except (OSError, ValueError, KeyError, TypeError) as error:
                    status = 503
                    body = json.dumps({'error': 'Snapshot unavailable: ' + type(error).__name__}).encode()
            else:
                status, mime, body = 404, 'text/plain; charset=utf-8', b'Not found'
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    print(BANNER + 'http://127.0.0.1:' + str(server.server_port) + '/', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True, help='external audit directory, separate per run')
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
    sub.add_parser('open', help='start a localhost viewer and open it in the browser')
    viewer = sub.add_parser('serve')
    viewer.add_argument('--port', type=int, default=8765)
    viewer.add_argument('--stale-seconds', type=int, default=300)
    args = parser.parse_args()
    try:
        if args.lock_timeout < 0:
            raise ValueError('lock-timeout must be nonnegative')
        directory = state_path(args.state)
        if args.command == 'show':
            print(json.dumps(view(directory, 300), ensure_ascii=False))
        elif args.command == 'open':
            read_state(directory)
            child = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), '--state', str(directory),
                 'serve', '--port', '0'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, start_new_session=True)
            ready, _, _ = select.select([child.stdout], [], [], 5)
            line = child.stdout.readline().strip() if ready else ''
            child.stdout.close()
            if not line.startswith(BANNER):
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                raise Degraded('viewer_unavailable', 'dashboard server failed to start')
            url = line[len(BANNER):]
            try:
                opened = webbrowser.open(url)
            except Exception:
                opened = False
            print(json.dumps({'url': url, 'pid': child.pid, 'browser_opened': opened}))
            if not opened:
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
