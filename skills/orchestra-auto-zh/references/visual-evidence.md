# 视觉证据：拍下真实渲染，不碰用户的窗口

**什么时候读**：票面要求 Orca 实际渲染的截图（插件面板 / 场景 / 编辑器 / 状态栏）时；
写 spec 前与验收视觉证据前各读一次。

---

## 1. 为什么有这份文件

`orca plugin shot --scenario` 和 `--act` 会把预览画在 Orca 真实窗口里，因此要求窗口已隐藏或
最小化。顺手写出的取证脚本通常是：点 `AXMinimizeButton` → 连拍十几张 → `AXMinimized=false`
+ `set frontmost of process "Orca" to true`。结果是窗口在 genie 动画里消失十几秒，然后被抢回
前台；一轮迭代重拍两三次，用户看到的就是「Orca 不停最小化又恢复」。

要修的是取证方式，不是取消取证。截图仍必须来自 `orca plugin shot` 的真实渲染，
**窗口一次也不该被最小化或抢焦点**。

## 2. 硬规则

1. **证据来自真实渲染器**：用 `orca plugin shot` 生成的 PNG，不用手工示意图、静态 HTML 或
   另一实例的重绘冒充。
2. **一批只隐藏一次**：整批截图交给 `scripts/visual-evidence.py run`。禁止逐张最小化/恢复，
   也禁止在批与批之间反复切换窗口可见性。
3. **禁止窗口操作**：不得点击 `AXMinimizeButton`、不得 `set frontmost`、不得 `activate`、
   不得移动 / 缩放 / 抬升窗口。唯一允许的可见性写入是脚本做的一次应用级隐藏（`AXHidden`
   等价物，无动画、不激活）。
4. **不观测就不改**：读不到当前前台应用与窗口可见性时，脚本拒绝隐藏并如实报告；不要用
   「应该没事」代替观测。
5. **还原到批前状态，并证明**：批前 Orca 在前台，还原时把它放回前台（这是还原，不是抢），
   但隐藏期间用户已经切到别的应用时不要抢回来——把用户留住，报告 `userTookOver: true`；
   批前不在前台，还原后不得让它变成前台。恢复失败必须写进报告，不能静默继续。
6. **有时间预算**：默认 60 秒，超时立即停拍并还原。只拍票面要求的场景；同一张图换宽度或
   主题已经能说明问题时，不要再加第五、第六个变体。
7. **非 macOS 不猜**：脚本以 `unsupported_platform` 拒绝。此时只拍不需要隐藏窗口的
   panel / 状态栏截图，或如实报告「场景截图未取得」，不要临时发明该平台的窗口控制。

## 3. 用法

```bash
SKILL=~/.agents/skills/orchestra-auto-zh          # 本轮实际安装路径
python3 "$SKILL/scripts/visual-evidence.py" run --plan /tmp/evidence-plan.json \
  --report /tmp/evidence-report.json
```

```json
{
  "plugin": "/abs/path/to/plugin",
  "shots": [
    {"scenario": "ready", "out": "docs/evidence/orca-ready-dark.png",
     "width": 900, "theme": "dark"},
    {"scenario": "ready", "out": "docs/evidence/orca-ready-narrow.png",
     "width": 420, "theme": "dark"}
  ]
}
```

- `out` 的相对路径按当前目录解析；脚本传给 `orca plugin shot` 的始终是绝对路径。
- `scenario` 与 `act` 互斥；带任一者才需要隐藏窗口。纯 `panel` 截图不隐藏窗口，脚本不会
  为它动可见性。
- `--dry-run` 只做校验与窗口探测，不隐藏、不拍照，可先确认 plan 与当前窗口状态。
- 报告同时写到 stdout 与 `--report`；退出码 `0` 通过、`1` 有截图失败（窗口已还原）、
  `2` 用法 / plan / 平台 / 探测错误（未隐藏）、`3` 窗口或前台关系未还原、`4` 超时（窗口已还原）。

## 4. 报告里看什么

| 字段 | 含义 |
| --- | --- |
| `window.restored` | 批后窗口可见性与批前一致（脚本隐藏过则必须重新可见且未最小化） |
| `window.focusRestored` | 前台关系与批前一致：批前 Orca 在前台则回到前台（或用户已接管），否则批后不得是前台 |
| `window.frontmostWhileHidden` | 隐藏后由系统顶到前台的应用；它就是「用户没有切走」的比对基准 |
| `window.userTookOver` | 隐藏期间前台应用又变了：脚本不抢回焦点，只把窗口还原 |
| `window.hiddenSeconds` | 用户窗口被隐藏的真实时长，写进 Answer 供人判断打扰程度 |
| `window.hiddenByScript` | 本批是否真的隐藏过；纯 panel 截图为 `false` |
| `shots[].settled` / `themeApplied` / `errors` | 单张是否画完、主题是否生效、渲染报了什么错 |

`themeApplied: false` 带 `themeReason` 通常说明拍摄时窗口没被隐藏——先查是不是绕过了脚本。
`settled: false` 的图不算证据：等面板画完再拍，或把该状态写进报告的 `errors`。

## 5. 验收与打回

- G-impl 的视觉证据必须附报告，且 `window.restored` 与 `window.focusRestored` 均为 `true`。
- Answer 写明报告路径、`hiddenSeconds` 与每张 PNG 对应的场景 / 宽度 / 主题。
- 见到 `AXMinimizeButton`、`set frontmost`、窗口最小化脚本或第二轮重拍才恢复的取证方式，
  按证据缺陷打回：重拍，不是补一句「用户没意见」。
- 票面要的本来就是窗口级截图（`--window`）时不需要隐藏，直接拍；这条例外只覆盖
  「窗口本身是证据」的情况。

实现与字段的机器可读版本：`visual-evidence.py run --print-schema`。
