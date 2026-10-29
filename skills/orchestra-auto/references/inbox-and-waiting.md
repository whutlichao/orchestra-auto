# Communication, wakeups and lost-contact recovery

**When to read this**: before the first dispatch of each Run, for identity binding, inbox setup,
wakeups, and lost-contact recovery. Before later dispatches, only verify that the inbox remains
active. Command semantics follow this round's `orca skills get orchestration` and its messaging and worker-contract references. This file
uses `<ORCA>` for the executable chosen this round; the script receives that path through
`ORCA_CLI_COMMAND`.

---

## Contents

- [1. Identity and the inbox consumer](#1-identity-and-the-inbox-consumer)
  - [1.1 The status contract](#11-the-status-contract)
- [2. Collecting and acknowledging](#2-collecting-and-acknowledging)
- [3. Coordinator liveness and wakeups](#3-coordinator-liveness-and-wakeups)
  - [3.1 The contract: a wake is a command that ends](#31-the-contract-a-wake-is-a-command-that-ends)
  - [3.2 This machine: pi and omp](#32-this-machine-pi-and-omp)
  - [3.3 Without that capability](#33-without-that-capability)
  - [3.4 Install the next wait after every wake](#34-install-the-next-wait-after-every-wake)
  - [3.5 The wait, and its switches](#35-the-wait-and-its-switches)
- [4. The communication contract workers must follow](#4-the-communication-contract-workers-must-follow)
- [5. Missing completion reports and lost contact](#5-missing-completion-reports-and-lost-contact)
- [6. Teardown](#6-teardown)

---

## 1. Identity and the inbox consumer

After creating a Run, write the returned Run ID, coordinator handle, and each
Task/Dispatch/worker handle into a ledger outside the repository. Handles come from the real
calling identity — never from a terminal title or from "the most recent terminal". A background
process names the coordinator explicitly with `check --terminal <coordinator_handle>`; `check`
does not take `--from`.

Each Run gets its own directory and its own single consumer. Startup example:

```bash
ORCA_CLI_COMMAND=<confirmed executable path> <SKILL_DIR>/scripts/inbox-loop.sh \
  <run_id> /tmp/orch-<repo>-<run_id> <coordinator_handle>
```

Run that command the way §3 describes — as a blocking call the harness surfaces — **not** as
`nohup … &`. The script persists every batch atomically, fsyncs the parent directory after the
rename, acknowledges heartbeat-only batches, and then **ends the process** when something needs
you: that end is your wakeup. `--status --json` inspects a directory without consuming its queue,
and it is the interface new automation must use:

```bash
<SKILL_DIR>/scripts/inbox-loop.sh --status --json /tmp/orch-<repo>-<run_id>
```

The script needs Python 3's standard library only (POSIX file locks; works on macOS/Linux/WSL). A
file lock at startup excludes a second instance in the same directory, and a directory bound to a
Run/terminal/CLI may not be reused. Orca's waiter slots remain the final cross-directory
constraint. Do not migrate an old script's run directory: confirm and stop the old loop and its
waiter first, keep the old messages, and use a fresh Run-specific directory.

`--status` only inspects that directory's lock and state; it does not scan other Runs' processes
and does not consume the queue.

### 1.1 The status contract

`--status --json` prints one JSON object on stdout and exits `0`/`1`/`2`/`3`:

| Field | Meaning |
| --- | --- |
| `loop` | `alive` while a loop holds this directory's lock, `dead` otherwise, `unknown` when the lock cannot be probed — never judged from the pid file |
| `phase` | the `phase` recorded in `state.json` (`starting`, `waiting`, `checking`, `backoff`, `reconciling`, `wake`, `error`), or `null` |
| `saved` | number of `deliveries/*.json` files — durable records, not necessarily handled |
| `unacked` | `saved` minus the delivery ids with a **successful** local receipt; `pending` and `failed` are evidence, not an ack |
| `blocked` | `{"deliveryId", "reason": "unacknowledged"}` naming the FIFO head while it is unacknowledged, else `null` |
| `last_activity_at` | the newest mtime among `state.json`, `watch.log` and `deliveries/`, in UTC ISO-8601, or `null`; the only timestamp to trust from a dead loop |
| `watch` | `on`/`off`, the `WORKER_WATCH` this install recorded, `unknown` if no install recorded one |
| `run`, `terminal` | the binding this directory belongs to, or `null` |
| `reasons` | the degradations found: `blocked`, `watch_off`, `loop_not_running`, or `fatal` / `unreadable_delivery` / `missing_state_directory` |
| `degraded` | `true` when `reasons` is non-empty, so a caller never has to compare codes |
| `error` | the fatal text when the loop recorded one |

Exit codes, and nothing else:

- `0` — resting after a wake (the normal `phase=wake` death with the head acknowledged), or a
  healthy live loop: the lock is held or the state is clean, no unacked head, watch on;
- `1` — a fatal error: `phase=error`, the last `ERROR` line in `watch.log`, or an unreadable
  delivery file (never read as an empty inbox);
- `2` — the state directory does not exist;
- `3` — degraded or blocked: an unacknowledged FIFO head (`blocked`), worker watching switched off
  (`watch_off`), or a loop that stopped with an active phase and wrote neither a wake nor an
  error (`loop_not_running`).

The computation is **local**: delivery files, `receipts.jsonl`, `state.json`, `watch.log` and the
directory lock. `--status --json` runs neither `check` nor `check --peek`, so it never consumes the
queue and never changes a delivery's fate.

Legacy plain `--status` (no `--json`) keeps its current `0` (a loop holds the directory) / `1`
(everything else) exit codes for one minor version and prints a deprecation notice on stderr; it
shows the same local facts in text form. The new `2`/`3` codes belong to `--json` alone. The
install command's own exit `2` — "another instance holds this directory / the directory is bound to
a different Run, terminal or CLI" — is a separate contract and does not change; a refused install
leaves the owner's state directory byte for byte as it was.

`loop: DEAD` is the normal resting state after a wake — the loop exited on purpose, and
`state.json` then carries `phase=wake`, the reason and the `deliveryId`.
`phase=error`, or an `ERROR` line in `watch.log`, is a different thing: that stop is a wake you
must investigate before restarting. Do not auto-restart until the binding, permission or waiter
issue is resolved, and do not kill another Run's process.

**Never judge liveness by the pid file alone.** The consumer removes `inbox-loop.pid` as it exits,
so "no pid file" and "the pid in the file no longer exists" both mean dead, and only `state.json`
says *why* it died: `phase=error` with the error text, or `phase=wake` with the wake reason. An
external waiter that polled pid-file liveness alone already missed one real death this way — the
file had been unlinked by the consumer on its way out, so the waiter read "nothing to check" as
"still fine" and stayed asleep.

Each cycle the script first checks without a filter and then installs a blocking wait, so
heartbeats and ordinary messages cannot pile up at the head of the queue. A complete response is
written atomically to `deliveries/*.json`, hashed by deliveryId, and the parent directory is
fsynced after the rename — the file write alone is not enough, because the rename is a directory
entry — so a saved batch survives power loss; files are never rotated away. A batch that is
**already** on disk **and was already announced** is never treated as news: it is your outstanding
duty, so the loop pauses instead of waking you again for it or busy-looping on it. `NEW-MESSAGE`
is only a hint about the most recently saved batch, **not a to-do list**; when a new message
arrives or a session resumes, enumerate every delivery file and reconcile against the ledger.

On a fresh install the loop reconciles every delivery file against the successful receipts. If the
oldest unacknowledged batch was saved but never announced, it emits exactly one `delivery` wake
whose `detail` names the reconciliation, writes `reconciled_delivery_id` and a local observation
to `state.json`, then exits. A second install does not wake again for that batch, and a batch that
was already announced is not news. This is install-time behaviour only: in steady state a saved
batch remains your outstanding duty. The reconciliation reads `worker-list` and `state.json` once,
records the worker baseline in the observation, and writes only `state.json`; the coordinator
transcribes the observation into the ledger, and the consumer never writes the ledger itself. The
observation names `kind`, `saved`, `unacked`, `blocked`, `reconciled_delivery_id` and
`baseline_worker_signature`, with `write_scope: state.json` and `transcribe_to: ledger`.

Errors are written to disk explicitly and end the process; a CLI failure, a JSON error or a lost
binding must never be interpreted as an empty queue.

## 2. Collecting and acknowledging

Maintain a per-message-ID ledger: `received → processing → handled`, recording reply receipts,
acceptance results, and terminal-ownership decisions. Reconcile first on recovery; a duplicate
Delivery is never re-replied, re-merged or re-released.

Write the `processing` entry **before** the action it describes, never after it, and have it name the
action (a merge, a `reply --id`, a file mutation) and the attempt. The ordering is the whole point.
An action can complete and the process die before any record of it exists, and because the ack is
deferred until the batch is handled, the batch stays unacknowledged while its effect may already have
landed. A ledger written afterwards cannot tell that case apart from "never ran", and the consumer
gives at-least-once delivery with no duplicate wake — it does not give exactly-once effects. So on
recovery an entry stuck at `processing` means **establish the fact before replaying**: read the real
state (is the PR merged, did the reply land, was the file changed) and decide from that evidence, not
from the ledger. Where the effect is verifiable and the action is idempotent, a replay under the same
delivery ID is safe. Where the effect cannot be established from evidence, it goes to the human
rather than being re-executed blind — an unverified replay is never inferred from the ledger alone.

1. Read the whole Delivery and verify its Run/Task/Dispatch belong to this round's authoritative
   attempt; keep old attempts' messages as evidence and never complete a new attempt from them.
2. Answer questions with `reply --id <message_id>`; record anything awaiting a human decision as
   pending and never self-answer it on timeout. Validate a `worker_done` against the real Dispatch
   and artifacts; a settled lifecycle is not a passed deliverable.
3. Only after handling the whole batch and any needed terminal-ownership decision do you ack the
   `.result.deliveryId` of **that read snapshot** through the script:
   `inbox-loop.sh --ack <delivery_id> <state_dir>`. It appends a `pending` receipt, calls
   `orca orchestration check --ack <delivery_id>`, then appends `success` or `failed` to
   `receipts.jsonl`. An Orca refusal records `failed` and leaves the batch unacked; retry with the
   same delivery id, which reuses the same local `request_id`, and keep the delivery file. The
   receipt is never written as `success` before Orca accepts, so a success receipt is the proof
   the ack landed; on failure keep the ledger entry and re-verify — never assume it was
   acknowledged. The entry exits `0` when the ack landed, `1` when Orca refused or the id was
   never saved, and `2` for a usage error or a state directory without a binding.
4. When a question waiting on a human blocks the FIFO, the head is **blocked**: its delivery id
   and the reason `unacknowledged` are computed from the saved deliveries and successful receipts
   (`blocked` in the status contract), so handle the head first. To inspect later messages without
   consuming them, run a read-only
   `check --run <run_id> --terminal <handle> --all --json`, handle other workers' questions and
   completion reports, and record them; `--all` offers no ackable Delivery and you must not
   fabricate an ID. Consume the original batch afterwards, batch by batch, and only reconcile
   already-handled messages.

Every ack — automatic or coordinator — appends an append-only receipt line to `receipts.jsonl` in
the state directory: `{delivery_id, request_id, acked_at, result}`. `pending` is written before
the Orca call and `success` or `failed` after it; `request_id` is the stable local key
`ack:<delivery_id>`, so a retry after a refusal reuses it. A delivery id this consumer never saved
is refused before Orca is called: `check --all` cannot yield an ackable id, and no id may be
fabricated. The receipt file is what makes `unacked` shrink and the blocked FIFO head clear.

The script auto-acks only a non-empty batch that is already on disk and where **every** message is
a heartbeat; mixed batches and unknown types always go to the coordinator. The automatic ack
writes the same pending/success receipts; an Orca refusal records `failed`, leaves the batch
unacked, and ends the loop as an error wake — it is never retried by the loop. Type filtering only
decides when the waiter wakes, and does not remove the obligation to handle the whole batch. The
script never deletes a local to-do, and an empty queue does not mean the ledger is complete.

## 3. Coordinator liveness and wakeups

A successful `send` is a durable enqueue; wake and nudge are best-effort. `nohup`, a file
appearing, or a live background consumer does **not** mean the agent will produce a new turn, and
you must not claim "it will continue automatically" on that basis.

### 3.1 The contract: a wake is a command that ends

Harness-independent, a wake has exactly one shape:

> A wake is a **command whose end — or whose output — the harness surfaces to you as a new agent
> message.**

Nothing else wakes you. The inbox consumer is built to *be* that command: it persists the batch
first, then ends the process and prints exactly one line on stdout. That is the correct division of
labour, and it is stated as: **the consumer persists batches and ends on a wake event.** It still
never sends anything, never replies, and never acknowledges a batch a human has to read.

The wake line (stdout, exactly one line):

```json
{"event": "wake", "reason": "delivery", "run": "run_ab12", "terminal": "term_coord",
 "deliveryId": "dlv_9f", "types": ["worker_done"], "messageCount": 1,
 "saved": "/tmp/orch-repo-run_ab12/deliveries/<sha256-of-deliveryId>.json", "detail": "new batch dlv_9f saved: 1 message(s) ['worker_done']"}
```

| Field | Meaning |
| --- | --- |
| `event` | always `wake` — the line's purpose, so a parser never has to guess |
| `reason` | `delivery` (a new batch was persisted), `worker_state` (a worker signature changed), `error` (the loop's own fatal error) |
| `run`, `terminal` | the binding the loop was installed for |
| `deliveryId` | the batch that caused the wake; `null` for the other two reasons |
| `types` | the **whole** batch's message types in FIFO order; `[]` when the wake was not a batch |
| `messageCount` | number of messages in that batch; `0` otherwise |
| `saved` | the file the batch was written to *before* the wake; `null` otherwise |
| `detail` | one line naming the reason; for `error`, the error text, flattened and truncated |

Exit codes: `0` a wake; `1` a fatal error of the running loop — itself a wake, with the same line at
`reason=error` plus `watch.log` and `state.json phase=error`; `2` the installation was refused
before the loop started — another instance already holds this state directory, or the directory is
bound to a different Run/terminal/CLI (both print the same `reason=error` line and an `ERROR` line in
`watch.log`, and neither touches the `state.json` of the loop that owns the directory) — or the
arguments were a usage error, the one path that prints nothing on stdout and says so on stderr
instead, because the loop never started and there is no state directory to report to. Everywhere
else, outside `--status`, stdout carries that line and nothing else.

All three legs wake, and each one exists because the others cannot see it:

1. **New messages** — a batch that was not on disk before and contains anything other than
   heartbeats. A heartbeat-only batch is acknowledged and does **not** wake: it carries no
   decision. A FIFO replay of a batch already saved and already announced does not wake either —
   it is your outstanding duty, not news, so handle it from the ledger. The one exception is
   install-time: an unacknowledged batch that was saved but never announced produces exactly one
   reconciliation wake (§1), and the persisted `reconciled_delivery_id` prevents a second one.
2. **The loop's own death or error** — a non-zero exit is a wake by itself, because the process
   ending is what the harness surfaces; the `reason=error` line says why. A consumer that stops
   silently is the "total silence" case in §5.1, not an empty queue. One real case, from the
   field: the Orca app restarts while the consumer is blocked in `check --wait`, and the CLI
   answers `runtime_unavailable` ("The Orca runtime closed the connection before responding…",
   with a `request-show --request <id>` pointer). The consumer writes `phase=error`, exits
   non-zero and prints `reason=error`; nothing is lost, because the queued Deliveries are still on
   the server and come back on the next install. **Never turn an error exit into a normal one to
   satisfy this wake contract**: the exit is the wake, and an exit that swallows the failure hides
   exactly the incident you need to look at.
3. **Worker settlement state** — every `WORKER_POLL_SECONDS` (default 15 s) the loop reads the
   read-only `worker-list --run <run_id> --include-remote --cursor <cursor> --limit <n>` and
   reduces this Run's workers to a signature. It follows `page.nextCursor` until `page.hasMore`
   is false, so a remote worker on a later page is part of the signature; a page that claims
   more rows but carries no usable, unseen cursor — or no `page` object at all — is a
   **truncated** read and is reported as an error, never compared as if it were complete. When
   the signature no longer matches the one taken at install time, the loop wakes. This is what
   catches a worker that changed state *without* sending a message — crashed, requeued, settled
   without a report — so you do not have to wait for a `worker_done` that may never come.

   Each line is `dispatchId:workerState:dispatchStatus:stage.detail`, extended with the scalar
   fields that actually exist: `liveness.verdict` (the live CLI's field), `liveness.state` (the
   spec's name for it, and `unknown` when absent — the deviation is deliberate and recorded
   rather than silently swapped for the observed one), `attention.requiresAction`,
   `nextAction.kind` and `stage.detail`, plus a trailing `liveness.reason=<value>` segment only
   when that field is present. A missing field serializes as `unknown`, an object-valued field
   is reduced to `unknown` rather than embedded whole, and a count is never the signature.

Compare **whole signature lines, never a count**. A count cannot see one worker replace another,
and a snapshot taken after the event is how a real wakeup was missed once already: a question that
landed before the snapshot never triggered it, and only a content-level fingerprint fixed it. For
the same reason, never let a single signal (a file appearing, a `NEW-MESSAGE` marker, a heartbeat)
stand in for "there is something here".

**`WORKER_WATCH=0` switches leg 3 off and is a degradation, never healthy.** The install records
`watch: off` in `state.json`; `--status --json` reports `watch: "off"`, `reasons: ["watch_off"]`
and exit `3`. The documented replacement is a periodic read the coordinator installs itself —
`worker-list --run <run_id> --include-remote --json`, every page, at the interval
`WORKER_POLL_SECONDS` would have used — recorded in the ledger as an observation with its
timestamp. Leg 3 is the only wake that sees a worker change state without sending anything, so
switching it off moves that duty to the coordinator; it does not remove it.

### 3.2 This machine: pi and omp

Their bash tool surfaces long calls as jobs: a non-PTY command that outlives the foreground wait is
moved into a background job (`Backgrounded as job <id> …`), and its completion or failure is
delivered by the job manager as a follow-up message. Results also auto-deliver without polling —
`wait` blocks until the next background result or peer message, and its safety cap is 30 minutes.
So the wake is:

- install the loop as that long call and end the turn;
- when its stdout arrives, **that message is the wake**: handle the batch, then install the next
  wait (§3.4).

Polling the loop's stdout, or sleeping in the foreground to watch the job, is the anti-pattern this
replaces: it burns turns and can miss the harness's own delivery.

### 3.3 Without that capability

Fall back to the bounded foreground tier: keep the coordinator turn and wait in tool calls, no more
than 60 seconds each; after each return, read the inbox and ledger and continue other authorized
work. Do not emit a final and assume a background script will wake you. If the environment forces
the turn to end, report explicitly which Dispatches are still running, the last progress, the
ledger directory, and the limitation that you cannot be woken automatically; the recovery entry
point is that ledger plus the current Orca state. Never write that state as completed.

### 3.4 Install the next wait after every wake

Every wake ends the process on purpose, so **handling messages and installing the next wait are one
step**. Not re-installing is exactly the failure users report as "poke it and it moves, then it
stops responding": the batch was persisted, the wake arrived, the messages were handled — and then
nothing was waiting any more, so the next message had no listener. Treat the reinstall like the ack
in §2: part of finishing the batch.

Timeouts are not wakes. A blocking `check --wait` that reaches `--timeout-ms` returns an empty
batch, the loop backs off and installs the next wait by itself, and nothing is printed.

### 3.5 The wait, and its switches

The consumer waits with the CLI's native block — `check --wait --types
worker_done,escalation,question --timeout-ms <n>` — instead of a short poll plus `sleep`. Facts to
rely on, straight from `check --help`: `--types` is only the wake **condition**; the Delivery a
`--wait` returns is always the whole FIFO batch and is never filtered by type; and the CLI writes a
JSON keepalive line to **stderr** every 15 seconds so a caller can tell the process is alive.
`_keepalive` is unrelated to heartbeat messages. The loop keeps that child's streams in its own
files, so keepalive lines never contaminate the wake line on stdout.

| Environment switch | Default | Meaning, and why that default |
| --- | --- | --- |
| `ORCA_CLI_COMMAND` | `orca` | the executable confirmed this round; the loop passes it to every call |
| `WAIT_TIMEOUT_MS` | `30000` | cap for one blocking `check --wait`. The loop re-installs the wait after each timeout, so the cap only decides how often it reconnects; keep it comfortably below whatever deadline the harness puts on the call |
| `BACKOFF_SECONDS` | `5.0` | pause before installing the next wait after a timeout or an acknowledged heartbeat batch, so a CLI that answers instantly cannot be hammered |
| `PENDING_BACKOFF_SECONDS` | `30.0` | pause while the FIFO head is a batch you have already saved and not acked. Long, because nothing changed on the server side yet |
| `WORKER_POLL_SECONDS` | `15.0` | how often leg 3 re-reads the worker signature, during a wait and during a pause. Short enough to notice a change between two 30-second waits |
| `WORKER_WATCH` | on (`1`) | leg 3 itself; `0` (or `false`/`off`/`no`) switches it off. On by default because it reads one read-only list per interval and it is the only leg that sees a worker change state without sending anything; switch it off for a Run whose workers churn their stage detail, or when the installed CLI cannot answer `worker-list --run`. Off is a documented degradation (`watch: off`, exit `3`) and carries the replacement duty above: poll `worker-list --include-remote` yourself |

## 4. The communication contract workers must follow

These requirements must be written into the §4.4 spec; a worker never sees this file automatically:

- Use the live preamble's real executable, handle, Task ID, Dispatch ID and any attached authority
  parameters.
- Check proactively with `check --terminal <own handle>` at every natural checkpoint, including on
  start, after tests, before switching files, and before finishing. The coordinator reaches a
  running worker with `send --to dispatch:<id>`; a send needs read/executed evidence afterwards.
- Resume an `ask` timeout under the same message ID; follow the preamble's heartbeat cadence; never
  open a user-facing question UI directly.
- On success or failure send exactly one `worker_done` from your own terminal, with an explicit
  outcome, a three-sentence summary and real artifact paths; keep the success receipt as evidence,
  then end the turn. A plain final, an already-open PR, a heartbeat, or "I am done" is not a
  lifecycle report.
- When the send result is unknown, inspect the Dispatch/message facts first; a settled dispatch is
  never re-sent. Only when it is confirmed unsettled, the report was not enqueued, and the
  authority is still valid may you retry once; if it still fails, keep the artifacts and report
  the communication blockage. On consumer_fenced, stop immediately.

## 5. Missing completion reports and lost contact

### 5.1 The message left the worker and the coordinator never saw it

"Whatever it sends me reaches nothing" usually breaks in one of five places. Check them in this
order; the first two account for nearly every case.

| Break | Symptom and one-line evidence | Action |
| --- | --- | --- |
| Wakeup | A `worker_done` is already on disk or in the queue, yet the coordinator's turn ended long ago and nothing started a new one: delivery files newer than your last turn, or a stale `NEW-MESSAGE`, with no confirmed wake method from §3 | A successful `send` proves a durable enqueue only; wake and nudge are best-effort. Confirm which §3 branch you have. With a harness that surfaces job completion, the end of the consumer — the wake line in §3.1 — **is** the wake; if it never arrived, check that the consumer was installed as §3.2 describes and not via `nohup`, and that its next wait was installed after the previous wake (§3.4). Without such a harness, stay in the bounded tier of §3.3 or record the un-wakeable state before yielding. Never read "the loop is alive" as "I will be woken" |
| Unacked FIFO head | Earlier messages were fine, later `worker_done` messages never appear, and an unanswered question or decision sits at the head of the batch | `check` returns the bound Run's **oldest unacknowledged** Delivery and replays that exact batch until `--ack`, so one unacked batch hides everything behind it. The head is `blocked` with its delivery id and reason `unacknowledged`, computed from the delivery files and `receipts.jsonl`. Handle the whole batch, then ack that snapshot's `deliveryId` through `inbox-loop.sh --ack` (§2 step 3) so the receipt records pending, then success or failed. While a human decision blocks the head, read later messages with the read-only `check --all` (§2 step 4); `--all` never yields an ackable ID. An ack is not a reply |
| Interrupted action | The batch is still unacked, its ledger entry sits at `processing`, and the process died between doing the work and recording it — so the effect may already have landed | Establish the fact before replaying (§2): read the real state — `pr view` for a merge, the reply receipt, the file itself. Replay under the same delivery ID only when the effect is verifiable and the action is idempotent; an effect you cannot establish from evidence goes to the human instead of being re-executed blind. This is the `runtime_unavailable` judgement one layer up: the mutation may already have taken effect, so prove it or ask, never assume |
| Identity or mailbox | Nothing arrives at any inbox, or messages land where you are not looking | `check` names its caller with `--terminal <handle>` and rejects `--from`; a handle taken from a terminal title, from "the most recent terminal", or from another session or Run addresses the wrong mailbox. Take handles from the real calling identity (`orca status --json`) and keep them in the ledger. A `worker_done` or heartbeat cannot target a group, and its sender handle must equal the Dispatch assignee exactly or the task never settles |
| Dead consumer | Total silence, heartbeats included: `--status` reports DEAD with `phase=error` or an `ERROR` line in `watch.log`; or the wake line arrived with `reason=error` | The script writes CLI failures, JSON errors and lost bindings to disk and exits non-zero on purpose, so a stop is never an empty queue — and that stop is itself a wake (§3.1). Judge death from `state.json`, never from the pid file alone (§1). Read the `ERROR` line and the `detail`; a binding/waiter problem usually means another instance or another Run's loop took the slot. Before re-issuing anything, follow the CLI's own recovery guidance when it printed one: a `runtime_unavailable` error carries a `request-show --request <id>` pointer, and the mutation may already have taken effect, so establish that with `request-show` and replay it with `--retry-request <id>` rather than treating it as a fresh attempt. The consumer never retries by itself. Waiter slots are a cross-directory resource: before starting a new loop, confirm and stop the old one, and never migrate an old state directory |
| Worker never sent | Only heartbeats arrive, or a plain final that claims success does; an auto-acked heartbeat-only batch looks like traffic while reporting nothing | Write the §4 contract into the spec, and classify with 5.3 |

### 5.2 Reconcile all three sources, never one

When a report is missing or a worker looks gone, read these together; each one on its own can look
empty, or healthy when it is not:

1. **Local**: `--status --json` for `loop`, `phase`, `saved`, `unacked`, `blocked`,
   `last_activity_at`, `watch` and `reasons` (§1.1) — it is one local read and it never consumes the
   queue; enumerate **every** `deliveries/*.json` instead of trusting `NEW-MESSAGE`, which hints at
   the newest saved batch and is not a to-do list; compare them with the successful receipts in
   `receipts.jsonl` (an unacked head is `blocked`); read the `ERROR` lines in `watch.log`,
   `state.json`'s last reconciliation observation, and its `phase=wake` reason for whatever ended
   the last install.
2. **Server queue**: the read-only `check --run <run_id> --terminal <handle> --all --json`.
3. **Worker state**: `worker-list --include-remote --json` (read every page), then `worker-show`
   and `worker-read` for the Dispatches in question — `projection.attention.requiresAction`, the
   literal `projection.nextAction`, and `liveness.reason`.

Classify the result with 5.3. Do not stare only at `NEW-MESSAGE`, and do not wait only for a
`worker_done`.

### 5.3 Lifecycle branches

| Observation | Action |
| --- | --- |
| Dispatch settled, no worker_done in the inbox | Reconcile with `check --all`, the real settlement state and the artifacts; record it and continue acceptance. Do not ask for a resend and do not dispatch a duplicate editor |
| Worker turn ended, not settled, and the final turn sent no worker_done | Read the current Orca recovery guide. Once you confirm the terminal still belongs to the original active Dispatch and recovery is allowed, send one explicit finish-the-receipt instruction: check follow-ups, verify artifacts, report worker_done from your own preamble; re-check turnStart/transcript |
| `send` enqueued but unread by the worker | Check whether it is stuck in an `ask`, a permission prompt, or an already-idle turn; for a confirmed idle local terminal you may use the terminal resume mechanism to remind it to read the original Dispatch inbox. Do not re-dispatch the body as a new Task |
| `liveness=unverifiable` / `agentWait=null` / idle title | This is only missing evidence; read the transcript/`worker-show` and `nextAction`. Do not stop, do not release, do not re-dispatch |
| Real exit evidence and recovery impossible | Follow the recovery guide for stop/abandon/retry, keep the artifacts, and only dispatch a recovery worker after the old attempt's authority has ended |
| Wrong identity/Run binding, consumer_fenced | Stop operating on that identity and verify the real caller and the authority relationship; do not impersonate a handle and do not unconditionally create a new Run |

A finish-the-receipt nudge or a wakeup attempt is made at most once, then the evidence is
re-checked; if there is still no progress, preserve the state and report the blockage. Do not loop
injecting Enter, do not repeat the report, and do not re-dispatch on implicit authorization. The
coordinator never sends a worker_done on a worker's behalf and never edits or commits code for it.

## 6. Teardown

Completion reports, acceptance and merges are recorded separately. Release only after a real
settlement and persisted artifacts, per the lifecycle protocol; a settled terminal awaiting
acceptance or merge must not be silently left behind — keep an explicit next step and deadline in
the ledger. Once every expected Dispatch is handled and the ledger has no open items, stop this
Run's consumer (read the loop PID from this directory and confirm the process identity before
SIGTERM); the script reclaims its own CLI child processes. A consumer that already exited on a wake
needs no teardown — install nothing further and leave `state.json` as the record. Only after the
persistent audit report has saved the key receipts and conclusions should you consider cleaning up
temporary message files.
