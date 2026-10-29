# 协调台账（coordination ledger）

**什么时候读**：每个有副作用的动作之前写 intent、之后写 result 时；一个 Run 首次 append
之前、对账崩溃窗口时，或 coordinator 要报告未决 intent 与待处理项时。

台账是持久的 append-only **索引**，不是权威。发生了什么由 Orca、git 与 tracker 决定，台账
指向它们。会闸住副作用的记录——动作前的 intent、preflight 回执、acceptance 或 closeout
记录——必须在动作执行前落盘。append 失败会非零退出且绝不报告记录已写入，所以不要在失败的
写入上认定动作已获授权或门禁已通过。

## 目录

- [位置与权限](#位置与权限)
- [Schema v1](#schema-v1)
- [CLI](#cli)
- [持久性与并发](#持久性与并发)
- [保留策略](#保留策略)
- [记录失败语义](#记录失败语义)
- [崩溃窗口与分类](#崩溃窗口与分类)
- [固定派发顺序](#固定派发顺序)
- [动作矩阵](#动作矩阵)
- [两条硬规则](#两条硬规则)
- [进度投影](#进度投影)
- [权威快照](#权威快照)
- [对账报告](#对账报告)
- [对账并发](#对账并发)
- [模块缝](#模块缝)

## 位置与权限

- `$ORCA_LEDGER_HOME` 指定台账根目录，缺省为调用者主目录下的 `~/.orca/ledger`，绝不在项目内。
- 每个 Run 一个文件：`runs/<run_id>/ledger.jsonl`，旁边是 `archive/`。
- 根目录、`runs/`、每个 Run 目录与 `archive/` 都是 `0700`；文件是 `0600`。
- 台账不含凭证、token 或原始终端输出。`detail` 只放简短的结构化事实与引用；`evidence_ref`
  指向证据，而不是复制证据。
- 路径中任何一段是 symlink、根目录位于任何仓库内、或是目标仓库的祖先时都会被拒绝。CLI 从
  当前工作目录推断目标仓库；模块调用方显式传入。
- 保留策略用到的环境变量见下文。它们只改变记录何时归档，不改变 append 的含义。

## Schema v1

记录恰好包含以下键；出现未知字段即非法，`detail` 是扩展点。

| 字段 | 含义 |
| --- | --- |
| `schema_version` | `1` |
| `record_id` | UUID；幂等键 |
| `kind` | `intent`、`result`、`observation`、`decision`、`pending`、`gap`、`health`、`preflight`、`acceptance`、`closeout` |
| `run_id` | 可移植的 Run 标识；必须等于 `append --run-id` |
| `subject` | 只允许 `task_id`、`dispatch_id`、`attempt`、`pr`、`ticket`；`attempt` 是正整数，`pr` 是整数或短字符串 |
| `action` | `worker-start`、`send`、`reply`、`ack`、`stop`、`abandon`、`retain`、`release`、`merge`、`pr-create`、`tracker-close`、`progress`、`health`、`preflight`、`acceptance`、`closeout` |
| `request_id` | 有键动作的 UUID；仅 `readback_only` 与 `none` 可为空 |
| `idempotency` | `joinable`、`replay_safe`、`readback_only`、`lookup_then_create`、`none` |
| `state` | `intent`、`completed`、`pending`、`absent`、`failed`、`refused`、`unknown`、`observed`、`divergent` |
| `evidence_ref` | 路径或 URL；intent 可以为空 |
| `observed_at` | RFC3339 UTC（`Z` 或 `+00:00`） |
| `actor` | `coordinator`、`worker`、`human`、`rule` |
| `detail` | 对象；kind 专属字段，如 `detail.health_state`、`detail.levels`、`detail.items`、`detail.expected_effect`、`detail.attempt_source` 或 `detail.raw_receipt` |

单条记录上限为 64 KiB；更大的记录非法且不会被写入。

本 schema 是编排稳定性 spec 定义的**本地契约**，不是对任何实时 Orca 回执形状的断言。未经验证
的回执字段放在 `detail.raw_receipt`，绝不当作稳定契约断言。

## CLI

CLI 是产品面，输入输出都是 JSON。

```bash
LEDGER="$SKILLS_ROOT/orchestra-auto-zh/scripts/ledger.py"
RUN=run_example

# 动作之前写 intent，动作之后写 result。
python3 "$LEDGER" append --run-id "$RUN" --record "$(cat intent.json)"
python3 "$LEDGER" append --run-id "$RUN" --record - < result.json

# 读取完整记录（最新在最后），可加过滤。
python3 "$LEDGER" show --run-id "$RUN"
python3 "$LEDGER" show --run-id "$RUN" --kind intent --state pending
python3 "$LEDGER" show --run-id "$RUN" --action send --subject task_id=task-1 --subject ticket=31

# 按 kind/state 计数，并报告未决 intent 与待处理项。
python3 "$LEDGER" summary --run-id "$RUN"
```

- `append` 输出 `{"schema_version": 1, "run_id": ..., "record_id": ..., "appended": ...}`。
  完全相同的重放 `appended` 为 `false`，文件不被改动。
- `show` 输出记录 JSON 数组，按追加顺序（最新在最后）。它读取活动文件；归档记录仍以纯
  JSONL 保存在 `archive/` 下可直接阅读。
- `summary` 输出包含 `records`、`kinds`、`states`、`open_intents`、`pending_items` 与
  `corrupt_lines` 的对象。未决 intent 是没有同 `request_id`（非空）`result` 的 `intent`
  记录；待处理项是 kind 或 state 为 `pending` 的记录。
- 退出码 `0` 成功，`1` 输入 / schema / I/O 失败，`2` 用法错误。损坏行只是 stderr 警告，
  不是读取失败：完整记录仍会返回。

## 持久性与并发

- append 在整个读-改-写过程中对 `runs/<run_id>/ledger.lock` 持有排他 `flock`。真正防止长行
  与另一进程交错的是这把锁，而不是单独的 `O_APPEND`。
- 每条记录写成一行，写后 fsync 文件；首次创建 `ledger.jsonl` 时还 fsync 父目录，使文件名
  本身持久。
- 中断的 append 会留下没有换行的尾随片段。读取方丢弃它并保留所有完整记录；下一次 append
  先补一个换行终止该片段，新记录绝不会与它粘连。
- 不构成合法 schema-v1 记录的完整行会被**保留并报告**，读取绝不静默跳过；报告会给出物理行号。
  归档重写会把这类行与尾随片段移入 `archive/corrupt-*.jsonl`，而不是删除。
- 幂等按规范化后的 `record_id` 判定：UUID 大小写与 JSON 键顺序不影响，其他值都影响。完全
  相同是 no-op；同一 id 携带不同载荷是一致性错误，什么都不写。归档中的 id 也会被检查，所以
  对已归档记录的迟到重放同样是 no-op。

## 保留策略

缺省为每 Run 30 天或 10 MiB，以先到者为准；两者是可配置的缺省值，不是产品要求。
`ORCA_LEDGER_RETENTION_DAYS` 与 `ORCA_LEDGER_RETENTION_BYTES` 可在该 Run 的环境里覆盖。

达到任一限制时，完整记录按最旧优先移入同 Run 的
`archive/ledger-<timestamp>-<n>.jsonl`，活动文件被原子重写。归档文件先 fsync、其目录也
fsync，之后才替换活动文件，因此绝不会删除未归档的记录。重写时遇到的损坏行或半行片段会保存在
`archive/corrupt-<timestamp>-<n>.jsonl`。

## 记录失败语义

| 记录 | 动作前失败 | 动作后失败 | 对编排的影响 |
| --- | --- | --- | --- |
| 台账 intent | 不执行动作；报告并停止或升级 | 不适用 | 阻止动作 |
| 台账 result | 不适用 | 重试；在证据目录保留原始回执；记录 gap；绝不重放动作 | 不阻塞；稍后对账 |
| 台账 acceptance / closeout | 不 release，不宣称门禁通过 | 重试；release 等待记录持久 | 阻止 release 与门禁关闭 |
| progress 写入 | 不适用 | 记录 gap；下一个检查点用同一 `event_id` 重试 | 不阻塞 |
| ack 回执 | 不适用 | 不假设 ack 已落地；用同一 `request_id` 重试 ack；保留 delivery 文件 | 不阻塞；状态显示未 ack |
| health 记录 | 不适用 | 保留内存中的评估并重试；看板投影不是权威 | 不阻塞 |
| preflight 回执 | 不派发 | 不适用 | 阻止派发 |
| registry 描述符 / `server.json` | 回退到 `serve --state`；记录 gap | 看板不可用；记录 gap | 不阻塞 |
| 权威读取（Orca、PR host、tracker、git） | 不推断效果 | 重试读取；仍不可读则记 `unknown`；破坏性动作依赖它时升级 | 破坏性动作依赖时升级 |

规则是：**闸住副作用的记录会阻止副作用；描述结果的记录永远不阻塞编排，稍后修复。** 不要从
失败命令“没有报错”推断持久性，也不要在记录尚未持久时宣称门禁通过。

## 崩溃窗口与分类

intent 记录在动作执行前落盘。崩溃会把 Run 留在三个窗口之一，每次分类都会带上它：

| 窗口 | 崩溃停在哪里 | 分类 |
| --- | --- | --- |
| W1 | 动作之前：intent 已落盘，无回执，权威侧无效果 | `orphan_intent` |
| W2 | 动作之后：存在回执或可见的权威效果 | `matched`、`divergent`、`orphan_effect` |
| W3 | 无法判定：回执或某个权威源读不到 | `unknown` |

五种分类是 `matched`（记录与权威一致）、`orphan_intent`（intent 无可证效果）、
`orphan_effect`（权威侧有效果但没有记录认领）、`divergent`（记录与权威不一致）与
`unknown`（权威读不到）。每个结果都给出 `evidence`：来源、引用，以及该来源的 `ok` 或
`unknown` 状态。

`request-show` 回执为 `completed`、`pending`、`absent` 或 `unknown`。**`absent` 不是“动作
没发生”的证明**——它只说明权威不认识这个 request 键，因此仍由效果探测来判定。只有 Orca
Task ID 可以作 join key：临时进度任务（`orca_task_id: null`）永远不是 join key，所以只有
权威给出非空 Task ID 或 dispatch 身份后才会写投影。

## 固定派发顺序

每次派发遵循同一顺序，绝不跳过尚未持久的前置记录：

1. 写 preflight 回执（preflight 失败阻止派发）；
2. 写台账 intent（intent 写失败阻止调用）；
3. 执行 `worker-start --retry-request <request_id>`；
4. 写带 raw receipt 摘要和证据引用的 result（result 写失败时保留原始回执、记录 gap、绝不重放）；
5. 尽力写 progress；失败记录 gap，派发继续，下一检查点用同一 `event_id` 重试；
6. 执行回执后检查（`turnStart`、隔离、inbox）并记录 observation。回执后检查失败是证据，不是
   再次派发的理由。

丢失的 `worker-start` 响应从权威对账，绝不重新派发。缺少 intent 阻止第二次派发；`absent`
不是“没发生”的证明。send、reply、ack、stop、abandon、retain、release 遵循同样的 request-id
纪律。

## 动作矩阵

`幂等` 是动作前键类别；`回读` 是能证明效果的权威；`不确定时` 是 pending 或不可读回执允许做的事。

| 动作 | 幂等 | 动作前键 | 回读 | 不确定时 |
| --- | --- | --- | --- | --- |
| dispatch / `worker-start` | `joinable` | `request_id` | 回执 `state`/`stage`/`turnStart`/`dispatchId`；按 task/spec/worktree 查 worker 列表 | `request-show`；`completed` → 采纳已记录结果；`pending` → 同键重放；`absent` → 先盘点 Orca 与 git worktree，绝不再次派发 |
| `send`、`reply` | `joinable` | `request_id` | 消息 id/state 与线程可见性 | `request-show`；绝不因为没收到回复就自动重发 |
| `ack` | `replay_safe` | `request_id` | ack 回执 | 同键重放 |
| `stop`、`abandon` | `joinable` | `request_id` | `worker-show`/`worker-list` 终态 | `request-show` 加盘点；没有证明绝不重试 |
| `retain`、`release` | 结算后 `replay_safe` | `request_id` | 回执加 `worker-show` | `release_unknown` 以回执为准；绝不 release 未结算 worker |
| `merge` | `readback_only` | 无 | PR host `MERGED`、merge SHA、ancestry | 只读；绝不自动重放 |
| `pr-create` | `lookup_then_create` | head 分支 | PR 号与 head SHA | 按 head 查找；找到就采纳 |
| `tracker-close` | `readback_only` | 无 | tracker 回读 | 只读；关闭遵循仓库与人工规则 |
| `progress` | `replay_safe` | `event_id` | 快照/回执 | 同一 `event_id`；失败记录 gap，绝不阻塞派发 |
| `preflight` | 预写（`none`） | `record_id` | 台账回执 | 重跑检查；回执不可读绝不派发 |
| `health` | `replay_safe` | `record_id` | 台账回执 | 同一 `record_id` |
| `acceptance`、`closeout` | `none` | `record_id` | 台账记录 | 重试至持久；绝不提前 release 或关闭门禁 |

四类结果绝不自动重放，一律成为 `decision`：merge（`merge_replay`）、tracker 关闭
（`tracker_close_replay`）、失败未经证明的重试（`unproven_retry`；记录结果为未知时是
`unknown_outcome`），以及 worker 结算未获证明的 release 或 retain（`unsettled_release`）。
没有回复是 `send_reply_no_resend`：没有回复不等于失败。worker 仍在运行时 stop 或 abandon 是
`stop_abandon_no_retry`。

## 两条硬规则

1. **动作前写 intent。** intent 至少包含 `request_id`、action、`run_id`、已知的
   task/dispatch/attempt、目标、预期效果、时间戳与 actor。
2. **不确定时先读权威。** `request-show` 的 `absent` 不是“没发生”；只有权威侧的正面缺失
   （该 Task 没有 Dispatch、该 head 没有 PR）才允许判定“未执行”。

## 进度投影

台账是派发记录；`progress.json` 是尽力而为的投影，绝不能成为派发的唯一记录。progress 写入按
`event_id` 是 `replay_safe`；失败记录一条 action 为 `progress` 的 `gap`，绝不阻塞派发。下一个
检查点用同一 `event_id` 重试，并从台账回填缺失的 worker 行（dispatch id、attempt、worktree）。

对账与回填写入携带 `bookkeeping: true`，不刷新 `last_observed` 或 `last_effective_progress`；
只有真正的新事实才刷新它们。progress Run 携带 `orca_run_id`；对账会与台账 Run 比较，可读且非
空的绑定不一致时产生一条 Run 级 `divergent` 结果（W2，action `progress`），带
`divergent_marker` 修复与 `run_binding_mismatch` 决策。
临时 progress 任务（`provisional: true`、`orca_task_id: null`）永远不是 join key；回执之后
只有 Orca Task ID 是 join key。`turnStart: observed` 一步写为 `observed`；`unsupported` 或缺失
写为 `unknown`，而 `unobserved`（已检查但未观察到）与 `unknown` 不同。`state: ready` 不是已开始
的 turn。仅当 spec 明确携带 PROGRESS/STATE 路径时才启用 worker 自报；`source=worker` 写入永远
不是验收证据。

attempt 映射：派发结果优先使用 Orca 回执的 `attempt`；回执没有已验证的 attempt 时，记录
`detail.attempt_source: local`，该本地计数不参与权威对账。写入时把回执 camelCase 字段转成
snake_case，并在 `detail.raw_receipt` 保留原始回执；设计未验证的回执字段绝不当作稳定契约断言。

## 权威快照

`reconcile` 把当前状态读成一份快照。每个来源要么是带行的 `ok`，要么是带原因的 `unknown`——
缺失的适配器绝不是空成功。

| 字段 | 内容 |
| --- | --- |
| `schema_version` | `1` |
| `run_id`、`observed_at` | 可移植 Run 标识与 RFC3339 UTC 读取时间 |
| `orca.request_show` | 映射：request id → `{state: completed/pending/absent/unknown, reason, receipt}` |
| `orca.workers`、`orca.tasks` | `{state, reason, rows}`；worker 行带 `task_id`、`dispatch_id`、`attempt`、`worker_state`、`worktree` |
| `orca.worker_show` | 映射：dispatch id → worker 行 |
| `orca.messages` | 可选 `{state, reason, rows}`；缺失表示消息权威未知 |
| `pr_host` | `{state, reason, pull_requests}`，行含 `pr`、`head`、`merged` |
| `tracker` | `{state, reason, tickets}`，行含 `ticket`、`state` |
| `git` | `{state, reason, ancestry}`，行含 `commit`、`merged_into` |
| `progress` | 可选 `{state, reason, orca_run_id}`；缺失表示 progress Run 绑定未知 |

顶层或来源内出现未知字段即非法，打字错误不会静默丢掉权威。`LiveAuthority` 负责构建快照：
Orca 读命令经 `ORCA_CLI_COMMAND` 解析；`worker-list` 带 `--include-remote`，它与 `task-list`
都跟随 `page.hasMore` / `page.nextCursor`。cursor 缺失或重复、非零退出、非 JSON 回复都记
`unknown` 并附原因，绝不当作空结果。

## 对账报告

`classify(records, snapshot)` 是纯函数，返回的报告由 `reconcile` 执行：

| 字段 | 含义 |
| --- | --- |
| `schema_version`、`run_id`、`observed_at` | 报告标识 |
| `classifications` | 每个 intent 与每个未被认领的效果一项：`classification`、`window`、`action`、`request_id`、`subject`、`evidence`、`repairs`、`decision`、`conflicts` |
| `repairs` | 汇总所有结果中的允许修复 |
| `decisions` | 必须由 coordinator 决定的项，绝不自动执行 |
| `unreadable` | 读不到的来源及原因 |
| `counts` | 每种分类的计数 |
| `authority_sources` | `orca`、`pr_host`、`tracker`、`git` → `ok` 或 `unknown` |
| `replays` | 由 `reconcile` 添加：实际执行的同键重放及其结果 |
| `closeout` | 由 `reconcile` 添加：closeout 的 `record_id` 与报告路径 |

`reconcile` 把机器报告写到 `runs/<run_id>/reports/reconcile-<digest>.json`（`0600`，原子写），
并且最多追加这些记录：迟到回执的 `result`、迟到效果的 `observation`、投影更新的 `progress`
observation、标记分歧的 `observation`、读不到来源的 `gap`、待决项的 `pending`，以及一条
`closeout`。生成的 id 是确定性的（对 Run 与来源摘要做 `uuid5`），所以对同一权威重复对账不
追加、不重放，并返回已存的报告。摘要排除 `observed_at` 与对账生成的记录；每条生成的记录用
`detail.repair` 标明修复种类。

```bash
# stdout 输出 JSON 报告；--human 输出可读渲染。
python3 "$LEDGER" reconcile --run-id "$RUN"
python3 "$LEDGER" reconcile --run-id "$RUN" --authority recorded.json --human
# 对照 progress 投影的 Run 绑定与台账 Run。
python3 "$LEDGER" reconcile --run-id "$RUN" --progress progress.json
# 绝不重放 pending 的可 join 动作；改为记录决策。
python3 "$LEDGER" reconcile --run-id "$RUN" --no-replay
```

`--authority FILE` 只提供 `pr_host`、`tracker` 与 `git` 段（也可携带 `progress` 段）；Orca
始终经 CLI 读取。`--progress FILE` 读取 progress 快照的 `run.orca_run_id`。重放是唯一
获准的变更：用同一个 `--retry-request` id 重跑 intent 上记录的命令。对账绝不 merge、关闭
ticket，也绝不 stop、release 或重试 worker。

## 对账并发

台账只读一次，作为一致的**前缀**且**不加锁**，所以其他进程的 append 可以继续。持有每 Run
锁时不调用任何 Orca 或宿主适配器；所有权威调用与重放先返回，随后每次 append 短暂持锁。这是
刻意为之：避免台账锁与 Orca、PR host 或 tracker 之间产生锁顺序。读取之后追加的记录不会丢
——下一次对账会看到它。

## 模块缝

`ledger.py` 可按路径 import，独立于 CLI。稳定函数：`validate_record(record)`、
`normalize(record)`、`append(record)`、`read(run_id, filters)`、
`read_report(run_id, filters)`（记录加损坏行）、`summary(run_id)`、
`home_path(raw, target_repo)`、`validated_authority(snapshot)`、`classify(records, snapshot)`、
`validate_report(report)`、`reconcile(run_id, authority, home, replay)`、
`render_report(report)`，以及 `LiveAuthority` / `RecordedSource` 适配器。模块调用方注入一个
带 `snapshot(run_id, records)` 的权威对象；只有在允许重放时才需要
`replay(request_id, action, subject, command)`。测试使用 `tests/fake_orca.py` 中的共享假件，
绝不接触真实 Orca 或网络。
