# Orca 编排机制与本项目的适配

调查日期：2026-11-08；项目规则的调查基线为提交 `51d3cb2cf06bd1986a87e74837e3a2184e582100`，同一工作区后续修复不改变以下基线发现。对象是本机 `/usr/local/bin/orca` 1.4.253；`orca status --json` 报告本地 runtime 为 ready、reachable，appVersion 为 1.4.253。本文只做只读查询，没有发起 worker、测试消息投递或修改 Orca 状态。下述 CLI 行为是该版本随包指南和帮助的契约，不是对所有版本的保证。执行时先按本地 `orca-cli` 发现入口（`~/.agents/skills/orca-cli/SKILL.md`） 选择可执行文件，并读取其 `skills get orchestration` 和相关命令帮助；跨主机也要检查对应 host 的能力。

## 一手资料

- 本机版本匹配的指南：`orca skills get orchestration`，条件参考通过 `orca skills get orchestration --reference references/coordinator-loop.md`、`references/messaging-and-gates.md`、`references/recovery-and-cleanup.md` 读取；`orca skills get orca-cli` 解释 worktree、启动和 handoff。它们随执行程序变化，命令是该版本的可复验来源。
- 本机帮助：`orca orchestration worker-start --help`、`ask --help`、`check --help`、`orca --version`。这些帮助约束具体参数与退出语义。
- 官方公开 [Orchestration CLI 文档](https://github.com/stablyai/orca/blob/main/docs/site/content/docs/cli/orchestration.mdx) 和 [orchestration 指南](https://github.com/stablyai/orca/blob/main/skill-guides/orchestration.md) 有相同概念，但 `main` 会变化；版本不一致时以当前执行程序的指南和帮助为准。

## 如何选择机制

| 目的 | 机制 | 要点 |
| --- | --- | --- |
| 移交后不再负责结果 | `orca worktree create ... --agent ... --prompt ...` 或现有 agent 的 `terminal send` | full handoff 不创建由原 agent 监督的 Task；收到 prompt 投递证据即可交付，不等待结果。 |
| 需要结果、提问应答、依赖或生命周期 | `orca orchestration worker-start` 及编排消息 | worker 属于 Task/Dispatch，交付后 coordinator 被唤醒并处理，不用终端输入代替编排消息。 |
| 独立子代理已足够 | 本机指南建议优先使用宿主自身子代理 | 需要不同 agent、独立 Orca workspace 或用户指定 Orca 时才有额外理由使用 Orca。 |

来源：版本匹配的 `orca-cli`、`orchestration` 指南及上面的官方文档。项目是特意为监督、隔离和验收提供更强工作流，因此可以作出比 Orca 默认值更严格的约束；不要将这种约束当成 Orca 本身的要求。

## 对象和放置

- **Run** 是协调范围和 coordinator 邮箱；**Task** 保存工作及依赖；**Dispatch** 是某次派发尝试；worker 是受监督的执行者。`worker-start --spec` 可一起创建 Task 和尝试；未绑定 Run 时该版本可以自动创建。对于已有依赖图先 `run-create`、`task-create --deps`，再从 `task-list --ready` 选可执行任务。来源：本机 `coordinator-loop.md`、`worker-start --help`、[官方 CLI 文档](https://github.com/stablyai/orca/blob/main/docs/site/content/docs/cli/orchestration.mdx)。
- Orca 的示例优先 `--worktree current`，只有用户要求或共享会冲突时才新建。当前项目要求所有受监督 worker 放在 `new-top-level`，这是项目的隔离政策，代价是更多 worktree、设置和分支管理。新建 worktree 的 lineage 与 Git base 是不同维度；不能从是否为 child 推断分支基线。来源：本机 `orchestration`、`orca-cli` 指南；本项目 [派活闭环](../../skills/orchestra-auto/references/dispatch-loop.md)。
- 新 workspace 默认运行 setup；repo 配置决定 agent 与 setup 并行还是等 setup。当前或既有 worktree 不会重新运行 setup。新 worker 可能是结构化 chat，也可能是 terminal；使用 `worker-show` / `worker-read --source auto` 和 orchestration 消息，不假设一定有 terminal。来源：`worker-start --help`。
- Task spec 至少包含目标、要做的变更、限制、允许编辑范围、完成证据，并明确非零退出是失败还是需要采集的结果。它不等同于产品 spec：前者是给 worker 的自包含委派说明，即使任务是生成产品 spec 也需要它。来源：本机 `orchestration` 指南的 Task spec 小节。

## 提问与唤醒的时序

1. coordinator 启动独立 worker 后结束当前 turn；Orca 在收到消息时唤醒 coordinator。结构化 chat 通常收到完整消息；唤醒中指明 `check` 时才由该 agent 自己运行它。来源：本机 `orchestration` 指南。
2. 活跃 worker 发 `orca orchestration ask --question ...` 默认**立即返回**；worker 结束 turn，Orca 在答复到达后以邮件唤醒。工作在等待答案的逻辑依赖不代表命令正在阻塞。`--wait` 和 `--resume` 才同步等待；超时不取消提问。已完成的 worker 有不同的等待行为。来源：`orca orchestration ask --help`。
3. coordinator 判断问题是否已由任务、用户指令或项目证据解决；未解决的决策交给用户，再以原消息 ID `reply`。`send --to dispatch:<id>` 用于继续指示 worker；工作中在其当前 turn 结束时接收，已结束或关闭的本地 worker 会作为 follow-up Task 重启（paired server 已结束 worker 的例外须读远程参考）。来源：本机 `orchestration` 指南。
4. 普通 `check` 返回最早未读 FIFO 批次并立即标为 handled；这只说明邮件已消费，不证明答复、提交或其他业务动作完成。必须先处理整批，再进行下一次消费；丢响应可按帮助使用 `--retry-request`，上下文丢失可用明确 mailbox 的 `inbox` 查历史。监视脚本不得消费消息或 ack；本项目进一步规定不轮询，由 native wake 驱动。来源：`check --help` 和 `messaging-and-gates.md`。

**与本项目的差异**：基线提交中的[中文应答协议](https://github.com/whutlichao/orchestra-auto/blob/51d3cb2cf06bd1986a87e74837e3a2184e582100/skills/orchestra-auto-zh/references/reply-protocol.md) 与[英文应答协议](https://github.com/whutlichao/orchestra-auto/blob/51d3cb2cf06bd1986a87e74837e3a2184e582100/skills/orchestra-auto/references/reply-protocol.md) 将 worker 的 ask 描述为阻塞；后续是否修复以工作区当前文件为准；对 1.4.253 默认调用并不准确。应改为区分“此决策未答复前不得继续依赖它的工作”与“`ask` 调用是否阻塞”，并指示 worker 默认结束 turn、由唤醒恢复；超时恢复仅适用于实际使用 `--wait` / `--resume` 的调用。否则执行者可能等待一个早已返回的命令或误判 worker 失联。项目允许 coordinator 代答细节、决策问人，这是项目规则，不能推断 Orca 自行执行了这些分类。

## 恢复与完成

- `worker_done` 结算 Task；失败报告不能标成功。Task 已结算不等同于物理进程已退出，也不等同于产物已通过项目验收。收到通知后仍需要按项目门禁核对真实产物与证据。来源：本机 `orchestration`、`recovery-and-cleanup.md`。
- 沉默、超时、不可验证 liveness 本身不证明 agent 已退出，也不允许自动停止、放弃、重试或释放。失败启动应查看 `failedStage`、副作用、保留资源并按版本匹配恢复指南处理；重试同一 Task 用 `--retry-of` 且显式重选放置，不应制造新 Task 绕过熔断。三次失败启动后的 breaker 需要用户明确要求重试或重新分配，且仍在原 Task 上处理。来源：`worker-start --help`、`recovery-and-cleanup.md`。
- `worker-start` 的成功/ready 或 input accepted 不能简单解释成 turn 已启动：按回执 `next`、turnStart 及 worker 的实际能力核验，不能基于静默重复派发。来源：本机 `worker-start --help` 和本项目 [启动与恢复](../../skills/orchestra-auto/references/worker-start-and-recovery.md)。
- 新 worktree worker 默认保留供用户使用；其他已完成 worker 通常自动关闭，具体按 `worker-list` 和回执 `next`，用户保留的 session 不要擅自关闭。来源：`worker-start --help`、本机 `orchestration` 指南。

## 对重新设计通道的启示与待验证项

1. 将**工作路由**（研究、澄清、规格、实施）与**执行拓扑**（直接、宿主子代理、Orca 监督、完整移交）分开判断。Orca 解决委派、收发和生命周期，不会判断需求是否应该 grill；这需要项目自己的入口判据和证据。
2. 产品 spec、worker Task spec 和代码任务票是不同产物：不要因必须先写 worker spec 就假定产品需求已澄清；可以委派一个任务去做 grill。
3. 有意采用更强隔离、人工决策和验收门是项目政策，应说明理由及成本；避免误称为 Orca 固有约束，必要时通过小范围验证比较其他拓扑。
4. 本次没有执行真实的 ask/reply、恢复、跨 host 或并发 dispatch，以上是指南和帮助契约而非端到端实验结果；涉及版本差异或故障恢复时应在受控测试环境单独验证。
