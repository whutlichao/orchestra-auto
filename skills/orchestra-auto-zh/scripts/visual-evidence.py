#!/usr/bin/env python3
"""Quiet visual evidence: batch Orca-rendered shots without taking the user's window.

``orca plugin shot --scenario`` and ``--act`` draw their preview inside Orca's real
window, so they need that window hidden or minimized. The obvious way to satisfy that is
the one this skill exists to prevent: click the minimize button, shoot, then ``set
frontmost``. That puts Orca under a multi-second genie animation, and the restore steals
focus from whatever the user was doing.

This script keeps the render and the user's screen. It hides the application once for the
whole batch with an accessibility ``visible`` write (no animation, no activation), shoots
every entry while it is hidden, then puts visibility and focus back exactly as it found
them and proves it in a machine-readable report.

What it never does: click a window control, minimize a window, move or resize one, raise
one, or activate Orca unless Orca was the frontmost app before the batch (restoring the
state it found, not creating a new one). If it cannot observe that state, it refuses to
hide anything.

macOS only. The hide/unhide primitive is AppleScript ``visible``; rather than guess
another platform's equivalent, the script refuses with ``unsupported_platform`` and the
reference says what to do instead.

Usage::

    python3 visual-evidence.py run --plan plan.json [--report report.json]
                                   [--max-seconds 60] [--dry-run]

Plan::

    {"plugin": "/abs/plugin/dir",
     "shots": [{"scenario": "ready", "out": "/abs/ready-dark.png",
                "width": 900, "theme": "dark"},
               {"panel": "main", "act": [{"press": {"text": "Refresh"}}],
                "out": "/abs/refreshed.png"}]}

The report on stdout (and at ``--report``) carries ``window.restored`` and
``window.focusRestored``; both must be true for the evidence to stand. Exit codes: 0 ok,
1 a shot failed, 2 usage/plan/platform/probe, 3 the window or focus was not restored,
4 the time budget ran out (the window is still restored).

Test seams: ``HOST_PLATFORM`` (module constant) and ``--osascript`` / ``--orca``, whose
defaults come from the environment. Tests never touch a real window.
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

SCHEMA_VERSION = 1
#: Patched by tests so the whole suite runs on Linux CI without a real window.
HOST_PLATFORM = sys.platform

EXIT_OK = 0
EXIT_SHOT_FAILED = 1
EXIT_USAGE = 2
EXIT_WINDOW_CHANGED = 3
EXIT_BUDGET = 4

DEFAULT_MAX_SECONDS = 60.0
CLI_ENV = "ORCA_CLI_COMMAND"
DEFAULT_CLI = "orca"
OSASCRIPT_ENV = "ORCA_VISUAL_EVIDENCE_OSASCRIPT"
DEFAULT_OSASCRIPT = "osascript"
CLI_TIMEOUT_SECONDS = 120

PLAN_KEYS = ("plugin", "shots")
SHOT_KEYS = ("scenario", "act", "out", "width", "theme", "panel")
THEMES = ("light", "dark")

PROBE_SCRIPT = """set frontName to "unknown"
tell application "System Events"
  try
    set frontName to name of first application process whose frontmost is true
  end try
  set orcaState to "missing"
  set orcaMini to "false"
  set orcaWins to "0"
  if exists process "Orca" then
    tell process "Orca"
      try
        set orcaState to (visible as string)
      on error
        set orcaState to "unknown"
      end try
      try
        set orcaWins to ((count of windows) as string)
      end try
      try
        set orcaMini to ((value of attribute "AXMinimized" of window 1) as string)
      end try
    end tell
  end if
end tell
return frontName & linefeed & orcaState & linefeed & orcaMini & linefeed & orcaWins
"""

HIDE_SCRIPT = (
    'tell application "System Events" to tell process "Orca" to set visible to false'
)
SHOW_SCRIPT = (
    'tell application "System Events" to tell process "Orca" to set visible to true'
)
#: Only ever run to restore the frontmost state the batch started in.
ACTIVATE_SCRIPT = (
    'tell application "System Events" to tell process "Orca" to set frontmost to true'
)


class PlanError(ValueError):
    """The plan file cannot be executed as written."""


def _error(code, message):
    return {"code": code, "message": message}


def _emit(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _empty_window():
    return {
        "visibilityBefore": None,
        "minimizedBefore": None,
        "frontmostBefore": None,
        "frontmostWhileHidden": None,
        "hiddenByScript": False,
        "activatedOnRestore": False,
        "hiddenSeconds": 0.0,
        "visibilityAfter": None,
        "minimizedAfter": None,
        "frontmostAfter": None,
        "restored": None,
        "focusRestored": None,
        "userTookOver": False,
        "budgetExceeded": False,
    }


def load_plan(path):
    """Parse and validate one plan; return ``(plugin, shots)`` with absolute paths."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise PlanError(f"cannot read plan: {error}") from error
    try:
        plan = json.loads(raw)
    except ValueError as error:
        raise PlanError(f"plan is not JSON: {error}") from error
    if not isinstance(plan, dict):
        raise PlanError("plan must be a JSON object")
    extra = sorted(set(plan) - set(PLAN_KEYS))
    if extra:
        raise PlanError(f"unknown plan keys: {', '.join(extra)}")
    plugin = plan.get("plugin")
    if not isinstance(plugin, str) or not plugin.strip():
        raise PlanError("plan.plugin must be a non-empty path")
    plugin_path = Path(plugin).expanduser()
    if not plugin_path.is_absolute():
        plugin_path = Path.cwd() / plugin_path
    if not plugin_path.is_dir():
        raise PlanError(f"plan.plugin is not a directory: {plugin_path}")
    shots = plan.get("shots")
    if not isinstance(shots, list) or not shots:
        raise PlanError("plan.shots must be a non-empty list")
    validated = []
    seen = set()
    for index, shot in enumerate(shots):
        where = f"plan.shots[{index}]"
        if not isinstance(shot, dict):
            raise PlanError(f"{where} must be an object")
        unknown = sorted(set(shot) - set(SHOT_KEYS))
        if unknown:
            raise PlanError(f"{where} has unknown keys: {', '.join(unknown)}")
        out = shot.get("out")
        if not isinstance(out, str) or not out.strip():
            raise PlanError(f"{where}.out must be a non-empty path")
        out_path = Path(out).expanduser()
        if not out_path.is_absolute():
            out_path = Path.cwd() / out_path
        if out_path.suffix.lower() != ".png":
            raise PlanError(f"{where}.out must name a .png file")
        if out_path in seen:
            raise PlanError(f"{where}.out repeats an earlier output: {out_path}")
        seen.add(out_path)
        scenario = shot.get("scenario")
        act = shot.get("act")
        if scenario is not None and act is not None:
            raise PlanError(f"{where} cannot carry both scenario and act")
        if scenario is not None and (not isinstance(scenario, str) or not scenario.strip()):
            raise PlanError(f"{where}.scenario must be a non-empty name")
        if act is not None:
            if not isinstance(act, list) or not act:
                raise PlanError(f"{where}.act must be a non-empty JSON list")
            if len(act) > 20:
                raise PlanError(f"{where}.act carries more than 20 actions")
        theme = shot.get("theme")
        if theme is not None and theme not in THEMES:
            raise PlanError(f"{where}.theme must be light or dark")
        width = shot.get("width")
        if width is not None and (isinstance(width, bool)
                                  or not isinstance(width, int) or width <= 0):
            raise PlanError(f"{where}.width must be a positive integer")
        panel = shot.get("panel")
        if panel is not None and (not isinstance(panel, str) or not panel.strip()):
            raise PlanError(f"{where}.panel must be a non-empty id")
        validated.append({
            "scenario": scenario,
            "act": act,
            "out": str(out_path),
            "width": width,
            "theme": theme,
            "panel": panel,
        })
    return str(plugin_path), validated


def needs_hidden_window(shots):
    """Only ``--scenario`` and ``--act`` draw inside Orca's window."""
    return any(shot["scenario"] is not None or shot["act"] is not None
               for shot in shots)


def shot_command(cli, plugin, shot):
    argv = [cli, "plugin", "shot", plugin]
    if shot["panel"] is not None:
        argv += ["--panel", shot["panel"]]
    if shot["scenario"] is not None:
        argv += ["--scenario", shot["scenario"]]
    if shot["act"] is not None:
        argv += ["--act", json.dumps(shot["act"], ensure_ascii=False)]
    if shot["width"] is not None:
        argv += ["--width", str(shot["width"])]
    if shot["theme"] is not None:
        argv += ["--theme", shot["theme"]]
    argv += ["--out", shot["out"], "--json"]
    return argv


def _run(argv, timeout=CLI_TIMEOUT_SECONDS):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _osascript(binary, script):
    return _run([binary, "-e", script], timeout=30)


def _as_int(text):
    try:
        return int(text.strip())
    except (TypeError, ValueError):
        return 0


def probe_window(binary):
    """Read frontmost app and Orca's visibility without changing either.

    Returns ``None`` when the state cannot be observed; the caller then refuses to hide
    anything, because an unobserved window is not a safe window to touch.
    """
    result = _osascript(binary, PROBE_SCRIPT)
    if result.returncode != 0:
        return None
    parts = result.stdout.strip("\n").split("\n")
    if len(parts) != 4:
        return None
    front, visible, minimized, windows = parts
    return {
        "frontmost": front.strip() or "unknown",
        "orca": "present" if visible.strip() in ("true", "false") else "missing",
        "visible": visible.strip() == "true",
        "minimized": minimized.strip() == "true",
        "windows": _as_int(windows),
    }


def decide_window(probe):
    """What the batch must change to reach a hidden window, and what it must restore."""
    if probe["orca"] != "present":
        return "refuse-missing"
    if probe["minimized"]:
        return "already-minimized"
    if not probe["visible"]:
        return "already-hidden"
    return "hide"


def _decode_shot(result):
    """Read one ``orca plugin shot --json`` result without trusting its shape."""
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    body = payload.get("result", payload)
    if not isinstance(body, dict):
        return None
    return body


def _terminate(signum, frame):
    raise SystemExit(128 + signum)


def _probe_or_fail(report, osascript):
    probe = probe_window(osascript)
    if probe is None:
        report["errors"].append(_error(
            "probe_failed",
            "could not observe Orca's window; refusing to change it",
        ))
    return probe


def _describe_visibility(probe):
    if probe["orca"] != "present":
        return "missing"
    return "visible" if probe["visible"] else "hidden"


def _run_shots(args, cli, plugin, shots, report, window, started_hidden_at):
    """Run every shot, recording each result; returns True when the budget expired."""
    for shot in shots:
        if started_hidden_at is not None:
            elapsed = time.monotonic() - started_hidden_at
            if elapsed > args.max_seconds:
                report["errors"].append(_error(
                    "budget_exceeded",
                    f"batch exceeded {args.max_seconds:.1f}s; stopping before {shot['out']}",
                ))
                window["budgetExceeded"] = True
                return True
        argv = shot_command(cli, plugin, shot)
        started = time.monotonic()
        try:
            result = _run(argv)
        except subprocess.TimeoutExpired:
            report["shots"].append({
                "scenario": shot["scenario"],
                "out": shot["out"],
                "argv": argv,
                "ok": False,
                "seconds": round(time.monotonic() - started, 3),
                "errors": [_error("timeout", "orca plugin shot did not finish")],
            })
            report["errors"].append(_error("shot_timeout", shot["out"]))
            continue
        seconds = round(time.monotonic() - started, 3)
        body = _decode_shot(result)
        entry = {
            "scenario": shot["scenario"],
            "out": shot["out"],
            "argv": argv,
            "ok": result.returncode == 0 and bool(body),
            "seconds": seconds,
        }
        if body:
            entry["settled"] = body.get("settled")
            entry["themeApplied"] = body.get("themeApplied")
            entry["themeReason"] = body.get("themeReason")
            entry["errors"] = body.get("errors") or []
            entry["width"] = body.get("width")
            entry["height"] = body.get("height")
            entry["theme"] = body.get("theme")
        else:
            entry["errors"] = [_error(
                "unreadable_result",
                (result.stderr or result.stdout or "no output").strip()[:400],
            )]
        if not entry["ok"]:
            report["errors"].append(_error(
                "shot_failed",
                f"{shot['out']} failed: {entry['errors']}",
            ))
        report["shots"].append(entry)
    return False


def _restore(osascript, before, hidden_by_script, started_hidden_at,
             hidden_frontmost, window):
    """Put the window back. Runs on every exit path, including a kill.

    Restoring the frontmost app is only safe when the user has not taken over while
    Orca was hidden. If the frontmost app changed after the hide, the person is using
    it: leaving Orca behind is the correct restore, and activating it would be the very
    focus theft this script exists to remove.
    """
    window_error = None
    if hidden_by_script:
        shown = _osascript(osascript, SHOW_SCRIPT)
        window["hiddenSeconds"] = round(
            time.monotonic() - (started_hidden_at or time.monotonic()), 3)
        if shown.returncode != 0:
            window_error = "unhide failed: " + (
                shown.stderr.strip() or "osascript failed")
    after = probe_window(osascript)
    if after is None:
        window_error = window_error or "could not re-read the window after the batch"
        return window_error
    if hidden_by_script and before["frontmost"] == "Orca":
        if hidden_frontmost is None:
            # Never activate on a guess: without the post-hide baseline there is no way
            # to tell a free desktop from a person who switched apps.
            final = after
        elif after["frontmost"] == hidden_frontmost:
            activated = _osascript(osascript, ACTIVATE_SCRIPT)
            window["activatedOnRestore"] = activated.returncode == 0
            if activated.returncode != 0 and window_error is None:
                window_error = "could not restore Orca as the frontmost app"
            final = probe_window(osascript)
            if final is None:
                window_error = window_error or "could not re-read the frontmost app"
                final = after
        else:
            # The user switched apps while Orca was hidden; do not take it back.
            window["userTookOver"] = True
            final = after
    else:
        final = after
    window["visibilityAfter"] = _describe_visibility(final)
    window["minimizedAfter"] = final["minimized"]
    window["frontmostAfter"] = final["frontmost"]
    if hidden_by_script:
        restored = final["orca"] == "present" and final["visible"] \
            and not final["minimized"]
    else:
        restored = (final["orca"] == before["orca"]
                    and final["visible"] == before["visible"]
                    and final["minimized"] == before["minimized"])
    window["restored"] = bool(restored)
    # The invariant is about Orca, not the whole desktop: the user may switch apps
    # mid-batch, and that is not our doing. We never activate Orca unless it was
    # frontmost before, so the only relation left to prove is that one.
    if before["frontmost"] == "Orca":
        focus_restored = final["frontmost"] == "Orca" or window["userTookOver"]
    else:
        focus_restored = final["frontmost"] != "Orca"
    window["focusRestored"] = bool(focus_restored)
    if not restored and window_error is None:
        window_error = "window state was not restored"
    if not focus_restored and window_error is None:
        window_error = "Orca ended the batch frontmost when it did not start that way"
    return window_error


def run_batch(args):
    """Validate, shoot, restore; return ``(exit_code, report)``."""
    report = {
        "schema": SCHEMA_VERSION,
        "ok": False,
        "dryRun": bool(args.dry_run),
        "plugin": None,
        "platform": HOST_PLATFORM,
        "maxSeconds": args.max_seconds,
        "window": _empty_window(),
        "shots": [],
        "errors": [],
    }
    try:
        plugin, shots = load_plan(args.plan)
    except PlanError as error:
        report["errors"].append(_error("invalid_plan", str(error)))
        return EXIT_USAGE, report
    report["plugin"] = plugin

    if HOST_PLATFORM != "darwin":
        report["errors"].append(_error(
            "unsupported_platform",
            f"quiet scenario captures need macOS; this host is {HOST_PLATFORM}. "
            "Read references/visual-evidence.md before choosing another path.",
        ))
        return EXIT_USAGE, report

    cli = args.orca
    if not shutil.which(cli):
        report["errors"].append(_error("orca_missing", f"Orca CLI not found: {cli}"))
        return EXIT_USAGE, report
    osascript = args.osascript
    if not shutil.which(osascript):
        report["errors"].append(_error(
            "osascript_missing", f"osascript not found: {osascript}"))
        return EXIT_USAGE, report

    before = _probe_or_fail(report, osascript)
    if before is None:
        return EXIT_USAGE, report

    window = report["window"]
    window["visibilityBefore"] = _describe_visibility(before)
    window["minimizedBefore"] = before["minimized"]
    window["frontmostBefore"] = before["frontmost"]

    decision = decide_window(before)
    if decision == "refuse-missing":
        report["errors"].append(_error("orca_missing", "Orca is not running"))
        return EXIT_USAGE, report

    needs_hidden = needs_hidden_window(shots)
    if args.dry_run:
        for shot in shots:
            report["shots"].append({
                "scenario": shot["scenario"],
                "out": shot["out"],
                "argv": shot_command(cli, plugin, shot),
                "ok": None,
                "seconds": 0.0,
            })
        window["hiddenByScript"] = needs_hidden and decision == "hide"
        window["restored"] = True
        window["focusRestored"] = True
        report["ok"] = True
        return EXIT_OK, report

    started_hidden_at = None
    hidden_by_script = False
    hidden_frontmost = None
    budget_expired = False
    window_error = None
    earlier = signal.signal(signal.SIGTERM, _terminate)
    try:
        if needs_hidden and decision == "hide":
            hidden = _osascript(osascript, HIDE_SCRIPT)
            if hidden.returncode != 0:
                report["errors"].append(_error(
                    "hide_failed",
                    "could not hide Orca: "
                    + (hidden.stderr.strip() or "osascript failed"),
                ))
                return EXIT_USAGE, report
            hidden_by_script = True
            window["hiddenByScript"] = True
            started_hidden_at = time.monotonic()
            # Read who the OS activated in Orca's place: that value is what "the user
            # did not switch" looks like at restore time.
            hidden_probe = probe_window(osascript)
            if hidden_probe is not None:
                hidden_frontmost = hidden_probe["frontmost"]
                window["frontmostWhileHidden"] = hidden_frontmost
        try:
            budget_expired = _run_shots(
                args, cli, plugin, shots, report, window, started_hidden_at)
        finally:
            # The restore runs on every exit path, including a shot that raised.
            window_error = _restore(
                osascript, before, hidden_by_script, started_hidden_at,
                hidden_frontmost, window)
    except (OSError, ValueError) as error:
        # A report that says what went wrong is worth more than a traceback, and the
        # window has already been put back by the inner finally.
        report["errors"].append(_error("unexpected_error", str(error)))
    finally:
        signal.signal(signal.SIGTERM, earlier)
    if window_error:
        report["errors"].append(_error("window_not_restored", window_error))

    shot_ok = all(entry["ok"] for entry in report["shots"]) \
        and len(report["shots"]) == len(shots)
    window_ok = window["restored"] is True and window["focusRestored"] is True
    report["ok"] = bool(shot_ok and window_ok and not budget_expired)
    if not window_ok:
        return EXIT_WINDOW_CHANGED, report
    if budget_expired:
        return EXIT_BUDGET, report
    if not shot_ok:
        return EXIT_SHOT_FAILED, report
    return EXIT_OK, report


def build_parser():
    parser = argparse.ArgumentParser(
        description="Capture Orca-rendered shots with one quiet window hide per batch.")
    sub = parser.add_subparsers(dest="command")
    run_parser = sub.add_parser("run", help="run one plan")
    run_parser.add_argument("--plan", help="plan JSON file (required for run)")
    run_parser.add_argument("--report", help="also write the report to this file")
    run_parser.add_argument("--max-seconds", type=float, default=DEFAULT_MAX_SECONDS,
                            help="stop after this long with Orca hidden (default 60)")
    run_parser.add_argument("--dry-run", action="store_true",
                            help="probe and validate without hiding or shooting")
    run_parser.add_argument("--orca", default=os.environ.get(CLI_ENV, DEFAULT_CLI),
                            help=f"Orca CLI executable (default ${CLI_ENV} or orca)")
    run_parser.add_argument("--osascript",
                            default=os.environ.get(OSASCRIPT_ENV, DEFAULT_OSASCRIPT),
                            help="test seam: osascript executable")
    run_parser.add_argument("--print-schema", action="store_true",
                            help="print the plan and report field schema and exit")
    return parser


def schema():
    return {
        "schema": SCHEMA_VERSION,
        "plan": {
            "required": ["plugin", "shots"],
            "shots": {
                "required": ["out"],
                "optional": ["scenario", "act", "width", "theme", "panel"],
                "rule": "scenario and act are mutually exclusive; naming either "
                        "requires a hidden window",
            },
        },
        "report": {
            "window": sorted(_empty_window()),
            "exitCodes": {
                "0": "every shot ran and the window and focus were restored",
                "1": "a shot failed; the window was still restored",
                "2": "usage, plan, platform or probe error; nothing was hidden",
                "3": "the window or the frontmost relation was not restored",
                "4": "the time budget expired; the window was restored",
            },
        },
    }


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "print_schema", False):
        _emit(schema())
        return EXIT_OK
    if args.command != "run":
        parser.error("a command is required: run")
    if not getattr(args, "print_schema", False) and not args.plan:
        parser.error("--plan is required for run")
    code, report = run_batch(args)
    _emit(report)
    if args.report:
        try:
            Path(args.report).write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
        except OSError as error:
            print(f"visual-evidence: cannot write report: {error}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
