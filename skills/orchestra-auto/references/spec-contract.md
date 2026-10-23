# The spec contract

**When to read this**: when you are about to write a worker's spec, on any channel and for any
ticket type.

### 4.4 The spec contract

The guide's five-element task-spec contract must be complete: **Target / Change / Constraints /
Ownership / Observable acceptance**. Plus a fixed first item:

```text
Pre-skill: <absolute path> — read it in full before starting, follow its procedure, do not skip.
```

**This line is the only entry point to the other skills.** Without it the worker never reads
`grill-with-docs` / `tdd` / `diagnosing-bugs` / `code-review`, and the "we didn't use the other
skills" failure reproduces itself. Put the path in the spec and let the worker read the whole
file; do not paraphrase it for them. **The path is the skill of the stage this round is actually
running** (SKILL.md §1), not a fixed one per channel: a round holding a valid spec names
`to-tickets`, one with a complete map names `implement-spec`, a bug fix names `diagnosing-bugs`
and `implement`. Confirm it resolves and is readable before writing it down; if it does not, report
that with evidence instead of inventing a path or installing something this round was not
authorized to install.

Template for an implementation ticket on channel B; write only the gates that apply. Rewrite
documentation tickets against their own documentation gates:

```text
【BEHAVIOR RULE (obey verbatim; do not paraphrase or trim)】
1. All questions go through orchestration ask; AskUserQuestion is banned. Open decisions are
   asked back, never invented.
2. You edit only the files and code inside this ticket's scope. On start, after every test run,
   before switching files, and before finishing, run check --terminal <your own real handle>
   --json with the live preamble's executable, read and act on coordinator follow-ups.
   Heartbeat at the preamble's cadence; on ask timeout resume the same message ID — do not ask
   twice and do not decide on a human's behalf.
   Closing order: check follow-ups → write the Answer and evidence → commit, push and create or
   update the PR for project changes → send worker_done from your own terminal using the live
   preamble's exact command → verify the success receipt → end the turn.
   worker_done must carry the real Task ID, Dispatch ID, an explicit succeeded/failed, and a
   three-sentence summary of changes, findings and what is left; files-modified/report-path only
   with real values. Failure must be reported too; a plain final is not a substitute.
   Keep the send receipt. When the outcome is unclear, inspect the current Dispatch first: a
   settled dispatch is never re-sent, and only a confirmed-unsettled, unqueued report may be
   retried once. On consumer_fenced, stop immediately: do not resend and do not use another
   agent's handle. If communication still fails, keep the artifacts and report the blockage
   explicitly. Once worker_done is accepted, idle immediately: check no further messages and
   start no new work; a task settled after that requires a fresh preamble.
   A follow-up inside an active Dispatch means an additional instruction in the mailbox — it
   creates no new Task and does not change the preamble.
3. Needing a human / credentials / a third party / a paid service / manual approval /
   irreversible live-data operations → escalation; do not act on the human's behalf.
4. Use deep-module vocabulary when describing design; follow the repository's language for
   existing terms in source, tests and reports.
5. Going beyond this spec's authorization → escalation; do not widen scope on your own.
6. Take red evidence on an isolated copy or a baseline worktree; back up and reconcile all
   uncommitted work before any restore or automatic formatting. Check `git status` and the diff
   before delivering; on an abnormally large diff, preserve evidence first and handle it file by
   file per the recovery reference. Configure a locally ignored formatter override before
   starting only when automatic reflow has been confirmed as a real risk in this repository.

Pre-skill: <absolute path> — read it in full before starting, follow its procedure, do not skip.
Target: <files/modules>
Change: <the concrete result to produce>
Constraints: <boundaries not to cross; use deep-module vocabulary for terms>
Ownership: <what this ticket may edit; the canonical ticket lives in the worktree and you own its status transitions and Answer appends>
Observable acceptance: <the applicable test commands and observable results; red→green evidence for fixes; the two-axis code-review section when needed. When project files change: parse and use the local pr skill to write the PR body, push the branch, create or update the PR with --body-file, and write the PR number and body path back into the Answer. Finally send and verify the worker_done receipt>
Question channel: orchestration ask (AskUserQuestion is banned).
```

For a long-horizon ticket map, `Pre-skill` is the absolute path to `implement-spec`. Add
`Skill resolution:` (use the Skill tool for `tdd` / `code-review` when available, otherwise read
their absolute paths), `PR skill:` (the absolute path to `pr`), `Tracker:`, `Spec:`, `Tickets:` and
`Integration branch:`. The worker must report the graph and frontier, each ticket's branch and
commit, the integration branch, the single integration PR, `Closes` links, and the Door claim with
evidence. Nested decision-class questions go up through the top-level worker; the worker must not
answer them on the human's behalf. Per-ticket TDD plus one `code-review` on the integration branch
is this path's review model; do not also schedule a per-ticket two-axis review.

**Ticket-type line: write the type, the gates and the test tier immediately after the pre-skill**:

```text
Type: research | design | spec | ticket splitting | implementation
Gates: <research = source and conclusion verification / design = G-docs / spec = G-spec / splitting = G-tickets / implementation = G-impl>
Test tier: narrow (affected tests + node glob) | wide (full suite)   ← required for implementation; narrow must state why the blast radius is bounded
Not run this ticket: <list the inapplicable source gates>   ← required for research/design/spec/splitting; fill in truthfully for implementation
```

**Why this line is mandatory**: the worker cannot see SKILL.md, so it treats exactly what the
spec says as its deliverable surface. With no ticket type in the spec it can only guess — and
the only general clue it holds about "gates" is the repository `CONTRIBUTING.md`'s
`Keep it green`, so a design ticket with zero source changes goes off and runs the whole suite.
The cost when this line is missing is concrete: a long harness-pool wait, one full run that hangs
far past any useful timeout, and false reds from binaries that were never installed — plus the
evidence files wasted on all of it.
Likewise, with no "not run" line the two-axis review becomes a universal requirement, and it
reviews the diff — a design ticket's diff is documentation, so the Standards axis can only pick
at the evidence directory (which is exactly where it reported "missing `pnpm test`" and goaded
the worker into the full run).

**The test tier is the same kind of line**: narrow-vs-wide is a decision the coordinator has already
made before dispatch (G-impl rule 1 and the pre-dispatch tick sheet), but the worker cannot see that
decision — only what the spec says. With no tier in the spec it falls back to "the full suite is
safest" or to picking a few tests itself; with a narrow tier but no reason, it cannot tell whether a
given test counts as "affected". So a narrow tier must carry one sentence on **why the blast radius
is bounded** (which layer changed, who depends on it, why shared interfaces, build configuration and
types are not reached), plus a selector the worker can run as written — `node glob` is the example,
not a requirement: a non-node repository names the equivalent test path or marker (for example
`python3 -m unittest tests.test_x`) instead of leaving the worker to assemble the command. If the
tier is wrong, the coordinator re-decides on send-back; the worker does not change the criterion on
the spot.

**Criterion**: decide the type before dispatch from Target / Change and the expected files, and
recheck it against the real diff once the worker finishes. If the real change crosses the type
boundary, escalate the scope question per §5; do not silently reclassify with a path regex.

A worker sees only the spec, never this skill. Write the applicable behaviour rules and the
actual delivery endpoint into the spec; item 6's local prevention applies only when its
condition holds, and the full disposition is in the recovery reference.
