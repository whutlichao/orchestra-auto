# 失败派活恢复、运行健康度与格式化噪声处置

**什么时候读**：当 `worker-start` 返回 `state: failed`、通过 `health.py` 评估异常或静默 worker 状态，或收尾时发现工作区存在万行级纯重排 diff 时。

## 1. `worker-start` 失败恢复（固定顺序）

绝不对失败的 `worker-start` 做无条件重派或凭记忆 `worker-release`。严格遵循 `closeout.py` 强制顺序：**读回取证并分类 → 结算回读（`worker-show`） → 持久化失败收尾（`failure-record`） → 授权决策（`failure-plan`） → `worker-release`**。

| 分类（`classification`） | 判定依据 | 动作 |
| :--- | :--- | :--- |
| **瞬态基础设施**（`transient_infrastructure`） | 就绪/连接阶段超时、占槽、忙碌，独立重读已恢复且无半成品工作 | 仅在完成步骤 2 与失败收尾后重派 |
| **setup 故障**（`setup_fault`） | setup 命令实际报错或配置错误导致退出 | 修复 setup 根因后再跑；禁往坏环境重派 |
| **隔离/落点故障**（`isolation_fault`） | worktree/分支/落点创建失败 | 记录并走降级方案（串行/逐票回退），不原地重复坏落点 |
| **产物已存在**（`artifact_present`） | worktree 内已产出现实改动或持久交付物 | 先按 `G-impl` 取证验收或打回，严禁覆盖丢失 |
| **权限/配置故障**（`authority_fault`） | 禁用或未配置 agent、缺凭证/权限、设置不可读 | 立即升级（绝不替人换 agent 或借用句柄） |

**分类前取证要求：**
- 运行 `terminal show`、`git worktree list`、`terminal read`、`git -C <worktree> status --porcelain`。记住：终端在 + worktree 在 + setup 零输出退出，它本身**不**证明就是瞬态故障（同样可能是 hook 根本没跑或静默死亡，必须核实 `recovery` 指引与实跑结果）。
- 重跑 setup 前必须满足三项：**幂等**（重跑不损坏状态）、**授权**（安装/迁移/外联在本轮边界内）、**副作用已知**（记下变动以便归因）。**谁执行**：新派发下的 `--setup run`（或涉及凭证审批时由人类执行，协调者不在 worker 目录代跑 setup）。
- **任何未提交工作先对账**，再拿 `worker-show` 回读推进固定命令顺序：

```bash
CLOSEOUT="$SKILLS_ROOT/orchestra-auto-zh/scripts/closeout.py"
<ORCA> orchestration worker-show --dispatch <failed_dispatch_id> --json > /tmp/worker-show.json
<ORCA> orchestration worker-list --json > /tmp/worker-list.json

python3 "$CLOSEOUT" failure-record \
  --run-id <run_id> --dispatch-id <failed_dispatch_id> \
  --task-id <task_id> --attempt <attempt> --worktree <worktree> \
  --classification <classification> \
  --evidence-ref <receipt_or_terminal_ref> \
  --answer-location <answer_location> \
  --read-back /tmp/worker-show.json --compact

python3 "$CLOSEOUT" failure-plan --run-id <run_id> \
  --dispatch-id <failed_dispatch_id> --read-back /tmp/worker-show.json --compact
```

`failure-record` 写入 `"recorded": true`、`"durable": true`、`"outcome": "failure"`、`"classification"`、`"evidence_refs"`、`"mapping"`（失败写进 `closeout-pending/` 并继续拒放），并返回 `"next_action": "authorize-release"`。唯有 `failure-plan` 返回 `"next_action": "release"` 且 `"allowed": true`（若为 `read-settled` / `record-closeout` / `escalate` 则绝不可越步放行），才允许执行：

```bash
<ORCA> orchestration worker-release --dispatch <failed_dispatch_id> --json
```

同类 setup 连续 2 次失败、命中 `authority_fault` 或单票 2 连败触发熔断时，停下报告人类。

- **失败重试契约（`1.4.265`）**：完成结算收尾且根因已消除后，必须在原 Task 上使用 `worker-start --task <task_id> --retry-of <dispatch_id> --worktree <placement> --agent <agent>` 重试（`--retry-of` 必传 `--task`，不可用 `--spec`，且不继承放置参数）。Orca 会在单条 `--retry-of` 链连败 3 次后熔断；本 skill 更严格的单票 2 连败熔断会先停下问人，熔断后须经人类明确要求方可通过 `task-update --status ready --run <owning_run_id>` 重开原 Task。
- **自动释放与常驻生命周期（`orchestration.worker-auto-release.v1`）**：未保持打开的 worker 会在 `worker_done` 后约 2 分钟触发自动释放（见 `worker-show`/`worker-list` 的 `autoReleaseAt`）；而 `new-top-level` worker 默认保持打开，需在收尾后显式 `worker-release`（同时清除 `worker-keep-open`）。释放后只要记录仍在，`worker-read --source auto` 仍可读取输出。

## 2. Run 健康状态机（`health.py`）

一条命令采集实时信号并用原生模式评估（`mode="native"` 默认跳过已退役的 `inbox` / `unacked` / `waiter_slot`，仅长程模式强制 `nested_roster`；`--observation` 保留作历史审计）：

```bash
HEALTH="$SKILLS_ROOT/orchestra-auto-zh/scripts/health.py"
python3 "$HEALTH" collect --run-id "$RUN" --evaluate --compact
python3 "$HEALTH" retry --run-id "$RUN" --compact
```

**五态与最坏优先序（`lost > blocked > unknown > degraded > healthy`）：**
- `healthy`：所有生效信号在窗口内新鲜正常。
- `degraded`：非关键信号老化、`WORKER_WATCH=0`（`watch_disabled` → 改定时查 `worker-list`）或看板陈旧；可继续编排。
- `blocked`：待人类决策 / 单向门 / 审批 / `agentWait` 对象阻塞，不自动越过。
- `lost`：有正面证据表明 worker 已在未发 `worker_done` 下终止（通常由 `unknown` 超时取证后转入）。
- `unknown`：证据缺失或过期 —— 绝不 `healthy`，亦非失败，下一步只做取证。

| 信号 | 来源 | 默认窗口 | 失常后果 |
| :--- | :--- | :--- | :--- |
| `heartbeat` | worker / 台账（`last_heartbeat_at`） | preamble cadence × 2 + 60 秒（preamble 无正周期时不强制） | 有存活证据 `degraded` 否则 `unknown` |
| `inbox phase` | 历史 `state.json`（仅退役循环） | waiting / checking / backoff 15 分；`error` 立即触发 | `degraded` / `blocked` |
| `unacked 批次` | 旧投递文件与 ack 对照 | 每检查点 | `blocked` |
| `worker-list 快照` | Orca `worker-list` | 存在活跃 worker 时 5 分 | `degraded` |
| `worker-show 观测` | Orca `worker-show` | 5 分 | `degraded` |
| `turnStart` | 回执 / 观测 | 120 秒启动窗；10 分钟未证实升级 | `unknown` → 升级 |
| `progress freshness` | `progress.json` | 300 秒 | 仅记 `gap`（不拉高态） |
| `last effective progress` | 台账 / 非心跳有效标记 | 连续 3 轮无有效推进（忽略心跳） | `degraded`（`no_effective_progress`） |
| `reclaimable 终端` | Orca `worker-list` | 结算超 10 分钟未释 | `degraded` |
| `waiter slot 泄漏` | 历史重装进程/锁检查 | 每次重装 | `degraded` / `blocked` |
| `nested roster` | 长程首份报告 | `long_horizon: true` 时必填 | `unknown` → `degraded` |

注意 `observation.agentWait`（即 `worker_show.agent_wait`）：为 `null` 代表 Orca 已确认无等待，为对象代表等待决策即 `blocked`，字段缺失代表从未查过即 `unknown`。写台账失败自动落盘到 `health-pending/` 并在之后 `health.py retry` 补写，不阻断主线。**release 不是健康状态**，健康状态**从不授权 release**。

## 3. 格式化重排噪声恢复

改动出现四位数以上 diff 时，先当调查信号而非直接 `git checkout`。立即把证据备份到仓库外（`git status --porcelain=v1 -uall`、`git diff --binary > worktree.patch`、`git diff --cached --binary > index.patch`、`git ls-files --others --exclude-standard > untracked.txt`；注意已 staged diff 非空不证明工作区全是噪声）。交由 worker 逐文件对账还原噪声；仅当该工作区工具链确实触发了全文件强重排，才写入仅由 `.git/info/exclude` 忽略、不提交的本地配置 `{"format":{"enabled":false}}`。
