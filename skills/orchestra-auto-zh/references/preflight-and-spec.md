# 派活预检、Worker Spec 契约与规划门禁

**什么时候读**：编写 worker spec 前、每次派活前预检（`§4.0.1`）、拿到回执后复核（`§4.0.2`）、收尾格式化检查（`§4.0.3`），或验收规划门禁与六条散文锚点（`G-docs`、`G-wayfinder`、`G-spec`、`G-tickets`）。

## 预检回执（`preflight.py`）

每次派活前**必须**先写入一条持久 `kind: preflight` 回执；传入 `--spec` 可在单次命令中机械校验五要素、`BEHAVIOR RULE`、`前置 skill` 磁盘路径存在性、`票型：`、`门禁：`、`测试档：` 与 `code-review`：

```bash
PREFLIGHT="$SKILLS_ROOT/orchestra-auto-zh/scripts/preflight.py"
# 规划分支示例（R1–R10 / R14–R16，如 R2 grill-with-docs -> design / G-docs）：
python3 "$PREFLIGHT" inspect-spec --spec /tmp/worker-spec.md --type design --compact
python3 "$PREFLIGHT" check --run-id "$RUN" \
  --ticket-id 42 --type design --gate G-docs --origin "GH#42" \
  --spec /tmp/worker-spec.md \
  --guide /tmp/orchestration-guide.md --guide-fetched-at "$FETCH_TS" --round-started-at "$ROUND_TS" \
  --channel-note "channel B (R2 grill-with-docs)" --worktree-note "new-top-level" \
  --long-horizon-note "single stage" --main-rule-note "clean" --canonical-note "worktree" \
  --legacy-migrated --migration-ref "native-only" --compact

# 实施分支示例（R11–R13，如 R12 implement -> impl / G-impl）：
python3 "$PREFLIGHT" inspect-spec --spec /tmp/worker-spec.md --type impl --compact
python3 "$PREFLIGHT" check --run-id "$RUN" \
  --ticket-id 42 --type impl --gate G-impl --origin "GH#42" \
  --spec /tmp/worker-spec.md \
  --guide /tmp/orchestration-guide.md --guide-fetched-at "$FETCH_TS" --round-started-at "$ROUND_TS" \
  --channel-note "channel B" --worktree-note "new-top-level" \
  --long-horizon-note "single ticket" --main-rule-note "clean" --canonical-note "worktree" \
  --legacy-migrated --migration-ref "native-only" --compact
```

传统 `--ticket ticket.json --context context.json` 仍完全兼容。回执写入 `items`、`blocking`、`overrides`（`item`、`by`、`reason`）与 `post_dispatch`；非零退出或 `record.written == false` 均阻断派发（`message-capability` 不可豁免）。看板写失败只记台账 gap，不阻断派发。

**两项独立阻塞机械探测（先于 12 条核对单执行）：**
- **`live-settings`**（mechanical）：从 `orca agent hooks status --json` 读取 `result.settingsPath`，对其目录内所有非备份 `*.db` 只读探测 `profile_state_documents`（`domain = 'settings'`），提取 `defaultTuiAgent` 与禁用 TUI agent 列表（`disabledTuiAgents`；version-scoped 版本限定：Orca CLI `1.4.244` 于 `2026-10-29`、`1.4.265` 于 `2026-11-20`，`1.4.277` 于 `2026-11-26` 复核）。绝不回退读 `orca-data.json` 或备份文件；详见 [preflight 实现细节](../../../docs/design/preflight-internals.md)。用户点名 agent 且未禁用则优先，help 示例不是穷举（`example list is not exhaustive` / 示例不是穷举：启用值交由 Orca 校验，遇 `agent_unconfigured` 立即停下且不替换 agent）。
- **`message-capability`**（mechanical，不可豁免）：探测 `--version`、`skills get orchestration`、`check --help`（`--retry-request`）、`inbox --help`（`--terminal`、`--full`），并校验结构化会话（`ORCA_STRUCTURED_SESSION=1` + `ORCA_AGENT_SESSION_ID` + `status --json`）或 TUI 终端绑定（`ORCA_TERMINAL_HANDLE` + `ORCA_AGENT_PANE` + `terminal list --json` + `inbox --terminal <handle>`）与旧收件器迁移证明。若新版 CLI 仅变更措辞，必须由人类明确审阅（`context.messaging.human_verification`）后才放行。注意：`orca bot`（`send --to bot:<name>`）是代表主人执行的独立助手通道，不可替代受监督的 `worker-start`。

## 前置 Skill 选择与调用方式

**按阶段与票型选 `前置 skill`，不按通道固定**：`grill-with-docs`、`research`、`to-spec`、`to-tickets`、`implement-spec`、`diagnosing-bugs` + `implement`、`code-review`。落笔前先核实包装层与所有依赖 `SKILL.md` 路径在磁盘可读；缺路径**按 §9 带证据报告**，不从记忆编路径、不私自安装。只写已确认受宿主支持的调用方式（未知写未确认），绝不把直接读文件报成原生调用。`setup-matt-pocock-skills`（`disable-model-invocation: true`）须显式写路径并在隔离 worktree 中运行。

## Worker Spec 精简模板（`BEHAVIOR RULE` 与五要素）

```text
【BEHAVIOR RULE（原样遵守，不得删减）】
1. 提问：一律用 `orchestration ask`（禁 AskUserQuestion）；open 决策 ask 回来，不替人拍板。
2. 范围与收尾顺序：只改 Ownership 内的文件。处理完已送达 follow-up → 写 Answer 与实测证据 → 提交、推送并用 `--body-file` 创建或更新 PR（PR 号、body 路径与 CI 读数记入 GitHub 与仓库外台账，**不为携带这些事实追加项目提交**；**仓库内 Answer 只记录交付了什么与所在分支**，**最终 PR HEAD 的 CI 仍由 coordinator 在合并前验收**——仅当用户或仓库明确要求把该事实写进仓库时以该规则为准）→ 从自己终端按 live preamble 原命令发 `worker_done`（真实 Task ID、Dispatch ID、succeeded/failed、三句摘要）→ 核实回执 → 立即 idle。保存回执，重发前先查 Dispatch；`consumer_fenced` 立即停下。
3. 升级门禁（escalation）：凡涉及人类决策、凭证、第三方账号、付费服务、人工审批或不可逆 live 数据操作，立即升级，不得代办。
4. 术语：描述架构/设计时用 deep-module / codebase-design 词汇；源码、测试与报告沿用仓库术语。
5. 边界：越出 Ownership/授权范围必须升级。
6. 红绿与格式安全：在隔离副本/基线 worktree 取红证据；还原或格式化前备份未提交工作；交付前核查 `git status` 与 `git diff --numstat`；异常大 diff 按恢复规则逐文件处理。

前置 skill：
- 名称：<本阶段 skill>
- 显式调用：<支持形式或未确认>
- 文件位置：<实际绝对路径>/SKILL.md
skill 执行要求：本任务明确要求使用该 skill。优先宿主显式调用，否则按规则回退读取并遵循配套文件；直接读取不得报为原生调用。
票型：research | design | spec | ticket-splitting | impl | docs
门禁：<只写适用门禁，如 G-impl, G-merge>
测试档：<窄（写明为何影响面可圈定及可直接跑的选择器） | 宽（全量门禁及理由）；实施票必填>
Target：<文件/模块>
Change：<具体交付结果>
Constraints：<不许逾越的边界>
Ownership：<可编辑范围；票据正本在任务 worktree 内，worker 负责 Status 与 Answer 追加>
Observable acceptance：<可核实测试命令与红→绿证据；实施票须逐字含双轴 `code-review`（除非 spec 与 Answer 同写豁免理由）；涉项目 PR 时写明目标仓库与 base（fork 为 upstream 默认分支，绝非 fork 自己的 main）、`--repo ... --base ... --head ...`、PR body 章节与 `worker_done` 回执；须 Orca 截图时附 `visual-evidence.py` 要求与 `references/visual-evidence.md`>
skill 执行证据：<名称、路径、加载方式、可核查记录与关键产物>
提问通道：orchestration ask（禁 AskUserQuestion）
```

长程票图（`implement-spec`）额外附 `tdd`、`code-review`、`pr` 路径以及 `Tracker`、`Spec`、`Tickets`、`Integration branch`（要求回报图 frontier、逐票 branch/commit、唯一集成 PR 与 `Closes`、Door 声明；采用逐票 TDD + integration branch 一次 `code-review`）。

## 三个检查点：派前（§4.0.1）、派后（§4.0.2）、收尾（§4.0.3）

**只有 §4.0.1 能阻止派活**；§4.0.2 阻止盲等或撤销临时豁免；§4.0.3 在 `worker_done` 后卡住过关。

### 4.0.1 派活前：12 条核对清单

- [ ] **CLI 存活**（`cli-app-up`、`cli-command-surface`；mechanical）：`<ORCA>` 可用，`orca status --json` 与最小 `worker-list` 正常返回。
- [ ] **本轮已拉取指南**（`guide-freshness`；mechanical）：`skills get orchestration` 拉取时间 >= 本轮开始时间（旧盘面不算）。
- [ ] **通道已定**（`channel-decided`；judgment）：若先做过只读探索，按新信息复判通道。
- [ ] **票型已定且入 spec**（`ticket-type`；judgment）：写进 `票型：` 行；文档票只列文档门禁。
- [ ] **Spec 五要素完整**（`spec-elements`；judgment）：Target / Change / Constraints / Ownership / Observable acceptance + `BEHAVIOR RULE` + 可读 `前置 skill` 路径 + 执行证据。
- [ ] **worktree 参数正确**（`worktree-parameters`；judgment）：`--worktree new-top-level --name <slug> --agent <已启用>`（跨 cwd 补 `--repo`，fork 显式 `--base-branch upstream/<默认>`）。
- [ ] **长程票图已就绪**（`long-horizon-map`；judgment）：票图完整、tracker 可达、`implement-spec`、唯一集成 PR（嵌套隔离能力本身归 §4.0.2）。
- [ ] **main 铁律自检通过**（`main-worktree-rule`；judgment）：本轮绝不改动主工作区代码或提交（仅合并后 `--ff-only` 同步）。
- [ ] **票据正本位置明确**（`ticket-canonical-copy`；judgment）：正本在任务 worktree 内，main 上只读验收。
- [ ] **门禁与测试档可判定**（`gates-decidable`；judgment）：实施票必含 `测试档：` 行；除非满足条件（单张票 + 方案已定 + 影响面可圈定且理由同写入 spec 与 Answer 设为 `provisional`），必含 `code-review`。
- [ ] **基线对齐 base commit**（`baseline-source-commit`；mechanical）：spec 中实测基线日志来自本票同一 base commit（或显式标注仅作量级参考）。
- [ ] **格式化预防已覆盖**（`formatter-prevention`；judgment）：写入第 6 条边界，仅确有重排风险时加本地忽略配置。

### 4.0.2 派活后：启动证据与观测记录

以下事实在 **Dispatch 存在之前**并不存在，拿到回执后立即调用 `preflight.py post-dispatch` 记录：
- 核实 `turnStart`（见 `dispatch-and-comms.md`），无则看终端/屏幕/`worker-read`，绝不盲等或对其乱补 Enter。
- 长程 worker 须在首份报告中证明能建 隔离 worktree 与分支而不碰 main。
- 记录 `review-exemption-recheck`（用真实 worktree 的 `git diff --numstat` 复核预期文件与行数，超界即撤销豁免但不改写预检回执）、`dashboard-backfill`（失败只记 gap）与 `inbox-health`（不豁免 `message-capability`）。

### 4.0.3 收尾时：格式化重排噪声检查

在 `worker_done` 之后、通过 `G-impl` 前，在任务 worktree 执行 `git status --porcelain` 与 `git diff --numstat`：干净过关；少量改动追问清楚；万行级整份**重排**噪声按 [recovery-and-health.md](recovery-and-health.md) 存证并逐文件恢复。

## 规划门禁（`G-docs`、`G-wayfinder`、`G-spec`、`G-tickets`）

所有受控规划产物（`docs/adr/*.md`、`GLOSSARY.md`、`GLOSSARY-MAP.md`、Spec、票据文件与 `gh issue create` / tracker 写入）均由隔离 worktree 中的 worker 产出，协调者绝不直接编写或建票。协调者随口问几个宏观选择题绝不满足 `G-docs`。

- **`G-docs` / `grill`**：核查 worker 驱动的真实 `grill-me` / `grill-with-docs` 执行证据（问题/回答/依据、落地的 `GLOSSARY.md` 术语表与 `docs/adr/` ADR）。任何 ADR 落盘、`to-spec` 或 `to-tickets` 前必过本门，除非用户显式跳过。
- **`G-wayfinder`**：票据四状态为`可领取`、`处理中`、`阻塞`、`完成`。牢记：**frontier 为空不等于完成**——可能是全部票已分配、剩余均被阻塞或真正做完；`frontier 为空但有票在「处理中」或「阻塞」`时**不得**结束该阶段。决策树只作索引，阐明转向 §7/实施还是止于决策；分支（`research` AFK、`prototype` HITL、`to-questionnaire` 异步、`wizard` 人类、`task`）未落地前被阻塞票保持阻塞。
- **`G-spec`**：尽量高/少的接缝（接缝选址属架构分叉，须升级人定）、充分场景、非空 Out of Scope。
- **`G-tickets`**：纵切 tracer bullets、无环 DAG、先落 tracker、单上下文可消化；拆分粒度方案用“票数 + 每票交付物”给人类拍板。

## 散文契约：六条不得漂移的锚点

1. **`absent` 不是没发生的证明** —— `request-show` 回 `absent` 只表示未知该键，先盘点 Orca 与 git 再判定（[ledger-and-dashboard.md](ledger-and-dashboard.md)）。
2. **看板故障绝不阻断派发** —— `progress.py` 写入/锁/浏览器故障只记台账 gap，`open` 不在派发关键路径。
3. **`worker_done` 不是验收** —— 只证明结算；验收要可观察结果与证据（`grep` 只证字符串存在；健康状态不授权 release）。
4. **仅结算且有收尾记录后才 release** —— 必须有已结算 `worker-show` 与持久 `kind: closeout`（未结算绝不 release）。
5. **`ready` 不是已启动轮次** —— 须见 `turnStart: observed` 或产物/状态投影/屏幕证据。
6. **`unknown` 不是失败** —— 缺证据或读不到权威时下一步是取证，不是盲重派；空等 != 空队列。

**测试范围说明**：`tests/test_package_content.py` **只查锚点，不等于完整语义等价**——保留短语清单**不是翻译**，也不证明段落间无漂移，审阅者仍须核查 diff。
