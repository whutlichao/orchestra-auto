# Launch kit — orchestra-auto

**Everything in this directory is a draft.** It exists so a maintainer can publish it deliberately,
after reading it. Preparing this material involves **no** account sign-in, **no** community contact,
**no** posting, and **no** third-party or account binding. If you are the maintainer, the drafts are
yours to edit; if you are not, improve the text in a pull request and let the maintainer publish.

Two facts worth internalizing before reading any draft below:

1. **There is no author-facing submission form.** Per the
   [skills.sh FAQ](https://skills.sh/docs/faq), a repository appears on the directory automatically
   from anonymous install telemetry. Nobody "submits" a skill; people install it, and the count
   follows. So no draft here claims this repository is already listed, and none should.
2. **Nothing here should be posted to make a number move.** Do not install the skill to seed a
   rank. The draft channels below are for reaching people who have the problem this skill solves.

Read [launch-kit.zh-CN.md](launch-kit.zh-CN.md) for the Chinese version.

---

## 1. Positioning

**One line.** A supervised Orca workflow where the coordinator plans, dispatches and verifies, workers
edit and open PRs, and the coordinator merges once the gates pass — without a confirmation round trip
per ticket.

**The problem it solves.** Orchestrating agents across a real codebase is easy to start and hard to
finish. The failure modes are specific and repeatable:

- the orchestrator edits the main working copy because isolation was left implicit;
- a worker's receipt says `ready` and everyone assumes a turn began;
- a message settles and nobody notices it was never handled;
- a scope question gets answered by the agent instead of the person;
- the same gate fails twice and the loop retries forever instead of stopping;
- a diff tens of thousands of lines long turns out to be a formatter reflow, and the real work is
  discarded with it.

**What this skill is.** A written procedure that names those failure modes and gives each one a
concrete countermeasure, plus one small script that makes the message inbox safe to rely on.

**What it is not.** Not a general orchestration framework, not portable to other CLIs, not a
decision-maker. It is explicit that the human decides scope, approach forks and anything involving
credentials, third parties, payment, approval or irreversible data.

**Who it is for.**

- Teams already using [Orca](https://www.onorca.dev/) who supervise more than one agent at a time.
- Anyone who has watched an agent orchestration end with an unreviewed merge or a silently dropped
  handoff.
- Maintainers who want the *process* pinned down while the product decisions stay theirs.

**Who it is not for.** Anyone without Orca — the procedure drives `orca orchestration` verbs and does
not pretend otherwise. Anyone looking for a library to `import`.

**Why two language bundles instead of one.** They are genuinely separate installs with the same
semantics. A bilingual team installs both and lets each agent read its own language, rather than
running everything through a translation layer.

---

## 2. Audience

| Segment | Where they are | What lands with them |
| --- | --- | --- |
| Orca users running multi-agent work | Orca's own docs and community | The concrete failure modes; they recognize them immediately |
| Agent-skills early adopters | skills.sh, the `skills` CLI, Hacker News | A non-toy skill: it encodes judgement, not a packaged API call |
| Team leads burned by unreviewed agent merges | Reddit (r/ExperiencedDevs, r/LLMDevs), HN | The gates and the stop thresholds |
| Chinese-language AI engineering | V2EX, 掘金, 微信公众号 | The decision-vs-process split, which reads well in Chinese |
| Skill authors | GitHub | The package structure, the dual-language parity tests, the disclosure scan |

---

## 3. Install

```bash
# English
npx skills add whutlichao/orchestra-auto --skill orchestra-auto

# 简体中文
npx skills add whutlichao/orchestra-auto --skill orchestra-auto-zh
```

Orca prerequisite: an installed Orca with a reachable CLI. Verify with `orca skills get orchestration`
— if that guide is not available, this skill cannot work. Full details in the
[README](../README.md#prerequisites-and-capability-boundary).

---

## 4. 90-second demo script

The goal is to show **one decision the skill gets right and a naive orchestrator gets wrong**, then
show the safety net. Read the bracket notes as stage directions; they are not spoken.

```text
[0:00 — Cold open. State the problem, not the product.]
"Every multi-agent demo works. Almost no multi-agent run finishes cleanly.
 The interesting part isn't the dispatch — it's the last 20%."

[0:10 — Show the first failure mode.]
[Screen: two tickets, two workers, both receipts reading state: ready.]
"Both receipts say ready. That means the bytes were written. It does not mean
 either agent started. A naive loop opens its inbox and waits forever."

[0:25 — Show the skill's countermeasure.]
[Screen: SKILL.md section 4.3.1.]
"This skill makes you prove the turn actually started — read the terminal title,
 compare it to that agent's idle banner — before you enter the wait loop. And if
 it didn't start, the fix is one bare Enter, not a resend of the whole prompt."

[0:40 — Show the second failure mode, the one that burns real money.]
[Screen: a worker's question — "should I also delete this unused component?"]
"A worker asks a perfectly reasonable question. It even recommends an answer.
 A naive orchestrator takes the recommendation and keeps moving."

[0:52 — Show the classification.]
[Screen: SKILL.md section 5.1.]
"This skill makes you classify first. Directory placement? Answer it yourself.
 'Also delete this other thing'? That's a scope change — it goes to the human,
 with your leaning attached. The worker's recommendation is input, never the answer."

[1:05 — Show the safety net.]
[Screen: the acceptance section.]
"And when it goes wrong: the diff is tens of thousands of lines because an editor
 reflowed the whole file. The skill says preserve the evidence first, then decide —
 never 'the diff looks like noise, revert it'."

[1:20 — Close.]
"It's two installable skills, English and Chinese, MIT, and the safety rules are
 the product. There's one script: it makes the message inbox atomic, deduplicated,
 and honest about failures — and when a message actually arrives it ends itself to
 wake me, because a file on disk can't start a turn. Link below."

[1:30 — END]
```

**Delivery notes.** Do not demo a successful merge; that is the least interesting part and every
product shows it. Demo a decision. If you have time for one more beat, show the disclosure scan
rejecting something — it makes the "this is maintained carefully" claim concrete instead of
asserted.

---

## 5. FAQ

**Is this tied to Orca?**
Yes, and it says so up front. The procedure drives `orca orchestration` verbs for dispatch, mail,
gates and lifecycle. Its *judgements* (channel choice, gate choice, decision classification) are
transferable; its *commands* are not, and the README states the boundary rather than hedging.

**Does it work with my agent?**
With whichever TUI agents your Orca profile has enabled. The skill reads Orca's current default and
the current disabled list at dispatch time instead of naming an agent, precisely so it does not rot
when you change your setup.

**Do the two language bundles differ in behaviour?**
No. They are the same procedure, and the test suite asserts parity: the same reference files, the
same eval scenarios in the same order with the same assertion counts, and a byte-identical script.

**Will it merge without asking me?**
It merges without asking you *per ticket* once the gates pass, which is the point of the workflow.
It stops and asks when the gates fail, when the repository blocks the merge, when the user asks to
keep a PR open, or when the merge would change the scope you set. Those exceptions are written down.

**Will it decide product questions for me?**
No. Scope, approach forks and the escalation five go to you. Only detail-class questions — naming,
directory placement, implementation technique, test style — are answered for you.

**What is the one script?**
`inbox-loop.py`: a standard-library inbox consumer that saves every delivery batch atomically by
delivery ID, deduplicates by message ID, acknowledges only batches that are entirely heartbeats, and
exits loudly on a CLI error instead of pretending the queue was empty. It is also the wake, and the
wake is an ending: a file on disk cannot start a coordinator turn, but the exit of a long-running
command is what the harness surfaces as a new message. So the consumer ends the process and prints
exactly one machine-readable JSON line on stdout when a newly persisted non-heartbeat batch arrives
(`reason=delivery`), when this Run's worker signature changes (`reason=worker_state`), or when the
loop itself fails (`reason=error`, non-zero exit) — which is why it is reinstalled after every wake.

**Is it safe to run?**
It installs Markdown plus one standard-library Python script; installing runs nothing. The skill's
own safety rules cover the rest, including not reading or echoing credentials, and stopping rather
than self-answering an escalation-class question.

**Is it on skills.sh?**
Check `https://skills.sh/whutlichao/orchestra-auto` for the current state. Listing is driven by
anonymous install telemetry and there is no submission step, so the honest answer is "look, don't
assume" — and nobody should install it to influence that.

**How is it licensed?**
MIT. Contributions welcome.

---

## 6. Channel drafts

All drafts below are text to be posted by a human, after review. Posting them from an automated
agent, or from this repository's CI, is out of scope: no account sign-in, no community contact, no
account binding.

### 6.1 GitHub (repository README / release notes)

Covered by [README.md](../README.md). For a release, use:

> **orchestra-auto v0.1.0** — the first public release.
>
> Two installable Agent Skills (English + 简体中文) that encode a supervised Orca workflow:
> coordinator plans and verifies, workers edit and open PRs, the coordinator merges once the gates
> pass.
>
> The interesting content is the safety rules, not the happy path: isolated worktrees, real evidence
> that a worker's turn began, live worker settings instead of hardcoded agents, atomic and
> deduplicated message handling by a consumer that ends the process to wake the coordinator,
> decisions escalated to a human, per-ticket-type gates, evidence preserved before any revert, and
> bounded retries with stop thresholds.
>
> Checks: `python3 -m unittest discover` — script behaviour against a stub CLI, dual-language parity,
> frontmatter/link/JSON validity, and a disclosure scan that is written not to cry wolf.
>
> MIT.

### 6.2 skills.sh

Do **not** write a submission post — there is no submission channel. What a maintainer can do:

1. Add `skills.sh.json` at the repository root (already present here) to group the two bundles on the
   repository page. This only changes the display; per the
   [customize docs](https://skills.sh/docs/customize) it requires no approval or allowlist.
2. Let installs happen naturally. The directory picks the repository up from anonymous install
   telemetry with the CLI's telemetry enabled.
3. Verify the state later by visiting `https://skills.sh/whutlichao/orchestra-auto` — and report what
   you actually see, not what you hope is there.

### 6.3 OpenCode community

> **orchestra-auto: a supervised-orchestration skill, in English and Chinese**
>
> I kept hitting the same failures running agents across a real repo: the orchestrator editing
> `main` because isolation was implicit, `state: ready` being treated as "the agent started",
> messages settling unhandled, scope questions answered by the agent instead of me.
>
> So I wrote the procedure down. Two installable skills, same semantics:
>
> ```bash
> npx skills add whutlichao/orchestra-auto --skill orchestra-auto      # English
> npx skills add whutlichao/orchestra-auto --skill orchestra-auto-zh   # 简体中文
> ```
>
> It's Orca-specific — it drives `orca orchestration` verbs — and says so up front. The interesting
> part is the safety rules: per-ticket worktrees, real start evidence, live worker settings rather
> than a hardcoded agent, atomic message handling with one small stdlib script that ends the process
> to wake the coordinator, decisions escalated to a human, gates by ticket type, evidence preserved
> before any revert, bounded retries.
>
> 38 scenarios per language in `evals/`, plus real behaviour tests for the inbox script (atomic
> writes, replay safety, ack discipline, the wake line, loud failure). MIT. Feedback welcome.

### 6.4 X / Twitter

> Agents are easy to start and hard to finish.
>
> I wrote down the finish: isolated worktrees, proof a worker actually started, live worker settings,
> atomic message handling, decisions escalated to a human, gates by ticket type, bounded retries.
>
> English + 简体中文, MIT 🧵

Follow-up posts (thread, one idea each): (2) `state: ready` is not evidence — the procedure for
proving a turn began; (3) the scope-change question a worker asks that a naive loop answers for you;
(4) the tens-of-thousands-line diff that was a formatter reflow, and why you preserve before you
revert; (5) one
Python script that makes an agent inbox safe to rely on.

*Do not post a thread announcing a skills.sh listing you have not verified.*

### 6.5 Reddit

Suggested subreddits: r/ExperiencedDevs, r/LLMDevs, r/LocalLLaMA (only if the discussion is about
orchestration practice, not model choice). Lead with the failure modes, not the product; the
subreddits punish launch posts and reward "here is what went wrong for me".

> **Title:** The hard part of multi-agent coding is the last 20%, not the dispatch
>
> **Body:** I've been running supervised agents across a real repo, and the dispatch was never the
> problem. The problems were:
>
> - The orchestrator quietly editing `main`, because "a fresh worker means a fresh terminal" is not
>   "a fresh worktree" and that difference bites you once and then forever.
> - Treating a `state: ready` receipt as proof the agent started. It means the bytes were written.
> - A settled message that nobody handled, because the inbox was a queue of files nobody re-read.
> - A worker asking "should I also delete this unused component?" and the orchestrator just taking
>   the recommendation. That's a scope change; it should have gone to me.
> - A diff tens of thousands of lines long that was an editor reflowing a wide file to 80
>   columns. Nearly lost the real work with it.
>
> I eventually wrote the procedure down so I'd stop re-learning it. It's published as two installable
> skills (English and Chinese, same semantics), Orca-specific:
>
> ```bash
> npx skills add whutlichao/orchestra-auto --skill orchestra-auto
> ```
>
> Most of it is prose: when to isolate, what counts as evidence, which questions go to the human,
> when to stop retrying. There's one small stdlib script that saves message batches atomically,
> only auto-acks batches that are entirely heartbeats, and ends the process when a real batch lands
> — that exit is what wakes the coordinator.
>
> Repo: https://github.com/whutlichao/orchestra-auto — happy to hear where the rules are wrong;
> I'd rather be corrected than keep guessing.

### 6.6 Hacker News

> **Show HN: orchestra-auto – a supervised agent-orchestration workflow, as a skill**
>
> https://github.com/whutlichao/orchestra-auto
>
> I run agents across a real codebase under supervision, and I kept making the same mistakes, so I
> wrote the procedure down and published it as two installable Agent Skills (English and 简体中文,
> identical semantics).
>
> It's Orca-specific — it drives `orca orchestration` for dispatch, mail and lifecycle — and the
> README says so plainly rather than claiming to be general.
>
> The content is mostly judgement calls: isolate every ticket into its own top-level worktree; don't
> treat a `ready` receipt as proof a turn began; read the live agent setting instead of hardcoding
> one; persist every message batch atomically and only auto-ack heartbeats; classify a worker's
> question before answering it, and send scope changes to the human; run gates by ticket type; keep
> the evidence before reverting an odd diff; stop after bounded retries.
>
> One small standard-library Python script handles the inbox and ends the process to wake the
> coordinator when a real batch lands. The rest is Markdown plus 38 prompt-level scenarios per
> language so the reasoning is testable, and real behaviour tests for the script.
>
> It's deliberately not portable: the judgements transfer, the commands don't. Curious whether
> others hit the same failures or different ones.

### 6.7 中文渠道（V2EX / 掘金）

> **标题：把「监督下的多 agent 编排」写成了一份可安装的 skill**
>
> 正文：
>
> 用 agent 在一个真实仓库里干活，难的不是派活，是最后那 20%。我反复踩的几个坑：
>
> - 编排者自己动手改主工作副本——「新 worker 是新终端」不等于「新 worktree」，这个差别踩一次记一辈子；
> - 把 `state: ready` 当成「agent 已经开干」的证据——它只说明字节写进去了；
> - 消息结算了却没人处理，因为收件箱只是一堆没人回头看的文件；
> - worker 问「顺手把那个没人用的组件也删了吧」，编排者直接采纳推荐——那是 scope 变更，本该问我；
> - 一个上万行的 diff 其实是编辑器把宽行文件重排到 80 列，差点把真实工作一起丢掉。
>
> 后来我把流程写了下来，做成两个可安装的 skill（中文 / 英文，语义完全一致），Orca 专用：
>
> ```bash
> npx skills add whutlichao/orchestra-auto --skill orchestra-auto-zh
> ```
>
> 内容大部分是判断规则：什么时候隔离、什么算证据、哪些问题必须问人、什么时候该停止重试。
> 只带一个小脚本（仅标准库），负责把消息批次原子落盘、只自动 ack 全是心跳的批次，并在真批次
> 到达时结束进程——那次退出就是唤醒 coordinator 的信号，所以每次唤醒之后都要重新安装。
>
> 仓库：https://github.com/whutlichao/orchestra-auto
> 欢迎指出规则里的错误——我宁愿被纠正，也不想继续猜。

**微信公众号 / 长文版本**：以「三个具体故障 + 各自的机械判据」为骨架展开，不要写成产品介绍。
中文读者对「决策问人、细节代答」这条分歧点反应最好，值得单独一节。

---

## 7. 14-day promotion cadence

A maintainer's schedule. Nothing here is automated, and nothing here is an instruction to an agent.
Adjust freely; the ordering matters more than the dates.

| Day | Action | Why this order |
| --- | --- | --- |
| 1 | Merge the release; verify the README install commands by running them yourself | Never promote an install command you have not run |
| 2 | Publish the GitHub release with the 6.1 text | Gives every later link a stable target |
| 3 | Verify the skills.sh state by visiting the public page; record what you actually see | The listing is observable, not submittable |
| 4 | Post the HN draft (6.6), weekday morning, then **be present for 3 hours** | HN rewards authors who answer; a drive-by post dies |
| 5 | Post the Reddit draft (6.5) after incorporating HN's best objection | The objection is free editing |
| 6-7 | No posting. Fix whatever day 4 and 5 surfaced, as commits | Responding with code is the strongest single signal |
| 8 | Post the Chinese draft (6.7) to V2EX | Different timezone, different audience, no overlap with HN |
| 9 | Post the OpenCode draft (6.3) to the project's community | Narrower audience; assumes context the earlier posts built |
| 10 | Post the X thread (6.4) | Short-form works better once long-form has explained it |
| 11 | 掘金 long-form (6.7), expanded | Chinese long-form benefits from the V2EX reaction |
| 12-13 | Answer issues and questions; note every requested rule change | This is where you learn if the procedure is right |
| 14 | Publish a short "what changed after feedback" note | Closing the loop is what makes the next release credible |

**Cadence rules.** One channel per day. Never post the same text twice. Always be present for a few
hours after a post. If a channel produces nothing, that is data, not a reason to post harder.

---

## 8. Metrics

**What to look at.**

| Layer | Signal | Where | What it actually tells you |
| --- | --- | --- | --- |
| Reach | Repository views, unique visitors | GitHub traffic | Whether the drafts landed anywhere |
| Conversion | Clone → install, stars | GitHub, install telemetry | Whether the README's install section works |
| Adoption | Anonymous install count | skills.sh, once visible | Whether people keep it — and it is not a target |
| Quality | Issues and discussions opened | GitHub | The only metric that measures whether the rules are right |
| Depth | PRs correcting a rule or a translation | GitHub | The strongest signal this is useful |
| Durability | Repository still maintained at day 90 | GitHub | Whether it earned its place |

**What not to optimize.**

- **Do not install the skill to move a number.** The count is anonymous aggregated telemetry; seeding
  it corrupts the one signal you have. Do not use `DISABLE_TELEMETRY` as a growth lever either — it
  is a user's privacy switch.
- **Do not chase stars.** Two people filing real rule corrections beat two hundred stars.
- **Do not claim an unverified listing.** If skills.sh has not picked the repository up, say so. A
  false "we're listed" claim costs more than the listing is worth, and it is exactly the kind of
  unverified claim this repository's own rules forbid.
- **Do not add telemetry to the packages.** The skill ships none, and the README tells users it ships
  none. Keeping that true is worth more than any metric on this page.
