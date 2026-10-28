# 可选进度记录与 localhost 看板

**何时读取**：用户需要观察 Run、task、worker 的进展、本地看板或可复现离线演示时。
按需加载，不是派发前置条件。

目录：[使用](#使用)、[可视化看板](#阅读可视化看板)、[协议 v1](#协议-v1)、[流程写入责任](#流程写入责任)、[恢复与边界](#恢复与边界)。

## 使用

这是可选旁路。coordinator 在正常流程动作完成后写入可观察事实。记录工具和页面均不调度、
验收或查询 Orca，不执行 `check`、`ack` 或 inbox 命令；现有唯一 inbox 接收器保持权威。
写入不依赖网页服务存活。进度命令失败时保留警告并继续编排，不能把看板错误变成派发失败。

在安装包位置运行，将 `PROGRESS` 指向其 `scripts/progress.py`。运行依赖仅 POSIX 上的 Python 3
标准库。`AUDIT_ROOT` 指向**所有仓库之外**已有持久审计位置，每个 Run 使用新的子目录。
禁止仓库内部、仓库根目录、目标仓库祖先目录或符号链接路径。输入支持 `--data JSON` 或
`--data -` 从 stdin 读取。仅接受摘要白名单字段，不接受环境、密钥、原始消息、终端正文或任意
metadata。允许字段中的摘要也必须人工去敏；这是字段白名单，不是秘密检测器。证据和 artifact
只存引用，不读取文件正文。

```bash
PROGRESS=skills/orchestra-auto-zh/scripts/progress.py
STATE="$AUDIT_ROOT/run-example"
python3 "$PROGRESS" --state "$STATE" init --event-id init-1 --effective --data \
  "{\"id\":\"run-example\",\"target_repo\":\"$PWD\",\"mode\":\"REAL\",\"phase\":\"implementation\",\"status\":\"active\"}" \
  || printf '%s\n' 'Progress warning; continue orchestration' >&2
python3 "$PROGRESS" --state "$STATE" serve --port 8765
```

打开 **http://127.0.0.1:8765/**，每三秒自动刷新；在另一终端执行记录命令，服务关闭也可写入。
Ctrl-C 只停止查看，快照保留。`show` 不启动服务即可输出快照；`serve --port 0` 自动选择并打印地址。
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

可选浏览器回归使用仓库外的 Playwright 与 Chromium，不是运行依赖。设置 `PLAYWRIGHT_MODULE` 指向
该外部包，然后针对独立 DEMO 服务执行
`node tests/progress_dashboard_browser.cjs http://127.0.0.1:8766/ EVIDENCE_DIR`，覆盖四种视口、
事实边界、刷新状态保留与安全渲染。

## 协议 v1

`progress.json` 是 UTF-8 快照，包含 `schema_version: 1`、`run`、按 ID 索引的 `tasks`、`workers`、
`deliveries`、`artifacts`、有序 `events` 和私有幂等 `receipts`。每次成功写入在进程锁下原子替换
整个快照；HTTP/show 不暴露 receipts。未知版本报错且不改文件。可备份此单文件作为审计；
工具不写目标仓库。后续快照演进必须定义新的文档化版本。

统一写接口：

```text
python3 PROGRESS --state STATE COMMAND --event-id STABLE_ID --source coordinator|worker [--effective] --data JSON
```

相同事件 ID 只能重试完全相同的 command、source、JSON、effective；一致重试无副作用，时间戳
也不更新，冲突重试失败。新观察使用新 ID。source 默认 coordinator，是可信本地写入者声明的来源，
不是身份认证。只有 coordinator 观察到有效进展后才能加 `--effective`。`last_observed` 随记录的
活动更新，`last_effective_progress` 仅由显式验证更新，不估算完成百分比或剩余时间。

| 命令 | JSON 摘要字段（其他键拒绝） |
| --- | --- |
| `init` | 必填 `id`、`target_repo`（已有目录）、`mode`（REAL/DEMO）；可选 `title`、`phase`、`status` |
| `run` | 更新 `phase`、`status`、`title` |
| `task` | 必填 `id`；可选 `title`、`dependencies`（ID 列表）、`status`、`blocked_reason`、`pending_decision` |
| `worker` | 必填 `id`；首次登记还需已有 `task_id`、`dispatch_id`、正整数 `attempt`；可选 `parent_worker_id`（先登记父级）、`worktree`、`start_confirmation`、`lifecycle`、`reported_lifecycle`、`reported_summary` |
| `event` | 必填 `type`、`summary`；可选已有 `task_id`、`worker_id`、`dispatch_id`、`attempt`、`evidence`（引用列表） |
| `delivery` | 必填已有 `task_id`；独立布尔字段 `implemented`、`integrated`、`merged`、`tracker_closed`；可选 `evidence` |
| `artifact` | 必填 `id`、`kind`（report/test/commit/pr/other）、`label`、`reference`；可选已有 `task_id` |
| `end` | 必填 `status`（completed/failed/paused/cancelled）；可选 `phase`、`summary`；记录 `ended_at` |

Run status：`unknown`、`active`、`waiting_user`、`failed`、`paused`、`completed`、`cancelled`。
Task status：`unknown`、`pending`、`ready`、`running`、`blocked`、`waiting_user`、`reported`、
`accepted`、`rejected`、`failed`、`paused`、`cancelled`。
Worker lifecycle：`unknown`、`dispatched`、`running`、`waiting_user`、`reported_done`、`failed`、
`lost`、`reclaimable`、`closed`。start_confirmation：`unknown`、`observed`、`unobserved`、`unsupported`。
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

以下包装函数在 `set -e` 下也始终成功，警告可见，正常编排继续：

```bash
progress_record() {
  python3 "$PROGRESS" --state "$STATE" "$@" || {
    printf '%s\n' 'Progress warning; continue orchestration' >&2
    return 0
  }
}
progress_record task --event-id task-1 --data '{"id":"t1","title":"Protocol","dependencies":[],"status":"ready"}'
progress_record worker --event-id dispatch-1 --data '{"id":"w1-a1","task_id":"t1","dispatch_id":"d1","attempt":1,"worktree":"protocol-worktree","lifecycle":"dispatched","start_confirmation":"unobserved"}'
progress_record worker --event-id start-1 --effective --data '{"id":"w1-a1","lifecycle":"running","start_confirmation":"observed"}'
progress_record worker --event-id report-1 --source worker --data '{"id":"w1-a1","task_id":"t1","dispatch_id":"d1","attempt":1,"reported_lifecycle":"reported_done","reported_summary":"Implementation ready for review"}'
progress_record delivery --event-id accept-1 --effective --data '{"task_id":"t1","implemented":true,"evidence":["acceptance-report.txt"]}'
progress_record end --event-id end-1 --data '{"status":"paused","phase":"waiting","summary":"Awaiting integration"}'
```

## 流程写入责任

coordinator 拥有验证状态。每个动作完成后，在处理该动作的正常 turn 内，使用稳定 task/dispatch/attempt
标识和证据引用写入。禁止从聊天或终端正文脆弱解析。嵌套 implementer 显式结构化上报给顶层 worker，
由顶层 worker 转交 task ID、dispatch ID、attempt 和 parent worker ID；先登记父级。
嵌套自报在 coordinator 验证前仍是自报。

| 正常流程节点 | 责任人与可选记录 |
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

无看板时原有派发、启动、问答、验收、合并、生命周期流程全部正常。缺少记录或服务故障均不授权
改变这些要求。

## 恢复与边界

工具完全不调用 Orca，无 worker-list 契约依赖，也无新增接收器。只知道显式提交的事实，不能发现
未上报的嵌套 worker、merge、tracker closure 或离线 worker。页面分别显示最后观察和最后有效进展；
过期仅指观察年龄，不是失败证明。查询失败保留最后有效快照，明显显示错误与其载入时间，不以空成功替代。

写入先 fsync 文件、原子替换再 fsync 目录。替换前崩溃留下旧快照，可能残留被忽略的 `.progress-*`
临时文件；替换后崩溃留下新快照，稳定事件 ID 保证安全重试。获取锁最多等待两秒，冲突则警告返回，避免无限阻塞 coordinator；稍后用相同事件 ID 重试。进程退出释放锁。不要删除活跃锁文件
或自动修复。损坏/未知版本快照报错且不覆盖：先保存证据，将已知正确审计备份恢复到另一个仓库外目录，
再显式对账后续事实。不迁移、不截断、不猜测重建，备份也必须在仓库外。事件历史持续增长，是本地审计
记录，不是数据库或保留服务；完成的 Run 目录按本地策略归档。

服务仅绑定 IPv4 localhost，无写 API，不提供任意路径或文件正文，不加载外部资产。文本全部安全渲染，
仅 HTTP(S) 引用可点击；文件路径、commit ID 等仅为文本。默认也没有原始 inbox/终端/消息视图。
可信本地写入者和本地浏览器可访问，它不是有认证的多用户服务；不要转发端口或公开审计目录。
