# 运行健康（Run health）

**什么时候读**：每次唤醒后、向用户报告 Run 状态之前，以及 worker 失联时。每次唤醒跑一次
`health evaluate`，输入就是那次唤醒携带的一条 observation；收件器结束就是唤醒，因此不需要常驻
检查器，恢复轮次可以从未变的状态重建。

健康检查只回答一个问题：**这个 Run 此刻能证明什么？** 它不回答「工作做得好不好」——那是验收，
也从不授权 release。

## 五种状态

| 状态 | 含义 |
| --- | --- |
| `healthy` | 每个期望信号都在窗口内新鲜，且无需任何动作 |
| `degraded` | 非关键信号陈旧、watch 关闭、或投影落后；可以继续，但要记录原因 |
| `blocked` | 等待人类或 coordinator 决策（单向门、escalation、审批、头部阻塞批次）；不自动推进 |
| `lost` | 有正向证据证明 agent 已退出且未发 `worker_done`，或联系丢失且有正向终止证据 |
| `unknown` | 证据缺席——从未观测、超 TTL 无正向证据、liveness 不可验证。既不健康也没失败；下一步是取证 |

**聚合采用 worst-of**：`lost > blocked > unknown > degraded > healthy`。一个 `unknown` 信号
胜过任意多个 `degraded`；未观测的信号一律 `unknown`，绝不 `healthy`。字段缺失永远不是通过。

## 信号与窗口

下列窗口都是**可配置默认值**，不是产品要求。`health evaluate` 接受 `--config <file>`，值写成
JSON 对象；写明数值是为了让人能推理，不是让测试依赖某个常量。

| 信号 | 来源 | 窗口 | 失效时 |
| --- | --- | --- | --- |
| heartbeat | worker 消息 / 台账（`last_heartbeat_at`） | preamble cadence × 2 + 60s grace | 有正向存活证据 → `degraded`，否则 `unknown` |
| inbox phase / `updatedAt` | 收件器 `state.json` | waiting / checking / backoff 15 分钟；`error` 立即 | 陈旧 → `degraded`，出错 → `blocked` |
| unacked 批次 | delivery 文件 vs ack 回执 | 每个检查点 | `blocked` |
| worker-list 快照 | Orca `worker-list` | 5 分钟，且仅在有活跃 worker 时 | `degraded` |
| worker-show 观测 | Orca `worker-show` | 5 分钟 | `degraded` |
| turnStart | 回执 / 观测 | 120s 窗口；10 分钟未证实则升级 | `unknown` → 升级 |
| progress freshness | `progress.json` | 300s | 只记 observability gap |
| last effective progress | 台账 / 消息，或收件器 observation | 连续 3 轮无进展 | `degraded` |
| reclaimable 终端 | worker list | 每次收尾；超过 10 分钟 | `degraded` |
| waiter slot 泄漏 | 进程 / 锁 | 每次重装 | `degraded`；阻止安装时 `blocked` |
| nested roster | worker 报告 | 长程票首次报告 | `unknown` → `degraded` |

表里三条最容易做错的规则：

- **心跳**。阈值是**实时 preamble cadence** × 2 加 grace。preamble 没有 cadence 时心跳
  **不评估**，信号为 `unknown`。绝不预设五分钟：猜出来的阈值会把安静的 worker 变成假警报，
  或把已经死掉的 Run 判成绿色。
- **turnStart**。`turnStart: observed` 是回合已开始的证据，直接接受；`unsupported`、
  `unobserved` 或字段缺失都是 `unknown`，不是失败。窗口内按 agent 支持面取证（终端标题或屏幕、
  `worker-read`、状态投影、产物），不要判派活失败；超过窗口再升级。
- **`observation.agentWait`**。字段存在且为 `null` 是正向观测：Orca 查过，没有等待。对象表示
  worker 在等决策，即 `blocked`。字段**缺失**表示 Orca 从未查过，是 `unknown`。等待中的
  worker 是健康的——唤醒后休息不是失败，`ready` 也不是已开始的回合。在 **signals 快照**里同一
  事实是 `worker_show.agent_wait` 键：`null`、对象或缺失，含义同上。快照的其余键是
  `worker_show.status`（及 `worker_done`）、`heartbeat.cadence_seconds` 与 `last_heartbeat_at`、
  以及每个块的 `observed_at`——整个形状是 CLI 读取的本地契约，不是对实时 Orca 回执的断言。

## 收件器挂载的 observation

**接线**。收件器每次唤醒向自己的 `state.json` 写**恰好一条**轻量 observation（`wake_observation`），
并把同一对象作为唤醒行的 `observation` 字段发出。coordinator 把它转录进台账作为 observation，
并对它跑一次 `health evaluate`。没有常驻进程：收件器负责观测，coordinator 负责评估，恢复轮次
仅凭台账就能重建同一状态。

observation 的字段及其对健康的意义：

| observation 字段 | 变成 | 含义 |
| --- | --- | --- |
| `observed_at` | 快照的 `observed_at` | 收件器查看的时刻；所有年龄都从它起算 |
| `saved`、`unacked`、`unacked_delivery_ids`、`blocked` | `unacked` | 与 `--status --json` 同一套算术；非零即 `blocked` |
| `watch` | `watch` | `off` 是 `degraded` + `watch_disabled`，绝不 healthy |
| `phase`、`error` | `inbox` | `error` 立即 `blocked`；静止唤醒不记录 phase |
| `binding` | `binding` | 下面的绑定丢失记录 |
| `last_effective_progress` | `last_effective_progress` | 下面的停滞信号 |

observation **没有**携带的信号——heartbeat、`worker-show`、`worker-list`、`turnStart`、投影、
reclaimable 终端、waiter slot、nested roster——一律读作 `unknown`。这是刻意的：单个收件器无法
证明 Run 健康，所以仅凭 observation 的评估只会说「继续取证」，coordinator 必须先用自己的读取
补齐这些块才可能到 `healthy`。

```bash
python3 "$HEALTH" evaluate --run-id "$RUN" --observation observation.json
```

`--observation` 与 `--signals` 是同一条命令，只是先做上面的翻译；`--signals` 接收完整快照，仍是
评估手工快照的方式。observation 里的 run id 与 `--run-id` 不一致时会被拒绝，而不是合并。

## 停滞检测（stall detection）

`last_effective_progress` 只在**有效进展**上推进：非心跳消息、artifact 变化、commit、stage 变化。
**心跳绝不刷新它**——心跳证明 agent 醒着，不证明工作动了，而只会心跳的 Run 正是这个信号要抓的停滞。

- `rounds_without_progress` 计连续无有效进展的轮次（唤醒）。连续三轮即 `degraded`，
  `reason: no_effective_progress`；上限由配置里的 `stall_rounds` 决定。
- `markers` 记录上一轮推进了什么，使用共享词汇 `message`、`artifact`、`commit`、`stage`。
  收到的心跳列在 `ignored_markers`，不会被悄悄计入。
- 三次空等待沿用既有规则，计入 `empty_waits`；有批次到达即清零。
- **consumer 模式**（`mode: consumer`）下，该块还必须携带 `--status --json` 的 `unacked` 计数：
  收件器读文件看不到空等待，所以空等待对它不可见，而这个计数是同一信号的一部分。缺少该计数的
  consumer 块是 `unknown`（`consumer_unacked_missing`），绝不是通过。因此空等待从不被当作
  不可见：它被计数、被发布，并与未 ack 的队头一起评估。

## 绑定丢失（binding loss）

`consumer_fenced` 或 Run 绑定丢失会停止该身份上的消费与生命周期操作。收件器把结果、错误文本与
两条拒绝——不冒充句柄、不无条件新建 Run——记为 state 里的 `binding` 以及 observation 的 `binding`
块，评估器据此定级：

| 记录的 `binding` | 健康 | 下一步 |
| --- | --- | --- |
| `outcome: consumer_fenced` | `blocked`，`reason: binding_loss` | 归属 `unproven` 时 `escalate`，否则 `record_pending` |
| `outcome: run_binding_lost` 且 `ownership: verified` | `blocked`，`reason: binding_loss` | `record_pending`——重建绑定，而不是重建 Run |
| `outcome: run_binding_lost` 但归属未证实 | `unknown`，`reason: ownership_unobserved` | `escalate` |
| `outcome: none`（查过，无丢失） | 不贡献任何发现 | 继续 |

收件器无法建立的归属一律升级，绝不猜测：它记 `ownership: unproven`，因为核验真实调用方是
coordinator 的读取，不是收件器的职责。绑定丢失绝不是 retry 触发条件，没有 block 就是没有观测——
Run 的其它信号继续决定聚合结果。

`progress.json` 可以投影 `run.health`，但健康权威在台账。投影陈旧只记一个 gap，从不抬高状态。

## 状态迁移与授权

| 迁移 | 触发 | 授权 |
| --- | --- | --- |
| healthy → degraded | 非关键信号陈旧、watch off、看板 gap | 规则 |
| degraded → healthy | 信号恢复 | 规则 |
| any → blocked | 待决策项（单向门 / escalation / 审批 / 头部阻塞） | 记录 decision/pending |
| blocked → healthy | 决策已解决 | human 或 coordinator |
| any → unknown | 证据缺席、TTL 超时、不可验证 | 规则 |
| unknown → lost | 正向终止证据 | 规则（凭证据） |
| lost → retry | 显式 retry 决策 | coordinator（人类边界内） |
| any → release | 已结算 + 收尾记录 | coordinator；不确定交人类 |

`lost` 通常从 `unknown` 到达：TTL 过期、补齐证据、由证据决定。授权 `lost` 的是**证据**而不是
上一个状态，所以已证实的终止不会被更早的绿色结论掩盖。**release 不是健康状态**，而是需要结算与
持久收尾记录的动作；健康本身从不授权 release，结算不确定时由人类决定。

## Coordinator 面

```bash
HEALTH="$SKILLS_ROOT/orchestra-auto/scripts/health.py"
RUN=run_example
python3 "$HEALTH" evaluate --run-id "$RUN" --signals signals.json
python3 "$HEALTH" evaluate --run-id "$RUN" --signals - --config windows.json
```

命令打印机器结果：

```json
{
  "schema_version": 1,
  "run_id": "run_example",
  "record_id": "…",
  "recorded": true,
  "state": "degraded",
  "reasons": [{"signal": "watch", "state": "degraded", "reason": "watch_disabled",
               "evidence": "WORKER_WATCH=0: wakes are not observed; poll worker-list periodically",
               "next_action": "poll_worker_list"}],
  "next_action": "poll_worker_list",
  "projection": {"run_id": "run_example", "run": {"health": "degraded",
                                                  "next_action": "poll_worker_list"}}
}
```

- `reasons` 每个发现一条：信号、它贡献的状态、稳定原因码、证据与细节。健康的信号不列出。
- 即使 `unknown`、`blocked` 或 `lost` 也退出 `0`：它们是观测，不是命令失败。退出 `1` 表示
  输入非法（JSON 错误、未知信号或配置键、非法记录），用法错误退出 `2`。
- 命令写入一条 `kind: health` 台账记录，`detail` 含
  `{state, entered_at, reason, signals, evidence_refs, decided_by, next_action}`。记录由快照
  推导，同一快照得到同一 `record_id`，重跑幂等。
- 每次调用都与该 Run 上一条健康记录比较，打印 `previous_state` 与 `transition`（`from`、
  `to`、`allowed`、`authorization`）。状态不变时 `entered_at` 保留进入该状态的时刻，因此
  「degraded 自何时起」可回答。表未授权的迁移以 `allowed: false` 记录，而不是默默接受。
- 从其它状态进入 `blocked` 需要记录在案的待决项；评估本身就是那条记录——阻止性发现（待决项、
  未 ack 批次、inbox error、等待中的 agent）就是证据，记录带着它。

### 记录写不进去时

健康记录写入失败**绝不阻塞编排**。评估照常打印并保留在内存，`recorded` 为 `false`，
`record_error` 给出原因。台账恢复可写后，用打印出的 id 重试同一评估：

```bash
python3 "$HEALTH" evaluate --run-id "$RUN" --signals signals.json --record-id <打印出的 id>
```

健康记录按 `record_id` 是 `replay_safe`：重试不会重复。记录持久在台账里，重启后的 coordinator
读到上次状态，而不是从零开始。`progress.json` 全程只是投影——可以显示状态，绝不能被当作权威。

## `WORKER_WATCH=0`

允许关闭 watch，但此时 Run 是 `degraded`，`reason: watch_disabled`，
`next_action: poll_worker_list`，替代方案是定期 `worker-list` 轮询。它绝不会被报成 healthy：
没有唤醒就没有任何东西能证明这个 Run 还活着。

## 怎么读结论

- `lost` 或 `blocked`：停下，做状态指明的那个决定。`lost` 之后的 retry 是 coordinator 在人类
  边界内的决定，不是规则。
- `unknown`：先补齐缺失的证据。缺席不是证明，未观测的信号也从来不是绿灯。
- `degraded`：继续，并记录原因。工作可以推进。
- `healthy`：继续。它只说明信号新鲜，不说明工作可验收；验收与 release 仍是各自的门。
