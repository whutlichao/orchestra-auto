# spec 契约

**什么时候读**：准备给 worker 写 spec 时，任何通道、任何票型。

### 4.4 spec 契约

Guide 的 Task-spec contract 五要素必须齐：**Target / Change / Constraints / Ownership /
Observable acceptance**。外加第一条固定为：

```text
前置 skill：<绝对路径>——开工前完整读它，按其流程执行，不许跳过。
```

**这一行是其余 skills 的唯一入口。** 没有它，worker 就不会去读 grill-with-docs /
tdd / diagnosing-bugs / code-review，于是「不用其他 skills」的故障就复现了。路径写在 spec 里，
让 worker 自己读全文，不要由你转述。**这条路径是本轮实际所处阶段对应的 skill**（SKILL.md §1），
不是按通道固定：已持有有效 spec 的一轮写 `to-tickets`，有完整票图的写 `implement-spec`，
修 bug 写 `diagnosing-bugs` 与 `implement`。落笔前先确认路径可解析、可读；
不可读就带证据报告，而不是编一个路径或擅自安装本轮未获授权的 skill。

实施类 B 通道的 spec 模板；只写本票适用的门禁。文档票按对应文档门禁改写：

```text
【BEHAVIOR RULE（原样遵守，不得转述或删减）】
1. 提问通道一律 orchestration ask，禁 AskUserQuestion；open 决策 ask 回来，不编造。
2. 你只编辑本票范围内的文件与代码。开工后、每次测试后、换文件前、完成前，用 live preamble
   的可执行文件执行 check --terminal <自己的真实句柄> --json，读取并处理 coordinator follow-up。
   heartbeat 按 preamble 节奏；ask 超时续等同一 message ID，不重复提问、不自行替人决策。
   收尾顺序：检查 follow-up → 写 Answer/证据 → 项目改动提交、推送并创建或更新 PR
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

前置 skill：<绝对路径>——开工前完整读它，按其流程执行，不许跳过。
Target：<文件/模块>
Change：<要产出的具体结果>
Constraints：<不许碰的边界；术语用 codebase-design 词汇>
Ownership：<本票可编辑范围；票据正本在 worktree 内，你负责 Status 流转与 Answer 追加>
Observable acceptance：<适用的测试命令与可观察结果；修复类红→绿证据；需要时含 code-review 双轴。项目文件有改动时：解析并使用本机 pr skill 编写 PR body，推分支，以 --body-file 创建或更新 PR，再把 PR 号、body 路径与 CI 读数记入 GitHub 与仓库外台账，不为它们追加项目提交（用户或仓库明确要求把该事实写进仓库时，以该规则为准）。最后发送并核实 worker_done 回执>
提问通道：orchestration ask（禁 AskUserQuestion）。
```

长程票图下，`前置 skill` 是 `implement-spec` 的绝对路径。另加 `skill 解析：`（有 Skill tool 时
用 `tdd` / `code-review`；没有就直接读它们的绝对路径）、`PR skill：`（`pr` 的绝对路径）、
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
