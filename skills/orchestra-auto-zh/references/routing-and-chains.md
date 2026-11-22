# 路由选择、显式流程记录与链式恢复

**什么时候读**：每次收到用户输入或跨会话续接时，用于将任务拆分归类到唯一独立路由（`R0–R16`）、在意图不明时输出排序选择列表、向用户宣告进入的分支、守住写操作重入红线，以及运行 `init-check.py`、`flow.py` 与 `chain.py` 时。

## 1. 强制初始分类路由门禁协议

任何用户输入——包括上一轮编排结束后的跨会话续接（即使用户没有重复编排关键词）——在展开讨论、写文件或操作 tracker 前，一律先过本门禁：

1. **分类阶段仅允许最小只读探查**：仅在确认仓库现状所需时做只读查看；分类阶段绝不展开多轮架构讨论、不起草 ADR、不修改任何文件或 Issue。
2. **输入不明确 / 命中多条分支 → 排序给用户选择**：当用户输入可能对应多条分支（例如「讨论技术选型与接入架构」「帮我看看这个模块怎么优化」）时，**绝不静默猜测，也绝不直接按 `R0` 展开讨论**。按匹配度从高到低给出**编号排序列表**（通常列出 `R0–R16` 中最贴合的 2–4 项），并等待用户拍板：
   ```text
   当前需求存在多条可行路由，请按需选择一条（已按匹配度排序）：
   1. R2 `grill-with-docs`（B 通道 worker，G-docs）— 结合仓库现状做真实访谈收敛架构/技术取舍，并按需产出 ADR/术语表。
   2. R5 `research`（B 通道 AFK worker，G-research）— 先做带一手出处的外部调研报告，再回流决策。
   3. R3 `wayfinder-chart`（B 通道 worker，G-wayfinder）— 规划本身跨会话且路径迷雾时，先建决策地图与分型决策票。
   4. R0 `direct-answer`（A 通道只读，零写入）— 仅做简短口头问答，不落盘任何文件、ADR 或 GitHub Issue。
   ```
3. **输入明确 → 必选唯一分支并向用户宣告**：一旦能明确判定（或用户已点名 skill / 从排序列表中选定），**必须选定唯一一条独立分支（`R0–R16`）**，并在开始执行前**明确告知用户进入了哪条分支**：
   ```text
   已进入分支：R2 `grill-with-docs`（B 通道 — 受监督 worker）
   判定依据：目标仓库内存在未收敛的架构取舍，写 ADR / Spec / 拆票前必须先过 grill。
   目标与终点：<仓库> -> 收敛决策 + ADR/术语表（`G-docs`）
   执行者与门禁：在 `--worktree new-top-level` 中派发 worker 运行 `grill-with-docs`（`grilling` + `domain-modeling`），门禁 `G-docs`
   ```
4. **进入分支后全由 Worker 执行（`R1–R16`）**：一旦进入任意工作分支（`R1–R16`），**所有实质工作全部由受监督 worker 完成**（`worker-start --worktree new-top-level --agent <已启用>`）。协调者绝不在主会话里亲自开展 grill/架构讨论或直接改动产物，只负责：(a) 路由分流与派前预检；(b) 派发 worker 并通过 `reply --id <msg>` 中转决策类 `orchestration ask` 与人类答复；(c) 阶段 4 门禁验收、按授权合并、收尾与释放。
5. **讨论升级为写操作重入红线与受控产物禁令**：`R0（direct-answer）` 严格限定为只读且临时的口头问答（**零**持久写操作）。协调者绝不可直接写入或修改任何**受控产物**——包括项目源码/测试、`docs/adr/*.md`、`GLOSSARY.md`、`GLOSSARY-MAP.md`、Spec、票据文件，以及规划/拆票/分拣/探路阶段的 tracker 写操作（`gh issue create` / `edit` / `close`）。一旦从 `R0` 或普通讨论跨越到任何受控写入，**必须立即中止直答并重新进入本路由门禁**。
6. **写前 Grill 前置条件与反伪 Grill 规则**：在协调者聊天中随口问 3 个宏观选择题**绝不等于**完成了 `R1 grill-me` 或 `R2 grill-with-docs`（`G-docs`）。任何 `docs/adr/*.md` 落盘、`R9 to-spec` 或 `R10 to-tickets`（`gh issue create`）之前，必须先由 worker 通过 `R1`/`R2`（`G-docs`），除非用户显式要求跳过 grill 并已提供收敛好的决策/规格。
7. **阶段流转协议**：当前分支在阶段 4 通过门禁后，若已到达授权终点则直接收口汇报；若需继续推进下一阶段，必须显式重入本路由门禁——单一后续明确时直接宣告进入的新分支（如 `R2` → `R9 to-spec` → `R10 to-tickets` → `R13 implement-spec`），存在多条可行后续时排序给用户选择。

## 2. 独立拆分的 17 条路由矩阵（`R0–R16`）

每一种路由场景单独成行，互不混杂（用户明确点名 skill 时以用户为准）：

| ID | 路由 / Skill | 通道 / 执行者 | 单一触发条件 | 前置 Skill（`--type` / `--gate`） | 完成证据 | 后续分支 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **R0** | `direct-answer` | A 通道（协调者只读；**无 worker、零写入**） | 纯问答、口头状态说明或只读解释，且零持久写操作 | 无（不建 Run / 不派 worker） | 口头回答已交付 | 任何写意图 → 重入 `R1–R16` |
| **R1** | `grill-me` | B 通道（Worker） | 想法或取舍未定，且**无目标仓库 / 工作目录** | `grill-me`（`grilling`）· `design` / `G-docs` | Worker 问答记录、已收敛决策与依据 | `R9`、`R5`、`R6` 或收口 |
| **R2** | `grill-with-docs` | B 通道（Worker） | **在仓库内**的未定想法、架构/技术选型或权衡 | `grill-with-docs`（`grilling` + `domain-modeling`）· `design` / `G-docs` | Worker 问答记录、依据、`GLOSSARY.md` / `docs/adr/*.md` | `R9`、`R12`、`R5`、`R6` 或收口 |
| **R3** | `wayfinder-chart` | B 通道（Worker） | 规划本身跨多 session 且路径迷雾，需初建决策地图 | `wayfinder`（`chart`）· `design` / `G-wayfinder` | 地图 Issue + 分型决策票 + 真实阻塞关系（建图即止） | `R4`（后续会话）或收口 |
| **R4** | `wayfinder-work` | B 通道（Worker） | 已有 wayfinder 地图，认领并解决下一张 frontier 票（每 session ≤1 非研究票） | `wayfinder`（`work`）· `design` / `G-wayfinder` | 先认领、按 HITL/AFK 解决、关票并更新地图指针 | `R4`、`R9` 或 `R12` |
| **R5** | `research` | B 通道（Worker，AFK） | 决策依赖可核查的一手或外部事实材料 | `research` · `research` / `G-research` | 带一手出处与边界说明的 Markdown 调研报告 | 返回提问分支（`R1`/`R2`/`R4`/`R9`） |
| **R6** | `prototype` | B 通道（Worker，HITL） | 外观、交互、状态机或逻辑需可运行样本确认 | `prototype` · `design` / `G-prototype` | 临时分支可运行产物 + 已记录的人类反馈 | 返回提问分支（`R1`/`R2`/`R4`/`R9`） |
| **R7** | `to-questionnaire` | B 通道（Worker，异步 HITL） | 缺失答案掌握在外部人员/团队手中 | `to-questionnaire` · `docs` / `G-questionnaire` | 按约定流程准备/发出问卷；依赖票保持阻塞 | 收到真实回复后回流原分支 |
| **R8** | `wizard` | B 通道（Worker + 人类） | 只能由人类亲自完成的操作（凭证/后台/审批） | `wizard` · `docs` / `G-wizard` | 经核验的人类操作向导；保持 pending 直到人类确认完成 | 人类完成后回流原分支 |
| **R9** | `to-spec` | B 通道（Worker） | 决策已收敛（已过 grill 或显式跳过），需撰写多 session 权威规格 | `to-spec` · `spec` / `G-spec` | 高/少接缝、清晰用户故事、非空 Out of Scope | `R10` 或止于规格终点 |
| **R10** | `to-tickets` | B 通道（Worker） | 已验收的 Spec 需纵切为 DAG 票图并发布到 tracker | `to-tickets` · `ticket-splitting` / `G-tickets` | 无环纵切 DAG + tracker 建票就绪（`gh issue create`） | `R12`（单票）或 `R13`（多票图） |
| **R11** | `diagnosing-bugs` | B 通道（Worker） | 根因不明的 Bug 或难复现回归，需先定位并建立红复现 | `diagnosing-bugs` · `impl` / `G-impl` | 已证实根因 + 稳定失败的红复现测试 | `R12`（修复）及可选 `R16` |
| **R12** | `implement` | B 通道（单 Worker） | 边界清晰的单次改动、根因明确的修 Bug 或单张就绪原子票 | `implement`（+ `code-review`）· `impl` / `G-impl`、`G-merge` | 入口依据（为何免 grill）、TDD 红→绿、`code-review`、PR；遇新分叉回 `R2` | `G-merge` 收口 |
| **R13** | `implement-spec` | C 通道（1 个集成 Worker） | 已验收的完整多票 DAG 票图（`to-tickets` 过门且 tracker 可达） | `implement-spec`（+ `tdd`/`code-review`/`pr`）· `impl` / `G-impl`、`G-merge` | 嵌套隔离证明、逐票 TDD、唯一集成 PR（含 `Closes`） | `G-merge` 收口 |
| **R14** | `triage` | B 通道（Worker） | 外部原始 Issue/PR 需分类打标（不重复 triage `to-tickets` 产物） | `triage` · `docs` / `G-triage` | 按 `docs/agents/triage-labels.md` 完成打标与澄清 | `R12`、`R2` 或收口 |
| **R15** | `improve-codebase-architecture` | B 通道（Worker） | 架构接缝疼痛或测试 seam 难以建立，需梳理重构候选 | `improve-codebase-architecture`（`codebase-design`）· `design` / `G-docs` | 排序后的重构候选交人类选定 | 人类选定后进 `R2` / `R9` / `R12` |
| **R16** | `retro` | B 通道（Worker，先只读） | 流程偏差、漏门或重复踩坑需要复盘 | `retro` · `research` / `G-retro` | 只读 `retro-candidates.md` 写于仓库**外**；人类挑选后再派工落地 | 人类选定后经 `R12` 落地 |

## 3. 仓库初始化门禁（`G-docs` / `G-tickets` 入口）

执行 `python3 scripts/init-check.py check --repo <target>`（在 `orca bot` 等非 git 目录协调时必须显式传入目标仓库路径，并动态读取 `<target>/docs/agents/triage-labels.md` 与 `<target>/docs/agents/issue-tracker.md`，绝不硬编码标签名）：
- 退出 `0`：已初始化（`AGENTS.md`/`CLAUDE.md` 含 `## Agent skills`，`docs/agents/issue-tracker.md` 与 `domain.md` 非空，托管仓库 `gh repo view`/`glab repo view` 可用）。
- 退出 `1`：未初始化 → 与人类确认后，再按显式路径派 worker 运行 `setup-matt-pocock-skills`（`disable-model-invocation: true`，绝不直接落 main）。
- 退出 `2`：`unknown` → 报告探测报错并核实，绝不当作未初始化。

## 4. 显式流程记录（`flow.py`）与恢复（`chain.py`）

`ledger.jsonl` 是观测索引；`flow.jsonl`（`$ORCA_FLOW_HOME/runs/<run_id>/flow.jsonl` 或台账旁）才是 Run 的生效流程计划、门禁与交付终点权威：

```bash
FLOW="$SKILLS_ROOT/orchestra-auto-zh/scripts/flow.py"
CHAIN="$SKILLS_ROOT/orchestra-auto-zh/scripts/chain.py"

# 一条命令按单分支预设或组合链预设初始化 flow.jsonl（无需手拼 JSON）
python3 "$FLOW" init-route --run-id "$RUN" --route grill-with-docs --goal "收敛鉴权 ADR" --compact
# 单分支预设（R1–R16）：
#   grill-me | grill-with-docs | wayfinder-chart | wayfinder-work | research | prototype |
#   to-questionnaire | wizard | to-spec | to-tickets | diagnosing-bugs | implement |
#   implement-spec | triage | improve-codebase-architecture | retro
# 组合链预设（多阶段 Run）：
#   design-only | single-impl | full-chain | wayfinder | ticket-map | bugfix

# 查看当前就绪节点、阻塞项、waiting_user 与下游作废状态
python3 "$CHAIN" resume --run-id "$RUN" --ledger-home "$ORCA_LEDGER_HOME" --compact

# 机械底线格式校验（不替代 coordinator 门禁判决）
python3 "$CHAIN" exit-check --step to-spec --artifact docs/spec.md --compact
python3 "$CHAIN" exit-check --step to-tickets --artifact docs/tickets.md --source list --compact
```

**恢复规则：**
- `flow.jsonl` 优先级高于台账推断（纯台账 `legacy` 恢复只提供五位置启发建议，不能证明当前终点或门禁）。
- 节点状态：`pending`、`in_progress`、`waiting_user`、`blocked`、`accepted`、`not_applicable`（须带 `entry_evidence`）、`invalidated`。
- 上游推翻或修改时向 `flow.py` 追加 `invalidate` —— 全部传递下游节点自动变为 `invalidated`，未重过前不得收尾。
- `chain.py exit-check --step to-tickets`：可多次 `--artifact` 校验散票目录，或用 `--source list|file|issue` 消除单文件歧义；退出 `2` 表示格式歧义。
- 把规划交给新上下文时，必须完整移交已确认问答、被否方案、未决问题与责任人、已核实代码事实、目标仓库/base/终点与 Out of Scope 边界。
