# The spec contract

**When to read this**: when you are about to write a worker's spec, on any channel and for any
ticket type; or when you must hand a planning stage to a fresh context and need to know what the
handoff has to carry.

## Contents

- [4.4 The spec contract](#44-the-spec-contract)
- [Planning context and handoff](#planning-context-and-handoff)

### 4.4 The spec contract

The guide's five-element task-spec contract must be complete: **Target / Change / Constraints /
Ownership / Observable acceptance**. Plus a fixed first item:

```text
Pre-skill:
- Name: <skill for this stage>
- Explicit invocation: <form supported by this agent; write unverified if unknown>
- File location: <actual absolute path>/SKILL.md
Skill execution: This task explicitly requires this skill. Prefer explicit invocation supported by the host; record failures or unsupported invocation. Read the file and follow it as a fallback only when host rules allow it; if the host forbids alternative execution, stop and report the blockage. Follow the skill's requirements for supporting files. Do not report direct reading as native invocation.
```

**This block hands the stage skill to the worker.** Choose the skill for the actual stage, not the channel; for a bug fix name both `diagnosing-bugs` and `implement`. Confirm each path resolves and is readable before writing the spec. Specify an invocation form only when supported by the selected agent; otherwise mark it unverified. Do not assume a spec-internal command is parsed as a native invocation. Report missing paths with evidence rather than inventing paths or installing without authorization. Let the worker load the full instructions, not a paraphrase.

Template for an implementation ticket on channel B; write only the gates that apply. Rewrite
documentation tickets against their own documentation gates:

```text
【BEHAVIOR RULE (obey verbatim; do not paraphrase or trim)】
1. All questions go through orchestration ask; AskUserQuestion is banned. Open decisions are
   asked back, never invented.
2. You edit only the files and code inside this ticket's scope. Follow the live preamble for native messages and any requested check.
   Heartbeat only if the preamble requires it; on ask timeout resume the same message ID — do not ask
   twice and do not decide on a human's behalf.
   Closing order: handle delivered follow-ups → write the Answer and evidence → commit, push and create or
   update the PR for project changes → record the PR number, the PR body path and the CI reading
   for the PR head in GitHub and this run's out-of-repo ledger; no project commit is added to
   carry those facts, and the deliverable files are already final before the PR is created →
   send worker_done from your own terminal using the live preamble's exact command → verify the
   success receipt → end the turn.
   Those facts exist only after the PR does, so a commit to carry them can only be a second push
   whose tree is identical to the accepted one — no information gained, one more full CI round.
   The ticket's in-repo Answer therefore reports what was delivered and the branch it sits on; the
   PR reference lives on GitHub and in the ledger. The final PR HEAD's CI remains the
   coordinator's check before the merge, and the worker does not push to chase it. When the user
   or the repository actually requires those facts inside the tree — a changelog entry naming the
   PR, a release manifest, a tracker field, an Answer that must name it — follow that rule instead.
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

Pre-skill:
- Name: <skill for this stage>
- Explicit invocation: <form supported by this agent; write unverified if unknown>
- File location: <actual absolute path>/SKILL.md
Skill execution: This task explicitly requires this skill. Prefer explicit invocation supported by the host; record failures or unsupported invocation. Read the file and follow it as a fallback only when host rules allow it; if the host forbids alternative execution, stop and report the blockage. Follow the skill's requirements for supporting files. Do not report direct reading as native invocation.
Target: <files/modules>
Change: <the concrete result to produce>
Constraints: <boundaries not to cross; use deep-module vocabulary for terms>
Ownership: <what this ticket may edit; the canonical ticket lives in the worktree and you own its status transitions and Answer appends>
Observable acceptance: <the applicable test commands and observable results; red→green evidence for fixes; the two-axis code-review section when needed. When project files change: parse and use the local pr skill to write the PR body, push the branch, create or update the PR with --body-file, then record the PR number, the body path and the CI reading in GitHub and the out-of-repo ledger with no project commit added for them (follow the user's or the repository's own rule when it explicitly requires those facts in the tree). Writing the PR target and base into the spec is part of this line: name the repository the PR lands in and the base branch there — for a fork that is the upstream repository's default branch, never the fork's own main — and do not leave either to gh's default resolution; the shape is `gh pr create --repo <upstream-owner/repo> --base <default branch> --head <fork-owner>:<branch>`. Finally send and verify the worker_done receipt>
Skill execution evidence: <name and path; method: native invocation / direct read / not loaded; verifiable invocation or read record, or failure reason; key steps and artifacts>
Question channel: orchestration ask (AskUserQuestion is banned).
```

When the ticket needs Orca-rendered screenshots, add `Visual evidence: <plan path> → scripts/visual-evidence.py run --plan <plan> --report <report>` to the spec, forbid minimizing the window and `set frontmost`, and require the report's `restored` and `focusRestored` to be true; the protocol is [visual evidence](visual-evidence.md).

For a long-horizon ticket map, the `Pre-skill` block specifies `implement-spec`. Add
`Skill resolution:` (name `tdd` / `code-review`, their invocation forms and absolute paths, applying the same invocation and fallback rules), `PR skill:` (the absolute path to `pr`), `Tracker:`, `Spec:`, `Tickets:` and
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

## Planning context and handoff

The preferred path keeps grill, `to-spec` and `to-tickets` in **one planning context**, so the
spec and the ticket split build on the same conversation. Split only when the work itself needs
it, the host's context is degrading, or the user's chosen topology requires isolation. A worker per
stage is this project's supervision choice, not a requirement of the planning skills; when a
stage runs in a fresh context, the handoff below is what keeps it faithful.

**Three different documents, not one.** A *worker task spec* is the self-contained delegation
described in §4.4 — it says what this dispatch must produce. A *product spec* is the artifact
`to-spec` publishes; a worker that is asked to write one receives task spec and product spec
separately. A *planning handoff* is the record that moves a stage across contexts. None of them
substitutes for another, and none of them proves a decision was made.

**The handoff record.** When a stage moves to a fresh context — across harnesses, directories,
collaborators, or mid-phase forks — write the following and point at it from the new context. The
record lives outside the repository; the pointer lives where the work continues.

```text
Handoff: <one-line goal> | endpoint <authorized delivery endpoint>
Decisions: <decision → the user's own words or a direct pointer to them → source (user / ADR / spec)>
Constraints: <numbers, negative requirements ("do not…", "only…"), ordering, exclusions>
Facts: <verified facts and where they were verified — files, issues, commits, commands>
Open questions: <each with who can answer it and why it is still open>
Skills: <the routes and their dependencies, with real paths and invocation class>
Artifacts: <pointers, never copies, to specs, tickets, research, prototypes, diffs>
Next: <the first action in the new context, and the gate that will close it>
```

Pointers, not copies: a document that already exists is referenced, so the new context reads the
current version rather than a stale paste. The user's original decisions are the one exception —
their words are quoted, because a paraphrase is how a constraint gets softened. Sensitive
material stays desensitized.

**What must survive the handoff**: every numeric constraint, every negative requirement, every
decision whose source is the user, and every open question. A handoff that carries the goal but
loses "the retry cap is 30 s, never 60" or "do not touch the settings page" has not moved the
work; it has restarted it.

**When a pointer suffices.** A tightly scoped subtask inside the same phase only needs the task
spec and a context pointer — the files it must read. The full handoff record is for a real boundary:
a new harness, directory or collaborator, a forked side task, or a stage resumed after the original
context was cleared. Do not dress a subtask up as a handoff, and do not send a boundary crossing
without one.

**The user's answer returns through the original message.** When a worker asks a decision-class
question, the answer is recorded in the handoff or Answer with the human's key words, then sent back
on the same orchestration channel; introducing another tool to answer a worker splits the audit
trail. `grilling` and `prototype` decisions must come from the user, never from a coordinator or a
worker substituting for them.
