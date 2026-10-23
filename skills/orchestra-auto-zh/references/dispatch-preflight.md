# 派活前检查：三个检查点——派活前、派活后、收尾

**什么时候读**：分三个时机读，而且只读该时机那一节。**每次派活前的 §4.0.1**（逐项打勾）；
**拿到回执后的 §4.0.2**（启动证据，以及长程票图的隔离能力证明）；**收尾时的 §4.0.3**，
即 `worker_done` 之后、宣称门禁通过之前。

**为什么拆成三节**：一张把「我的 spec 写完了吗」与「worker 终端里 turn 真的起了吗」混在一起的
清单，会把「尚未全绿」读成「不许派活」，而后者这类事实在 Dispatch 存在之前根本不存在。
所以下面每一条都放在它变得可观察的那个时机。只有 §4.0.1 能阻止派活；§4.0.2 阻止的是已经
开始的等待，§4.0.3 阻止的是门禁，不是派活。

### 实时 worker 设置查找

仅在 Run 即将派活时查询。用实时 Orca CLI 定位当前 profile（例如读取 `orca agent hooks status --json` 的
`result.settingsPath` 并赋给 `SETTINGS_PATH`，其文件所在目录作为 `PROFILE_DIR`）；该路径只用于定位 profile，
**不要从 `orca-data.json` 读取 agent 设置**。

从该 profile 目录下**活动的状态数据库**只读查询设置——即该目录里唯一不是保留备份的那个 SQLite 文件。
恢复快照同样以 `.db` 结尾，所以要「选」而不是「按名写死」，且绝不把多个文件一起交给 `sqlite3`：
候选为 0 个或多个时停止并报告，不要猜；读到备份只会得到过期设置。当前 schema 将设置保存在
`profile_state_documents` 表中 `domain = 'settings'` 的 `payload` JSON，默认 TUI agent 和禁用列表分别是
payload 顶层的 `defaultTuiAgent` 与 `disabledTuiAgents`。例如：

```bash
PROFILE_DIR="$(dirname "$SETTINGS_PATH")"
STATE_DB="$(find "$PROFILE_DIR" -maxdepth 1 -name '*.db' ! -name '*.backup.*')"
[ "$(printf '%s\n' "$STATE_DB" | grep -c .)" = "1" ] || { echo "expected exactly one profile state database"; exit 1; }
sqlite3 -readonly "$STATE_DB" \
  "SELECT json_object(
     'defaultTuiAgent', json_extract(payload, '$.defaultTuiAgent'),
     'disabledTuiAgents', json_extract(payload, '$.disabledTuiAgents')
   ) FROM profile_state_documents WHERE domain = 'settings';"
```

确认结果中 `defaultTuiAgent` 非空，且 `disabledTuiAgents` 是数组（空数组有效）。若数据库、表、字段或值不可读，
先依据当前 CLI help 与 Orca guide 核对 profile-state schema；仍无法确认时停止并报告，不能回退到
`orca-data.json`、过期导出、快照、备份、本 skill 文本、eval 夹具或旧日志。

用户点名 agent 时优先使用点名值，否则使用实时默认值；两种情况都核对实时禁用列表，并确认 `worker-start --help` 能识别所选 agent。
若被禁用、不受支持或设置不可读，停止派活。

### 4.0.1 派活前 tick sheet（逐项打勾，未全绿不 dispatch）

唯一作用是让「我是不是漏了哪条」变成一次勾选，而不是靠记性——长 spec 写到后半段时，前面
已经确认过的约束最容易悄悄掉队。括号里是它的归属小节。

- [ ] **CLI 活着**：`<ORCA>` 可解析（`ORCA_CLI_COMMAND` / `orca`），`orca status --json`
      报 app up。起不来 → 按 §9 报告精确错误并停，不换可执行文件、不猜命令。
- [ ] **指南在盘上**：§4.2 的 `skills get orchestration` 已写入 `/tmp`，本轮不再重复抓取。
- [ ] **通道已判定，且若探索过就已复核**（§1 末尾「探索后必须重判」）。
- [ ] **票型已判定，且 spec 里写了票型行**（§4.4）：研究、设计、规格、票据拆分或实施。
      派活前依据 Target、Change 和预期文件判断；worktree 尚未创建，不运行针对它的 `git diff`。
      文档票只写适用的文档门禁，不把源码测试或双轴源码 review 写成验收项。完成后再以真实 diff 复核票型。
- [ ] **spec 五要素齐**：Target / Change / Constraints / Ownership / Observable acceptance，
      且开头两段是 BEHAVIOR RULE 与前置 skill 绝对路径（§4.4）。
- [ ] **worktree 参数对**：`--worktree new-top-level` + `--name <票 slug>` + `--agent <§0 的 worker>`；
      长程票图用 `--name <feature>-integration`，只派一个 worker；
      会话 cwd 与目标 repo 不一致时已加 `--repo <selector>`（§4.3）。
- [ ] **长程票图的图本身确实就绪**（说的是图，不是 worker）：一个集成 PR、`implement-spec` 作为前置 skill、
      票图完整、tracker 可达。worker 能否真的建出隔离的嵌套 worktree，是它的首次报告，
      属于 §4.0.2，永远不在这一条里。
- [ ] **main 铁律自检真的过了一遍**（§2）：本轮不会在 main 工作区修改项目文件或创建提交；仅 §6.5.1 已合并成果的快进同步可更新主分支。
- [ ] **票据正本位置明确**：正本在任务 worktree 内，worker 负责 Status 流转与 Answer 追加；
      coordinator 在 main 上只读验收（§4.4）。
- [ ] **门禁已点名且可判定**：本阶段该过哪几道（G-docs / G-spec / G-tickets / G-impl /
      G-merge），每条的证据形式都能按 §6 机械判定。实施票多勾两笔：
      ① 按 §6 G-impl 第 1 条选定测试档（窄/宽），并**已写进 spec 的 `测试档：` 行**（紧接
      `门禁：` 行）；若选「窄」，同一行里已写明为何影响面可圈定，并给出能照着直接跑的选择器
      （`node glob`，或本仓库等价的测试路径 / 标记）；
      ② 若本票要省双轴 review，已按 §3 的条件确认并写明理由（条件形式见下）。
      测试档只存在于你的判断里、spec 里没有这一行，等于没选：worker 看不到你的推理，
      只会跑全量或自己挑几条测试。
      **机械判据（测试档）**：`grep -c '测试档：' <spec>` 得 0 就不许 dispatch（实施票）。
      **机械判定法（review）**：实施票 spec 的 `Observable acceptance` 里必须**逐字出现 `code-review`**
      （连同「双轴 / Standards + Spec」字样），除非按 §3 省掉。长程票图写「integration branch
      上的一次双轴 code-review」加逐票 TDD，不得再要求逐票双轴 review。跑一句
      `grep -c 'code-review' <spec>`，得 0 就是没写、不许 dispatch。
      为什么要有这条机械判据：一轮实施票的 spec 全文 `review-mentions=0`，因为它们的前置
      skill 指向 `implement`（其正文写着要用 `/code-review`），我据此以为「worker 会自己做」——
      但 worker 只按 spec 里写明的验收清单交付，前置 skill 里的大原则不会自动变成它的交付项。
      **豁免是有条件的，不看喜好**：§3 只允许对这样的改动省双轴 review——①单张票，
      ②方案已定，③影响面可圈定（拿 `git diff --numstat` 与改动文件列表对这张票的 base 看，
      不靠猜）。跨票或共享接口的改动走更宽的 G-impl 验证，review 不能省。条件成立时，
      理由必须**同时**写进 spec 与 Answer；只存在于 coordinator 脑子里的理由，会让 worker
      无从知道 review 被豁免，也让复审者看不到原因。
- [ ] **spec 里的每个基线数字都核过 base commit**：凡你在 spec 里引用「改动前基线：Test Files X / Tests Y」这类
      实测数字，必须确认那条日志来自**与本票 worktree 相同的 base commit**。
      常见事故：一个带新测试的**分支**上的全量日志，被当成另一票（从 main 切出）的改动前基线，
      用例数因此对不上——worker 的双轴 review 把这个不自洽当成 P1 提了出来，没人提就是一张假绿。
      **机械判据**：贴基线前比较产生该日志的 worktree 与本票 base 的 commit，两者不同就**不要用**
      那个数字——或者明写「基线来自 `<commit>`，与本票 base 差 N 个 commit，仅作量级参考」。
- [ ] **本仓库若确有自动格式化风险，已写入针对性的预防与安全恢复要求**（§4.4）。

打回与熔断不在这张表里——照 §6 的「打回协议」执行（带具体缺项清单；同一票连续 2 次不过
同一门禁则停下问人）。

### 4.0.2 派活之后：启动证据与隔离能力报告

这些事实在 Dispatch 存在之前根本不存在，所以在**拿到回执之后、进入长等待之前**检查。它们停的是
等待，不是派活。

- **turn 真的开始了**（§4.3.1）。`turnStart: observed` 就是回执自带的启动证据：接受它，继续往下走。
  当它是 `unsupported`、`unobserved` 或缺失时，该 agent 无法回报启动，只能读它实际支持的证据——
  有终端看标题与屏幕，无终端用 `worker-read` / 状态投影 / 产物进展（见
  [启动与恢复](worker-start-and-recovery.md)）。不要对没有终端的 worker 强行 `terminal show`。
  空闲标题没变化既不能证明 turn 起了，也不能证明它没起；只有在屏幕显示完整 preamble 停在
  输入框且没有正在跑的 turn 时，才允许补一次裸 Enter。无法证实时按 §9 取证，不进入盲等。
- **长程 worker 证明了嵌套隔离，或显式降级**：它的首次报告要说明能建隔离 worktree、分支从
  integration branch 切出，能后台并行或明确串行，并能合并而不碰主工作副本。做不到就把串行
  降级（或逐票回退）记入台账并写明原因；绝不让两个 implementer 共用一个 worktree。
- **收件器已装上且健康**（§4.5）：本 Run 只有一个收件器，绑定真实 coordinator 句柄，
  以 harness 能呈现的调用装上，而不是 `nohup`。

这里红了按证据处理：能证实的就修，需要人的就升级，产物先保住。它不会悄悄变成「派活失败了，
重派一次」——那个结论有自己的证据门槛，见
[失败派活恢复](failed-dispatch-recovery.md)。

### 4.0.3 收尾时、宣称门禁通过之前：格式化噪声

worker 报 `worker_done` 之后、宣称门禁通过之前，**先跑这三条**（§6 的 G-impl 第 0 步）：

```bash
cd <任务 worktree>
git status --porcelain
git diff --numstat | awk '{a+=$1; d+=$2} END {print "worktree 未提交:", a, "增", d, "删"}'
```

- 未提交为空 → 干净，继续验收。
- 有未提交且总量是**万行级** → 整份重排噪声，读 `references/reformat-noise-recovery.md` 恢复。
- 有未提交且量正常 → 问 worker 为什么有未提交改动，不要默认收下。

**判据一句话**：改动文件的 `git diff --numstat` 出现四位数以上，就去看行宽是否被压到 80
附近——编辑器插件与仓库 formatter 在写文件时会按自己的默认行宽重排**整个文件**，而仓库
没有 formatter 配置时这一动作不受约束，很多仓库源码本是宽行风格。

**恢复由 worker 执行，coordinator 只存证并打回。** 先备份完整 diff、未跟踪文件与 index，
再逐文件确认哪些内容是真实工作。`git diff --cached` 非空也不能证明工作区其余改动全是噪声；
确认并留底之前不得 checkout。完整恢复顺序见参考文件。

**治本**：只有确认当前 worker 确实加载了会重排的插件、且仓库存在自动重排风险时，才按
[恢复参考](reformat-noise-recovery.md) 配置本地忽略的覆盖项；其他环境不引入该配置。
