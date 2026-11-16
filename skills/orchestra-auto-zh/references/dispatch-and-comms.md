# 派活循环、身份认证、原生唤醒与回复协议

**什么时候读**：启动 worker（`worker-start`）、核实会话身份或 `turnStart` 证据、处理 `orchestration ask`/回复/权限弹窗、排查卡住或安静的 worker，或迁移历史 `inbox-loop` 状态时。

## 1. worktree 覆盖、Base Ref 与 `worker-start`

Orca 默认会把 worker 放进当前工作区（即 `main`），**这里必须显式覆盖**：

```bash
<ORCA> orchestration worker-start --spec "<spec>" \
  --worktree new-top-level --name <ticket-slug> --setup run --agent <已启用agent> --json \
  --retry-request <request_id>
```

- 必传 `--worktree new-top-level` 与显式 `--agent`（缺省报 `agent_unconfigured`；绝不用 `current` 或 `new-child`）。工作目录与目标仓库不同时显式加 `--repo <selector>`。
- **Fork 与自有仓库判别（`orca repo show --repo <selector> --json`）**：若 `result.repo.upstream` 非空，先 `git fetch upstream` 再显式 `--base-branch upstream/<默认分支>`（fork 的默认分支只镜像 upstream）。若 coordinator 工作区落后 base 过多导致 Task 停在 `ready` 无回执，同步重建或说明 `allow-stale-base: true`，绝不盲重派。
- 固定派发顺序：preflight 回执 → 台账 `intent`（带 `request_id`）→ `worker-start --retry-request` → 台账 `result`（保留 `detail.raw_receipt`，无 attempt 时记 `attempt_source: local`）→ 尽力写 `progress.py` → 派活后观测。

## 2. 会话身份与原生唤醒契约

严禁后台轮询 `check` 或启动已退役的 `scripts/inbox-loop.*`（会抢邮件并压掉唤醒）；只读巡检仅可单次 `check --peek`。Orca 采用**至少一次**原生唤醒送达：完整处理每一批、按消息 ID 去重、重做副作用前先对账权威状态。

按当前会话形态解析本机句柄（不从窗口标题猜测）：
- **结构化会话 / 托管 worker**：`ORCA_AGENT_SESSION_ID` + `orca status --json`（`result.caller.orcaSessionId`）。
- **Orca 直接拉起的 TUI agent 终端**（`caller` 故意留空）：校验 `ORCA_TERMINAL_HANDLE` 属于 `ORCA_AGENT_PANE`：

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

唤醒携带 `check` 命令时由接收方亲自运行（须指定 `--terminal "$ORCA_TERMINAL_HANDLE"`）；默认立即标记返回的最老未读 FIFO 批为已处理。若 `check` 输出丢失用 `--retry-request` 找回；失忆后读 `orca orchestration inbox --terminal run:<run_id> --limit 50 --full`（或 `dispatch:<id>`）。

## 3. 确认 `turnStart` 与处理交互提示

- `turnStart: observed`（或该 attempt 产物/状态投影）即为启动证据；`state: ready` **不是**启动证据。
- 当 `turnStart` 为 `unsupported`、`unobserved` 或缺失时：先 `worker-show --dispatch <id> --json`。有终端看 `terminal show --terminal <handle> --json`；无终端改查 `worker-read --source auto` 与状态投影（不对无终端 worker 强呼 `terminal show`）。
- 未变的空闲标题（`Pi ready`、`OMP ready`、`Terminal 1`、`OC | ...`）既不证起也不证未起。**仅当**看清完整 preamble 停在输入框且当前未运行 turn 时，允许向原终端补发**一次**裸 Enter；否则取证而不连按 Enter 或重派第二个 Dispatch。
- **权限提示 / `agentWait`**：核实具体操作、目标路径与风险。仅授权当前轮次范围内操作；涉及越权、账号、付费或不可逆改动须升级，绝不套用 bypass。

## 4. Worker 提问分类与 `reply` 规范

收到提问先分类，禁止直接照 `➡️ <recommended>` 自动作答：

- **决策类（暂停回复并向人类呈报：原问题 + 你的倾向与依据 + 其他方案代价）**：
  1. 改变交付物外形、交互文案或用户可感知行为的取舍；
  2. 多可行方案架构分叉（接缝、依赖、拆票方式）；
  3. 范围边界增减或验收松紧；
  4. **升级五项**（凭证、第三方账号、付费、人工审批、不可逆 live 数据）—— 原样升级并仅冻结该票，其余并行票继续推进。
- **细节类（协调者直接基于 spec/ADR/仓库作答）**：局部命名、作用域内文件落点、局部测试风格。
- 回复一律走 `reply --id <message_id>`，并在记录写明 `Q → A (human-decided / answered-for-them) → 理由`。worker 端禁 `AskUserQuestion`，协调者向人提问走宿主人类提问通道。

## 旧收件器迁移

若一个 Run 曾运行过已退役的 `inbox-loop`，迁移前必须先肃清，保留全部旧状态目录与日志不动：
1. **盘点所有潜在持有者**：记录全部主机、会话、`binding.json`、`state.json`、`inbox-loop.pid`、`inbox.lock`、自动拉起配置与实锁持有进程及 `check --wait` 子进程。主机或锁归属不明 → **阻塞迁移**并升级。
2. **静止已核实所有者**：**先停止其启动器/自启任务**，仅在**核实进程身份**属于本 Run 后优雅终止（不按宽泛进程名或过期 PID 误杀），**确认进程退出且没有遗留 check** 后复查 `inbox.lock`（绝不靠删锁文件强行过关）。
3. **对账全部本地证据**：逐份读取所有 `deliveries/*.json` 与完整 `receipts.jsonl`（不轻信 `inbox 最近 50 条`），按**消息 ID** 去重。记住：**ack 成功不证明**业务动作已完成，**回执失败或缺失**也不证明未执行；核对真实 PR/worker/台账，**不自动重复**不确定的不可逆动作或旧 ack。
4. **完成后恢复原生唤醒**：把每条处置、未决人工决策与清零结果记入台账后再继续，未决人类问题仍旧保留阻塞。
