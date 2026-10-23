# Worker start and TUI waiting

**When to read this**: `worker-start` did not confirm `turnStart: observed`, or the worker shows a
permission prompt or no progress for a long time. The current Orca `worker-show`, `worker-read`
and recovery guides are authoritative; the title observations below apply only to TUI workers that
have a terminal.

## Confirming the turn

`state: ready` / `stage: input_accepted` does not prove the model has started. `turnStart: observed`
does: it is the receipt's own start evidence, so nothing extra is needed on top of it — the steps
below are for `unsupported`, `unobserved` or a missing field, where the agent cannot report a turn
start. First use `orca orchestration worker-show --dispatch <id> --json` to see the actual worker and
its terminal handle. A worker without a terminal is judged from `worker-read --source auto`, the
status projection and artifact progress — do not call terminal commands for it.

With a terminal, read `orca terminal show --terminal <handle> --json`. Idle banners such as
`Pi ready` / `OMP ready` only mean there is no start evidence yet; `opencode` exposes an
`OC | <title>` shape, and a `codebuddy` title may stay `Terminal 1` forever, so you need screen or
session-file progress. Title shapes change between versions, so a single title flip is not proof
that work has finished. An unchanged idle title is a signal to look at the screen, not a verdict:
it neither proves the turn began nor proves it did not.

If the screen confirms the complete preamble is parked in the composer and no turn is currently
running, you may send **one** bare Enter to the **original terminal** and re-check the state after
it. When the composer is empty, the content is incomplete, or the state is unclear, gather evidence
per the Orca recovery guide: do not resend the whole spec, do not loop on Enter, and do not create a
duplicate Dispatch.

## Permission prompts and lost contact

When `agentWait` is set or the screen shows a permission dialog, read the specific operation being
requested, the target path and the risk. Handle only operations authorized for this round; when
extra permission, a third-party account, payment or an irreversible operation is involved, ask or
escalate per §5. Never treat Enter as a universal approval, and never switch to a bypass mode just
to avoid the prompt. Re-check `agentWait` and worker progress after handling it.

When the title returns to idle while the Task is unsettled, judge from the worktree's progress,
`worker-show`, `worker-read` and subtask artifacts whether you lost the wakeup. Only when the
recovery guide confirms the terminal still belongs to the original active Dispatch and recovery is
allowed should you send one explicit resume instruction; if there is still no progress, preserve the
artifacts and report the blockage.
