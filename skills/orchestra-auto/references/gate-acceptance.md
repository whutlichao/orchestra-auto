# Gate acceptance, merge execution and worker lifecycle

**When to read this**: when a ticket is closing, to pick and run the gate its type calls for; and
again when merging a project PR or settling a worker's lifecycle.

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
