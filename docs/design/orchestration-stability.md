# Orca 编排稳定性设计

Ticket: the spec ticket in the tracker · Run: recorded in the out-of-repo ledger · Dispatch: recorded in the out-of-repo ledger
Base: main bf96405
状态：设计定稿；决策记录见 §6；本票 Answer 在仓库外台账（A4=C），工作树只放本设计文档。

## 1. 问题与目标

### 1.1 问题

orchestra-auto 把编排安全规则写成了大量散文，但缺少与副作用动作配套的**持久记录层**。结果是：

- 派发（worker-start/dispatch）在回执丢失时没有可执行的取证程序，也没有可安全重放的键；协调者可能创建第二个 Dispatch/worktree。
- 收件器（inbox-loop.py）是唯一消费者，但"已落盘未 ack"的批次不会再次唤醒，一次丢失的唤醒等于永久静默。
- 派发前 12 项检查中只有 3 项有机械判据，其余靠自述；检查结果不落盘，run 健康无法事后审计。
- run 健康没有状态机、没有持久时间戳；`--status` 把正常静止、致命错误、目录不存在压成同一个退出码。
- 验收证据分散在六处，accepted SHA、merge SHA、tracker 关闭状态没有权威持久存储；崩溃在验收与收尾之间即丢失。
- 看板（progress.json）是投影，却被期望承载派发/验收事实；看板写失败还会级联阻断同一 worker 的后续记录。
- 多个 coordinator 各自启动看板服务，无法共享；新增需求要求单例服务。

### 1.2 目标

形成一份**可执行设计**，覆盖：

1. 派发与看板记录耦合
2. 持久回执及对账
3. 派发前检查
4. 运行健康检查
5. 验收证据汇总
6. 恢复收尾对账
7. 单例进度看板（follow-up）

并确保：

- 不盲目重试有副作用的动作：动作前有稳定键，动作后先读权威，重放只限幂等、可验证、已授权的路径。
- 保留人类与 coordinator 的决策边界：单向门、范围变更、未证实失败的 retry、结果不明的 release 仍由人或 coordinator 决定。
- **绝不把看板故障升级为派发故障**：看板缺失只是可观测性缺口。

### 1.3 非目标

- 不写代码、不改脚本、不建后续实施票。
- 不改变 Orca 生命周期语义，不新增调度器，不改变自动合并的授权边界。
- 不把 progress.json 升级为权威事实源。

## 2. 现状与权威地图

### 2.1 实体

| 实体 | 含义 | 权威 |
| --- | --- | --- |
| Run | 持久命名空间与 coordinator 收件箱 | Orca |
| Task | 工作单元 | Orca |
| Dispatch | 一次权威的 Task 尝试 | Orca |
| attempt | Dispatch 的重试序号/身份 | Orca（回执字段；缺失时本地记录且不用于权威） |
| settlement | worker_done/escalation 被接受后的生命周期结算 | Orca |
| Delivery | 一条/批收件箱消息 | Orca + 收件器落盘文件 |
| PR / branch / merge | 交付与合并事实 | PR host + git |
| tracker 状态 | 票的关闭/重开 | tracker |
| progress.json | 只读投影 | 非权威 |
| 协调台账 | 意图、回执、观测、决策、待办的持久索引 | 非权威；指向权威 |

### 2.2 权威规则

1. **Orca 生命周期是编排状态的唯一权威**。终态标题、屏幕、旧数据库行、provider transcript 都不是。
2. **PR host 与 git 是分支/PR/合并事实的权威**。合并只看 PR host 的 `MERGED`、merge SHA 与 `git merge-base --is-ancestor`。
3. **tracker 是关闭/重开的权威**；关闭动作本身受仓库约定与人类边界约束。
4. **progress.json 是投影**：工具只记录显式提交的事实，不调用 Orca，不认证写入者；`merged`/`tracker_closed` 只能在真实证据后由 coordinator 写入。
5. **协调台账是持久索引**：它让崩溃窗口可见、让对账可执行，但不替代权威；对账永远先读权威，再与台账比较。
6. **缺席不是证据**：`unknown`/`unverifiable` 是一等状态；只有正向证明（`exited`、`turnStart: observed`、PR `MERGED`、tracker 读回）才授权 stop/abandon/retry/release/merge 记录。

### 2.3 现有持久化面的错配

- 唯一带稳定幂等键的收据是 progress 事件，而它被明确设计为非权威、可失败（receipts #12）。
- 成功路径的 worker-start 回执没有持久化要求；只有 failed 路径要求记台账（dispatch-dashboard #6）。
- 仓库外台账是散文，没有 schema、位置、耐久性要求（receipts #14）。
- 收件器把 delivery 文件当作"已落盘"证据，但 `atomic()` 不 fsync 父目录（receipts #11）。
- 自动 ack 丢弃 `check()` 响应，ack 回执未持久化（receipts #10）。
- 看板写失败只警告、不排队；失败与"尚未发生"在快照里不可区分（dispatch-dashboard #4、failure-corpus #14）。

结论：系统需要一层**与看板解耦的持久回执/台账**，把"可重放但不可信 / 可信但不可重放"的错配拆开：权威仍归 Orca/PR host/tracker，台账只负责意图、回执与对账索引。

## 3. 真实故障语料

完整语料来自五路只读调查（91 条发现，见 §9.2 追溯矩阵）。按证据强度分三档。

### 3.1 已证实事故（仓库历史/发布材料/eval）

| # | 事故 | 后果 | 设计落点 |
| --- | --- | --- | --- |
| F1 | 唤醒后不重装收件器 | Run 静默停摆，state.json 只留 phase=wake | §5.4 健康状态机、§5.2 收件器回执 |
| F2 | 已落盘未 ack 的批次不再唤醒 | 一次丢失唤醒 = 永久静默 | §5.2 对账、§5.4 `unacked` 信号 |
| F3 | FIFO 头部未 ack 的 Delivery 挡住后续 worker_done | 一条待人类问题饿死所有报告 | §5.4 blocked 状态、§5.2 头部阻塞对账 |
| F4 | 中断动作的 processing 台账无法区分"已生效/没跑" | 重复副作用或永久卡住 | §5.2 request_id + read-back |
| F5 | app 重启时 check --wait 返回 runtime_unavailable，动作可能已生效 | 消费者只报告不重试，或盲目重试 | §5.2 崩溃窗口表 |
| F6 | pid 文件不能判活；`--status` 正常 wake 与致命错误都返回 1 | 健康检查误判 | §5.4 退出码与机器字段 |
| F7 | 收件器签名基线在安装时重置，安装间隙变化被吸收 | 状态变化永不唤醒 | §5.4 重装全量对账 |
| F8 | 泄漏的 CLI 子进程占住 waiter slot，下次安装失败 | Run 看似被监视，实际无人监听 | §5.4 waiter slot 信号 |
| F9 | failed worker-start 的零输出 setup 被读成瞬态/配置问题 | 盲目重派或毁掉证据 | §5.2 setup receipt、§5.6 release 前置 |
| F10 | 格式化重排噪声可能连带丢掉真实工作 | 交付污染 | §5.3 预检、§5.6 收尾门禁（沿用现有恢复参考） |
| F11 | PR 事实回填提交多触发一轮完整 CI | 规则从一笔收紧为零笔 | §5.5 证据落点、A7 决策 |
| F12 | 无证据的验收声明仍可能通过；预检 grep 只证明字符串存在 | 假绿 | §5.3、§5.5 最低证据 |
| F13 | `state: ready` 不是 turnStart；标题/屏幕/权限对话框各有边界 | 把"未开始/卡权限"当"在跑" | §5.4 turnStart 观测窗口 |
| F14 | "给我 PR"被真实模型读成"停在开放 PR" | 该合并而未合并 | §5.5 终点判据、§5.6 收尾清单 |
| F15 | profile 目录多个 .db 备份时 glob 读到过期设置 | 用错 agent/禁用列表 | §5.3 实时设置查询纳入预检 |
| F16 | 空 frontier 被误当成"图做完了" | 规划门禁误判 | §5.3 预检、G-docs 证据 |
| F17 | 看板 `open` 在 viewer 启动失败时返回 1 | `set -e` 下派发前中止 | §5.1 命令边界保证 |

### 3.2 代码/测试已证实、无事故

| # | 事实 | 设计落点 |
| --- | --- | --- |
| C1 | 快照没有 Dispatch 实体；dispatch 只能由 worker 行或 event 字符串表示 | §5.1 |
| C2 | 没有 write-ahead dispatch intent；记录严格后置 | §5.1、§5.2 |
| C3 | 进度写失败没有持久 pending 标记，且级联阻断后续记录 | §5.1 |
| C4 | progress task 要求先存在，而 Orca Task 由 worker-start --spec 创建 | §5.1 task 命名空间 |
| C5 | 成功回执没有持久副本 | §5.2 |
| C6 | 恢复路径不读 progress.json，而它可能是 dispatch_id 的唯一副本 | §5.6 对账顺序 |
| C7 | 事件历史只记通用摘要，不是 payload 审计 | §5.2 台账 schema |
| C8 | `unobserved` 与 `unknown` 混用；回执已带 observed 时仍先写 warning | §5.1 |
| C9 | `verified_at` 只是写入时间；worker 更新没有 evidence 字段 | §5.1、§5.5 |
| C10 | attempt 字段映射未定义（camelCase vs snake_case） | §5.1 |
| C11 | progress `run.id` 没有 Orca 绑定 | §5.1 |
| C12 | worker 自报路径不可执行；spec 未带 PROGRESS/STATE | §5.1 |
| C13 | 对账/修复写入会刷新 `last_observed`，worker 级 freshness 可被洗新 | §5.4 |
| C14 | 台账持久性/格式未规范，弱于 progress.json | §5.2 |
| C15 | 回执后两个清单互不包含（§4.0.2 与看板表） | §5.3 统一检查点 |
| C16 | 看板 delivery 布尔是声明，不是回执；source 不认证 | §5.5 最低证据 |
| C17 | 看板锁冲突只警告返回，事实可能静默丢失 | §5.1 gap 记录 |
| C18 | 四级验收没有 revert/reopen 失效路径 | §5.5 |
| C19 | 收件器 `--status` 用 `check --peek`，文档用 `check --all` | §5.4 命令面统一 |
| C20 | `show` 硬编码 300s，不接受调用方阈值 | §5.4 信号 TTL |
| C21 | 两个语言包只做 token/脚本字节级 parity，散文语义无守卫 | §5.8 迁移、§9 票 T7 |
| C22 | 评测/测试计数在 README、launch-kit 之间不一致 | §5.5 证据一致性（后续票） |

### 3.3 合理推测（PLAUSIBLE，无事故编号）

| # | 形态 | 设计落点 |
| --- | --- | --- |
| P1 | 单向门分类靠人工读 diff，一个小触发项卡住整张集成 PR | §5.5 门决策回执、§5.6 pending item |
| P2 | consumer_fenced / Run 绑定丢失只有"停止并核验"，没有恢复流程 | §5.6 binding-loss 协议 |
| P3 | run health 没有停滞检测；heartbeat 不等于有效进展 | §5.4 stall 信号 |
| P4 | worker 推荐被当作答案 / scope 变更被自答 | §5.2 决策记录、§5.6 恢复边界 |
| P5 | 嵌套 implementer 漏报时看板无法发现 | §5.4 roster 信号 |

### 3.4 未纳入本设计 / 延后

| 发现 | 处理 |
| --- | --- |
| 双语语义 parity 只到 token/字节级（C21） | 后续票 T7：prose contract + 语义检查；本设计只要求新增条款双语同步。 |
| 评测/测试计数在 README、launch-kit 不一致（C22） | 后续票 T7：单一来源生成 + 断言；不影响本设计结论。 |
| launch-kit 内部计数矛盾 | 同上；属文档一致性，非编排稳定性。 |
| 单向门部分合并策略（P1） | 需人类决策；本设计只记录 door 决策与 pending，不改变合并授权。 |
| consumer_fenced 的具体恢复步骤（P2） | §5.6 给协议与证据门槛；逐步命令由后续票细化。 |
| worker 推荐锚定的提示词修复（P4） | 属 ask/reply 协议文本；后续票在 reply-protocol 中加"推荐不是答案"约束。 |
| 嵌套 roster 的强制字段（P5） | §5.4 列为信号；强制上报格式由长程实施票决定。 |
| 四级验收的公开不确定性（README 开放问题） | §5.5 扩展模型给出方向；最终语义由 T5 实施票与人类确认。 |

## 4. 崩溃窗口与证据源

### 4.1 三个窗口

每个有副作用的动作都有三个窗口：

| 窗口 | 已发生 | 未发生 | 恢复规则 |
| --- | --- | --- | --- |
| W1 动作前 | intent 已写 | 动作未执行 | `request-show` = absent 且权威无效果 → 可执行或放弃；有 intent 才允许判断"从未运行" |
| W2 动作后 | 动作已生效 | 结果未记录 | 权威显示效果 → 补记 result；绝不重复执行 |
| W3 两者不确定 | 可能已生效 | 可能未生效 | 先读权威；有正向效果 → 采纳；有正向缺席 → 视为未执行；权威不可读 → `unknown`，升级，绝不重试 |

### 4.2 动作→证据→规则

| 动作 | 幂等类 | 动作前键 | 动作后读回 | 不确定时 |
| --- | --- | --- | --- | --- |
| dispatch/worker-start | joinable | request_id | 回执 state/stage/turnStart/dispatchId；worker-list 按 task/spec/worktree 反查 | request-show；completed 读回执；pending 同键重放；absent 先盘点 Orca 与 git worktree |
| send | joinable | request_id | message id；check/thread 可见性 | request-show；绝不因"没回复"自动重发 |
| reply | joinable | request_id | message 状态/线程 | 同上 |
| ack | replay-safe | request_id | ack 回执 | 同键重放 |
| worker-stop/abandon | joinable | request_id | worker-show/worker-list 终态 | request-show + 盘点；未证实不重试 |
| worker-retain/release | replay-safe（结算后） | request_id | 回执 + worker-show | release_unknown 按回执恢复；未结算绝不 release |
| merge | readback-only | 无 | PR host MERGED + merge SHA + ancestry | 只读回；绝不自动重放 |
| PR create | lookup-then-create | head 分支 | PR 号/head SHA | 先按 head lookup；找到即采纳 |
| tracker close | readback-only | 无 | tracker 读回 | 只读回；关闭动作按仓库约定与人类边界 |
| progress 写入 | replay-safe（event_id） | event_id | 快照/receipt | 同 event_id 重试；失败记 gap，不阻断 |
| preflight | write-ahead | record_id | 台账 receipt | 重跑检查，不派发 |

### 4.3 两条硬规则

1. **动作前先写 intent**。intent 至少含：request_id、action、run_id、task_id、dispatch_id/attempt（若已知）、target、expected_effect、created_at、actor。
2. **不确定时先读权威，再决定**。`request-show` 的 `absent` 不是"没发生"；只有权威正向缺席（如该 Task 无 Dispatch、该 head 无 PR）才允许判定"未执行"。

## 5. 设计

### 5.0 不变量

- **I1 权威单一**：Orca/PR host/tracker 是权威；progress 是投影；台账是索引。
- **I2 动作有键**：每个副作用动作在动作前有稳定键或明确的"无键类"（merge/tracker close）。
- **I3 先读后重放**：丢响应先 request-show/read-back；只有 completed 读回执、pending 同键重放、absent 先盘点。
- **I4 缺席不是失败**：unknown/unverifiable 一等公民；只有正向证明授权破坏性动作。
- **I5 看板旁路**：看板失败记 gap，绝不改变派发/启动/验收/合并/生命周期的分类。
- **I6 决策边界**：单向门、范围变更、未证实 retry、结果不明 release、tracker 例外关闭，仍由人或 coordinator 决定。
- **I7 证据门槛**：每个验收 claim 有最低证据；worker_done 只证明结算，不证明验收。
- **I8 收尾可对账**：收尾清单逐行有权威来源与可重复性；单向门有待办项。
- **I9 单例看板**：只读、localhost、按 Run 隔离、无状态可恢复、版本可协商。

### 5.1 派发与看板记录解耦

**问题**：快照没有 Dispatch 实体（C1），dispatch 记录严格后置（C2），进度写失败会级联（C3），task ID 命名空间未定义（C4），成功回执不持久（C5）。

**设计**：

1. **Dispatch 是台账的一等 subject**。台账记录 `subject: {run_id, task_id, dispatch_id, attempt, worktree, spec_ref}`；progress 是否新增 dispatch 实体由后续实施票决定，不阻塞本设计。progress 可以用 `worker` 行或 `event` 表示 dispatch，但不得成为 dispatch 的唯一记录。
2. **派发顺序固定**：
   1. 写 preflight receipt（§5.3）。
   2. 写台账 `intent`（request_id、task/spec/worktree/attempt、expected_effect）。
   3. 执行 `worker-start --retry-request <request_id>`。
   4. 写台账 `result`（原始回执摘要 + evidence_ref）。
   5. **尽力**写 progress（worker 行/event）；失败写台账 `gap`，下个检查点用同一 event ID 重试。
   6. 回执后检查（turnStart/隔离/收件器）写 `observation`；不通过按证据处理，不重派。
3. **看板写失败不级联**：progress 写入按 event ID 幂等；失败只记 gap。若 worker 行未登记导致后续 start 更新失败，coordinator 在下一个检查点补登 worker 行（含 dispatch_id/attempt/worktree），再补 start/event。台账是补登的事实来源。
4. **task 命名空间**：Orca Task ID 是唯一 join key。若流程需要在 worker-start 前写 progress task，使用 `provisional: true` + `orca_task_id: null`，回执后绑定；绑定前不得用 progress task ID 与 Orca 对账。`worker-start --spec` 建 Task 的路径下，progress task 记录在回执后补写并绑定。
5. **`unknown` 与 `unobserved` 分离**：
   - `unknown`：尚未判定（动作刚发生、观测窗口未到）。
   - `unobserved`：已检查、未观察到。
   - 回执自带 `turnStart: observed` 时，dispatch/start 一次写入 `observed`，不再先写 warning。
   - `warning` 只表示记录缺口，不是启动失败；页面必须显示"看板 warning ≠ turn 未开始，回到 Orca 证据"。
6. **attempt 映射**：以 Orca 回执的 attempt 字段为准；缺失时记 `attempt_source: local`，本地计数不用于权威对账。字段名统一 snake_case，回执 camelCase 在写入时转换并保留原始值。
7. **run 绑定**：台账记录 `run_id`（Orca）；progress run 初始化增加 `orca_run_id`；对账第一步校验两者相等，不等即 `divergent`。
8. **worker 自报**：仅在 spec 显式带 PROGRESS/STATE 路径时启用；source=worker 的写入永不作为验收证据；coordinator 拥有验收记录。角色边界写进 spec 契约。
9. **命令边界保证看板不阻断**：`progress.py open`/record 的退出码语义改为：
   - `0`：成功或已降级（JSON 带 `degraded: true`、`reason`）。
   - `1`：调用方输入错误/状态损坏（调用方 bug，不是派发失败）。
   - coordinator 的包装函数对任何非零只记 gap 并继续；派发控制流不分支于 progress 退出码。
10. **对账写入不洗新**：对账/补登写入带 `bookkeeping: true`，不更新 `last_observed`/`last_effective_progress`；只有真实新事实才刷新。

**验收（后续实施票）**：

- 模拟 worker-start 回执丢失：台账有 intent、Orca 有 Dispatch → 对账采纳、不重派；台账无 intent、Orca 无 Dispatch → 允许重派。
- progress 写失败（锁冲突/未登记 task）→ 台账 gap、派发继续、下个检查点补登成功。
- 回执带 `turnStart: observed` → 一次写入 observed，不出现 warning 态。
- 看板 open 失败 → 退出码 0/degraded，派发继续。

### 5.2 持久回执与对账

**问题**：dispatch/send/reply 没有幂等键与 read-back（receipts #1、#7）；interrupted-action 用 delivery ID 而非 request ID（#2）；worker-start 丢响应无流程（#3）；12 类记录只有 3 类有机器身份（#4）；恢复规则不统一（#5）；三个窗口无证据映射（#6）；release 语义分裂（#9）；ack 回执未持久化（#10）；父目录未 fsync（#11）；台账无 schema（#14）。

**设计**：

1. **协调台账（orchestration ledger）**：
   - 位置：仓库外，默认 `$ORCA_LEDGER_HOME`（缺省 `~/.orca/ledger`），按 Run 分文件：`runs/<run_id>/ledger.jsonl`；保留最近 30 天或每 Run 10MB，超出归档到 `archive/`。
   - 写入者：仅 coordinator；worker 只写 progress（source=worker），验收记录由 coordinator 写（现有角色边界）。
   - 格式：append-only JSONL，一行一条记录；写入用 `O_APPEND`，写后 fsync 文件，首次创建时 fsync 父目录；容忍尾部半行（读取时丢弃最后一条不完整 JSON）。
   - 权限：目录 0700；不含凭证；与 progress 的 `state_path` 同规则——必须在任何仓库之外，拒绝 symlink 与 repo 祖先。
   - 记录 schema（v1）：
     ```json
     {
       "schema_version": 1,
       "record_id": "uuid",
       "kind": "intent|result|observation|decision|pending|gap|health|preflight|acceptance|closeout",
       "run_id": "run_...",
       "subject": {"task_id": "...", "dispatch_id": "...", "attempt": "...", "pr": "...", "ticket": "..."},
       "action": "worker-start|send|reply|ack|stop|abandon|retain|release|merge|pr-create|tracker-close|progress|...",
       "request_id": "uuid",
       "idempotency": "joinable|replay_safe|readback_only|lookup_then_create|none",
       "state": "intent|completed|pending|absent|failed|refused|unknown|observed|divergent",
       "evidence_ref": "path-or-url",
       "observed_at": "RFC3339",
       "actor": "coordinator|worker|human|rule",
       "detail": {}
     }
     ```
   - `record_id` 幂等：重放同一记录不重复追加（读时按 record_id 去重）。
   - `state` 是记录生命周期；kind 专属字段放 `detail`（例如 health 的 `detail.health_state`、acceptance 的 `detail.levels`、preflight 的 `detail.items`）。
2. **request_id 纪律**：
   - 每个 joinable 动作前生成 UUID v4，写入 intent。
   - 调用带 `--retry-request <request_id>`。
   - 结果写入 result；丢响应按 §4.2 处理。
   - request_id 由调用方生成；若 Orca 回执返回不同 id，以回执为准并在 detail 保留两者。
3. **send/reply/ack**：
   - send 只证明 durable enqueue，不证明已读/已开工；台账记录 message id 与 request_id。
   - 不以"没回复"自动重发；重发是一次新决策，记 decision。
   - ack 回执持久化：`{delivery_id, request_id, acked_at, result}`；自动 ack（仅 heartbeat）与人工 ack 都写。
4. **收件器耐久**：
   - delivery 原子写后 fsync 父目录（对齐 progress.py 的做法）。
   - 未 ack 批次在重装时做一次全量对账：delivery 文件 vs 台账 handled；未处理的批次重新发一次唤醒（或由 coordinator 主动 check），并记 `gap`/`observation`。
   - `--status` 输出机器字段：`loop`、`phase`、`saved`、`unacked`、`last_activity_at`、`watch`；退出码区分 `resting_after_wake`/`fatal`/`not_found`（见 §5.4）。
5. **release**：
   - 采纳 Orca replay-safe 语义：结算后的 release 可重放；未结算绝不 release。
   - release 前写 `closeout`/`acceptance` 记录（至少 dispatch→task→attempt→worktree 映射与证据引用），再 release。
   - `release_pending`/`release_unknown` 按回执精确恢复；结果不明先 `worker-show`/`worker-list`，再决定是否重放。
6. **merge**：
   - 无幂等键；绝不自动重放。
   - 动作前写 intent；动作后读 PR host：`MERGED`、merge SHA、base、`git merge-base --is-ancestor <accepted_sha> <merge_sha>`；写 observation。
   - 不确定 → 升级；人类合并的单向门另记 pending（§5.6）。
7. **PR create**：
   - 先按 head 分支 lookup；存在即采纳并记 result。
   - create 响应丢失 → 再 lookup；找到即采纳；找不到才允许重试（重试仍先写 intent）。
8. **progress 事件**：
   - 保留 event_id 幂等；只作为投影级回执，不参与权威对账。
   - `receipts` 与 Orca 回执分开命名：`progress_receipt` vs `orca_receipt`，避免"回执"一词混用。
9. **对账程序（reconcile）**：
   1. 读台账：所有 `intent` 无对应 `result` 的记录。
   2. 对每条 intent：`request-show --request <request_id>`；completed → 读回执补 result；pending → 同键重放（仅 joinable）；absent → 读权威（worker-list/worker-show/task-list/PR host）判断效果。
   3. 扫描权威：worker-list（分页、`--include-remote`）、task-list、PR host、tracker；与台账 subject 左连接。
   4. 分类：`matched`、`orphan_intent`（有意图无效果）、`orphan_effect`（有效果无意图，补 late result）、`divergent`（字段冲突）、`unknown`（权威不可读）。
   5. 自动修复仅限：补 late result/observation、补 gap、按权威更新投影、标记 divergent。retry/release/merge/tracker close 一律走决策边界。
   6. 输出对账报告（台账 `closeout` 记录 + 人读摘要）。
10. **不自动重放的清单**：merge、tracker close、对未证实失败 worker 的 retry、对未结算 worker 的 release、任何 `absent` 且权威不可读的动作。

**验收**：

- 动作表的每一类（§4.2）都有幂等类/read-back/不确定处置，可机械检查（文档测试）。
- 模拟 W1/W2/W3 三个窗口，reconcile 分别产出 orphan_intent/orphan_effect/unknown。
- ack 回执、父目录 fsync、release 前置、PR lookup 各有单测。
- `absent` 不被当作"没发生"：有 intent + absent + 权威无效果 → orphan_intent；无 intent + absent → 不判定。

### 5.3 派发前检查

**问题**：12 项只有 3 项机械判据（preflight #1）；实时设置查询在 §4.0.1 之外（#2）；review 豁免要求派发前看 diff（#3）；指南新鲜度只看文件存在（#4）；CLI 活着只证明 app 应答（#5）；没有 durable preflight receipt（#16）。

**设计**：

1. **preflight receipt**：每次派发前写台账 `kind: preflight`，schema：
   ```json
   {
     "schema_version": 1,
     "record_id": "uuid",
     "kind": "preflight",
     "run_id": "run_...",
     "ticket": {"id": "...", "type": "design|impl|research|spec|split", "gate": ["G-docs"], "origin": "in-repo|tracker"},
     "checked_at": "RFC3339",
     "items": [
       {"id": "cli-alive", "class": "mechanical", "result": "pass|fail|waived", "command": "...", "observed": "...", "evidence_ref": "..."},
       {"id": "channel", "class": "judgment", "result": "pass", "note": "...", "evidence_ref": "..."}
     ],
     "blocking": false,
     "overrides": [{"item": "...", "by": "coordinator|human", "reason": "..."}],
     "post_dispatch": [{"id": "review-exemption-diff", "required": true}, {"id": "turn-start", "required": true}]
   }
   ```
2. **12 项 + 1 项**：
   - 现有 12 项保留；每项标 `class: mechanical|judgment`。
   - 新增 **live-settings**（mechanical，阻塞）：按 dispatch-preflight 的 SQLite 查询读 `defaultTuiAgent`/`disabledTuiAgents`；不可读 → 停止派活。
   - 现有 **指南在盘上** 升级为 **guide-freshness**（mechanical）：记录本轮抓取的 mtime/版本；只有"本轮抓取"才算 pass，"盘上存在"不算。
   - 修正"只有 §4.0.1 能阻止派活"的表述：把 live-settings 纳入 tick sheet，或明确它是独立 blocking preflight；二者选一，文档必须一致。
3. **review 豁免**：
   - 派发前只能写 `provisional`，证据是预计改动文件清单 + base 上的接口影响分析。
   - 派发后拿到真实 worktree 时跑 `git diff --numstat` 复核；不满足条件 → 撤销豁免，补 review 或打回。
   - 复核结果写 `post_dispatch` observation。
4. **基线数字**：记录日志来源 commit；与票 base 不同则标注"仅量级参考"或不用。
5. **CLI 活着**：拆成两个证据：app up（`orca status --json`）与命令面可用（`check`/`worker-list`/waiter slot 的最小探测）。
6. **不阻断规则**：preflight 的失败是派发阻塞；看板写入失败不是。preflight receipt 写台账，progress 只做投影。
7. **统一回执后清单**：§4.0.2 增加"看板记录已补/已记 gap"项；看板表增加"收件器已装且健康"项；两项都写台账 observation。

**验收**：

- 任一 blocking 项 fail → 拒绝派发；override 必须有 by/reason。
- review 豁免 provisional → post-dispatch 复核 → 撤销/确认两条路径。
- live-settings 不可读 → 停止派活并记 receipt。
- 看板失败不改变 preflight 结果。

### 5.4 运行健康检查

**问题**：`--status` 退出码混淆（#7）；签名字段与文档/Orca 不一致（#8）；不分页/不带 remote（#9）；WORKER_WATCH=0 盲区（#10）；心跳阈值不存在（#11）；空等待/无进展两套条件（#12）；turnStart 观测窗口（#13）；收件器健康不可证（#14）；run 健康无持久记录（#16）；无常驻健康检查（#17）；缺席当失败（#18）；阈值缺失（#19）；状态机缺失（#20）。

**设计**：

1. **状态机**：`healthy | degraded | blocked | lost | unknown`。
   - `healthy`：所有期望信号在 TTL 内新鲜；无 attention 需要动作。
   - `degraded`：非关键信号陈旧、watch 关闭、看板不可用、一次空等待；可继续，但记录原因。
   - `blocked`：等待人类/coordinator 决策（单向门、escalation、审批、头部阻塞消息）；不自动推进。
   - `lost`：正向证明 agent 已退出且未发 worker_done，或联系丢失且有正向终止证据。
   - `unknown`：证据缺席（从未观测、超过 TTL 无正向证据、liveness unverifiable）；既不健康也不失败；下一步是取证。
2. **信号与 TTL**（设计默认值，实施票可调）：

   | 信号 | 来源 | TTL/窗口 | 陈旧→状态 |
   | --- | --- | --- | --- |
   | heartbeat | worker 消息/台账 | preamble cadence × 2 + 60s grace；cadence 未知则不评估 | degraded（有正向存活证据时）/unknown |
   | inbox phase/updatedAt | state.json | waiting/checking/backoff 15min；error 立即 | degraded/blocked |
   | unacked 批次 | delivery 文件 vs 台账 | 每检查点 | blocked |
   | worker-list 快照 | Orca | 5min（有活跃 worker） | degraded |
   | worker-show 观测 | Orca | 5min | degraded |
   | turnStart | 回执/观测 | 派发后 120s 窗口；10min 未证实升级 | unknown → 升级 |
   | progress freshness | progress.json | 300s（沿用） | 只记 observability gap |
   | last_effective_progress | 台账/消息 | 3 轮无进展 | degraded |
   | reclaimable 终端 | worker-list | 每次收尾；>10min | degraded |
   | waiter slot 泄漏 | 进程/锁 | 每次重装 | degraded/blocked |
   | nested roster | worker 报告 | 长程票首次报告 | unknown/degraded |

3. **转移与授权**：

   | 转移 | 触发 | 授权 |
   | --- | --- | --- |
   | healthy→degraded | 非关键信号陈旧/watch off/看板 gap | 规则自动 |
   | degraded→healthy | 信号恢复 | 规则自动 |
   | any→blocked | 待决策项（单向门/escalation/审批/头部阻塞） | 记录 decision/pending |
   | blocked→healthy | 决策已解决 | human/coordinator |
   | any→unknown | 证据缺席/TTL 超时/unverifiable | 规则自动 |
   | unknown→lost | 正向终止证据 | 规则（证据） |
   | lost→retry | 显式 retry 决策 | coordinator（在人类边界内） |
   | any→release | 已结算 + 收尾记录 | coordinator；不确定→human |

   注：release 不是健康状态转移，而是结算后的动作；列在此处只表示它的授权条件。健康状态本身不授权 release。

4. **持久化**：台账 `kind: health` 记录 `{state, entered_at, reason, signals, evidence_refs, decided_by, next_action}`；progress 可投影 `run.status`，但健康权威在台账。
5. **`--status` 契约**：输出机器字段；退出码：`0` = resting_after_wake/healthy，`1` = fatal，`2` = not_found，`3` = degraded/blocked。文档与测试同步更新。
6. **worker 签名**：扩展为 `dispatchId:liveness:attention.requiresAction:nextAction:stage.detail`；分页读全；`--include-remote`；字段缺失记 `unknown`，不记 healthy。
7. **WORKER_WATCH=0**：允许，但 health 记 `degraded` + `reason: watch_disabled`，并要求定期 `worker-list` 轮询作为替代；`--status` 显示降级。
8. **心跳**：使用 live preamble 的 cadence；台账记 per-worker `last_heartbeat_at`；不把 5 分钟写死。preamble 无 cadence → 记 `unknown`，不伪造阈值。
9. **turnStart**：`observed` 即证据；`unsupported/unobserved/缺失` → `unknown`，按 agent 支持面取证（终端标题/屏幕、worker-read、状态投影、产物）；窗口内不判失败；超时升级给人。
10. **停滞检测**：`last_effective_progress`（非心跳消息、artifact 变化、commit、stage 变化）；连续 3 轮无进展 → degraded；连续 3 次空等待 → 枚举（沿用现有规则）；收件器模式下空等待不可见，改由 `last_effective_progress` + unacked 判断。
11. **健康检查挂载**：收件器循环每个 wake 写一条 `observation`（轻量）；coordinator 每次醒来跑一次 `health evaluate`；无常驻检查器也能在恢复时重建。
12. **重装全量对账**：收件器重装时读一次 worker-list/state.json，更新基线并写 observation；避免安装间隙变化被吸收。
13. **绑定丢失**：`consumer_fenced`/Run 绑定丢失 → 停止、保存证据、按 §5.6 协议；不得冒充其他句柄。

**验收**：

- 状态机转移表逐行有测试（含 unknown→lost、lost→retry 的人类边界）。
- `--status` 退出码/字段测试。
- 签名分页/remote/字段缺失测试。
- WORKER_WATCH=0 降级测试。
- turnStart 窗口与升级测试。
- 重装全量对账测试。

### 5.5 验收证据汇总

**问题**：票型证据矩阵分散（#1）；accepted/merge SHA 无权威存储（#2）；验收结论只在运行末报告（#3）；release 销毁证据（#4）；缺收尾清单（#5）；单向门无 pending（#6）；progress 无法表达 SHA（#7）；长程只抽查（#8）；证据强度不齐（#9）；远端 tracker Answer 位置未定义（#10）；四级模型无法表达跨 PR/永不合并/revert（#11）；缺 eval（#12）。

**设计**：

1. **验收记录（acceptance record）**：台账 `kind: acceptance`，按票/PR：
   ```json
   {
     "ticket": "...", "gate": ["G-impl"], "attempt": "...",
     "accepted_sha": "...", "accepted_at": "...",
     "pr": {"number": 12, "head_at_acceptance": "...", "base": "main"},
     "checks": [{"name": "tests", "command": "...", "result": "pass", "evidence_ref": "..."}],
     "review": {"axis": ["standards", "spec"], "result": "pass", "evidence_ref": "..."},
     "levels": {
       "implemented": {"value": true, "evidence_ref": "...", "at": "..."},
       "integrated": {"value": true, "evidence_ref": "...", "at": "..."},
       "merged": {"value": true, "merge_sha": "...", "evidence_ref": "...", "at": "..."},
       "tracker_closed": {"value": true, "evidence_ref": "...", "at": "..."}
     },
     "decided_by": "coordinator|human", "decided_at": "..."
   }
   ```
2. **最低证据**：
   - merge：PR host `MERGED` + merge SHA + `git merge-base --is-ancestor accepted_sha merge_sha`。
   - tracker close：tracker 读回字段（状态 + 时间）。
   - start：`turnStart: observed`，或该 attempt 的产物/状态投影证据。
   - 测试：命令 + 结果 + 日志引用；grep 只证明字符串存在，不证明执行。
   - review：双轴结果 + 引用；豁免需 spec 与 Answer 同时写理由。
   - worker_done：只记 settlement，不记 acceptance。
3. **四级模型扩展**：
   - 每级按 PR 记录，支持一票多 PR（部分合并）。
   - 永不合并：`merged: {value: false, terminal: "never_merge", decided_by: human}`。
   - revert/reopen：追加新 observation，`merged`/`tracker_closed` 置 false 并保留历史（不覆盖）。
4. **落点**：台账是索引；Answer/PR body 是人读摘要并引用 evidence_ref；progress 只做投影（delivery 布尔必须绑定 evidence_ref 才允许 true）。不新增独立 manifest 文件，除非 C2 决策为 B。
5. **长程验收**：每票至少一条 acceptance 记录；抽查改为"每票记录 + 按风险抽样复核"，记录抽样规则与未复核项。
6. **远端 tracker 票**：Answer 位置由票正本约定；台账记录 `answer_location`；换 attempt 后仍可读。
7. **G-docs 证据**：设计文档含 grill 轮次与落点；术语表；frontier 为空；模板章节一致。worker Answer 是自报，coordinator 读文档核验。

**验收**：

- acceptance schema 校验；缺 evidence_ref 的 true 拒绝。
- revert/reopen/never-merge/多 PR 四条路径测试。
- 无证据的 merged/tracker_closed 不能写 true。
- 收尾报告从台账生成，不靠散文。

### 5.6 恢复与收尾对账

**问题**：accepted/merge SHA 不可重推（#2）；验收结论崩溃即丢（#3）；release 销毁证据（#4）；缺清单（#5）；单向门无 pending（#6）；恢复不读快照（dispatch-dashboard #7）；没有 binding-loss 协议（P2）。

**设计**：

1. **收尾对账清单**（每行：权威来源、可重复性、动作）：

   | # | 事实 | 权威 | 可重复 | 动作 |
   | --- | --- | --- | --- | --- |
   | 1 | accepted SHA per PR | 台账 acceptance | 否 | 必须在验收时持久化 |
   | 2 | PR MERGED/base/merge SHA | PR host API | 是 | 读回写 observation |
   | 3 | merge 覆盖 accepted head | git merge-base --is-ancestor | 是 | 失败→divergent/升级 |
   | 4 | main 含 merge | git rev-parse/merge-base | 是 | 记录 |
   | 5 | 本地 main 同步 | git status/rev-parse/upstream + ff-only | 是 | 阻塞则暂停后续合并 |
   | 6 | 每票已集成 | integration branch ancestry / PR Closes | 是 | 逐票记录 |
   | 7 | tracker 状态 | tracker 读回 | 是 | 关闭/重开记录 |
   | 8 | worker 生命周期 | worker-show/worker-list/release 回执 | 是 | 清点 reclaimable |
   | 9 | 看板投影 | progress.json | 是（非权威） | 只记 gap，不作证据 |
   | 10 | 单向门待办 | 台账 pending | 是 | 到期检查 |

2. **单向门 pending item**：
   ```json
   {"kind": "pending", "pending_type": "human_merge", "pr": 12, "accepted_sha": "...",
    "owner": "human", "condition": "PR merged", "check_after": "RFC3339",
    "status": "pending", "recorded_at": "..."}
   ```
   - 人类合并后，coordinator 下次 turn 读 PR host 记录 merge SHA、main 同步、tracker 关闭。
   - 若无人启动 turn，pending 在台账与看板上可见（"awaiting human merge"），不会消失。
3. **release 前置**：release 前确认已结算，且收尾记录已持久化——成功路径至少含 acceptance 与 dispatch→task→attempt→worktree 映射，失败路径至少含失败分类与证据引用；release 结果不明先查 worker-show。未结算绝不 release。
4. **崩溃恢复（reconcile）**：§5.2 的对账程序 + §5.6 清单；恢复顺序 Orca → 台账 → 快照；快照只作线索，不作权威。
5. **binding-loss**：`consumer_fenced`/绑定丢失 → 停止、保存证据、核对 Run/Dispatch 归属；不新建 Run、不冒充句柄；恢复动作按证据门槛，必要时升级。
6. **§6.5.1 同步**：本地 main 同步阻塞时暂停后续合并（现有规则），台账记 `blocked`；不静默继续。
7. **tracker 关闭**：按仓库/tracker 约定；`Closes` 自动关闭则读回验证；未自动关闭时由 coordinator 在授权内关闭，否则升级人类；不把 coordinator 变成 tracker 写者。

**验收**：

- 崩溃在 merge 与 tracker 对账之间 → 恢复后清单能补齐 2/3/4/7。
- 单向门 pending 在无 turn 时仍可见；有 turn 时自动对账。
- release 前无收尾记录（成功路径 acceptance，失败路径失败分类）→ 拒绝 release 或先补记录。
- binding-loss 恢复测试。

### 5.7 单例进度看板

**需求**：多个 coordinator 共用一个本地进度看板服务；若已有服务则向它登记；否则启动单例。考虑并发启动、发现与存活检查、Run 隔离、旧服务兼容、故障恢复、安全边界。

**设计**：

1. **布局**（仓库外，按用户）：
   ```
   $ORCA_PROGRESS_HOME（缺省 ~/.orca/progress，0700）
   ├── server.json          # {pid, port, protocol_version, started_at, binary_version}
   ├── startup.lock         # flock，仅启动期持有
   └── runs/
       ├── run_xxx.json     # {run_id, state_path, registered_at, label}
       └── run_yyy.json
   ```
2. **启动算法**：
   1. 建目录 0700；`flock(startup.lock, timeout=10s)`。
   2. 读 `server.json`；若存在，探测 `/health`（500ms 超时）。
      - 健康且 `protocol_version` 兼容 → 登记本 Run descriptor，打开 `/?run=<id>`，释放锁。
      - 健康但不兼容 → 不杀旧服务；报告可操作错误；本 Run 回退到 `serve --state`（degraded），台账记 gap。
      - 连接被拒（确定已死）→ 删除 `server.json`，进入启动。
      - 超时/5xx（可能卡死但仍活着）→ **不杀、不删**；报告 degraded，回退 `serve --state`，台账记 gap；提示用户检查旧服务。
   3. 启动 `serve --port 0 --registry <dir>`；等待 `/health` ready（5s 超时）。
   4. 原子写 `server.json`（temp + fsync + rename）；登记 descriptor；打开浏览器；释放锁。
3. **并发**：锁串行化启动；后到者读到健康服务即登记；若赢家尚未写 `server.json`，后到者等锁或重试探测；不会起第二个服务。
4. **发现/存活**：`GET /health` → `{ok, protocol_version, pid, started_at, runs}`；`GET /?run=<id>` 渲染页面，`GET /snapshot?run=<id>` 返回该 Run 的只读快照；页面列出 registry 中的 Run；descriptor 的 state_path 不可读 → 显示 `unavailable`；不自动删除。
5. **Run 隔离**：服务只读每个 Run 的 progress.json；复用 progress.py 的 `state_path` 校验（拒绝 symlink/repo 祖先）；HTTP 响应只暴露 Run ID，不暴露文件路径；无写 API；跨 Run 只做列表。
6. **旧服务兼容**：
   - `protocol_version` 写入 `server.json` 与 `/health`；客户端要求最低版本。
   - 无 `/health` 的旧服务视为 legacy：不登记、不杀；本 Run 回退 `serve --state` 并记 gap；提示用户停止旧服务。
   - `serve --state` 保留给 DEMO/测试/单 Run。
7. **故障恢复**：服务崩溃 → 下次 `open` 探测 stale、清理、重启、重登记；progress.json 是持久源，服务无状态；崩溃期间看板不可用只记 gap，不影响任何 Run。
8. **安全**：仅绑定 127.0.0.1；GET only；CSP；无外部资源；`Cache-Control: no-store`；校验 Host 头为 localhost/127.0.0.1（防 DNS rebinding）；只读已注册的 state_path，不提供任意路径读取；目录 0700；descriptor 不含凭证；文档明确单用户信任模型，不暴露端口/不做多用户认证。
9. **页面**：Run 列表 + 选中 Run 的 tasks/workers/deliveries/events；freshness 显示；健康投影（若台账有 health）显示；"warning ≠ 启动失败"提示。
10. **迁移**：现有每 Run `open` 改为单例感知；测试从"每 Run 独立端口"改为"单例 + 两 Run 隔离"，保留 `serve` 测试；DEMO 不变。

**验收**：

- 两 Run 同时 open → 一个服务、两个 descriptor、页面可切换、快照隔离。
- 并发启动 → 锁串行，只有一个服务。
- 服务被杀 → 下次 open 重启并重登记。
- 旧版本/无 `/health` → 回退 + gap，不杀旧服务。
- 看板不可用 → 派发继续。
- HTTP 只读/localhost/CSP/无路径泄漏/503 损坏快照。

### 5.8 变更面与接缝

**变更面**（后续实施票，不在本票改）：

- 文档：`skills/orchestra-auto{,-zh}/SKILL.md`、`references/*`（inbox-and-waiting、dispatch-preflight、gate-acceptance、failed-dispatch-recovery、failure-and-reporting、progress-dashboard、long-horizon-tickets）。
- 脚本：`scripts/inbox-loop.py`（fsync、ack 回执、status 字段/退出码、签名分页/remote）、`scripts/progress.py`（单例 registry、open 退出码、health 投影、bookkeeping 写入）。
- 新增：`scripts/ledger.py`（append/reconcile）、`scripts/preflight.py` 或文档内 procedure、`scripts/health.py`。
- 测试：`tests/test_inbox_loop.py`、`tests/test_progress_dashboard.py`、`tests/test_package_content.py`、新增 ledger/health/preflight 测试。
- eval：收尾对账、单例看板、看板不阻断、revert/reopen、binding-loss。

**接缝（deep module）**：

- `ledger.append(record) -> record_id`：唯一写入口；幂等、fsync、schema 校验。
- `ledger.reconcile(authority) -> report`：读权威、分类、自动修复边界。
- `preflight.check(ticket, context) -> receipt`：机械项执行、判断项记录、blocking 判定。
- `health.evaluate(signals) -> state`：纯函数状态机，证据表驱动。
- `registry.register(run) / discover() / probe()`：单例看板的深模块；HTTP 层只做只读渲染。
- `closeout.plan(run) -> checklist` / `closeout.reconcile()`：逐行权威与可重复性。

**接口原则**：调用方只依赖 receipt/report 的稳定字段；脚本内部可替换。看板与台账不互相调用；两者都只读权威。

## 6. 决策记录（grill rounds）

### 6.1 设计树与 frontier

设计树按「产物治理 → 架构 → 回执/证据 → 健康 → 收尾」分层。第 1 轮问不依赖任何未决答案的 frontier（A1-A7、B1-B2、C1-C5、D1-D3）；第 2 轮原计划问依赖第 1 轮选择的 frontier（台账位置/写入者、registry 范围、旧服务、TTL 默认值、验收记录、release 前置）。coordinator 在答复中明确授权这些属于具体实现与文档组织，按现有边界处理；故第 2 轮不再上报，采用值见 §6.3。frontier 为空，无静默假设。

```text
交付物
├── A1 位置/模板 → A4 Answer 位置 → A7 提交规则
├── A2 语言
├── A3 范围 → B1 单例架构 → B2 可见性 + Q3 registry 范围 + Q4 旧服务
├── A5 术语/ADR
└── A6 实施大纲
回执/证据
├── C1 台账 → Q1 位置/保留 + Q2 写入者
├── C2 验收汇总 → Q6 tracker 关闭
├── C3 release → Q7 前置
├── C4 PR/merge
└── C5 收件器耐久
健康
├── D1 落点 → Q5 TTL
├── D2 心跳
└── D3 WORKER_WATCH
```

### 6.2 第 1 轮

coordinator 回答（message id 记录在仓库外台账，2026-10-29T09:29Z）：

- **A1 A**：`docs/design/orchestration-stability.md`，本文件结构；仓库无 effort 模板的偏差在 §9.1 报告。
- **A2 A**：中文设计文档；后续实施票同步两个语言包。
- **A3 C**：流程 + 脚本行为/接口 + 单例看板独立章节。
- **A4 C**：Answer 只在仓库外台账；工作树只放本设计文档。→ 本文件不设 In-repo Answer 节。
- **A5 A**：不新建 CONTEXT.md/ADR；术语在 §10.1，偏差在 §9.1 报告。
- **A6 A**：含 change surface、seam、迁移步骤与后续实施票大纲。
- **A7 B**：PR 号/CI 只进 GitHub 与仓库外台账；仓库内不追加提交。→ 本文件不含 PR 事实；收尾不追写。
- **B1 A**：每用户一个长驻 server；仓库外 registry + 每 Run 描述文件 + 文件锁 + `/health`；`serve --state` 保留给 DEMO/单 Run（§5.7）。
- **B2 A**：页面列出并切换所有已注册 Run；localhost 单用户信任模型（§5.7）。
- **C1 B**：仓库外 append-only JSONL 台账，与 progress.json 分离，coordinator 写（§5.2）。
- **C2 A**：沿用现有证据位置 + 统一核对清单，不新增独立 manifest（§5.5）。
- **C3 A**：采纳 Orca replay-safe release 语义，前提是核对当前 Orca 契约（§5.2、§5.6）。
- **C4 A**：PR 创建 lookup-before-create；merge 绝不自动重放，只读 PR host 证据（§5.2）。
- **C5 A**：delivery 原子写后 fsync 父目录；自动/人工 ack 都持久化 ack 回执（§5.2）。
- **D1 A**：健康权威观测在台账，progress 只做投影（§5.4）。
- **D2 A**：心跳用 live preamble 的 cadence + grace，不写死 5 分钟（§5.4）。
- **D3 A**：允许 `WORKER_WATCH=0`，但记 degraded 并给替代轮询；不得因此宣称 healthy（§5.4）。

### 6.3 第 2 轮（依赖第 1 轮，coordinator 授权按现有边界处理）

coordinator 明确：以上答复之外的剩余项属于具体实现与文档组织；若出现影响用户可见行为或实质范围的方案分叉，才需精简后上报；不得自行建立不必要的治理约定。据此，第 2 轮不再上报，直接采用下列**在现有授权与现有规则内**的默认值，并记录落点：

| 项 | 采用值 | 依据 | 落点 |
| --- | --- | --- | --- |
| 台账位置/保留 | `$ORCA_LEDGER_HOME`，缺省 `~/.orca/ledger`；按 Run 分文件；30 天或 10MB 后归档 | C1=B；仓库外运维索引，不新增仓库约定 | §5.2 |
| 台账写入者 | 仅 coordinator；worker 只写 progress（source=worker） | 现有角色边界（§2 角色分工） | §5.1、§5.2 |
| registry 范围 | 每用户（`$ORCA_PROGRESS_HOME`），本机共享，不跨用户/机器 | B1=A；localhost 单用户信任 | §5.7 |
| 旧服务处理 | 不杀；不兼容/卡死回退 `serve --state` 并记 gap；仅连接被拒才重启 | B1=A；"不破坏未证实状态"原则 | §5.7 |
| 健康 TTL | §5.4 表默认值（heartbeat 2×cadence+60s、worker-list 5min、turnStart 120s/10min、progress 300s），可配置覆盖 | D1=A；数值是设计默认，不是新治理约定 | §5.4 |
| 验收记录/tracker 关闭 | 台账 acceptance record；tracker 关闭沿用现有授权并读回，例外升级人类 | C2=A；不改变人类/coordinator 边界 | §5.5、§5.6 |
| release 前置 | 结算 + 收尾记录（成功 acceptance，失败分类）；`release_unknown` 先 worker-show；未结算不重放 | C3=A；核对当前 Orca 契约（release 重放安全） | §5.2、§5.6 |

**Frontier 状态**：第 1 轮全部落定；第 2 轮由 coordinator 明确授权在现有边界内处理，无静默假设、无待上报的用户可见行为/范围分叉。设计树 frontier 为空。

## 7. 迁移步骤

1. **文档与 schema**：落地本设计的术语、schema、状态机、清单；补两语言语义 parity 的 prose contract。
2. **台账核心**：`ledger.py` append/reconcile；request_id 纪律写进 dispatch/send/reply/ack/release 流程。
3. **收件器耐久**：父目录 fsync、ack 回执、`--status` 字段/退出码、重装全量对账、签名分页/remote。
4. **预检回执**：preflight schema、live-settings/guide-freshness 入表、review 豁免 post-dispatch 复核。
5. **健康状态机**：health record、信号 TTL、转移表、停滞检测、绑定丢失协议。
6. **验收/收尾**：acceptance record、收尾清单、单向门 pending、release 前置。
7. **单例看板**：registry/lock/health/兼容/隔离/迁移；测试从每 Run 端口改为单例。
8. **eval 与 parity**：新增 eval；双语语义检查；计数单一来源。

每步独立可验收；前一步不依赖后一步。破坏性变更（如 `--status` 退出码）先加机器字段，再改退出码，保留一个版本的兼容层。

## 8. 后续实施票大纲

| 票 | 范围 | 验收 | 依赖 | 门类 |
| --- | --- | --- | --- | --- |
| T1 台账核心 | ledger.py、request_id 纪律、reconcile 分类 | 三窗口测试、幂等 append、schema 校验 | 无 | 实施 |
| T2 收件器耐久 | fsync、ack 回执、status 字段/退出码、重装对账 | 掉电/重放/未 ack/退出码测试 | T1 | 实施 |
| T3 预检回执 | preflight schema、12+1 项、review 复核 | blocking/override/不可读设置测试 | T1 | 实施 |
| T4 健康状态机 | health.py、信号 TTL、转移、停滞、绑定丢失 | 状态机全转移、turnStart 窗口、watch off | T1 | 实施 |
| T5 验收/收尾 | acceptance record、清单、pending、release 前置 | revert/reopen/多 PR/崩溃恢复 | T1 | 实施 |
| T6 单例看板 | registry/lock/health/兼容/隔离 | 并发/重启/旧服务/隔离/安全 | T1 | 实施 |
| T7 文档与 eval | 两语言、prose contract、eval | parity、eval 通过 | T1-T6 | 文档 |

单向门判断：T2 的 `--status` 退出码变更、T6 的单例服务迁移属于行为变更，按 G-merge 门规则判断是否需人类合并；本设计不预设。

## 9. 验证与证据

### 9.1 本票（设计票）

本票不跑：源码测试、双轴源码 review、实现代码。

- 文档结构：按选定模板；章节完整。
- 术语：与仓库现有词汇一致；新增术语在本文件定义。
- 追溯矩阵：§3 的每条 F/C/P 映射到设计章节；§9.2 列出领域矩阵；§10.2 列出调查来源。
- grill 轮次：§6 记录 Q→A→落点；frontier 为空。
- 双语偏差：本票只写中文设计；后续实施票同步两包（若 A2=A）。
- 偏差报告：仓库无 effort 模板/CONTEXT.md/ADR；G-docs 机械项无法满足，按 A5 决策报告。
  - 模板：仓库无 effort 模板；本文件用第 1 轮选定的结构，章节映射见 §6/§9。
  - 术语：仓库无 CONTEXT.md/GLOSSARY.md；G-docs 文本说 CONTEXT.md，README 引用 glossary，两者都不存在。术语在 §10.1 定义，未初始化新约定（按 A5）。
  - ADR：未建。理由：仓库无 `docs/adr/` 约定；本设计的核心决策（权威/投影/台账边界）虽重要，但可在后续实施票中调整，且没有"难以逆转 + 反直觉 + 真实取舍"三者同时成立到必须现在写 ADR 的程度。若 A5 选择 C，则补 `docs/adr/0001-*.md`。

### 9.2 追溯矩阵（领域 → 发现 → 设计）

| 领域 | 调查发现 | 设计章节 |
| --- | --- | --- |
| 派发与看板记录耦合 | dispatch-dashboard #1-#18（快照无 Dispatch、无 write-ahead intent、进度写失败级联、task 命名空间、成功回执不持久、恢复不读快照、事件非 payload、unobserved/unknown、attempt 映射、run 绑定、worker 自报、洗新、台账弱于投影、清单互斥） | §5.1、§5.2、§5.5 |
| 持久回执及对账 | receipts #1-#16（无幂等键/read-back、delivery ID vs request ID、worker-start 丢响应、12 类记录、恢复规则不统一、三窗口、send/reply 证据、PR/merge、release 分裂、ack 回执、父目录 fsync、progress 幂等错配、worker_done 非验收、台账无 schema、收件器记录、双语 parity） | §4、§5.2、§5.8 |
| 派发前检查 | preflight #1-#5、#14-#16（3/12 机械判据、实时设置在表外、review 豁免无 diff、指南新鲜度、CLI 活着、无 receipt、收件器健康、清单互斥） | §5.3 |
| 运行健康检查 | preflight #6-#13、#15、#17-#21（信号 TTL、--status、签名、分页/remote、WORKER_WATCH、心跳、空等待/无进展、turnStart、无持久记录、无常驻检查、缺席当失败、状态机） | §5.4 |
| 验收证据汇总 | acceptance #1-#12（矩阵分散、SHA 无存储、结论易丢、release 毁证、清单、pending、progress schema、长程抽查、证据强度、tracker 位置、四级模型、eval） | §5.5、§5.6 |
| 恢复收尾对账 | acceptance #2-#6、dispatch-dashboard #7、failure-corpus #22（不可重推事实、崩溃、release、清单、pending、恢复顺序、binding-loss） | §5.6 |
| 单例进度看板 | follow-up 需求 + failure-corpus #15、dispatch-dashboard #17（open 退出码、看板不阻断） | §5.7 |
| 真实故障语料 | failure-corpus #1-#24（F1-F17、C1-C22、P1-P5） | §3、各节 |

### 9.3 后续实施票的验证

- 单测：ledger append/幂等/reconcile 分类；inbox fsync/ack/status；progress registry/单例/隔离；preflight schema；health 状态机；acceptance/revert。
- 集成/eval：崩溃三窗口、未 ack 重装、看板不阻断、单例并发、旧服务回退、单向门 pending、binding-loss。
- prose contract：两语言语义 parity；关键否定/顺序条款。

## 10. 附录

### 10.1 术语

| 术语 | 定义 |
| --- | --- |
| 协调台账（ledger） | 仓库外 append-only JSONL，记录 intent/result/observation/decision/pending/gap/health/preflight/acceptance/closeout；非权威索引。 |
| intent | 动作前的意图记录，含 request_id 与 expected_effect。 |
| result | 动作后的回执记录，含 state 与 evidence_ref。 |
| orphan_intent | 有 intent 无效果；可能从未执行。 |
| orphan_effect | 有效果无 intent；补 late result。 |
| divergent | 台账与权威字段冲突。 |
| 可观测性缺口（gap） | 看板/记录失败，不影响派发。 |
| 单向门 pending | 等待人类合并/决定的持久待办。 |
| bookkeeping 写入 | 对账/补登写入，不刷新 last_observed。 |

### 10.2 调查来源

- 五路只读调查（91 条发现）：receipts（16）、acceptance/closeout（12）、preflight/health（21）、dispatch-dashboard（18）、failure-corpus（24）。
- 主要文件：`skills/orchestra-auto-zh/SKILL.md`、`references/{inbox-and-waiting,dispatch-preflight,gate-acceptance,failed-dispatch-recovery,failure-and-reporting,progress-dashboard,long-horizon-tickets,spec-contract}.md`、`scripts/{inbox-loop,progress}.py`、`tests/*`、`CONTRIBUTING.md`、`launch-kit/*`、`evals/evals.json`。
- Orca 指南：`orca skills get orchestration` 的 kernel 与 references（本轮读取的本地副本；权威以当前 CLI 为准）。

### 10.3 不做的事

- 不实现代码；不建票；不改自动合并授权；不引入多用户认证；不做远程看板；不把 progress 升级为权威。
- 不重写 Orca 生命周期；不改变 worker_done 语义；不新增调度器。
- 不解决所有双语漂移；只在本设计范围列后续票。

### 10.4 命令模板（设计级，后续实施票细化）

```bash
# 1) 派发：intent → mutation → result → 回执后检查
orca orchestration worker-start --task <task_id> --retry-request <request_id> \
  --worktree new-top-level --name <slug> --agent <agent> --json
orca orchestration request-show --request <request_id> --json
orca orchestration worker-list --run <run_id> --include-remote --json
orca orchestration worker-show --dispatch <dispatch_id> --json

# 2) 收件器：检查、ack、状态
orca orchestration check --terminal <handle> --json
orca orchestration check --terminal <handle> --ack <delivery_id> --json
orca orchestration check --terminal <handle> --all --json

# 3) 单例看板：open 注册/启动，serve 保留给 DEMO/单 Run
python3 skills/orchestra-auto-zh/scripts/progress.py open --state <progress.json> --run <run_id>
python3 skills/orchestra-auto-zh/scripts/progress.py serve --port 0 --registry <dir>
curl -s http://127.0.0.1:<port>/health

# 4) 收尾对账：PR host / git / tracker 读回
git merge-base --is-ancestor <accepted_sha> <merge_sha>
git rev-parse HEAD
git status --porcelain=v2 --branch
```

### 10.5 风险

| 风险 | 缓解 |
| --- | --- |
| 台账在仓库外、不随仓库版本化 | 台账是运维索引；权威仍可重建大部分事实；accepted SHA 必须验收时写入 |
| 本地时钟/时区 | 统一 RFC3339 UTC；对账以事件顺序为主 |
| 单例服务成为单点 | 无状态、可重启；看板故障只记 gap；每 Run 的 progress.json 独立 |
| 旧服务不兼容 | 不杀旧服务；回退 `serve --state`；提示用户 |
| 过度工程 | 台账最小 schema；看板只读；健康状态机纯函数；分阶段实施 |
| 文档语义漂移 | 后续票加 prose contract；本设计不一次性解决 |
