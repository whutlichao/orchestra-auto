# Communication, wakeups and recovery

**When to read**: before the first dispatch and after a lost response or context. Resolve the executable once using the local Orca discovery stub, then read its version-matched `skills get orchestration` and command help. The observations below are from Orca **1.4.248**; another version requires its own guide and help. If native wake or message recovery is not supported, stop and report the unsupported contract rather than guess an older protocol.

## Identity and native wake

Record Run, Task, Dispatch and runtime-issued handles in the out-of-repo ledger. Follow the live worker preamble; it takes precedence. Start independent workers and end the turn: Orca wakes the coordinator with each whole chat message. A wake naming a `check` command requires the agent itself to run that command. Handle the entire batch before another consuming check, and record each message ID, its handling status, outcome and evidence. Deduplicate repeated messages by ID; reconcile effects before repeating any mutation.

A plain `check` marks its returned oldest unread FIFO batch handled immediately, including on `--wait`. It is not an acknowledgement of the coordinator's business action. Never install a background consuming check, poller, or the retired `inbox-loop` script: it can take mail away from the agent and suppress the native wake. For a one-off read-only inspection use `check --peek`; do not poll or install a watcher. `--types` on a wait controls waking, not the contents of the delivered batch.

**How this session's handle is resolved.** Orca has two identity classes; do not mix them:

- **Structured sessions / supervised workers**: `orca status --json`'s
  `result.caller.orcaSessionId`, which the CLI resolves from the injected
  `ORCA_AGENT_SESSION_ID`. Only these sessions carry it.
- **A TUI agent terminal Orca launched directly** (the usual shape for pi / omp here):
  `ORCA_TERMINAL_HANDLE` in the environment. That is the runtime handle Orca issued to this
  terminal; match `ORCA_AGENT_PANE` (`<tabId>:<leafId>`) against the row in
  `orca terminal list --json` to confirm it is yours, then write it to the ledger.

An empty `caller` is **not** "no identity" — a TUI agent terminal never receives a session id,
and `status` omits caller by design rather than guessing from a terminal. Verify the handle with
this block (a non-zero exit means it is not yours):

```bash
[ -n "${ORCA_TERMINAL_HANDLE:-}" ] || { echo "no runtime terminal handle" >&2; exit 1; }
orca terminal list --json | python3 -c '
import json, os, sys
handle = os.environ["ORCA_TERMINAL_HANDLE"]
tab, leaf = os.environ["ORCA_AGENT_PANE"].split(":", 1)
row = next(r for r in json.load(sys.stdin)["result"]["terminals"] if r["handle"] == handle)
assert (row["tabId"], row["leafId"]) == (tab, leaf), "handle does not belong to this pane"
print(handle)
'
```

If identity cannot be verified, do not guess from a title or the newest terminal. The agent executes a check when the native wake names it; for example `--terminal "$ORCA_TERMINAL_HANDLE"` must name its own handle.


## Recovery and audit

If a check response is lost, retry with its printed `--retry-request` to recover that batch. If context is lost, read `orca orchestration inbox --terminal run:<run_id> --limit 50 --full` for the coordinator, or `dispatch:<dispatch_id>` for a worker, including handled mail. Reconcile that evidence with the ledger and real worker/PR state. A recorded message does not prove its side effect occurred: read back before replay, reuse a documented idempotency key where supported, and escalate an ambiguous irreversible effect. Do not fabricate a delivery ID or assume an old acknowledgement is required. The 1.4.248 help describes `--ack` only as optional compatibility for older Orca; no older runtime has been verified here, so do not enable the old consumer or invent its semantics.

The retired `scripts/inbox-loop.py` and `.sh` fail closed without changing old state or calling Orca. Preserve existing delivery files and receipts as historical audit evidence; do not delete or migrate them automatically. No current health claim may be inferred from an old loop state.

## Legacy consumer migration

Perform this migration **before relying on native wake or resuming this Run** whenever an old consumer may have run. Replacing the script on disk does not stop a process already executing the old code. Keep the old state directories and logs intact throughout; never run their old `--ack` entrypoint.

1. **Inventory all potential owners.** On every host and session that could have run the consumer, use the Run ledger, launcher or job records, process listings, command lines and working directories to find every old loop instance and every bound state directory for this mailbox, including copies with different paths. Record `binding.json` (`run`, `terminal`, `cli`), `state.json`, `inbox-loop.pid`, `inbox.lock`, the job/launcher and observed process and child identities. Check the actual lock holder and its process command line/start time, not a PID file alone: PIDs can be stale or reused, a free per-directory lock does not exclude a consumer in another directory, and a missing PID file does not prove exit. Include any in-flight `orca orchestration check --wait` child. If hosts, permissions, inventory completeness, ownership or identity cannot be established, **block migration**, preserve evidence, and escalate; do not assume a process is gone.
2. **Quiesce the verified owner.** First stop its launcher or automatic restart under the owner’s control, then stop only the verified process identity belonging to this Run using its owning job/session's graceful stop. For the known old script, SIGTERM invokes its child cleanup; send it only after verified process identity and authority, never by an unverified or reused PID, broad process-name match, or to a user-owned/unknown process. Confirm the launcher cannot restart it, confirm exit and no surviving check from that consumer, and independently inspect the lock holder and process/child inventory on the same host again. An orphaned or unidentifiable check, an inaccessible host or a held/unknown lock is not proof of quiescence: block migration and escalate. Do not remove the lock file or state to force a pass.
3. **Reconcile all local evidence.** After quiescence, enumerate every known state directory, every `deliveries/*.json` batch and the complete `receipts.jsonl`, including archived copies if present; preserve unreadable, partial or conflicting records. Read each batch's `result.deliveryId` and `result.messages`, correlate its receipt and ledger entries, and verify Run/Task/Dispatch and attempt ownership. Deduplicate by message ID across batches, inbox and ledger, while preserving source paths and conflicts. A successful ack is not proof that the business action was handled; a failed or missing receipt is not proof that it was not handled. Do not rely on the latest 50 inbox rows as a complete archive. For every message record `received`, `processing`, `handled` or pending with evidence; inspect the actual reply, PR, worker or other authoritative effect before taking further action. Never automatically repeat an uncertain irreversible effect or legacy ack. An unreadable archive, absent message ID, unresolved conflict or unverifiable effect remains pending and blocks migration until resolved; escalate decisions and preserve evidence.
4. **Resume only after the inventory and reconciliation are complete.** Record the quiescence evidence, every source examined, each message disposition and remaining decisions in the out-of-repo ledger. Confirm no competing consumer remains, then follow the current native-wake contract. Pending human decisions are not successful migration; report the blockage rather than inventing a disposition.

## Worker and lifecycle recovery

A worker uses the current preamble to ask or finish, then ends its turn. A `worker_done` settles its Task; a failure is never marked successful. For silence or uncertain liveness use `worker-list`, `worker-show`, `worker-read` and actual artifacts; silence alone never authorizes stop, retry or release. If the app restarts, confirm status and inventory workers before acting; resolve the original binding and recover mail from inbox. Follow the live guide's recovery reference for uncertain or fenced operations. Never impersonate a handle or create a duplicate Run as a substitute for evidence.

In 1.4.248 finished workers close automatically unless kept open; new-worktree workers default to kept-open. Inspect `worker-list` and follow its `next` when present; preserve user-kept chats, and release only settled resources when appropriate and authorized. Auto-release of reclaimable workers occurs at `resource.autoReleaseAt` unless reused or retained. Do not treat retained as reclaimable or infer process exit from Task completion.
