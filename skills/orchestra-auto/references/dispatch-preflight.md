# Dispatch preflight: the tick sheet and the teardown check

**When to read this**: before every dispatch (tick every line), and again at teardown after a
worker reports `worker_done` and before you declare a gate passed.

### Live worker settings lookup

Only do this lookup when the Run is about to dispatch. Use the live Orca CLI to locate the current
profile (for example, read `result.settingsPath` from `orca agent hooks status --json` and assign it
to `SETTINGS_PATH`; its parent directory is `PROFILE_DIR`). Use that path only to locate the profile;
**do not read agent settings from `orca-data.json`**.

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

If the user named an agent, use it ahead of the default; otherwise use the live default. In either
case, check the live disabled list and confirm the selected agent is recognized by
`worker-start --help`. A disabled, unrecognized, or unreadable selection stops dispatch.

### 4.0 Pre-dispatch tick sheet (tick every line; do not dispatch until all are green)

Its only purpose is to turn "did I forget one?" into a checklist rather than a memory test —
when a long spec is half written, an already-confirmed constraint is the thing that quietly
falls off. The parenthesised section is where it belongs.

- [ ] **The CLI is alive**: `<ORCA>` resolves (`ORCA_CLI_COMMAND` or `orca`) and `orca status --json`
      reports the app up. If it will not start → report the exact error per §9 and stop; do not
      swap the executable and do not guess commands.
- [ ] **The guide is on disk**: §4.2's `skills get orchestration` output was written to `/tmp` and
      is not re-fetched this round.
- [ ] **The channel is decided, and was re-checked if exploration happened** (§1, last paragraph).
- [ ] **The ticket type is decided and appears as a line in the spec** (§4.4): research, design,
      spec, ticket splitting, or implementation. Judge it before dispatch from Target / Change
      and the expected files; the worktree does not exist yet, so do not run `git diff` against
      it. A documentation ticket lists only the documentation gates that apply and must not put
      source tests or a two-axis source review in its acceptance list. Recheck the type against
      the real diff afterwards.
- [ ] **The spec has all five elements**: Target / Change / Constraints / Ownership / Observable
      acceptance, with the BEHAVIOR RULE paragraph and the pre-skill's absolute path as the
      first two items (§4.4).
- [ ] **The worktree parameters are right**: `--worktree new-top-level` + `--name <ticket slug>`
      + `--agent <§0's worker>`; a long-horizon map uses `--name <feature>-integration` and one
      worker. If the session cwd is not the target repo, add `--repo <selector>` (§4.3).
- [ ] **A long-horizon map is really ready**: one integration PR, `implement-spec` as the
      pre-skill, a complete ticket map and a reachable tracker; the worker's first report proves
      nested worktree isolation or records the serial fallback (§ long-horizon reference).
- [ ] **The main hard-rule self-check really ran** (§2): this round will not modify project files
      or create commits in the main working copy; only §6.5.1's fast-forward sync of already
      merged work may update the main branch.
- [ ] **The ticket's canonical copy is located**: it lives inside the task worktree, and the
      worker owns its status transitions and Answer appends; the coordinator only reads it on
      main for acceptance (§4.4).
- [ ] **The gates are named and decidable**: which apply at this stage (G-docs / G-spec /
      G-tickets / G-impl / G-merge) and what evidence each demands, so §6 can judge mechanically.
      Implementation tickets get two extra ticks: ① per §6's first G-impl rule, choose the test
      tier (narrow or wide) and **write it into the spec's `Test tier:` line** (directly after
      `Gates:`); if "narrow", the same line must say why the blast radius is bounded and name a
      selector that runs as written (`node glob`, or this repository's equivalent test path or
      marker); ② if this ticket skips the two-axis review, confirm §3's
      count-based criterion and write the reason down.
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
      A recurring failure: a round of implementation specs had `review-mentions=0` across the
      board, because their pre-skill pointed at `implement`, whose body says to use
      `/code-review` — and from that I assumed "the worker will do it itself". But a worker
      delivers only the acceptance list the spec spells out; **a general principle living in the
      pre-skill never becomes one of its deliverables**. What is not in the checklist, it will
      not add.
- [ ] **Every baseline number in the spec was verified against the base commit**: whenever you
      cite a measured number such as "pre-change baseline: Test Files X / Tests Y", confirm that
      the log came from the **same base commit as this ticket's worktree**.
      A recurring failure: a full-suite log from a **branch** that carried new tests gets used as
      the pre-change baseline of a different ticket cut from `main`, so the case count does not
      match. The worker's two-axis review raised that inconsistency as P1, quite
      correctly — and had it not, the mistake would have become a false-green gate.
      **Mechanical criterion**: before pasting a baseline, compare the producing worktree's
      commit with this ticket's base commit; if they differ, **do not use** that number — or
      write "baseline from commit <X>, N commits off this ticket's base, for order-of-magnitude
      reference only".
- [ ] **If this repository really has a formatter hazard, the targeted prevention and safe
      recovery requirements are written into the spec** (§4.4).
- [ ] **After dispatch, the turn really started** (§4.3.1), and §4.5's single background inbox
      consumer is healthy.

Send-backs and circuit breakers are not in this sheet — follow §6's send-back protocol (with a
concrete list of what is missing; two consecutive failures of the same gate on the same ticket
stops and asks the human).

### 4.0.1 Always check on teardown: formatter noise

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
