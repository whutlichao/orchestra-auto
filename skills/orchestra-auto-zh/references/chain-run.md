# 链条的位置、续跑与出口检查

**什么时候读：** 同一次 Run 的两轮之间、崩溃或应用重启之后，或者 coordinator 需要从账本而不是从对话里回答「这条链接下来走到哪」的时候。

## 目录

- [位置，而不是步骤](#位置而不是步骤)
- [续跑：链条从哪继续](#续跑链条从哪继续)
- [出口检查：产物形状的地板](#出口检查产物形状的地板)
- [什么时候停下来问人](#什么时候停下来问人)
- [续跑不做什么](#续跑不做什么)

## 位置，而不是步骤

链条在分叉前是线性的，所以按五个**位置**建模。`implement` 和 `implement-spec` 是同一个位置，因为两者关在同一个门禁上：一条验收记录说不清跑的是哪一个，靠猜的续跑会把链条换到错的分支上。

| 位置 | 门禁 | 步骤 | 关掉它的产物 |
| --- | --- | --- | --- |
| `grill` | G-docs | `grill-with-docs` | 说清的改动、术语表条目和相应的 ADR |
| `spec` | G-spec | `to-spec` | 已发到 tracker 的 spec |
| `tickets` | G-tickets | `to-tickets` | 带阻塞关系的票图 |
| `build` | G-impl | `implement` **或** `implement-spec` | 一张定案的票，或整份 spec 的 integration 分支 |
| `review` | G-merge | `code-review`，然后合并 | 双轴 review 与合并 |

门禁是权威，位置只是门禁上方的一个标签。`chain.py steps` 把这张表以 JSON 打印出来，coordinator 不需要每次从散文里重新推导。

## 续跑：链条从哪继续

```bash
python3 scripts/chain.py resume --run-id <run_id>
python3 scripts/chain.py resume --run-id <run_id> --ledger /path/to/ledger.jsonl
```

`resume` 读这次 Run 的账本（默认 `$ORCA_LEDGER_HOME/runs/<id>/ledger.jsonl`），只保留 `kind: acceptance` 记录，然后对每个位置问一个问题：**这个门禁有没有一条记录，它的 checks 全过、review 是 pass 或 waiver？**

- 没有 checks 的记录是没有证据的主张，不算落地。
- check 失败或 review 失败，不算落地。
- 未知门禁什么也不落地：一个拼写错误不应该推动链条。
- 后来的记录不会让先前的位置「取消落地」；历史是只追加的。

答案只有两种状态：

```json
{"state": "continue", "landed": ["grill", "spec"], "next": {"position": "tickets", "gate": "G-tickets"}}
{"state": "complete", "landed": ["grill", "spec", "tickets", "build", "review"], "next": null}
```

`problems` 记录读的时候跳过了什么：末尾的半行会被丢弃，损坏的完整行会按行号报告。悄悄丢一条记录的续跑会声称错误的位置，比拒绝回答更糟。

答案是一个位置，不是一次派活。它只说明从哪继续；怎么继续——通道、agent、worktree、spec——仍然由 §4 决定。
这个脚本只按完整五位置链检查本 Run 的验收记录，不能识别直接实施、已有 spec/票的入口或仅设计的交付终点。
这些路径中，早期门禁缺记录不等于必须补跑，也不能凭它的 `complete` 判定授权范围已经交付；
对照模式声明、入口证据与实际交付终点判断，缺少依据时先核实。

## 出口检查：产物形状的地板

```bash
python3 scripts/chain.py exit-check --step to-spec   --artifact /path/to/spec.md
python3 scripts/chain.py exit-check --step to-tickets --artifact /path/to/tickets.md
```

只有两个步骤的产物形状值得机械检查，因为下一格会忠实地实现它拿到的任何东西：

- **`to-spec`** —— spec 带一行 `Test tier:`（narrow 或 wide，附理由），并且有一个至少包含一条可验收条目的验收小节。被下一个标题切断的小节按空处理。
- **`to-tickets`** —— 每个 `## ` 票块都带一行验收：`验收：`、`Acceptance:`、`Done when:` 或 `完成标准`。没有验收行的票没法演示，也就没法验收。

票据检查只接受本项目的合并 `## <票名>` 格式，不能解析上游的一票一文件模板或 tracker issue 的
章节：可能误报或把章节误当多张票。不要用它判定这些格式；按 G-tickets 核验真实票据及验收标准。
spec 的格式地板也是本项目附加要求，并非上游默认模板。

其余步骤会返回一句拒绝，并点名真正的信号（测试运行、review、PR 状态）——假装检查测试运行，比直说不查更糟。那些事实属于 `acceptance.py`（`verify`）和 `closeout.py`（`reconcile`）。

**这是地板，不是判决。** 一份 spec 可以带着 `Test tier:` 行却写错方向。exit-check 只能抓住缺失的小节，抓不住错误的决定；门禁仍然是 coordinator 的，检查通过不是跳过双轴 review 的许可。

## 什么时候停下来问人

下面这些情况结束这一轮，而不是开启下一轮。前两条是机械的；其余是 §5 的决策类规则，集中在这里，免得一次续跑把 coordinator 劝过它们。

| 情况 | 停在哪 | 谁决定 |
| --- | --- | --- |
| 同一张票的同一个门禁连续失败两次 | 这张票的这一轮 | coordinator 打回，或触发 per-ticket 熔断 |
| 整个 Run 连续两个门禁失败 | 整个 Run | coordinator 报告，人决定要不要继续 |
| 单向 Merge Danger 门 | 合并 | 人来合 |
| 凭证、第三方账号、付费、人工审批、不可逆的 live 数据 | 这一步 | 人 |
| 交付形态、方案分叉、范围变了 | 这一步 | 人 |
| `resume` 报告 `complete` | 无——Run 已结束 | coordinator 写 closeout |

## 续跑不做什么

- **revert 之后它不会把位置改回未落地。** revert 是一条 `observation`，只降低级别、不改写历史，所以 `resume` 仍把该位置算作已落地。记录到 revert 时由 coordinator 显式重开那张票；如果链条必须回到更早的位置，那是一次新的 Run，不是一次悄悄的续跑。
- **它不读 Orca、git 或 PR host。** 它判断不了一个 worker 是不是死了；那是 `run-health.md` 和 `worker-show` 的事。
- **它不选下一个动作。** 位置不是派活：通道、agent、worktree 名和 spec 仍然归 §4。
