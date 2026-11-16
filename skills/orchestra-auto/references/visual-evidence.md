# Visual Evidence: Quiet Orca-Rendered Captures

**When to read**: before writing the spec or accepting `G-impl` for any ticket requiring Orca-rendered UI screenshots (panel, scenario, editor, status bar).

## Hard Rules

1. **Real renderer only**: PNG from `orca plugin shot`, never mockups or manual redraws.
2. **Batch via `scripts/visual-evidence.py run`**: hides Orca once per batch (never per-shot minimize/unhide cycles).
3. **Never touch window controls or focus**: never click `AXMinimizeButton`, never run `set frontmost`, `activate`, move, resize, or raise.
4. **Fail closed if unobservable**: refuses to hide if pre-batch frontmost app or visibility cannot be read.
5. **Prove restoration**: restores pre-batch visibility and frontmost app (unless user switched away mid-batch: `userTookOver: true`) and reports `window.restored` + `window.focusRestored` + `window.hiddenSeconds`.
6. **Budget & platform**: defaults to 60 s (`--max-seconds`); non-macOS refuses cleanly with `unsupported_platform` rather than guessing window commands (panel-only shots need no hiding).

## Usage & Acceptance

```bash
python3 "$SKILLS_ROOT/orchestra-auto/scripts/visual-evidence.py" run \
  --plan /tmp/evidence-plan.json --report /tmp/evidence-report.json
```

Plan schema: `{"plugin": "/abs/plugin", "shots": [{"scenario": "ready", "out": "ready.png", "width": 900, "theme": "dark"}]}` (`--dry-run` probes without hiding). Exit codes: `0` pass, `1` shot failed (window restored), `2` invalid plan / `unsupported_platform` / unobservable, `3` failed window/focus restoration, `4` timeout.

For `G-impl` acceptance, verify the report has both `window.restored == true` and `window.focusRestored == true`, `settled == true` (`themeApplied: true` when themed), and record the report path, `hiddenSeconds`, scenario, width, and theme in the Answer. Window minimization or `set frontmost` is an automatic send-back.
