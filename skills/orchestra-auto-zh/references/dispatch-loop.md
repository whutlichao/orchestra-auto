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

- `--worktree new-top-level`：独立顶层 worktree，从仓库默认 base ref 切。
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
- 回执拿到后**立刻**走 §4.3.1 确认 turn 真的开始，并核验 §4.5 的收件器与等待方式。

### 4.3.0 前置 skill：按阶段与票型选，不按通道

spec 里的 `前置 skill：` 写的是**你实际在跑的那个阶段**的 skill。没有固定的「通道→skill」映射：
已持有有效 spec 的一轮从 `to-tickets` 进入，已有票图的从 `implement-spec` 进入，
需要落盘回答的研究问题用 `research`，修 bug 先 `diagnosing-bugs` 再 `implement`。

| 本轮的阶段 | 前置 skill |
| --- | --- |
| 解释、状态、讨论 | 无（A 通道） |
| 边界清楚的小改动、修 bug、已有 spec 或票 | `implement`（修 bug 先 `diagnosing-bugs`） |
| 必须落盘的研究 | `research` |
| 对着代码库把设计问尖锐 | `grill-with-docs` |
| 规格 | `to-spec` |
| 拆票 | `to-tickets` |
| 实施完整票图 | `implement-spec` |
| 外部 issue、冲突、术语 | 仓库的 triage / 冲突处理 / `domain-modeling` 流程 |

派活前确认所选路径可读，且本阶段的依赖齐备：`to-spec` / `to-tickets` 要 tracker 配置，
`implement-spec` 要票图与 tracker，任一阶段都要它后续会用到的 skill。缺失 → 按 §9 带证据报告那次失败的解析。不凭记忆换一个路径，也不在本轮擅自安装。

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

### 4.5 通信闭环：入队、收件、处理、唤醒分别核验

派活前读 [通信与恢复协议](inbox-and-waiting.md)。每个 Run 维护一份 repo 外台账：
Run/coordinator handle、Task/Dispatch/worker handle/worktree、最后进展、消息 ID、处理状态、
完成回执、PR、下一次检查时间。上下文压缩或恢复后先读台账与真实 Orca 状态，不凭旧对话猜身份。

- 首个 worker 启动前准备唯一收件器与等待方式；每次派活后核验 turn 已开始并检查收件器。
- 每个 Run 一个收件器，绑定真实 coordinator handle；消息按 deliveryId 原子保存，
  重启不覆盖、不轮转删除未处理证据。收件器同时就是唤醒：出现新的非心跳批次或 worker 状态变化时，
  它结束进程并在 stdout 打印一行（见 [通信与唤醒](inbox-and-waiting.md) §3），因此要作为 harness
  能呈现的调用装上，并在每次唤醒后重新装上。
- 每条消息按 message ID 去重，验证 Run/Task/Dispatch。处理整批及所需终端归属决定后才 ack。
  当前批次被人类决策阻塞时用只读 `check --all` 检查后续消息，及时处理其他 worker；
  不能把 ack 当回复，也不能从另一次 check 抽取一个未审阅的 deliveryId 来 ack。
- 长操作结束后与空闲时都收件。单次前台等待不超过 60 秒；连续三轮没有进展便查
  worker-list/worker-show/worker-read、队列与本地台账，按证据恢复，不能无限盲等。
- 除环境强制结束的例外外，主动结束仍有在途任务的 coordinator turn 前必须有**已确认能触发下一 turn 的通知机制**。未验证的后台进程、
  消息文件或 send 成功不算。没有自动唤醒时保持当前 turn 有界轮询；环境必须交还控制权时，
  明确说明尚在运行、不能自动唤醒和恢复入口，不声称会自动继续，不默认要求用户定期催促。
- worker 缺 worker_done 或已 idle 的恢复、错误与停止条件，按 reference 执行。
