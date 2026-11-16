# Long-Horizon Ticket Maps & Multi-Ticket Integration

**When to read**: when `G-tickets` accepted a multi-ticket map (`implement-spec`), or when tracking integration PR delivery across multiple dependent tickets.

## 1. Integration Branch Model

Start **one** coordinator-supervised top-level worker (`--worktree new-top-level --name <feature>-integration`) with `Pre-skill: implement-spec` (plus resolved `tdd`, `code-review`, `pr` skill paths, `Tracker`, `Spec`, `Tickets`, and `Integration branch` pointers).

- Before spending tickets, the worker's first report must prove nested isolation (creating clean branch/worktree off the integration branch and merging without touching `main`).
- If nested isolation fails, degrade explicitly in the ledger to serial implementers inside its own worktree or per-ticket Orca workers (default cap `3`). Two implementers must never share one worktree.
- The coordinator accepts the single **integration PR** (`Closes` links for spec and tickets), never enters nested worktrees directly, and spot-checks at least two tickets' test/acceptance evidence instead of rubber-stamping a summary. Review model: per-ticket TDD + **one** two-axis `code-review` on the integration branch (do not add per-ticket dual reviews).

## 2. Four Separate Acceptance Levels

Report the four completion levels independently — never merge them into one claim:

1. **Ticket implementation complete** (`implemented`) — ticket's own TDD/checks pass in its branch.
2. **Integration accepted** (`integrated`) — branch merged into integration branch and combined suite passes.
3. **PR merged** (`merged`) — integration PR verified `MERGED` via PR host + git ancestry/equivalence.
4. **Tickets closed per tracker** (`tracker_closed`) — read back from tracker after merge.

Do not stall acceptance by **requiring every ticket to be closed before the merge** (when `Closes` links close them on merge, tickets stay open at PR creation), and never create a false close-out by **reporting a ticket as closed while the PR is only open**. Reconcile tracker state after the merge per [acceptance and closeout](acceptance-and-closeout.md).

### 4.1 Settlement, `pending` One-Way Doors, and Release

Close long-horizon runs through [acceptance and closeout](acceptance-and-closeout.md) §6.7 (`closeout.py`):

- **Pending one-way doors stay visible without a coordinator turn**: an open one-way integration PR records `pending` (`pending_type: human_merge`). `ledger.py show --run-id "$RUN" --kind pending` and the dashboard expose it until a later turn reconciles PR host `MERGED`, merge SHA, synced local main, and closed tracker state.
- **Release comes after settlement and a durable closeout**: settled `worker-show` (never `ready` and never health state) plus a persisted closeout record (mapping + acceptance on success, classification + evidence on failure) are mandatory before `worker-release`. On `release_unknown`, read back `worker-show` and `worker-list`; an unsettled worker is never released. Unwritable closeout records persist to `closeout-pending/` and block release until retried.

## 3. Door-Aware Merge & `retro` Governance

- For authoritative one-way-door triggers, read G-merge Step 0 in [acceptance-and-closeout](acceptance-and-closeout.md) rather than duplicating triggers here. Two-way + authorized + all green → auto-merge integration PR, sync `main`, reconcile tracker. One-way / uncertain / PR-only request → stop with integration PR open and report exact human merge steps (tickets stay open).
- On recurring harness defects (wrong gate pass, boundary bypass, missed wake), run `retro` read-only at a safe point, write `retro-candidates.md` outside the repo, and let the human approve candidates before dispatching fixes via normal gates + eval scenarios (never auto-edit the skill mid-run).

| Condition | Action |
| :--- | :--- |
| Incomplete map / unreachable tracker | Send back to `to-tickets`; dispatch nothing |
| Worker cannot isolate nested worktrees / run subagents | Degrade to serial or per-ticket Orca workers (cap 3) and record gap |
| Integration PR is one-way or uncertain | Leave open (`pending_type: human_merge`); human merges manually |
| Uncleaned nested worktrees or edited `main` | Preserve scene, send back, do not accept |
