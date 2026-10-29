# 发布材料 — orchestra-auto

**本目录下所有内容都是草稿。** 它们存在的意义是让维护者审阅后自己决定发不发。准备这些材料
不涉及**任何**账号登录、**不接触**社区、**不代为发帖**、**不提交**账号绑定或第三方授权。
你是维护者，这些草稿随你改；你不是，那就开 PR 改文字，由维护者去发。

读下面任何一段之前，先记住两个事实：

1. **不存在面向作者的投稿入口。** 按
   [skills.sh FAQ](https://skills.sh/docs/faq)，仓库是通过匿名安装遥测自动出现在目录里的。
   没人「提交」skill；有人安装，计数自然跟着走。因此这里的草稿**都不声称本仓库已被收录**，
   也不该有人这么写。
2. **这里没有任何东西值得为了推动一个数字去发。** 不要为了刷排名去安装这个 skill。下面这些
   渠道是给真正遇到这个问题的人看的。

英文版见 [launch-kit.md](launch-kit.md)。

---

## 1. 定位

**一句话。** 一套监督下的 Orca 工作流：coordinator 规划、派活、验收，worker 编辑并创建 PR，
门禁通过后 coordinator 合并——不必逐张票据请人确认。

**它解决什么问题。** 在真实代码库里编排 agent，开始容易、收尾难。失败的形态非常具体、且会
反复出现：

- 编排者自己动手改主工作副本，因为「隔离」从没被写明；
- worker 回执写着 `ready`，所有人就默认 turn 已经开始了；
- 消息已经结算，却没人注意到它从未被处理；
- scope 问题由 agent 自己答了，而不是交给人；
- 同一道门禁连挂两次，循环原地重试而不是停下；
- 上万行的 diff 其实是格式化重排，真实工作跟着被一起丢掉。

**这个 skill 是什么。** 一份把上述失败形态逐一点名、并给每条配上具体对策的流程文档，外加
一个小脚本，让消息收件箱真正可以依赖。

**它不是什么。** 不是通用编排框架，不能移植到别的 CLI，也不做决策。它明确写着：范围、方案
分叉，以及一切涉及凭证、第三方、付费、审批、不可逆数据的问题，都由人来定。

**给谁用。**

- 已经在用 [Orca](https://www.onorca.dev/)、并且会同时监督多个 agent 的团队。
- 见过一次编排以「未审阅的合并」或「静默丢失的交接」收场的人。
- 希望把*过程*钉死、但产品决策仍归自己的维护者。

**不给谁用。** 没有 Orca 的人——这套流程驱动的是 `orca orchestration` 的动词，它不会假装
自己通用。以及想找一个 `import` 就能用的库的人。

**为什么是两个语言包而不是一个。** 它们是两个语义相同的独立安装包。双语团队两个都装，让每个
agent 读自己那门语言，而不是所有东西都过一层翻译。

---

## 2. 受众

| 人群 | 在哪 | 什么内容能打动他们 |
| --- | --- | --- |
| 跑多 agent 的 Orca 用户 | Orca 官方文档与社区 | 那些具体的失败形态——他们一眼就认出来 |
| Agent skills 早期采用者 | skills.sh、`skills` CLI、Hacker News | 一个非玩具型 skill：它编码的是判断力，不是包了一个 API 调用 |
| 被未审阅的 agent 合并坑过的团队负责人 | Reddit（r/ExperiencedDevs、r/LLMDevs）、HN | 门禁与停止阈值 |
| 中文 AI 工程圈 | V2EX、掘金、微信公众号 | 「决策归人、流程自动化」这条分野，用中文读特别顺 |
| skill 作者 | GitHub | 包结构、双语一致性测试、脱敏扫描 |

---

## 3. 安装

```bash
# English
npx skills add whutlichao/orchestra-auto --skill orchestra-auto

# 简体中文
npx skills add whutlichao/orchestra-auto --skill orchestra-auto-zh
```

Orca 前提：已安装 Orca 且 CLI 可达。用 `orca skills get orchestration` 验一下——这条指南拿不到，
这个 skill 就没法工作。细节见 [README](../README.zh-CN.md#前提与能力边界)。

---

## 4. 90 秒 demo 讲稿

目标是展示**一个 skill 判断正确、而朴素编排器判断错误的决策**，然后展示安全网。方括号里的
是舞台提示，不念出来。

```text
[0:00 — 冷开场。说问题，别说产品。]
「多 agent 的 demo 都能跑通。几乎没有一次多 agent 运行能干净收尾。
 有意思的部分不是派活——是最后那 20%。」

[0:10 — 第一个失败形态。]
[画面：两张票、两个 worker，回执都写着 state: ready。]
「两条回执都写着 ready。那只说明字节写进去了，不说明 agent 开始了。
 朴素循环这时候就去开收件箱，然后一直等。」

[0:25 — 这个 skill 的对策。]
[画面：SKILL.md §4.3.1。]
「这个 skill 要求你先证明 turn 真的开始了——读终端标题，跟那个 agent 的
 空闲横幅比——然后才进等待循环。要是它没开始，补救是补一次裸回车，
 不是重发整份 prompt。」

[0:40 — 第二个失败形态，烧钱的那个。]
[画面：worker 的提问——「顺手把那个没人用的组件也删了吧？」]
「worker 问了个完全合理的问题，还附了推荐答案。朴素编排器直接采纳，
 继续往前走。」

[0:52 — 分类。]
[画面：SKILL.md §5.1。]
「这个 skill 要求先分类。目录位置？自己答。『顺手把另一个东西也删了』？
 那是 scope 变更——交给人，附上你的倾向。worker 的推荐是输入，不是答案。」

[1:05 — 安全网。]
[画面：验收一节。]
「以及出错的时候：diff 上万行，因为编辑器把整个文件重排了。
 skill 说先留证据、再判断——绝不是『看着像噪声，还原吧』。」

[1:20 — 收尾。]
「两个可安装 skill，中文和英文，MIT，安全规则本身就是产品。只有一个脚本：
 它让消息收件箱原子化、去重、并且如实报告失败——真有消息到来时它会结束自己来
 唤醒我，因为落盘的文件开不了一次 turn。链接在下面。」

[1:30 — 结束]
```

**讲法提醒。** 不要 demo 一次成功的合并——那是全场最没意思的部分，而且所有产品都在演它。
要 demo 一个**决策**。如果还有多一个节拍的时间，展示脱敏扫描拒绝某个东西——它能让「这个仓库
维护得很仔细」从声称变成事实。

---

## 5. 常见问题

**它绑定 Orca 吗？**
是，而且它一开头就写明了。流程驱动的是 `orca orchestration` 的动词：派活、邮件、门禁、生命周期。
它的*判断*（通道选择、门禁选择、决策分类）可以迁移；它的*命令*不行，README 直说这条边界，
不绕弯子。

**支持我的 agent 吗？**
支持你 Orca profile 已启用的那些 TUI agent。skill 在派活时读 Orca 的当前默认值与当前禁用列表，
而不是写死一个名字——正是为了你换设置时它不会烂掉。

**两个语言包行为一样吗？**
一样。同一套流程，而且测试套件断言一致性：同样的 reference 文件、同样的场景按同样顺序排、
每个场景的断言数相同，脚本逐字节相同。

**它会不问我就合并吗？**
门禁通过后它不再*逐张票*问你，这正是这套流程的意义。门禁失败、仓库阻止合并、用户要求保留
PR、或合并会改变你设定的范围时，它会停下并问你。这些例外都写在文档里。

**它会替我决定产品问题吗？**
不会。范围、方案分叉与 escalation 五类都交给你。只有细节类问题——命名、目录位置、实现手法、
测试写法——才代答。

**唯一那个脚本是干什么的？**
`inbox-loop.py`：一个只用标准库的收件器，按 delivery ID 原子保存每一批投递、按 message ID 去重、
只 ack 全是心跳的批次，遇到 CLI 报错就显式退出而不是假装队列为空。它同时就是唤醒本体，而唤醒就是
一次结束：落盘的文件无法开启 coordinator 的 turn，而一条长命令的结束正是 harness 会呈现为新消息的
东西。所以收件器会在下列时刻结束进程、并在 stdout 打印恰好一行机器可读的 JSON——新落盘的非心跳批次
（`reason=delivery`）、这个 Run 的 worker 签名变化（`reason=worker_state`）、或循环自身失败
（`reason=error`，非零退出）。也正因为如此，每次唤醒之后都要重新安装它。

**能安全运行吗？**
它安装的是 Markdown 加一个只用标准库的 Python 脚本；安装本身不执行任何东西。其余部分由 skill 自己
的安全规则覆盖，包括不读取、不回显凭证，以及遇到 escalation 类问题时停下而不是自答。

**它在 skills.sh 上吗？**
自己去 `https://skills.sh/whutlichao/orchestra-auto` 看当前状态。收录由匿名安装遥测驱动，不存在
投稿步骤，所以诚实的回答是「去看，别假设」——并且不要为了影响它去安装。

**许可？**
MIT。欢迎贡献。

---

## 6. 各渠道草稿

以下全部是「由人审阅后发布」的文字。由自动化 agent 或本仓库 CI 代发，均超出范围：不登录账号、
不接触社区、不提交账号绑定。

### 6.1 GitHub（README / release notes）

由 [README.zh-CN.md](../README.zh-CN.md) 覆盖。发版时可用：

> **orchestra-auto v0.1.0** — 首个公开发布。
>
> 两个可安装的 Agent Skill（中文 + English），把一套监督下的 Orca 工作流写下来：
> coordinator 规划与验收，worker 编辑并创建 PR，门禁通过后 coordinator 合并。
>
> 有意思的是安全规则而不是顺风路径：隔离的 worktree、worker turn 真的开始的证据、
> 读实时 worker 设置而不是写死 agent、由收件器负责的原子且去重的消息处理（真批次到达时它结束
> 进程来唤醒 coordinator）、决策升级给人、按票型的门禁、
> 任何还原之前先留证据、有界重试与停止阈值。
>
> 检查：`python3 -m unittest discover` — 对着桩 CLI 测脚本行为、双语一致性、
> frontmatter/链接/JSON 合法性，以及一份刻意不误报的脱敏扫描。
>
> MIT。

### 6.2 skills.sh

**不要**写投稿帖——不存在投稿渠道。维护者能做的：

1. 在仓库根放 `skills.sh.json`（本仓库已有），把两个包分组显示在仓库页上。按
   [customize 文档](https://skills.sh/docs/customize)，这只影响展示，无需审批或白名单。
2. 让安装自然发生。目录会通过 CLI 匿名安装遥测把仓库收进去（需遥测开启）。
3. 之后访问 `https://skills.sh/whutlichao/orchestra-auto` 核验状态——并报告你**实际看到**的，
   不是你希望看到的。

### 6.3 OpenCode 社区

> **orchestra-auto：一套中英双语的「监督式编排」skill**
>
> 我在真实仓库里跑 agent 时反复撞到同几个失败：编排者自己去改 `main`（因为隔离没写明）、
> 把 `state: ready` 当成「agent 已开始」、消息结算了没人处理、scope 问题被 agent 自己答了。
>
> 所以我把流程写了下来。两个可安装 skill，语义完全一致：
>
> ```bash
> npx skills add whutlichao/orchestra-auto --skill orchestra-auto      # English
> npx skills add whutlichao/orchestra-auto --skill orchestra-auto-zh   # 简体中文
> ```
>
> 它是 Orca 专用的——驱动 `orca orchestration` 的动词——并且一开头就说清楚。有意思的部分是
> 安全规则：每票独立 worktree、真实的启动证据、读实时 worker 设置而不是写死、原子化的消息处理
> （一个小脚本，真批次到达时它会结束进程来唤醒 coordinator）、决策升级给人、按票型的门禁、
> 还原前先留证、有界重试。
>
> 每种语言 `evals/` 里有 38 个场景，另有收件脚本的真实行为测试（原子写入、重放安全、ack 纪律、
> 唤醒行、显式失败）。MIT。欢迎反馈。

### 6.4 X / Twitter

> agent 干活，开始容易，收尾最难。
>
> 我把收尾写下来了：隔离 worktree、worker 真的开始的证据、实时 worker 设置、原子化的消息处理、
> 决策升级给人、按票型的门禁、有界重试。
>
> 中文 + English，MIT 🧵

后续帖（串，一条一个观点）：(2) `state: ready` 不是证据——证明 turn 开始的流程；
(3) worker 问的那个 scope 变更问题，朴素循环会替你答掉；(4) 那个上万行、其实是格式化重排的
diff，以及为什么先留证再还原；(5) 一个 Python 脚本，让 agent 收件箱真正可以依赖。

*不要发一条宣告某个你尚未核实的 skills.sh 收录状态的串。*

### 6.5 Reddit

建议版块：r/ExperiencedDevs、r/LLMDevs、r/LocalLLaMA（仅当讨论编排实践而非选型时）。以失败
形态开头，不要以产品开头；这些版块惩罚 launch 帖、奖励「我踩了什么坑」。

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

## 7. 14 天推广节奏

这是给维护者的日程。这里没有任何自动化，也没有任何一条是给 agent 的指令。可自由调整；
顺序比日期重要。

| 天 | 动作 | 为什么是这个顺序 |
| --- | --- | --- |
| 1 | 合并发版；自己把 README 的安装命令跑一遍 | 绝不推广一条你没跑过的安装命令 |
| 2 | 用 6.1 的文字发 GitHub release | 给后面每个链接一个稳定目标 |
| 3 | 访问公开页面核验 skills.sh 状态；记录你实际看到的 | 收录是可观察的，不是可提交的 |
| 4 | 工作日早上发 HN 草稿（6.6），然后**在场 3 小时** | HN 奖励回复的作者；发完就走等于死帖 |
| 5 | 吸收 HN 最好的那条反驳后再发 Reddit 草稿（6.5） | 那条反驳是免费的编辑意见 |
| 6-7 | 不发帖。把第 4、5 天暴露的问题以提交形式修掉 | 用代码回应是最强的单一信号 |
| 8 | 把中文草稿（6.7）发到 V2EX | 不同时区、不同受众，与 HN 不重叠 |
| 9 | 把 OpenCode 草稿（6.3）发到项目社区 | 受众更窄，前提是前面几帖已经建立了语境 |
| 10 | 发 X 串（6.4） | 长文解释清楚之后，短内容效果更好 |
| 11 | 掘金长文（6.7）扩展版 | 中文长文能从 V2EX 的反应里获益 |
| 12-13 | 回 issue 和问题；把每一条被要求的规则改动记下来 | 这里才知道这套流程对不对 |
| 14 | 发一篇短的「反馈之后改了什么」 | 闭环才让下一次发布可信 |

**节奏规则。** 一天一个渠道。同一段文字绝不发两次。发帖后一定在场几小时。某个渠道毫无反应，
那是数据，不是发得更猛的理由。

---

## 8. 指标

**看什么。**

| 层 | 信号 | 在哪 | 它实际说明什么 |
| --- | --- | --- | --- |
| 触达 | 仓库浏览、独立访客 | GitHub traffic | 草稿有没有落到人眼前 |
| 转化 | clone → 安装、star | GitHub、安装遥测 | README 的安装一节到底能不能用 |
| 采用 | 匿名安装计数 | skills.sh（可见之后） | 人有没有留下来——而它不是目标 |
| 质量 | 新开的 issue 与讨论 | GitHub | 唯一能衡量「规则对不对」的指标 |
| 深度 | 修正规则或翻译的 PR | GitHub | 它有用的最强信号 |
| 持久 | 第 90 天仓库仍在维护 | GitHub | 它是否配得上自己的位置 |

**不要优化什么。**

- **不要为了推动一个数字去安装。** 那个计数是匿名聚合遥测；自己灌水就毁掉了你唯一的信号。
  也不要把 `DISABLE_TELEMETRY` 当增长手段——那是用户的隐私开关。
- **不要追 star。** 两个提交真实规则修正的人，胜过两百颗星。
- **不要声称未经核实的收录。** skills.sh 还没收本仓库，就照实说。一条假的「我们已收录」的代价
  远高于那个收录本身，而且它恰好是本仓库自己的规则所禁止的那类未经验证的声称。
- **不要给包加遥测。** skill 不带遥测，README 也告诉了用户它不带。守住这一点，比这一页上任何
  指标都值钱。
