# 门禁验收、合并执行与 worker 生命周期

**什么时候读**：票据收尾、要按票型选门禁并执行验收时；以及合并项目 PR、结算 worker 生命周期时。

## 6. 门禁验收

coordinator 自行核验可观察结果与证据，失败就列明缺项打回。UI / 运行时改动要在隔离数据
与隔离环境中按仓库浏览器纪律实际操作，不只凭测试声称通过。结论和关键数字写入 §7 的
持久报告；过程日志可放临时证据目录，报告不能依赖临时目录长期存在。

### 按票型选门禁

| 票型 | 门禁 | 验收重点 |
| --- | --- | --- |
| 设计 / wayfinder | G-docs / G-wayfinder | 决策、术语、frontier 与文档结构 |
| 研究 | 来源与结论核验 | 来源可追溯、结论有证据、局限与交付终点明确 |
| 规格 | G-spec | 用户故事、seam、范围与 tracker 状态 |
| 拆票 | G-tickets | 垂直切片、依赖、每票可验收性 |
| 实施 | G-impl | 改动行为、测试、质量、交付物 |
| 项目改动的自动合并 | G-merge | PR、CI、组合结果与合并安全 |

文档/规划票只跑适用门禁；没有源码行为改动时，不强制跑源码测试或双轴源码 review。
完整文档门禁见 [规划门禁](gates-docs-planning.md)，只在相应票收尾时读对应小节。
派活前按预期 Target / Change 定票型，完成后用实际 diff 复核；越界时按 §5 处理范围问题。

### G-impl — 实施票

0. **现场**：读 `git status --porcelain`、改动文件列表和 `git diff --numstat`。
   未提交内容必须解释；异常大的 diff 先保护真实工作，再按
   [格式化恢复](reformat-noise-recovery.md)调查，不能直接还原。
1. **行为与测试**：按影响面运行仓库现有测试。单票且影响面可圈定时跑受影响测试；
   共享接口、构建配置、类型或影响面不明时跑全量。同一份代码不在本地重复跑全量；
   最终 head 以 CI 或一次本地全量为准。修复类测试应在修复前失败、修复后通过；
   新行为留可观察的红绿证据。纯文案、样式或配置改动用实际渲染、检查或适用测试证明结果。
   UI 票的证据要落在产品渲染出的文字、元素、无障碍名或真实浏览器行为上。
   核对 spec 的 `测试档：` 行与实跑一致：窄档的选择器、结果与被引用的理由对得上；实跑比声明
   更窄（只挑了一条）或每轮都跑全量（耗时且未说明）都打回，改判由 coordinator 重写该行。
   窄档管的是迭代期的验证范围，不豁免上一条的「最终 head 以 CI 或一次本地全量为准」。
2. **质量**：运行项目适用的静态检查、lint 与格式门禁，以仓库实际配置为准——例如 TS 的
   typecheck、Python 的 mypy/ruff、Go 的 `go vet`、Rust 的 clippy/fmt，都只是按语言举例，
   不是必跑清单。仓库没有某一类门禁时就不虚构一项，在报告里写明「该项目无此门禁」，
   不要因为缺工具而报一例假红。若仓库有既存问题，在该票 base commit 上取得可比基线，
   报告新增问题数；不能用另一分支的日志充当本票基线。
3. **review 与覆盖**：Answer 给出前置 skill 的实际执行证据。需要双轴 code-review 时，
   分别给 Standards 与 Spec 结论、findings 及处理结果；§3 定义的小实施票可按 spec 中
   已记录的理由省略。字段、prop 或参数换来源时清点所有相关调用点，逐个说明改动或保留原因。
   同票内同类操作保持一致，差异要有可检验的理由。
4. **任务状态**：任务状态与 Answer 对应真实完成程度；`worker_done` 回执只证明生命周期结算，
   不能代替产物验收。消息丢失时核对 Dispatch 与实际产物，不重复派编辑者。
5. **交付物**：项目文件有改动时，核对 worktree 干净、提交存在且分支已推送；
   核对 OPEN PR 的 head 与本票最终提交一致，正文准确写明 Summary、Evidence、
   Merge Danger 与 `Closes`；worker 使用 `pr` skill 或仓库模板，以 `--body-file` 创建或更新。
   长程票图的交付物是一个集成 PR：核对每张票的分支都已合入，抽查至少两张票的证据；
   逐票 TDD 加最终 integration branch 上的一次 `code-review` 取代逐票双轴 review，
   并把该选择记入报告。找不到 PR 或正文缺证据，打回 worker 补齐；coordinator 不代写。

### G-merge — 项目 PR 的门感知合并门禁

0. **门检查**：读 PR 正文的 `Door` 与 `Blast Radius`，再用真实 diff 核对声明。
   **本节是单向门的唯一权威定义**，其他文档只指向这里，不重复一份。
   单向门触发项：schema/数据迁移或删除；破坏性的公共 API/CLI/SDK 变更（删除、重命名，
   或修改既有签名/语义/默认值）或持久化格式变更——纯增量、向后兼容的新增本身不算单向门；
   认证/权限/密钥；计费/定价；部署/基础设施；不可逆外部副作用；移除功能；主版本依赖升级；
   影响面很宽且无简单回滚。任意一项成立，整个 PR 就是单向门；拿不准按单向门处理。
   正文写 two-way 但 diff 有触发项时，打回 worker 修正正文，再按单向门处理。
   单向门到此为止：保留开放 PR，报告人类手动合并的确切动作。绝不自动合并单向门。

先看仓库对 CI 和合并的约定。区分已知基线故障与本票引入的真实失败。多 PR 由集成 worker
在隔离 worktree 按拟采用的合并方式与 DAG 顺序组合最终 head，记录 base、各 head SHA 与
每一步的 tree SHA；coordinator 核对组合结果，并在最终 tree 上跑适用的全量门禁——默认走
CI；仓库没有可用 CI 时，才在本地跑一次等价的全量门禁作为替代。
单 PR 在最终 head 上完成同等验证。组合冲突或真红时先修 PR；任一 head 或目标 base 变化后
重新判断原证据是否仍覆盖拟合并内容。双向门且门禁全绿即进入 §6.5 自动合并，
无需另请用户确认；用户明确要求保留开放 PR 时停止于此。

### §6.5 合并执行与收尾

1. 按 DAG 或 stacked PR 顺序，核对当前 head 与已验收 SHA；使用仓库允许的合并方式逐张合并。
   合并冲突交回该票 worker 在其 worktree 解决，重新过受影响门禁与 G-merge。
2. 每张合并后执行 §6.5.1，并核对主分支 tree SHA 与已验证的对应前缀一致。只有出现计划外的
   主分支提交、不同的合并结果或待合并 head 变化时，才暂停并更新集成证据。
3. 全部合并后核对最终主分支包含预期提交，做快速检查并记录 SHA。异常时保留现场、派修复票，
   不把已合并状态写成验收通过。
4. **合并后按 tracker 对账。** 合并不等于票已关闭：tracker 按自己的规则关闭（通常靠合并时
   `Closes` 链接生效，有时只能人工关）。把票状态读回来，逐票记录是否已关闭，未关闭的写明
   在等什么。因为 PR 合并了、或因为 PR 还开着就声称票已关闭，正是这一步要抓的假收口；
   长程票图的报告里，票表把四个状态——已实施、已集成、已合并、tracker 已关闭——分开列。
5. 到本轮工作范围对应的阶段即收口；设计 PR 合并不自动授权创建实施票。单向门不进入此合并
   循环：由人类手动合并，票在那次合并前保持开启，之后的 turn 再记录 merge SHA、同步 main
   并对账票据。

#### §6.5.1 每张 PR 合并后同步本地主分支

记录 PR 的 MERGED 状态、base 和 merge SHA。只在已确认的主工作区位于目标主分支、
工作区与 index 干净、远端与 upstream 明确时执行 `git pull --ff-only`。再用
`git merge-base --is-ancestor <merge_sha> HEAD` 核对，记录本地 HEAD。
条件不满足时保留现场，报告“远端已合并，本地同步阻塞”，暂停后续合并；不自动 stash、
reset、checkout 或创建本地 merge commit。网络错误查明后可重试一次。

### §6.6 验收记录与四级状态

门禁结论要么持久落盘，要么等于没发生。在 release worker 或告诉任何人门禁通过之前，先把它记成
台账 `kind: acceptance` 记录；写入失败就报告为写入失败，绝不当作通过，并用同一个 `record_id`
重试。

```bash
ACCEPTANCE="$SKILLS_ROOT/orchestra-auto/scripts/acceptance.py"
python3 "$ACCEPTANCE" record --run-id "$RUN" --record "$ACCEPTANCE_JSON"
python3 "$ACCEPTANCE" show --run-id "$RUN" --ticket 39
```

每个 PR 一条记录，携带 `ticket`、`gate`、`attempt`、`accepted_sha`、`accepted_at`、
`pr {number, head_at_acceptance, base}`、`checks [{name, command, result, evidence_ref}]`、
`review {axis[], result, evidence_ref}`、四个 `levels`、`decided_by`（`coordinator` 或
`human`）、`decided_at` 与 `answer_location`。`validate(record)` 拒绝任何为 `true` 却没有
`evidence_ref` 的级别；CLI 先校验再写入，拒绝时以非零退出并输出错误清单。加
`--authority FILE` 时会再用记录的 PR host/tracker/git 读取结果交叉核对已声明的级别。

**最低证据**：缺少证据的声明是打回，不是细节。

| 声明 | 证据 |
| --- | --- |
| 合并 | PR host 的 `MERGED`、merge SHA，以及 `git merge-base --is-ancestor <accepted_sha> <merge_sha>` |
| tracker 关闭 | tracker 读回为 `closed`，并带读回时间 |
| 启动 | `turnStart: observed`，或该 attempt 的产物 / 状态投影引用 |
| 测试 | 命令、结果与日志引用。`grep` 只证明字符串存在，绝不等于跑过测试 |
| review | 两个轴（`standards`、`spec`）与 review 引用；豁免要在 spec **和** Answer 里都写明理由 |

`worker_done` 只证明结算。它既不是启动证据，也绝不产生验收：凭它把某级置为 `true` 会被拒绝。

**四级状态**：`implemented`、`integrated`、`merged`、`tracker_closed` 是四个独立的声明，按 PR
分别记录。票级级别只有在**每个**关联 PR 记录都支持时才为真——一张票可以合理地跨多个 PR，任何一个
PR 弱就拉低票级，不能被平均掉。同一 PR 的两条记录互相矛盾时标为 `divergent`，该 PR 不声明任何
级别。`never_merge` 是 merged 级的终态，归人类决定：`{"value": false, "terminal":
"never_merge", "decided_by": "human"}` 并附理由，spike 才不会再显得永远未完成。revert 或
reopen **追加** observation（`acceptance.py observe`），把受影响级别置 false，并把原证据保留为
历史；任何东西都不被覆盖。

**pending 而非冻结**：issue #20 里关于四级的问题仍未定。把它们记为台账 `pending` 项
（`acceptance.py open-questions`）并附证据，受影响语义保持 `provisional`，票据无需等人类回复即可
收尾。后来的人类决定追加 observation（`resolve-question`），绝不改写已有记录。

### §6.7 收尾：清单、单向门 pending 与 release 前置

收尾是对账，不是凭记忆。`scripts/closeout.py` 从权威推导：`plan(facts)` 是对权威事实的纯
规划函数，`reconcile` 通过适配器读权威并持久化一条 `kind: closeout` 台账记录。存储用
`ledger.py`，验收 schema 用 `acceptance.py`，release 判定消费 `health.authorize_release`——
三者都不在这里重写。

```bash
CLOSEOUT="$SKILLS_ROOT/orchestra-auto/scripts/closeout.py"
python3 "$CLOSEOUT" plan --run-id "$RUN" --facts facts.json
python3 "$CLOSEOUT" reconcile --run-id "$RUN" --authority recorded.json \
  --answer-location "$ANSWER"
python3 "$CLOSEOUT" show --run-id "$RUN"
```

**十行清单，每行标注权威、可重复性与动作。**

| # | 事实 | 权威 | 可重复 | 动作 |
| --- | --- | --- | --- | --- |
| 1 | 每个 PR 的 accepted SHA | 台账 acceptance 记录 | **否** | 必须在验收时持久化；事后无法重建 |
| 2 | PR 的 `MERGED`、base 与 merge SHA | PR host API | 是 | 读回并写 observation |
| 3 | merge 覆盖 accepted head | `git merge-base --is-ancestor` | 是 | 失败 → divergent / 上报 |
| 4 | main 含该 merge | `git rev-parse` / ancestry | 是 | 记录 ancestry 结果 |
| 5 | 本地 main 已同步 | `git status` / `rev-parse` / upstream，ff-only | 是 | 阻塞 → 暂停后续合并 |
| 6 | 每张票已集成 | 集成分支 ancestry / PR `Closes` | 是 | 逐票记录结果 |
| 7 | tracker 状态 | tracker 读回 | 是 | 记录关闭或重开 |
| 8 | worker 生命周期 | `worker-show` / `worker-list` / release 回执 | 是 | 枚举 reclaimable |
| 9 | dashboard 投影 | `progress.json` | 是（非权威） | 只记 gap，绝不当证据 |
| 10 | 单向门 pending | 台账 `pending` | 是 | 到期后再次检查 |

只有第 1 行不可重复，规划结果用 `non_repeatable` 标出。读不到的权威是 `unknown`，绝不静默
当作通过；读不到的权威与失败检查一样阻断宣告。本地 main 未同步时第 5 行**阻断后续合并**
（`merge_blocked`），而不只是拉低清单。第 8 行枚举所有 reclaimable 或 `release_unknown`
的 worker，并列出需要先用 `worker-show` 读回的那些。第 9 行只记 gap：`progress.json` 是投影，
看板故障或投影陈旧既不会阻断收尾，也绝不会被当作证据引用。

**收尾是某一时点的对账。** 十行合起来是一个快照；与上一次快照不同，本身不是需要修复的失败。

**单向门 pending 无需 coordinator turn 即可见。** 需要人类合并时记录一条台账 `pending` 项
（`pending_type: human_merge`、`pr`、`accepted_sha`、`owner`、`condition`、`check_after`）。
无需任何 turn 就能看到它：`ledger.py show --run-id "$RUN" --kind pending` 与 `ledger.py
summary` 都会列出，看板显示“等待人类合并”。下一次 turn 再对账 PR host、merge SHA、main 同步
与 tracker；四项全为真时 `reconcile` 才追加一条解决 observation，并停止阻断。检查仍为假的
pending 项保持可见，并带着失败的那一项检查。第 10 行只对**单向门** pending 项设门：其他
pending 项（例如四级状态的开放问题）仍在台账可见，但不阻断宣告，因为规格允许它们在无人类回复时
收尾。

**release 前置。** release 需要已结算的 Dispatch——不是 `ready`，也不是健康状态——以及持久化的
收尾记录：成功时至少要有验收记录与 dispatch → task → attempt → worktree 映射，失败时至少要有失败
分类及其证据引用。`release_unknown` 先读 `worker-show` 与 `worker-list`；无论健康状态如何，绝
不 release 未结算的 worker。写不成的收尾记录**不**算通过：release 与门禁宣告都等写入修复，
未持久的记录留在 `closeout-pending/` 以便重试。

**崩溃恢复。** 顺序是 Orca → 台账 → 快照；投影只是线索，不是权威。合并与 tracker 对账之间
崩溃时，用第 2、3、4、7 行修复：PR host 仍报告 `MERGED` 与 merge SHA，git 仍能证明 ancestry
与包含关系，tracker 自己的读回说明票是否关闭。缺失的事实以失败收尾记录下来，带分类与证据引用，
而不是凭记忆补上。

**revert 与 reopen 消费 T09 的 observation。** revert 或 reopen 只追加一次 observation
（`acceptance.py observe`）；收尾消费它并拉低第 2/3/4 行（revert）或第 7 行（reopen）。收尾绝不
为同一事实再追加第二条 observation。

**tracker 关闭要读回。** 合并关键词关票时，收尾读回状态并记录。没有关闭时，coordinator 只在既有
授权内关闭；例外上报，而不是把自己变成不受限的 tracker 写者。

**记录带 Answer 位置。** 每条收尾记录都存 `answer_location`（第 1 行还存每条验收记录自己的位置），
后续 attempt 直接找到 Answer，而不是重新推导。

**release 重放安全按版本范围记录。** 当前 CLI 帮助之外的 release 重放语义仍是假设。收尾记录验证
来源、验证时间与该版本范围；未验证的 release 字段绝不写成稳定契约。

### Worker 生命周期与打回

每张结算的 Dispatch 按当前 Orca completion accounting 处理：继续派 follow-up、用户要求
保留时 retain，或在产物已持久化且 release 不会破坏开放 PR 后 release。默认在合并及同步
完成后再 release；明确只交开放 PR 时，确认远端分支安全后按当前 Orca 语义处理。
结束前查询 reclaimable，逐项结清或记录受阻原因。

打回写明哪条门禁、缺什么证据、只需补哪些内容。已结算 worker 的 follow-up 使用新 Task，
复用原终端时显式指定原 worktree selector；新 turn 重走 §4.3.1。连续两次不过同一门禁时
暂停该票，提供差异分析请用户决定。跨票累计打回达到 5 次，或长时间无实质进展时，
停止新派活并报告当前状态；其余已在途工作按可用证据安全收尾。

进度看板：每个实际编排 Run 首次派活前初始化并自动打开；之后在正常动作完成后按
[进度看板](progress-dashboard.md)节点责任表记录显式事实及结构化 task/dispatch/attempt 报告；
记录错误只警告，不能阻断本流程。
