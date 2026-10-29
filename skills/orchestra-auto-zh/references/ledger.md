# 协调台账（coordination ledger）

**什么时候读**：每个有副作用的动作之前写 intent、之后写 result 时；一个 Run 首次 append
之前、对账崩溃窗口时，或 coordinator 要报告未决 intent 与待处理项时。

台账是持久的 append-only **索引**，不是权威。发生了什么由 Orca、git 与 tracker 决定，台账
指向它们。会闸住副作用的记录——动作前的 intent、preflight 回执、acceptance 或 closeout
记录——必须在动作执行前落盘。append 失败会非零退出且绝不报告记录已写入，所以不要在失败的
写入上认定动作已获授权或门禁已通过。

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

| 记录 | 动作前失败 | 动作后失败 |
| --- | --- | --- |
| intent | 不执行动作；报告并升级 | 不适用 |
| result / observation | 不适用 | 重试记录；绝不重放动作；必要时记 gap |
| acceptance / closeout / preflight | 不 release、不关闭、不派发 | 重试至持久；门禁保持打开 |

规则是：**闸住副作用的记录会阻止副作用；描述结果的记录永远不阻塞编排，稍后修复。** 不要从
失败命令“没有报错”推断持久性，也不要在记录尚未持久时宣称门禁通过。

## 供对账使用的模块缝

`ledger.py` 可按路径 import，独立于 CLI。后续工作流可用的稳定函数：`validate_record(record)`、
`normalize(record)`、`append(record)`、`read(run_id, filters)`、
`read_report(run_id, filters)`（记录加损坏行）、`summary(run_id)` 与
`home_path(raw, target_repo)`。`classify` 与 `reconcile` 属于后续工作流，本票刻意不实现；
它们应消费这些函数，而不是重新解析文件格式。
