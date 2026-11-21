# 路由选择、显式流程记录与链式恢复

**什么时候读**：在 14 条路由间选择路径、运行仓库初始化检查（`init-check.py`）、初始化显式 `flow.jsonl`，或中断后用 `chain.py` 恢复进度时。

## 路由入口与初始化门禁

scope 或状态不明时先只读探索，再按事实选用唯一路由（用户点名 skill 时以用户为准）：

| 条件 | 路由 / Skill | 完成证据 | 返回处 |
| :--- | :--- | :--- | :--- |
| 纯问答、状态说明、口头结论 | 直接作答（不建 Run/worker/改动） | 回答已交付 | — |
| 方案/取舍未定（无工作目录 / 有目录） | `grill-me` / `grill-with-docs`（`grilling` + `domain-modeling`） | 记录问题、回答、依据；收敛的术语/ADR 落文档 | 需要该设计的下一阶段 |
| 小修小改、Bug 修复或已有有效 spec/票 | `implement`（修 bug 先走 `diagnosing-bugs`） | 写清范围、验收与依据来源；分叉则回 grill | G-impl |
| 规划本身跨会话且路径仍迷雾 | `wayfinder`（`chart` 或 `work`） | `chart` 产出票图与决策票；`work` 认领并解决 1 张非研究票 | `to-spec` 或小步直连 impl |
| 决策依赖可核查的一手材料 | `research`（AFK） | 带一手出处的 Markdown 报告落地 | 提问阶段 |
| 外观/状态/逻辑需可运行样本确认 | `prototype`（HITL） | 人类对临时分支可运行产物给出反馈 | 提问阶段 |
| 需外部人员答复 / 只能人手操作 | `to-questionnaire` / `wizard` | 按约定流程发出问卷 / 交互脚本确认完成 | 被阻塞的票 |
| 已收敛的多会话构建 | `to-spec` → `to-tickets` → `implement` / `implement-spec` | G-spec 接缝 + G-tickets 纵切 DAG + tracker 就绪 | G-impl |
| 原始 issue/PR 需分拣标签 | `triage` | 按 `docs/agents/triage-labels.md` 打标（`to-tickets` 产物直接 agent-ready） | `implement` |
| 架构接缝疼痛 / 难复现回归 | `improve-codebase-architecture` / `diagnosing-bugs` | 用户选定候选 / 稳定红→绿回归环 | `grill-with-docs` / `retro` |
| 流程缺陷或重复踩坑 | `retro`（仅只读分析） | 仓库外写 `retro-candidates.md`，人类挑选后再派工落地 | 下一轮 |

**仓库初始化门禁（`G-docs` / `G-tickets` 入口）：** 执行 `python3 scripts/init-check.py check --repo <target>`（在 `orca bot` 等非 git 目录协调时必须显式传入目标仓库路径，并动态读取 `<target>/docs/agents/triage-labels.md` 与 `<target>/docs/agents/issue-tracker.md`，绝不硬编码标签名）：
- 退出 `0`：已初始化（`AGENTS.md`/`CLAUDE.md` 含 `## Agent skills`，`docs/agents/issue-tracker.md` 与 `domain.md` 非空，托管仓库 `gh repo view`/`glab repo view` 可用）。
- 退出 `1`：未初始化 → 与人类确认后，再按显式路径派 worker 运行 `setup-matt-pocock-skills`（`disable-model-invocation: true`，绝不直接落 main）。
- 退出 `2`：`unknown` → 报告探测报错并核实，绝不当作未初始化。

## 显式流程记录（`flow.py`）与恢复（`chain.py`）

`ledger.jsonl` 是观测索引；`flow.jsonl`（`$ORCA_FLOW_HOME/runs/<run_id>/flow.jsonl` 或台账旁）才是 Run 的生效流程计划、门禁与交付终点权威：

```bash
FLOW="$SKILLS_ROOT/orchestra-auto-zh/scripts/flow.py"
CHAIN="$SKILLS_ROOT/orchestra-auto-zh/scripts/chain.py"

# 一条命令按标准预设初始化 flow.jsonl（无需手拼 JSON）
python3 "$FLOW" init-route --run-id "$RUN" --route full-chain --goal "实现功能 X" --compact
# 预设：design-only | single-impl | full-chain | wayfinder | ticket-map | bugfix

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
