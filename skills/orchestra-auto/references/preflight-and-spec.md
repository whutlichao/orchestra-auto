# Preflight, Worker Spec Contract, and Planning Gates

**When to read**: before writing any worker spec, dispatching a worker (`§4.0.1`), verifying start/observations (`§4.0.2`), finishing teardown (`§4.0.3`), or accepting planning/prose gates (`G-docs`, `G-wayfinder`, `G-spec`, `G-tickets`).

## Preflight Verification (`preflight.py`)

Every dispatch must write a durable `kind: preflight` receipt **before** calling `worker-start`. Run with `--spec` to validate the 5 spec elements, `BEHAVIOR RULE`, `Pre-skill` file existence on disk, `Type:`, `Gates:`, `Test tier:`, and `code-review` automatically:

```bash
PREFLIGHT="$SKILLS_ROOT/orchestra-auto/scripts/preflight.py"
# Planning branch example (R1–R10 / R14–R16, e.g. R2 grill-with-docs -> design / G-docs):
python3 "$PREFLIGHT" inspect-spec --spec /tmp/worker-spec.md --type design --compact
python3 "$PREFLIGHT" check --run-id "$RUN" \
  --ticket-id 42 --type design --gate G-docs --origin "GH#42" \
  --spec /tmp/worker-spec.md \
  --guide /tmp/orchestration-guide.md --guide-fetched-at "$FETCH_TS" --round-started-at "$ROUND_TS" \
  --channel-note "channel B (R2 grill-with-docs)" --worktree-note "new-top-level" \
  --long-horizon-note "single stage" --main-rule-note "clean" --canonical-note "worktree" \
  --legacy-migrated --migration-ref "native-only" --compact

# Implementation branch example (R11–R13, e.g. R12 implement -> impl / G-impl):
python3 "$PREFLIGHT" inspect-spec --spec /tmp/worker-spec.md --type impl --compact
python3 "$PREFLIGHT" check --run-id "$RUN" \
  --ticket-id 42 --type impl --gate G-impl --origin "GH#42" \
  --spec /tmp/worker-spec.md \
  --guide /tmp/orchestration-guide.md --guide-fetched-at "$FETCH_TS" --round-started-at "$ROUND_TS" \
  --channel-note "channel B" --worktree-note "new-top-level" \
  --long-horizon-note "single ticket" --main-rule-note "clean" --canonical-note "worktree" \
  --legacy-migrated --migration-ref "native-only" --compact
```

Legacy `--ticket ticket.json --context context.json` remains supported. The receipt records `items`, `blocking`, `overrides` (`item`, `by`, `reason`), and `post_dispatch`; non-zero exit or `record.written == false` blocks dispatch (`message-capability` is non-waivable). A failed dashboard write is a ledger gap, never a preflight failure.

**Independent mechanical preflight checks (run before the 12-line sheet):**
- **`live-settings`** (mechanical): reads `result.settingsPath` from `orca agent hooks status --json` and probes non-backup `*.db` files read-only via `profile_state_documents` (`domain = 'settings'`) for `defaultTuiAgent` and disabled TUI agent list (`disabledTuiAgents`; version-scoped: Orca CLI `1.4.244` on `2026-10-29`, `1.4.265` on `2026-11-20`, re-verified `1.4.277` on `2026-11-26`). Never fall back to `orca-data.json` or backups; see [preflight internals](../../../docs/design/preflight-internals.md). User-named agent wins over default if enabled; help examples are not exhaustive (`example list is not exhaustive`; pass enabled agent to `--agent` and let Orca validate it; on `agent_unconfigured`, stop and do not substitute blindly).
- **`message-capability`** (mechanical, non-waivable): probes selected Orca `--version`, `skills get orchestration`, `check --help` (`--retry-request`), `inbox --help` (`--terminal`, `--full`), plus live structured session (`ORCA_STRUCTURED_SESSION=1` + `ORCA_AGENT_SESSION_ID` + `status --json`) or TUI terminal binding (`ORCA_TERMINAL_HANDLE` + `ORCA_AGENT_PANE` + `terminal list --json` + `inbox --terminal <handle>`), and legacy consumer migration attestation. If CLI wording changes compatibly, only an explicit human may review the version/hash-bound probes (`context.messaging.human_verification`). Note: `orca bot` (`send --to bot:<name>`) is an autonomous owner-acting assistant channel, not a supervised `worker-start` replacement.

## Pre-skill Selection & Invocation

Choose `Pre-skill` **by stage and ticket type, not by channel**: `grill-with-docs`, `research`, `to-spec`, `to-tickets`, `implement-spec`, `diagnosing-bugs` + `implement`, `code-review`. Confirm wrapper and dependency `SKILL.md` paths exist and are readable on disk (`pre-skill` check in `preflight.py`). Never substitute paths from memory or install unauthorized skills; Missing → report per §9 (blocker report). Write only invocation forms supported by the target agent (or `unverified`), never falsely claim direct file reading was native invocation. Setup skills (`setup-matt-pocock-skills`, `disable-model-invocation: true`) always name their explicit path and belong only in an isolated worker worktree.

## Worker Spec Template (`BEHAVIOR RULE` & Five Elements)

```text
【BEHAVIOR RULE (obey verbatim; do not trim)】
1. Questions: always use `orchestration ask` (AskUserQuestion is banned). Return open decisions; never guess for the user.
2. Scope & Delivery Order: edit only files in Ownership. Handle delivered follow-ups -> write Answer & test evidence -> commit, push, and create/update PR with `--body-file` (record PR number, body path & CI reading in GitHub and out-of-repo ledger; no project commit is added to carry those facts; the in-repo Answer therefore reports what was delivered and its branch, and final PR HEAD CI stays coordinator's check before the merge — only if user/repo explicitly requires those facts in the tree, follow that rule instead) -> run exact `worker_done` command (real Task ID, Dispatch ID, succeeded/failed, 3-sentence summary) -> verify receipt -> idle immediately. Keep send receipts; inspect Dispatch before any retry; stop immediately on `consumer_fenced`.
3. Escalation: stop and escalate if needing human, credentials, third party, paid service, manual approval, or irreversible live-data operations.
4. Language: use deep-module / codebase-design vocabulary for architecture notes; follow repo language in code/tests/reports.
5. Boundaries: crossing Ownership/scope requires escalation.
6. Safety & Formatting: take red evidence cleanly before fixing; back up uncommitted work before restores/formatting; check `git status` and `git diff --numstat` before delivery; handle reflow noise file-by-file per recovery rules.

Pre-skill:
- Name: <stage skill name>
- Explicit invocation: <supported form or unverified>
- File location: <actual absolute path>/SKILL.md
Skill execution: prefer native invocation if host supports; read file as fallback only if allowed; follow supporting files; never report direct read as native invocation.
Type: research | design | spec | ticket-splitting | impl | docs
Gates: <applicable gates only, e.g. G-impl, G-merge>
Test tier: <narrow (why blast radius is bounded + runnable selector) | wide (full gate + reason); mandatory for impl>
Target: <files/modules>
Change: <exact deliverable>
Constraints: <explicit boundaries>
Ownership: <editable scope; canonical ticket lives in worktree and worker owns status/Answer appends>
Observable acceptance: <verifiable test commands + red->green evidence; for impl include literal two-axis `code-review` (standards + spec) unless review-exempt with reason in spec AND Answer; for project PRs name target repo, base branch (`upstream/<default>` for forks, never fork main), `--repo ... --base ... --head ...`, PR body sections, and `worker_done` receipt; add `visual-evidence.py` requirement when Orca screenshots are needed>
Skill execution evidence: <skill name/path, method, verifiable record, key steps/artifacts>
Question channel: orchestration ask (AskUserQuestion is banned)
```

For long-horizon ticket maps (`implement-spec`), also pass resolved `tdd`/`code-review`/`pr` skill paths, `Tracker`, `Spec`, `Tickets`, and `Integration branch` (requiring ticket map frontier, branch/commit per ticket, single integration PR with `Closes`, Door declaration, per-ticket TDD + single integration `code-review` instead of per-ticket dual reviews).

## Checkpoints: Before (§4.0.1), After (§4.0.2), Teardown (§4.0.3)

Only §4.0.1 can block a dispatch; §4.0.2 stops blind waits/revokes provisional exemptions; §4.0.3 gates acceptance after `worker_done`.

### 4.0.1 Before dispatch — the twelve-line tick sheet

- [ ] **CLI is alive** (`cli-app-up`, `cli-command-surface`; mechanical): `<ORCA>` resolves, `orca status --json` and minimal `worker-list` reply cleanly.
- [ ] **Guide fetched this round** (`guide-freshness`; mechanical): `skills get orchestration` timestamp >= round start (stale on-disk file fails).
- [ ] **Channel decided** (`channel-decided`; judgment): re-checked after any read-only exploration.
- [ ] **Ticket type decided** (`ticket-type`; judgment): appears in spec (`Type:`); docs tickets list only doc gates.
- [ ] **Spec complete** (`spec-elements`; judgment): Target / Change / Constraints / Ownership / Observable acceptance + `BEHAVIOR RULE` + readable `Pre-skill` paths + execution evidence.
- [ ] **Worktree parameters exact** (`worktree-parameters`; judgment): `--worktree new-top-level --name <slug> --agent <enabled>` (+ `--repo` if cwd differs; fork uses `--base-branch upstream/<default>`).
- [ ] **Long-horizon map ready** (`long-horizon-map`; judgment): complete map, reachable tracker, `implement-spec`, one integration PR (isolation proof itself is §4.0.2).
- [ ] **Main worktree clean rule** (`main-worktree-rule`; judgment): main workspace never edited directly (only post-merge `--ff-only` sync).
- [ ] **Canonical ticket located** (`ticket-canonical-copy`; judgment): worker edits ticket in its task worktree; coordinator reads on main for acceptance.
- [ ] **Gates & test tier decidable** (`gates-decidable`; judgment): `Test tier:` line present on impl specs; literal `code-review` present unless conditional exemption (single ticket + settled + bounded blast radius + reason in spec and the Answer) recorded as `provisional`.
- [ ] **Baselines match base commit** (`baseline-source-commit`; mechanical): cited baseline numbers came from the exact same base commit as the new worktree (or explicitly flagged order-of-magnitude only).
- [ ] **Formatter hazard covered** (`formatter-prevention`; judgment): Spec rule 6 included and local override used only when reflow risk is real.

### 4.0.2 Right after dispatch — start evidence & observations

These facts do not exist before the Dispatch does, so record them immediately after receipt via `preflight.py post-dispatch`:
- Confirm `turnStart` (see `dispatch-and-comms.md`), or mark `unobserved` and inspect terminal/screen/`worker-read` instead of blind waiting.
- Long-horizon workers must verify isolated worktree/branch creation in their first report (`review-exemption` recheck confirms isolated worktree diff too).
- Record `review-exemption-recheck` (`git diff --numstat` vs expected files/line limit — unexpected files/size revokes exemption without mutating receipt), `dashboard-backfill` (gap only on failure), and `inbox-health` (never waives `message-capability`).

### 4.0.3 Teardown — formatter noise check (before G-impl acceptance)

Run `git status --porcelain` and `git diff --numstat` in the task worktree: empty = clean; normal = ask worker to explain uncommitted files; abnormal multi-thousand-line reflow noise → follow formatter recovery in [recovery-and-health.md](recovery-and-health.md).

## Planning Gates (`G-docs`, `G-wayfinder`, `G-spec`, `G-tickets`)

All controlled planning artifacts (`docs/adr/*.md`, `GLOSSARY.md`, `GLOSSARY-MAP.md`, specs, ticket files, and `gh issue create` / tracker writes) are produced by dispatched workers in isolated worktrees — never written directly by the coordinator. Ad-hoc coordinator Q&A (such as 3 macro multiple-choice questions) never satisfies `G-docs`.

- **`G-docs` / `grill`**: verify real worker-driven `grill-me` / `grill-with-docs` execution evidence (questions, answers, rationale, settled terminology in `GLOSSARY.md` / ADRs in `docs/adr/` if applicable, and repo conventions). Required before ADR writes, `to-spec`, or `to-tickets` unless explicitly skipped by the user.
- **`G-wayfinder`**: ticket states are `takeable`, `in progress`, `blocked`, `done`. Remember: **"an empty frontier is not completion"** (`the frontier is empty but tickets are in progress or blocked` must NOT close the stage; empty frontier has three causes — every ticket assigned, every remaining ticket blocked, or the work genuinely finished). Map is an index (`decisions so far` point to owning ticket). State whether handoff moves to §7/impl or stops at decisions. Decision branches (`research` AFK, `prototype` HITL, `to-questionnaire` async HITL, `wizard` human, `task` HITL/AFK) keep blocked work blocked until real answers/facts land.
- **`G-spec`**: fewest/highest seams (seam placement = approach fork → escalate per decision rules), clear user stories, non-empty Out of Scope.
- **`G-tickets`**: vertical tracer bullets, explicit DAG dependencies, tracker populated first, acyclic graph + reachable tracker + single-context size. Splitting granularity (count & what each ticket delivers) goes to the human.

## Prose Contract (Six Critical Anchors)

These six clauses state what may never be inferred across language packs:

1. **`absent` is not proof** — `request-show` returning `absent` means unknown key, not unexecuted; inspect Orca workers and git before deciding ([ledger-and-dashboard.md](ledger-and-dashboard.md)).
2. **A dashboard failure never blocks dispatch** — failed `progress.py` write / lock / viewer failure logs a ledger gap only; `open` is never on the dispatch critical path.
3. **`worker_done` is not acceptance** — it proves settlement only; acceptance needs observable checks and evidence (`grep` proves only string presence, and health never authorizes release).
4. **Release only after settlement and a closeout record** — requires settled `worker-show` and durable `kind: closeout` record (never release an unsettled worker).
5. **`ready` is not a started turn** — require `turnStart: observed` or artifact/status projection/screen evidence.
6. **`unknown` is not failure** — missing/stale/unreadable authority demands evidence collection, never blind retry; empty wait != empty queue.

**Note on test scope**: `tests/test_package_content.py` **checks anchors, not full semantic equivalence** — keeping these phrases is **not a translation** and **not proof** that surrounding prose did not drift; reviewers must still read diffs carefully.
