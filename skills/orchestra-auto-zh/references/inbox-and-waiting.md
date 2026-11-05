# 通信、原生唤醒与恢复

**何时读**：首次派活前、响应或上下文丢失后。按本机 Orca 发现入口选定可执行文件，读取同一版本的 `skills get orchestration` 和命令帮助。以下观察来自 Orca **1.4.248**；其他版本须重新查证其 guide 和 help。若不支持原生唤醒或消息恢复，停止并报告不支持的契约，不猜旧版协议。

## 身份与原生唤醒

在仓库外台账记录 Run、Task、Dispatch 与运行时签发的 handle。实时 worker preamble 优先。启动独立 worker 后结束 turn：Orca 以完整 chat 消息原生唤醒协调者；若唤醒内容指定 `check` 命令，由 agent 自己执行。再次消费前处理整批，记录每条消息 ID、处理状态、结果与证据；按 ID 去重，并在重复副作用前对账。

普通 `check`（包括 `--wait`）返回最早未读 FIFO 批次时立即标记已处理；这不代表业务动作已完成。不要安装后台消费式 check、轮询器或旧 `inbox-loop`：它会抢走 agent 的消息并阻止原生唤醒。确需一次只读查看时使用 `check --peek`；不要轮询或安装观察脚本。`--types` 只控制等待唤醒，不筛选返回的整批消息。

**本会话的 handle 怎么来。** Orca 有两类身份，别混用：

- **结构化会话 / 受监督 worker**：`orca status --json` 的 `result.caller.orcaSessionId`，
  CLI 从注入的 `ORCA_AGENT_SESSION_ID` 解出，只有这类会话才有。
- **Orca 直接启动的 TUI agent 终端**（本机 pi / omp 的常见形态）：环境里的
  `ORCA_TERMINAL_HANDLE`。这是 Orca 发给这个终端的运行时句柄；用 `ORCA_AGENT_PANE`
  （`<tabId>:<leafId>`）与 `orca terminal list --json` 的行精确对上，确认它属于你，再写台账。

`caller` 为空**不是**「没有身份」——TUI agent 终端本来就不注入 session id，`status` 按设计
省略 caller 而不是拿终端去猜。用下面一行核对自己的 handle（不匹配即非零退出）：

```bash
[ -n "${ORCA_TERMINAL_HANDLE:-}" ] || { echo "no runtime terminal handle" >&2; exit 1; }
orca terminal list --json | python3 -c '
import json, os, sys
handle = os.environ["ORCA_TERMINAL_HANDLE"]
tab, leaf = os.environ["ORCA_AGENT_PANE"].split(":", 1)
row = next(r for r in json.load(sys.stdin)["result"]["terminals"] if r["handle"] == handle)
assert (row["tabId"], row["leafId"]) == (tab, leaf), "handle does not belong to this pane"
print(handle)
'
```

如果没有可核验身份，不猜终端标题或最新终端。只有实时唤醒指定 check 时 agent 才执行它；例如参数 `--terminal "$ORCA_TERMINAL_HANDLE"` 必须指自己的 handle。


## 恢复与审计

check 响应丢失时使用其打印的 `--retry-request` 取回同一批次。上下文丢失时，协调者读取 `orca orchestration inbox --terminal run:<run_id> --limit 50 --full`，worker 使用 `dispatch:<dispatch_id>`；其中包括已处理消息。将证据与台账、真实 worker 和 PR 状态对账。消息已记录不代表副作用已发生：重放前先读回，支持时复用已记录的幂等键；不可逆效果不明时升级。不要伪造 delivery ID 或假设必须执行旧式 ack。1.4.248 help 仅把 `--ack` 描述为兼容旧 Orca 的可选项；这里没有验证任何旧运行时，因此不启用旧收件器，也不推测旧版语义。

已退役的 `scripts/inbox-loop.py` 和 `.sh` 在调用 Orca 或修改旧状态前安全退出。保留旧 delivery 文件与回执作为历史审计证据，不自动删除或迁移；不能用旧循环状态证明当前健康。

## Worker 与生命周期恢复

worker 遵循实时 preamble 提问或完成后结束 turn。`worker_done` 结算 Task；失败不能标成功。静默或存活不明时检查 `worker-list`、`worker-show`、`worker-read` 和实际成果；静默本身不授权停止、重试或释放。app 重启后先确认状态、盘点 worker，再操作；查明原绑定并从 inbox 恢复消息。不确定或被 fencing 的操作遵循实时 guide 的恢复参考，不冒用 handle，也不以新建重复 Run 代替证据。

1.4.248 中已完成 worker 默认自动关闭，kept-open 除外；新 worktree worker 默认 kept-open。检查 `worker-list` 并遵循返回的 `next`；保留用户要求保留的聊天，仅在适当且获授权时释放已结算资源。reclaimable worker 在 `resource.autoReleaseAt` 自动释放，除非被复用或保留；不能把 retained 当 reclaimable，也不能由 Task 完成推断进程退出。
