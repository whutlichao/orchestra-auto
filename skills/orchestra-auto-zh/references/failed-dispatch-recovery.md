# `worker-start` 回执 `state: failed`：先分类，再处置

**什么时候读这份文件**：只在 `orchestration worker-start` 的回执里出现 `state: failed` 时。
正常路径不读它——读它本身不该成为你每次派活的固定开销。

**为什么单列成文件**：它有 6 步、约 700 token，而 `state: failed` 是低频事件。
留在 SKILL.md 里意味着每次会话都要为一件不常发生的事付这份上下文。

**唯一要记住的一句话**：「不许 relaunch」指的是**不许无条件重试**，不是「唯一出路是重试」。
下面的分类合法地可能落到：重派、修 setup、接受已经存在的产物，或报告并停止。

---

## 这套顺序的由来（每一步的理由都出自这里）

某次 `worker-start` 回执：

```text
failedStage: agent_readiness
lastError:   terminal_handle_stale
```

读起来像「终端没了，重派也没用」。实际取证发现**两个终端都还在、worktree 也建好了**，
只有 setup 终端 `status: exited`、`tail: []`（零输出）。就绪检查抢在 setup 完成之前判了失败，
故障不在票上，也不在配置上。

这里的教训比它看上去窄：**终端在 + worktree 在 + setup 零输出退出**只是「继续查」的信号，
不是瞬态故障的证明——零输出同样可能是 hook 根本没跑、跑在错误的工作目录，或在打印前就死了。
真正定案的是那个状态对应的 recovery 指引，加上一次确实跑通的 setup。把三条只读观察当成充分证据，
正是这份文件过去把人引向配置方向、或直接引向盲目重派的根因。

---

## 处置顺序（6 步，顺序有意义）

### 1. 读回执，然后分类

```text
failedStage         失败发生在哪个阶段
lastError           错误码
recovery            ← 当前 recovery 指引：先读它再想
residualResources   残留资源，据此决定要不要先清理
```

归入且只归入一类；决定动作的是分类，不是回执的措辞：

| 分类 | 落到这一类的证据 | 动作 |
| --- | --- | --- |
| 瞬态基础设施 | 失败阶段是就绪/连接类，退出信息是瞬态错误（超时、连接重置、占用、槽位争用），独立复查现在成功，且没有半成品工作 | 允许重派——在走完第 3、5 步之后 |
| setup 故障 | setup 命令真跑一次就失败，或其退出信息写明真实错误 | 修 setup 的病因；不要往坏的 hook 上重派 |
| 隔离/落点故障 | 失败阶段是 worktree、分支或落点创建 | 报告并按已记录的回退方案处理（串行 implementer 或逐票模型）；不要重试同一落点 |
| 产物已存在 | worktree 里有真实未提交工作，或交付物已持久化 | 这不是失败：按 G-impl 第 5 条核验，再决定收留还是打回 |
| 权限/配置故障 | 错误写明 agent 未配置或被禁用、缺权限或凭证、设置不可读 | 按 §5 / §9 升级；绝不替换 agent，绝不冒用他人身份 |

`recovery` 字段通常已经把该状态下的命令写好了。**先读它再设计重试方案**；两者冲突时，
recovery 参考优先于本文件。

### 2. 取证——只读，且只取这一类需要的

```bash
<ORCA> terminal show --terminal <setup句柄> --json   # 终端是否还在（orphaned）
git worktree list                                     # worktree 是否真建了
<ORCA> terminal read --terminal <setup句柄> --json    # setup 终端的 status 与 tail
git -C <worktree> status --porcelain                  # 有没有真实工作要保护
```

每条观察**能证明**什么、**不能证明**什么：

- 终端在 + worktree 在 + setup `exited` 且 `tail: []` ⇒ 就绪检查没看到输出。它本身**不**证明
  瞬态故障，也不证明 hook 可用；那要第 3 步与该状态的 recovery 指引来补。
- 干净 worktree 只证明没有产物受威胁，不证明 setup 跑过。
- 非空 worktree 的优先级高于失败分类：先保存并对账里面的工作。

### 3. 重跑 setup：条件、副作用与执行者

重跑 setup 是项目动作，授权前三条都要过：

- **幂等**：hook 能跑第二次而不重复产生效果（安装、迁移、seed、生成文件）。证明不了就停下报告。
- **授权**：本轮范围覆盖它的行为——联网、装依赖、跑数据库迁移、调外部服务。超出范围是升级，
  不是重试。
- **副作用已知**：先读 hook 里的命令，并记录跑之前的状态（已装依赖、生成文件、已应用的迁移），
  这样它的效果可归因，有害效果能被报告而不是事后才发现。

**谁执行**：由 worker 自己在新一次派活里 `--setup run` 执行，或由人类在你需要凭证/审批时执行。
coordinator 不在 worker 的 worktree 里手跑项目 setup——那个 worktree 是 worker 的，而 hook 会写盘（§2）。
如果手跑是唯一能回答问题的方式，用一次性副本或为此专门建的诊断 worktree，绝不用本票的 worktree，
并保持本票工作原样。

跑通 ⇒ 分类是瞬态，走完第 5 步后可以重派。跑不通 ⇒ 按真实错误处理，重派不是修复。

### 4. 读 recovery 参考

```bash
<ORCA> skills get orchestration --reference references/recovery-and-cleanup.md
```

它的状态表写着：

> `failed` or `stopped` → Start a replacement with `--retry-of`; repeat placement explicitly

所以**不是不能重派，是要用对姿势**：

```bash
<ORCA> orchestration worker-start --task <task_id> \
  --retry-of <failed_dispatch_id> \
  --worktree <显式 selector> --agent <§0 的 worker> --json
```

两个必须注意的约束：

- **`--retry-of` 必须配 `--task`**。配 `--spec` 会另建一个 Task，等于换了一张票。
- **placement 不静默继承**。`--worktree` 要显式给（用 `--repo` 或
  `id:<repo-id>::<worktree 绝对路径>`），别指望它从失败的 Dispatch 那里继承过来。

### 5. 先存证，再 release——顺序不能倒

先把回执、终端读取输出与 worktree 状态记进台账：release 会结算该 Dispatch，结算之后它就不再是
你能回读的证据。任何未提交工作先对账（留底、逐文件确认哪些是真的）再谈别的；对有未对账工作的
worktree，绝不 `checkout`、`reset` 或清理。

然后 release 掉那个失败 Dispatch：

```bash
<ORCA> orchestration worker-release --dispatch <failed_dispatch_id> --json
```

它从未跑过 Task，属它的终端却仍占着配额。**先 release 再重派**，否则新 worker 可能因为配额或
waiter 槽位起不来。顺序不能倒。但 release 不是条件反射：对「产物已存在」与「权限/配置故障」两类，
在产物对账完成或问题升级之前就 release，会毁掉你要用的证据。

### 6. 重派后照 §4.3.1 确认 turn 真的开始

回执 `ready` 仍然证明不了任何事——这条对首次派活和重派一样成立。`turnStart: observed` 就是启动
证据；否则按 §4.3.1 读标题或屏幕，且只有在完整 preamble 停在输入框、没有正在跑的 turn 时，
才补**一次**裸 Enter。

---

## 什么时候该停

- 第 3 步 setup **跑不通** ⇒ 不是瞬态，按真实错误排查，别重派。
- 分类是权限/配置故障 ⇒ 升级；重试修不了缺失的凭证或被禁用的 agent，而替换它们不在 coordinator 权限内。
- 同一张票反复 `failed` ⇒ 已达 §6 打回协议的单票熔断（连续 2 次过不了同一门禁），
  或该走 §9 的「报告并停」。别在重派上无限循环。
- 取证发现 worktree 里有**未提交的真实改动** ⇒ 那不是「失败」，那是「做了一半」。
  按 §6 G-impl 第 5 条先确认交付物是否已持久化，再决定收留还是打回。

## 相关

- SKILL.md §4.3.1 — 派活后确认 turn 开始（重派后同样适用）
- [派活前检查](dispatch-preflight.md) §4.0.2 — 同样的启动与隔离检查，清单形式
- SKILL.md §6 打回协议 — 单票熔断与 run 级熔断
- SKILL.md §2 — 写操作不落 main，也不落在 worker 的 worktree
