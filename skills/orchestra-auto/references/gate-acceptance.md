# Gate acceptance, merge execution and worker lifecycle

**When to read this**: when a ticket is closing, to pick and run the gate its type calls for; and
again when merging a project PR or settling a worker's lifecycle.

## Contents

- [Choosing gates by ticket type](#choosing-gates-by-ticket-type)
- [G-impl — implementation tickets](#g-impl--implementation-tickets)
- [G-merge — door-aware merge gate for project PRs](#g-merge--door-aware-merge-gate-for-project-prs)
- [§6.5 Merge execution and teardown](#65-merge-execution-and-teardown)
- [§6.6 Acceptance records and the four levels](#66-acceptance-records-and-the-four-levels)
- [§6.7 Closeout: the checklist, pending one-way doors and the release gate](#67-closeout-the-checklist-pending-one-way-doors-and-the-release-gate)
- [Worker lifecycle and send-backs](#worker-lifecycle-and-send-backs)

## 6. Gate acceptance

The coordinator verifies observable results and evidence itself, and on failure lists what is
missing and sends it back. UI and runtime changes are exercised for real in an isolated
environment with isolated data per the repository's browser discipline, not accepted on a test's
say-so. Conclusions and key numbers go into §7's durable report; process logs may live in a
temporary evidence directory, and the report must not depend on that directory surviving.

### Choosing gates by ticket type

| Ticket type | Gate | Acceptance focus |
| --- | --- | --- |
| Design / wayfinder | G-docs / G-wayfinder | Decisions, terminology, frontier, document structure |
| Research | Source and conclusion verification | Traceable sources, evidence-backed conclusions, stated limits and delivery endpoint |
| Spec | G-spec | User stories, seams, scope, tracker state |
| Ticket splitting | G-tickets | Vertical slices, dependencies, each ticket acceptable on its own |
| Implementation | G-impl | Changed behaviour, tests, quality, deliverables |
| Automatic merge of project changes | G-merge | PR, CI, combined result, merge safety |

Documentation and planning tickets run only the applicable gates; with no source behaviour change
there is no obligation to run source tests or a two-axis source review. The full documentation
gates are in [planning gates](gates-docs-planning.md) — read only the relevant section
when that kind of ticket closes. Decide the type before dispatch from the expected Target /
Change and recheck it against the real diff afterwards; on a boundary crossing, handle the scope
question per §5.

### G-impl — implementation tickets

0. **The scene**: read `git status --porcelain`, the changed-file list and `git diff --numstat`.
   Uncommitted content must be explained; on an abnormally large diff, protect the real work
   first and investigate per [formatter recovery](reformat-noise-recovery.md) rather
   than reverting outright.
1. **Behaviour and tests**: run the repository's existing tests as the blast radius dictates. A
   single ticket with a bounded radius runs the affected tests; a shared interface, build
   configuration, types, or an unclear radius runs the full suite. Do not run the full suite
   repeatedly locally on the same code: the final head is settled by CI, or by one local full run
   when CI cannot cover it. A fix's test should fail before the fix and pass after it; new
   behaviour leaves observable red→green evidence. Pure copy, style or configuration changes prove
   the result with real rendering, a check, or an applicable test. A UI ticket's evidence must land
   on text, elements, accessible names or real browser behaviour produced by the product.
   Check that the spec's `Test tier:` line matches what actually ran: a narrow tier's selector,
   its results and the stated reason must line up. Running narrower than declared (just one picked
   test) or running the full suite on every round (slow, unexplained) is a send-back; a
   re-decision is the coordinator rewriting that line. The tier scopes iteration, it does not
   exempt the final head from CI or the one local full run above.
2. **Quality**: run the static checks, lint and format gates the project actually has, as the
   repository's own configuration defines them — a TS typecheck, Python's mypy/ruff, `go vet`,
   Rust's clippy/fmt are examples by language, not a required checklist. When the repository has no
   gate of some kind, do not invent one: state "this project has no such gate" in the report rather
   than raising a false red over a missing tool. If the repository has pre-existing problems,
   obtain a comparable baseline on **this ticket's** base commit and report the number of newly
   introduced problems; a log from another branch is not this ticket's baseline.
3. **Review and coverage**: the Answer gives execution evidence for the pre-skill. When a
   two-axis code review is required, give the Standards and Spec conclusions separately, with
   findings and their disposition; the small-ticket exemption in §3 is allowed only when the
   spec records the reason. When a field, prop or parameter changes source, enumerate every
   affected call site and state for each whether it changed or stayed, and why. Keep same-class
   operations within a ticket consistent, and justify any difference with something checkable.
4. **Task state**: the task's status must match the real degree of completion; the `worker_done`
   receipt only proves the lifecycle settled and does not replace artifact acceptance. If a
   message is lost, reconcile the Dispatch with the real artifacts rather than dispatching a
   second editor.
5. **Deliverables**: when project files changed, verify the worktree is clean, the commit exists
   and the branch is pushed; verify the OPEN PR's head matches the ticket's final commit and its
   body states Summary, Evidence, Merge Danger and `Closes` accurately; the worker uses the `pr`
   skill or the repository template with `--body-file`. For a long-horizon map, the deliverable is
   one integration PR: verify every ticket branch is merged and spot-check at least two tickets'
   evidence; per-ticket TDD plus the final integration `code-review` replaces per-ticket two-axis
   reviews, and the report records that choice. If the PR is missing or the body lacks evidence,
   send it back; the coordinator does not write it.

### G-merge — door-aware merge gate for project PRs

0. **Door check**: read the PR body's `Door` and `Blast Radius`, then verify the claim against the
   diff yourself. **This list is the single authoritative definition of a one-way door**; every
   other document points here instead of restating it. One-way triggers: schema/data migration or
   deletion; breaking public API/CLI/SDK change (removal, rename, or a changed
   signature/semantics/default) or a persisted format change — a purely additive,
   backward-compatible addition is not itself one-way; auth/permissions/secrets; billing/pricing;
   deploy/infra; irreversible external side effect; feature removal; major dependency upgrade; a
   broad blast radius with no simple revert. Any one makes the whole PR one-way; when unsure, treat
   it as one-way. A two-way claim contradicted by the diff goes back to the worker to correct the
   body and is then treated as one-way. A one-way PR stops here: leave it open and report the exact
   human merge action. Never auto-merge a one-way door.

Check the repository's conventions for CI and merging first. Separate known baseline failures
from real failures this ticket introduced. For several PRs, an integration worker composes the
final heads in an isolated worktree in the merge method and DAG order you intend to use, recording
the base, each head SHA and every step's tree SHA; the coordinator checks the combined result and
runs the applicable full gate on the final tree — CI by default, and only when the repository has
no usable CI, one equivalent local full run instead. A single PR gets the same verification on its
final head. Fix a PR first on a combined conflict or a real red; once any head or the target base
changes, re-judge whether the earlier evidence still covers what you intend to merge. For a two-way
PR, all gates green means §6.5's automatic merge, with no further user confirmation; when the user
explicitly asked to keep an open PR, stop here.

### §6.5 Merge execution and teardown

1. In DAG or stacked-PR order, check the current head against the accepted SHA and merge each one
   with the method the repository allows. Merge conflicts go back to that ticket's worker to
   resolve in its worktree, and the affected gates plus G-merge are re-run.
2. After each merge run §6.5.1 and check the main branch's tree SHA against the verified prefix.
   Pause and update the integration evidence only on an unplanned main-branch commit, a different
   merge result, or a changed head still awaiting merge.
3. After all merges, confirm the final main branch contains the expected commits, do a quick
   check and record the SHA. On an anomaly, preserve the scene and dispatch a fix ticket rather
   than writing a merged state as a passed acceptance.
4. **Reconcile the tickets against the tracker after the merge.** A merged PR is not a closed
   ticket: the tracker closes work by its own rule (usually the `Closes` links firing on merge, and
   sometimes only by hand). Read the ticket states back and record, per ticket, whether it closed,
   and for any that did not, what it is waiting on. Claiming a ticket closed because the PR merged,
   or because the PR is open, is exactly the false close-out this step exists to catch; a
   long-horizon map's ticket table in the report carries the four states — implemented, integrated,
   merged, tracker-closed — separately.
5. Close out at the stage matching this round's scope; merging a design PR does not by itself
   authorize creating implementation tickets. A one-way PR is not in this merge loop: the human
   merges it manually, the tickets stay open until that merge, and a later turn records the merge
   SHA, syncs main and reconciles the tickets.

#### §6.5.1 Syncing local main after each PR merge

Record the PR's MERGED state, its base and the merge SHA. Run `git pull --ff-only` only after
confirming the main working copy sits on the target main branch, that the working tree and index
are clean, and that the remote and upstream are explicit. Then verify with
`git merge-base --is-ancestor <merge_sha> HEAD` and record the local HEAD. When a condition does
not hold, preserve the scene and report "merged remotely, local sync blocked", pausing later
merges; do not auto-stash, reset, check out, or create a local merge commit. A network error may
be retried once after diagnosis.

### §6.6 Acceptance records and the four levels

A gate result is durable or it did not happen. Record it as a ledger `kind: acceptance` record
before you release the worker or tell anyone the gate passed; a failed write is reported as a
failed write, never as a pass, and is retried with the same `record_id`.

```bash
ACCEPTANCE="$SKILLS_ROOT/orchestra-auto/scripts/acceptance.py"
python3 "$ACCEPTANCE" record --run-id "$RUN" --record "$ACCEPTANCE_JSON"
python3 "$ACCEPTANCE" show --run-id "$RUN" --ticket 39
```

One record per PR carries `ticket`, `gate`, `attempt`, `accepted_sha`, `accepted_at`,
`pr {number, head_at_acceptance, base}`, `checks [{name, command, result, evidence_ref}]`,
`review {axis[], result, evidence_ref}`, the four `levels`, `decided_by` (`coordinator` or
`human`), `decided_at` and `answer_location`. `validate(record)` rejects a level that is `true`
without an `evidence_ref`; the CLI validates before it writes and exits non-zero with the error
list when it refuses. `--authority FILE` additionally cross-checks claimed levels against a
recorded PR-host/tracker/git read.

**Minimum evidence.** A claim without its evidence is a send-back, not a nuance.

| Claim | What proves it |
| --- | --- |
| merge | PR host `MERGED`, the merge SHA, and `git merge-base --is-ancestor <accepted_sha> <merge_sha>` |
| tracker close | a tracker read-back showing `closed`, with the read time |
| start | `turnStart: observed`, or the attempt's artifact / status-projection reference |
| tests | the command, the result and the log reference. A `grep` only proves a string exists, so it is never a test run |
| review | both axes (`standards`, `spec`) and the review reference; a waiver records its reason in the spec **and** the Answer |

`worker_done` proves settlement only. It is never start evidence and never produces an
acceptance: a level made `true` on it is rejected.

**Four levels.** `implemented`, `integrated`, `merged` and `tracker_closed` are separate claims,
recorded per PR. The ticket-level level is true only when **every** linked PR record supports
it — a ticket may legitimately span several PRs, and one weak PR lowers the ticket's level
rather than being averaged away. Two records that disagree about the same PR are `divergent`,
and no level is claimed for that PR. `never_merge` is a terminal state for the merged level and
belongs to a human: `{"value": false, "terminal": "never_merge", "decided_by": "human"}` with a
reason, so a spike stops looking incomplete. A revert or a reopen **appends** an observation
(`acceptance.py observe`) that sets the affected level false and keeps the original evidence as
history; nothing is overwritten.

**Pending, not frozen.** The four-level questions in issue #20 are open. Record them as ledger
`pending` items (`acceptance.py open-questions`) with their evidence, keep the affected
semantics `provisional`, and close the ticket without waiting for a human reply. A later human
decision appends an observation (`resolve-question`); it never rewrites the earlier records.

### §6.7 Closeout: the checklist, pending one-way doors and the release gate

A closeout is a reconciliation, not a memory. `scripts/closeout.py` derives it from authority:
`plan(facts)` is a pure planner over authority facts, and `reconcile` reads authority through
adapters and persists one `kind: closeout` ledger record. Storage is `ledger.py`, the acceptance
schema is `acceptance.py`, and the release decision consumes `health.authorize_release` — none of
them is re-implemented.

```bash
CLOSEOUT="$SKILLS_ROOT/orchestra-auto/scripts/closeout.py"
python3 "$CLOSEOUT" plan --run-id "$RUN" --facts facts.json
python3 "$CLOSEOUT" reconcile --run-id "$RUN" --authority recorded.json \
  --answer-location "$ANSWER"
python3 "$CLOSEOUT" show --run-id "$RUN"
```

**Ten rows, each with its authority, repeatability and action.**

| # | Fact | Authority | Repeatable | Action |
| --- | --- | --- | --- | --- |
| 1 | the accepted SHA per PR | ledger acceptance record | **no** | must be persisted at acceptance; it cannot be reconstructed later |
| 2 | PR `MERGED`, base and merge SHA | PR host API | yes | read back and write an observation |
| 3 | the merge covers the accepted head | `git merge-base --is-ancestor` | yes | failure → divergent, escalate |
| 4 | main contains the merge | `git rev-parse` / ancestry | yes | record the ancestry result |
| 5 | local main is synced | `git status` / `rev-parse` / upstream, ff-only | yes | blocked → pause later merges |
| 6 | every ticket is integrated | integration branch ancestry / PR `Closes` | yes | record the result per ticket |
| 7 | tracker state | tracker read-back | yes | record the close or reopen |
| 8 | worker lifecycle | `worker-show` / `worker-list` / release receipt | yes | enumerate reclaimable workers |
| 9 | dashboard projection | `progress.json` | yes (non-authoritative) | gap only; never evidence |
| 10 | one-way-door pending | ledger `pending` | yes | check again when due |

Only row 1 is non-repeatable; the plan reports it as `non_repeatable`. A fact that cannot be
read is `unknown`, never a silent pass, and an unreadable authority blocks the declaration just
like a failed check. Row 5 **blocks later merges** while local main is unsynced (`merge_blocked`)
rather than only lowering the checklist. Row 8 enumerates every reclaimable or
`release_unknown` worker, and names those that need `worker-show` read-back first. Row 9 records
a gap and nothing else: `progress.json` is a projection, so a dashboard failure or a stale
projection never blocks a closeout and is never cited as evidence.

**A closeout is a point-in-time reconciliation.** The ten rows together are one snapshot;
that a snapshot differs from the previous one is not itself a failure to be reconciled.

**Pending one-way doors stay visible without a coordinator turn.** A human merge records a ledger
`pending` item (`pending_type: human_merge`, `pr`, `accepted_sha`, `owner`, `condition`,
`check_after`). No turn is needed for it to be seen: `ledger.py show --run-id "$RUN" --kind
pending` and `ledger.py summary` list it, and the dashboard shows "awaiting human merge". The
next turn reconciles it against the PR host, the merge SHA, main sync and the tracker; only when
all four are true does `reconcile` append one resolution observation and stop blocking. A
pending item whose checks are still false stays visible with the check that failed. Row 10 gates
**one-way-door** pending items only; the other pending items (the open four-level questions, for
example) stay visible in the ledger but do not block a declaration, because the spec lets those
close without a human reply.

**Release precondition.** Release requires a settled Dispatch, not `ready` and not health, and a
persisted closeout record: on success at least the acceptance records plus the
dispatch → task → attempt → worktree mapping, on failure at least a failure classification and
its evidence references. `release_unknown` first reads `worker-show` and `worker-list`; an
unsettled worker is never released, whatever the health state says. A closeout record that could
not be written is **not** a pass: release and the gate declaration wait until the write is
repaired, and the undurable record is kept at `closeout-pending/` for the retry.

**Crash recovery.** The order is Orca → ledger → snapshot; the projection is a clue, not
authority. A crash between the merge and the tracker reconciliation is repaired from rows 2, 3,
4 and 7: the PR host still reports `MERGED` with the merge SHA, git still proves ancestry and
containment, and the tracker's own read-back says whether the ticket closed. The missing facts
are recorded as a failure closeout with its classification and evidence references rather than
being filled in from memory.

**Revert and reopen consume the T09 observation.** A revert or reopen appends an observation
once (`acceptance.py observe`); the closeout consumes it and lowers rows 2/3/4 (a revert) or row
7 (a reopen). The closeout never appends a second observation for the same fact.

**Tracker close is read back.** When a merge keyword closes the ticket, the closeout reads the
state back and records it. When it does not, the coordinator closes it only within existing
authorization; an exception escalates instead of becoming an unrestricted tracker write.

**The record carries the Answer location.** Every closeout record stores `answer_location` (and
each accepted record's own location on row 1), so a later attempt finds the Answer instead of
re-deriving it.

**Release replay safety is version-scoped.** Release replay semantics beyond the current CLI help
are an assumption. The closeout records the verification source and the timestamp it was
verified at, together with the version scope; no unverified release field is written as a stable
contract.

### Worker lifecycle and send-backs

Handle every settled Dispatch under the current Orca completion accounting: dispatch a follow-up,
retain it when the user asks, or release it once the artifacts are persisted and release will not
break an open PR. By default release after the merge and sync complete; when the user asked for
an open PR only, handle it per current Orca semantics once the remote branch is confirmed safe.
Query reclaimable before finishing and either settle every item or record why it is blocked.

A send-back states which gate failed, what evidence is missing, and exactly what needs adding.
For a settled worker, use a new Task for the follow-up and, when reusing the original terminal,
name the original worktree selector explicitly; a new turn re-runs §4.3.1. When the same gate
fails twice in a row on one ticket, pause that ticket and bring the user a differential analysis.
When send-backs reach 5 across tickets, or there is no substantive progress for a long stretch,
stop dispatching and report the current state; wind down the remaining in-flight work safely
using the available evidence.

Progress dashboard: initialize and open it before the first dispatch of every actual Run, then follow
the checkpoint ownership table in [progress dashboard](progress-dashboard.md) after the normal action.
Record explicit facts and structured task/dispatch/attempt reports; recording errors warn and never
block this workflow.
