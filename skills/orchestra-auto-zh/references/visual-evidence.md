# 视觉证据：无打扰采集 Orca 真实界面截图

**什么时候读**：撰写要求 Orca 界面真实渲染截图的 spec 之前，以及 `G-impl` 验收该视觉证据报告之前。

## 硬规约

1. **只认真实渲染**：必须是 `orca plugin shot` 产出的 PNG，绝不接受手工重绘或静态草图。
2. **统一批处理脚本**：全批交给 `scripts/visual-evidence.py run`，每批仅隐藏 Orca 一次，绝不逐张最小化切换。
3. **严禁窗口控件与抢焦点操作**：绝不点 `AXMinimizeButton`、绝不调用 `set frontmost` / `activate`、不移动/缩放/抬升窗口。
4. **不可观测即拒藏**：读不到前置 frontmost 应用与可见性时拒绝执行隐藏。
5. **原样恢复并存证**：拍完恢复原可见性与焦点（期间若人类主动切换到别的应用，记 `userTookOver: true` 且不抢回），并在报告中给出 `window.restored`、`window.focusRestored` 与 `window.hiddenSeconds`。
6. **超时与平台边界**：默认 60 秒预算（`--max-seconds`）；非 macOS 明确报错 `unsupported_platform`，绝不臆测平台窗口脚本（纯 `panel` 截图无需隐藏窗口）。

## 用法与验收

```bash
python3 "$SKILLS_ROOT/orchestra-auto-zh/scripts/visual-evidence.py" run \
  --plan /tmp/evidence-plan.json --report /tmp/evidence-report.json
```

Plan 结构：`{"plugin": "/abs/plugin", "shots": [{"scenario": "ready", "out": "ready.png", "width": 900, "theme": "dark"}]}`（`--dry-run` 仅探测与校验）。退出码：`0` 成功、`1` 单张失败（已恢复窗口）、`2` 参数/平台/观测错误、`3` 窗口或焦点恢复失败、`4` 超时。

`G-impl` 验收必须核实报告中 `window.restored` 与 `window.focusRestored` 均为真、每张 `settled` 为真，并在 Answer 中点明报告路径、`hiddenSeconds`、scenario、width 与 theme；凡使用最小化或 `set frontmost` 者一律打回重拍。
