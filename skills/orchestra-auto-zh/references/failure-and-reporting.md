# 失败与上报纪律

**什么时候读**：CLI 起不来、`worker-start` 回执为 `state: failed`、熔断触发、或 Orca app 运行中重启时。

## 9. 失败与上报纪律

- CLI 起不来、orchestration 指南拿不到：报告精确错误并停止，**不换可执行文件、不猜命令**。
- `worker-start` 回执为 `state: failed` 时，先取证再决定，不盲目重派。照
  [失败派活恢复](failed-dispatch-recovery.md) 的流程处理。
- 任何环节触发熔断（单票：连续 2 次过不了同一门禁；run 级：§6 打回协议的累计阈值）或
  escalation 类问题（冻结该票、其余照常，§5.1）→ 立即停下问人，说明卡在哪个阶段、已有的
  证据、需要人决定什么。
- **Orca 重启或绑定丢失**：确认 app 状态，用 `worker-list --include-remote` 盘点，按当前版本的 inbox/retry 命令恢复消息。保留原身份和台账；核对副作用前不重发 mutation、不新建 Run。身份被 fencing 或归属未证实就停止生命周期操作；无法核实则保留证据并升级。见[通信恢复](inbox-and-waiting.md)。

- 已获授权的实施工作不因任务小或 repo 缺 `WORK_SCOPE` 而跳过派活；纯问答、报告及超出
  本轮交付终点的后续阶段不派活。范围不明时先只读探索并按 §1 复核。
- 后续阶段已在本轮范围内时直接推进；不在范围内时在报告写明状态与下一步入口。
- 设计术语遵循仓库语言；需要架构分析时使用 codebase-design 的词汇帮助表达。

进度看板：每个实际编排 Run 首次派活前初始化并自动打开；之后在正常动作完成后按
[进度看板](progress-dashboard.md)节点责任表记录显式事实及结构化 task/dispatch/attempt 报告；
记录错误只警告，不能阻断本流程。
