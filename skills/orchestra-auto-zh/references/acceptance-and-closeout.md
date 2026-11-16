# 门禁验收、合并安全、十行收尾与阻塞报告

**什么时候读**：准备通过 `G-impl` / `G-merge` 门禁、持久化四级验收记录（`acceptance.py`）、运行十行收尾对账（`closeout.py reconcile --live`）或提交阻塞/总结报告时。

## 1. 按票型选择门禁

| 票型 | 适用门禁 | 验收焦点 |
| :--- | :--- | :--- |
| 研究票 | 来源与结论核实 | 一手出处、证据支撑、限度写明、满足终点 |
| 设计 / wayfinder 票 | `G-docs` / `G-wayfinder` | 决策收敛、词表/ADR、frontier 清空（见 `preflight-and-spec.md`） |
| 规格 / 拆票 | `G-spec` / `G-tickets` | 高接缝少而清晰、Out of Scope、纵切 DAG、已进 tracker |
| 实施票 | `G-impl` | worktree 干净、对齐 `测试档：` 的红→绿实测、静态检查、双轴 `code-review`（或 spec 与 Answer 共写豁免）、`--body-file` 开 PR |
| 项目改动自动合并 | `G-merge` | 单向/双向门核验、合树 CI、DAG 合并、本地快进、tracker 对账 |

纯文档票仅跑适用文档检查，不强加源码单测。签名/参数改源须逐处盘点调用点。严格执行打回熔断：同票同门禁连续 2 次失败停下问人；跨票累计打回达 5 次暂停派发并报告现状。

## 2. `G-impl` 与 `G-merge` 规则

- **收尾核查**：先跑 `git status --porcelain` 与 `git diff --numstat`（万行级重排见 [recovery-and-health.md](recovery-and-health.md)）；核实所跑测试与 `测试档：` 及 base commit 基线一致、拿到红→绿证据；涉及 Orca 实际渲染截图时须查 `visual-evidence.md` 报告（`window.restored` 与 `focusRestored` 均为真）。`worker_done` 只证结算，协调者不替 worker 手改交付代码。
- **单向门权威触发清单（`G-merge` 第 0 步）**：**本清单是单向门的唯一权威定义**（其余文档只指向此处）。命中任一条或拿不准即属单向门（PR 描述若自称双向但 diff 矛盾，打回修正正文并仍按单向处理）：
  - 数据库 schema / 数据迁移或删除；
  - 破坏性公共 API / CLI / SDK 变更（删改签名、语义、默认值）或持久化格式变更（纯向后兼容新增本身不算）；
  - 认证 / 权限 / 密钥；
  - 计费 / 定价；
  - 部署 / 基础设施改动；
  - 不可逆外部副作用；
  - 移除既有功能；
  - **主版本依赖升级**；
  - 影响面大且难一键回滚。
- **合并放行判定**：**用户授权合并、双向门且门禁全绿**时自动合入并同步；若只要求开 PR（或单向门 / 红 / 阻断），必须停在开放 PR，写明人类合并指令与依据。
- **同步本地 main（`§6.5.1`）**：先确认主工作区处于目标分支、干净且有显式 upstream：普通仓库跑 `git pull --ff-only`；fork 从上游拉 `git fetch upstream` 快进本地 main 到 `upstream/<默认>` 后推 fork 镜像，并校验 `git merge-base --is-ancestor <merge_sha> HEAD`。严禁 auto-stash、reset 或强推。
- **合并后按 tracker 对账**：PR `MERGED` 不自动等同于 issue 关闭，必须读回 tracker 实态并记录未闭原因。

## 3. §6.6 四级验收记录（`acceptance.py`）

先落盘持久 `kind: acceptance` 记录，再宣称过门或 release worker：

```bash
ACCEPTANCE="$SKILLS_ROOT/orchestra-auto-zh/scripts/acceptance.py"
python3 "$ACCEPTANCE" record --run-id "$RUN" --record "$ACCEPTANCE_JSON"
python3 "$ACCEPTANCE" show --run-id "$RUN" --ticket 39 --compact
```

记录字段：`ticket`、`gate`、`attempt`、`accepted_sha`、`accepted_at`、`pr {number, head_at_acceptance, base}`、`checks [{name, command, result, evidence_ref}]`、`review {axis[], result, evidence_ref}`、`levels`、`decided_by`、`decided_at`、`answer_location`。

**最小证据与四级规则：**
- 不允许任何 level 为真却没有 `evidence_ref`：`merge` 要求 PR host 的 `MERGED`、merge SHA 及 `ancestry`（`git merge-base --is-ancestor`）或 `equivalence`（`git diff` 为空）之一；tracker 要求**读回** `closed`；start 要求 `turnStart: observed`（或该次 attempt 产物/投影——`worker_done` 只证明结算）；测试要求实跑命令与日志（`grep` 只证明字符串存在）；review 要求双轴（`standards`、`spec`）或在 spec **和** Answer 同时写明豁免理由。
- 票据四级（`implemented`、`integrated`、`merged`、`tracker_closed`）只有在**每个**关联 PR 记录都支持时才成立；同一 PR 存在互相冲突的记录判为 `divergent`。人类可把 `merged` 设为终态 `never_merge`（`never_merge` 是 merged 级的终态）。revert 或 reopen 必须通过 `acceptance.py observe` **追加** observation，不改写既有记录；开放中的四级问题（`open-questions`）保持受影响语义为 `provisional` 而允许关票，待后续决策后通过 `resolve-question` 落地。

## 4. §6.7 十行 Run 收尾核对（`closeout.py`）

从外部权威对账并落盘 `kind: closeout`（传入 `--live` 自动收集 git / gh / orca，可选 `--authority FILE` 覆盖）：

```bash
CLOSEOUT="$SKILLS_ROOT/orchestra-auto-zh/scripts/closeout.py"
python3 "$CLOSEOUT" reconcile --run-id "$RUN" --live --answer-location "$ANSWER" --compact
python3 "$CLOSEOUT" show --run-id "$RUN" --compact
```

| # | 核对事实 | 权威来源 | 可重算 | 动作 |
| :--- | :--- | :--- | :--- | :--- |
| 1 | 每 PR 的 accepted SHA | 台账验收记录 | **否** | 验收当下持久化，事后无法重造 |
| 2 | PR `MERGED`、base 与 merge SHA | PR host API | 是 | 读回并记 observation |
| 3 | 合并覆盖 accepted head | `git merge-base --is-ancestor` / `git merges`（equivalence） | 是 | 失败判 `divergent` 并升级 |
| 4 | main 包含该 merge | `git rev-parse` / ancestry | 是 | 记录包含结果 |
| 5 | 本地 main 已同步 | `git status` / upstream ff-only | 是 | 阻断后续合并（`merge_blocked`） |
| 6 | 每票已集成 | integration 分支祖先 / `Closes` | 是 | 逐票记录 |
| 7 | tracker 状态 | tracker 读回 | 是 | 记录关闭或重开 |
| 8 | worker 生命周期 | `worker-show` / `worker-list` / release 回执 | 是 | 列出 `reclaimable` / `release_unknown`；尊重 retained |
| 9 | dashboard 投影 | `progress.json` | 是（非权威） | 只记 gap，绝不作证据或阻断 |
| 10 | 单向门 pending | 台账 `pending`（`human_merge`） | 是 | 复核 PR/SHA/同步/tracker；无需 coordinator turn 亦可见 |

收尾规则（`scripts/closeout.py`）：只有第 1 行不可重复。第 9 行只记 gap（`progress.json` 永不作证据）。合并与对账间崩溃可按第 2、3、4、7 行修复。每条 closeout 记录必须写 `answer_location` 及 release 重放核查的版本范围。release 需要已结算的 Dispatch 与持久 closeout 记录（`release_unknown` 先读 `worker-show`；写入失败写进 `closeout-pending/`，release 与门禁宣告都等写入修复）。未结算 worker 绝不 release。

## 5. 升级与阻塞报告（§9）

CLI 无法启动或拉不到指南时带报错立即停下（不私换二进制、不猜命令）。`worker-start` 返回 `failed` 或 Orca 重启时，先用 `worker-list --include-remote` 与收件箱对账再行动。报告须清晰给出：（1）卡点阶段与目标终点，（2）已核查证据（提交/PR/实测/closeout），（3）`Q → A` 决策审计列表，（4）未决阻塞与精准下一步。
