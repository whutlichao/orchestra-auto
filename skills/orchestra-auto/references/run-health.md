# Run health

**When to read**: after every wake, before you report a Run's state to the user, and when a
worker goes quiet. Run one `health evaluate` per wake, fed by the one observation that wake
carried; the inbox loop's end is the wake, so no resident checker is required and a recovery
turn rebuilds the state from the ledger.

Health answers exactly one question: **what can this Run prove right now?** It never answers
"is the work good?" — that is acceptance — and it never authorizes a release.

## The five states

| State | Meaning |
| --- | --- |
| `healthy` | every expected signal is fresh within its window and no action is required |
| `degraded` | a non-critical signal is stale, watch is off, or the projection is behind; work may continue and the reason is recorded |
| `blocked` | a human or coordinator decision is pending (one-way door, escalation, approval, head-of-line batch); nothing advances automatically |
| `lost` | positive proof the agent exited without a `worker_done`, or contact is lost with positive termination evidence |
| `unknown` | evidence is absent — never observed, TTL expired without positive evidence, liveness unverifiable. It is neither healthy nor failed; the next step is collecting evidence |

**Aggregation is worst-of**: `lost > blocked > unknown > degraded > healthy`. A single
`unknown` signal outranks any number of `degraded` ones, and an unobserved signal is `unknown`,
never `healthy`. A missing field is never a pass.

## Signals and windows

Every window below is a **configurable default**, not a product requirement. `health evaluate`
accepts `--config <file>` with a JSON object of overrides; the values are documented so a
reader can reason about them, not so a test can depend on one constant.

| Signal | Source | Window | When it fails |
| --- | --- | --- | --- |
| heartbeat | worker message / ledger (`last_heartbeat_at`) | preamble cadence × 2 + 60 s grace | `degraded` with positive liveness, else `unknown` |
| inbox phase / `updatedAt` | the inbox loop's `state.json` | waiting / checking / backoff 15 min; `error` immediately | `degraded` when stale, `blocked` on error |
| unacked batch | delivery files versus ack receipts | every checkpoint | `blocked` |
| worker-list snapshot | Orca `worker-list` | 5 min, only while workers are active | `degraded` |
| worker-show observation | Orca `worker-show` | 5 min | `degraded` |
| turnStart | receipt / observation | 120 s window; escalate after 10 min unproven | `unknown` → escalate |
| progress freshness | `progress.json` | 300 s | an observability gap only |
| last effective progress | ledger / messages, or the inbox observation | 3 rounds without progress | `degraded` |
| reclaimable terminal | worker list | every closeout; older than 10 min | `degraded` |
| waiter-slot leak | process / lock | every reinstall | `degraded`, or `blocked` when it prevents the install |
| nested roster | worker report | first report of a long-horizon ticket | `unknown` → `degraded` |

Three rules in that table are easy to get wrong:

- **Heartbeats.** The threshold is the *live preamble cadence* × 2 plus grace. When the preamble
  carries no cadence the heartbeat is **not evaluated** and the signal is `unknown`. Never invent
  five minutes: a guessed threshold turns a quiet worker into a false alarm, or a dead one into a
  green Run.
- **turnStart.** `turnStart: observed` is evidence the turn began and is accepted. `unsupported`,
  `unobserved` or an absent field is `unknown` — never a failure. Inside the window gather the
  evidence the agent supports (terminal title or screen, `worker-read`, status projection,
  artifacts) and do not fail the dispatch; after it, escalate.
- **`observation.agentWait`.** A present `null` is a positive observation: Orca looked and found
  no wait. An object means the worker is waiting on a decision, which is `blocked`. An **absent**
  field means Orca never looked, which is `unknown`. A waiting worker is healthy — resting after
  a wake is not failure, and `ready` is not a started turn. In a **signals snapshot** the same
  fact is the key `worker_show.agent_wait`: `null`, an object, or absent, with those meanings.
  The snapshot's other keys are `worker_show.status` (and `worker_done`), `heartbeat.cadence_seconds`
  with `last_heartbeat_at`, and `observed_at` per block — the whole shape is the local contract
  the CLI reads, never a claim about a live Orca receipt.

## The observation the inbox loop mounts

**The wiring.** The inbox loop writes exactly one lightweight observation per wake — one per
wake, never more and never none — into its own `state.json` (`wake_observation`) and publishes
the same object as the `observation` field of the wake line. The coordinator transcribes it into
the ledger as an observation and runs one `health evaluate` against it. Nothing is resident: the
loop observes, the coordinator evaluates, and a recovery turn rebuilds the same state from the
ledger alone.

The observation's fields, and what each one means for health:

| Observation field | Becomes | Meaning |
| --- | --- | --- |
| `observed_at` | the snapshot's `observed_at` | when the loop looked; every age is measured from it |
| `saved`, `unacked`, `unacked_delivery_ids`, `blocked` | `unacked` | the same `--status --json` arithmetic; a nonzero count is `blocked` |
| `watch` | `watch` | `off` is `degraded` with `watch_disabled`, never healthy |
| `phase`, `error` | `inbox` | `error` is `blocked` immediately; a resting wake (`phase: wake`) maps no inbox block, because it is not a live phase |
| `binding` | `binding` | the binding-loss record, below |
| `last_effective_progress` | `last_effective_progress` | the stall signal, below |

Every signal the observation does **not** carry — heartbeat, `worker-show`, `worker-list`,
`turnStart`, the projection, reclaimable terminals, waiter slots, the nested roster — reads
`unknown`. That is deliberate: one consumer cannot prove a Run healthy, so the evaluation of an
observation alone says "collect the rest", and the coordinator supplies those blocks from its own
reads before it can reach `healthy`.

```bash
python3 "$HEALTH" evaluate --run-id "$RUN" --observation observation.json
```

`--observation` is the same command as `--signals`, with the translation above applied first;
`--signals` takes a full snapshot and stays the way to evaluate a hand-built one. A run id in the
observation that disagrees with `--run-id` is refused rather than merged.

## Stall detection

`last_effective_progress` advances only on **effective** progress: a non-heartbeat message, an
artifact change, a commit, or a stage change. A **heartbeat never refreshes it** — a heartbeat
proves the agent is awake, not that the work moved, and a Run that only heartbeats is exactly the
stall this signal exists to catch.

- `rounds_without_progress` counts consecutive rounds (wakes) with no effective progress. Three
  consecutive rounds is `degraded` with `reason: no_effective_progress`; the limit is
  `stall_rounds` in the config.
- `markers` names what progressed in the last round, in the shared vocabulary `message`,
  `artifact`, `commit`, `stage`. A heartbeat that arrives is reported under
  `ignored_markers`, not silently kept.
- Three empty waits keep the existing rule and are counted in `empty_waits`; a batch that arrives
  resets the streak.
- **In consumer mode** (`mode: consumer`) the block must also carry the `--status --json`
  `unacked` count: a consumer cannot see an empty wait by reading files, so empty waits are
  invisible to it and the count is part of the same signal. A consumer-mode block without it is
  `unknown` (`consumer_unacked_missing`), never a pass. An empty wait is therefore never read as
  invisibility: it is counted, published, and evaluated with the unacked head.

## Binding loss

`consumer_fenced` or a lost Run binding stops consumption and lifecycle operations for that
identity. The loop records the outcome, the error text and the two refusals — never impersonate a
handle, never create a new Run unconditionally — as `binding` in its state and in the observation's
`binding` block, and the evaluator grades it:

| Recorded `binding` | Health | Next action |
| --- | --- | --- |
| `outcome: consumer_fenced` | `blocked`, `reason: binding_loss` | `escalate` while ownership is `unproven`, else `record_pending` |
| `outcome: run_binding_lost` with `ownership: verified` | `blocked`, `reason: binding_loss` | `record_pending` — re-establish the binding, not the Run |
| `outcome: run_binding_lost` without a verified owner | `unknown`, `reason: ownership_unobserved` | `escalate` |
| `outcome: none` (checked, no loss) | contributes nothing | continue |

An ownership the consumer cannot establish is an escalation, never a guess: the loop records
`ownership: unproven` because verifying the real caller is the coordinator's read, not the
consumer's. A binding loss is never a retry trigger, and no block is an observation — the Run's
other signals still decide the aggregate.

`progress.json` may project `run.health`, but the ledger holds health authority. A stale
projection is recorded as a gap and never raises the state.

## Transitions and authorization

| Transition | Trigger | Authorization |
| --- | --- | --- |
| healthy → degraded | non-critical stale signal, watch off, dashboard gap | rule |
| degraded → healthy | signals recover | rule |
| any → blocked | pending decision, one-way door, escalation, approval, head-of-line batch | record the decision/pending item |
| blocked → healthy | decision resolved | human or coordinator |
| any → unknown | evidence absent, TTL expired, unverifiable | rule |
| unknown → lost | positive termination evidence | rule, on the evidence |
| lost → retry | explicit retry decision | coordinator, inside the human boundary |
| any → release | settled plus a closeout record | coordinator; uncertain goes to the human |

`lost` is normally reached from `unknown`: a TTL expires, evidence is collected, and the
evidence decides. What authorizes `lost` is the **evidence**, not the previous state, so a
proven termination is never hidden behind an earlier green verdict. **Release is not a health
state.** It is an action whose authorization needs settlement and a durable closeout record;
health alone never authorizes it, and an uncertain settlement is the human's decision.

## The coordinator surface

```bash
HEALTH="$SKILLS_ROOT/orchestra-auto/scripts/health.py"
RUN=run_example
python3 "$HEALTH" evaluate --run-id "$RUN" --signals signals.json
python3 "$HEALTH" evaluate --run-id "$RUN" --observation observation.json
python3 "$HEALTH" evaluate --run-id "$RUN" --signals - --config windows.json
```

The command prints the machine result:

```json
{
  "schema_version": 1,
  "run_id": "run_example",
  "record_id": "…",
  "recorded": true,
  "state": "degraded",
  "reasons": [{"signal": "watch", "state": "degraded", "reason": "watch_disabled",
               "evidence": "WORKER_WATCH=0: wakes are not observed; poll worker-list periodically",
               "next_action": "poll_worker_list"}],
  "next_action": "poll_worker_list",
  "projection": {"run_id": "run_example", "run": {"health": "degraded",
                                                  "next_action": "poll_worker_list"}}
}
```

- `reasons` holds one entry per finding: the signal, the state it contributes, a stable reason
  code, the evidence and any detail. Healthy signals are omitted.
- Exit `0` even for `unknown`, `blocked` or `lost`: those are observations, not command
  failures. Exit `1` is invalid input (bad JSON, an unknown signal or config key, and invalid
  record). A usage error exits `2`.
- The command writes a `kind: health` ledger record with
  `{state, entered_at, reason, signals, evidence_refs, decided_by, next_action}` under
  `detail`. The record is derived from the snapshot, so the same snapshot yields the same
  `record_id` and a re-run is idempotent.
- Each call compares against the previous health record for the Run and prints
  `previous_state` and `transition` (`from`, `to`, `allowed`, `authorization`). While the state
  does not change, `entered_at` keeps the instant that state was entered, so "degraded since"
  is answerable. A transition the table does not authorize is recorded with
  `allowed: false` rather than silently accepted.
- `blocked` from an earlier state needs a recorded pending item; the evaluation itself is that
  record — a blocked finding (a pending item, an unacked batch, an inbox error, a waiting agent)
  is evidence, and the record carries it.

### When the record cannot be written

A failed health-record write **never blocks orchestration**. The evaluation is printed and kept
in memory, `recorded` is `false`, and `record_error` names the cause. Retry the same evaluation
with the printed id once the ledger is writable:

```bash
python3 "$HEALTH" evaluate --run-id "$RUN" --signals signals.json --record-id <printed id>
```

The health record is `replay_safe` by `record_id`: a retry never duplicates it. Because the
record persists in the ledger, a restarted coordinator reads the last state instead of starting
from nowhere. `progress.json` stays a projection throughout — it may show the state and must
never be read as authority.

## `WORKER_WATCH=0`

Watching is allowed to be off, but the Run is then `degraded` with `reason: watch_disabled` and
`next_action: poll_worker_list`, and the alternative is a periodic `worker-list` poll. It is
never reported as healthy: with no wakes there is nothing that proves the Run is alive.

## Reading the verdict

- `lost` or `blocked`: stop and make the decision the state names. A retry after `lost` is a
  coordinator decision inside the human boundary, not a rule.
- `unknown`: collect the missing evidence before doing anything else. Absence is not proof, and
  an unobserved signal is never a green light.
- `degraded`: continue, and record the reason. Work may proceed.
- `healthy`: continue. It says the signals are fresh, not that the work is acceptable; acceptance
  and release remain their own gates.
