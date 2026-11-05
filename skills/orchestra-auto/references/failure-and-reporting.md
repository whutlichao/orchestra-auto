# Failure and reporting discipline

**When to read this**: when the CLI will not start, a `worker-start` receipt says `state: failed`,
a circuit breaker trips, or the Orca app restarts mid-run.

## 9. Failure and reporting discipline

- If the CLI will not start or the orchestration guide cannot be fetched: report the exact error
  and stop. **Do not swap the executable and do not guess commands.**
- When a `worker-start` receipt says `state: failed`, gather evidence before deciding; do not
  relaunch blindly. Follow the [failed-dispatch recovery](failed-dispatch-recovery.md) procedure.
- When anything trips a breaker (per ticket: two consecutive failures of the same gate; run level:
  §6's cumulative send-back threshold) or an escalation-class issue (freeze that ticket, keep the
  rest going, §5.1) → stop and ask the human immediately, stating which stage you are stuck in,
  the evidence you have, and what the human needs to decide.
- **Orca restarts, or binding is lost**: confirm app status, inventory with `worker-list --include-remote`, and recover messages using the version-matched inbox/retry commands. Preserve the original identity and ledger; do not resend mutations or create another Run before reconciling their effects. A fenced or unverified identity stops lifecycle operations until ownership is established; preserve evidence and escalate if it cannot be established. See [communication recovery](inbox-and-waiting.md).

- Authorized implementation work is not skipped because the task is small or the repo lacks a
  `WORK_SCOPE` section; pure Q&A, reports and stages beyond this round's delivery endpoint are not
  dispatched. When the scope is unclear, explore read-only first and re-judge per §1.
- Advance later stages directly when they are inside this round's scope; when they are not, state
  their status and the next entry point in the report.
- Follow the repository's language for design terminology; use deep-module vocabulary when
  architectural analysis needs it.

Progress dashboard: initialize and open it before the first dispatch of every actual Run, then follow
the checkpoint ownership table in [progress dashboard](progress-dashboard.md) after the normal action.
Record explicit facts and structured task/dispatch/attempt reports; recording errors warn and never
block this workflow.
