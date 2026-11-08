# Matt Pocock skills：工作方式与本项目适配评估

研究日期：2026-11-08。上游仓库 `mattpocock/skills` 的 HEAD 为 [`4ae2317ddb22b046e26455e4f9c9b3aaaf62212d`](https://github.com/mattpocock/skills/tree/4ae2317ddb22b046e26455e4f9c9b3aaaf62212d)。本机 `~/.agents/skills/` 中 `grill-with-docs`、`grilling`、`to-spec`、`to-tickets`、`implement`、`implement-spec`、`wayfinder`、`domain-modeling`、`tdd`、`diagnosing-bugs`、`code-review` 的 `SKILL.md` 与该版本逐字节相同；这只说明本机此时的文件一致，不保证其他机器或以后版本一致。以下链接固定该 commit，避免 `main` 漂移。

## 核心结论

- 上游 [README](https://github.com/mattpocock/skills/blob/4ae2317ddb22b046e26455e4f9c9b3aaaf62212d/README.md) 将 grill 视为防止需求错位的常用起点，建议每次改动都使用。它是一组可组合的小工具，不声称所有工作必须走一条不可分支的流水线。
- [`grill-with-docs`](https://github.com/mattpocock/skills/blob/4ae2317ddb22b046e26455e4f9c9b3aaaf62212d/skills/engineering/grill-with-docs/SKILL.md) 本身是包装器，调用 `grilling` 与 `domain-modeling`；前者要求用户参与决定，后者记录实际收敛出的领域语言和必要的 ADR。它不是“产出一份固定模板设计文档”的同义词。
- [`to-spec`](https://github.com/mattpocock/skills/blob/4ae2317ddb22b046e26455e4f9c9b3aaaf62212d/skills/engineering/to-spec/SKILL.md) 汇总现有对话与代码认知，而非再次访谈；不过它明确要求与用户核对测试 seam。这不是与“不访谈”矛盾，而是具体设计校验。
- [`to-tickets`](https://github.com/mattpocock/skills/blob/4ae2317ddb22b046e26455e4f9c9b3aaaf62212d/skills/engineering/to-tickets/SKILL.md) 可从对话、计划或 spec 开始，并不要求必有 spec；把工作切为可验证的纵向切片、声明真实阻塞关系，先请用户确认粒度和依赖，再发布票据。大范围机械重构有 expand–contract 例外。
- [`implement`](https://github.com/mattpocock/skills/blob/4ae2317ddb22b046e26455e4f9c9b3aaaf62212d/skills/engineering/implement/SKILL.md) 面向 spec 或票，尽可能在商定的 seam 用 TDD、运行类型检查和测试，完成后 review 并提交。上游文字并非“任意小改动都自动视作已有 spec”。
- [`implement-spec`](https://github.com/mattpocock/skills/blob/4ae2317ddb22b046e26455e4f9c9b3aaaf62212d/skills/engineering/implement-spec/SKILL.md) 要求 spec 和关联票图：每票一个新上下文、独立 worktree 的 implementer；以阻塞关系驱动 frontier 并发；集成分支合入后只做一次整体 review、修复、清理。不是 coordinator 自行按固定顺序一票票重复完整 review。

## 如何进行一次真正的 grill

[grilling](https://github.com/mattpocock/skills/blob/4ae2317ddb22b046e26455e4f9c9b3aaaf62212d/skills/productivity/grilling/SKILL.md) 把设计建模为依赖树。每轮列出当前所有前置条件已满足的决策问题，编号并给推荐，等待用户回答后重新计算下一轮；不能提前猜尚未决定的前置项。事实由 agent 查证，可并行探索，不让用户代查；实际决策由用户作出。frontier 为空后，还需用户确认共同理解后再行动。用户已有明确决定可以作为上下文，不等于协调者可以假扮用户决定。

[domain-modeling](https://github.com/mattpocock/skills/blob/4ae2317ddb22b046e26455e4f9c9b3aaaf62212d/skills/engineering/domain-modeling/SKILL.md) 要求即时挑战术语、用场景试探边界、与代码核对；术语达成一致时按需写 `GLOSSARY.md`，而非预先创建空文件。ADR 必须同时满足难以撤回、缺少上下文会令人意外、有真实取舍这三点，才建议创建。需尊重目标仓库配置的实际路径。

[wayfinder](https://github.com/mattpocock/skills/blob/4ae2317ddb22b046e26455e4f9c9b3aaaf62212d/skills/engineering/wayfinder/SKILL.md) 用于大到一个会话无法容纳且路径不明的工作，默认解决决策而非实施。其 research 是 AFK，prototype 与 grilling 是 HITL；明确指出 agent 替人回答 grilling 会破坏 HITL。故 worker 消息可以由 coordinator 做事实查证和转述，但产品决策与共同理解确认应真实到达人类；能由用户已有明确指令解决的问题无需重复确认。

## 技巧与边界

1. 先检查当前请求、现有 issue/spec、代码、术语及 ADR；区分“用户已经决定”与“还需要决定”，不能把模型的推荐伪装成用户的选择。
2. grill 问彼此独立的当前决策，推荐要说明理由；依赖前一答案的问题放下轮。查证事实与可独立推进的工作不必因问题阻塞。
3. 小任务可按用户和运行环境约束采用轻量澄清，但这属于本项目的适配选择，不能声称上游 README 建议小改动跳过 grill。已有有效 spec/票是续跑入口，不证明当前新需求已经被 grill。
4. spec 发布前检查测试 seam；票图发布前与用户核对切片、阻塞和粒度；用户已明确批准相同决定可复用，发生实质变化时重新确认。
5. 每次交接携带实际原始 skill 路径及对话、issue、研究和提交等上下文指针；跨上下文不能只发“调用 grill”，否则会丢失设计决定和用户声音。

## 基线与 orchestra-auto 的差异及需验证问题

下表对照调查开始时的基线提交 `51d3cb2`；工作区随后修正了部分规则，当前状态以对应文件为准。

| 当前本项目规则 | 对照上游的判断 |
| --- | --- |
| [通道 B](../../skills/orchestra-auto-zh/SKILL.md) 对边界清楚的小改动直接实施；完整实施通常才 grill | 比上游 README 的建议更宽松。若执行者只凭“小”推断清楚、未核验已知决定及未决问题，就可能静默略过对齐。这是风险推断，不能据此断定某次运行实际漏做。 |
| [应答协议](../../skills/orchestra-auto-zh/references/reply-protocol.md) 决策问人、实现细节代答 | 与避免让用户代查事实的方向一致；但任何未由用户授权的实际设计决策不能由 coordinator 自答。worker 的推荐不是用户确认。须特别保护最终共同理解确认及 `to-spec` 的 seam、`to-tickets` 的票图确认。 |
| [G-docs](../../skills/orchestra-auto-zh/references/gates-docs-planning.md) 要求模板设计文档和 `CONTEXT.md` | 上游 `grill-with-docs` 只组合两个 skill，不保证固定设计文档；当前上游领域术语为 `GLOSSARY.md`，且按需创建。固定文件、模板是项目附加合同，应明确其来源和无产物时的通过方式，避免与上游实际行为冲突。 |
| [阶段映射](../../skills/orchestra-auto-zh/references/dispatch-loop.md) 把 `implement` 用于边界清楚的小改动 | 上游描述输入为 spec 或票；若本项目允许用户原话构成工作说明，应明确其证据及与上游的适配，不应暗示上游明确允许无上下文直接实施。 |
| 线性链 grill → spec → tickets → build | 是本项目选择的常见执行路线；上游 `to-tickets` 允许直接消费已充分讨论的对话或计划，也有 wayfinder 等分支。resume 台账的顺序不应成为证明实际发生过用户对齐的替代证据。 |

以上是文档与原文对照，不是对实际历史运行的审计，也不证明任何特定 worker 做过或没做过 grill。
