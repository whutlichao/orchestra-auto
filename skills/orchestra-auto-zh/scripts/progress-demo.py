#!/usr/bin/env python3
"""Create a reproducible DEMO in a new external directory; never overwrite a run."""
import argparse
import json
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True)
    parser.add_argument('--target-repo', default=str(Path.cwd()))
    args = parser.parse_args()
    script = Path(__file__).with_name('progress.py')
    records = [
        ('init', {'id': 'demo-run', 'title': 'DEMO · 产品交付流程观察',
                  'target_repo': args.target_repo, 'mode': 'DEMO',
                  'phase': 'acceptance', 'status': 'active'}),
        ('task', {'id': 'core', 'title': '核心协议实施', 'status': 'accepted'}),
        ('task', {'id': 'view', 'title': '进展看板设计', 'dependencies': ['core'],
                  'status': 'running'}),
        ('task', {'id': 'docs', 'title': '使用文档与发布说明', 'dependencies': ['view', 'release-policy'],
                  'status': 'blocked', 'blocked_reason': '等待看板验收及发布策略确认'}),
        ('task', {'id': 'decision', 'title': '发布时机决策', 'status': 'waiting_user',
                  'pending_decision': 'DEMO：请选择发布时间'}),
        ('task', {'id': 'retry', 'title': '验收打回 · 补齐证据', 'status': 'rejected',
                  'blocked_reason': 'DEMO：请补齐恢复场景证据'}),
        ('worker', {'id': 'top', 'task_id': 'view', 'dispatch_id': 'demo-dispatch',
                    'attempt': 1, 'worktree': 'demo-view', 'start_confirmation': 'observed',
                    'lifecycle': 'running', 'reported_lifecycle': 'reported_done',
                    'reported_summary': 'DEMO：worker 自报完成，尚未由 coordinator 验收'}),
        ('worker', {'id': 'nested', 'task_id': 'view', 'dispatch_id': 'demo-nested',
                    'attempt': 1, 'parent_worker_id': 'top', 'worktree': 'demo-nested',
                    'start_confirmation': 'unobserved', 'lifecycle': 'lost'}),
        ('worker', {'id': 'review-helper', 'task_id': 'view', 'dispatch_id': 'demo-helper',
                    'attempt': 1, 'parent_worker_id': 'nested', 'worktree': 'demo-helper',
                    'start_confirmation': 'unknown', 'lifecycle': 'unknown'}),
        ('delivery', {'task_id': 'core', 'implemented': True, 'integrated': True,
                      'merged': False, 'tracker_closed': False}),
        ('artifact', {'id': 'test-report', 'task_id': 'core', 'kind': 'test',
                      'label': 'DEMO test reference (illustrative)', 'reference': 'demo-tests.txt'}),
        ('artifact', {'id': 'sample-pr', 'task_id': 'core', 'kind': 'pr',
                      'label': 'DEMO PR reference (illustrative)',
                      'reference': 'https://example.com/demo/pull/1'}),
        ('event', {'type': 'review_rejected', 'task_id': 'retry',
                   'summary': 'DEMO review failed; coordinator requested corrections',
                   'evidence': ['demo-review.txt']}),
    ]
    # A second invocation cannot replay init and then overwrite subsequent REAL data.
    state = Path(args.state).expanduser()
    if state.exists():
        parser.error('demo requires a new state directory; never reuse REAL or existing DEMO data')
    for index, (command, data) in enumerate(records):
        result = subprocess.run([sys.executable, str(script), '--state', args.state, command,
                                 '--event-id', 'demo-' + str(index), '--data', json.dumps(data)])
        if result.returncode:
            return result.returncode
    print('DEMO ready. Serve this directory with progress.py --state STATE serve')
    return 0


if __name__ == '__main__':
    sys.exit(main())
