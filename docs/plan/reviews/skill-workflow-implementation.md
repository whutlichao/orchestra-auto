# 工作流重构实施记录（WF-01—WF-08 收敛）

实施日期：2026-11-08。基线提交 `0b1bb3d`（分支 `content-let-me-look`，worktree `booby`）。
本文件记录实际交付、验证证据与**未验证**范围，不替代 [spec](../../design/skill-workflow-spec.md) 的验收条目。

## 1. 执行环境与基线

| 项 | 值 |
| --- | --- |
| 基线提交 | `0b1bb3d`，工作树干净 |
| 全量回归（改动前） | `python3 -m unittest discover -s tests` → 740 tests, OK (skipped=1) |
| Orca CLI | `orca` 1.4.253；`orca status --json` runtime ready / reachable |
| 上游 skills | 本机 `~/.agents/skills/`，20 个相关 `SKILL.md` 已记录 SHA-256（见 §6） |
| Python | 本机 3.15；CI 矩阵 3.10 / 3.14 |

未运行真实 Orca 演练（无 Run、无 worker、无消息）；本记录不声称任何 Orca 端到端行为已验证。

## 2. 交付内容

| 任务 | 交付 | 证据 |
| --- | --- | --- |
| WF-01 路由与能力 | `SKILL.md` §1 增加「路由索引」，逐族点名 wayfinder / research / prototype / questionnaire / wizard / triage / diagnosing-bugs / improve-codebase-architecture / retro，避免入口只露常见链；双语 `dispatch-loop.md` 新增 15 行路由表（条件 / 输入 / skill 与依赖 / 完成证据 / 回流）；`SKILL.md` §1 拆成「通道」与「路由记录」两件事并去掉重复入口判据；新增「调用类型、wrapper 依赖与宿主能力」三节 | `tests/test_package_content.py`（引用可达、锚点、入口 <300 行）；`tests/test_doc_contract.py` |
| WF-02 上下文与交接 | 双语 `spec-contract.md` 新增「规划上下文与交接」：三份文档边界、交接记录字段、数值/否定约束必须保留、指针而非复制、用户回答经原消息回流 | `tests/test_chain.py` 的 `handoff` 场景在 eval 中；文档契约测试 |
| WF-03 决策分支闭环 | 双语 `gates-docs-planning.md` 新增「决策支线」表（research / prototype / questionnaire / wizard / task）及阻塞规则；`chain-run.md` 说明 wayfinder 只记 chart/work/map-complete 指针 | 同上；`wayfinder-*`、`questionnaire-and-wizard-block` 场景 |
| WF-04 流程记录与兼容 | 新增 `scripts/flow.py`（schema v1：flow / state / invalidate / import；7 种节点状态；只追加；过期验收不覆盖新状态；失效向下游传播；导入需来源与核验）；`ledger.md` 增加「流程记录是计划，不是另一本台账」 | `tests/test_flow.py`（39 项，含 CLI 与临时文件） |
| WF-05 路由感知续跑 | `chain.py resume` 优先读 flow 并输出 `ready` / `waiting_user` / `blocked` / `invalidated` / `evidence_gaps`；无 flow 时保留旧五位置链并标 `legacy`；`uncertain` 退出码 1 | `tests/test_chain.py` 新增 11 项（含 flow 优先、uncertain、legacy） |
| WF-06 产物格式检查 | `chain.py exit-check`：`inspect_spec` 分离 native / dispatch；票据按 `list` / `file` / `issue` 三种真实边界检测，歧义退出码 2 并要求 `--source`；新增未知阻塞、自阻塞、环、本地链接、多文件集合 | `tests/test_chain.py` 新增 15 项 |
| WF-07 治理入口与双语集成 | 双语 14 个新 eval 场景（路由、小修复、过期 spec、依赖缺失、交接、wayfinder chart/work、问卷/向导、flow 设计终点、legacy、失效传播、issue 正文、spec 双块、triage/retro）；README / launch-kit 计数与入口大小表按文件复算；CI macOS 模块表补 `test_flow` | `tests/test_package_content.py` 77 项通过 |
| WF-08 Orca 真实演练 | **未执行**（本轮授权为本地实施） | 无 |

## 3. 验证结果

```text
python3 -m unittest discover -s tests     # 795 tests, OK (skipped=1)
python3 -m py_compile skills/*/scripts/*.py
bash -n skills/*/scripts/inbox-loop.sh
```

冒烟（临时目录，未写入用户数据）：

```text
flow.py append(flow) → append(state accepted) → plan
  → state=complete, ready=[], nodes=[(grill, accepted), (spec, not_applicable)]
chain.py resume --ledger-home <tmp>   # flow 存在 → complete，无 legacy 块
chain.py exit-check --step to-tickets --artifact 01.md --artifact 02.md → passed
chain.py exit-check --step to-tickets --artifact issue.md --source issue → passed
```

`legacy` 路径、`uncertain` 路径、集合与 issue 正文路径均由 `tests/test_chain.py` 覆盖，未在冒烟中重复。

## 4. 未验证范围（不得当作已通过）

- **独立 verify 未执行**：本文件是实施者自述记录，不是独立审阅；任务文件要求的「由独立 verify 对照 spec 审阅」仍未完成，因此 WF-01—WF-08 的 `status` 保持原值，不改 done。
- **真实 Orca 演练（AC15 后半）**：无真实 ask/reply、原生唤醒、worker_done、worktree 隔离、chart/work 运行证据。
- **agent 场景评测的真实执行**：14 个新场景是提示词层面的 fixture，未在真实模型上跑过；它们证明覆盖范围，不证明模型行为。
- **不同宿主对 user-invoked skill 的支持**：只核查了本机 `disable-model-invocation` 元数据，未在其它宿主验证。
- **CI 实跑**：本地跑了全量；GitHub Actions 矩阵未触发（本轮未推送）。

## 5. 已知限制

- `flow.py` 的 schema 是 v1；未来版本必须拒绝推断未知字段（当前 `validate_record` 只看已知必填项，未知额外字段被忽略——这是有意的前向兼容，但未写兼容测试）。
- `exit-check` 的 ``list`` 模式依旧要求票名在 `##` 标题里；上游一票一文件格式由 `--source file` / 集合模式覆盖，两者不会互相误判。
- `chain.py resume` 在 flow 文件存在但无有效记录时（例如只有损坏行）会返回 `uncertain`，不再回退 legacy——这是有意的，避免悄悄降级。

## 6. 上游与 Orca 取证版本

- `ask-matt` `1371f5ce005d…`、`grill-me` `caaf8b8de168…`、`grilling` `a146842ed93c…`、`grill-with-docs` `7de372c13488…`、`wayfinder` `0c9fa96791ae…`、`to-spec` `43ad9cf318e5…`、`to-tickets` `56ec23c5b278…`、`implement` `483bdc4ccf4a…`、`implement-spec` `1993a473cec4…`、`triage` `917b0baf74be…`、`retro` `33fbc28568e3…`、`handoff` `4cbb043950da…`、`to-questionnaire` `b5eb929842ee…`、`prototype` `714de632d116…`、`research` `1f629e18ab89…`、`wizard` `bdf31d48211e…`、`domain-modeling` `7b925d7b1e34…`、`codebase-design` `2c20617f87ec…`、`improve-codebase-architecture` `35b03a3db61e…`、`diagnosing-bugs` `849198df88cd…`（完整哈希见本机 `~/.agents/skills/*/SKILL.md`）。
- Orca 指南与帮助：`orca skills get orchestration`、`orca orchestration worker-start --help`、`orca --version`（1.4.253）。
