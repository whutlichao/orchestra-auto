# Long-horizon tickets: implement-spec, one integration PR, door-aware merge

**When to read this**: G-tickets has accepted a complete ticket map and this round implements it;
you are about to dispatch or accept a long-horizon worker; you are deciding whether an integration
PR may auto-merge; or a process error suggests a retro.

## Contents

- [1. Entry condition: the map is complete](#1-entry-condition-the-map-is-complete)
- [2. Dispatch one long-horizon worker](#2-dispatch-one-long-horizon-worker)
- [3. Nested topology and worktree capability](#3-nested-topology-and-worktree-capability)
- [4. Coordinator acceptance](#4-coordinator-acceptance)
- [5. Door-aware merge](#5-door-aware-merge)
- [6. Retro on process errors](#6-retro-on-process-errors)
- [7. Fallbacks and stop conditions](#7-fallbacks-and-stop-conditions)

## 1. Entry condition: the map is complete

`implement-spec` takes a spec plus a task graph. Before dispatching it, all of these must hold:

- A spec pointer exists (the spec file or parent issue). If this round produced only tickets,
  synthesize the pointer from the parent issue or design document; if there is no spec and no
  parent, go back to `to-spec` rather than inventing one.
- Every ticket has a title, **What to build**, acceptance criteria and a status the tracker can
  close. The local-file tracker uses `.scratch/<feature>/issues/<NN>-<slug>.md`; a real tracker
  uses native blocking edges where it has them.
- Every blocker references an existing ticket. The graph is acyclic and at least one ticket is on
  the frontier. A dependency that points at a missing or closed-as-abandoned ticket is a broken
  map, not a frontier.
- The tracker is reachable: `docs/agents/issue-tracker.md` exists and the worker can use its
  command surface (`gh` / `glab` auth is live, or the local ticket files are visible from a
  worktree). A gitignored `.scratch` directory is absent from a fresh worktree, so expose it
  through an absolute path on the shared filesystem (or commit the ticket files).
- Each ticket fits one fresh context; prefactoring and expand–contract tickets come first. If a
  ticket is too large, send it back to `to-tickets`; do not let `implement-spec` discover that
  halfway through.

A single settled ticket stays on channel B. This path is for a map, not a wrapper around one
ticket.

## 2. Dispatch one long-horizon worker

Start one supervised worker for the whole map, not one worker per ticket:

```text
worker-start --spec "<long-horizon spec>" --worktree new-top-level \
  --name <feature-slug>-integration --setup run --agent <live agent> --json
```

The spec is a set of pointers, not a copy of the tickets. It must include:

```text
Pre-skill: <absolute>/implement-spec/SKILL.md — read in full before starting.
Skill resolution: use the Skill tool for `tdd` / `code-review` when available; otherwise read
<absolute>/tdd/SKILL.md and <absolute>/code-review/SKILL.md directly. Never paraphrase them.
PR skill: <absolute>/pr/SKILL.md — use it for the one integration PR body.
Tracker: <docs/agents/issue-tracker.md or absolute local ticket directory>.
Spec: <absolute path or issue URL>.
Tickets: <tracker query / local directory; name the frontier and the blocking edges>.
Integration branch: <name>; base: <default base ref>.
Concurrency: at most <N> implementers; default 3 unless the user or repo says otherwise.
```

The worker is an **orchestrator**. It must not implement tickets itself. It maintains the graph
and frontier, starts a fresh implementer per ready ticket, merges their branches through a merger
step, runs the final review and opens the PR. Context pointers to the spec, tickets, notes and
commits are the communication channel; do not restate the map in every prompt.

Every implementer reports its worktree, branch, commit, tests and what it merged from the
integration branch. Every decision-class question — scope, user-observable behaviour, an approach
fork, or one of the five escalations — goes up to the coordinator through the top-level worker.
The worker must not answer it on the human's behalf.

## 3. Nested topology and worktree capability

`implement-spec` assumes each implementer has its own worktree. That assumption needs proof before
the worker spends the map on it. The worker's first report must show:

1. it can create an isolated worktree (`git worktree add` or `orca worktree create`) and a branch
   cut from the integration branch;
2. it can run implementers in the background, or will run them serially;
3. it can merge branches without editing the main working copy.

If isolation works, run one implementer per frontier ticket in parallel up to the cap. Each
implementer confirms its worktree is based on the integration branch, calls `tdd`, merges the
integration tip into its branch before reporting done, and leaves the branch clean. A merger
subagent — or the worker itself when the harness has no subagents — merges completed branches into
the integration branch in DAG order and re-runs the affected tests after each merge.

If isolation cannot be proved, **do not let two implementers share a worktree**. Run them serially,
one ticket at a time, in the worker's own worktree, and record the degradation. If the worker
cannot run subagents at all, fall back to the per-ticket Orca worker model in §7 and say why in the
report.

If the implementers are Orca nested workers, respect `nested_worker_depth_exceeded`; creating
another Run does not reset the depth. If they are harness subagents, they carry no Orca lifecycle:
the top-level worker remains accountable for their worktrees, branches and output.

## 4. Coordinator acceptance

The coordinator accepts the **integration PR**, not each nested branch. Four things are reported
separately, because collapsing them is where a merge gets claimed as a close-out:

1. **Ticket implementation complete** — each ticket's branch is merged into the integration branch
   and its acceptance criteria are demonstrated. Nothing is left only in a worktree.
2. **Integration accepted** — the final tree passes the applicable full gate, and a spot-check of at
   least two tickets' evidence confirms what the summary claims.
3. **PR merged** — the integration PR is open with its head matching the final accepted SHA, and the
   integration branch was rebased onto the current default base before it was marked ready; it is
   merged only through §5's door rules.
4. **Tickets closed per tracker** — the tracker's own rule closes them, usually from the PR body's
   `Closes` links when that PR merges. Closure is therefore *after* the merge, and reconciling it
   is a post-merge step (§6.5 of [gate acceptance](gate-acceptance.md)), not a precondition.

Check:

- The PR is one PR for the map (or the documented fallback), its head matches the final accepted
  SHA, and the integration branch was rebased onto the current default base before the PR was
  marked ready.
- Every ticket's branch is merged; no ticket branch is left only in a worktree. The PR body carries
  `Closes` links for the spec and the tickets. At acceptance the tickets may still be **open** —
  the `Closes` links are what close them when the PR merges, so requiring every ticket to be closed
  before the merge would deadlock a correct run. What acceptance requires is that every ticket's
  work is in the branch and that each will close; what it forbids is reporting a ticket as closed
  while the PR is only open.
- The final tree passes the applicable full gate. Spot-check at least two tickets' evidence —
  their tests and their acceptance criteria — rather than trusting the summary alone. Per-ticket
  TDD is the implementer's contract; the one two-axis `code-review` on the integration branch is
  this path's review gate. Record that choice in the report.
- The worker cleaned up nested worktrees; the main working copy is untouched; there is no
  unexplained uncommitted change.

On a missing branch, a red test, a ticket whose work is absent or a stale head, send the finding back
to the top-level worker. The coordinator does not enter nested worktrees or finish a ticket itself.

## 5. Door-aware merge

The worker writes the PR body with the `pr` skill, including `Door`, `Blast Radius` and evidence.
The coordinator then verifies the door claim against the real diff. The worker's label is input,
not proof.

**The one-way trigger list lives in [gate acceptance](gate-acceptance.md) G-merge step 0** — read it
there instead of keeping a second copy here; any single trigger makes the whole PR one-way, an
additive backward-compatible addition is not a trigger by itself, and an uncertain claim is treated
as one-way. This section adds only what is specific to a long-horizon map: the door decides whether
the **integration PR** may merge at all, and the ticket reconciliation below follows from that.

- **Two-way + G-merge green + repository allows** → auto-merge, sync main, then reconcile the
  tickets against the tracker: confirm the `Closes` links actually closed what they named, and
  record anything the tracker left open (and why) in the report.
- **One-way, uncertain, a gate red, or a rule blocks** → stop at the open PR. Report the door, the
  evidence, the blast radius and the exact human merge action. Do not merge it, do not sync main,
  and do not ask for a second automatic approval. The tickets stay **open**: the PR is open, not
  merged, so nothing is closed and no ticket status may claim otherwise. A human merges it
  manually; the run's endpoint is the open PR, and reconciling the tickets happens after that merge.

## 6. Retro on process errors

A product bug is a send-back. A **process error** is a defect in the harness that could recur:
a worker bypassing the role boundary, a gate that accepted a red result, a dispatch that failed
twice for the same reason, a wake that stalled, a wrong ticket type, scope drift, a
merge-door misclassification, or repeated send-backs on the same gate.

When one occurs, preserve the evidence and continue the round when safe. At a safe point, run
`retro` read-only: read its skill file, then read the primary sources — the session, worker
transcripts, `git`/PR evidence, the ledger and the gate results. Because `retro` is normally
user-invoked, treat this round's request for the flow as authorization for the read-only analysis
only; it never authorizes landing. If the Skill tool is unavailable, read the `writing-for-agents`
skill file directly. Save `retro-candidates.md` **outside** the target repository, ordered by
severity. Each candidate names its category (navigation, automated checks, coding standards,
AGENTS.md, tool economy, no-ops, information access), the evidence, the exact file or check to
change, the verification, and the risk.

Present the candidates to the human. Do not land one automatically. When the human selects
candidates, dispatch a worker to land them under the normal gates: for this skill repository that
means both language variants, matching eval cases, tests and README; for a target repository it
means that repository's conventions. Use the `pr` skill for the PR, and add one eval case per
accepted candidate so the defect cannot silently return. If the round is still running, landing
improvements waits until the round's endpoint; retro never edits the skill mid-run.

## 7. Fallbacks and stop conditions

| Condition | Action |
| --- | --- |
| The map is incomplete (cycle, missing blocker, no acceptance, tracker unreachable) | Send back to G-tickets / `to-tickets`; dispatch nothing. |
| Only a single settled ticket | Channel B; one worker in one worktree. |
| The worker cannot prove isolated worktrees | Serial implementers in its own worktree; record the degradation. |
| The worker cannot run subagents | Per-ticket Orca workers, default cap 3; record the fallback. |
| The integration PR is one-way | Leave it open; a human merges it. |
| Nested worktrees remain or main was touched | Preserve the scene, send back, do not accept. |
| The worker stalls or loses contact | Follow the worker-start / inbox recovery references; do not dispatch a duplicate editor. |
