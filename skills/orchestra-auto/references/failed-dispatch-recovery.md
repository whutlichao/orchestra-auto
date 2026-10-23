# `worker-start` returning `state: failed`: classify first, then act

**When to read this**: only when an `orchestration worker-start` receipt shows `state: failed`. The
normal path does not read it — reading it must not become a fixed cost on every dispatch.

**Why it is its own file**: it has six steps and costs roughly 700 tokens, while `state: failed` is
a low-frequency event. Keeping it in SKILL.md would mean paying that context cost every session for
something that rarely happens.

**The one sentence to remember**: "no relaunch" means **no unconditional retry**, not "the only
recovery is a retry". The classification below can legitimately end in retry, in fixing the setup,
in accepting an artifact that already exists, or in reporting and stopping.

---

## What the original case taught (each step below comes from it)

A receipt of this shape:

```text
failedStage: agent_readiness
lastError:   terminal_handle_stale
```

reads like "the terminal is gone, so re-dispatching is pointless". The evidence showed **both
terminals still present and the worktree already created**, with only the setup terminal
`status: exited` and `tail: []` (zero output). The readiness check judged before setup finished, so
the fault was in neither the ticket nor the configuration.

The lesson is narrower than it first looks. That combination — terminals present, worktree present,
setup exited with zero output — was **a signal to keep investigating**, not a proof of a transient
fault: zero output is equally consistent with a hook that never ran, a wrong working directory, or a
hook that died before printing. What settled it was the recovery guidance for that exact state plus
a setup run that demonstrably worked. Treating the three read-only observations as sufficient is how
this file used to send the reader down the configuration path — or into a blind re-dispatch.

---

## Procedure (six steps, order matters)

### 1. Read the receipt, then classify

```text
failedStage         Which stage failed
lastError           Error code
recovery            ← the current recovery guidance: read it before thinking
residualResources   Leftover resources; this decides whether cleanup comes first
```

Classify into exactly one class; the class, not the receipt's wording, decides the action:

| Class | Evidence that puts it here | Action |
| --- | --- | --- |
| Transient infrastructure | The failed stage is a readiness/connection stage, the exit information names a transient error (timeout, reset, busy, contended slot), an independent re-read now succeeds, and no half-done work exists | A re-dispatch is legitimate — after step 3 and step 5 |
| Setup fault | The setup command fails when it is actually run, or its exit information names a real error | Fix the setup cause; do not re-dispatch into a broken hook |
| Isolation / placement fault | The failed stage is worktree, branch or placement creation | Report it and take the documented fallback (serial implementers, or the per-ticket model); do not retry the same placement |
| Artifact already present | The worktree holds real uncommitted work, or the deliverable is already persisted | Not a failure: verify per G-impl item 5, then accept or send back |
| Authority / configuration fault | The error names an unconfigured or disabled agent, a missing permission or credential, or an unreadable setting | Escalate per §5 / §9; never substitute an agent and never borrow another identity |

The `recovery` field usually already contains the command to run for its state. Read it before
designing any retry scheme; the recovery reference is authoritative over this file when they differ.

### 2. Take evidence — read-only, and only what the class needs

```bash
<ORCA> terminal show --terminal <setup handle> --json   # are the terminals still there (orphaned)
git worktree list                                       # was the worktree really created
<ORCA> terminal read --terminal <setup handle> --json    # the setup terminal's status and tail
git -C <worktree> status --porcelain                     # is there real work to protect
```

What each observation does **and does not** prove:

- terminals present + worktree present + setup `exited` with `tail: []` ⇒ the readiness check saw no
  output. It does **not** by itself prove a transient fault or a ready hook; that needs step 3 and
  the recovery guidance for this state.
- A clean worktree proves no artifact is at risk, not that the setup ran.
- A non-empty worktree outranks the failure class: preserve and reconcile the work first.

### 3. Re-running setup: conditions, side effects, and who executes it

A setup re-run is a project action, so check all three before authorizing one:

- **Idempotent**: the hook can run a second time without duplicating effects (installs, migrations,
  seeds, generated files). If that cannot be shown, stop and report instead.
- **Authorized**: this round's scope covers what the hook does — network access, package installs,
  database migrations, external calls. Anything outside it is an escalation, not a retry.
- **Side-effect aware**: read the hook's commands first and record the state before the run (installed
  dependencies, generated files, migrations applied), so its effects can be attributed and any
  harmful one can be reported rather than discovered later.

**Who executes it**: the worker's own `--setup run` on a fresh dispatch, or the human when the hook
needs credentials or approval. The coordinator does not hand-run project setup inside a worker's
worktree — that worktree is the worker's, and a hook may write (§2). When a manual run is the only
way to answer the question, use a throwaway copy or a diagnostic worktree created for that purpose,
never the ticket's own worktree, and keep the ticket's work untouched.

It passes ⇒ the class is transient; a re-dispatch is safe. It fails ⇒ handle the real error; a
re-dispatch is not the fix.

### 4. Read the recovery reference

```bash
<ORCA> skills get orchestration --reference references/recovery-and-cleanup.md
```

Its state table says:

> `failed` or `stopped` → Start a replacement with `--retry-of`; repeat placement explicitly

So **re-dispatching is not forbidden; the posture matters**:

```bash
<ORCA> orchestration worker-start --task <task_id> \
  --retry-of <failed_dispatch_id> \
  --worktree <explicit selector> --agent <§0's worker> --json
```

Two constraints to respect:

- **`--retry-of` must be paired with `--task`.** Pairing it with `--spec` creates another Task,
  which is a different ticket.
- **Placement is not silently inherited.** Give `--worktree` explicitly (with `--repo` or
  `id:<repo-id>::<absolute worktree path>`); do not expect it to carry over from the failed
  Dispatch.

### 5. Preserve, then release — in that order

Capture the receipt, the terminal read output and the worktree status into the ledger first: a
release settles the Dispatch, and a settled Dispatch is no longer evidence you can re-read.
Reconcile any uncommitted work (back it up, confirm file by file what is real) before anything else;
never `checkout`, `reset` or clean a worktree that holds work you have not accounted for.

Then release the failed Dispatch:

```bash
<ORCA> orchestration worker-release --dispatch <failed_dispatch_id> --json
```

It never ran the Task, yet its terminals still occupy quota. **Release before re-dispatching**,
otherwise the new worker may fail to start on quota or waiter slots. The order cannot be reversed.
Release is not a cleanup reflex: for the artifact-present and authority-fault classes, releasing
before the artifact is reconciled or the question is escalated destroys the evidence you need.

### 6. After re-dispatching, confirm the turn really started per §4.3.1

A `ready` receipt still proves nothing — that holds for a first dispatch and for a re-dispatch
alike. `turnStart: observed` is the start evidence; otherwise read the title or screen per §4.3.1,
and send **one** bare Enter only when the complete preamble is parked in the composer with no turn
running.

---

## When to stop

- Step 3's setup run **fails** ⇒ not transient; investigate the real error, do not re-dispatch.
- The failure class is authority/configuration ⇒ escalate; a retry cannot fix a missing credential
  or a disabled agent, and substituting one is out of the coordinator's authority.
- The same ticket keeps returning `failed` ⇒ the per-ticket breaker of §6's send-back protocol is
  reached (two consecutive failures of the same gate), or it is time for §9's "report and stop". Do
  not loop on re-dispatching.
- The evidence shows **real uncommitted changes** in the worktree ⇒ that is not "failed", that is
  "half done". Confirm whether the deliverable is already persisted per §6 G-impl item 5, then
  decide between accepting and sending back.

## Related

- SKILL.md §4.3.1 — confirm the turn started after dispatch (applies to a re-dispatch too)
- [dispatch preflight](dispatch-preflight.md) §4.0.2 — the same start and isolation checks in
  checklist form
- SKILL.md §6 send-back protocol — per-ticket and run-level breakers
- SKILL.md §2 — no write ever lands on main, and no coordinator write lands in a worker's worktree
