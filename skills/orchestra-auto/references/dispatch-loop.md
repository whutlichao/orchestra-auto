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

- `--worktree new-top-level`: an isolated top-level worktree, cut from the base ref chosen below.
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
  native wake contract.

**Base ref: read the fork state once per repository, before the first dispatch into it.** The
fact is already recorded by Orca; the skill only has to read it:

```bash
orca repo show --repo <selector> --json            # result.repo.upstream is null when not a fork
orca repo search-refs --repo <selector> --query <default branch> --json
```

- `upstream` **null** (a repository of your own): keep today's behaviour. The worktree is cut from
  the repository's default base ref and no `--base-branch` is passed.
- `upstream` **non-null** (a fork): run `git fetch upstream` first, cut from
  `upstream/<default branch>`, and pass `--base-branch upstream/<default branch>` explicitly.
  Without it the fork's private main commits ride into the PR as a gap the upstream repository
  never had — the worktree's branch is `upstream`'s common ancestor plus everything the fork
  stacked on its own main plus this ticket.
- The judgement is made by **delivery endpoint** — which repository the PR merges into — not by
  the size of the change or the ticket type. It is a repository-level fact, settled once, not a
  per-ticket improvisation; later claims about the same repository must agree with it.
- A ticket that genuinely depends on the fork's private commits is the one exception: pass
  `--base-branch origin/<default branch>` and write down the reason.

**A fork's default branch stays a pure mirror.** It should only mirror upstream's default branch;
private work goes on its own branch or PR and is never stacked on the fork's main. Under that
convention §6.5.1's `--ff-only` sync holds unconditionally. A fork that already accumulated
private commits clears them first — merge them upstream or drop them — instead of picking a base
ref that hides them for another round.

**A stale coordinator worktree is refused before dispatch.** Check its lag against the target base
first — in a fork `git fetch upstream`: the coordinator tracks `origin/main`, the worker cuts from
`upstream/main`. Past a threshold Orca refuses and the Task stays `ready` with no Dispatch,
looking like a worker that never started. Gather §9 evidence (`worker-list --run` `stage`/`outcome`,
empty receiver), pull/rebase or recreate it, never re-dispatch. `allow-stale-base: true` only
for expected drift, reasoned in the spec (Orca 1.4.245: 20 commits; version-scoped — CLI/guide).

## 4.3.0 Pre-skill: pick it by stage and ticket type, not by channel

The `Pre-skill:` line in the spec names the skill of the stage you are actually running. There is no
fixed channel-to-skill mapping: a round that already holds a valid spec enters at `to-tickets`, one
with an existing ticket map enters at `implement-spec`, a research question answered on disk uses
`research`, and a bug fix uses `diagnosing-bugs` and then `implement`.

| This round's stage | Pre-skill |
| --- | --- |
| Explanation, status, discussion | none (channel A), unless an interactive skill is requested |
| Explicit non-code interview | `grill-me` → `grilling` |
| Bounded change, bug fix, existing spec or ticket | `implement` (`diagnosing-bugs` first for a bug) |
| Research that lands on disk | `research` |
| A design made sharp against the codebase | `grill-with-docs` |
| Large foggy effort spanning sessions: chart or work a decision map | `wayfinder` |
| A decision needing a runnable throwaway to react to | `prototype` |
| A spec | `to-spec` |
| A ticket split | `to-tickets` |
| Implementation of a complete map | `implement-spec` |
| External issues, conflicts, terminology | the repository's triage / conflict / `domain-modeling` flow |
| The repo has no bootstrap yet | land `setup-matt-pocock-skills` first, then enter the matching stage above (SKILL.md §1.1) |

Before dispatch, confirm the chosen path is readable and that this stage's dependencies exist: the
tracker configuration for `to-spec` / `to-tickets`, the map and tracker for `implement-spec`, and the
skills it will reach for any of them. Missing → report per §9 with the resolution that failed. Do not
substitute a path from memory, and do not install anything this round was not authorized to install.

Prove the gate's checks on the spot; any one missing means uninitialized: an `## Agent skills` block in
`AGENTS.md` or `CLAUDE.md` (by setup's own selection rule — when `CLAUDE.md` exists, edit it and never
create a second file); a non-empty `docs/agents/issue-tracker.md` and `docs/agents/domain.md` (plus
`docs/agents/triage-labels.md` when `triage` is installed); and, for a GitHub / GitLab tracker,
`gh repo view` / `glab repo view` printing the project from inside the target repo. These three
criteria are exactly what `scripts/init-check.py check` reports, next to a `repository` probe that
establishes which root they are read from: exit `0` initialized, `1` uninitialized
(one criterion is missing), `2` unknown (the probe could not decide). Take the evidence from the script,
and read `unknown` as undecided — never as uninitialized; SKILL.md §1.1 gives the per-exit handling.
`setup-matt-pocock-skills` carries `disable-model-invocation: true`, so explicitly name it and its path —
never expect it to show up in a skill list on its own. It asks one question at a time (tracker, triage
label vocabulary, domain doc layout); handle those answers per SKILL.md §5. Its output is governance
documentation and lands through a worker with initialization artifacts verified, never on main.
The grill-specific G-docs criteria do not apply to setup.

## Entry evidence

Before selecting an entry, establish the evidence for the user's goal, observable behaviour,
scope, acceptance and key choices. For unresolved trade-offs, use `wayfinder` if the effort spans
sessions and the route is foggy; otherwise use `grill-me` for a non-code interview or
`grill-with-docs` for codebase/domain alignment. Record which decisions need the user. Enter at an existing spec or ticket
only after checking that it applies to this request, is current and resolves those questions. For
a small change or fix entered directly at implementation, record the evidence for scope,
acceptance and settled key choices in the mode declaration; re-route when exploration reveals a
new fork. The absence of a spec is not a reason to skip grill. When the user explicitly asks for
grill, do it even for a small change.

`grill-me` wraps `grilling`; `grill-with-docs` invokes both `grilling` and `domain-modeling`.
Resolve and pass all required skill paths to a worker; reading a wrapper alone is not evidence its
dependencies ran. Preserve the user's questions, answers and constraints across fresh contexts.
`grilling` and `prototype` require the user's real decisions or feedback. Respect host restrictions
on user-invoked entrypoints; if a required skill is unavailable, report the failed resolution.

For `wayfinder`, specify **chart** or **work** and pass the destination or map pointer and tracker
operations. Chart grills the destination and breadth, creates the map and specifiable decision
tickets, wires blockers, starts research where authorized, then stops. Work claims a frontier
ticket, resolves it by type and updates the map; at most one non-research ticket per session.
Research is AFK, prototype and grilling are HITL, task is HITL or AFK depending on the blocker.
Decision tickets differ from implementation tickets. Validate each invocation and the completed
map separately under G-wayfinder. Research and prototype may use throwaway branches and context
pointers without a project PR when authorized; do not apply the implementation PR contract to a
disposable artifact.

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

## 4.5 Communication loop

Read [communication and recovery](inbox-and-waiting.md). Keep an out-of-repo ledger of Run, Task, Dispatch, worker, message IDs, handling states, evidence and PR. Verify turn start after dispatch, then end the turn for Orca's native wake. Handle each whole message or batch, deduplicate by message ID and reconcile uncertain effects before replay. Only the agent consumes its own inbox; never start the retired consumer or a background consuming check. Recover lost context via the documented inbox and retry commands. Investigate silence using worker-list, worker-show, worker-read and artifacts; silence alone does not establish termination.

Progress dashboard: initialize and open it before the first dispatch of every actual Run, then follow
the checkpoint ownership table in [progress dashboard](progress-dashboard.md) after the normal action.
Record explicit facts and structured task/dispatch/attempt reports; recording errors warn and never
block this workflow.
