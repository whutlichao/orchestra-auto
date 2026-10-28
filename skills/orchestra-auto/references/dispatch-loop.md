# Dispatch loop: guide fetch, worktree override, turn start, communication loop

**When to read**: for every B/C dispatch, for the full worker-start flags, turn-start confirmation
narrative, orchestration-guide fetch, or communication-loop verification detail beyond the short
SKILL.md stubs.

## 4.2 Fetch the orchestration guide (once, to disk)

```bash
<ORCA> skills get orchestration > /tmp/orchestration-guide-<repo>.md
```

Grep the on-disk file afterwards. Do not repeatedly `head -120` / `sed -n '250,420p'` the same
output — that burns dozens of tool calls for nothing (one session spent several dozen on it). It
lives in `/tmp` only (§2). Pull `--reference references/<file>.md` when you hit a
specific action gate.

## 4.3 ★ Overriding Orca's default placement (the single most important rule here)

**Orca's default puts a worker in the current (= main) workspace** — "a fresh worker means a
fresh agent terminal, **not** a new Git worktree". That directly contradicts §2's hard rule, and
**orchestra-auto explicitly overrides Orca's default here.**

**Project binding comes from the calling Orca context, not the shell's cwd.** The coordinator is
normally standing in the target repo, but if the session cwd differs from the target repo
(working against a fixture, or dispatching into another repository) you must add an explicit
`--repo <selector>`, otherwise the worktree is created inside whatever project the cwd belongs
to. This one was learned the hard way.

Every task worker, without exception:

```bash
<ORCA> orchestration worker-start --spec "<spec>" \
  --worktree new-top-level --name <ticket slug> --setup run --agent <§0's worker> --json
```

- `--worktree new-top-level`: an isolated top-level worktree, cut from the default base ref.
  **Never `current`, never `new-child`** — the first is main, the second may land inside a
  subdirectory of main.
- `--name`: the ticket slug, so the worktree is recognizable later and locatable on a send-back.
- `--agent <§0's worker>`: **must be explicit; there is no "use the default" form** —
  `worker-start` raises `agent_unconfigured` when `--agent` is missing or unrecognized, because
  it does not read the default TUI agent (that default only serves the GUI composer path). So
  this column cannot be omitted, and its **value** comes from §0's worker column.

  Per-agent titles, permission prompts and TUI resume criteria are in the
  [start and recovery reference](worker-start-and-recovery.md); read it only when the
  receipt does not confirm the turn started or you hit an interactive prompt.

- Judge whether an agent is usable from the live **disabled list** read in §0, not from `which`;
  Orca may recognize an agent that the current profile still disables. The live settings read is
  the only configuration criterion; the dated observations in this table only explain verified
  start behaviour and must not be used to infer current defaults or availability.
- `--spec` in one shot (`worker-start --spec` creates the Task and the attempt together). Reach
  for `task-create` + `worker-start --task <id>` only when several tickets have dependencies,
  when you need a planned fan-out, or when retrying a known Task.
- Immediately after the receipt, confirm the turn really started per §4.3.1, and verify §4.5's
  inbox consumer and waiting method.

## 4.3.0 Pre-skill: pick it by stage and ticket type, not by channel

The `Pre-skill:` line in the spec names the skill of the stage you are actually running. There is no
fixed channel-to-skill mapping: a round that already holds a valid spec enters at `to-tickets`, one
with an existing ticket map enters at `implement-spec`, a research question answered on disk uses
`research`, and a bug fix uses `diagnosing-bugs` and then `implement`.

| This round's stage | Pre-skill |
| --- | --- |
| Explanation, status, discussion | none (channel A) |
| Bounded change, bug fix, existing spec or ticket | `implement` (`diagnosing-bugs` first for a bug) |
| Research that lands on disk | `research` |
| A design made sharp against the codebase | `grill-with-docs` |
| A spec | `to-spec` |
| A ticket split | `to-tickets` |
| Implementation of a complete map | `implement-spec` |
| External issues, conflicts, terminology | the repository's triage / conflict / `domain-modeling` flow |

Before dispatch, confirm the chosen path is readable and that this stage's dependencies exist: the
tracker configuration for `to-spec` / `to-tickets`, the map and tracker for `implement-spec`, and the
skills it will reach for any of them. Missing → report per §9 with the resolution that failed. Do not
substitute a path from memory, and do not install anything this round was not authorized to install.

## 4.3.1 Confirming the turn started after dispatch

`state: ready` only means the Dispatch is ready. The receipt's `turnStart: observed` **is** the
start evidence: accept it and go on to the wait — the steps below are for the receipts that cannot
carry it. If `turnStart` is `unsupported`, `unobserved`, or absent, read the [start and recovery
reference](worker-start-and-recovery.md) and confirm the start with evidence that agent actually
supports. Orca allows workers without a terminal: check `worker-show` for the handle first, and
never force `terminal show` onto a worker that has no terminal. An unchanged idle title is not a
verdict either way: one bare Enter is allowed only when the screen shows the complete preamble in
the composer with no turn running. If you cannot prove it, gather evidence per §9 rather than
entering a blind wait. The checklist form of this step is §4.0.2 of
[dispatch preflight](dispatch-preflight.md).

### 4.3.2 Long-horizon dispatch (one worker per map)

When G-tickets accepted a complete map, start **one** worker for the whole map:
`--name <feature>-integration`, with `implement-spec` as the pre-skill and the `Tracker:`,
`Spec:`, `Tickets:` and `Integration branch:` pointers from
[long-horizon tickets](long-horizon-tickets.md). It must prove it can create isolated nested
worktrees before spending the map; if not, it runs implementers serially, or the coordinator falls
back to one worker per ticket (default cap 3). The coordinator accepts the single integration PR,
not each nested branch, and never enters a nested worktree.

## 4.5 Communication loop: enqueue, collect, handle and wake are verified separately

Read the [communication and recovery protocol](inbox-and-waiting.md) before
dispatching. Keep one ledger per Run outside the repository: Run/coordinator handle,
Task/Dispatch/worker handle/worktree, last progress, message IDs, handling state, completion
receipt, PR, next check time. After context compaction or recovery, read the ledger and the
real Orca state first; never guess identity from an old conversation.

- Prepare a single inbox consumer and a waiting method before the first worker starts; after
  every dispatch confirm the turn started and check the consumer.
- One consumer per Run, bound to the real coordinator handle; messages are saved
  atomically by deliveryId, a restart never overwrites, and unhandled evidence is never rotated
  away. The consumer is also the wake: it ends the process, with one line on stdout, when a new
  non-heartbeat batch or a worker-state change arrives (§3 of
  [inbox and waiting](inbox-and-waiting.md)), so install it as a call the harness surfaces and
  install it again after every wake.
- Deduplicate every message by message ID and verify its Run/Task/Dispatch. Acknowledge only
  after the whole batch and any needed terminal-ownership decisions are handled. When the
  current batch is blocked on a human decision, use a read-only `check --all` to inspect
  following messages and keep handling other workers promptly; an ack is not a reply, and you
  must not ack with an unreviewed deliveryId lifted from a different check.
- Collect messages after long operations and while idle. A single foreground wait never exceeds
  60 seconds; after three rounds with no progress, inspect worker-list / worker-show /
  worker-read, the queue and the local ledger, and recover from evidence rather than waiting
  blindly.
- Unless the environment forces the turn to end, ending a coordinator turn with work still in
  flight requires a **confirmed** notification mechanism for the next turn. An unverified
  background process, a message file, or a successful `send` does not count. With no automatic
  wakeup, keep the current turn in bounded polling; when the environment must take control back,
  state explicitly what is still running, that you cannot be woken automatically, and where the
  recovery entry point is — never claim you will continue automatically, and never leave the
  user to prod you on a schedule.
- Recovery for a missing worker_done or an already-idle worker, plus error and stop conditions,
  follow the reference.

Optional progress: when enabled, follow the checkpoint ownership table in
[progress dashboard](progress-dashboard.md) after the normal action. Record explicit facts and
structured task/dispatch/attempt reports; recording errors warn and never block this workflow.
