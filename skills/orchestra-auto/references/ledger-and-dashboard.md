# Out-of-Repo Ledger & Progress Dashboard Operations

**When to read**: whenever recording intents/results/decisions (`ledger.py`), inspecting or reconciling a Run after crash/compaction (`ledger.py status` / `reconcile`), or initializing/updating the local progress viewer (`progress.py`) **before the first dispatch** of every actual Run.

## 1. Out-of-Repo Ledger (`ledger.py`)

Never write ledger files inside any git repository (`$ORCA_LEDGER_HOME`, default `~/.orca/ledger`). Use one-shot `status --compact` for fast resume context, `append` for durability, and `reconcile` for external authority alignment (uses cached terminal `raw_receipt` unless `--refresh-requests` is passed):

```bash
LEDGER="$SKILLS_ROOT/orchestra-auto/scripts/ledger.py"
python3 "$LEDGER" status --run-id "$RUN" --compact
python3 "$LEDGER" append --run-id "$RUN" --record - < intent.json
python3 "$LEDGER" reconcile --run-id "$RUN" [--no-replay] [--refresh-requests] [--human] --compact
```

(`ORCA_CLI_COMMAND` selects the Orca executable cleanly. For storage locking, fsync, 30-day/10 MiB archival, and full snapshot schemas, see [ledger internals](../../../docs/design/ledger-internals.md).)

**Two hard rules:**
1. **Write the intent before the action** (`record_id`, `request_id`, `action`, `run_id`, `subject`, `expected_effect`, timestamp, actor).
2. **When uncertain, read authority before deciding** — a `request-show` reply of `absent` never proves the action did not run; inspect workers, git, PR host, and tracker first.

**Fixed dispatch order:** (1) durable `preflight` receipt (fails block dispatch) → (2) durable ledger `intent` (fails block action) → (3) execute `worker-start --retry-request <request_id>` → (4) ledger `result` (write failures preserve raw receipt in evidence dir, log gap, never replay action; `attempt_source: local` if receipt lacks attempt) → (5) best-effort `progress.py` (gap on failure) → (6) post-dispatch observations.

**Crash windows (W1/W2/W3) & Record-failure semantics:** A record that gates a side effect (`preflight`, `intent`, `acceptance`, `closeout`) blocks that side effect on write failure; outcome/projection records (`result`, `progress`, `health`) record a gap and never halt orchestration.
- **W1** (before action: intent durable, no receipt/effect) → `orphan_intent`.
- **W2** (after action: receipt or visible authority effect) → `matched`, `divergent`, `orphan_effect`.
- **W3** (unreadable receipt/authority) → `unknown`.

## 2. Action Matrix & Non-Replay Decisions

| Action | Idempotency | Key before the action | Read-back | When uncertain |
| :--- | :--- | :--- | :--- | :--- |
| dispatch / `worker-start` | `joinable` | `request_id` | `state`/`stage`/`turnStart`/`dispatchId`; `worker-list` | `request-show`: `completed` adopts outcome; `pending` replays same key; `absent` inventories Orca + worktrees first |
| `send`, `reply` | `joinable` | `request_id` | Message ID & thread state | `request-show`; missing reply = `send_reply_no_resend` |
| `ack` (historical) | legacy | `request_id` | Historical receipt | Never auto-replay unverified semantics |
| `stop`, `abandon` | `joinable` | `request_id` | `worker-show`/`worker-list` terminal state | Running worker = `stop_abandon_no_retry` |
| `retain`, `release` | `replay_safe` post-settle | `request_id` | Receipt + `worker-show` | Unsettled = `unsettled_release`; follow `release_unknown` receipt |
| `merge` | `readback_only` | none | PR host `MERGED`, merge SHA, ancestry | Read only; never auto-replay (`merge_replay`) |
| `pr-create` | `lookup_then_create` | head branch | PR number & head SHA | Look up by head branch; adopt if found |
| `tracker-close` | `readback_only` | none | Tracker read-back | Read only (`tracker_close_replay`) |
| `progress` | `replay_safe` | `event_id` | Snapshot / receipt | Retry same `event_id`; gap on failure |
| `preflight`, `acceptance`, `closeout` | `none` | `record_id` | Ledger record | Retry until durable; gate/release blocks until durable |
| `health` | `replay_safe` | `record_id` | Ledger record | Retry same `record_id`; never blocks dispatch |

Never auto-replay `merge_replay`, `tracker_close_replay`, `unproven_retry` / `unknown_outcome`, or `unsettled_release`.

## 3. Progress Dashboard Operations (`progress.py`)

Sidecar read-only dashboard (**never** blocks dispatch, polls Orca, or serves arbitrary files). Set `AUDIT_ROOT` to a persistent directory **outside every repository** (never symlink/repo/ancestor). Before the first dispatch of every REAL Run, initialize and run `open` (starts singleton localhost service + attempts browser open; if unable to open browser, tell the user the URL to visit manually; stop daemon with `kill PID`). Isolated demos use `progress-demo.py` + `serve --state <dir> --port 8766` (explicit `serve` **does not open a browser**). Full HTTP routes, CSP/DNS-rebinding headers, singleton `server.json`/`startup.lock`, explicit `registry prune`, and Playwright checks live in [dashboard architecture](../../../docs/design/dashboard-architecture.md).

**Protocol v2 & Checkpoints:** snapshots carry `schema_version: 2`, `run.orca_run_id`, `run.health`, `task.provisional`, `task.orca_task_id`, `last_observed`, `last_effective_progress`, `pending_decision`, and `--bookkeeping` (for reconciliation backfills that must not advance timestamps). Use `--json` + `--lock-timeout` (exit `0` on `degraded` / `lock_busy` so orchestration never stalls).

Safe wrapper (continues cleanly even under `set -e` and records all checkpoints — `task_id`, `dispatch_id`, `attempt`, `parent_worker_id`, `G-impl`, `G-merge`, `implemented`, `integrated`, `merged`, `tracker_closed`, `turnStart`, `unobserved`, `ready`, `request_id`, `preflight`, `intent`, `result`, `observation`):

```bash
progress_record() {
  local output status
  status=0
  output=$(python3 "$PROGRESS" --state "$STATE" --json "$@" 2>&1) || status=$?
  if [ "$status" -ne 0 ]; then
    printf '%s\n' 'Progress warning; continue orchestration; record a ledger gap' >&2
  elif printf '%s' "$output" | grep -q '"degraded": true'; then
    printf '%s\n' 'Progress degraded; continue orchestration; record a ledger gap' >&2
  fi
  return 0
}
progress_record task --event-id task-1 --data '{"id":"t1","title":"Protocol","dependencies":[],"status":"ready"}'
progress_record worker --event-id dispatch-1 --data '{"id":"w1-a1","task_id":"t1","dispatch_id":"d1","attempt":1,"worktree":"protocol-worktree","lifecycle":"dispatched","start_confirmation":"unobserved"}'
progress_record worker --event-id start-1 --effective --data '{"id":"w1-a1","lifecycle":"running","start_confirmation":"observed"}'
progress_record worker --event-id report-1 --source worker --data '{"id":"w1-a1","task_id":"t1","dispatch_id":"d1","attempt":1,"reported_lifecycle":"reported_done","reported_summary":"Implementation ready for review"}'
progress_record delivery --event-id accept-1 --effective --data '{"task_id":"t1","implemented":true,"evidence":["acceptance-report.txt"]}'
progress_record end --event-id end-1 --data '{"status":"paused","phase":"waiting","summary":"Awaiting integration"}'
```
