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

| Record | Failure before the action | Failure after the action | Effect on orchestration |
| --- | --- | --- | --- |
| ledger intent | do not run the action; report and stop or escalate | not applicable | blocks the action |
| ledger result | not applicable | retry; preserve the raw receipt in the evidence directory; record a gap; never replay the action | no block; reconcile later |
| ledger acceptance / closeout | do not release or declare the gate passed | retry; release waits until the record is durable | blocks release and gate close |
| progress write | not applicable | record a gap; retry the same `event_id` at the next checkpoint | no block |
| ack receipt | not applicable | do not assume the ack landed; retry the ack with the same `request_id`; keep the delivery file | no block; status shows unacked |
| health record | not applicable | keep the in-memory evaluation and retry; the dashboard projection is not authority | no block |
| preflight receipt | do not dispatch | not applicable | blocks dispatch |
| registry descriptor / `server.json` | fall back to `serve --state`; record a gap | dashboard unavailable; record a gap | no block |
| authority read (Orca, PR host, tracker, git) | do not infer an effect | retry the read; if still unreadable, record `unknown`; escalate if a destructive action depends on it | escalate if a destructive action depends on it |

The rule: **a record that gates a side effect blocks the side effect; a record that describes
an outcome never blocks orchestration and is repaired later.** Never infer durability from a
failed command's absence of an error, and never claim a gate passed while its record is not
durable.

## Crash windows and classification

An intent record is durable before its action runs. A crash leaves the Run in one of three
windows, and every classification carries it:

| Window | Where the crash left the Run | Classifications |
| --- | --- | --- |
| W1 | before the action: intent durable, no receipt, no authority effect | `orphan_intent` |
| W2 | after the action: a receipt or a visible authority effect exists | `matched`, `divergent`, `orphan_effect` |
| W3 | undecidable: a receipt or an authority source is unreadable | `unknown` |

The five classifications are `matched` (record and authority agree), `orphan_intent` (an
intent with no provable effect), `orphan_effect` (an authority effect no record claims),
`divergent` (record and authority disagree) and `unknown` (authority could not be read).
Every outcome names its `evidence`: a source, a reference and that source's `ok` or
`unknown` state.

A `request-show` receipt is `completed`, `pending`, `absent` or `unknown`. **`absent` is
not proof that the action did not happen** — it only says the request key is unknown to the
authority, so the effect probe still decides. An Orca Task ID is the only join key: a
provisional progress task (`orca_task_id: null`) is never a join key, so a projection is
only written once the authority supplies a non-empty Task ID or a dispatch identity.

## Fixed dispatch order

Every dispatch follows one order and never skips a durable predecessor:

1. write the preflight receipt (a failed preflight blocks the dispatch);
2. write the ledger intent (a failed intent write blocks the call);
3. execute `worker-start --retry-request <request_id>`;
4. write the result with the raw receipt summary and an evidence reference (a failed result write
   preserves the raw receipt, records a gap and never replays the action);
5. write progress best-effort; a failure records a gap and dispatch continues, and the next
   checkpoint retries with the same `event_id`;
6. run the post-receipt checks (`turnStart`, isolation, inbox) and record an observation. A failed
   post-receipt check is evidence, not a reason to dispatch again.

A lost `worker-start` response is reconciled from authority, never re-dispatched. A missing intent
prevents a second dispatch; `absent` is not proof that nothing ran. The same request-id discipline
applies to send, reply, ack, stop, abandon, retain and release.

## Action matrix

`Idempotency` is the pre-action key class; `Read-back` is the authority that proves the effect;
`When uncertain` is what a pending or unreadable receipt permits.

| Action | Idempotency | Key before the action | Read-back after | When uncertain |
| --- | --- | --- | --- | --- |
| dispatch / `worker-start` | `joinable` | `request_id` | receipt `state`/`stage`/`turnStart`/`dispatchId`; worker list by task/spec/worktree | `request-show`; `completed` → adopt the recorded outcome; `pending` → replay with the same key; `absent` → inventory Orca and git worktrees first, never dispatch again |
| `send`, `reply` | `joinable` | `request_id` | message id/state and thread visibility | `request-show`; never auto-resend merely because no reply arrived |
| `ack` | `replay_safe` | `request_id` | ack receipt | replay with the same key |
| `stop`, `abandon` | `joinable` | `request_id` | `worker-show`/`worker-list` terminal state | `request-show` plus inventory; never retry without proof |
| `retain`, `release` | `replay_safe` after settlement | `request_id` | receipt plus `worker-show` | `release_unknown` follows the receipt; never release an unsettled worker |
| `merge` | `readback_only` | none | PR host `MERGED`, merge SHA, ancestry | read only; never auto-replay |
| `pr-create` | `lookup_then_create` | head branch | PR number and head SHA | look up by head; adopt if found |
| `tracker-close` | `readback_only` | none | tracker read-back | read only; the close follows repository and human rules |
| `progress` | `replay_safe` | `event_id` | snapshot/receipt | same `event_id`; a failure records a gap and never blocks dispatch |
| `preflight` | write-ahead (`none`) | `record_id` | ledger receipt | rerun the checks; never dispatch on an unreadable receipt |
| `health` | `replay_safe` | `record_id` | ledger receipt | same `record_id` |
| `acceptance`, `closeout` | `none` | `record_id` | ledger record | retry until durable; never release or close the gate early |

Four outcomes are never replayed automatically, each a `decision`: a merge
(`merge_replay`), a tracker close (`tracker_close_replay`), a retry after an unproven
failure (`unproven_retry`, and `unknown_outcome` when the recorded outcome is unknown), and
a release or retain of a worker whose settlement is not proven (`unsettled_release`). A
missing reply is `send_reply_no_resend`: no reply is not proof of failure. A stop or
abandon with a running worker is `stop_abandon_no_retry`.

## Two hard rules

1. **Write the intent before the action.** The intent contains at least `request_id`, action,
   `run_id`, task/dispatch/attempt when known, target, expected effect, timestamp and actor.
2. **When uncertain, read authority before deciding.** `request-show` `absent` is not "did not
   happen"; only positive absence in authority (no Dispatch for that Task, no PR for that head)
   permits the "not executed" judgment.

## Progress projection

The ledger is the dispatch record; `progress.json` is a best-effort projection and never the only
record of a dispatch. A progress write is `replay_safe` by `event_id`; its failure records a `gap`
with action `progress` and never blocks dispatch. The next checkpoint retries with the same
`event_id` and backfills a missing worker row from the ledger (dispatch id, attempt, worktree).

Reconciliation and backfill writes carry `bookkeeping: true` and do not refresh `last_observed` or
`last_effective_progress`; only a genuinely new fact refreshes them. The progress Run carries
`orca_run_id`; a mismatch with the ledger Run is `divergent`. A provisional progress task
(`provisional: true`, `orca_task_id: null`) is never a join key; after the receipt the Orca Task ID
is the only join key. `turnStart: observed` is written as `observed` in one step; `unsupported` or a
missing field is `unknown`, and `unobserved` (checked, not observed) stays distinct from `unknown`.
`state: ready` is not a started turn. Worker self-report is enabled only when the spec explicitly
carries PROGRESS/STATE paths; `source=worker` writes are never acceptance evidence.

Attempt mapping: a dispatch result uses the Orca receipt's `attempt` when present. When the receipt
has no verified attempt, record `detail.attempt_source: local` and exclude the local count from
authoritative reconciliation. Convert receipt camelCase fields to snake_case on write and preserve
the raw receipt in `detail.raw_receipt`; a receipt field the design has not verified is never
asserted as a stable contract.

## Authority snapshot

`reconcile` reads the current state into one snapshot. Every source is either `ok` with
rows, or `unknown` with a reason — a missing adapter is never an empty success.

| Field | Content |
| --- | --- |
| `schema_version` | `1` |
| `run_id`, `observed_at` | portable Run id and RFC3339 UTC read time |
| `orca.request_show` | map: request id → `{state: completed/pending/absent/unknown, reason, receipt}` |
| `orca.workers`, `orca.tasks` | `{state, reason, rows}`; worker rows carry `task_id`, `dispatch_id`, `attempt`, `worker_state`, `worktree` |
| `orca.worker_show` | map: dispatch id → the worker row |
| `orca.messages` | optional `{state, reason, rows}`; absent means the message authority is unknown |
| `pr_host` | `{state, reason, pull_requests}` with `pr`, `head`, `merged` |
| `tracker` | `{state, reason, tickets}` with `ticket`, `state` |
| `git` | `{state, reason, ancestry}` with `commit`, `merged_into` |

Unknown top-level or per-source fields are rejected, so a typo cannot silently drop
authority. `LiveAuthority` builds the snapshot: Orca read commands resolve through
`ORCA_CLI_COMMAND`; `worker-list` is called with `--include-remote` and both it and
`task-list` follow `page.hasMore` / `page.nextCursor`. A missing or repeated cursor, a
non-zero exit or a non-JSON reply is `unknown` with the reason, never an empty result.

## Reconciliation report

`classify(records, snapshot)` is pure and returns the report `reconcile` acts on:

| Field | Meaning |
| --- | --- |
| `schema_version`, `run_id`, `observed_at` | report identity |
| `classifications` | one outcome per intent and per unclaimed effect: `classification`, `window`, `action`, `request_id`, `subject`, `evidence`, `repairs`, `decision`, `conflicts` |
| `repairs` | the allowed repairs gathered from every outcome |
| `decisions` | the items a coordinator must decide; never auto-executed |
| `unreadable` | sources that could not be read, with reasons |
| `counts` | one count per classification |
| `authority_sources` | `orca`, `pr_host`, `tracker`, `git` → `ok` or `unknown` |
| `replays` | added by `reconcile`: every same-key replay it ran and its outcome |
| `closeout` | added by `reconcile`: the closeout `record_id` and the report path |

`reconcile` writes the machine report to
`runs/<run_id>/reports/reconcile-<digest>.json` (mode `0600`, atomic) and appends at most
these records: a `result` for a late receipt, an `observation` for a late effect, a
`progress` observation for a projection update, an `observation` marking a divergence, a
`gap` for an unreadable source, a `pending` record for a pending decision, and one
`closeout`. Generated ids are deterministic (`uuid5` over the Run and the source digest),
so a repeated reconciliation over the same authority appends nothing, replays nothing and
returns the stored report. The digest excludes `observed_at` and the reconcile-generated
records; `detail.repair` names the repair on every generated record.

```bash
# JSON report on stdout; --human prints the readable rendering.
python3 "$LEDGER" reconcile --run-id "$RUN"
python3 "$LEDGER" reconcile --run-id "$RUN" --authority recorded.json --human
# Never repeat a pending joinable action; record a decision instead.
python3 "$LEDGER" reconcile --run-id "$RUN" --no-replay
```

`--authority FILE` supplies only the `pr_host`, `tracker` and `git` sections; Orca is always
read through the CLI. A replay is the one sanctioned mutation: it repeats the command
recorded on the intent with the same `--retry-request` id. Reconciliation never merges,
closes a ticket, or stops, releases or retries a worker.

## Reconciliation concurrency

The ledger is read once as a consistent prefix **without** its lock, so appends from other
processes continue. No Orca or host adapter is called while the per-Run lock is held; every
authority call and replay returns first, and each append then takes the lock briefly. This
is deliberate: it avoids a lock ordering between the ledger lock and Orca, the PR host or
the tracker. A record appended after the read is not lost — the next reconciliation sees it.

## Module seam

`ledger.py` is importable by path, independent of the CLI. Stable functions:
`validate_record(record)`, `normalize(record)`, `append(record)`, `read(run_id, filters)`,
`read_report(run_id, filters)` (records plus corrupt lines), `summary(run_id)`,
`home_path(raw, target_repo)`, `validated_authority(snapshot)`, `classify(records,
snapshot)`, `validate_report(report)`, `reconcile(run_id, authority, home, replay)`,
`render_report(report)`, and the `LiveAuthority` / `RecordedSource` adapters. Module callers
inject an authority object with `snapshot(run_id, records)` and, only if replay may run,
`replay(request_id, action, subject, command)`; tests use the shared fake in
`tests/fake_orca.py` and never touch a live Orca or the network.
