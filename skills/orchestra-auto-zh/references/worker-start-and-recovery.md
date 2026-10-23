# Worker 启动与 TUI 等待

**何时读取**：`worker-start` 没有确认 `turnStart: observed`，或 worker 出现权限提示、长时间无进展。
以当前 Orca 的 `worker-show`、`worker-read` 和 recovery 指南为准；下面的标题经验只适用于有终端的 TUI worker。

## 确认 turn

`state: ready` / `stage: input_accepted` 不证明模型已开始。`turnStart: observed` 则证明：它就是回执
自带的启动证据，不需要在它之上再叠取证——下面的步骤用于 `unsupported`、`unobserved` 或字段缺失、
即 agent 无法回报 turn 开始的场合。先用 `orca orchestration worker-show --dispatch <id> --json`
查看实际 worker 与终端句柄。无终端 worker 通过 `worker-read --source auto`、状态投影与产物进展判断，
不调用 terminal 命令。

有终端时读 `orca terminal show --terminal <handle> --json`。`Pi ready` / `OMP ready` 等空闲横幅
只说明尚无开始证据；`opencode` 可观察 `OC | <标题>`，`codebuddy` 标题可能一直为 `Terminal 1`，
需结合屏幕或会话文件进展。标题形态随版本变化，不能单凭一次标题翻转判断工作已完成。
空闲标题没变化是「去看屏幕」的信号，不是判决：它既不能证明 turn 已开始，也不能证明它没开始。

若屏幕确认完整 preamble 停在输入框，且当前未运行 turn，可对**原终端**补一次裸 Enter，
随后复核状态。输入框为空、内容不完整或状态不明时，按 Orca recovery 指南取证，
不重发整份 spec，不循环 Enter，不创建重复 Dispatch。

## 权限与失联

`agentWait` 或屏幕显示权限对话框时，先读请求的具体操作、目标路径及风险。
只处理本轮已授权的操作；需要额外权限、第三方账号、付费或不可逆操作时按 §5 提问或升级。
不得把 Enter 当作通用批准，也不为避免提示而切换到 bypass 模式。
完成处理后复核 `agentWait` 与 worker 进展。

标题回到空闲而 Task 未结算时，结合 worktree 进展、worker-show、worker-read 和子任务产物
判断是否失去唤醒。只有 recovery 指南确认终端仍属于原 active Dispatch 且允许恢复，
才发送一次明确的续跑指令；仍无进展则保留产物并报告阻塞。
