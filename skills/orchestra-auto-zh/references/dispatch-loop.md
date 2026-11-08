# 派活闭环：指南拉取、worktree 覆盖、turn 确认、通信闭环

**什么时候读**：每次 B/C 派活都读，以获取完整 worker-start 参数、turn 开始确认叙述、编排指南拉取，或超出 SKILL.md 短 stub 的通信闭环核验细节时。

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

spec 里的 `前置 skill：` 写的是**你实际在跑的那个阶段**的 skill。没有固定的「通道→skill」映射：
已持有有效 spec 的一轮从 `to-tickets` 进入，已有票图的从 `implement-spec` 进入，
需要落盘回答的研究问题用 `research`，修 bug 先 `diagnosing-bugs` 再 `implement`。

| 本轮的阶段 | 前置 skill |
| --- | --- |
| 解释、状态、讨论 | 无（A 通道）；明确要求交互 skill 时除外 |
| 明确要求非代码访谈 | `grill-me` → `grilling` |
| 边界清楚的小改动、修 bug、已有 spec 或票 | `implement`（修 bug 先 `diagnosing-bugs`） |
| 必须落盘的研究 | `research` |
| 对着代码库把设计问尖锐 | `grill-with-docs` |
| 跨 session 且路径不明：绘制或推进决策地图 | `wayfinder` |
| 需要可运行的一次性原型来讨论 | `prototype` |
| 规格 | `to-spec` |
| 拆票 | `to-tickets` |
| 实施完整票图 | `implement-spec` |
| 外部 issue、冲突、术语 | 仓库的 triage / 冲突处理 / `domain-modeling` 流程 |
| 仓库还没有初始化产物 | 先补 `setup-matt-pocock-skills`，再进入上表对应阶段（SKILL.md §1.1） |

派活前确认所选路径可读，且本阶段的依赖齐备：`to-spec` / `to-tickets` 要 tracker 配置，
`implement-spec` 要票图与 tracker，任一阶段都要它后续会用到的 skill。缺失 → 按 §9 带证据报告那次失败的解析。不凭记忆换一个路径，也不在本轮擅自安装。

`grill-me` 包装 `grilling`；`grill-with-docs` 调用 `grilling` 与 `domain-modeling`。
派活前解析并传递全部依赖路径，只读 wrapper 不等于执行依赖。跨新上下文传递用户原话、回答与约束；
`grilling` 和 `prototype` 的决策或反馈须来自用户。遵守宿主对用户调用入口的限制，缺失时如实报告。

`wayfinder` 要区分 **chart 建图** 与 **work 逐票推进**，传目的地或地图指针及 tracker 操作。
建图先问清目的地、广度探索，创建可描述的决策票并连边；获授权时启动研究，然后停止。
逐票模式先认领 frontier，按类型解决并更新地图；每 session 最多解决一张非 research 票。
research 是 AFK，prototype 与 grilling 是 HITL，task 依阻塞内容判断。决策票不同于实施票；
每次调用和最终地图分别按 G-wayfinder 验收。研究和原型在授权下可用一次性分支和上下文指针，
无需给临时产物套用实施 PR 契约。

初始化门的判据要现场取证，三项缺一即未初始化：`AGENTS.md` 或 `CLAUDE.md` 里有 `## Agent skills` 块
（按 setup 自己的选择规则：有 `CLAUDE.md` 就改它，不要另建一份）；`docs/agents/issue-tracker.md` 与
`docs/agents/domain.md` 存在且非空（装了 `triage` 时还有 `docs/agents/triage-labels.md`）；追踪器是
GitHub / GitLab 时，在目标仓库内 `gh repo view` / `glab repo view` 能打印项目。这三项判据就是
`scripts/init-check.py check` 报告的内容，外加一个确定取证根目录的 `repository` 探针：
退出 `0` 已初始化、`1` 未初始化（三项缺一）、`2` 无法判定；
取证以脚本为准，`unknown` 按未判定处理，不当作未初始化，逐退出码的处理见 SKILL.md §1.1。
`setup-matt-pocock-skills`
带 `disable-model-invocation: true`，只能按路径点名，别指望它从技能清单里自己出现。它一次问一个问题
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
