# G-docs / G-wayfinder / G-spec / G-tickets — gates for documentation and planning tickets

**When to read this**: this ticket is a design, spec or ticket-splitting ticket and you are closing
it for acceptance. **Read only the section you need** — these four gates do not share criteria
(especially G-docs and G-wayfinder, whose artifacts have completely different shapes, so their
criteria must not be borrowed from each other).

**An implementation ticket runs none of these four**; it runs G-impl (see SKILL.md §6).

---

## Contents

- [G-docs — requirement alignment (the grill-with-docs path: document structure completeness)](#g-docs--requirement-alignment-the-grill-with-docs-path-document-structure-completeness)
- [G-wayfinder — the wayfinder path (map and decision tickets)](#g-wayfinder--the-wayfinder-path-map-and-decision-tickets)
- [Decision branches: research, prototype, questionnaire, wizard, task](#decision-branches-research-prototype-questionnaire-wizard-task)
- [G-spec — after to-spec lands](#g-spec--after-to-spec-lands)
- [G-tickets — after to-tickets lands](#g-tickets--after-to-tickets-lands)

---

## G-docs — requirement alignment (the grill-with-docs path: document structure completeness)

**Accept the outcome of grill.** The wayfinder path goes through G-wayfinder.

- Record each round's questions, answers, rationale and destinations. Explore facts; a
  recommendation cannot substitute for the user's decision.
- The design tree's frontier is empty and the user confirms shared understanding. Unresolved
  questions cannot pass the gate.
- Record resolved terminology when needed, using the target repository's domain-doc conventions;
  no new terminology means no file is required.
- Offer an ADR only for a hard-to-reverse decision that is surprising without context and has
  real trade-offs, following repository conventions.
- When the user or repository requires a design document, verify its content and conventions;
  upstream grill itself requires neither a fixed template nor a design file.

### G-docs is not the end of the line

A full implementation request normally runs `grill-with-docs → to-spec → to-tickets →
implement-spec`. G-docs accepts only alignment; apply G-merge to authorized project changes. Record the
follow-up entry point and stop if this round asked only for design,
or continue into to-spec and to-tickets if this round includes implementation. See SKILL.md §7.

---

## G-wayfinder — the wayfinder path (map and decision tickets)

**Applicability**: §1 judged the input as "too large for one session, heavy fog (several
directions undecided)" and this round uses `wayfinder`. First check whether the target repository
already has wayfinding conventions; do not invent labels or mappings when they are missing.

**Read the repository's own wayfinding section first**, then run this section's criteria: the map
and its child tickets, blocking, frontier queries, claiming and resolving all live there.
**This section only covers what to accept.**

**Chart acceptance** is separate from map completion: check the agreed destination, breadth-first
grill, map, typed tickets and blockers, fog and authorized research handoffs. Chart ends with open
decisions; those do not fail chart acceptance. If there is no fog and the effort fits one session,
ask how to proceed instead of forcing a map. **Work acceptance** checks a claimed ticket's resolution
comment, closure and map pointer, newly specified tickets/fog updates, and real user participation
for HITL. A work session resolves at most one non-research ticket. Only apply the completion checks
below when the map is claimed complete.

- **The map exists and is a single issue**, carries the map label, and its body has the four
  sections: destination / notes / decisions so far / not yet specified / out of scope.
- **Every child ticket is a decision ticket, not an implementation slice**: its body answers only
  "what has to be decided", and it carries a type label (research / prototype / grilling / task).
  Research can produce facts, and a task can do prerequisite work that unblocks a decision; neither
  is an implementation slice delivering the destination.
- **Blocking uses native dependencies when supported**, otherwise the tracker's documented fallback.
  Verify that frontier queries account for that representation and that every edge is real.
- **Sort every child ticket into one of four states before judging anything**: *takeable* (no open
  blocker, no assignee), *in progress* (assigned, decision not yet recorded), *blocked* (at least
  one open blocker), *done* (the decision is recorded and the tracker closed it). Write the four
  counts down — a filtered tracker view can make the second and third states invisible, which is how
  an empty frontier gets mistaken for a finished map.
- **An empty frontier is not completion.** It only says nothing is currently takeable, and three
  different situations produce it: every ticket assigned, every remaining ticket blocked, or the
  work genuinely finished. Only the last one closes this stage. The closing condition is: every
  required decision is *done*, no ticket is left in progress or blocked with work unfinished, and
  **"not yet specified" contains no unresolved in-scope fog** (the fog has cleared). When the
  frontier is empty but tickets are in progress or blocked, the stage is **not** done: wait for
  them, unblock them, or report the blockage — do not accept the map.
- **The two conditions are verified separately**: an empty frontier ≠ cleared fog (the first only
  means nothing is currently takeable; the second requires every formable question to have taken
  form). "Every ticket is done" ≠ "the frontier is empty" either: an assigned or blocked ticket is
  unfinished work the frontier query cannot see.
- **Every "decisions so far" entry points at the ticket that made it** and does not restate the
  decision — the map is an **index, not a repository**, and a decision lives in exactly one place
  (its own ticket).
- **The closing signal is not "is it done" but "is the path clear"**: wayfinder produces decisions
  by default (plan, don't do). If the effort is meant to carry it through to execution, either the
  effort overrode that explicitly in its notes, or the fog has cleared and it is time to hand off
  to SKILL.md §7 for implementation tickets. **State in the report which of the two it is.**

---

## Decision branches: research, prototype, questionnaire, wizard, task

Each branch below returns to the decision that opened it; none of them may silently become the
implementation. Accept one only when its own output exists and the decision it served is recorded
back at the original question.

| Branch | Mode | Input | Output and evidence | Return |
| --- | --- | --- | --- | --- |
| `research` | AFK | one decision question and its sources | a cited Markdown file; every claim traces to a primary source | the decision that asked, not the user's choice |
| `prototype` | HITL | the one question a runnable artifact answers | the user reacted; the question and verdict are recorded; the prototype is kept on a throwaway branch or pointer | the asking stage |
| `to-questionnaire` | HITL (async) | recipient, and what the user needs back | the questionnaire file exists and is handled through the named process; **writing it authorizes no sending** | the asking stage, once real answers arrive |
| `wizard` | human executes | a step only a human can perform | an interactive script the human confirms step by step | the task that was blocked, with the values it captured |
| `task` (wayfinder) | HITL or AFK | work needed before a decision | the work is done and its resulting facts are recorded on the ticket | the blocked decision ticket |

Blocking rules that apply to all five:

- A questionnaire with no recipient answer, and a wizard the human has not finished, leave the
  dependent node **blocked**; the coordinator does not fill in answers or perform the human's side.
- Research conclusions and prototype verdicts are input to a decision; they never replace the
  user's decision, and they do not authorize implementation.
- The dispatcher is the case that does *do* rather than decide; it earns its place only by
  unblocking a decision, never by delivering the destination.
- Throwaway branches and pointers are authorized temporary artifacts: apply the implementation PR
  contract only when the user authorized a project change, not to the research or prototype.

## G-spec — after to-spec lands

- The spec has landed at this round's delivery endpoint; when the tracker needs publishing, labels
  and states follow the target repository's conventions. If the repository separates spec anchors
  from implementation tickets, do not mix the two. When an upstream skill conflicts with the
  repository's conventions, reconcile the user's goal first, then land per this repository's rules.
- Seams are declared and as few as possible (ideally one) and as high as possible; use deep-module
  vocabulary.
- User stories cover enough ground; Out of Scope is non-empty.

**Seam placement is §5.1 criterion 2 (approach fork)** — raise it per SKILL.md §5 with your
leaning and let the human decide; record the answer in the audit list.

---

## G-tickets — after to-tickets lands

- Every ticket is a vertical tracer bullet (cuts through all layers, independently verifiable, fits
  in a single context) with declared blocking edges; a wide refactor uses expand–contract rather
  than a hard cut.
- Tickets are published to the configured tracker first (remote issues or committed local files).
  In the long-horizon path the worker cuts the integration branch from the default base (for a
  fork, `upstream/<default branch>` per §4.3), and each implementer cuts from that integration
  branch; a downstream ticket waits only until its blockers are merged into the integration
  branch, not until the whole map lands.
- **Long-horizon entry check**: a spec pointer exists; every ticket has a title, What to build,
  acceptance criteria and a status; every blocker references an existing ticket; the graph is
  acyclic; at least one ticket is on the frontier; the tracker is reachable and local ticket files
  are visible from a worktree; each ticket fits one fresh context. If any fails, send it back to
  `to-tickets`; do not dispatch `implement-spec`.
- In the per-ticket fallback, a downstream ticket cuts its base after the upstream PR merges.
  Stacked PRs remain an explicit exception (tight schedule, short dependency chain); write the
  stack order on the tickets, merge in stack order per §6.5, and rebase the whole stack if any one
  of them rebases.

**Splitting granularity is §5.1 criterion 3 (scope boundary)** and also goes to the human. When
writing the splitting options, describe **the ticket count and what each ticket delivers** in
to-tickets' own language rather than inventing colloquial shorthand (SKILL.md §7 has the
prohibition and the substitution table).

---

## Related

- SKILL.md §6 — the master table routing gates by ticket type (read it first to know which one
  applies)
- SKILL.md §7 — advancing by delivery endpoint, splitting language, and design-ticket close-out
  reconciliation
- SKILL.md §5 — decisions to the human, details answered for them (both seam placement and
  splitting granularity go through it)
