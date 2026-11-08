# Matt Pocock skills：完整路由与组合调查

调查时间：2026-11-08。固定官方仓库提交 [`4ae2317ddb22b046e26455e4f9c9b3aaaf62212d`](https://github.com/mattpocock/skills/tree/4ae2317ddb22b046e26455e4f9c9b3aaaf62212d)；远端 HEAD 与本次取样一致。本文是对该版本的研究快照，运行时仍需读取实际安装版本；下文“本项目”指 orchestra-auto，不把其约束当作上游原义。

## 先看整体，而非只看一条链

上游把 skills 设计成可组合的能力；[ask-matt 路由](https://github.com/mattpocock/skills/blob/4ae2317ddb22b046e26455e4f9c9b3aaaf62212d/skills/engineering/ask-matt/SKILL.md)区分主流程、进入主流程的 on-ramp、独立能力、下层词汇和上下文边界。它自己也要求在解释某个 skill 或建议跳过之前读该 skill 原文。因此以下路径是上游建议，不是所有项目都必须执行的协议。

| 当前情形 | 上游路径或能力 | 交接重点 |
| --- | --- | --- |
| 无工作目录，想把计划、文字或设计谈清楚 | `grill-me → grilling` | 不建立领域文档；重点是用户参与的访谈。 |
| 有工作目录，构想可在一轮 session 内收敛 | `grill-with-docs → grilling + domain-modeling` | 访谈和领域建模同时进行，按需即时记录术语和 ADR。 |
| 单靠讨论无法判断 UI 或状态模型 | `handoff → prototype → handoff` | 产物是回答一个问题的可丢弃原型；保留原型为证据，把结论带回原会话。 |
| 调研资料才能做决定 | `research` | 新鲜上下文的后台 agent 查一手资料，产出带引用的研究；研究服务于决定，不替用户决定。 |
| 目的地可描述，但规划本身超出一个 session 且路径迷雾重 | `wayfinder` | 先画动态决策地图；大部分情形地图收敛后经 `to-spec`，而非把决策票直接当实施票。 |
| 多 session 的构建已经谈清 | `to-spec → to-tickets → implement` 或 `implement-spec` | 单票新上下文手动实施，或一张票图在 integration branch 并发实施。 |
| 单 session 内可完成的改动已经谈清 | `implement`，保留当前上下文 | 上游路由并不要求对每个小任务先产出 spec 与票图。 |
| 外部提交的 issue / PR 需要澄清 | `triage` | 有需要才 grill；`to-tickets` 产出的票按上游定义已经可供 agent 领取，不要重复 triage。 |
| 难复现 bug、性能回归 | `diagnosing-bugs → retro` | 先构造针对当前故障的紧反馈，再假设、修复和回归；并非每个修复都等同需要全规划。 |
| 需要设计模块、术语或改进环境 | `codebase-design`、`domain-modeling`、`improve-codebase-architecture`、`retro` | 分别负责模块设计词汇、业务词汇、发现架构机会与改善工作环境。 |

来源：[ask-matt](https://github.com/mattpocock/skills/blob/4ae2317ddb22b046e26455e4f9c9b3aaaf62212d/skills/engineering/ask-matt/SKILL.md)、[README](https://github.com/mattpocock/skills/blob/4ae2317ddb22b046e26455e4f9c9b3aaaf62212d/README.md)。前者将 grill 置于有目录的主流程开头，后者建议每次改动都做对齐；本项目对“明确小改动直接实施”的宽松分支是本项目适配，不是上游建议原文。

## grill-me、grilling 与 grill-with-docs 不是同义名字

- [`grill-me`](https://github.com/mattpocock/skills/blob/4ae2317ddb22b046e26455e4f9c9b3aaaf62212d/skills/productivity/grill-me/SKILL.md)：**用户主动调用**的独立入口；包装器只调用 `grilling`。这是当前有效名称，不是 `grilling` 的历史别名。`ask-matt` 将它定位在没有工作目录的讨论场景。
- [`grilling`](https://github.com/mattpocock/skills/blob/4ae2317ddb22b046e26455e4f9c9b3aaaf62212d/skills/productivity/grilling/SKILL.md)：可由模型调用的访谈原语。设计树中前置条件已满足的问题形成 frontier；同一轮编号、给建议并等待用户答复，然后重算 frontier。事实由 agent 探索；决定由用户作出；frontier 为空后仍须确认共同理解，才执行。
- [`grill-with-docs`](https://github.com/mattpocock/skills/blob/4ae2317ddb22b046e26455e4f9c9b3aaaf62212d/skills/engineering/grill-with-docs/SKILL.md)：**用户主动调用**的另一个包装器，分别调用 `grilling` 与 `domain-modeling`；实际需要记录领域词汇或 ADR 时依据目标仓库约定落盘。

上游[指南](https://github.com/mattpocock/skills/blob/4ae2317ddb22b046e26455e4f9c9b3aaaf62212d/docs/engineering/grill-with-docs.md)承认包装器只有一行，依赖可能没有加载；更严重的是大部分决策只存在对话里，术语表不是 spec，也不是每个决定都应写 ADR。上游建议在同一会话将对话交给 `to-spec`，并对照用户答案检查 spec。**推断**：跨 worker/上下文编排时，单独传 wrapper 的路径不能证明这两个依赖真的被加载，也不能保证数值、负向约束、先后顺序等原始决定被保存；需要分别验加载证据及原话到下游产物的可追踪性。

## wayfinder：从迷雾中找到路，而不是按图施工

判据是**规划本身**超过一个 agent session 且通往 destination 的路线不清；不是任务文件数多、需求多、需要多个实施 worker 就自动满足。它的产物是一个 tracker map 与动态决策票图；除 Notes 明确覆盖的情形，默认 plan, don't do。来源：[wayfinder 原文](https://github.com/mattpocock/skills/blob/4ae2317ddb22b046e26455e4f9c9b3aaaf62212d/skills/engineering/wayfinder/SKILL.md)、[wayfinder 指南](https://github.com/mattpocock/skills/blob/4ae2317ddb22b046e26455e4f9c9b3aaaf62212d/docs/engineering/wayfinder.md)。

### Chart：先定位与铺图

1. 调用 `grilling` 和 `domain-modeling`，先让用户参与明确 destination；再广度优先探索，识别可以清楚表达的问题和仍无法精确定义的 fog。
2. 若没有 fog 且一 session 能装下，停止画图并请用户选择接下来如何做。否则建一个地图 issue：Destination、Notes、Decisions so far、Not yet specified、Out of scope；地图只索引，决定细节留在票里。
3. 已能清楚表述的问题建子票，即使当前被阻塞；无法准确表述者留在 Not yet specified。先创建 issue，再用真实 ID 连阻塞边和交叉引用。地图和票只带 `wayfinder:` 标签，不用 `ready-for-agent`。
4. 为创建的 research 票开启新上下文 subagent 并行研究，研究保存在 `research/<name>` 临时分支、从票链接，不开 PR；chart session 到此停止，不亲自顺手解决其他票。

### Work：一张票一个工作单元

读取低分辨率地图；选择指定票或第一个 open、unblocked、unclaimed 的 frontier 子票，先分配给执行者作为 claim。按标签选择能力；完成后在 resolution comment 记录答案、关闭 issue、在地图追加链接和简要结论；将新清晰的问题从 fog 毕业为新票，按需修订其他票或标记超出范围。**除 research 外，一个 session 不解决超过一票。**

| 决策票类型 | 协作 | 对应动作 |
| --- | --- | --- |
| `grilling` | HITL | 默认类型，调用 `grilling` 与 `domain-modeling`，用户亲自回答。 |
| `prototype` | HITL | 可运行草稿帮助用户判断外观或行为，票链接原型。 |
| `research` | AFK | 新鲜上下文研究外部资料、知识库等事实，研究 agent 可并行。 |
| `task` | HITL 或 AFK | 为做决定解除阻塞的必要动作，如获取接入或查看数据；不是实现交付物的票。 |

HITL 决策必须来自真实用户交流，agent 自问自答破坏该类型。frontier 空只说明当前没有可领取票，不证明全部问题完成；应区分已领取、被阻塞、已解决以及仍在 fog 的内容。地图收敛后，通常用 `to-spec` 收拢链接的决定，再 `to-tickets` 形成实施图；只有工作最终确实很小，`ask-matt` 才建议可直接进入 `implement`。

**本项目差异与验证点**：项目已在 [SKILL.md](../../skills/orchestra-auto-zh/SKILL.md) 和 [G-wayfinder](../../skills/orchestra-auto-zh/references/gates-docs-planning.md) 提到 wayfinder，不能说完全不存在；但需要逐项检验入口可发现性、chart/work 两种模式、按标签分派、研究分支处理、每 session 一票、用户参与和地图完成后的 spec 交接是否都真正落地。将其只当作 `grill → spec → tickets` 前的一句可选提示，会遮蔽这套动态路由。它的决策票图和 `to-tickets` 的实施图的 frontier、完成信号与授权不同，不能互换。

## 上游上下文与权限约定

[`ask-matt`](https://github.com/mattpocock/skills/blob/4ae2317ddb22b046e26455e4f9c9b3aaaf62212d/skills/engineering/ask-matt/SKILL.md) 建议 grill、spec、拆票尽可能保留在同一个未清空上下文，按票实施再用新上下文；接近上下文能力边界时在阶段边界压缩。跨 harness、目录、同事或阶段中途分叉才考虑 `handoff`。编排采用多 worker 时，这是与上游的显式权衡，而不是天然等价；handoff 要覆盖决定原话、依据、未决问题、来源和用户确认。

用户主动调用的 skill 声明 `disable-model-invocation: true`，Codex 对应元数据也关闭隐式触发；model-invoked skill 可自动到达。上游 [CHANGELOG](https://github.com/mattpocock/skills/blob/4ae2317ddb22b046e26455e4f9c9b3aaaf62212d/CHANGELOG.md) 在 #878 强调显式调用可组合依赖，#880 纠正其他 skill 不能通过 Skill tool 调用 user-invoked `setup-matt-pocock-skills`；缺设置时是提示人运行。**项目需要分别处理**用户明确点名、宿主允许的显式委派，以及仅凭其他 skill 的文字试图自动触发，不能把读取绝对路径宣称为上游调用语义等价。

## 能力清单与版本命名

以当前 `SKILL.md` frontmatter 为准，公开 engineering 与 productivity 下列出：

| 分类 | User-invoked | Model-invoked |
| --- | --- | --- |
| Engineering | `ask-matt`, `grill-with-docs`, `implement`, `implement-spec`, `improve-codebase-architecture`, `retro`, `setup-matt-pocock-skills`, `to-spec`, `to-tickets`, `triage`, `wayfinder` | `code-review`, `codebase-design`, `diagnosing-bugs`, `domain-modeling`, `pr`, `prototype`, `research`, `tdd`, `wizard` |
| Productivity | `grill-me`, `handoff`, `teach`, `to-questionnaire`, `wait-what` | `grilling`, `writing-for-agents` |

还包括 misc 的 `git-guardrails-claude-code`, `migrate-to-shoehorn`, `scaffold-exercises`, `setup-pre-commit`，以及 in-progress 的 `chief-of-staff`, `claude-handoff`, `loop-me`, `setup-ts-deep-modules`, `software-factory`, `writing-beats`, `writing-fragments`, `writing-shape`。它们并不全属于本项目规划流程；in-progress 不应自动视为稳定契约。目录见 [engineering](https://github.com/mattpocock/skills/tree/4ae2317ddb22b046e26455e4f9c9b3aaaf62212d/skills/engineering)、[productivity](https://github.com/mattpocock/skills/tree/4ae2317ddb22b046e26455e4f9c9b3aaaf62212d/skills/productivity)、[misc](https://github.com/mattpocock/skills/tree/4ae2317ddb22b046e26455e4f9c9b3aaaf62212d/skills/misc)、[in-progress](https://github.com/mattpocock/skills/tree/4ae2317ddb22b046e26455e4f9c9b3aaaf62212d/skills/in-progress)。

固定版本没有名为 `plan-to-issues` 的 skill；[CHANGELOG #464](https://github.com/mattpocock/skills/blob/4ae2317ddb22b046e26455e4f9c9b3aaaf62212d/CHANGELOG.md) 记录的是 `to-plan` 与 `to-issues` 合并为 `to-tickets`、`to-prd` 改名为 `to-spec`，`decision-mapping` 改名为 `wayfinder`。这些是上游记载的历史，不能据此断言任意环境中的第三方同名 skill 行为相同。
