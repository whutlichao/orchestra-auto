# 通信、唤醒与失联恢复

**什么时候读**：每个 Run 首次派活前，处理身份绑定、收件器、唤醒与失联恢复；后续派活只核对收件器仍有效。
命令语义以本轮 `orca skills get orchestration` 及其 messaging 与 worker-contract 参考为准。
本文件使用 `<ORCA>` 表示本轮选定的可执行文件；
脚本通过 `ORCA_CLI_COMMAND` 接收该路径。

---

## 目录

- [1. 身份和收件器](#1-身份和收件器)
  - [1.1 状态契约](#11-状态契约)
- [2. 收件与确认](#2-收件与确认)
- [3. 协调者持续运行与唤醒](#3-协调者持续运行与唤醒)
  - [3.1 契约：唤醒就是「一条会结束的命令」](#31-契约唤醒就是一条会结束的命令)
  - [3.2 本机形态：pi 与 omp](#32-本机形态pi-与-omp)
  - [3.3 没有这种能力时](#33-没有这种能力时)
  - [3.4 每次唤醒后必须重新装上等待](#34-每次唤醒后必须重新装上等待)
  - [3.5 等待方式与开关](#35-等待方式与开关)
- [4. worker 必须执行的通信契约](#4-worker-必须执行的通信契约)
- [5. 缺完成报告与失联恢复](#5-缺完成报告与失联恢复)
- [6. 收尾](#6-收尾)

---

## 1. 身份和收件器

创建 Run 后把实际返回的 Run ID、coordinator handle、各 Task/Dispatch/worker handle 写入
repo 外台账。句柄来自真实调用方身份，不从终端标题或“最新终端”推断。后台进程显式指定
`check --terminal <coordinator_handle>`；`check` 不使用 `--from`。

每个 Run 使用独立目录、唯一收件器。启动示例：

```bash
ORCA_CLI_COMMAND=<已确认的可执行文件路径> <SKILL_DIR>/scripts/inbox-loop.sh \
  <run_id> /tmp/orch-<repo>-<run_id> <coordinator_handle>
```

按 §3 的方式运行它——作为 harness 能呈现结果的阻塞调用——**不要**用 `nohup … &`。
脚本把每批投递原子落盘，并在重命名后 fsync 父目录，自动 ack 纯心跳批次，在出现需要你处理的
事件时**结束进程**：这个结束就是你的唤醒。`--status --json` 只查看目录，不消费队列，
也是新自动化必须使用的接口：

```bash
<SKILL_DIR>/scripts/inbox-loop.sh --status --json /tmp/orch-<repo>-<run_id>
```

脚本依赖 Python 3 标准库（POSIX 文件锁，适用 macOS/Linux/WSL）。启动时用文件锁排除同目录
双实例，目录绑定 Run/terminal/CLI 后禁止复用；Orca 的 waiter 槽仍是跨目录的最终约束。
不要迁入旧脚本运行目录：先确认并停止旧 loop 及其 waiter，保留旧消息，使用新的 Run 专属目录。

`--status` 只查看该目录的锁与状态，不扫描其他 Run 的进程，不消费队列。

### 1.1 状态契约

`--status --json` 在 stdout 打印一个 JSON 对象，退出码为 `0`/`1`/`2`/`3`：

| 字段 | 含义 |
| --- | --- |
| `loop` | 有 loop 持有该目录的锁时为 `alive`，否则 `dead`；锁无法探测时为 `unknown`——绝不只凭 pid 文件判断 |
| `phase` | `state.json` 记录的 `phase`（`starting`、`waiting`、`checking`、`backoff`、`reconciling`、`wake`、`error`），或 `null` |
| `saved` | `deliveries/*.json` 文件数——耐久记录，不代表已处理 |
| `unacked` | `saved` 减去有**成功**本地 receipt 的 delivery id；`pending` 与 `failed` 只是证据，不是 ack |
| `blocked` | FIFO 头部未 ack 时为 `{"deliveryId", "reason": "unacknowledged"}`，否则 `null` |
| `last_activity_at` | `state.json`、`watch.log` 与 `deliveries/` 中最新的 mtime，UTC ISO-8601，或 `null`；死掉的 loop 唯一可信的时间戳 |
| `watch` | 本次安装记录的 `WORKER_WATCH`：`on`/`off`；没有安装记录时为 `unknown` |
| `run`、`terminal` | 该目录所属绑定，或 `null` |
| `reasons` | 发现的降级原因：`blocked`、`watch_off`、`loop_not_running`，或 `fatal` / `unreadable_delivery` / `missing_state_directory` |
| `degraded` | `reasons` 非空即 `true`，调用方不必自己比较退出码 |
| `error` | loop 记录下的致命文本 |

退出码只有这四种含义：

- `0`——唤醒后的正常静止（`phase=wake` 且头部已 ack），或健康的在跑 loop：持有锁或状态干净、
  无未 ack 头部、watch 开启；
- `1`——致命错误：`phase=error`、`watch.log` 最后一条 `ERROR` 行，或 delivery 文件不可读
  （绝不当作空队列）；
- `2`——状态目录不存在；
- `3`——降级或阻塞：FIFO 头部未 ack（`blocked`）、关闭了 worker watch（`watch_off`），或 loop
  在活跃 phase 上停止且既没写唤醒也没写错误（`loop_not_running`）。

计算完全是**本地**的：delivery 文件、`receipts.jsonl`、`state.json`、`watch.log` 与目录锁。
`--status --json` 既不调用 `check` 也不调用 `check --peek`，所以它从不消费队列，也从不改变任何
批次的命运。

旧式纯文本 `--status`（不带 `--json`）保留当前的 `0`（有 loop 持有目录）/ `1`（其他一切）退出码，
保留一个 minor 版本，并在 stderr 打印弃用提示；它以文本形式展示同样的本地事实。新的 `2`/`3`
只属于 `--json`。安装命令自己的 exit `2`——「另一个实例占用该目录 / 目录已绑定到别的 Run、
terminal 或 CLI」——是另一份契约，不变；被拒绝的安装让 owner 的状态目录逐字节保持原样。

唤醒后 `loop: DEAD` 是正常静止状态——loop 是刻意退出的——此时 `state.json` 带 `phase=wake`、
原因与 `deliveryId`。`phase=error` 或 `watch.log` 里的 `ERROR` 行是另一回事：那次停止本身就是一次唤醒，重启前
必须先查清。绑定、权限或 waiter 问题解决前不自动重启，不杀其他 Run 的进程。

**判活绝不能只看 pid 文件。** 收件器退出时会 unlink `inbox-loop.pid`，所以「pid 文件不存在」
与「文件里的 pid 已不存在」都算死亡，而只有 `state.json` 说明它是**怎么死的**：`phase=error`
带错误文本，或 `phase=wake` 带唤醒原因。只轮询 pid 文件存活的外挂等待器就因此漏掉过一次真实
死亡——收件器退出时已把文件删掉，等待器把「没东西可查」读成了「一切正常」，继续睡。

脚本每轮先做无过滤 check，再装上阻塞等待，防止心跳与普通消息积压。完整响应按 deliveryId
哈希原子写入 `deliveries/*.json`，并在重命名后 fsync 父目录——只 fsync 文件不够，因为重命名
改的是目录项——所以已落盘的批次能扛住断电；文件不轮转删除。**已经在盘上且已经宣告过**的批次
从不当作新消息：它是你尚未处理的责任，脚本会暂停等待而不是为它再唤醒一次或空转。
`NEW-MESSAGE` 仅是最近落盘的提示，**不是待办清单**；收到新消息或恢复会话时枚举全部 delivery
文件，对照台账处理。

重新安装时，脚本把每个 delivery 文件与成功 receipt 对账。若最老的未 ack 批次已落盘却从未宣告，
它会发出恰好一次 `delivery` 唤醒，`detail` 写明这是对账；把 `reconciled_delivery_id` 与一条
本地观察写入 `state.json`，然后退出。第二次安装不会为同一批再唤醒，已经宣告过的批次也不算
新消息。这只发生在安装时：稳定运行期间，已落盘批次仍是你未处理的责任。对账只读一次
`worker-list` 与 `state.json`，把 worker baseline 记进观察，并且只写 `state.json`；协调者把
这条观察转录进台账，收件器自己从不写台账。观察包含 `kind`、`saved`、`unacked`、`blocked`、
`reconciled_delivery_id` 与 `baseline_worker_signature`，并带 `write_scope: state.json` 和
`transcribe_to: ledger`。

错误显式落盘并退出，不能将 CLI 失败、JSON 错误或绑定失效解释成空队列。

## 2. 收件与确认

维护逐 message ID 的台账：`received → processing → handled`，记录 reply 回执、验收结果、
终端归属决定。恢复时先对账；重复 Delivery 不重复回复、不重复合并或 release。

`processing` 这一条要写在它所描述的动作**之前**，绝不写在其后，并且要写明动作是什么（一次合并、
一个 `reply --id`、一次文件改动）以及属于哪个 attempt。顺序就是全部要害：动作可能已经完成、进程
在任何记录存在之前就死了；而 ack 要等整批处理完才发，所以批仍处于未 ack 状态，而它的效果可能已经
落地。事后补写的台账分不清这种情况和「根本没跑过」，而收件器给的是 at-least-once 投递加不重复
唤醒——它不给 exactly-once 效果。所以恢复时一条卡在 `processing` 的记录意味着**先确立事实再决定
是否重放**：读真实状态（PR 是否已合并、reply 是否已送达、文件是否已改），据证据判断，而不是据台账
判断。效果可验证且动作幂等时，用同一个 delivery ID 重放是安全的；效果无法从证据确立时，交给人类，
绝不盲目重跑——未经核实的重放绝不能只凭台账推断出来。

1. 读完整 Delivery，验证其 Run/Task/Dispatch 属于本轮权威 attempt；旧 attempt 消息保留作证据，
   不据此完成新 attempt。
2. 提问用 `reply --id <message_id>` 回答；待人工决定的记录为 pending，不能超时自答。
   worker_done 对照真实 Dispatch 和产物验收；生命周期结算不等于交付验收通过。
3. 完成整批消息及必要的终端归属决定后，才通过脚本 ack **这份已读快照**里的
   `.result.deliveryId`：`inbox-loop.sh --ack <delivery_id> <state_dir>`。它先追加 `pending`
   receipt，再调用 `orca orchestration check --ack <delivery_id>`，然后向 `receipts.jsonl`
   追加 `success` 或 `failed`。Orca 拒绝时记 `failed`，批次保持未 ack；重试用同一个 delivery
   id——本地 `request_id` 随之复用——并保留 delivery 文件。Orca 接受之前绝不写 `success`，
   所以成功 receipt 才是 ack 已落地的证明；失败时保留台账并重新核验，不假定已确认。入口在
   ack 落地时退出 `0`，Orca 拒绝或该 id 从未落盘时退出 `1`，用法错误或状态目录没有绑定时
   退出 `2`。
4. 一条等待人类决定的问题堵住 FIFO 时，头部就是 **blocked**：它的 delivery id 与原因
   `unacknowledged` 可由已落盘 delivery 和成功 receipt 计算（状态契约里的 `blocked`），所以
   先处理头部。要查看后续消息而不消费它们，执行只读
   `check --run <run_id> --terminal <handle> --all --json`，先处理其他 worker 的问题/完成
   报告并登记；`--all` 不提供可 ack 的 Delivery，不编造 ID。原批次完成后逐批消费，对已处理
   消息只对账。

每次 ack——自动或协调者发起——都向状态目录的 `receipts.jsonl` 追加一行
`{delivery_id, request_id, acked_at, result}`，只增不改。`pending` 写在 Orca 调用之前，
`success` 或 `failed` 写在调用之后；`request_id` 是稳定的本地键 `ack:<delivery_id>`，拒绝后
重试复用它。收件器从未落盘的 delivery id 会在调用 Orca 前被拒绝：`check --all` 不产出可 ack
的 id，任何 id 都不得编造。receipt 文件使 `unacked` 能减少、被堵的 FIFO 头部能解除。

脚本只会自动 ack 已落盘且**每条都是 heartbeat**的非空批次；混合批次和未知类型一律交给
coordinator。自动 ack 写同样的 pending/success receipt；Orca 拒绝时记 `failed`、批次保持未
ack，并以 error 唤醒结束进程——脚本绝不自行重试。类型过滤只决定 waiter 何时醒来，不改变
整批处理义务。脚本不会删除本地待办，队列为空也不说明台账都已完成。

## 3. 协调者持续运行与唤醒

`send` 成功是 durable enqueue；wake/nudge 是 best effort。`nohup`、文件出现、后台
收件器存活都不等于 agent 会产生新 turn，不能据此宣称“会自动继续”。

### 3.1 契约：唤醒就是「一条会结束的命令」

与 harness 无关，唤醒只有一种形态：

> 唤醒 = 一条**结束（或输出）会被当前 harness 呈现成一条 agent 消息**的命令。

除此之外没有东西能叫醒你。收件器就是被做成这条命令的：它先把批次落盘，然后结束进程并在
stdout 打印恰好一行。这才是正确的分工，写全了就是：**收件器负责落盘，并在唤醒事件上结束。**
它仍然从不发消息、从不代为回复、从不 ack 需要人看的批次。

唤醒行（stdout，恰好一行）：

```json
{"event": "wake", "reason": "delivery", "run": "run_ab12", "terminal": "term_coord",
 "deliveryId": "dlv_9f", "types": ["worker_done"], "messageCount": 1,
 "saved": "/tmp/orch-repo-run_ab12/deliveries/<deliveryId 的 sha256>.json", "detail": "new batch dlv_9f saved: 1 message(s) ['worker_done']"}
```

| 字段 | 含义 |
| --- | --- |
| `event` | 恒为 `wake`——这一行的用途，解析方不必猜 |
| `reason` | `delivery`（新批次已落盘）、`worker_state`（worker 签名变化）、`error`（收件器自身致命错误） |
| `run`、`terminal` | 这次安装所绑定的 Run 与句柄 |
| `deliveryId` | 触发唤醒的批次；另外两种原因下为 `null` |
| `types` | **整批**消息的类型，按 FIFO 顺序；非批次唤醒时为 `[]` |
| `messageCount` | 该批消息条数；非批次唤醒时为 `0` |
| `saved` | 唤醒**之前**已写入的文件；非批次唤醒时为 `null` |
| `detail` | 一行说明原因；`error` 时是错误文本，已压平成一行并截断 |

退出码：`0` 唤醒；`1` 运行中的收件器自身的致命错误——同样是一次唤醒，带同一行（`reason=error`）
以及 `watch.log` 与 `state.json phase=error`；`2` 循环尚未启动就被拒绝安装——本状态目录已被另一个
实例占用，或该目录已绑定到另一个 Run/句柄/CLI（两者都打印同样的 `reason=error` 那一行，并在
`watch.log` 里留一条 `ERROR`，且都不去动该目录所属循环的 `state.json`）——或参数即用法错误，
后者是唯一一条 stdout 什么都不打印的路径：循环根本没启动，于是它在 stderr 上说明，也没有可上报的
状态目录。除此之外，除 `--status` 与 `--ack` 外 stdout 只有那一行，没有别的内容。

三条腿都会唤醒，每条都因为另外两条看不见它而存在：

1. **新消息**——盘上原先没有、且并非全是心跳的批次。纯心跳批次会被自动 ack，**不**唤醒：
   它不携带任何决策。已经落盘且已经宣告过的批次的 FIFO 重放也不唤醒——那是你尚未处理的责任，
   不是新消息，照台账处理即可。唯一的例外在安装时：已落盘却从未宣告的未 ack 批次会产生恰好
   一次对账唤醒（§1），而持久化的 `reconciled_delivery_id` 防止第二次。
2. **收件器自身死亡或错误**——非零退出本身就是一次唤醒，因为 harness 呈现的正是“进程结束”；
   `reason=error` 那一行说明原因。静默停掉的收件器是 §5.1 的「彻底安静」，不是空队列。
   现场实例：Orca app 重启时收件器正阻塞在 `check --wait`，CLI 回 `runtime_unavailable`
   （“The Orca runtime closed the connection before responding…”，并给出 `request-show --request
   <id>` 指引）。脚本落盘 `phase=error`、非零退出并打印 `reason=error`；消息不会丢——队列仍在
   服务端，下次安装立刻取回。**绝不能为了迁就唤醒契约把错误退出改写成正常退出**：退出本身就是
   唤醒，而吞掉失败的退出恰好会掩盖你正需要看的那次事故。
3. **worker 结算状态**——每 `WORKER_POLL_SECONDS`（默认 15 秒），脚本用只读
   `worker-list --run <run_id> --include-remote --cursor <cursor> --limit <n>` 把本 Run 的
   worker 归约成签名。它沿 `page.nextCursor` 一直读到 `page.hasMore` 为假，所以落在后续页上的
   remote worker 也属于签名；一页声称还有行却没有可用且未出现过的 cursor——或整个 `page`
   对象缺失——就是**截断**读取，按错误上报，绝不当作完整签名比较。与安装时取的签名不一致即
   唤醒。这一条负责抓住**没有发消息就变了状态**的 worker——崩溃、被重排、没报完成就结算——
   这样你不必干等一个可能永远不来的 `worker_done`。

   每行是 `dispatchId:workerState:dispatchStatus:stage.detail`，再延伸到实际存在的标量字段：
   `liveness.verdict`（现行 CLI 的字段）、`liveness.state`（spec 的字段名，缺失时为 `unknown`
   ——这处偏离是刻意的并已记录，而不是悄悄换名）、`attention.requiresAction`、
   `nextAction.kind` 与 `stage.detail`，另外只有 `liveness.reason` 存在时才附带末尾段
   `liveness.reason=<值>`。缺失字段一律序列化为 `unknown`，对象值字段归约为 `unknown`
   而不是整块嵌入，绝不用计数当签名。

比较要比较**整行签名，绝不要比个数**。个数看不出「一个 worker 被另一个替换」，而在事件之后
才取快照正是真实漏唤醒的成因：那次提问落在快照之前，没有触发任何东西，直到改成内容级指纹
才可靠。同理，不要让单一信号（文件出现、`NEW-MESSAGE` 标记、心跳）替代「这里有东西」。

**`WORKER_WATCH=0` 关掉第 3 条腿，是降级，绝不是健康。** 安装会把 `watch: off` 记进
`state.json`；`--status --json` 报 `watch: "off"`、`reasons: ["watch_off"]`、退出 `3`。文档化的
替代做法是协调者自己装一个周期读取——`worker-list --run <run_id> --include-remote --json`，
读遍每一页，间隔沿用 `WORKER_POLL_SECONDS`——并把带时间戳的观察记进台账。第 3 条腿是唯一能
看见「worker 没发消息就改了状态」的唤醒，关掉它只是把这份职责移回协调者，不是取消它。

### 3.2 本机形态：pi 与 omp

它们的 bash 工具把长命令呈现为作业：非 PTY 命令超出前台等待窗口后被移入后台作业
（`Backgrounded as job <id> …`），其完成或失败由作业管理器作为 follow-up 消息投递；结果也会
自动投递，无需轮询——`wait` 会阻塞到下一个后台结果或 peer 消息，安全上限 30 分钟。所以唤醒
就是：

- 把收件器作为那条长命令装上，然后结束 turn；
- 它的 stdout 到达时，**那条消息就是唤醒**：处理批次，然后装上下一次等待（§3.4）。

轮询收件器的 stdout，或在前台 sleep 盯着作业，就是要被替代的反模式：既浪费 turn，又可能
错过 harness 自己的投递。

### 3.3 没有这种能力时

退回有界前台档：保持 coordinator 当前 turn，用工具有界等待，每次不超过 60 秒；返回后
读收件箱与台账并继续其他已授权工作。不能发 final 后假定后台脚本会叫醒自己。若环境必须结束
turn：明确报告仍在运行的 Dispatch、最后进展、台账目录与无法自动唤醒的限制；恢复入口是读该
台账及 Orca 当前状态。不能把这个状态写成已完成。

### 3.4 每次唤醒后必须重新装上等待

每次唤醒都会刻意结束进程，所以**处理消息与装上下一次等待是同一步**。没重新装 = 用户说的
「戳一下动一下，然后又不反应了」：批次落盘了、唤醒到了、消息也处理了，但已经没有东西在等，
下一条消息无人接收。把「续装」当成 §2 里的 ack 一样，是处理完一批的收尾动作。

超时不是唤醒。阻塞的 `check --wait` 到 `--timeout-ms` 后返回空批次，脚本自行退避并装上
下一次等待，什么都不打印。

### 3.5 等待方式与开关

收件器用 CLI 原生阻塞等待——`check --wait --types worker_done,escalation,question
--timeout-ms <n>`——而不是「短查 + sleep」轮询。可以直接依赖的事实（取自 `check --help`）：
`--types` 只是唤醒**条件**；`--wait` 返回的 Delivery 永远是整批 FIFO、绝不按类型过滤；
CLI 每 15 秒向 **stderr** 写一行 JSON keepalive，供调用方判断进程存活。`_keepalive` 与
heartbeat 消息无关。脚本把该子进程的流放在自己的文件里，keepalive 永远不会污染 stdout 上的
唤醒行。

| 环境开关 | 默认 | 含义与默认理由 |
| --- | --- | --- |
| `ORCA_CLI_COMMAND` | `orca` | 本轮确认的可执行文件；脚本把它用于每次调用 |
| `WAIT_TIMEOUT_MS` | `30000` | 单次阻塞 `check --wait` 的上限。超时后脚本自行续装等待，所以这个值只决定重连频率；应明显小于 harness 给该调用的期限 |
| `BACKOFF_SECONDS` | `5.0` | 超时或 ack 掉纯心跳批次后、装下一次等待前的停顿，防止瞬间返回的 CLI 被打爆 |
| `PENDING_BACKOFF_SECONDS` | `30.0` | FIFO 头部是你已落盘但尚未 ack 的批次时的暂停间隔。取值较长，因为服务端此时没有变化 |
| `WORKER_POLL_SECONDS` | `15.0` | 第三条腿在等待中与暂停中重读 worker 签名的间隔。短到能在两次 30 秒等待之间发现变化 |
| `WORKER_WATCH` | 开（`1`） | 第三条腿本身的开关；`0`（或 `false`/`off`/`no`）关闭。默认开：每个间隔只有一次只读列表调用，且它是唯一能看见「worker 变了状态却没发消息」的那条腿；若某 Run 的 worker stage 细节频繁抖动，或本机 CLI 无法应答 `worker-list --run`，就关掉它。关掉是文档化的降级（`watch: off`、退出 `3`），并接管上面的替代职责：自己轮询 `worker-list --include-remote` |

## 4. worker 必须执行的通信契约

这些要求必须写进 §4.4 的 spec，worker 不会自动看到本文件：

- 使用 live preamble 的真实可执行文件、handle、Task ID、Dispatch ID 与附加 authority 参数。
- 每次自然检查点主动 `check --terminal <自己的句柄>`，包括开工、测试后、换文件前、完成前。
  coordinator 对运行中的 worker 用 `send --to dispatch:<id>`；发送后需有读取/执行证据。
- ask 超时用同一 message ID resume；心跳遵循 preamble；不直接开用户提问界面。
- 成功或失败都从自己的终端发送一次 worker_done，显式 outcome、三句摘要与真实产物路径；
  发送成功回执留作证据后结束回合。plain final、PR 已开、心跳或“我完成了”不算生命周期报告。
- 发送结果不明先查 Dispatch/消息事实；已结算不重发。仅确认未结算、报告未入队且 authority
  仍有效时允许一次重试；仍失败则保留产物、报告通信阻塞。consumer_fenced 时立即停止。

## 5. 缺完成报告与失联恢复

### 5.1 消息已发出，协调者却看不到

「它发的东西到我这儿毫无反应」通常断在五处之一。按顺序核；前两条覆盖绝大多数情况。

| 断点 | 症状与一眼判据 | 动作 |
| --- | --- | --- |
| 唤醒 | worker_done 已在盘上或队列里，但协调者的 turn 早已结束、没有东西起新 turn：delivery 文件比你上一次 turn 更新，或 `NEW-MESSAGE` 早已过期，而 §3 的唤醒方式从未被确认 | `send` 成功只证明 durable enqueue；wake/nudge 是 best-effort。先确认自己处在 §3 哪一档：harness 能呈现作业完成时，收件器的结束——§3.1 那行唤醒——**就是**唤醒；若它始终没来，核对收件器是否按 §3.2 装的（而不是 `nohup`），以及上一次唤醒后是否装回了下一次等待（§3.4）。没有这种 harness 就停在 §3.3 的有界档，或在结束 turn 前记录无法自动唤醒的状态。绝不能把「进程还活着」当成「我会被叫醒」 |
| FIFO 头部未 ack | 早先消息正常，之后的 worker_done 再不出现，批次头部压着一条未回答的提问或决策 | `check` 返回 Run 下**最老未 ack** 的 Delivery，并一直重放这一批直到 `--ack`，所以一批没 ack 就挡住它后面的一切。头部处于 `blocked`，带 delivery id 与原因 `unacknowledged`，由 delivery 文件与 `receipts.jsonl` 计算。整批处理完再通过 `inbox-loop.sh --ack` ack 该快照的 `deliveryId`（§2 第 3 条），让 receipt 记下 pending 与 success/failed。人类决策堵住头部时，用只读 `check --all` 读后续消息（§2 第 4 条）；`--all` 从不出产可 ack 的 ID。ack 不是回复 |
| 动作被打断 | 批仍是未 ack，台账那条停在 `processing`，进程死在「做完事」和「记下事」之间——所以效果可能已经落地 | 先确立事实再重放（§2）：读真实状态——合并查 `pr view`、回复查回执、文件就读文件本身。只有效果可验证且动作幂等时，才用同一个 delivery ID 重放；无法从证据确立的效果交给人类，不要盲目重跑。这就是 `runtime_unavailable` 那条判断往上一层：变更可能已经生效，所以要证明它、或者去问，绝不假设 |
| 身份/邮箱 | 哪个收件箱都收不到，或消息落在你没在看的地方 | `check` 用 `--terminal <handle>` 认调用方且不接受 `--from`；从终端标题、"最新终端"或别的会话/Run 拿来的 handle 指向错误邮箱。handle 取自真实调用方身份（`orca status --json`）并写入台账。worker_done/heartbeat 不能发组地址，其 sender handle 必须精确等于 dispatch assignee，否则该任务永不结算 |
| 收件器已死 | 彻底安静（连心跳也没有）：`--status` 报 DEAD 且 `phase=error`、或 `watch.log` 有 `ERROR` 行；或收到的唤醒行 `reason=error` | 脚本刻意把 CLI 失败、JSON 错误与绑定丢失落盘并非零退出，一次停止从不等价于空队列——而且那次停止本身就是一次唤醒（§3.1）。判死看 `state.json`，不要只看 pid 文件（§1）。读 `ERROR` 行与 `detail`；绑定/waiter 问题通常意味着别的实例或别的 Run 的 loop 占走了槽。在重新下发任何东西之前，先按 CLI 自己给出的恢复指引走：`runtime_unavailable` 错误会附带 `request-show --request <id>` 指引，而那次改动可能已经生效；用 `request-show` 确认、再用 `--retry-request <id>` 重放，而不是当成一次全新尝试。收件器自己从不重试。waiter 槽是跨目录资源：起新 loop 前先确认并停掉旧 loop，绝不迁移旧状态目录 |
| worker 压根没发 | 只收到心跳，或收到声称成功的 plain final；被自动 ack 的纯心跳批次看着有流量，实际什么也没报 | 把 §4 的通信契约写进 spec，并按 5.3 分类 |

### 5.2 三处同查，不能只看一处

报告缺失或 worker 像失联时，把这三处一起读；任何单独一处都可能显得空，或在并不健康时显得健康：

1. **本地**：`--status --json` 看 `loop`、`phase`、`saved`、`unacked`、`blocked`、
   `last_activity_at`、`watch` 与 `reasons`（§1.1）——一次本地读取，绝不消费队列；枚举**全部**
   `deliveries/*.json`，而不是只信
   `NEW-MESSAGE`（它只提示最近落盘的批次，不是待办清单）；把它们与 `receipts.jsonl` 里的成功
   receipt 对比（未 ack 的头部即 `blocked`）；读 `watch.log` 的 `ERROR` 行、`state.json` 里
   最近一次对账观察，以及 `phase=wake` 的原因——那是上一次安装结束的缘由。
2. **服务端队列**：只读 `check --run <run_id> --terminal <handle> --all --json`。
3. **worker 状态**：`worker-list --include-remote --json`（有分页须读完），再对相关 Dispatch
   读 `worker-show` 与 `worker-read`——`projection.attention.requiresAction`、字面
   `projection.nextAction`、`liveness.reason`。

按 5.3 分类。不能只盯 `NEW-MESSAGE`，也不能只等 `worker_done`。

### 5.3 生命周期分支

| 观察 | 动作 |
| --- | --- |
| Dispatch 已结算，收件箱没 worker_done | 用 `check --all`、真实结算状态及产物对账，补记录并继续验收；不要求重发、不另派重复编辑者 |
| worker 回合已结束、未结算、最后 turn 无 worker_done | 读当前 Orca recovery 指南。确认该终端仍属于原 active Dispatch 且允许恢复时，发一次明确的补收尾指令：检查 follow-up、核验产物、从自己的 preamble 报 worker_done；复核 turnStart/transcript |
| send 入队但 worker 未读 | 检查是否卡在 ask、权限提示或回合已 idle；对已确认的本地空闲终端可用终端续跑机制提醒其读取原 Dispatch 邮箱，不能把正文重复派成新 Task |
| liveness=unverifiable / agentWait=null / 标题空闲 | 仅为缺证据；读 transcript/worker-show 和 nextAction，不 stop、不 release、不重派 |
| 有真实退出证据且无法恢复 | 按 recovery 指南处理 stop/abandon/retry，保留产物，旧 attempt 权威结束后才派恢复 worker |
| 身份/Run 绑定错误、consumer_fenced | 停止该身份操作，核验真实调用方与权威关系；不伪装 handle、不无条件新建 Run |

补收尾或唤醒尝试最多一次，随后复核证据；仍无进展时保留状态并报告阻塞，不循环注入 Enter、
不重复报告、不以静默授权重派。coordinator 永远不代发 worker_done，不替 worker 改代码或提交。

## 6. 收尾

完成报告、验收和合并分别记录。release 仅在真实结算且产物已持久化后按生命周期协议执行；
已结算但待验收/合并的终端不得静默遗留，需在台账保留明确的下一步与处理期限。
所有预期 Dispatch 处理完且台账无待办后，停止本 Run 的收件器（读取本目录的 loop PID，
先确认进程身份再 SIGTERM）；脚本会回收自己的 CLI 子进程。已经因唤醒而退出的收件器无需收尾——
不要再装任何东西，把 `state.json` 留作记录。持久审计报告保存关键回执和结论后，才考虑清理
临时消息文件。
