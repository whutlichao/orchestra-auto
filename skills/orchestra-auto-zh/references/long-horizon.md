# 长程票图与多票集成分支模式

**什么时候读**：当 `G-tickets` 验收通过完整票图（`implement-spec`），或跨多张依赖票推进单一集成 PR 交付时。

## 1. 集成主干模型

针对整张票图只起**一名**协调者托管的顶层 worker（`--worktree new-top-level --name <feature>-integration`），`前置 skill` 写 `implement-spec`（并附已解析的 `tdd`、`code-review`、`pr` 路径及 `Tracker`、`Spec`、`Tickets`、`Integration branch`）。

- 开工前其首份报告必须证明具备**嵌套隔离能力**（能从 integration branch 切出隔离分支/worktree 并合回，绝不触碰 `main`）。
- 无法证明时在台账显式记录降级，转为同一 worktree 内串行实现或回落逐票 Orca worker（默认上限 `3`）。绝不允许两个 implementer 共用同一 worktree。
- 协调者仅验收这**唯一集成 PR**（正文带规范与各票 `Closes` 链接），不直接进入嵌套 worktree；从各票抽查至少两张票的实测与验收条件，避免只看口头总结。Review 采用逐票 TDD 加 integration branch 一次双轴 `code-review`（不再跑逐票双轴 review）。

## 2. 四级验收彼此独立

严禁将四项进度混为一谈：

1. **票实施完成**（`implemented`）—— 该票自身分支 TDD/检查通过。
2. **集成已验收**（`integrated`）—— 已合入集成主干且集成测试通过。
3. **PR 已合并**（`merged`）—— PR host `MERGED` 且经 git 祖先/等价性验证。
4. **票据按 tracker 关闭**（`tracker_closed`）—— 合并后按 tracker 实态读回。

严禁因**合并前所有票都必须关闭**而卡死流程（借助 `Closes` 自动闭单的票在开 PR 时仍为 open），也严禁**PR 还开着就报告票已关闭**制造假闭环。合并后的 tracker 核查遵循[验收与收尾](acceptance-and-closeout.md)。

### 4.1 收尾、单向门 `pending` 与 release 门禁

长程流程同样走[验收与收尾](acceptance-and-closeout.md) `§6.7` 的十行 `closeout.py` 对账，不凭记忆：

- **单向门 `pending` 无需协调回合即可保持可见**：单向集成 PR 停在开放态时写一条台账 `pending`（`pending_type: human_merge`），可通过 `ledger.py show --run-id "$RUN" --kind pending` 与看板直接查看；下一回合从 PR host、merge SHA、主干同步与 tracker 逐项复核消账。
- **release 在结算与持久收尾之后**：放行 `worker-release` 前必须具备已结算 `worker-show`（`ready` 或健康状态绝不代替结算）与持久 `kind: closeout` 记录（成功须含映射与验收记录，失败须含分类与证据）。遇 `release_unknown` 先回读 `worker-show` 与 `worker-list`；未结算 worker **绝不 release**。closeout 写入失败落盘到 `closeout-pending/` 并继续阻断 release。

## 3. 分门合并与 `retro` 治理

- 权威单向门触发条件统一读[验收与收尾](acceptance-and-closeout.md) `G-merge` 第 0 步（本页不保留第二份拷贝）。用户授权合并且双向全绿 → 自动合并集成 PR、同步 `main` 并核实 `Closes` 关票；若单向、存疑或仅要求开 PR → 止于开放集成 PR 并明确人类合并步骤（各票仍保持 open）。
- 若途中出现反复漏门、越界等**流程错误**，在安全节点只读跑 `retro` 并将候选修改写入仓库外 `retro-candidates.md`，由人类筛选后再走完整流程与 eval 用例落地（绝不运行中擅改 skill）。

| 异常情形 | 处置动作 |
| :--- | :--- |
| 票图不完整或 tracker 不可达 | 退回 `to-tickets`；暂不派活 |
| 无法证明嵌套 worktree / 子代理隔离 | 显式记录降级为串行或逐票 worker（上限 3） |
| 集成 PR 命中单向门或存疑 | 保持 PR 开放并登记 `pending_type: human_merge`，交由人类合并 |
| 未清理嵌套 worktree 或污染 `main` | 保留现场直接打回，绝不验收 |
