# Gate Acceptance, Merge Safety, Closeout, and Reporting

**When to read**: before declaring any ticket gate passed (`G-impl`, `G-merge`), verifying four-level acceptance (`acceptance.py`), running the 10-row Run closeout (`closeout.py reconcile --live`), or writing a final/blocker report (`§7`/`§9`).

## 1. Gate Matrix by Ticket Type

| Ticket Type | Gate | Acceptance Focus |
| :--- | :--- | :--- |
| Research | Source verification | Primary citations, verified conclusions, limits, endpoint |
| Design / Wayfinder | `G-docs` / `G-wayfinder` | Settled decisions, glossary/ADRs, cleared frontier (`preflight-and-spec.md`) |
| Spec / Ticket splitting | `G-spec` / `G-tickets` | High/few seams, Out of Scope, vertical DAG slices, published tracker |
| Implementation | `G-impl` | Worktree cleanliness, red→green tests matching `Test tier:`, static checks, 2-axis `code-review` (unless properly exempted in spec + Answer), open PR with `--body-file` |
| Automatic Merge | `G-merge` | Door classification, combined tree CI, DAG merge, local sync, tracker reconciliation |

Docs-only tickets check doc deliverables only (no fabricated source tests). Spot-check call-site completeness when signatures/params change. Enforce send-back circuit breakers: two consecutive identical gate failures on one ticket → pause ticket and ask user; 5 cumulative send-backs across tickets → stop dispatching and report state.

## 2. `G-impl` & `G-merge` Rules

- **Teardown & Tests**: verify `git status --porcelain` and `git diff --numstat` (reflow noise → [recovery-and-health.md](recovery-and-health.md)); confirm `Test tier:` matches executed tests and base-commit baselines; capture red→green evidence; if Orca-rendered UI screenshots are required, demand valid `visual-evidence.md` report with `window.restored == true` and `focusRestored == true`. Remember `worker_done` proves settlement only; do not hand-edit worker code.
- **Authoritative One-Way-Door Trigger List (`G-merge` Step 0)**: **this list is the single authoritative definition of a one-way door** (all other docs point here). Any single trigger makes the whole PR one-way (uncertain → one-way; contradicted two-way claim → send back to update PR body and still treat as one-way):
  - Schema/data migration or deletion;
  - Breaking public API/CLI/SDK change (removal, rename, altered signature/semantics/default) or persisted format change (purely additive backward-compatible change is not alone one-way);
  - Auth / permissions / secrets;
  - Billing / pricing;
  - Deploy / infra;
  - Irreversible external side effect;
  - Feature removal;
  - Major dependency upgrade (`major dependency upgrade` or irreversible breaking dependency change);
  - Broad blast radius with no simple revert.
- **Merge decision**: When the user has authorized merge, the PR is two-way and all gates are green, proceed to merge and sync. When the user asked only for a PR (or PR is one-way / red / blocked), stop with it open and state the exact human merge action.
- **Sync local main (`§6.5.1`)**: verify clean main working tree on target branch with explicit upstream: normal repo uses `git pull --ff-only`; fork synced from upstream uses `git fetch upstream`, fast-forward local main to `upstream/<default>`, then push to fork mirror, checking `git merge-base --is-ancestor <merge_sha> HEAD`. Never auto-stash, reset, or force-push.
- **Reconcile tickets after merge**: Reconcile the tickets against the tracker after the merge — `MERGED` PR != closed tracker issue until confirmed by read-back.

## 3. §6.6 Acceptance Records (`acceptance.py`)

Persist one `kind: acceptance` record per PR before claiming acceptance or releasing workers:

```bash
ACCEPTANCE="$SKILLS_ROOT/orchestra-auto/scripts/acceptance.py"
python3 "$ACCEPTANCE" record --run-id "$RUN" --record "$ACCEPTANCE_JSON"
python3 "$ACCEPTANCE" show --run-id "$RUN" --ticket 39 --compact
```

Required fields: `ticket`, `gate`, `attempt`, `accepted_sha`, `accepted_at`, `pr {number, head_at_acceptance, base}`, `checks [{name, command, result, evidence_ref}]`, `review {axis[], result, evidence_ref}`, `levels`, `decided_by` (`coordinator`|`human`), `decided_at`, `answer_location`.

**Minimum evidence & four-level rules:**
- No level may be `true` without an `evidence_ref`: `merge` needs PR host `MERGED` + merge SHA + either `ancestry {ancestor, descendant, result: true}` or `equivalence {accepted_sha, merge_sha, diff_empty: true, command}`; tracker needs read-back `closed`; start needs `turnStart: observed` (or attempt artifact/status projection — `worker_done` proves settlement only); tests need command + log (A `grep` only proves a string exists, never a test run); review needs both axes (`standards`, `spec`) or waiver in **both** the spec **and** the Answer.
- Ticket-level levels (`implemented`, `integrated`, `merged`, `tracker_closed`) hold only when **every** linked PR record supports them; conflicting claims on one PR yield `divergent`. Humans may declare `never_merge` (`never_merge` is a terminal state). A revert or reopen **appends** an observation (`acceptance.py observe`) rather than mutating history. Open four-level questions (`acceptance.py open-questions`) keep the affected semantics `provisional` without blocking closure until `resolve-question`.

## 4. §6.7 Ten-Row Run Closeout (`closeout.py`)

Persist a durable `kind: closeout` record from external authority (collect git/gh/orca automatically with `--live`, overriding via `--authority FILE` as needed):

```bash
CLOSEOUT="$SKILLS_ROOT/orchestra-auto/scripts/closeout.py"
python3 "$CLOSEOUT" reconcile --run-id "$RUN" --live --answer-location "$ANSWER" --compact
python3 "$CLOSEOUT" show --run-id "$RUN" --compact
```

| # | Fact | Authority | Repeatable | Action |
| :--- | :--- | :--- | :--- | :--- |
| 1 | The accepted SHA per PR | Ledger acceptance record | **no** | Persist at acceptance; cannot reconstruct later |
| 2 | PR `MERGED`, base & merge SHA | PR host API | yes | Read back & record |
| 3 | Merge covers accepted head | `git merge-base --is-ancestor` / git merges (`equivalence`) | yes | Failure → `divergent`, escalate |
| 4 | Main contains merge | `git rev-parse` / ancestry | yes | Record containment |
| 5 | local main is synced | `git status` / upstream ff-only | yes | Blocked → pause later merges (`blocks later merges`, `merge_blocked`) |
| 6 | Every ticket integrated | Integration ancestry / PR `Closes` | yes | Record per ticket |
| 7 | Tracker state | tracker read-back | yes | Record closed/reopened |
| 8 | worker lifecycle | `worker-show` / `worker-list` / release receipt | yes | Enumerate reclaimable / `release_unknown`; retain user-kept workers |
| 9 | dashboard projection | `progress.json` | yes (non-auth) | Gap only; never blocks closeout or counts as evidence |
| 10 | one-way-door pending | Ledger `pending` (`human_merge`) | yes | Reconcile PR/SHA/sync/tracker; stays visible without a coordinator turn |

Closeout rules (`scripts/closeout.py`): Only row 1 is non-repeatable (`non_repeatable`). Row 9 records a gap and nothing else (`progress.json` never blocks declaration). Crash recovery between merge and closeout is repaired from rows 2, 3, 4 and 7. Every closeout record stores `answer_location` and the version scope of release replay verification. Release requires a settled Dispatch AND a durable closeout record (`release_unknown` first reads `worker-show`; failed writes spill to `closeout-pending/`, so release and the gate declaration wait until repaired). Never release unsettled workers regardless of health.

## 5. Escalation & Blocker Reporting (§9)

If Orca CLI fails to start or guide fails to fetch: report exact error and stop (never swap executable or guess). On `worker-start` `state: failed` or app restart: verify state via `worker-list --include-remote` and inbox before re-acting; never orphan or overwrite original Run identity. Structure every blocker/final report around: (1) current stage & target endpoint, (2) verified evidence (commits/PRs/tests/closeout status), (3) `Q -> A` decision audit log, and (4) unresolved items with exact human next step.
