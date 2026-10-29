# The coordination ledger

**When to read**: write an intent record before a side-effecting action and a result record
after it; read this file before the first append of a Run, when reconciling a crash window,
or when the coordinator reports open intents and pending items.

The ledger is a durable, append-only **index**, not an authority. Orca, git and the tracker
decide what happened; the ledger points at them. A record that gates a side effect — intent
before an action, a preflight receipt, an acceptance or closeout record — must be durable
before the action runs. A failed append exits non-zero and never reports the record as
written, so never treat an effect as authorized, or a gate as passed, on a failed write.

## Location and permissions

- `$ORCA_LEDGER_HOME` names the ledger home; the default is `~/.orca/ledger` under the
  invoking user's home, never inside a project.
- One file per Run: `runs/<run_id>/ledger.jsonl`, with `archive/` beside it.
- The home, `runs/`, each Run directory and `archive/` are mode `0700`; files are `0600`.
- The ledger carries no credentials, tokens or raw terminal output. `detail` holds short
  structured facts and references; `evidence_ref` points at evidence instead of copying it.
- The path is refused when any component is a symlink, when the home is inside any
  repository, or when it is an ancestor of the target repository. The CLI infers the target
  repository from the working directory; module callers pass it explicitly.
- Environment overrides used by retention are documented below. They change when records
  archive, never what an append means.

## Schema v1

A record has exactly these keys; an unknown field is invalid, and `detail` is the extension
point.

| Field | Meaning |
| --- | --- |
| `schema_version` | `1` |
| `record_id` | UUID; the idempotency key |
| `kind` | `intent`, `result`, `observation`, `decision`, `pending`, `gap`, `health`, `preflight`, `acceptance`, `closeout` |
| `run_id` | portable Run identifier; must equal `append --run-id` |
| `subject` | object with `task_id`, `dispatch_id`, `attempt`, `pr`, `ticket` only; `attempt` is a positive integer and `pr` an integer or short string |
| `action` | `worker-start`, `send`, `reply`, `ack`, `stop`, `abandon`, `retain`, `release`, `merge`, `pr-create`, `tracker-close`, `progress`, `health`, `preflight`, `acceptance`, `closeout` |
| `request_id` | UUID for a keyed action; empty only for `readback_only` and `none` |
| `idempotency` | `joinable`, `replay_safe`, `readback_only`, `lookup_then_create`, `none` |
| `state` | `intent`, `completed`, `pending`, `absent`, `failed`, `refused`, `unknown`, `observed`, `divergent` |
| `evidence_ref` | path or URL; may be empty for an intent |
| `observed_at` | RFC3339 UTC (`Z` or `+00:00`) |
| `actor` | `coordinator`, `worker`, `human`, `rule` |
| `detail` | object; kind-specific fields such as `detail.health_state`, `detail.levels`, `detail.items`, `detail.expected_effect`, `detail.attempt_source`, or `detail.raw_receipt` |

A record is limited to 64 KiB; a larger record is invalid and is not written.

This schema is a **local contract** taken from the orchestration-stability spec. It is not a
claim about any live Orca receipt shape. A receipt field that is not verified stays in
`detail.raw_receipt` and is never asserted as stable.

## CLI

The CLI is product surface and uses JSON in and out.

```bash
LEDGER="$SKILLS_ROOT/orchestra-auto/scripts/ledger.py"
RUN=run_example

# Write the intent before the action; write the result after it.
python3 "$LEDGER" append --run-id "$RUN" --record "$(cat intent.json)"
python3 "$LEDGER" append --run-id "$RUN" --record - < result.json

# Read complete records, newest last, with optional filters.
python3 "$LEDGER" show --run-id "$RUN"
python3 "$LEDGER" show --run-id "$RUN" --kind intent --state pending
python3 "$LEDGER" show --run-id "$RUN" --action send --subject task_id=task-1 --subject ticket=31

# Counts per kind/state plus open intents and pending items.
python3 "$LEDGER" summary --run-id "$RUN"
```

- `append` prints `{"schema_version": 1, "run_id": ..., "record_id": ..., "appended": ...}`.
  `appended` is `false` for an identical re-append and the file is not touched.
- `show` prints a JSON array of records in append order (newest last). It reads the active
  file; an archived record stays readable as plain JSONL under `archive/`.
- `summary` prints an object with `records`, `kinds`, `states`, `open_intents`,
  `pending_items` and `corrupt_lines`. An open intent is an `intent` record with no `result`
  carrying the same non-empty `request_id`; a pending item is any record whose kind or state
  is `pending`.
- Exit `0` success, `1` invalid input / schema / I/O, `2` usage error. A corrupt line is a
  warning on stderr, not a failed read: the complete records are still returned.

## Durability and concurrency

- Appends hold an exclusive `flock` on `runs/<run_id>/ledger.lock` for the complete
  read-modify-write. This, not `O_APPEND` alone, is what keeps a long line from interleaving
  with another process's line.
- Every record is written in one line and the file is fsynced after it. The first creation
  of `ledger.jsonl` also fsyncs its parent directory, so the name itself is durable.
- An interrupted append leaves a trailing fragment without a newline. Readers discard it and
  keep every complete record. The next append terminates the fragment with a newline first,
  so the new record is never glued onto it.
- A complete line that is not a valid schema-v1 record is **preserved and reported**; a read
  never silently skips it. Reports name the line number. A retention rewrite moves such
  lines and a trailing fragment into `archive/corrupt-*.jsonl` instead of deleting them.
- Idempotency is by `record_id` over the normalized record: UUID case and JSON key order do
  not matter, every other value does. The identical record is a no-op; the same id with a
  different payload is a consistency error that writes nothing. Archived ids are still
  checked, so a late replay of an archived record is also a no-op.

## Retention

Defaults are 30 days or 10 MiB per Run, whichever is reached first; both are configurable
defaults, not product requirements. `ORCA_LEDGER_RETENTION_DAYS` and
`ORCA_LEDGER_RETENTION_BYTES` override them for a Run's environment.

When either limit is exceeded, complete records are moved — oldest first — into
`archive/ledger-<timestamp>-<n>.jsonl` under the same Run, and the active file is rewritten
atomically. The archive file is fsynced and its directory fsynced before the active file is
replaced, so no unarchived record is ever deleted. A corrupt line or partial fragment present
during a rewrite is preserved in `archive/corrupt-<timestamp>-<n>.jsonl`.

## Record-failure semantics

| Record | Failure before the action | Failure after the action |
| --- | --- | --- |
| intent | do not run the action; report and escalate | not applicable |
| result / observation | not applicable | retry the record; never replay the action; record a gap if needed |
| acceptance / closeout / preflight | do not release, close or dispatch | retry until durable; the gate stays open |

The rule: **a record that gates a side effect blocks the side effect; a record that describes
an outcome never blocks orchestration and is repaired later.** Never infer durability from a
failed command's absence of an error, and never claim a gate passed while its record is not
durable.

## Module seam for reconciliation

`ledger.py` is importable by path, independent of the CLI. Stable functions for later
workstreams: `validate_record(record)`, `normalize(record)`, `append(record)`,
`read(run_id, filters)`, `read_report(run_id, filters)` (records plus corrupt lines),
`summary(run_id)` and `home_path(raw, target_repo)`. `classify` and `reconcile` are a later
workstream and are deliberately not implemented here; they should consume these functions
rather than re-open the file format.
