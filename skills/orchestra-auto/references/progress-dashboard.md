# Optional progress record and localhost dashboard

**When to read**: when the user wants observable Run/task/worker progress, a local dashboard,
or a reproducible offline demonstration. Load this only on demand; it is not a dispatch prerequisite.

Contents: [Operation](#operation), [Visual dashboard](#reading-the-visual-dashboard), [Protocol v1](#protocol-v1),
[Workflow ownership](#workflow-ownership), [Recovery and boundaries](#recovery-and-boundaries).

## Operation

This is an optional sidecar. The coordinator records observable facts after normal workflow actions.
Neither recording nor viewing schedules workers, accepts work, polls Orca, consumes messages, or
calls `check`, `ack`, or inbox commands. The existing unique inbox receiver remains authoritative.
There is no dependency on a running web server. On a progress command failure, retain the warning
and continue orchestration; do not turn a dashboard error into a dispatch failure.

Run from the installed bundle, setting `PROGRESS` to its `scripts/progress.py`. Runtime dependency:
Python 3 standard library on POSIX. Set `AUDIT_ROOT` to an existing persistent audit location
**outside every repository**, and use one new child directory per Run. Never use a repository root,
its descendants, an ancestor of the target repository, or a symlink. JSON may be passed as `--data`
or `--data -` on stdin. No environment, credentials, raw messages, terminal output or arbitrary
metadata are accepted as fields. Even allowed summaries must be manually sanitized; this is a
field allowlist, not a secret detector. Evidence and artifact values are references, never file contents.

```bash
PROGRESS=skills/orchestra-auto/scripts/progress.py
STATE="$AUDIT_ROOT/run-example"
python3 "$PROGRESS" --state "$STATE" init --event-id init-1 --effective --data \
  "{\"id\":\"run-example\",\"target_repo\":\"$PWD\",\"mode\":\"REAL\",\"phase\":\"implementation\",\"status\":\"active\"}" \
  || printf '%s\n' 'Progress warning; continue orchestration' >&2
python3 "$PROGRESS" --state "$STATE" serve --port 8765
```

Open **http://127.0.0.1:8765/**. The read-only page refreshes every three seconds; recording
commands run in another terminal and work whether the server is up or down. Stop viewing with
Ctrl-C; the snapshot remains. `show` prints the current snapshot without starting a server.
`serve --port 0` selects and prints an available address. `--stale-seconds 300` controls freshness;
unknown timestamps are unknown, old observations are stale, and a coordinator-recorded `lost`
lifecycle is displayed independently. Fresh activity does not mean meaningful progress.

For an isolated **DEMO**, use a **new** directory, separate from all **REAL** runs:

```bash
python3 skills/orchestra-auto/scripts/progress-demo.py --state "$AUDIT_ROOT/demo-example" --target-repo "$PWD"
python3 "$PROGRESS" --state "$AUDIT_ROOT/demo-example" serve --port 8766
```

Open **http://127.0.0.1:8766/**. The DEMO badge and illustrative references identify simulated
facts. The demo refuses any existing directory and cannot overwrite a real run. It makes no Orca
calls and needs no credentials. It includes normal work, dependency blocking, waiting for a human,
review rejection, nested workers, a lost worker and independent delivery facts. Use
`serve --stale-seconds 0` to demonstrate stale observations. Never present demo references as tests run.

## Reading the visual dashboard

The single bundled page defaults to Chinese and has an English switch. Both bundles ship the
same page bytes. A light canvas, fixed left section navigation and a top bar order the Run
overview, attention items, task statistics, static reference flow, dependency graph, tasks and
workers, delivery evidence and events. No external fonts, assets or frontend packages load. A
snapshot refreshes every three seconds. Task filters, expanded details, graph scroll and language
survive updates; errors show their time and preserve the complete last valid page.

Attention lists only recorded block reasons, pending decisions (including a Run waiting for a user
without any task-level decision), failed records and lost workers; each matching record counts
separately in its group; unreported problems never appear. The overview keeps last observation and
effective progress in separate tiles, and worker rows keep verified lifecycle, self report,
start confirmation and freshness separate.

The donut partitions **actual recorded task counts**, including unknown states; its center is the
actual task total. Each textual legend button filters the task list, with a visible filter and reset.
An empty snapshot shows zero with no invented slice. Neither chart estimates completion percentage
or remaining time. Four delivery cards independently count strict `true`, strict `false` and
null/missing (unverified) values; missing delivery records never mean success.

The workflow arrows are a **static reference path**, not scheduling, execution telemetry, or a
claim that previous steps passed. Blue indicates matching observations at that step only. Each step
has an expandable count basis:

| Reference step | Exact snapshot basis |
| --- | --- |
| Plan | Number of recorded `tasks`; this means tasks are recorded, not a verified plan approval |
| Dispatch / start | Workers with `start_confirmation == "observed"`; dispatched alone is not observed start |
| Implement | Tasks with `status == "running"`; not an implementation acceptance count |
| Report done | Workers with `reported_lifecycle == "reported_done"`; explicitly self-reported, not verified acceptance |
| Accept | Tasks with `status == "accepted"` |
| Integrate | Deliveries with `integrated === true` |
| Merge | Deliveries with `merged === true`; a PR artifact sets none |
| Tracker reconciliation | Deliveries with `tracker_closed === true`; merge sets none |
| Wait branch | Tasks whose status is `waiting_user` or have a nonempty `pending_decision`, counted once |
| Rework branch | Tasks with `status == "rejected"` |

Zero means no matching record, not verified false or proof the action never happened. The current
phase reads `run.phase` directly and never lights predecessor steps. Delivery false/null remain
separate from zero workflow counts. Worker self reports never update verified lifecycle or task state.

The scrollable native SVG dependency view draws prerequisite → task edges from the original
`dependencies` arrays. It retains unknown nodes, cycles and long titles (full text in accessible
SVG titles and expandable dependency text). This is a readable bounded layout, not a computed
execution order. Worker rows preserve parent/child indentation; verified lifecycle, self report,
start confirmation and freshness are separate. Full identities/worktrees and summaries are tucked
into expandable details. HTTP(S) references without credentials are links; all other values remain
text, and no file contents are served.

Optional browser regression checks require an externally installed Playwright and Chromium, not
runtime dependencies. Set `PLAYWRIGHT_MODULE` to that external package, then run
`node tests/progress_dashboard_browser.cjs http://127.0.0.1:8766/ EVIDENCE_DIR` against an isolated
DEMO server. It covers four viewports, semantic edge cases, refresh state and safe rendering.

## Protocol v1

`progress.json` is a UTF-8 snapshot with `schema_version: 1`, `run`, keyed `tasks`, `workers`,
`deliveries`, `artifacts`, ordered `events`, and private idempotency `receipts`. Each successful
mutation atomically replaces the entire snapshot under a process lock. `receipts` is omitted from
HTTP/show. Unknown versions fail without modifying the file. Back up this single snapshot for audit;
the tool never writes to the target repository. Snapshot evolution requires a new documented version.

All writes have this interface:

```text
python3 PROGRESS --state STATE COMMAND --event-id STABLE_ID --source coordinator|worker [--effective] --data JSON
```

Reuse an event ID only for an identical retry (same command, source, JSON and effective flag).
An identical retry is a no-op, including timestamps; a conflicting retry fails. New observations use
new IDs. The default source is coordinator; this is provenance supplied by a trusted local writer,
not authentication. Only the coordinator can set `--effective` after observing meaningful progress.
`last_observed` advances on recorded activity; `last_effective_progress` advances only with this
explicit verification. No elapsed-time or completion percentage estimate is made.

| Command | JSON summary fields (unlisted keys rejected) |
| --- | --- |
| `init` | Required `id`, `target_repo` (existing directory), `mode` (`REAL`/`DEMO`); optional `title`, `phase`, `status` |
| `run` | `phase`, `status`, `title` updates |
| `task` | Required `id`; optional `title`, `dependencies` (ID list), `status`, `blocked_reason`, `pending_decision` |
| `worker` | Required `id`; first registration also requires existing `task_id`, `dispatch_id`, positive integer `attempt`; optional `parent_worker_id` (registered first), `worktree`, `start_confirmation`, `lifecycle`, `reported_lifecycle`, `reported_summary` |
| `event` | Required `type`, `summary`; optional existing `task_id`, `worker_id`, `dispatch_id`, `attempt`, `evidence` (reference list) |
| `delivery` | Required existing `task_id`; independent boolean `implemented`, `integrated`, `merged`, `tracker_closed`; optional `evidence` |
| `artifact` | Required `id`, `kind` (`report`/`test`/`commit`/`pr`/`other`), `label`, `reference`; optional existing `task_id` |
| `end` | Required `status` (`completed`/`failed`/`paused`/`cancelled`); optional `phase`, `summary`; records `ended_at` |

Run status: `unknown`, `active`, `waiting_user`, `failed`, `paused`, `completed`, `cancelled`.
Task status: `unknown`, `pending`, `ready`, `running`, `blocked`, `waiting_user`, `reported`,
`accepted`, `rejected`, `failed`, `paused`, `cancelled`.
Worker lifecycle: `unknown`, `dispatched`, `running`, `waiting_user`, `reported_done`, `failed`,
`lost`, `reclaimable`, `closed`. Start confirmation: `unknown`, `observed`, `unobserved`, `unsupported`.
A missing field is unknown, not inferred success. IDs are portable letters/digits/`_.:-` (1–128
characters); summary strings are at most 4096 characters; reference lists at most 100 short strings;
JSON input at most 64 KiB. Tasks may name not-yet-recorded dependencies, displayed as unknown.
Dependencies and blocks are facts recorded by the coordinator, not an automatically computed schedule.
Each worker ID represents one immutable task/dispatch/attempt/parent identity; retries get new IDs.

Worker source can only update an already registered worker's `reported_lifecycle`,
`reported_summary`, or append an event. Every report must carry the matching `task_id`,
`dispatch_id`, positive `attempt`, and worker `id` (or event `worker_id`); stale attempts are rejected. It cannot alter verification, task state or delivery.
A worker event is visibly a worker report even if its summary claims success. Worker reports are
not acceptance; `worker_done` does not set `implemented`. The coordinator records `implemented`
after G-impl, `integrated` after integration evidence, `merged` after the merge receipt, and
`tracker_closed` only after tracker reconciliation. Opening a PR sets none of these automatically.
Absent facts are null and displayed as unverified; explicit true/false values mean verified yes/no. Corrections can explicitly set false.

Example record sequence. This wrapper always returns success, including under `set -e`; the warning remains visible and the coordinator continues its normal work:

```bash
progress_record() {
  python3 "$PROGRESS" --state "$STATE" "$@" || {
    printf '%s\n' 'Progress warning; continue orchestration' >&2
    return 0
  }
}
progress_record task --event-id task-1 --data '{"id":"t1","title":"Protocol","dependencies":[],"status":"ready"}'
progress_record worker --event-id dispatch-1 --data '{"id":"w1-a1","task_id":"t1","dispatch_id":"d1","attempt":1,"worktree":"protocol-worktree","lifecycle":"dispatched","start_confirmation":"unobserved"}'
progress_record worker --event-id start-1 --effective --data '{"id":"w1-a1","lifecycle":"running","start_confirmation":"observed"}'
progress_record worker --event-id report-1 --source worker --data '{"id":"w1-a1","task_id":"t1","dispatch_id":"d1","attempt":1,"reported_lifecycle":"reported_done","reported_summary":"Implementation ready for review"}'
progress_record delivery --event-id accept-1 --effective --data '{"task_id":"t1","implemented":true,"evidence":["acceptance-report.txt"]}'
progress_record end --event-id end-1 --data '{"status":"paused","phase":"waiting","summary":"Awaiting integration"}'
```

## Workflow ownership

The coordinator owns verified state. Record after the action, with stable task/dispatch/attempt IDs
and evidence references, in the same normal turn that handles that action. Do not parse chat text
or terminal output to infer state. Nested implementers explicitly report structured facts to the
top-level worker, who forwards task ID, dispatch ID, attempt and parent worker ID to the coordinator;
register parents first. Nested self reports remain self reports until coordinator verification.

| Normal workflow checkpoint | Writer and optional progress record |
| --- | --- |
| Run start | Coordinator: `init` REAL with target, stage/status; `task` for titles, dependencies and known blocks |
| Dispatch receipt | Coordinator: register `worker` with task/dispatch/attempt/worktree/parent, `dispatched`, start unknown/unobserved; record dispatch evidence event |
| Start confirmation | Coordinator: update start_confirmation and verified lifecycle only from real start evidence, `event` with evidence, `--effective` if justified; ready alone is not observed start |
| Worker question / human wait | Top-level worker forwards structured question summary; coordinator records `event`, task `waiting_user`, `pending_decision`, and verified worker lifecycle; Run waiting_user only if globally waiting |
| Human answer | Coordinator records sanitized decision event, clears pending_decision with an empty string, updates task/block/run state after replying through the existing channel |
| Heartbeat / completion report | Worker source: reported fields/event; coordinator records receipt evidence and may mark task `reported`; no acceptance or delivery inference, heartbeat is not effective progress |
| G-impl acceptance / send-back | Coordinator records evidence event, task `accepted` or `rejected` with reason; sets implemented only after checks; rejected work stays unaccepted until rechecked |
| Integration / PR creation | Coordinator records integration evidence and integrated independently; artifact kind pr records the actual PR reference, never inferred merge |
| G-merge / merge | Coordinator records gate event, then merged only after actual merge evidence; preserves existing one-way-door human decision rules |
| Tracker reconciliation | Coordinator explicitly records tracker_closed only after checking actual tracker state; no tracker means leave unverified and explain in event |
| Failure / lost worker / recovery | Coordinator records failure/block reason and verified failed/lost lifecycle; a retry has a new worker ID/dispatch/attempt; heartbeat alone cannot erase failure |
| Pause / cancellation / close-out | Coordinator records end with actual status and summary, artifact reports/tests/commits/PRs, lifecycle reclaimable/closed only after verification; unfinished delivery stays unfinished |

All existing dispatch/start, question/reply, acceptance, merge and lifecycle requirements remain in
force without this feature. Neither a missing record nor a viewer outage authorizes changing them.

## Recovery and boundaries

The recorder never calls Orca, so there is no worker-list contract or additional receiver to keep
alive. It only knows facts explicitly submitted. It cannot discover an unreported nested worker,
a merge, tracker closure, or an offline worker. The page labels last observation and effective
progress separately; staleness is observation age, not proof of failure. A viewer fetch error retains
the last valid snapshot with a visible error and its load time; it never substitutes an empty success.

The snapshot is fsynced, replaced atomically, and its directory fsynced. A crash before replace leaves
the old valid snapshot and perhaps an ignored `.progress-*` temporary file; a crash after replace
leaves the new snapshot, whose stable event ID makes retries safe. Lock acquisition times out after two seconds with a warning, so a contended recorder cannot
indefinitely hold the coordinator turn. Retry later with the same event ID. Locks release on process exit.
Do not delete an active lock file or run an automatic repair. A corrupt/unsupported snapshot fails
closed without overwrite: preserve it, restore a known-good audit backup into a separate external
directory and reconcile subsequent facts explicitly. No migration, truncation or guessed reconstruction.
Keep backup copies outside repositories as well. The growing event history is a local audit record,
not a database or a retention service; archive completed Run directories according to local policy.

The service binds only IPv4 localhost, has no write API, serves no arbitrary paths or file content,
and loads no external assets. All values render as text; only HTTP(S) references become links.
File paths, commit IDs and other references remain text. No raw inbox/terminal/message view exists.
Local writers and local browser access are trusted; this is not an authenticated multi-user service.
Do not forward its port or publish the audit directory.
