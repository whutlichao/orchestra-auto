# Routing, Explicit Flow, and Chain Resume

**When to read**: at the start of every user turn or cross-session continuation to classify into one standalone route (`R0–R16`), present ranked choices when ambiguous, announce the entered branch, enforce the write-escalation re-entry gate, run repo initialization (`init-check.py`), initialize `flow.jsonl`, or resume an interrupted Run (`chain.py`).

## 1. Mandatory Route Entry Gate Protocol

Every user input — including follow-ups across sessions where no orchestration keyword is repeated — must pass this gate before any discussion, file write, or tracker mutation:

1. **Minimal Read-Only Classification Only**: inspect repo files or git/tracker state read-only only when needed to tell which branch applies. Never hold an extended architecture debate, draft ADRs, or edit files during classification.
2. **When Unclear / Multi-Match → Present Ranked Branch Choices**: if the input could reasonably fit more than one branch (for example, "let's discuss tech selection and integration architecture" or "look at how to improve this module"), do **not** pick silently and do **not** start answering in `R0`. Output a **ranked numbered menu** ordered by likelihood (typically top 2–4 candidates from `R0–R16`) and wait for the user's pick:
   ```text
   Route clarification needed — please pick one branch (ranked by fit):
   1. R2 `grill-with-docs` (Channel B worker, G-docs) — repo-grounded interview to settle architecture/tech trade-offs and produce ADR/glossary if settled.
   2. R5 `research` (Channel B AFK worker, G-research) — cited primary-source comparison before making design choices.
   3. R3 `wayfinder-chart` (Channel B worker, G-wayfinder) — multi-session planning map when the problem space itself is still foggy.
   4. R0 `direct-answer` (Channel A read-only, zero writes) — brief verbal Q&A only; no files, ADRs, or GitHub issues will be written.
   ```
3. **When Clear → Pick Exactly One Branch & Explicitly Announce Entry**: once unambiguous (or the user named a skill/branch or picked from the ranked menu), select **one** standalone branch (`R0–R16`) and explicitly inform the user which branch was entered before taking action:
   ```text
   Entered Route: R2 `grill-with-docs` (Channel B — supervised worker)
   Why: Unsettled architecture trade-offs in target repo; required before ADR / spec / tickets.
   Target & Endpoint: <repo> -> settled decisions + ADR/glossary (`G-docs`)
   Executor & Gate: worker in `--worktree new-top-level` running `grill-with-docs` (`grilling` + `domain-modeling`), Gate `G-docs`
   ```
4. **Worker-Only Execution After Branch Entry (`R1–R16`)**: once any branch `R1–R16` is entered, **ALL substantive work is executed by a supervised worker** (`worker-start --worktree new-top-level --agent <enabled>`). The coordinator never conducts the grill/design discussion itself, never drafts/writes controlled artifacts directly, and only handles: (a) routing & preflight, (b) worker dispatch & relaying decision-class `orchestration ask` messages between worker and human via `reply --id <msg>`, and (c) Phase 4 gate verification, merge (when authorized), closeout, and release.
5. **Write-Escalation Re-Entry Gate & Controlled Artifacts**: `R0 (direct-answer)` is strictly read-only and ephemeral (**zero** persistent writes). The coordinator may never directly write or mutate any **controlled artifact** — target code/tests, `docs/adr/*.md`, `GLOSSARY.md`, `GLOSSARY-MAP.md`, specs, ticket files, or planning/triage/wayfinding tracker mutations (`gh issue create`, `gh issue edit`, `gh issue close`). Crossing from `R0` or casual chat into any controlled write **must stop immediately and re-enter this Route Entry Gate**.
6. **Pre-Write Grill Prerequisite & Anti-Fake-Grill Rule**: asking 3 high-level multiple-choice questions inside the coordinator thread does **not** equal completing `R1 grill-me` or `R2 grill-with-docs` (`G-docs`). Before any ADR write (`docs/adr/*.md`), `R9 to-spec`, or `R10 to-tickets` (`gh issue create`), work must pass `R1`/`R2` (`G-docs`) through a dispatched worker unless the user explicitly instructs to skip grill and supplies settled decisions/spec.
7. **Stage Transition Protocol**: when an entered branch passes its Phase 4 gate, stop if the authorized endpoint is reached. If advancing to another stage, re-enter this gate explicitly: announce the single next branch when unambiguous (e.g., `R2` → `R9 to-spec` → `R10 to-tickets` → `R13 implement-spec`), or present a ranked menu when multiple next branches are viable.

## 2. Standalone 17-Branch Routing Matrix (`R0–R16`)

Every routing situation is split into its own row — never mix distinct routes together (explicit user naming of a skill overrides heuristics):

| ID | Route / Skill | Channel / Executor | Single Trigger Condition | Pre-skill (`--type` / `--gate`) | Completion Evidence | Next Branches |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **R0** | `direct-answer` | Channel A (Coordinator read-only; **no worker, zero writes**) | Pure Q&A, verbal status, or read-only explanation with zero persistent writes | None (no Run / no worker) | Verbal answer delivered | Any write intent → re-enter `R1–R16` |
| **R1** | `grill-me` | Channel B (Worker) | Unsettled idea/trade-offs with **no target repo / cwd** | `grill-me` (`grilling`) · `design` / `G-docs` | Worker Q&A log, settled decisions & rationale | `R9`, `R5`, `R6`, or stop |
| **R2** | `grill-with-docs` | Channel B (Worker) | Unsettled idea, architecture/tech selection, or trade-offs **in a repo / cwd** | `grill-with-docs` (`grilling` + `domain-modeling`) · `design` / `G-docs` | Worker Q&A log, rationale, `GLOSSARY.md` / `docs/adr/*.md` | `R9`, `R12`, `R5`, `R6`, or stop |
| **R3** | `wayfinder-chart` | Channel B (Worker) | Planning itself spans >1 session with foggy path; needs initial decision map | `wayfinder` (`chart`) · `design` / `G-wayfinder` | Map issue + typed decision tickets + blockers (stops after chart) | `R4` (later session) or stop |
| **R4** | `wayfinder-work` | Channel B (Worker) | Existing wayfinder map; claim & resolve next frontier ticket (≤1 non-research/session) | `wayfinder` (`work`) · `design` / `G-wayfinder` | Ticket claimed first, resolved via HITL/AFK, closed, map pointer updated | `R4`, `R9`, or `R12` |
| **R5** | `research` | Channel B (Worker, AFK) | Decision depends on cited primary/external verifiable facts | `research` · `research` / `G-research` | Cited Markdown findings with primary sources & limits | Asking branch (`R1`/`R2`/`R4`/`R9`) |
| **R6** | `prototype` | Channel B (Worker, HITL) | Look, UX, state machine, or logic needs runnable human confirmation | `prototype` · `design` / `G-prototype` | Runnable artifact on throwaway branch + recorded user reaction | Asking branch (`R1`/`R2`/`R4`/`R9`) |
| **R7** | `to-questionnaire` | Channel B (Worker, async HITL) | Missing answers owned by an external person/team | `to-questionnaire` · `docs` / `G-questionnaire` | Questionnaire prepared/sent via named process; dependent ticket stays blocked | Blocked branch once real reply arrives |
| **R8** | `wizard` | Channel B (Worker + human) | Step only a human can perform (credentials, console, manual approval) | `wizard` · `docs` / `G-wizard` | Verified human guide; stays pending until human confirms completion | Blocked branch after human finishes |
| **R9** | `to-spec` | Channel B (Worker) | Settled decisions (post-grill or explicit skip) need an authoritative multi-session spec | `to-spec` · `spec` / `G-spec` | Spec with high/few seams, user stories, non-empty Out of Scope | `R10` or stop at spec endpoint |
| **R10** | `to-tickets` | Channel B (Worker) | Accepted spec needs vertical tracer-bullet DAG tickets published to tracker | `to-tickets` · `ticket-splitting` / `G-tickets` | Acyclic vertical DAG + tracker populated (`gh issue create`) | `R12` (single) or `R13` (multi-ticket map) |
| **R11** | `diagnosing-bugs` | Channel B (Worker) | Bug or hard regression with unclear root cause needing reproduction first | `diagnosing-bugs` · `impl` / `G-impl` | Verified root cause + failing red reproduction test | `R12` (fix) and optionally `R16` |
| **R12** | `implement` | Channel B (Single Worker) | Single bounded change, known-cause bugfix, or single ready atomic ticket | `implement` (+ `code-review`) · `impl` / `G-impl`, `G-merge` | Entry evidence (why no grill), TDD red→green, `code-review`, PR; new fork re-routes to `R2` | `G-merge` closeout |
| **R13** | `implement-spec` | Channel C (1 Integration Worker) | Accepted complete multi-ticket DAG map (`to-tickets` done & tracker reachable) | `implement-spec` (+ `tdd`/`code-review`/`pr`) · `impl` / `G-impl`, `G-merge` | Nested isolation proof, per-ticket TDD, single integration PR with `Closes` | `G-merge` closeout |
| **R14** | `triage` | Channel B (Worker) | Raw external Issue/PR needing classification & labels (never re-triage `to-tickets` output) | `triage` · `docs` / `G-triage` | Labels/clarification per `docs/agents/triage-labels.md` | `R12`, `R2`, or stop |
| **R15** | `improve-codebase-architecture` | Channel B (Worker) | Structural seam pain or testability bottleneck needing refactor candidates | `improve-codebase-architecture` (`codebase-design`) · `design` / `G-docs` | Ranked refactor candidates presented to human for selection | `R2`, `R9`, or `R12` after human picks |
| **R16** | `retro` | Channel B (Worker, read-only first) | Process error, gate bypass, or repeated harness defect | `retro` · `research` / `G-retro` | Read-only `retro-candidates.md` saved **outside** repo; human selects before any landing worker | Landing via `R12` after human picks |

## 3. Repo Initialization Gate (`G-docs` / `G-tickets` Entry)

Run `python3 scripts/init-check.py check --repo <target>` (pass the explicit target repository path when coordinating from a non-git workspace such as an `orca bot` folder, and read `<target>/docs/agents/triage-labels.md` & `<target>/docs/agents/issue-tracker.md` dynamically rather than hardcoding label names):
- Exit `0`: initialized (`AGENTS.md`/`CLAUDE.md` `## Agent skills` block + `docs/agents/issue-tracker.md` & `domain.md` + `gh repo view`/`glab repo view` if hosted).
- Exit `1`: uninitialized → confirm with user before dispatching `setup-matt-pocock-skills` (`disable-model-invocation: true`, pass path explicitly; never lands on `main`).
- Exit `2`: `unknown` → report probe error and verify before assuming uninitialized.

## 4. Explicit Flow Record (`flow.py`) & Resume (`chain.py`)

`ledger.jsonl` indexes observations; `flow.jsonl` (`$ORCA_FLOW_HOME/runs/<run_id>/flow.jsonl` or beside ledger) owns the Run's active graph, gates, and terminal endpoint:

```bash
FLOW="$SKILLS_ROOT/orchestra-auto/scripts/flow.py"
CHAIN="$SKILLS_ROOT/orchestra-auto/scripts/chain.py"

# Initialize one standalone branch preset OR one composite chain preset (no hand-built JSON)
python3 "$FLOW" init-route --run-id "$RUN" --route grill-with-docs --goal "Settle auth ADR" --compact
# Standalone branch presets (R1–R16):
#   grill-me | grill-with-docs | wayfinder-chart | wayfinder-work | research | prototype |
#   to-questionnaire | wizard | to-spec | to-tickets | diagnosing-bugs | implement |
#   implement-spec | triage | improve-codebase-architecture | retro
# Composite chain presets (multi-stage Runs):
#   design-only | single-impl | full-chain | wayfinder | ticket-map | bugfix

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
