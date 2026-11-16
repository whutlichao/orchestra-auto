# Failed Dispatch Recovery, Run Health, and Reflow Noise

**When to read**: when `worker-start` returns `state: failed`, when `health.py` reports a non-healthy state or a worker goes silent, or when a worktree shows multi-thousand-line reflow noise at teardown.

## 1. Failed `worker-start` Recovery (Strict Order)

Never unconditionally retry a failed `worker-start` or run `worker-release` from memory. Follow the strict sequence enforced by `closeout.py`: **classify & read evidence → settled read-back (`worker-show`) → durable `kind: closeout` (`failure-record`) → authorization (`failure-plan`) → `worker-release`**.

| Class (`classification`) | Evidence Criteria | Action |
| :--- | :--- | :--- |
| **Transient infrastructure** (`transient_infrastructure`) | Readiness/connection stage failed on timeout/busy/contended slot; re-read succeeds; no half-done work | Safe to re-dispatch **only after** step 2 & settlement closeout |
| **Setup fault** (`setup_fault`) | Setup hook fails or reports real error | Diagnose & fix hook cause; do not re-dispatch into broken setup |
| **Isolation / placement fault** (`isolation_fault`) | Worktree/branch/placement creation failed | Degrade (serial or per-ticket fallback); never retry broken placement |
| **Artifact already present** (`artifact_present`) | Worktree already holds real uncommitted work / deliverable | Preserve & verify under `G-impl`; never discard or re-create blindly |
| **Authority / configuration fault** (`authority_fault`) | Disabled/unconfigured agent, missing credential/permission, unreadable settings | Escalate immediately; never substitute another agent or borrow handles |

**Evidence bar before classifying:**
- Inspect `terminal show`, `git worktree list`, `terminal read`, `git -C <worktree> status --porcelain`. Note: terminals present + worktree present + setup exited with `tail: []` does **not** by itself prove a transient fault (it equally fits a dead/unrun hook — verify recovery guidance & actual hook behavior).
- **Re-running setup** requires all three conditions: **Idempotent** (safe second run), **Authorized** (installs/migrations/network inside round scope), **Side-effect aware** (pre-state recorded so effects can be attributed). **Who executes it**: the worker's own `--setup run` on fresh dispatch or the human if credentials/approval needed (coordinator never runs setup inside worker worktree).
- **Reconcile any uncommitted work** before touching or releasing the worktree. Read back settlement (`worker-show`) then run `closeout.py` strictly in order:

```bash
CLOSEOUT="$SKILLS_ROOT/orchestra-auto/scripts/closeout.py"
<ORCA> orchestration worker-show --dispatch <failed_dispatch_id> --json > /tmp/worker-show.json
<ORCA> orchestration worker-list --json > /tmp/worker-list.json

python3 "$CLOSEOUT" failure-record \
  --run-id <run_id> --dispatch-id <failed_dispatch_id> \
  --task-id <task_id> --attempt <attempt> --worktree <worktree> \
  --classification <classification> \
  --evidence-ref <receipt_or_terminal_ref> \
  --answer-location <answer_location> \
  --read-back /tmp/worker-show.json --compact

python3 "$CLOSEOUT" failure-plan --run-id <run_id> \
  --dispatch-id <failed_dispatch_id> --read-back /tmp/worker-show.json --compact
```

`failure-record` verifies `"recorded": true`, `"durable": true`, `"outcome": "failure"`, `"classification"`, `"evidence_refs"`, `"mapping"`, returning `"next_action": "authorize-release"` (failed writes spill to `closeout-pending/` and keep release refused). Only when `failure-plan` returns `"next_action": "release"` and `"allowed": true` (`read-settled` / `record-closeout` / `escalate` otherwise — release never turns unsettled into settled) may you run:

```bash
<ORCA> orchestration worker-release --dispatch <failed_dispatch_id> --json
```

Stop and ask the user if setup fails twice, fault is `authority_fault`, or the per-ticket breaker (2 consecutive failures) trips.

## 2. Run Health State Machine (`health.py`)

Collect live native-wake signals and evaluate in one step (`mode="native"` omits retired `inbox` / `unacked` / `waiter_slot` unless supplied, and requires `nested_roster` only for long-horizon runs; `--observation` stays supported for historical audits):

```bash
HEALTH="$SKILLS_ROOT/orchestra-auto/scripts/health.py"
python3 "$HEALTH" collect --run-id "$RUN" --evaluate --compact
python3 "$HEALTH" retry --run-id "$RUN" --compact
```

**Five states & worst-of precedence (`lost > blocked > unknown > degraded > healthy`):**
- `healthy`: all active signals inside window.
- `degraded`: non-critical stale signal, `WORKER_WATCH=0` (`watch_disabled` → periodic `worker-list`), or stale projection; orchestration may continue.
- `blocked`: pending decision / one-way door / approval / `agentWait` object.
- `lost`: positive proof worker terminated without `worker_done` (normally transitioned from `unknown` on expiry + termination evidence).
- `unknown`: missing/expired/unverifiable evidence — **never `healthy`** and never failure; next step is `collect_evidence`.

| Signal | Source | Default Window | When Unhealthy |
| :--- | :--- | :--- | :--- |
| `heartbeat` | Worker / ledger (`last_heartbeat_at`) | preamble cadence × 2 + 60 s grace (optional unless positive cadence in preamble) | `degraded` if positive liveness else `unknown` |
| `inbox phase` | Historical `state.json` (retired loop only) | waiting / checking / backoff 15 min; `error` immediate | `degraded` / `blocked` |
| `unacked batch` | Legacy delivery vs ack receipts | every checkpoint | `blocked` |
| `worker-list snapshot` | Orca `worker-list` | 5 min while active workers > 0 | `degraded` |
| `worker-show observation` | Orca `worker-show` | 5 min | `degraded` |
| `turnStart` | Receipt / observation | 120 s window; escalate after 10 min unproven | `unknown` → escalate |
| `progress freshness` | `progress.json` | 300 s | `gap` only (never raises state) |
| `last effective progress` | Ledger / non-heartbeat markers | 3 consecutive non-progress rounds (heartbeats ignored) | `degraded` (`no_effective_progress`) |
| `reclaimable terminal` | Orca `worker-list` | > 10 min past settlement | `degraded` |
| `waiter-slot leak` | Legacy reinstall check | every reinstall | `degraded` / `blocked` |
| `nested roster` | Long-horizon first report | required when `long_horizon: true` | `unknown` → `degraded` |

Note on `observation.agentWait` (`worker_show.agent_wait`): `null` means Orca checked and no wait exists; an object means `blocked` on a decision; absent means `unknown` (Orca never checked). Failed health ledger writes persist to memory and `health-pending/` (`health.py retry`), never blocking orchestration. **Release is not a health state**: `health alone never authorizes it` — settlement + closeout do.

## 3. Formatter Reflow Noise Recovery

An abnormally large `git diff --numstat` (e.g., thousands of lines wrapped at ~80 columns) is an investigation signal, never permission to `git checkout -- .`. Before restoring anything, export evidence outside the repo (`git status --porcelain=v1 -uall`, `git diff --binary > worktree.patch`, `git diff --cached --binary > index.patch`, `git ls-files --others --exclude-standard > untracked.txt`; note non-empty staged index does not mean unstaged hunks are pure noise). Have the worker reconcile file by file from edits/commits, backing up every real change before restoring noisy files. Only if the worker's active tooling truly forces formatting reflow against repo style, add a locally excluded override (`{"format":{"enabled":false}}` in worktree root, excluded via `.git/info/exclude` — never committed).
