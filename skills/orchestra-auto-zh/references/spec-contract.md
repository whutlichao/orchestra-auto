# spec 契约

**什么时候读**：准备给 worker 写 spec 时，任何通道、任何票型；或者要把一个规划阶段交给新上下文、
需要知道交接必须携带什么时。

## 目录

- [4.4 spec 契约](#44-spec-契约)
- [规划上下文与交接](#规划上下文与交接)

### 4.4 spec 契约

Guide 的 Task-spec contract 五要素必须齐：**Target / Change / Constraints / Ownership /
Observable acceptance**。外加第一条固定为：

```text
前置 skill：
- 名称：<本阶段 skill 名称>
- 显式调用：<当前 agent 支持的调用形式；未知则写未确认>
- 文件位置：<实际绝对路径>/SKILL.md
skill 执行要求：本任务明确要求使用该 skill。优先按宿主支持的方式显式调用；调用失败或不支持时记录原因。仅在宿主规则允许时读取文件并遵循流程；若宿主禁止替代执行，停止并报告阻塞。按 skill 要求处理配套文件。直接读取不得报告为原生调用。
```

**这一区块是本阶段 skill 的交接契约。** 没有它，worker 可能不会加载 grill-with-docs /
tdd / diagnosing-bugs / code-review，导致「不用其他 skills」的故障。路径写在 spec 里，
让 worker 自己加载完整内容，不要由你转述。**这条路径是本轮实际所处阶段对应的 skill**（SKILL.md §1），
不是按通道固定：已持有有效 spec 的一轮写 `to-tickets`，有完整票图的写 `implement-spec`，
修 bug 写 `diagnosing-bugs` 与 `implement`。落笔前先确认路径可解析、可读；
不可读就带证据报告，而不是编一个路径或擅自安装本轮未获授权的 skill。只写已确认受该 agent 支持的调用形式；未知写未确认，不假定 spec 中的命令会被宿主解析为原生调用。

实施类 B 通道的 spec 模板；只写本票适用的门禁。文档票按对应文档门禁改写：

```text
【BEHAVIOR RULE（原样遵守，不得转述或删减）】
1. 提问通道一律 orchestration ask，禁 AskUserQuestion；open 决策 ask 回来，不编造。
2. 你只编辑本票范围内的文件与代码。遵循 live preamble 处理原生消息及其要求的 check。
   仅在 preamble 要求时按其节奏 heartbeat；ask 超时续等同一 message ID，不重复提问、不自行替人决策。
   收尾顺序：处理已送达的 follow-up → 写 Answer/证据 → 项目改动提交、推送并创建或更新 PR
   → PR 号、PR body 路径与 PR head 的 CI 读数记入 GitHub 与本轮仓库外台账；不为携带
   这些事实追加项目提交，交付文件在创建 PR 前已定稿 → 从自己的终端按 live preamble 原命令
   发送 worker_done → 核对成功回执 → 结束回合。
   这些事实只在 PR 创建后才存在，用一笔提交去携带它，只能得到与被验收的树相同的第二次推送：
   不产生信息，却多触发一轮完整 CI。所以票据的仓库内 Answer 只记录交付了什么与所在分支，
   PR 引用放在 GitHub 与台账上。最终 PR HEAD 的 CI 仍由 coordinator 在合并前验收，
   worker 不为追它而推送。用户或仓库确有把该事实写进仓库的要求时（changelog 要写 PR 号、
   release 清单、tracker 字段、票据 Answer 必须点名 PR），以该规则为准。
   worker_done 必须带真实 Task ID、Dispatch ID、显式 succeeded/failed，三句摘要说明改动、
   发现、遗留；files-modified/report-path 仅填真实值。失败也必须报告，普通 final 不能替代。
   保存发送回执；结果不明时先查当前 Dispatch，已结算不重发，只有确认未结算且未入队才重试一次。
   consumer_fenced 立即停止，不重发、不冒用他人句柄；通信仍失败时保留产物并明确报告阻塞。
   worker_done 被接受后立即 idle，不再检查消息或开新工作；结算后的新任务必须取得新的 preamble。
   active Dispatch 内的 follow-up 指邮箱中的追加指令，不创建新 Task、不更换 preamble。
3. 需要人类 / 凭证 / 第三方账号 / 付费 / 人工审批 / 不可逆 live 数据操作 → escalation，不代办。
4. 描述设计时可用 codebase-design 词汇；源码、测试和报告中的既有术语遵循仓库语言。
5. 越出本 spec 授权范围 → escalation，不擅自扩 scope。
6. 红证据在隔离副本或基线 worktree 上取得；任何还原或自动格式化前先保存并核对全部未提交工作。
   交付前检查 `git status` 与 diff；异常大 diff 先取证并按恢复参考逐文件处理。
   仅在确认仓库确有自动重排风险时，开工前按恢复参考配置本地忽略的 formatter 覆盖项。

前置 skill：
- 名称：<本阶段 skill 名称>
- 显式调用：<当前 agent 支持的调用形式；未知则写未确认>
- 文件位置：<实际绝对路径>/SKILL.md
skill 执行要求：本任务明确要求使用该 skill。优先按宿主支持的方式显式调用；调用失败或不支持时记录原因。仅在宿主规则允许时读取文件并遵循流程；若宿主禁止替代执行，停止并报告阻塞。按 skill 要求处理配套文件。直接读取不得报告为原生调用。
Target：<文件/模块>
Change：<要产出的具体结果>
Constraints：<不许碰的边界；术语用 codebase-design 词汇>
Ownership：<本票可编辑范围；票据正本在 worktree 内，你负责 Status 流转与 Answer 追加>
Observable acceptance：<适用的测试命令与可观察结果；修复类红→绿证据；需要时含 code-review 双轴。项目文件有改动时：解析并使用本机 pr skill 编写 PR body，推分支，以 --body-file 创建或更新 PR，再把 PR 号、body 路径与 CI 读数记入 GitHub 与仓库外台账，不为它们追加项目提交（用户或仓库明确要求把该事实写进仓库时，以该规则为准）。PR 的目标仓库与 base 要写进 spec，这也是本行的一部分：写明 PR 落到哪个仓库、那边的 base 是哪个分支——fork 时是 upstream 仓库的默认分支，绝不是 fork 自己的 main——不得留给 gh 默认解析；形态是 `gh pr create --repo <upstream-owner/repo> --base <默认分支> --head <fork-owner>:<branch>`。最后发送并核实 worker_done 回执>
skill 执行证据：<名称和路径；加载方式：原生调用 / 直接读取 / 未加载；可核查的调用或读取记录，或失败原因；关键步骤及产物>
提问通道：orchestration ask（禁 AskUserQuestion）。
```

票面要求 Orca 实际渲染的截图时，spec 再加一行 `视觉证据：<plan 路径> → scripts/visual-evidence.py run --plan <plan> --report <report>`，点明禁止最小化窗口与 `set frontmost`，报告必须 `restored` 与 `focusRestored` 均为真；协议见[视觉证据](visual-evidence.md)。

长程票图下，`前置 skill` 区块指定 `implement-spec`。另加 `skill 解析：`（为 `tdd` / `code-review` 按同一调用与后备规则写名称、调用形式及绝对路径）、`PR skill：`（`pr` 的绝对路径）、
`Tracker：`、`Spec：`、`Tickets：` 与 `Integration branch：`。worker 必须回报图与 frontier、
每票的 branch 与 commit、integration branch、唯一的集成 PR、`Closes` 链接，以及带证据的
Door 声明。嵌套的决策类问题经顶层 worker 上抛；worker 不得替人类作答。逐票 TDD 加
integration branch 上的一次 `code-review` 是本路径的 review 模型，不要再安排逐票双轴 review。

**票型行：紧随前置 skill 写出票型、门禁与测试档**：

```text
票型：研究票 | 设计票 | 规格票 | 票据拆分 | 实施票
门禁：<研究票=来源与结论核验 / 设计票=G-docs / 规格票=G-spec / 拆分=G-tickets / 实施票=G-impl>
测试档：窄（受影响测试 + node glob）| 宽（全量）   ← 实施票必写；窄档须写明为何影响面可圈定
本票不跑：<列出不适用的源码门禁>                 ← 研究/设计/规格/拆分票必写；实施票按实际填写
```

**为什么这一行是硬要求**：worker 读不到 SKILL.md，它只会把 spec 里写的东西当交付面。
spec 不写票型，它就只能去猜——而它手上唯一关于「门禁」的通用线索是仓库 `CONTRIBUTING.md`
的 `Keep it green`，于是一张零源码改动的设计票会去跑全量测试。缺这一行的代价很具体：一段很长的 harness pool 等待
+ 一次挂得远超任何有用超时的全量 + 一例它自己缺二进制的假红，产出若干废证据文件。同样地，
不写「不跑」，「双轴 review」就会被当成所有票的必需品，而它审的是 diff——设计票的 diff 是
文档，Standards 轴只能去挑证据目录的毛病（就是它报出「缺 `pnpm test`」把 worker 引得去跑全量）。

**测试档这一行同理**：窄/宽是 coordinator 在派活前就该做完的判断（见 G-impl 第 1 条与派活前
勾选项），但 worker 看不到这个判断，只会看到 spec 里写了什么。不写测试档，它就退回到
「跑全量最安全」或自行挑几条测试；写了窄档却不写理由，它也无法判断某条测试算不算「受影响」。
所以窄档必须带上「为什么影响面可圈定」的一句话（改的是哪一层、谁依赖它、为什么不触达共享
接口/构建/类型），并给出能直接照着跑的选择器——`node glob` 只是举例，仓库不用 node 时写等价的
测试路径或标记（例如 `python3 -m unittest tests.test_x`），不要让 worker 自己拼命令。
测试档判断错了由 coordinator 打回时改判，不是 worker 现场改口径。

**判据**：派活前根据 Target / Change 与预期文件定票型；worker 完成后再用真实 diff 复核。
若真实改动越过票型边界，按 §5 升级范围问题，不靠一个路径正则自动改判。

worker 只看到 spec，看不到本 skill。派活前把适用的行为规则与实际交付终点写入 spec；
第 6 条的本机预防仅在条件成立时启用，完整处置见恢复参考。

## 规划上下文与交接

首选把 grill、`to-spec` 和 `to-tickets` 留在**同一个规划上下文**里，让 spec 和拆票建立在同一段对话上。
只有工作本身需要拆分、宿主上下文正在劣化，或用户选择的执行拓扑要求隔离时才拆。每阶段一个 worker 是
本项目的监督选择，不是规划 skill 的要求；阶段真的换了上下文时，靠下面的交接记录保真。

**三份文档，不是一份。** *worker task spec* 是 §4.4 的委派说明，说清这次派活要产出什么。
*产品 spec* 是 `to-spec` 发布的产物；被要求写产品 spec 的 worker 分别拿到 task spec 与产品 spec。
*规划交接* 是把一个阶段移过上下文的记录。三者互不替代，也都不证明某个决定已经作出。

**交接记录。** 阶段要移到新上下文时——跨 harness、目录、协作者，或中途分叉——写下下面这份并从新上下文
指向它。记录放在仓库外，指针放在工作继续的地方。

```text
交接：<一句话目标> | 交付终点 <授权终点>
决定：<决定 → 用户原话或直接指针 → 来源（用户 / ADR / spec）>
约束：<数值、否定要求（「不要…」「只能…」）、顺序、排除项>
事实：<已验证的事实及其出处——文件、issue、commit、命令>
未决问题：<每一项谁能回答、为何仍开放>
skill：<路由及依赖，真实路径与调用类型>
产物：<指针，绝不复制——spec、票、研究、原型、diff>
下一步：<新上下文里的第一个动作，以及关闭它的门禁>
```

用指针不用复制：已存在的文档只引用，新上下文读当前版本而不是粘一份旧的。唯一例外是用户的原始决定——
引用原话，因为转述正是约束被软化的方式。敏感内容保持去敏。

**交接必须活下来的东西**：每个数值约束、每条否定要求、每个来源是用户的决定、每个未决问题。
只带走了目标却丢掉「重试上限是 30 s，绝不是 60」或「不要动设置页」的交接没有移动工作，只是重新开始。

**什么时候指针就够。** 同一阶段内紧密受限的子任务只需要 task spec 加一个上下文指针——它该读哪些文件。
完整的交接记录用于真正的边界：新的 harness、目录或协作者，分叉出去的支线，或原上下文清空后恢复的阶段。
不要给子任务套交接，也不要让一次边界跨越没有交接。

**用户的回答经原消息回流。** worker 提出决策类问题时，答案连同用户关键原话记入交接或 Answer，
再经同一编排通道 `reply`；换一个工具回答 worker 会拆散审计链。`grilling` 和 `prototype` 的决定必须来自用户，
不能由 coordinator 或 worker 代替。
