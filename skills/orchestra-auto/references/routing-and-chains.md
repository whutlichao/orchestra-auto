# Routing, Explicit Flow, and Chain Resume

**When to read**: when choosing a route among the 14 skill families, running repo initialization (`init-check.py`), initializing an explicit `flow.jsonl`, or resuming an interrupted Run (`chain.py`).

## Route Entry & Initialization Gate

Explore read-only if scope or current state is unclear, then pick one route by current evidence (explicit user naming of a skill overrides heuristics):

| Condition | Route / Skill | Completion Evidence | Returns To |
| :--- | :--- | :--- | :--- |
| Plain Q&A, status, or verbal explanation | Direct answer (no Run/worker/edits) | Answer delivered | — |
| Unsettled idea/trade-offs (no cwd / with cwd) | `grill-me` / `grill-with-docs` (`grilling` + `domain-modeling`) | Questions, answers, rationale; glossary/ADRs if settled | Stage needing settled design |
| Bounded fix, small change, or existing valid spec/ticket | `implement` (`diagnosing-bugs` first for bugs) | Scope, acceptance, and source choices stated; fork re-routes to grill | G-impl |
| Planning itself exceeds 1 session with foggy path | `wayfinder` (`chart` or `work`) | `chart`: map + typed decision tickets; `work`: 1 non-research ticket claimed & resolved | `to-spec` or small direct impl |
| Decision depends on cited external/primary facts | `research` (AFK) | Cited Markdown file landing traceable conclusions | Asking stage |
| Look/state/logic needs concrete runnable confirmation | `prototype` (HITL) | User reacted to runnable artifact on throwaway branch | Asking stage |
| Missing answers from external person / human-only step | `to-questionnaire` / `wizard` | Sent questionnaire via named process / interactive script confirmed | Blocked ticket |
| Aligned multi-session build | `to-spec` → `to-tickets` → `implement` / `implement-spec` | G-spec seams + G-tickets vertical DAG + tracker ready | G-impl |
| Raw issue needing labels/clarification | `triage` | Triage labels per `docs/agents/triage-labels.md` (`to-tickets` output stays agent-ready) | `implement` |
| Architecture seam pain / hard regression | `improve-codebase-architecture` / `diagnosing-bugs` | Candidate chosen / red→green regression loop | `grill-with-docs` / `retro` |
| Process error or repeated mistake | `retro` (read-only analysis first) | `retro-candidates.md` saved outside repo; user picks before worker lands | Later round |

**Repo initialization gate (`G-docs` / `G-tickets` entry):** run `python3 scripts/init-check.py check --repo <target>`:
- Exit `0`: initialized (`AGENTS.md`/`CLAUDE.md` `## Agent skills` block + `docs/agents/issue-tracker.md` & `domain.md` + `gh repo view`/`glab repo view` if hosted).
- Exit `1`: uninitialized → confirm with user before dispatching `setup-matt-pocock-skills` (`disable-model-invocation: true`, pass path explicitly; never lands on `main`).
- Exit `2`: `unknown` → report probe error and verify before assuming uninitialized.

## Explicit Flow Record (`flow.py`) & Resume (`chain.py`)

`ledger.jsonl` indexes observations; `flow.jsonl` (`$ORCA_FLOW_HOME/runs/<run_id>/flow.jsonl` or beside ledger) owns the Run's active graph, gates, and terminal endpoint:

```bash
FLOW="$SKILLS_ROOT/orchestra-auto/scripts/flow.py"
CHAIN="$SKILLS_ROOT/orchestra-auto/scripts/chain.py"

# Initialize one canonical route preset in a single command (no hand-built JSON)
python3 "$FLOW" init-route --run-id "$RUN" --route full-chain --goal "Implement feature X" --compact
# Presets: design-only | single-impl | full-chain | wayfinder | ticket-map | bugfix

# Inspect ready nodes, blockers, waiting_user, or invalidated downstream work
python3 "$CHAIN" resume --run-id "$RUN" --ledger-home "$ORCA_LEDGER_HOME" --compact

# Mechanical artifact floor check (never replaces coordinator gate judgment)
python3 "$CHAIN" exit-check --step to-spec --artifact docs/spec.md --compact
python3 "$CHAIN" exit-check --step to-tickets --artifact docs/tickets.md --source list --compact
```

**Resume rules:**
- `flow.jsonl` outranks ledger inference (`legacy` ledger-only resume suggests a 5-position heuristic and never proves endpoint or current gate).
- Node states: `pending`, `in_progress`, `waiting_user`, `blocked`, `accepted`, `not_applicable` (requires `entry_evidence`), `invalidated`.
- Reopening or revoking an upstream artifact appends `invalidate` in `flow.py` — every transitive downstream node becomes `invalidated` automatically and must re-run before closeout.
- `chain.py exit-check --step to-tickets`: repeatable `--artifact` checks collections (`file`), or `--source list|file|issue` disambiguates single files; exit `2` = ambiguous format.
- Handing planning to a fresh context preserves exact user answers, rejected options, unresolved questions with owners, verified codebase facts, target repo/base/endpoint, and out-of-scope boundaries.
