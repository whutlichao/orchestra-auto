# 进度记录与 localhost 看板

**何时读取**：每个实际编排 Run 首次派活前，以及需要查看进度或离线演示时。

目录：[使用](#使用)、[可视化看板](#阅读可视化看板)、[协议 v2](#协议-v2)、[单例注册表](#单例看板注册表)、[流程写入责任](#流程写入责任)、[恢复与边界](#恢复与边界)。

## 使用

这是不阻塞编排的旁路。coordinator 在正常流程动作完成后写入可观察事实。记录工具和页面均不调度、
验收或查询 Orca，不执行 `check`、`ack` 或 inbox 命令；现有唯一 inbox 接收器保持权威。
写入不依赖网页服务存活。进度命令失败时保留警告并继续编排，不能把看板错误变成派发失败。

运行依赖：POSIX 上的 Python 3 标准库。`AUDIT_ROOT` 指向**所有仓库之外**已有持久审计位置，
每个 Run 使用新的子目录。
禁止仓库内部、仓库根目录、目标仓库祖先目录或符号链接路径。输入支持 `--data JSON` 或
`--data -` 从 stdin 读取。仅接受摘要白名单字段，不接受环境、密钥、原始消息、终端正文或任意
metadata。允许字段中的摘要也必须人工去敏；这是字段白名单，不是秘密检测器。证据和 artifact
只存引用，不读取文件正文。

```bash
PROGRESS=/path/to/orchestra-auto-zh/scripts/progress.py
STATE="$AUDIT_ROOT/run-example"
python3 "$PROGRESS" --state "$STATE" init --event-id init-1 --effective --data \
  "{\"id\":\"run-example\",\"target_repo\":\"$PWD\",\"mode\":\"REAL\",\"phase\":\"implementation\",\"status\":\"active\"}" \
  || printf '%s\n' 'Progress warning; continue orchestration' >&2
python3 "$PROGRESS" --state "$STATE" open || printf '%s\n' 'Progress warning; continue orchestration' >&2
```

将 `PROGRESS` 换为当前安装的本 skill 目录下的绝对路径。首次派活前使用真实 Run ID、新的仓库外审计子目录及实际目标仓库执行上述 init 和 open；不要使用示例 ID 或路径。`open` 把该 Run 注册到每用户唯一的看板服务，必要时启动这个唯一服务，并尝试打开默认浏览器，以 JSON 输出 `{url, pid, degraded, reason, browser_opened}`；把 URL 告知用户。浏览器无法打开时告知用户手动访问该 URL。服务每三秒刷新，后续节点按下表记录；服务关闭也可写入。看板保持可用以便查看最终状态；需要停止时对该单例 PID 执行 `kill PID`，所有快照保留；下一次 `open` 会重启服务并重新注册。`show` 不启动服务即可输出快照；`serve --port 0` 自动选择并打印地址。
`--stale-seconds 300` 控制观察过期阈值；缺少时间显示 unknown，旧观察显示 stale，coordinator
明确记录的 lost 生命周期独立显示。新活动不代表有效进展。

**DEMO** 必须使用与所有 **REAL** Run 分开的**新目录**：

```bash
python3 skills/orchestra-auto-zh/scripts/progress-demo.py --state "$AUDIT_ROOT/demo-example" --target-repo "$PWD"
python3 "$PROGRESS" --state "$AUDIT_ROOT/demo-example" serve --port 8766
```

打开 **http://127.0.0.1:8766/**。DEMO 标记与示例引用清楚说明模拟性质。demo 拒绝任何已存在目录，
不能覆盖真实 Run，不调用 Orca，不需要凭证。包含正常工作、依赖阻塞、等用户、验收打回、
嵌套 worker、失联 worker、独立交付事实；用 `serve --stale-seconds 0` 展示过期。
不得将演示引用当作真实已执行测试。
DEMO 与其他按需查看都使用显式 `serve`：只打印地址、不打开浏览器；自动打开（`open`）仅用于实际
REAL Run 首次派活前。

## 阅读可视化看板

两个安装包提供 byte-identical 的同一页面，默认中文，可切换 English。浅色画布、固定左导航与顶栏
依次组织运行概览、关注事项、任务统计、静态参考流程、依赖图、任务与 Worker、交付证据和事件；
不加载远程字体、资源或前端包。每三秒读取一次，筛选、展开明细、依赖图滚动位置及语言在刷新后
保留；错误带时间，保留完整的最后有效页面。

关注事项只列出已记录的阻塞原因、待决事项（含无任务级决策但 Run 等待用户）、失败记录与失联
Worker；每个实体在所属分组单独计数，不推断未报告的问题。运行概览
将最近观测与最近有效进展分列；Worker 行将核验生命周期、自报、启动确认与时效分列。

环图只表示**实际记录的任务数量分布**，包含未知状态，中心为实际总数。文字图例可筛选任务，
筛选状态及重置明确可见。空快照显示零，不虚构扇区。不估算完成百分比或剩余时间。四项交付卡片
分别统计严格 `true`、严格 `false`、null/缺失（未核验）；没有 delivery 不代表成功。

流程箭头是**静态参考路径**，不是调度器、实时遥测或前序通过证明。蓝色仅代表本项有匹配记录，
每一步可展开统计依据：

| 参考步骤 | snapshot 的准确推导 |
| --- | --- |
| 计划 | `tasks` 记录数；仅表示任务已记录，不代表计划已审批 |
| 派活 / 启动 | `start_confirmation == "observed"` 的 worker 数；dispatched 不等于已观察启动 |
| 实施 | `status == "running"` 的任务数，不是实施验收数 |
| 完成报告 | `reported_lifecycle == "reported_done"` 的 worker 数；仅自报，不是已验收 |
| 验收 | `status == "accepted"` 的任务数 |
| 集成 | `integrated === true` 的 delivery 数 |
| 合并 | `merged === true` 的 delivery 数，PR 产物不自动设置 |
| tracker 对账 | `tracker_closed === true` 的 delivery 数，合并不自动设置 |
| 等待支路 | `status == "waiting_user"` 或 `pending_decision` 非空的任务数，同一任务只计一次 |
| 返工支路 | `status == "rejected"` 的任务数 |

零表示没有匹配记录，不是核验 false，也不是证明动作从未发生。当前阶段直接读 `run.phase`，不据此
点亮前序步骤。交付 false/null 与流程零计数分开；worker 自报不更新已核验生命周期或任务状态。

原生 SVG 依赖图从原 `dependencies` 数组绘制「依赖 → 任务」，保留未知节点、循环与长标题；完整文本
可通过 SVG 可访问标题及可展开依赖文本读取。图内滚动有边界，不计算调度顺序。Worker 父子缩进，
核验、自报、启动确认及时效分列；完整 ID、worktree 和摘要放入明细。仅无凭证 HTTP(S) 引用可点击，
其他内容为文本，不提供文件内容。

页面同时写明 **警告不是启动失败**：记录或看板警告只表示「继续编排」，记录一个 gap，绝不把一次派发
判定为启动失败。

可选浏览器回归使用仓库外的 Playwright 与 Chromium，不是运行依赖，也不是强制门禁——强制门禁是上文
的 CLI/HTTP 契约。设置 `PLAYWRIGHT_MODULE` 指向该外部包，然后针对注册了两个 Run 的单例服务执行
`node tests/progress_dashboard_browser.cjs 'http://127.0.0.1:PORT/?run=FIRST&second=SECOND' EVIDENCE_DIR`。
脚本拦截 `**/snapshot*`，通过 Run 列表切换所选 Run，覆盖四种视口、事实边界、刷新状态保留与安全渲染；
环境未提供 Playwright 时它只是不运行，测试套件不会因此失败。

## 协议 v2

`progress.json` 是 UTF-8 快照，包含 `schema_version: 2`、`run`、按 ID 索引的 `tasks`、`workers`、
`deliveries`、`artifacts`、有序 `events` 和私有幂等 `receipts`。每次成功写入在进程锁下原子替换
整个快照；HTTP/show 不暴露 receipts。读取接受 schema v1 与 v2；所有写入记为 v2，首次 v2 写入会
就地迁移 v1 快照。未知版本报错且不改文件。可备份此单文件作为审计；工具不写目标仓库。

schema v2 新增可选字段：`run.orca_run_id`（Orca Run 绑定，与台账 Run 不一致即 divergent）、
`run.health`（`healthy`/`degraded`/`blocked`/`lost`/`unknown`，是投影；健康权威在台账）、
`task.provisional`、`task.orca_task_id`，以及 event 上的 `bookkeeping`。Orca Task 尚不存在时先写
`provisional: true` 且 `orca_task_id: null` 的任务，回执后绑定；只有 Orca Task ID 是 join key。
未列出的 JSON 字段仍会被拒绝。

统一写接口：

```text
python3 PROGRESS --state STATE [--json] [--lock-timeout SECONDS] [--bookkeeping] COMMAND --event-id STABLE_ID --source coordinator|worker [--effective] --data JSON
```

相同事件 ID 只能重试完全相同的 command、source、JSON、effective 与 bookkeeping；一致重试无副作用，
时间戳也不更新，冲突重试失败。新观察使用新 ID。source 默认 coordinator，是可信本地写入者声明的
来源，不是身份认证。只有 coordinator 观察到有效进展后才能加 `--effective`。`last_observed` 随
记录的活动更新，`last_effective_progress` 仅由显式验证更新，不估算完成百分比或剩余时间。
`--bookkeeping` 标记对账与回填写入：仍会记录，但不刷新 `last_observed` 或
`last_effective_progress`，也不能与 `--effective` 同用。

### 退出码

使用 `--json` 时，写入在成功或有意降级时退出 `0`，在调用方输入或状态损坏时退出 `1`。降级在
stdout 输出 `{"degraded": true, "reason": ...}`；快照锁冲突是 `degraded/lock_busy`，不是调用方
错误。错误输出 `{"degraded": false, "error": ...}` 并退出 `1`。`--lock-timeout` 修改锁等待
（默认 2 秒）。不使用 `--json` 时，旧退出码再保留一个小版本：所有失败都以 `1` 退出并在 stderr
打印 `Progress warning`。coordinator 包装函数使用 `--json`，对任何非零或降级结果记录台账 gap
并继续；派发控制流绝不以进度退出码分支。看板失败永不阻塞派发。

| 命令 | JSON 摘要字段（其他键拒绝） |
| --- | --- |
| `init` | 必填 `id`、`target_repo`（已有目录）、`mode`（REAL/DEMO）；可选 `title`、`phase`、`status`、`orca_run_id`、`health` |
| `run` | 更新 `phase`、`status`、`title`、`orca_run_id`、`health` |
| `task` | 必填 `id`；可选 `title`、`dependencies`（ID 列表）、`status`、`blocked_reason`、`pending_decision`、`provisional`（布尔）、`orca_task_id`（标识符；provisional 阶段可为 null） |
| `worker` | 必填 `id`；首次登记还需已有 `task_id`、`dispatch_id`、正整数 `attempt`；可选 `parent_worker_id`（先登记父级）、`worktree`、`start_confirmation`、`lifecycle`、`reported_lifecycle`、`reported_summary` |
| `event` | 必填 `type`、`summary`；可选已有 `task_id`、`worker_id`、`dispatch_id`、`attempt`、`evidence`（引用列表） |
| `delivery` | 必填已有 `task_id`；独立布尔字段 `implemented`、`integrated`、`merged`、`tracker_closed`；可选 `evidence` |
| `artifact` | 必填 `id`、`kind`（report/test/commit/pr/other）、`label`、`reference`；可选已有 `task_id` |
| `end` | 必填 `status`（completed/failed/paused/cancelled）；可选 `phase`、`summary`；记录 `ended_at` |

Run status：`unknown`、`active`、`waiting_user`、`failed`、`paused`、`completed`、`cancelled`。
Run health：`healthy`、`degraded`、`blocked`、`lost`、`unknown`。
Task status：`unknown`、`pending`、`ready`、`running`、`blocked`、`waiting_user`、`reported`、
`accepted`、`rejected`、`failed`、`paused`、`cancelled`。
Worker lifecycle：`unknown`、`dispatched`、`running`、`waiting_user`、`reported_done`、`failed`、
`lost`、`reclaimable`、`closed`。start_confirmation：`unknown`、`observed`、`unobserved`；v1 快照
可能仍带旧值 `unsupported`。回执 `turnStart: observed` 一步写为 `observed`；`unsupported` 或缺失
写为 `unknown` 并采集该 agent 支持的证据；`unobserved` 表示已检查但未观察到，与 `unknown` 不同。
`state: ready` 永远不算已开始的 turn。
字段缺失表示未知，不推断成功。ID 为字母、数字或 `_.:-`，长度 1–128；摘要字符串最多 4096 字符，
引用列表最多 100 个短字符串，JSON 输入最多 64 KiB。task 可引用尚未记录的依赖，页面显示 unknown。
依赖和阻塞由 coordinator 显式写入，不是自动计算的调度。每个 worker ID 对应不可变的
任务/dispatch/attempt/父级身份，重试登记新 ID。

worker source 仅能更新已登记 worker 的 `reported_lifecycle`、`reported_summary` 或追加 event。
每份报告必须携带匹配的 `task_id`、`dispatch_id`、正整数 `attempt` 和 worker `id`（event 用
`worker_id`）；过期 attempt 拒绝。不能改验证、task 状态或交付事实。即使 event 摘要声称成功，
页面仍标为 worker 自报。`worker_done` 不等于验收，也不设置 implemented。coordinator 在 G-impl
通过后设置 implemented，有集成证据后设置 integrated，真实 merge 回执后设置 merged，tracker
实际对账后才设置 tracker_closed。开 PR 不自动改变任何交付字段。缺失事实为 null，页面显示
unverified；显式 true/false 表示 verified yes/no，可用 false 纠正历史错误。

以下包装函数使用 `--json`，在 `set -e` 下也始终成功；失败或降级都会留下可见警告，coordinator
据此记录台账 gap 并继续正常编排：

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

## 单例看板注册表

每用户只有一个只读看板服务，只有 `open` 会启动或采纳它。`$ORCA_PROGRESS_HOME`
（默认 `~/.orca/progress`，目录权限 `0700`，位于所有仓库之外，可用 `--registry` 覆盖）包含：

```text
$ORCA_PROGRESS_HOME
├── server.json          # {pid, port, protocol_version, started_at, binary_version}
├── startup.lock         # 仅在启动期间持有的 flock
├── starting.json        # {pid, port}：启动崩溃窗口
└── runs/
    └── run_xxx.json     # {run_id, state_path, registered_at, label}
```

`progress.json` 仍是持久事实来源；服务无状态，其丢失只是一个 gap，从不是 Run 事件。注册描述符
只含 Run ID、状态路径、注册时间与可选标签：不含凭证，也不含消息或终端内容。`$ORCA_PROGRESS_HOME`
不得位于任何仓库内；记录器与注册表共用同一个状态路径校验器（两者都拒绝符号链接与仓库路径）。

**启动算法。** 在 10 秒超时的 `flock(startup.lock)`（`--startup-timeout`）内：

1. 读取 `server.json`，以 500 毫秒超时探测 `GET /health`。
2. 健康且协议兼容：向该服务注册本 Run 并打开 `/?run=<id>`。对已注册 Run 的第二次 `open` 只更新
   其描述符，不重复创建。
3. 旧版服务（无 `/health`）或协议不兼容（`protocol_version` 低于客户端最低版本）：绝不 kill、绝不
   删除；回退并记录 gap，提示用户自行停止旧服务。
4. 超时或 5xx：视为卡死，绝不 kill 或删除；回退并记录 gap。
5. 连接被拒绝：只能证明记录中的服务确已消失，此时才删除 `server.json` 并启动新服务。
6. 否则启动 `serve --registry $ORCA_PROGRESS_HOME --port 0`，先写带子进程 pid 与端口的
   `starting.json`，最多等待 5 秒的 `/health`；随后原子写入 `server.json`（临时文件、fsync、
   rename、目录 fsync），注册本 Run 并删除 `starting.json`。

后续调用者若发现 `starting.json` 会探测它：采纳健康的子进程并写入 `server.json`，仅在记录的子进程
确已死亡时删除 `starting.json`。未能就绪的崩溃窗口子进程由启动它的调用者停止。启动锁超时视为降级：
绝不启动第二个服务。

**接口。** 脚本暴露 `registry.register(run)`、`registry.discover()`、`registry.probe()` 与
`registry.open(run) -> {url, pid, degraded, reason}`；HTTP 层只渲染只读快照。`GET /health` 返回
`{ok, protocol_version, pid, started_at, runs}`；`protocol_version` 从 `1` 开始。
`GET /?run=<id>` 渲染页面，`GET /snapshot?run=<id>` 返回该 Run 的快照；未知 Run 返回错误，绝不
返回其他 Run 的数据。

**HTTP 面与 Run 隔离。** 服务只暴露三条 GET 路由，没有任何写 API：

| 路由 | 返回 |
| --- | --- |
| `GET /health` | `{ok, protocol_version, pid, started_at, runs}`：服务状态与已注册 Run ID，绝不含状态路径 |
| `GET /?run=<id>` | 页面：由 `/health` 生成的 Run 列表，加上所选 Run 的 tasks、workers、deliveries、events、时效与健康投影，以及「警告不是启动失败」提示 |
| `GET /snapshot?run=<id>` | 该 Run 的只读快照，不含任何其他 Run 的数据 |

隔离靠每次请求只解析一个描述符：快照只读取所选 Run 的 `progress.json`，且必须经过共用的状态路径校验器；
响应只出现 Run ID，绝不出现状态路径；跨 Run 访问仅限 `/health` 与页面上的 Run ID 列表。描述符不可读、
或 `progress.json` 缺失/损坏时返回 `503` 与 `Snapshot unavailable`，并在 Run 列表中标记为不可用；
描述符绝不删除、绝不修复；未知 Run ID 返回 `404`。某个 Run 不可用时页面保留 Run 列表，绝不悄悄显示
另一个 Run 的数据。服务不保存任何按 Run 的状态，因此可重启：`progress.json` 仍是事实来源，服务中断
只是一个 gap。

**安全边界。** 服务只绑定 IPv4 `127.0.0.1`，绝不绑定通配或 IPv6 地址；所有路由只接受 GET，其他方法
（含向顶层表单的 `POST`）一律返回 `405` 与 `Allow: GET`。每个响应（包括错误响应）都带
`Content-Security-Policy: default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline';
img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`、
`Cache-Control: no-store`、`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY` 与
`Referrer-Policy: no-referrer`。页面不加载任何外部资源，只拉取同源 JSON。`Host` 头必须是
`localhost` 或 `127.0.0.1`（可带服务端口），否则在路由前直接返回 `403`——这是 DNS 重绑防护，
被重绑的页面无法读取看板。服务只读已注册的 `state_path`：绝不把请求路径映射为文件、绝不提供文件正文、
绝不接受路径作为输入，路径穿越、符号链接状态路径与仓库祖先均由共用校验器拒绝。`$ORCA_PROGRESS_HOME`
保持 `0700`，描述符不含凭证。这是单用户本地信任模型的服务：没有认证，不得对外暴露或转发端口。

**兼容与回退。** 无 `/health` 的旧服务是 legacy：不向它注册，也绝不 kill。任何 `serve --state`
回退（启动锁超时、legacy/不兼容/卡死服务、查看器启动失败、DEMO）都不注册、返回
`degraded: true` 与 `reason`，并豁免单例不变量。`serve --state` 仍用于 DEMO、测试与单 Run 查看，
且从不写 `server.json`。

**退出码与预算。** 使用 `--json` 时，`open` 在成功与有意降级（包括查看器无法启动）时退出 `0`，
在调用方输入错误时退出 `1`；不使用 `--json` 时沿用旧退出码，查看器无法启动仍退出 `1`。`open`
不在派发关键路径上：在派发决策之后或作为尽力而为的后置步骤调用，预算 15 秒，超时即继续。任何
注册表或启动失败都记录台账 gap，绝不阻断派发；任何路径都不会在等待 Orca 调用时持有启动锁，
且 `open` 完全不调用 Orca。

## 固定派发顺序与记录失败

每次派发遵循同一固定顺序，任何步骤都不跳过尚未持久的前置记录：

1. 写 preflight 回执；preflight 失败或被阻塞则不派发；
2. 写台账 intent；intent 写失败则不执行调用；
3. 执行 `worker-start --retry-request <request_id>`；
4. 写带 raw receipt 摘要和证据引用的 result；result 写失败时在证据目录保留原始回执、记录台账
   gap，且绝不重放该动作；
5. 尽力写 progress；progress 写失败记录台账 gap，派发继续；下一个检查点用同一事件 ID 重试，
   并从台账回填缺失的 worker 行（dispatch id、attempt、worktree）；
6. 执行回执后检查（`turnStart`、隔离、inbox）并记录 observation。回执后检查失败是证据，不是
   再次派发的理由。

丢失的 `worker-start` 响应通过权威对账处理，绝不重新派发：`completed` 的 request 键重放其已记录
结果，`absent` 不是“没发生”的证明。缺少 intent 就不会有第二次派发。send、reply、ack、stop、
abandon、retain、release 遵循同样的 intent 与 request-id 纪律；完整动作矩阵见
`references/ledger.md`。

attempt 映射：回执带 `attempt` 时直接使用；缺失时在台账 result 的 detail 记
`attempt_source: local`，该本地计数不参与权威对账。写入时把回执的 camelCase 字段转成
snake_case，并在 `detail.raw_receipt` 保留原始回执；绝不把未验证的回执字段当作稳定契约。

记录失败语义只有一条规则：**闸住副作用的记录会阻止副作用；描述结果的记录永远不阻塞编排，
稍后修复。** progress 写属于后者——失败永远是 gap，绝不变成派发失败——而 intent、preflight
回执、acceptance 或 closeout 写失败是其所保护动作的硬停止。

两条硬规则：

1. **动作前写 intent。** intent 至少包含 `request_id`、action、`run_id`、已知的
   task/dispatch/attempt、目标、预期效果、时间戳与 actor。
2. **不确定时先读权威。** `request-show` 的 `absent` 不是“没发生”；只有权威侧的正面缺失
   （该 Task 没有 Dispatch、该 head 没有 PR）才允许判定“未执行”。

仅当 spec 明确携带 PROGRESS/STATE 路径时才启用 worker 自报。`source=worker` 写入永远不是验收
证据；验收记录由 coordinator 拥有。

## 流程写入责任

coordinator 拥有验证状态。每个动作完成后，在处理该动作的正常 turn 内，使用稳定 task/dispatch/attempt
标识和证据引用写入。禁止从聊天或终端正文脆弱解析。嵌套 implementer 显式结构化上报给顶层 worker，
由顶层 worker 转交 task ID、dispatch ID、attempt 和 parent worker ID；先登记父级。
嵌套自报在 coordinator 验证前仍是自报。

| 正常流程节点 | 责任人与记录 |
| --- | --- |
| Run 开始 | coordinator：init REAL，目标仓库、阶段、状态；task 标题、依赖、已知阻塞 |
| dispatch 回执 | coordinator：worker 的 task/dispatch/attempt/worktree/父级，dispatched、启动 unknown/unobserved；event 记录派发证据 |
| 启动确认 | coordinator：仅依据真实证据更新 start_confirmation、验证 lifecycle、带证据 event；有依据才 effective，ready 不是 observed start |
| worker 问题／等用户 | 顶层 worker 转交结构化问题摘要；coordinator：event、task waiting_user、pending_decision 和验证 worker lifecycle；仅全局等待才设 Run waiting_user |
| 用户答复 | coordinator：去敏决策 event，答复仍走原渠道；pending_decision 清为空串，再更新 task/阻塞/Run |
| heartbeat／完成报告 | worker source 写自报字段/event；coordinator 记录收件证据，可设 task reported；不推断验收或交付，heartbeat 不是有效进展 |
| G-impl 验收／打回 | coordinator：带证据 event，task accepted 或 rejected 与原因；检查通过才 implemented；打回后需复验 |
| 集成／开 PR | coordinator：真实集成证据后独立设置 integrated；artifact pr 记录真实引用，不推断 merge |
| G-merge／合并 | coordinator：记录门禁 event，真实合并证据后才 merged；保留单向门人工决策规则 |
| tracker 对账 | coordinator：检查实际 tracker 状态后才 tracker_closed；无 tracker 则保留未确认并 event 说明 |
| 失败／失联／恢复 | coordinator：失败、阻塞原因与验证 failed/lost lifecycle；重试用新 worker ID/dispatch/attempt；heartbeat 不能抹掉失败 |
| 暂停／取消／收尾 | coordinator：end 记录真实状态、摘要，artifact 报告/测试/commit/PR；验证后才设 reclaimable/closed，未交付部分保持未交付 |

看板不可用时原有派发、启动、问答、验收、合并、生命周期流程全部正常。缺少记录或服务故障均不授权
改变这些要求。

## 恢复与边界

工具完全不调用 Orca，无 worker-list 契约依赖，也无新增接收器。只知道显式提交的事实，不能发现
未上报的嵌套 worker、merge、tracker closure 或离线 worker。页面分别显示最后观察和最后有效进展；
过期仅指观察年龄，不是失败证明。查询失败保留最后有效快照，明显显示错误与其载入时间，不以空成功替代。

写入先 fsync 文件、原子替换再 fsync 目录。替换前崩溃留下旧快照，可能残留被忽略的 `.progress-*`
临时文件；替换后崩溃留下新快照，稳定事件 ID 保证安全重试。获取锁最多等待两秒
（`--lock-timeout`，可配置的默认值），冲突则警告返回，避免无限阻塞 coordinator；使用 `--json`
时这是 `degraded/lock_busy` 且退出 `0`。稍后用相同事件 ID 重试。进程退出释放锁。不要删除活跃锁文件
或自动修复。损坏/未知版本快照报错且不覆盖：先保存证据，将已知正确审计备份恢复到另一个仓库外目录，
再显式对账后续事实。不迁移、不截断、不猜测重建，备份也必须在仓库外。事件历史持续增长，是本地审计
记录，不是数据库或保留服务；完成的 Run 目录按本地策略归档。

服务仅绑定 IPv4 localhost，无写 API，不提供任意路径或文件正文，不加载外部资产。文本全部安全渲染，
仅 HTTP(S) 引用可点击；文件路径、commit ID 等仅为文本。默认也没有原始 inbox/终端/消息视图。
可信本地写入者和本地浏览器可访问，它不是有认证的多用户服务；不要转发端口或公开审计目录。
