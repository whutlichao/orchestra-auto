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

## Worker and lifecycle recovery

A worker uses the current preamble to ask or finish, then ends its turn. A `worker_done` settles its Task; a failure is never marked successful. For silence or uncertain liveness use `worker-list`, `worker-show`, `worker-read` and actual artifacts; silence alone never authorizes stop, retry or release. If the app restarts, confirm status and inventory workers before acting; resolve the original binding and recover mail from inbox. Follow the live guide's recovery reference for uncertain or fenced operations. Never impersonate a handle or create a duplicate Run as a substitute for evidence.

In 1.4.248 finished workers close automatically unless kept open; new-worktree workers default to kept-open. Inspect `worker-list` and follow its `next` when present; preserve user-kept chats, and release only settled resources when appropriate and authorized. Auto-release of reclaimable workers occurs at `resource.autoReleaseAt` unless reused or retained. Do not treat retained as reclaimable or infer process exit from Task completion.
