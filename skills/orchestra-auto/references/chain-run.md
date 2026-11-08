# Chain positions, the explicit flow, resume and exit checks

**When to read this:** between two laps of the same Run, after a crash or an app restart, or
whenever the coordinator has to answer "where does this Run continue" from records instead of
from the conversation.

## Contents

- [The flow is the plan; the ledger is an index](#the-flow-is-the-plan-the-ledger-is-an-index)
- [Positions, not steps](#positions-not-steps)
- [Resume: the flow first, the legacy chain second](#resume-the-flow-first-the-legacy-chain-second)
- [Exit checks: the floor under an artifact](#exit-checks-the-floor-under-an-artifact)
- [Stop and ask, in one table](#stop-and-ask-in-one-table)
- [What resume does not do](#what-resume-does-not-do)

## The flow is the plan; the ledger is an index

A Run does not always chain five stages. It may be a design-only request, one settled ticket, a
wayfinder chart, a reused spec, or a direct small fix. The ledger records what was observed; it
cannot say which stages this Run chose, which gate applies where, or where the authorized
delivery endpoint is. **The explicit flow record is that plan**, and `resume` prefers it.

`scripts/flow.py` writes and reads it. The file lives outside every repository, one per Run:

```bash
python3 scripts/flow.py append --run-id <run_id> --record flow.json      # the plan, once
python3 scripts/flow.py append --run-id <run_id> --record state.json     # a node changed
python3 scripts/flow.py append --run-id <run_id> --record invalidate.json # revoke + downstream
python3 scripts/flow.py append --run-id <run_id> --record import.json    # a prior-Run artifact
python3 scripts/flow.py plan   --run-id <run_id>
```

The default location is `$ORCA_FLOW_HOME/runs/<run_id>/flow.jsonl`, falling back to
`~/.orca/flows`; `--flow` and `--flow-home` override it. `chain.py resume` looks for `flow.jsonl`
beside the ledger it was told to read, so one pointer is enough.

**Schema v1.** Every record carries `schema_version: 1`, `record_id` (idempotent), `kind` and
`flow_id`. An unknown schema version is refused rather than guessed. The four kinds:

| Kind | What it says |
| --- | --- |
| `flow` | the plan: `run_id`, `goal`, `endpoint`, and `nodes` with `id`, `gate`, `depends_on`, `applies` |
| `state` | one node's state: `pending`, `in_progress`, `waiting_user`, `blocked`, `accepted`, `not_applicable`, `invalidated` |
| `invalidate` | a `revocation`, `spec-change` or `artifact-update` that invalidates the named nodes and everything downstream |
| `import` | an external or previous-Run artifact, with `source_run`, `artifact`, `artifact_version` and `verification` |

An `accepted` node binds a `task_id`, a positive `attempt`, an `artifact_version` and an
`evidence_ref`: a claim with no evidence cannot be accepted. `not_applicable` needs the
`entry_evidence` that made it inapplicable, and `waiting_user` is not a failure. A node whose
`applies` is `false` needs that evidence in the plan itself.

The file is append-only and **order is the authority for one node**: a later state overrides an
earlier one, so a current failure is never hidden by an older success. An acceptance whose
attempt is older than what the node already saw does not override the newer state; it is
reported as a gap. An acceptance under a different `task_id` than the node's existing binding is
reported as `divergent`.

Old ledgers keep their meaning and are never rewritten. `flow.py` never reads or converts one.

For a wayfinder Run, the flow records **chart**, **work** and **map-complete** as nodes with
evidence pointers only; the map, its tickets, blockers and frontier stay the tracker's authority.
The local flow never restates a decision or becomes the map.

## Positions, not steps

The legacy chain is linear until it forks, so it is modelled as five **positions**. `implement`
and `implement-spec` are one position because they close on the same gate: one acceptance record
cannot say which of the two ran, and a resume that guessed would restart the chain in the wrong
shape.

| Position | Gate | Step(s) | Artifact that closes it |
| --- | --- | --- | --- |
| `grill` | G-docs | `grill-with-docs` | the settled change, its glossary terms and any ADR |
| `spec` | G-spec | `to-spec` | the spec published to the tracker |
| `tickets` | G-tickets | `to-tickets` | the ticket map with blocking edges |
| `build` | G-impl | `implement` **or** `implement-spec` | one settled ticket, or the spec's integration branch |
| `review` | G-merge | `code-review`, then the merge | the two-axis review and the merge |

The gate is the authority; the position is only a label over it. `chain.py steps` prints this
table as JSON so a coordinator never has to re-derive it from prose.

## Resume: the flow first, the legacy chain second

```bash
python3 scripts/chain.py resume --run-id <run_id>                  # flow beside the ledger, else legacy
python3 scripts/chain.py resume --run-id <run_id> --flow /path/flow.jsonl
python3 scripts/chain.py resume --run-id <run_id> --ledger /path/to/ledger.jsonl  # legacy
```

**When a flow record exists**, the answer is the flow's, and the ledger is not consulted:
`{"state": "continue", "ready": [{"node": "spec", "gate": "G-spec"}], ...}`,
`{"state": "complete", "ready": [], ...}` or `{"state": "uncertain", "ready": [], "evidence_gaps": [...]}`
(exit `1` for `uncertain`). `waiting_user`, `blocked` and `invalidated` are listed separately so a
human-blocked node is never read as a failed one. `complete` covers the authorized endpoint only
and is **not** merge authorization.

**Without a flow record**, `resume` keeps answering from the ledger exactly as before, and the
answer carries a `legacy` block:

```json
{"state": "continue", "landed": ["grill", "spec"], "next": {"position": "tickets", "gate": "G-tickets"},
 "legacy": {"state": "legacy", "reason": "...cannot prove the current stage, gates or endpoint..."}}
```

`legacy` is not a permission slip. It means the Run's actual entry, gates and endpoint are
unknown to the script, the suggestion is a five-position chain and nothing more, and the
coordinator must compare the mode declaration and the real delivery endpoint before acting.

In both modes `problems` carries what the reader had to skip: a trailing partial line is
discarded and a corrupt complete line is reported by line number. A resume that silently dropped
a record would claim the wrong position, which is worse than refusing to answer. Any invalid
record makes the answer `uncertain` and empties the ready set.

The answer is a position or a node, never a dispatch. §4 still decides how — channel, agent,
worktree and spec.

## Exit checks: the floor under an artifact

```bash
python3 scripts/chain.py exit-check --step to-spec   --artifact /path/to/spec.md
python3 scripts/chain.py exit-check --step to-tickets --artifact /path/to/tickets.md
python3 scripts/chain.py exit-check --step to-tickets --artifact 01-a.md --artifact 02-b.md
```

Two steps have an artifact shape worth checking mechanically, because the next step faithfully
implements whatever it is handed.

**`to-spec`** reports two blocks apart: `native` (an acceptance section with at least one
testable line) and `dispatch` (this project's `Test tier:` line, narrow or wide, with the
reason). A section cut off by the next heading is empty for this purpose. A spec that passes
`native` and lacks the project addition is a valid upstream spec that cannot yet enter
implementation here — the two are never collapsed into one verdict, and neither claims the
requirement itself is correct.

**`to-tickets`** checks each artifact at its real ticket boundary. The shape is detected, and an
explicit `--source` is the compatibility mode:

- `list` — this project's combined format: every `## <ticket>` block carries an acceptance line
  (`验收：`, `Acceptance:`, `Done when:`, `完成标准`) or a checked task line. Ordinary section
  titles (`## What to build`, `## Acceptance criteria`, `## Problem Statement`, …) are not
  tickets.
- `file` — one local ticket: the first `# <title>` is the ticket, and the acceptance section or
  the upstream local template's trailing `- [ ]` checkboxes are its criteria. A link to a local
  file that does not exist is reported against that ticket.
- `issue` — one issue body: the acceptance *section* is the criteria, and its `##` sections are
  never read as several tickets. Fetch the full body before checking; an unreachable network is
  not an empty ticket.
- Repeated `--artifact` is a collection: every file is one ticket, and every error names the file
  and the ticket.
- Unknown blockers, a self-blocker and a blocking cycle are reported separately, at the ticket
  that carries the edge.
- When the text fits more than one shape (a map title plus a ticket-like `##` heading), the
  check exits `2` and asks for `--source` instead of guessing.

`--source list` remains the compatibility mode for a combined list whose blocks carry no ticket
label, and the old invocation without `--source` still works wherever the shape is unambiguous.

Every other step returns a refusal naming the real signal (the test run, the review, the PR
state), because pretending to check a test run would be worse than declining. Those facts belong
to `acceptance.py` (`verify`) and `closeout.py` (`reconcile`).

**The check is a floor, not a verdict.** A spec can carry a `Test tier:` line and still specify
the wrong thing. Exit-check catches the missing section, never the wrong decision; the gate
remains the coordinator's, and a passing check is not a licence to skip the two-axis review.

## Stop and ask, in one table

These conditions end a lap instead of starting the next one. The first two are mechanical; the
rest are the decision-class rules in §5, gathered here so a resume never talks the coordinator
past them.

| Condition | What stops | Who decides |
| --- | --- | --- |
| The same gate fails twice for one ticket | that ticket's lap | coordinator sends back, or trips the per-ticket breaker |
| Two consecutive gates fail across the Run | the Run | coordinator reports; a human decides whether to continue |
| A one-way Merge Danger door | the merge | a human merges |
| Credentials, a third-party account, payment, manual approval, irreversible live data | the step | a human |
| The deliverable's shape, an approach fork, or scope changed | the step | a human |
| `resume` reports a `complete` flow or chain | nothing — the Run is finished | coordinator writes the closeout |

## What resume does not do

- **It does not un-land a node after a revert.** A revert is recorded as an `invalidate` record
  with `cause: revocation`; without one the node stays accepted. The flow is append-only, so the
  revoke is a new record, not an edit of history.
- **It does not convert an old ledger into a flow.** A legacy Run keeps its legacy answer; the
  first flow record for it is a new explicit plan, written by the coordinator, never inferred.
- **It does not read Orca, git or the PR host.** It cannot tell you a worker died; that is
  `run-health.md` and `worker-show`.
- **It does not choose the next action.** A node is not a dispatch: the channel, the agent, the
  worktree name and the spec are still §4's job.
