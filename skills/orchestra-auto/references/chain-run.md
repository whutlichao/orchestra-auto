# Chain positions, resume and exit checks

**When to read this:** between two laps of the same Run, after a crash or an app restart, or
whenever the coordinator has to answer "where does this chain continue" from the ledger instead
of from the conversation.

## Contents

- [Positions, not steps](#positions-not-steps)
- [Resume: where the chain continues](#resume-where-the-chain-continues)
- [Exit checks: the floor under an artifact](#exit-checks-the-floor-under-an-artifact)
- [Stop and ask, in one table](#stop-and-ask-in-one-table)
- [What resume does not do](#what-resume-does-not-do)

## Positions, not steps

The chain is linear until it forks, so it is modelled as five **positions**. `implement` and
`implement-spec` are one position because they close on the same gate: one acceptance record
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

## Resume: where the chain continues

```bash
python3 scripts/chain.py resume --run-id <run_id>
python3 scripts/chain.py resume --run-id <run_id> --ledger /path/to/ledger.jsonl
```

`resume` reads the Run's ledger (`$ORCA_LEDGER_HOME/runs/<id>/ledger.jsonl` by default), keeps
only `kind: acceptance` records, and per position asks one question: **is there a record for this
gate whose checks all passed and whose review is a pass or a waiver?**

- A record with no checks is a claim with no evidence, and does not land a position.
- A failed check or a failed review does not land it.
- An unknown gate lands nothing: a typo must not move the chain.
- A later record never un-lands an earlier position; history is append-only.

The answer is one of two states:

```json
{"state": "continue", "landed": ["grill", "spec"], "next": {"position": "tickets", "gate": "G-tickets"}}
{"state": "complete", "landed": ["grill", "spec", "tickets", "build", "review"], "next": null}
```

`problems` carries what the reader had to skip: a trailing partial line is discarded and a
corrupt complete line is reported by line number. A resume that silently dropped a record would
claim the wrong position, which is worse than refusing to answer.

The answer is a position, never a dispatch. It says where to continue; §4 still decides how —
channel, agent, worktree and spec.

## Exit checks: the floor under an artifact

```bash
python3 scripts/chain.py exit-check --step to-spec   --artifact /path/to/spec.md
python3 scripts/chain.py exit-check --step to-tickets --artifact /path/to/tickets.md
```

Two steps have an artifact shape worth checking mechanically, because the next step faithfully
implements whatever it is handed:

- **`to-spec`** — the spec carries a `Test tier:` line (narrow or wide, with the reason) and an
  acceptance section with at least one testable bullet. A section cut off by the next heading is
  empty for this purpose.
- **`to-tickets`** — every `## ` block carries an acceptance line: `验收：`, `Acceptance:`,
  `Done when:` or `完成标准`. A ticket without one cannot be demonstrated, so it cannot be
  accepted.

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
| `resume` reports a `complete` chain | nothing — the Run is finished | coordinator writes the closeout |

## What resume does not do

- **It does not un-land a position after a revert.** A revert is an `observation` and lowers the
  level without rewriting history, so `resume` still counts the position as landed. When a revert
  is recorded, the coordinator re-opens the ticket explicitly; if the chain must re-enter an
  earlier position, that is a new Run, not a silent resume.
- **It does not read Orca, git or the PR host.** It cannot tell you a worker died; that is
  `run-health.md` and `worker-show`.
- **It does not choose the next action.** A position is not a dispatch: the channel, the agent,
  the worktree name and the spec are still §4's job.
