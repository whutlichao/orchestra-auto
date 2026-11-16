# Coordination Ledger Internals

Developer and audit specification extracted from runtime skill documentation for `scripts/ledger.py`.

## Storage, Durability, and Concurrency

- Out-of-repo location: `$ORCA_LEDGER_HOME` (default `~/.orca/ledger`), refusing symlinks, repositories, and repository ancestors.
- Per-Run append lock: exclusive `flock` on `runs/<run_id>/ledger.lock` around read-modify-write. External authority/Orca calls never run while holding the lock.
- Crash tolerance & directory fsync: each record is single-line canonical JSON ending with `\n`, file-fsynced, and initial creation fsyncs the parent directory. An interrupted trailing fragment is discarded on read; a corrupt complete line is preserved and reported by line number.
- Record idempotency: `record_id` + normalized JSON equality (`UUID` case and JSON key order ignored). Identical replay is a no-op; conflicting payload raises an error. Archived IDs remain indexed so late replays still deduplicate.

## Retention and Archival

- Defaults: `ORCA_LEDGER_RETENTION_DAYS=30` days or `ORCA_LEDGER_RETENTION_BYTES=10 MiB` per Run.
- Exceeding either limit archives oldest complete records into `archive/ledger-<timestamp>-<n>.jsonl` (fsynced before replacing active `ledger.jsonl`) and moves corrupt/partial fragments to `archive/corrupt-<timestamp>-<n>.jsonl`.

## Authority Snapshot and Reconciliation Report Schema

`reconcile` collects an external authority snapshot where missing/unreachable sources stay `unknown` rather than empty success:

| Field | Schema / Content |
| --- | --- |
| `schema_version` | `1` |
| `run_id`, `observed_at` | Portable Run ID and RFC3339 UTC timestamp |
| `orca.request_show` | Map: `request_id` → `{state: completed \| pending \| absent \| unknown, reason, receipt}` |
| `orca.workers`, `orca.tasks` | `{state, reason, rows}` (`task_id`, `dispatch_id`, `attempt`, `worker_state`, `worktree`) |
| `orca.worker_show` | Map: `dispatch_id` → worker detail |
| `orca.messages` | Optional `{state, reason, rows}` (omitted means unknown) |
| `pr_host` | `{state, reason, pull_requests}` |
| `tracker` | `{state, reason, tickets}` |
| `git` | `{state, reason, ancestry}` |
| `progress` | Optional `{state, reason, orca_run_id}` |

Output report keys: `authority_sources`, `unreadable`, `replays`, `closeout`, `counts`, `repairs`, `decisions`.
Cli flags: `reconcile --run-id <id> [--no-replay] [--refresh-requests] [--human] [--compact]` (`ORCA_CLI_COMMAND` overrides Orca binary).
