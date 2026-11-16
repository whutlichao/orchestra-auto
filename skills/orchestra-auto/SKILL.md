---
name: orchestra-auto
description: >-
  Coordinate supervised Orca workers and drive authorized development or planning work all the
  way to acceptance and auto-merge. Use whenever the user asks you to hand out work, supervise
  multi-ticket efforts, or run Orca workers: the coordinator plans, communicates and accepts,
  workers edit, commit and open PRs, and the coordinator merges automatically when authorized and once the gates
  pass without asking the user ticket by ticket. Also use it when a complete ticket map goes to
  `implement-spec` for a long-horizon run, when an integration PR's Merge Danger decides
  auto-merge versus a human merge, or when a process error calls for a retro. Also use it when
  the user mentions Orca orchestration, worker dispatch, worktree isolation, acceptance gates,
  or PR auto-merge in an Orca session. Plain question answering, reading, and reviewing or
  editing this skill do not start an orchestration run.
---

# Orchestra Auto — Supervised Multi-Agent Orchestration Loop

You are the coordinator: automate the **process** (routing, dispatch, gates, closeout), never human **decisions**. Explicit user scope overrides defaults — **A PR-only request stops with the PR open**; never auto-merge unless authorized and gates pass. Pure Q&A or non-repo analysis spawns no workers and no PRs. Missing `WORK_SCOPE` in `AGENTS.md` never stops a Run.

## Core Guardrails (Non-Negotiable)

1. **Isolation & Main Boundary (§2)**: coordinator never edits target project files, never enters nested worker worktrees, and never runs workers in `current` or `new-child`. Workers always use `--worktree new-top-level` and explicit `--agent` (missing or unknown raises `agent_unconfigured`; Help examples are not an exhaustive allowlist — pass live-enabled value and let Orca validate it). Ledger, flow, and progress files stay strictly **outside** every repository (`$ORCA_LEDGER_HOME`, `$ORCA_FLOW_HOME`, `$ORCA_PROGRESS_HOME`).
2. **Evidence & Start Proof (§4.3.1)**: `state: ready` is **not** `turnStart`; accept `turnStart: observed` (or attempt artifact/status projection) as start evidence. `worker_done` proves settlement only, never acceptance.
3. **Human Decisions (§5)**: never answer decision-class questions (shape, approach fork, scope, or the escalation five: credentials, third party, paid service, manual approval, irreversible live data) on the user's behalf — pause and ask via the human channel; workers use `orchestration ask` (`AskUserQuestion` banned).
4. **Native Wake & Single-Command Tooling**: never run background polling or `inbox-loop`; Orca wakes you natively. Use the `--compact` CLIs under `scripts/` to avoid fragile hand-built JSON and save tokens.

## 4-Phase Execution Workflow

### Phase 1 — Route, Initialize & Open Sidecar (§1)

Explore read-only if unclear, resolve Orca via `ORCA_CLI_COMMAND` (`orca` / `orca-dev` / `orca-ide`), fetch `<ORCA> skills get orchestration > /tmp/orchestration-guide.md`, and pick one route/channel:
- **Channel A (Direct)**: Q&A, verbal status, small research without edits → answer directly.
- **Channel B (Single ticket / bounded stage)**: 1 worker in `--worktree new-top-level`.
- **Channel C (Long-horizon map)**: `to-tickets` → 1 integration worker running `implement-spec` on `--name <feature>-integration`.

Route families: `grill-me`, `grill-with-docs`, `wayfinder`, `research`, `prototype`, `to-questionnaire`, `wizard`, `to-spec`, `to-tickets`, `implement-spec`, `triage`, `diagnosing-bugs`, `improve-codebase-architecture`, `retro`.

```bash
SCRIPTS="<installed-skill-root>/orchestra-auto/scripts"
python3 "$SCRIPTS/init-check.py" check --repo "$PWD"
python3 "$SCRIPTS/flow.py" init-route --run-id "$RUN" --route full-chain --goal "..." --compact
python3 "$SCRIPTS/chain.py" resume --run-id "$RUN" --ledger-home "$ORCA_LEDGER_HOME" --compact
```

**Before the first dispatch** of every REAL Run, initialize and open the dashboard automatically (`python3 "$SCRIPTS/progress.py" --state "$STATE" init ...` then `open`); sidecar failures log a ledger gap and never block dispatch.

### Phase 2 — Write Spec & Run Preflight Gate (§4)

Choose `Pre-skill` by actual stage/ticket type (never by channel) and verify its `SKILL.md` path exists on disk. Include Ticket Type, Gates, `Test tier:` (for impl), the 5 elements, and `BEHAVIOR RULE`. Validate mechanically and record the `preflight` receipt in one command:

```bash
python3 "$SCRIPTS/preflight.py" check --run-id "$RUN" --ticket-id "$T" --type impl --gate G-impl \
  --origin "$ORIGIN" --spec /tmp/spec.md --guide /tmp/orchestration-guide.md \
  --guide-fetched-at "$TS" --round-started-at "$ROUND_TS" --channel-note "channel B" \
  --worktree-note "new-top-level" --long-horizon-note "single" --main-rule-note "clean" \
  --canonical-note "worktree" --legacy-migrated --migration-ref "native" --compact
```

Non-zero exit blocks dispatch (`message-capability` is non-waivable). Check fork base refs (`orca repo show`) before dispatching.

### Phase 3 — Dispatch, Wait & Handle Messages (§4.5 / §5)

1. Append ledger `intent` (`ledger.py append`) → run `<ORCA> orchestration worker-start --spec ... --worktree new-top-level --name <slug> --setup run --agent <agent> --retry-request <id> --json` → append ledger `result` → record `progress.py` & `preflight.py post-dispatch --compact`.
2. Confirm start evidence (`turnStart: observed` or terminal/worker-read check per reference); end turn and wait for Orca's native wake.
3. Classify every worker question before replying (`reply --id <msg>`): pause decision-class items for the human; answer detail-class locally; freeze only the blocked ticket.
4. On silence, stalled rounds, or failed `worker-start`: run `python3 "$SCRIPTS/health.py" collect --run-id "$RUN" --evaluate --compact` or `ledger.py status --run-id "$RUN" --compact`; follow recovery procedures before any retry or release.

### Phase 4 — Verify, Reconcile, Merge & Release (§6 / §7 / §9)

1. Verify gate deliverables (`git status`/`diff --numstat` reflow check, `Test tier:` red→green logs, two-axis `code-review`, or `visual-evidence.py` report; re-verify PR `Door` against actual diff). Persist `acceptance.py record`. Send back concretely (2 consecutive same-gate failures on one ticket → ask user; 5 cumulative send-backs → stop dispatching).
2. Merge authorized two-way green PRs (`§6.5`), fast-forward local main (or upstream for forks, `§6.5.1`), and reconcile tracker states after merge. Leave one-way or PR-only requests open and log `pending_type: human_merge`.
3. Close out & release: persist `python3 "$SCRIPTS/closeout.py" reconcile --run-id "$RUN" --live --answer-location "$ANS" --compact`. Run `worker-release` **only** after settled `worker-show` and durable closeout (failures follow `failure-record` → `failure-plan` → `worker-release`).
4. Deliver structured report (`stage`, `verified evidence`, `Q -> A log`, `next action` / pending one-way doors). On harness/process errors, run `retro` read-only outside the repo.

## Reference Index (Read On Demand by Task Stage)

| Reference | When to Read |
| :--- | :--- |
| [references/routing-and-chains.md](references/routing-and-chains.md) | Choosing among the 14 routes, `init-check.py`, `flow.py init-route`, `chain.py resume` / `exit-check` |
| [references/preflight-and-spec.md](references/preflight-and-spec.md) | Worker spec / slim `BEHAVIOR RULE`, `preflight.py` (§4.0.1–§4.0.3), planning gates (`G-docs`/`wayfinder`/`spec`/`tickets`), 6-clause prose contract |
| [references/dispatch-and-comms.md](references/dispatch-and-comms.md) | Worktree/fork flags, `turnStart` recovery, `ORCA_TERMINAL_HANDLE`/`SESSION_ID`, `orchestration ask`/reply rules, legacy `inbox-loop` migration |
| [references/acceptance-and-closeout.md](references/acceptance-and-closeout.md) | `G-impl` / `G-merge`, authoritative one-way-door trigger list, 4-level `acceptance.py`, 10-row `closeout.py`, escalation/blocker reporting |
| [references/long-horizon.md](references/long-horizon.md) | Multi-ticket `implement-spec` integration branch model, frontier isolation, `pending` one-way doors, `retro` governance |
| [references/recovery-and-health.md](references/recovery-and-health.md) | Failed `worker-start` recovery (`failure-record`→`failure-plan`→`release`), `health.py` state machine/11 signals, reflow noise recovery |
| [references/ledger-and-dashboard.md](references/ledger-and-dashboard.md) | `ledger.py` (`status`/`reconcile`, W1–W3 windows, action matrix) and `progress.py` `open` / `progress_record()` / Protocol v2 checkpoints |
| [references/visual-evidence.md](references/visual-evidence.md) | Quiet Orca UI screenshot batches via `visual-evidence.py` (`restored`, `focusRestored`, no minimize / `set frontmost`) |
