# 派活闭环：指南拉取、worktree 覆盖、turn 确认、通信闭环

**什么时候读**：每次 B/C 派活都读，以获取完整 worker-start 参数、turn 开始确认叙述、编排指南拉取，或超出 SKILL.md 短 stub 的通信闭环核验细节时。判定请求走哪条路由时也要读[入口证据](#入口证据)——那张表是路由的唯一权威，下面的前置 skill 表从它取值。

## 目录

- [4.2 取编排指南](#42-取编排指南只取一次存盘)
- [4.3 覆盖 Orca 的默认落点](#43--覆盖-orca-的默认落点本-skill-最关键的一条)
- [4.3.0 前置 skill：按阶段与票型选](#430-前置-skill按阶段与票型选不按通道)
- [入口证据：路由表与能力核对](#入口证据)
- [4.3.1 派活后确认 turn 开始](#431-派活后确认-turn-开始)
- [4.3.2 长程派活](#432-长程派活一张票图一个-worker)
- [4.5 通信闭环](#45-通信闭环)

### 4.2 取编排指南（只取一次，存盘）

```bash
<ORCA> skills get orchestration > /tmp/orchestration-guide-<repo>.md
```

之后 grep 盘上文件。不要反复 `head -120` / `sed -n '250,420p'` 翻同一个输出——那会白白烧掉
几十次工具调用（实测有会话为此花掉几十次 bash）。只存在 `/tmp`（§2）。遇到具体 action gate
时再 `--reference references/<file>.md`。

### 4.3 ★ 覆盖 Orca 的默认落点（本 skill 最关键的一条）

**Orca 默认把 worker 放进当前（= main）工作区**——「A fresh worker means a fresh agent
terminal, **not** a new Git worktree」。这与 §2 的铁律直接冲突，**orchestra-auto 明确覆盖
Orca 的这条默认**。

**项目绑定来自调用方 Orca 上下文，不是 shell 的 cwd。** coordinator 通常就站在目标 repo 里；
但若会话 cwd 与目标 repo 不同（对 fixture、对另一个仓库派活），必须显式加 `--repo <selector>`，
否则 worktree 会被建到 cwd 所属项目里去。这条是实际派活时踩出来的。

每个任务 worker 一律：

```bash
<ORCA> orchestration worker-start --spec "<spec>" \
  --worktree new-top-level --name <票 slug> --setup run --agent <§0 的 worker> --json
```

- `--worktree new-top-level`：独立顶层 worktree，从下文选定的 base ref 切。
  **不许 `current`，不许 `new-child`**——前者就是 main，后者可能落在 main 的子目录里。
- `--name`：用票 slug，方便事后认领与打回时定位。
- `--agent <§0 的 worker>`：**必须显式写，没有「用默认」的写法**——`worker-start` 在 `--agent`
  缺失或不被识别时直接抛 `agent_unconfigured`，它不查默认 TUI agent（那个默认只服务于
  GUI composer 路径）。所以这一栏不能省，但它的**值**来自 §0 的 worker 栏。

  不同 agent 的标题、权限提示和 TUI 恢复判据见 [启动与恢复参考](worker-start-and-recovery.md)；仅在回执未确认 turn 开始或遇到交互提示时读取。

- 判断某个 agent 能不能用，以 §0 查询到的实时**禁用列表**为准，而不是 `which`；
  Orca 可能识别某个 agent，但当前 profile 仍将它禁用。实时设置查询是唯一的配置判据；本表中的
  带日期的观察只用于解释已验证的启动行为，不能用来推断当前默认值或可用性。
- `--spec` 一次成型（`worker-start --spec` 会同时建 Task 与 attempt）。只有在多票有依赖、
  需要计划式 fan-out 或重试已知 Task 时才用 `task-create` + `worker-start --task <id>`。
- 回执拿到后**立刻**走 §4.3.1 确认 turn 真的开始，并核验 §4.5 的原生唤醒契约。

**base ref：每个仓库只读一次 fork 状态，在首次向它派活之前。** 事实是现成的，skill 只是要读它：

```bash
orca repo show --repo <selector> --json            # 非 fork 时 result.repo.upstream 为 null
orca repo search-refs --repo <selector> --query <默认分支> --json
```

- `upstream` 为 **null**（自有仓库）：维持现状。从仓库默认 base ref 切 worktree，不传
  `--base-branch`。
- `upstream` 非 **null**（fork）：先 `git fetch upstream`，从 `upstream/<默认分支>` 切，并显式
  传 `--base-branch upstream/<默认分支>`。少了这一步，fork main 的私有提交会作为 gap 一起进入
  PR——worker 分支等于 `upstream` 的共同祖先 + fork 堆在自己 main 上的全部提交 + 本票改动，
  而 upstream 从来没有那段历史。
- 判断依据是**交付终点**——PR 落到哪个仓库——不是改动大小或票型。这是仓库级的事实，一次定下，
  不是逐票临时判断；同一仓库后续的主张必须与它一致。
- 确实依赖 fork 私有提交的票是唯一例外：传 `--base-branch origin/<默认分支>`，并写明理由。

**fork 的默认分支保持纯镜像。** 它只应是 upstream 默认分支的镜像；私人改动走自己的分支或
PR，不要堆在 fork 的 main 上。在这个约定下 §6.5.1 的 `--ff-only` 同步恒成立。已经积累了私有
提交的 fork 要先把它们处理掉——合入 upstream 或丢弃——不要靠 base 选择再掩盖一轮。

**协调者工作区落后会被拒绝派发。** 派发前先核对 coordinator worktree 相对目标 base 的落后量——
fork 下它跟踪 `origin/main`，worker 却从 `upstream/main` 切，所以先 `git fetch upstream`。
超过阈值时 Orca 拒绝派发，Task 留在 `ready` 且没有 Dispatch，表面像「worker 没起来」：
按 §9 取证（`worker-list --run` 的 `stage`/`outcome`、没有收到消息），拉取/rebase 或重建
worktree，**不要重派**。`allow-stale-base: true` 只用于落后是预期状态，并在 spec 写明理由
（Orca 1.4.245 测得阈值 20 个提交，属版本范围观察；以当前 CLI help 与 guide 为准）。

### 4.3.0 前置 skill：按阶段与票型选，不按通道

spec 里的 `前置 skill：` 写的是**你实际在跑的那个阶段**的 skill——取自[入口证据](#入口证据)里记录的路由行。
没有固定的「通道→skill」映射：已持有有效 spec 的一轮从 `to-tickets` 进入，已有票图的从
`implement-spec` 进入，需要落盘回答的研究问题用 `research`，修 bug 先 `diagnosing-bugs` 再 `implement`。

派活前确认所选路径可读，且本阶段的依赖齐备：`to-spec` / `to-tickets` 要 tracker 配置，
`implement-spec` 要票图与 tracker，任一阶段都要它后续会用到的 skill。缺失 → 按 §9 带证据报告那次失败的解析。不凭记忆换一个路径，也不在本轮擅自安装。

`wayfinder` 要区分 **chart 建图** 与 **work 逐票推进**。判据是**规划本身**超过一个 session 且通往
destination 的路线不清，而不是文件、票或实施 worker 变多。建图先问清目的地、广度探索，创建可描述的
决策票并连边；获授权时启动研究，然后停止。逐票模式先认领 frontier，按类型解决并更新地图；
每 session 最多解决一张非 research 票。research 是 AFK，prototype 与 grilling 是 HITL，
task 依阻塞内容判断。决策票不同于实施票；每次调用和最终地图分别按 G-wayfinder 验收。
研究和原型在授权下可用一次性分支和上下文指针，无需给临时产物套用实施 PR 契约。

## 入口证据

### 路由表：请求进入哪条路的唯一权威

下表是**请求进入哪条路**的唯一权威。SKILL.md §1 决定通道与交付终点——A/B/C 是监督与交付形态，
不是 skill——§4.3.0 再把选定的路映射成 spec 必须携带的前置 skill 路径。上游 `ask-matt` 是这些路的
设计依据；监督、隔离、授权与恢复规则是本项目的适配；每个 skill 的原文仍是它那个阶段怎么做的权威。

选行之前先取证：用户目标、可观察行为、范围、验收和关键取舍。已有 spec 或票只有在核对过
它适用当前请求、未过期且解决了这些问题后才能作为入口；没有 spec 不是跳过 grill 的理由；
用户明确点名某个 skill 时优先于任何一行；改动会推翻的 ADR 或计划决策先定夺。

| # | 条件 | 该行需要的输入 | 路由（skill 与依赖） | 完成证据 | 回流到 |
| --- | --- | --- | --- | --- | --- |
| 1 | 普通解释、状态、讨论；口头研究结论 | 问题本身 | 在当前上下文直接答复；不创建 Run、不派 worker、不写项目文件 | 答复已交付 | — |
| 2 | 新想法、计划或设计，存在未决取舍，且没有工作目录 | 用户原话 | `grill-me`（用户调用）→ `grilling` | 每轮问题、回答与理由有记录；frontier 为空且用户确认共同理解 | 结论所需阶段对应的行 |
| 3 | 同上，但有工作目录 | 用户原话与代码库 | `grill-with-docs`（用户调用）→ `grilling` + `domain-modeling`；只有真正收敛出术语或难以撤回的决定时才写术语表和 ADR | 同第 2 行，另加仓库领域约定要求的记录 | 结论所需阶段对应的行 |
| 4 | 边界清楚的小改动或修复、续跑票，或已有有效 spec / 票 | 当前目标、范围、验收，以及每个既定决定的来源 | `implement`（修 bug 先 `diagnosing-bugs`） | 模式声明记录范围、验收和为何无需重新访谈；出现新分叉即回到第 2/3 行重新路由 | G-impl |
| 5 | 规划本身超过一个 session 且通往 destination 的路线迷雾重 | 目的地或地图指针；tracker 操作 | `wayfinder`（用户调用）：**chart** 或 **work** | 建图：目的地、广度访谈、地图、类型化决策票与真实阻塞边；逐票：认领票已解决且地图已更新；地图完成要求所有范围内决定已完成、无未解决 fog | `to-spec`（第 10 行），或有依据地转为小工作直接实施（第 4 行） |
| 6 | 决定依赖可查证资料 | 问题与来源 | `research`（AFK） | 带引用的 Markdown 落盘；结论回流当前决策，不替用户作决定 | 提出问题的 grill/规格阶段 |
| 7 | 讨论不足以判断外观、逻辑或状态 | 原型要回答的那一个问题 | `prototype`（HITL） | 用户对具体产物作出反馈；问题与结论有记录；一次性分支或指针 | 提出问题的阶段 |
| 8 | 决定依赖他人掌握的信息 | 接收人与所需回答 | `to-questionnaire`（用户调用）；生成文档不授权发送 | 问卷已写好并按指定流程送达；收到真实回答前票保持阻塞 | 提出问题的 grill/规格阶段 |
| 9 | 需求已对齐，单 session 可实现 | 已定的决定 | 同一上下文 `implement`，或向一个 worker 交付完整工作说明 | 适用票的验收证据 | G-impl |
| 10 | 已对齐的实施跨 session | 已定决定；有 spec 时附 spec | `to-spec` → `to-tickets`（均用户调用）；之后逐票 `implement` 或整图 `implement-spec` | 用户核对测试 seam（G-spec）与票粒度、阻塞关系（G-tickets） | G-spec / G-tickets / G-impl |
| 11 | 外部原始 issue 或 PR 需要澄清 | tracker 与仓库 triage 配置 | `triage`（用户调用） | 标签与状态遵循仓库约定；`to-tickets` 产出的票已可供 agent 领取，不再重复 triage | agent-ready 的 issue 进入 `implement` |
| 12 | 难复现故障或回归 | 针对本故障的紧反馈环 | `diagnosing-bugs`，然后 `implement` | 反馈环在旧代码为红、修复后为绿；回归测试锁定 | 修复后视需要 `retro` |
| 13 | 架构痛点，或测试 seam 难以建立 | 代码库 | `improve-codebase-architecture`（用户调用） | 用户选定候选 | 带选定方案进 `grill-with-docs`（第 3 行） |
| 14 | 只有人类能完成的步骤 | 被阻塞的步骤 | `wizard`（模型调用） | 交互脚本；人类逐步确认；agent 继续原任务 | 被阻塞的任务 |
| 15 | 交付完成、重复失误或流程故障 | 会话一手证据 | `retro`（用户调用） | 候选写在仓库外；人类选定后才由 worker 落地 | 后续轮次 |

`domain-modeling` 与 `codebase-design` 是下层词汇层；`writing-for-agents` 维护面向 agent 的文档。
`teach`、`wait-what` 保留为独立、用户主动选择的入口，不进入固定链。实验性上游 skill 不作为默认依赖。

### 调用类型、wrapper 依赖与宿主能力

派活前分别解析并记录到 spec 的三件事：

1. **调用类型** — 已安装 `SKILL.md` 的 frontmatter 决定它是用户调用（`disable-model-invocation: true`）
   还是模型调用。用户调用的 skill 由用户明确请求或宿主接受的调用形式进入；coordinator 不把一条路径
   当成一次调用。模型调用的 skill 可由需要它的阶段到达。
2. **wrapper 与依赖** — `grill-me` 包装 `grilling`；`grill-with-docs` 调用 `grilling` 与
   `domain-modeling`；`wayfinder` 通过 `research`、`prototype`、`grilling` 和 `task` 解决票；
   `implement` 驱动 `tdd` 并以 `code-review` 收尾。wrapper 的一行正文不证明依赖运行过，读过 wrapper
   也不是证据。逐项传递依赖的真实路径，并在派活前确认可解析、可读。
3. **宿主能力** — 把宿主实际支持的调用形式写进 spec 的 `前置 skill` 区块。宿主禁止该调用时，按本项目
   已记录的适配方式读取文件并如此标注；必需 skill 完全无法解析时，按 §9 报告能力缺口。
   不得把直接读取报告成原生调用，也不得安装本轮未获授权的 skill。

`setup-matt-pocock-skills` 遵循同一政策：§1.1 的门明确点名它，自动发现和绝对路径都不能绕过宿主限制；
grill 专用的 G-docs 判据不用于 setup。跨新上下文传递用户原话、回答与约束；
`grilling` 和 `prototype` 的决策或反馈须来自用户。

初始化门的判据要现场取证，三项缺一即未初始化：`AGENTS.md` 或 `CLAUDE.md` 里有 `## Agent skills` 块
（按 setup 自己的选择规则：有 `CLAUDE.md` 就改它，不要另建一份）；`docs/agents/issue-tracker.md` 与
`docs/agents/domain.md` 存在且非空（装了 `triage` 时还有 `docs/agents/triage-labels.md`）；追踪器是
GitHub / GitLab 时，在目标仓库内 `gh repo view` / `glab repo view` 能打印项目。这三项判据就是
`scripts/init-check.py check` 报告的内容，外加一个确定取证根目录的 `repository` 探针：
退出 `0` 已初始化、`1` 未初始化（三项缺一）、`2` 无法判定；
取证以脚本为准，`unknown` 按未判定处理，不当作未初始化，逐退出码的处理见 SKILL.md §1.1。
`setup-matt-pocock-skills`
带 `disable-model-invocation: true`，须明确指定名称、调用形式和路径，别指望它从技能清单里自己出现。它一次问一个问题
（追踪器 / triage 标签词表 / 领域文档布局），问回来的按 SKILL.md §5 处理；产物属治理文档，仍走
worker 落盘并核验初始化产物，不落 main；grill 专用的 G-docs 判据不用于 setup。

### 4.3.1 派活后确认 turn 开始

`state: ready` 仅表示 Dispatch 就绪。回执的 `turnStart: observed` **就是**启动证据：接受它，
直接进入等待——下面的取证步骤是给无法携带它的回执用的。当它为 `unsupported`、`unobserved`
或缺失时，读取[启动与恢复参考](worker-start-and-recovery.md) 并用该 agent 实际支持的证据确认开始。
Orca 允许无终端 worker，先看 `worker-show` 的句柄，不能对无终端 worker 强行使用 `terminal show`。
空闲标题没变化也不是判决：只有在屏幕显示完整 preamble 停在输入框且没有正在跑的 turn 时，
才允许补一次裸 Enter。无法证实时按 §9 取证，不进入盲等。这一步的清单形式见
[派活前检查](dispatch-preflight.md) §4.0.2。

### 4.3.2 长程派活（一张票图一个 worker）

G-tickets 验收完整票图后，为整张图启动 **一个** worker：`--name <feature>-integration`，
spec 以前置 skill `implement-spec` 开头，并带 `Tracker：` / `Spec：` / `Tickets：` /
`Integration branch：` 指针（见 [长程票图](long-horizon-tickets.md)）。它必须先证明能建隔离的
嵌套 worktree，再花整张图；不能证明就串行跑 implementer，或由 coordinator 回退到逐票 worker
（默认并发 3）。coordinator 只验收唯一的集成 PR，不逐票验收嵌套分支，也不进入嵌套 worktree。

### 4.5 通信闭环

读[通信与恢复](inbox-and-waiting.md)。仓库外台账记录 Run、Task、Dispatch、worker、消息 ID、处理状态、证据和 PR。派活后核验 turn 开始，再结束 turn 等待 Orca 原生唤醒。处理完整消息或批次，按消息 ID 去重，重放前核对不确定的副作用。只有 agent 自己消费邮箱；不要启动旧收件器或后台消费式 check。按文档使用 inbox 和 retry 恢复上下文；静默时检查 worker-list、worker-show、worker-read 与成果，静默本身不能证明进程退出。

进度看板：每个实际编排 Run 首次派活前初始化并自动打开；之后在正常动作完成后按
[进度看板](progress-dashboard.md)节点责任表记录显式事实及结构化 task/dispatch/attempt 报告；
记录错误只警告，不能阻断本流程。
