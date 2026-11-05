# Visual evidence: capture the real render without touching the user's window

**When to read**: when a ticket needs Orca-rendered screenshots (plugin panel, scenario,
editor or status bar); once before writing the spec, once before accepting the evidence.

---

## 1. Why this file exists

`orca plugin shot --scenario` and `--act` draw their preview inside Orca's real window, so
they require that window to be hidden or minimized. The script an agent writes by hand is
usually: click `AXMinimizeButton`, shoot a dozen images, then `AXMinimized=false` plus
`set frontmost of process "Orca" to true`. The window disappears under a genie animation
for ten-odd seconds and then takes focus back; an iteration that re-shoots two or three
times is exactly the "Orca keeps minimizing and restoring" the user reports.

Fix how the evidence is captured, never whether it is captured. Shots must still come from
`orca plugin shot`'s real render, and the window must never be minimized or focused.

## 2. Hard rules

1. **Evidence comes from the real renderer**: a PNG produced by `orca plugin shot`, not a
   hand-drawn mockup, a static HTML page, or a repaint from another instance.
2. **One hide per batch**: hand the whole batch to `scripts/visual-evidence.py run`. Do not
   minimize and restore per image, and do not flip visibility between batches.
3. **No window operations**: never click `AXMinimizeButton`, never `set frontmost`, never
   `activate`, never move, resize or raise a window. The only permitted visibility write is
   the script's single application-level hide (the `AXHidden` equivalent: no animation, no
   activation).
4. **No observation, no change**: when the current frontmost app and window visibility
   cannot be read, the script refuses to hide and says so; do not substitute "it should be
   fine" for an observation.
5. **Restore the state that was there, and prove it**: if Orca was frontmost before the
   batch, put it back (that is restoration, not stealing) — unless the person switched to
   another app while Orca was hidden, in which case leave them there and report
   `userTookOver: true`; if Orca was not frontmost, it must not be frontmost afterwards. A
   failed restore is reported, never silently skipped.
6. **Keep a time budget**: 60 seconds by default; on expiry the batch stops and restores.
   Shoot the scenarios the ticket asks for; when one image at another width or theme
   settles the question, do not add a fifth and sixth variant.
7. **Do not guess another platform**: the script refuses with `unsupported_platform`.
   Capture only panels or status bars that need no hidden window, or report that the
   scenario shots were not obtained — never invent that platform's window control.

## 3. Usage

```bash
SKILL=~/.agents/skills/orchestra-auto            # the path installed for this round
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

- A relative `out` resolves against the current directory; the script always passes an
  absolute path to `orca plugin shot`.
- `scenario` and `act` are mutually exclusive, and only they need a hidden window. A plain
  `panel` shot hides nothing, and the script will not touch visibility for it.
- `--dry-run` validates and probes the window without hiding or shooting, so the plan and
  the current window state can be checked first.
- The report goes to stdout and to `--report`. Exit codes: `0` passed, `1` a shot failed
  (window restored), `2` usage / plan / platform / probe error (nothing hidden), `3` the
  window or frontmost relation was not restored, `4` the budget expired (window restored).

## 4. What the report proves

| Field | Meaning |
| --- | --- |
| `window.restored` | Post-batch visibility matches the pre-batch state (if the script hid it, it is visible again and not minimized) |
| `window.focusRestored` | The frontmost relation matches the pre-batch state: Orca frontmost before means frontmost after (or the user took over); otherwise it must not be frontmost |
| `window.frontmostWhileHidden` | The app the OS activated in Orca's place; the baseline for "the user did not switch" |
| `window.userTookOver` | The frontmost app changed again while Orca was hidden: the script leaves it there rather than taking focus back |
| `window.hiddenSeconds` | How long the user's window was actually hidden; quote it in the Answer so a human can judge the intrusion |
| `window.hiddenByScript` | Whether this batch hid anything; `false` for a panel-only batch |
| `shots[].settled` / `themeApplied` / `errors` | Whether each image finished drawing, whether the theme applied, and what the render reported |

`themeApplied: false` with a `themeReason` usually means the window was not hidden at shoot
time — check whether the capture bypassed the script. An image with `settled: false` is not
evidence: wait for the panel to finish, or record that state in the report's `errors`.

## 5. Acceptance and send-back

- G-impl visual evidence carries the report, with `window.restored` and
  `window.focusRestored` both `true`.
- The Answer names the report path, `hiddenSeconds`, and the scenario / width / theme behind
  each PNG.
- `AXMinimizeButton`, `set frontmost`, a minimize script, or a re-shoot that only restored
  after a second round is an evidence defect: send it back to be re-shot, not annotated with
  "the user did not mind".
- When the ticket's evidence is the window itself (`--window`), no hiding is needed: shoot
  it directly. That exception covers only "the window is the evidence".

The machine-readable form of the plan and report: `visual-evidence.py run --print-schema`.
