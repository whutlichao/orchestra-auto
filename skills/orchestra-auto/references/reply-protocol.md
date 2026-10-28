# Reply protocol: decisions go to the human, details are answered for them

**When to read**: when classifying or answering a worker question (grill / wayfinder / prototype
etc.), or when deciding whether to escalate vs auto-answer.

For every worker question (from grill / wayfinder / prototype and so on), classify first, then
decide who answers. The core of this skill still stands — the flow must not stall on a question
— but "never ask the human" is no longer the default.
Merging an already-authorized project change is an execution step of §6, not a product decision
that needs re-approval; escalate per this section only when the merge would change the scope the
user set or needs extra external authorization.
The `➡️ <recommended>` line in a grilling format is reference information from the worker:
**no question may go straight from recommendation to answer without classification.**

## 5.1 Classification criteria (go through them one by one; any hit makes it decision-class)

1. **It affects the deliverable's shape or user-observable behaviour**: product direction,
   requirement trade-offs, visible UI / copy / interaction choices.
2. **Approach fork**: several viable implementation paths where the choice affects architecture,
   dependencies, seams, or the split of later tickets.
3. **Scope boundary**: whether a feature is in, what goes in Out of Scope, how strict acceptance
   is.
4. **The escalation five**: credentials, third-party accounts, payment, manual approval,
   irreversible live-data operations. These skip the other criteria, stop immediately, and the
   question is forwarded to the human verbatim, waiting for a human answer before anything
   continues. Stopping more often is preferred to answering for them.
   **What stops is this worker, not the whole run**: with several tickets in flight, an
   escalation freezes the ticket in trouble while the others keep advancing through their gates
   — one ticket's credential problem must not freeze the whole pipeline. A global freeze happens
   only when §6's send-back protocol trips the run-level breaker.

A question that touches none of the four — naming, directory placement, implementation
technique, test style, in-library API usage — is **detail-class**, and you answer it (§5.3).
Self-check as in §3: if you cannot write down why it touches none of the four, treat it as
decision-class. If the reason cannot be written, the classification does not hold.

Note that this is the opposite of the older direction: the old version listed "the requirement is
unclear / I am not sure which option the user wants" as never-ask, must-decide-alone. **Now that
is exactly what criteria 1 and 2 cover, so it must go to the human.** The user explicitly asked
that decisions not be answered straight from the recommendation — decision-class questions going
to the human are the target behaviour, not an interruption.

## 5.2 Decision-class: attach your leaning and ask the human

Pause the reply to the worker and put three things in front of the human:

- the worker's original question (including its recommendation and reasoning, if any);
- your own leaning and a one-line reason (grounded in the project context, an ADR, the spec, or
  the user's own words);
- a one-line cost for each remaining option.

Only after the human confirms or changes it do you reply to the worker with
`reply --id <original message_id>`. When the human's choice differs from yours, theirs wins; do
not relitigate. If you have a genuinely strong technical objection, say it once, and if the human
does not change their mind, follow theirs. A worker's `ask` blocks — it waits for your answer —
so make the question complete in one pass to reduce round trips.

## 5.3 Detail-class: the coordinator answers

Use the project context, an ADR, the spec, or the user's own words as the basis, and pick the
option closest to the intent the user already expressed and to the existing architecture. When
the worker's recommendation does not conflict with its reasoning, answer as recommended; when it
does, explain in the reply why you are dropping the recommendation. Write the reason into the
reply.

## 5.4 Record (both classes)

Append `Q → A (human-decided / answered-for-them) → one-line reason` to the task thread (or the
Answer's Decisions section); for human-decided ones also record the key points of the human's
choice. §7's report summarizes this audit list, and the human may overturn any answered-for-them
entry afterwards.

## 5.5 Reply channel

Replies go back over the same orchestration channel (`reply`); introduce no other tool to answer
a worker. Asking the human uses this session's question channel — that is the only place the
coordinator is allowed and required to ask; the worker-side ban in §4.4 BEHAVIOR RULE 1 is
unchanged.

Progress dashboard: initialize and open it before the first dispatch of every actual Run, then follow
the checkpoint ownership table in [progress dashboard](progress-dashboard.md) after the normal action.
Record explicit facts and structured task/dispatch/attempt reports; recording errors warn and never
block this workflow.
