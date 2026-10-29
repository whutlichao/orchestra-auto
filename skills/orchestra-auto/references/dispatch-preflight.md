# Dispatch preflight: three checkpoints — before dispatch, after dispatch, at teardown

**When to read this**: at three moments, and only the section that moment owns. **§4.0.1 before
every dispatch** (tick every line); **§4.0.2 right after the receipt** (start evidence and, for a
long-horizon map, the worker's isolation proof); **§4.0.3 at teardown**, after `worker_done` and
before you declare a gate passed.

**Why the sheet is split**: one checklist that mixes "is my spec written" with "did the worker's
terminal show a started turn" turns the rule *not every line is green yet* into *do not dispatch*,
and the second kind of fact does not exist before the Dispatch does. Each line below therefore sits
at the moment it becomes observable. Only §4.0.1 can block a dispatch; §4.0.2 stops a wait that has
already started, and §4.0.3 stops a gate, not a dispatch.

### Preflight receipt

Every dispatch writes a ledger `kind: preflight` receipt **before** the dispatch. The receipt
is the machine-readable form of this sheet; the sheet remains the source of truth, so a green
line in one and a red item in the other is a contradiction, not a judgement call.

```bash
python3 "$SKILLS_ROOT/orchestra-auto/scripts/preflight.py" check \
  --run-id "$RUN" --ticket ticket.json --context context.json
```

The `ticket` file carries `id`, `type`, `gate` and `origin`. The `context` file supplies the
rest: `run_id`, `round_started_at`, `base_commit`, optional `checked_at`, `guide`
(`fetched_at`, `path`, `version`, `command`), `judgments` (a `pass` or `fail` with `note` and
`evidence_ref` for each judgment item), `baselines` (`label`, `source_commit`, optional
`order_of_magnitude` or `used: false`), `overrides` and `review_exemption`; `post-dispatch`
also needs `worktree`, `dashboard` and `inbox`. A judgment item with no recorded result
fails; a mechanical item is observed by the CLI, never supplied by the coordinator.

The receipt records the ticket identity (`id`, `type`, `gate`, `origin`), `checked_at`, `items`,
`blocking`, `overrides` and `post_dispatch`. Every item carries `id`, `class`, `result`,
`command`, `observed`, `note` and `evidence_ref`. `class` is `mechanical` when the CLI observed
the value or `judgment` when the coordinator recorded it; `result` is `pass`, `fail` or
`waived`. The twelve §4.0.1 lines keep their identity and name their item id and class in the
line itself. `live-settings` is an explicit independent blocking preflight item, not a
thirteenth tick line: it runs before the sheet because the agent choice depends on it.

Every pre-dispatch item is blocking. A failed blocking item rejects the dispatch; an `overrides`
entry with `item`, `by` and `reason` is the only way to turn that item into `waived`, and an
incomplete override waives nothing. Exit `0` means the receipt was written and nothing blocks;
exit `1` means a blocking failure, invalid input, or a receipt that could not be written
durably; exit `2` is a usage error. **Never dispatch on a non-zero exit, and never dispatch
when the receipt write failed** — a failed record is not an absent record. The preflight
command never runs `worker-start`; the coordinator starts the worker only after exit `0`.

A dashboard write is never part of this decision. A failed dashboard write is a gap recorded
after the receipt, and it cannot turn a passing preflight into a failing one.

### Live worker settings lookup

Only do this lookup when the Run is about to dispatch. Use the live Orca CLI to locate the current
profile (for example, read `result.settingsPath` from `orca agent hooks status --json` and assign it
to `SETTINGS_PATH`; its parent directory is `PROFILE_DIR`). Use that path only to locate the profile;
**do not read agent settings from `orca-data.json`**.

This lookup is the `live-settings` item: class `mechanical`, blocking, and independent of the
twelve §4.0.1 lines. It is recorded in the receipt like every other item. An unreadable
database, table, field or value fails it and stops the dispatch, and no fallback source is
read.

Read the settings from the profile's **live state database** — the one SQLite file in that directory
that is not a retained backup. Recovery snapshots also end in `.db`, so select the database instead of
naming it, and never pass several files to `sqlite3`: zero or several candidates means stop and report
rather than guess, and a backup would answer with stale settings. In the current schema, settings
are stored as JSON in the `payload` column of the `profile_state_documents` row where
`domain = 'settings'`. The top-level `defaultTuiAgent` and `disabledTuiAgents` fields hold the default
TUI agent and disabled-agent list. For example:

```bash
PROFILE_DIR="$(dirname "$SETTINGS_PATH")"
STATE_DB="$(find "$PROFILE_DIR" -maxdepth 1 -name '*.db' ! -name '*.backup.*')"
[ "$(printf '%s\n' "$STATE_DB" | grep -c .)" = "1" ] || { echo "expected exactly one profile state database"; exit 1; }
sqlite3 -readonly "$STATE_DB" \
  "SELECT json_object(
     'defaultTuiAgent', json_extract(payload, '$.defaultTuiAgent'),
     'disabledTuiAgents', json_extract(payload, '$.disabledTuiAgents')
   ) FROM profile_state_documents WHERE domain = 'settings';"
```

Confirm that `defaultTuiAgent` is non-empty and `disabledTuiAgents` is an array (an empty array is
valid). If the database, table, fields, or values cannot be read, check the current CLI help and Orca
guide for the profile-state schema; if it still cannot be confirmed, stop and report. Do not fall
back to `orca-data.json`, stale exports, snapshots, backups, skill text, eval fixtures, or old logs.

The table and field names above were verified with Orca CLI 1.4.244 on macOS on 2026-10-29.
That is a version-scoped observation, not a stable contract: the receipt records the
verification source and timestamp, tests use fixtures, and a different Orca version must be
re-verified against its own CLI help and guide before its table or field names are trusted.

If the user named an agent, use it ahead of the default; otherwise use the live default. In either
case, check the live disabled list and the current `worker-start --help` agent contract. A “such as”
example list is not exhaustive: omission does not establish lack of support. If an authoritative
complete list exists for the current version, check it; otherwise pass the selected, enabled value
unchanged as an explicit `--agent` and let Orca validate it. Stop before dispatch if the selection
is known to be disabled or unsupported, or settings are unreadable. If the receipt reports
`agent_unconfigured` or another failure, follow failed-dispatch recovery, stop, and do not substitute
an agent or retry blindly.

### 4.0.1 Before dispatch — the tick sheet (tick every line; do not dispatch until all are green)

Its only purpose is to turn "did I forget one?" into a checklist rather than a memory test —
when a long spec is half written, an already-confirmed constraint is the thing that quietly
falls off. The parenthesised section is where it belongs.

- [ ] **The CLI is alive** (`cli-app-up`, `cli-command-surface`; mechanical): `<ORCA>` resolves
      (`ORCA_CLI_COMMAND` or `orca`) and `orca status --json` reports the app up. Record both
      evidence items: the app reply and a minimal command-surface probe (`worker-list`). If it
      will not start → report the exact error per §9 and stop; do not swap the executable and do
      not guess commands.
- [ ] **The guide was fetched this round** (`guide-freshness`; mechanical): only a
      `skills get orchestration` fetch whose timestamp falls inside this round passes. A file on
      disk from an earlier round does not pass, and re-reading it is not a fetch.
- [ ] **The channel is decided, and was re-checked if exploration happened** (`channel-decided`;
      judgment; §1, last paragraph).
- [ ] **The ticket type is decided and appears as a line in the spec** (`ticket-type`; judgment;
      §4.4): research, design,
      spec, ticket splitting, or implementation. Judge it before dispatch from Target / Change
      and the expected files; the worktree does not exist yet, so do not run `git diff` against
      it. A documentation ticket lists only the documentation gates that apply and must not put
      source tests or a two-axis source review in its acceptance list. Recheck the type against
      the real diff afterwards.
- [ ] **The spec has all five elements** (`spec-elements`; judgment): Target / Change /
      Constraints / Ownership / Observable acceptance, with the BEHAVIOR RULE paragraph and the
      pre-skill's absolute path as the first two items (§4.4).
- [ ] **The worktree parameters are right** (`worktree-parameters`; judgment):
      `--worktree new-top-level` + `--name <ticket slug>` + `--agent <§0's worker>`; a
      long-horizon map uses `--name <feature>-integration` and one worker. If the session cwd
      is not the target repo, add `--repo <selector>` (§4.3).
- [ ] **A long-horizon map is really ready** (`long-horizon-map`; judgment; the map itself, not
      the worker): one integration PR, `implement-spec` as the pre-skill, a complete ticket map
      and a reachable tracker. Whether the worker can actually create isolated nested worktrees
      is its first report, so that proof is §4.0.2, never this line.
- [ ] **The main hard-rule self-check really ran** (`main-worktree-rule`; judgment; §2): this
      round will not modify project files or create commits in the main working copy; only
      §6.5.1's fast-forward sync of already merged work may update the main branch.
- [ ] **The ticket's canonical copy is located** (`ticket-canonical-copy`; judgment): it lives
      inside the task worktree, and the worker owns its status transitions and Answer appends;
      the coordinator only reads it on main for acceptance (§4.4).
- [ ] **The gates are named and decidable** (`gates-decidable`; judgment): which apply at this
      stage (G-docs / G-spec /
      G-tickets / G-impl / G-merge) and what evidence each demands, so §6 can judge mechanically.
      Implementation tickets get two extra ticks: ① per §6's first G-impl rule, choose the test
      tier (narrow or wide) and **write it into the spec's `Test tier:` line** (directly after
      `Gates:`); if "narrow", the same line must say why the blast radius is bounded and name a
      selector that runs as written (`node glob`, or this repository's equivalent test path or
      marker); ② if this ticket skips the two-axis review, confirm §3's
      conditions and write the reason down (the conditional form is below).
      A tier that exists only in your head is a tier the spec does not have: the worker cannot see
      your reasoning, so it runs the full suite or picks a few tests on its own.
      **Mechanical test for the tier**: `grep -c 'Test tier:' <spec>` returns 0 → do not dispatch an
      implementation ticket.
      **Mechanical test for review**: an implementation ticket's `Observable acceptance` must contain
      the literal string `code-review` (together with a two-axis / Standards + Spec phrase)
      unless §3 exempts it. A long-horizon map writes "one two-axis code-review on the
      integration branch" plus per-ticket TDD; it must not also require a per-ticket two-axis
      review. Run `grep -c 'code-review' <spec>`; a zero means it is not written down and you
      may not dispatch.
      Why that mechanical test exists: a round of implementation specs had `review-mentions=0`
      across the board, because their pre-skill pointed at `implement`, whose body says to use
      `/code-review`, and from that I assumed "the worker will do it itself". A worker delivers
      only the acceptance list the spec spells out — a general principle never becomes one of its
      deliverables, and what is not in the checklist it will not add.
      **The exemption is conditional, not a matter of taste**: §3 allows skipping the two-axis
      review only for a change that is (a) a single ticket, (b) already settled in approach, and
      (c) of bounded blast radius — `git diff --numstat` and the changed-file list against the
      ticket's base, not a guess. A cross-ticket or shared-interface change gets wider G-impl
      verification and the review stays. When the exemption holds, the reason must be written in
      **both** the spec and the Answer; a reason that exists only in the coordinator's head leaves
      the worker unable to know the review was waived, and a reviewer unable to see why.
- [ ] **Every baseline number in the spec was verified against the base commit**
      (`baseline-source-commit`; mechanical): whenever you cite a measured number such as
      "pre-change baseline: Test Files X / Tests Y", confirm that the log came from the **same
      base commit as this ticket's worktree**.
      A recurring failure: a full-suite log from a **branch** that carried new tests gets used as
      the pre-change baseline of a different ticket cut from `main`, so the case count does not
      match — the worker's two-axis review raised exactly that as P1, and unchecked it becomes a
      false-green gate.
      **Mechanical criterion**: before pasting a baseline, compare the producing worktree's
      commit with this ticket's base commit; if they differ, **do not use** that number — or
      write "baseline from commit <X>, N commits off this ticket's base, for order-of-magnitude
      reference only".
- [ ] **If this repository really has a formatter hazard, the targeted prevention and safe
      recovery requirements are written into the spec** (`formatter-prevention`; judgment; §4.4).

Send-backs and circuit breakers are not in this sheet — follow §6's send-back protocol (with a
concrete list of what is missing; two consecutive failures of the same gate on the same ticket
stops and asks the human).

### 4.0.2 Right after dispatch — start evidence and the isolation report

These facts do not exist before the Dispatch does, so they are checked **after** the receipt and
**before** the long wait. They stop the wait, never the dispatch.

- **The turn really started** (§4.3.1). `turnStart: observed` is the receipt's own start
evidence: accept it and move on. When it is `unsupported`, `unobserved`, or absent, the agent
cannot report a start, so read the evidence that agent supports — the terminal title and screen
for a worker with a terminal, `worker-read` / the status projection / artifact progress for one
without ([start and recovery](worker-start-and-recovery.md)). Do not force `terminal show` onto a
worker that has no terminal. An unchanged idle title is neither proof that the turn started nor
proof that it did not; one bare Enter is allowed only when the screen shows the complete preamble
parked in the composer with no turn running. If you cannot prove the start, gather evidence per
§9 instead of entering a blind wait.
- **A long-horizon worker proved nested isolation, or the run degrades explicitly**: its first
report shows it can create an isolated worktree and branch cut from the integration branch, run
implementers in the background or serially, and merge without touching the main working copy. If
it cannot, record the serial fallback (or the per-ticket fallback) in the ledger and say why; two
implementers must never share one worktree.
- **A provisional review exemption is rechecked against the real worktree**
  (`review-exemption-recheck`). Before dispatch an exemption is `provisional` and names the
  expected changed files and the interface impact. After the receipt, run `git diff --numstat`
  against the ticket base in the real worktree: an unexpected file, or more changed lines than
  the exemption declared, revokes it — add the review or send the work back. The recheck is a
  post-dispatch observation, not a pre-dispatch gate, and it never rewrites the receipt. A
  waiver's reason is written in both the spec and the Answer.
- **The dashboard record was backfilled or a gap was recorded** (`dashboard-backfill`). This is
  a ledger observation: a failed dashboard write records a gap, never blocks dispatch, and never
  changes the preflight result.
- **The inbox consumer is installed and healthy** (`inbox-health`, §4.5): one consumer for this
  Run, bound to the real coordinator handle, installed as a call the harness surfaces rather
  than `nohup`. This is a ledger observation too, never a pre-dispatch gate.

Record these three observations after the receipt:

```bash
python3 "$SKILLS_ROOT/orchestra-auto/scripts/preflight.py" post-dispatch \
  --run-id "$RUN" --ticket ticket.json --context context.json
```

`post-dispatch` exits `0` when its observations are durable, even when it revokes an
exemption: the revocation is reported in the JSON and is evidence for the coordinator, not a
command failure. Exit `1` means the observations could not be recorded.

A red item here is handled as evidence: fix what is provable, escalate what needs a human, and
keep the artifacts. It never silently becomes "the dispatch failed, retry it" — that conclusion
has its own evidence bar in [failed dispatch recovery](failed-dispatch-recovery.md).

### 4.0.3 At teardown, before a gate is declared passed: formatter noise

After a worker reports `worker_done` and **before** declaring the gate passed, **run these three**:

```bash
cd <task worktree>
git status --porcelain
git diff --numstat | awk '{a+=$1; d+=$2} END {print "worktree uncommitted:", a, "added", d, "deleted"}'
```

- Uncommitted output empty → clean, continue acceptance.
- Uncommitted change totalling **tens of thousands of lines** → a whole-file reflow; read
  `references/reformat-noise-recovery.md` and recover.
- Uncommitted change of normal size → ask the worker why work is uncommitted; do not accept it
  by default.

**One-line criterion**: once a changed file's `git diff --numstat` shows four or more digits, go
look at whether lines were squeezed to roughly 80 columns. Editor plugins and repository
formatters can reflow an **entire file** to their default width on write when the repository
declares no formatter configuration, while plenty of repositories are written in wide lines.

**Recovery is executed by the worker; the coordinator only preserves evidence and sends back.**
Back up the complete diff, untracked files and the index first, then confirm file by file which
content is real work. A non-empty `git diff --cached` still does not prove the rest of the
working tree is noise; do not check out before confirming and backing up. The full recovery
order is in the reference file.

**Treating the cause**: only after confirming that the current worker's editor plugin actually
reflows and that the repository is at risk should you configure the local ignore per the
[recovery reference](reformat-noise-recovery.md); do not introduce that
configuration into other environments.
