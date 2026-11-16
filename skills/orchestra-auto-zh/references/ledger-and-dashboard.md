# 仓库外协调台账与本地进度看板操作

**什么时候读**：写入或查询动作 `intent`/`result`/`pending`（`ledger.py`）、在崩溃/上下文压缩后以 `ledger.py status` / `reconcile` 对账恢复，或在任意真实 Run **首次派活前**初始化并打开进度看板（`progress.py`）时。

## 1. 仓库外台账（`ledger.py`）

台账绝不能放进任何仓库内（`$ORCA_LEDGER_HOME`，默认 `~/.orca/ledger`）。优先使用 `status --compact` 一条命令拿全貌，`reconcile` 默认复用已终态缓存回执 `raw_receipt`（加 `--refresh-requests` 强制全重查）：

```bash
LEDGER="$SKILLS_ROOT/orchestra-auto-zh/scripts/ledger.py"
python3 "$LEDGER" status --run-id "$RUN" --compact
python3 "$LEDGER" append --run-id "$RUN" --record - < intent.json
python3 "$LEDGER" reconcile --run-id "$RUN" [--no-replay] [--refresh-requests] [--human] --compact
```

（`ORCA_CLI_COMMAND` 覆盖 Orca 可执行名。文件锁、`fsync`、30 天/10 MiB 归档与全量快照 schema 见[台账实现细节](../../../docs/design/ledger-internals.md)。）

**两条硬规则：**
1. **动作前写 intent**：写明 `record_id`、`request_id`、`action`、`run_id`、`subject`、`expected_effect`、时间与角色。
2. **不确定时先读权威**：`request-show` 回 `absent` 不代表未执行，必须盘点 Orca worker、git、PR host 与 tracker 后才下结论。

**固定派发顺序：**（1）写 preflight 回执（失败停）→（2）写台账 `intent`（失败停）→（3）执行 `worker-start --retry-request <request_id>` →（4）写 `result`（写失败保留原回执并记 gap，无 attempt 标 `attempt_source: local`，绝不重放）→（5）尽力写 `progress.py`（失败记 gap 不阻塞）→（6）派活后观测。

**崩溃窗口（W1/W2/W3）与记录失败语义：** 保护副作用落地的记录（`preflight`、`intent`、`acceptance`、`closeout`）写入失败即阻断该副作用；描述结果的记录（`result`、`progress`、`health`）失败只记 gap、后续修复。
- **W1**（动作前：intent 已落盘、无回执且无权威效果）→ `orphan_intent`。
- **W2**（动作后：存在回执或权威效果）→ `matched`、`divergent`、`orphan_effect`。
- **W3**（回执或权威读不到）→ `unknown`。

## 2. 动作矩阵与禁重放规则

| 动作 | 幂等属性 | 动作前键 | 回读核实 | 不确定时处置 |
| :--- | :--- | :--- | :--- | :--- |
| dispatch / `worker-start` | `joinable` | `request_id` | `state`/`stage`/`turnStart`/`dispatchId`；`worker-list` | `request-show`：`completed` 采纳结果；`pending` 同键重放；`absent` 先查 Orca 与 worktree |
| `send`, `reply` | `joinable` | `request_id` | 消息 ID 与线程状态 | `request-show`；未收复不重发（`send_reply_no_resend`） |
| `ack`（历史） | 历史兼容 | `request_id` | 历史回执 | 不自动重放未验证语义 |
| `stop`, `abandon` | `joinable` | `request_id` | `worker-show`/`worker-list` 终态 | 运行中不盲重发（`stop_abandon_no_retry`） |
| `retain`, `release` | 结算后 `replay_safe` | `request_id` | 回执 + `worker-show` | 未结算禁释放（`unsettled_release`）；`release_unknown` 先回读 |
| `merge` | `readback_only` | 无 | PR host `MERGED`、merge SHA、祖先 | 只读不自动重放（`merge_replay`） |
| `pr-create` | `lookup_then_create` | head 分支 | PR 号与 head SHA | 按分支查重并采纳 |
| `tracker-close` | `readback_only` | 无 | tracker 读回 | 只读不自动重放（`tracker_close_replay`） |
| `progress` | `replay_safe` | `event_id` | 快照/回执 | 同 `event_id` 补写；失败只记 gap |
| `preflight`、`acceptance`、`closeout` | `none` | `record_id` | 台账记录 | 重写直至落盘；未落盘不放行 |
| `health` | `replay_safe` | `record_id` | 台账记录 | 同 `record_id`；绝不阻塞派活 |

四大非重放类决策永不自动执行：`merge_replay`、`tracker_close_replay`、`unproven_retry` / `unknown_outcome`、`unsettled_release`。

## 3. 进度看板操作（`progress.py`）

只读 sidecar（绝不阻塞派发、轮询 Orca 或提供文件读）。`AUDIT_ROOT` 必须设在仓库外的持久目录（拒绝符号链接/仓库祖先）。所有真实 Run **首次派活前**初始化并运行 `open`（拉起单实例本地服务并尝试打开浏览器；无法自启时让用户**手动**访问输出地址；停止服务对其 PID 发 `kill PID`）。演示模式使用 `progress-demo.py` 加 `serve --state <dir> --port 8766`（显式 `serve` **不打开浏览器**）。完整 HTTP 接口、安全头/防 DNS 重绑定、单例 `server.json`/`startup.lock`、显式 `registry prune` 与 Playwright 细节见[看板架构](../../../docs/design/dashboard-architecture.md)。

**协议 v2 与记录检查点：** 快照包含 `schema_version: 2`、`orca_run_id`、`health`、`provisional`、`orca_task_id`、`last_observed`、`last_effective_progress`、`pending_decision` 与对账回填标记 `--bookkeeping`。写入一律配 `--json` 与 `--lock-timeout`（遇 `degraded`/`lock_busy` 仍退出 `0`）。

安全包装函数（`set -e` 下也不退出，覆盖全量字段 `task_id`、`dispatch_id`、`attempt`、`parent_worker_id`、`G-impl`、`G-merge`、`implemented`、`integrated`、`merged`、`tracker_closed`、`turnStart`、`unobserved`、`ready`、`request_id`、`preflight`、`intent`、`result`、`observation`）：

```bash
progress_record() {
  local output status
  status=0
  output=$(python3 "$PROGRESS" --state "$STATE" --json "$@" 2>&1) || status=$?
  if [ "$status" -ne 0 ]; then
    printf '%s\n' 'Progress warning; continue orchestration; record a ledger gap' >&2
  elif printf '%s' "$output" | grep -q '"degraded": true'; then
    printf '%s\n' 'Progress degraded; continue orchestration; record a ledger gap' >&2
  fi
  return 0
}
progress_record task --event-id task-1 --data '{"id":"t1","title":"Protocol","dependencies":[],"status":"ready"}'
progress_record worker --event-id dispatch-1 --data '{"id":"w1-a1","task_id":"t1","dispatch_id":"d1","attempt":1,"worktree":"protocol-worktree","lifecycle":"dispatched","start_confirmation":"unobserved"}'
progress_record worker --event-id start-1 --effective --data '{"id":"w1-a1","lifecycle":"running","start_confirmation":"observed"}'
progress_record worker --event-id report-1 --source worker --data '{"id":"w1-a1","task_id":"t1","dispatch_id":"d1","attempt":1,"reported_lifecycle":"reported_done","reported_summary":"Implementation ready for review"}'
progress_record delivery --event-id accept-1 --effective --data '{"task_id":"t1","implemented":true,"evidence":["acceptance-report.txt"]}'
progress_record end --event-id end-1 --data '{"status":"paused","phase":"waiting","summary":"Awaiting integration"}'
```
