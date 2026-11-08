# 链条位置、显式流程、续跑与出口检查

**什么时候读：** 同一次 Run 的两轮之间、崩溃或应用重启之后，或者 coordinator 需要从记录而不是从对话里回答「这次 Run 接下来走到哪」的时候。

## 目录

- [显式流程是计划，账本只是索引](#显式流程是计划账本只是索引)
- [位置，而不是步骤](#位置而不是步骤)
- [续跑：先看流程，再看旧链](#续跑先看流程再看旧链)
- [出口检查：产物形状的地板](#出口检查产物形状的地板)
- [什么时候停下来问人](#什么时候停下来问人)
- [续跑不做什么](#续跑不做什么)

## 显式流程是计划，账本只是索引

一次 Run 不一定走满五个阶段：它可能只要设计、只有一张定案的票、是一次 wayfinder 建图、
复用已有 spec，或是一个直接的小修复。账本记录的是观察到的事实，说不清这次 Run 选了哪些阶段、
哪个门禁在哪、授权交付终点是什么。**显式流程记录才是那份计划**，`resume` 优先读它。

`scripts/flow.py` 负责写入与读取。文件在仓库之外，每个 Run 一份：

```bash
python3 scripts/flow.py append --run-id <run_id> --record flow.json       # 计划，只写一次
python3 scripts/flow.py append --run-id <run_id> --record state.json      # 某个节点变了
python3 scripts/flow.py append --run-id <run_id> --record invalidate.json # 撤销并传播下游
python3 scripts/flow.py append --run-id <run_id> --record import.json     # 前次 Run 的产物
python3 scripts/flow.py plan   --run-id <run_id>
```

默认位置是 `$ORCA_FLOW_HOME/runs/<run_id>/flow.jsonl`，否则 `~/.orca/flows`；`--flow` 与
`--flow-home` 可覆盖。`chain.py resume` 会在它被指定读取的账本旁找 `flow.jsonl`，所以一个指针就够。

**schema v1。** 每条记录带 `schema_version: 1`、`record_id`（幂等）、`kind` 与 `flow_id`。
未知版本直接拒绝，不做推断。四种记录：

| kind | 说什么 |
| --- | --- |
| `flow` | 计划：`run_id`、`goal`、`endpoint`，以及带 `id`、`gate`、`depends_on`、`applies` 的 `nodes` |
| `state` | 某节点的状态：`pending`、`in_progress`、`waiting_user`、`blocked`、`accepted`、`not_applicable`、`invalidated` |
| `invalidate` | `revocation`、`spec-change` 或 `artifact-update`，让指定节点及其全部下游失效 |
| `import` | 外部或前次 Run 的产物，带 `source_run`、`artifact`、`artifact_version` 与 `verification` |

`accepted` 绑定 `task_id`、正整数 `attempt`、`artifact_version` 与 `evidence_ref`：没有证据的主张不能验收。
`not_applicable` 必须带使其不适用的 `entry_evidence`；`waiting_user` 不是失败。
计划中 `applies` 为 `false` 的节点，其证据同样写在计划里。

文件只追加，且**顺序就是单个节点的权威**：后来的状态覆盖先前的，所以当前的失败不会被更早的成功掩盖。
attempt 比该节点已经见过的更旧的验收，不会覆盖更新的状态，而是记为证据缺口；`task_id` 与该节点已有绑定
不一致的验收，记为 `divergent`。

旧账本保留原义，绝不改写；`flow.py` 也不读它、不转换它。

wayfinder 的 Run 中，流程只把 **chart**、**work** 与 **map-complete** 记为带证据指针的节点；
地图、票、阻塞与 frontier 仍以 tracker 为权威。本地流程不复述决定，也不成为地图。

## 位置，而不是步骤

旧链条在分叉前是线性的，所以按五个**位置**建模。`implement` 和 `implement-spec` 是同一个位置，
因为两者关在同一个门禁上：一条验收记录说不清跑的是哪一个，靠猜的续跑会把链条换到错的分支上。

| 位置 | 门禁 | 步骤 | 关掉它的产物 |
| --- | --- | --- | --- |
| `grill` | G-docs | `grill-with-docs` | 说清的改动、术语表条目和相应的 ADR |
| `spec` | G-spec | `to-spec` | 已发到 tracker 的 spec |
| `tickets` | G-tickets | `to-tickets` | 带阻塞关系的票图 |
| `build` | G-impl | `implement` **或** `implement-spec` | 一张定案的票，或整份 spec 的 integration 分支 |
| `review` | G-merge | `code-review`，然后合并 | 双轴 review 与合并 |

门禁是权威，位置只是门禁上方的一个标签。`chain.py steps` 把这张表以 JSON 打印出来，
coordinator 不需要每次从散文里重新推导。

## 续跑：先看流程，再看旧链

```bash
python3 scripts/chain.py resume --run-id <run_id>                  # 账本旁的 flow，否则走旧链
python3 scripts/chain.py resume --run-id <run_id> --flow /path/flow.jsonl
python3 scripts/chain.py resume --run-id <run_id> --ledger /path/to/ledger.jsonl  # 旧账本
```

**有 flow 记录时**，答案来自流程，不再读账本：
`{"state": "continue", "ready": [{"node": "spec", "gate": "G-spec"}], ...}`、
`{"state": "complete", "ready": [], ...}` 或
`{"state": "uncertain", "ready": [], "evidence_gaps": [...]}`（`uncertain` 退出码 `1`）。
`waiting_user`、`blocked`、`invalidated` 分别列出，等人处理的节点不会被读成失败的节点。
`complete` 只覆盖授权终点，**不是**合并授权。

**没有 flow 记录时**，`resume` 仍按原样从账本回答，并在答案里带一个 `legacy` 块：

```json
{"state": "continue", "landed": ["grill", "spec"], "next": {"position": "tickets", "gate": "G-tickets"},
 "legacy": {"state": "legacy", "reason": "...无法证明当前阶段、门禁与交付终点..."}}
```

`legacy` 不是通行证。它说明脚本不知道这次 Run 的实际入口、门禁与终点，给出的只是五位置链的建议，
coordinator 必须先对照模式声明与真实交付终点再行动。

两种模式下 `problems` 都记录读的时候跳过了什么：末尾半行被丢弃，损坏的完整行按行号报告。
悄悄丢一条记录的续跑会声称错误的位置，比拒绝回答更糟。任何无效记录都会让答案为 `uncertain` 并清空就绪集合。

答案是一个位置或节点，不是一次派活。怎么继续——通道、agent、worktree、spec——仍然由 §4 决定。

## 出口检查：产物形状的地板

```bash
python3 scripts/chain.py exit-check --step to-spec   --artifact /path/to/spec.md
python3 scripts/chain.py exit-check --step to-tickets --artifact /path/to/tickets.md
python3 scripts/chain.py exit-check --step to-tickets --artifact 01-a.md --artifact 02-b.md
```

只有两个步骤的产物形状值得机械检查，因为下一格会忠实地实现它拿到的任何东西。

**`to-spec`** 分两块报告：`native`（有验收小节，且至少一条可验收条目）与 `dispatch`
（本项目的 `Test tier:` 行，narrow 或 wide，附理由）。被下一个标题切断的小节按空处理。
通过 `native` 但缺本项目附加项的 spec，是「上游有效、但还不能进入本项目实施」，
两者不合并成一个判决，也都不评判需求本身是否正确。

**`to-tickets`** 按真实票边界检查每份产物；形状自动识别，显式 `--source` 是兼容模式：

- `list` —— 本项目的合并格式：每个 `## <票名>` 块带验收行（`验收：`、`Acceptance:`、
  `Done when:`、`完成标准`）或勾选任务行。普通章节标题（`## What to build`、
  `## Acceptance criteria`、`## Problem Statement` 等）不是票。
- `file` —— 一张本地票：第一个 `# <标题>` 是这张票，验收小节或上游本地模板末尾的 `- [ ]`
  复选框是它的验收标准。链接了不存在的本地文件，会记在这张票名下。
- `issue` —— 一条 issue 正文：验收*小节*是标准，它的 `##` 章节绝不读成多张票。检查前先取完整正文；
  网络不可达不等于空票。
- `--artifact` 重复即集合：每个文件是一张票，每条错误都点名文件和票。
- 未知阻塞、自阻塞与阻塞环分别报告，落在携带该边的票上。
- 文本同时符合多种形状时（地图标题加一个票样的 `##` 标题），退出码 `2` 并要求 `--source`，不猜。

`--source list` 仍是不带票名的合并列表的兼容模式；形状无歧义时，不带 `--source` 的旧调用照常可用。

其余步骤会返回一句拒绝，并点名真正的信号（测试运行、review、PR 状态）——假装检查测试运行，
比直说不查更糟。那些事实属于 `acceptance.py`（`verify`）和 `closeout.py`（`reconcile`）。

**这是地板，不是判决。** 一份 spec 可以带着 `Test tier:` 行却写错方向。exit-check 只能抓住缺失的小节，
抓不住错误的决定；门禁仍然是 coordinator 的，检查通过不是跳过双轴 review 的许可。

## 什么时候停下来问人

下面这些情况结束这一轮，而不是开启下一轮。前两条是机械的；其余是 §5 的决策类规则，集中在这里，
免得一次续跑把 coordinator 劝过它们。

| 情况 | 停在哪 | 谁决定 |
| --- | --- | --- |
| 同一张票的同一个门禁连续失败两次 | 这张票的这一轮 | coordinator 打回，或触发 per-ticket 熔断 |
| 整个 Run 连续两个门禁失败 | 整个 Run | coordinator 报告，人决定要不要继续 |
| 单向 Merge Danger 门 | 合并 | 人来合 |
| 凭证、第三方账号、付费、人工审批、不可逆的 live 数据 | 这一步 | 人 |
| 交付形态、方案分叉、范围变了 | 这一步 | 人 |
| `resume` 报告流程或链条 `complete` | 无——Run 已结束 | coordinator 写 closeout |

## 续跑不做什么

- **revert 之后它不会把节点改回未落地。** revert 记录为一条 `cause: revocation` 的 `invalidate`；
  没有这条记录，节点保持 accepted。流程只追加，撤销是新记录，不是改写历史。
- **它不会把旧账本转换成流程。** 旧 Run 继续得到 legacy 答案；为它写的第一条 flow 记录是 coordinator
  显式写下的新计划，绝不推断。
- **它不读 Orca、git 或 PR host。** 它判断不了一个 worker 是不是死了；那是 `run-health.md` 和 `worker-show` 的事。
- **它不选下一个动作。** 节点不是派活：通道、agent、worktree 名和 spec 仍然归 §4。
