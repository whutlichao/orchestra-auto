# The prose contract: six clauses that must not drift

**When to read**: during acceptance, when reviewing a change to either language bundle, or when
two documents appear to disagree about what may be concluded from a signal.

The rest of this skill is procedure. These six clauses are the load-bearing claims the procedure
rests on: each one names a conclusion the documents refuse, because a plausible alternative would
break a Run. They are repeated here so a reviewer — or a coordinator mid-run — can check all six
against a single page instead of reconstructing them from the reference files.

## The six clauses

1. **`absent` is not proof** of effect. A `request-show` reply of `absent` says the authority does
   not know the request key; it does not say the action never ran. Only positive absence in
   authority — no Dispatch for that Task, no PR for that head — permits "it did not execute".
   Inventory Orca and git before deciding. See [the coordination ledger](ledger.md).
2. **A dashboard failure never blocks dispatch.** The progress record, the registry and the
   browser are a sidecar. A failed write, a lock timeout, an unreachable old service or a viewer
   that cannot start is a ledger gap and a warning; dispatch continues on the same path. `open` is
   never on the dispatch critical path. See [progress record and dashboard](progress-dashboard.md).
3. **`worker_done` is not acceptance.** It proves the worker settled, nothing more. Acceptance
   needs the observable result plus evidence of the checks; a `grep` proves a string exists and
   nothing else, and a health state never authorizes a release.
4. **Release only after settlement and a closeout record.** A release requires a settled Dispatch
   and a durable closeout record: on success at least the acceptance records and the
   dispatch → task → attempt → worktree mapping, on failure at least a classification and evidence
   references. Never release an unsettled worker, whatever the health state says.
5. **`ready` is not a started turn.** `state: ready` means the Dispatch is ready, not that the
   agent's turn began. Accept `turnStart: observed` as the receipt's start evidence and gather the
   evidence the agent supports when it is `unsupported`, `unobserved` or absent.
6. **`unknown` is not failure.** Missing evidence, an unreadable authority, an expired TTL or an
   absent field is `unknown`: the next step is collecting evidence, not retrying the action. An
   empty wait is not an empty queue, and an unanswered question is not a refusal.

## What this page is not

The test that pins these clauses **checks anchors, not full semantic equivalence**: it lives in
`tests/test_package_content.py` and looks for each phrase in both bundles. That is a checklist, not
a translation: a translator can keep every anchor while drifting in the paragraph between them, so
a reviewer still reads the diff. The six clauses are also not proof that the surrounding procedure
is complete — only that the conclusions above are published, side by side, in both languages.
