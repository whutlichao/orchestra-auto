# Progress Dashboard Architecture

This document holds the developer and protocol internals extracted from the runtime skill bundle so coordinators only pay prompt context for operational commands while full HTTP, security, singleton registry, self-healing, and cleanup invariants remain specified and test-verified.

## HTTP surface and Run isolation

The singleton exposes three read-only GET routes on `127.0.0.1` and no write API:

| Route | Response |
| --- | --- |
| `GET /health` | `{ok, protocol_version, pid, started_at, runs, run_labels, run_states}`: service status, registered Run IDs, short readable labels (descriptor label first, else registered state `run.title`; truncated to 120 chars; unreadable states omitted) and projected statuses (only Runs whose `run.status` belongs to `RUN_STATES`), never state paths |
| `GET /?run=<id>` | HTML dashboard page: Run list in top-bar right dropdown (ended Runs `completed`/`cancelled`/`failed` folded under `#run-toggle` showing `"Show all (N)"` / `"Collapse"`, `"显示全部"` / `"收起"`; selected Run always visible; when `run_states` is missing every status stays unknown and nothing is folded while `/health` contract stays unchanged) + selected Run overview, attention items, donut counts, static workflow reference arrows, SVG dependency graph, tasks, workers, delivery evidence, events, and `"warning is not a start failure"` notice |
| `GET /snapshot?run=<id>` | JSON snapshot for that single Run (omits private `receipts`); nothing from any other Run |

**HTTP surface and Run isolation.** (**HTTP 面与 Run 隔离。**) Each snapshot lookup reads only the selected Run's `progress.json` through the shared state-path validator and names Run IDs/labels only, never their state paths (绝不出现状态路径). Cross-Run reads are limited to `/health` reading each registered Run's own state once to derive `run_labels` and `run_states`. A missing or corrupt snapshot answers `503` (`Snapshot unavailable`) and marks the Run entry unavailable; an unknown Run answers `404`. Neither the service nor `open` repairs or deletes descriptors or snapshots (descriptors/states are never deleted / 绝不删除; only explicit `registry prune` archives ended Runs). Because state lives in `progress.json`, the service is restartable (可重启). Fallback to `serve --state` remains available for degraded/legacy/DEMO runs.

**Security boundary and self-healing UI.**

**Security boundary.** (**安全边界。**)
- Binds `127.0.0.1` IPv4 only; non-GET requests return `405` with `Allow: GET`.
- `Host` must be `localhost` or `127.0.0.1` (optional `:port`), else `403` before routing (DNS-rebinding guard).
- Every response carries:
  - `Content-Security-Policy: default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`
  - `Cache-Control: no-store`
  - `X-Content-Type-Options: nosniff`
  - `X-Frame-Options: DENY`
  - `Referrer-Policy: no-referrer`
- No external fonts/assets load; file/commit values stay text (only credential-free HTTP(S) refs render as links).

**Self-healing browser polling.** Every poll uses an 8-second timeout (`8-second timeout` / `8 秒超时`) via `AbortController`. On timeout or failure, the page keeps the last valid view (`last valid view` / `最后有效视图`) alongside the error time. The in-page refresh button re-reads (`页面内「刷新」按钮立即重读`) immediately and aborts stale polls; returning the tab to the foreground (`回到前台`: visible, `pageshow`, or window `focus`) triggers a single debounced immediate refresh. When `run_states` is missing (or `run_states` 缺失 in legacy services), every status stays unknown, nothing is folded (不折叠), and the `/health` contract is unchanged (契约不变). Otherwise ended Runs fold in the top bar dropdown (顶栏下拉) behind `#run-toggle` (`Show all` / `Collapse`, `显示全部` / `收起`).

## Singleton dashboard registry (`$ORCA_PROGRESS_HOME`)

`open` is the only command starting or adopting the per-user service under `$ORCA_PROGRESS_HOME` (default `~/.orca/progress`, mode `0700`, outside repos):

```text
$ORCA_PROGRESS_HOME
├── server.json          # {pid, port, protocol_version, started_at, binary_version}
├── startup.lock         # flock held only during startup
├── starting.json        # {pid, port}: the startup crash window
├── archive/             # reversible archives created only by explicit registry prune (0700)
│   └── run_xxx/
│       ├── descriptor.json
│       └── <state-dir>/
└── runs/
    └── run_xxx.json     # {run_id, state_path, registered_at, label}
```

**Startup algorithm** under `flock(startup.lock)` (startup lock / 启动锁, 10 s timeout):
1. Read `server.json`, probe `GET /health` with 500 ms timeout (`500` ms).
2. Healthy and `protocol_version >= 1`: call `register(run)` (`discover()`, `probe()`, `open`) idempotently; repeat `open` updates descriptor.
3. Legacy (no `/health`) or incompatible `protocol_version` / hung (timeout or 5xx): never kill/delete; fall back to `serve --state` with `degraded: true`, record ledger `gap`.
4. Connection refused: remove stale `server.json` only and spawn `serve --registry $ORCA_PROGRESS_HOME --port 0`, write `starting.json` (startup crash window / 启动崩溃窗口), wait up to 5 s (`5 s` / `5 秒`) for `/health`, atomically write `server.json` (temp file, fsync, rename, directory fsync / 目录 fsync), register Run, unlink `starting.json`.
5. A concurrent caller finding `starting.json` probes and adopts a healthy child, removing `starting.json` only when the recorded PID is dead.

## Explicit cleanup (`registry prune`)

`registry prune` never runs it automatically (绝不自动调用):

```bash
python3 "$PROGRESS" --registry "$ORCA_PROGRESS_HOME" registry prune \
  [--older-than-days 7] [--keep 10] [--dry-run] [--json]
```

- Defaults: `--older-than-days 7` (`7 days` / `7 天`), `--keep 10` (`10 newest` / `最近 10`, ordered by descending `registered_at`). Invalid flags exit 1 (`exit 1` / `退出码 1`).
- Candidates: terminal Runs (`completed`, `cancelled`, `failed`) strictly older than cutoff (`ended_at` -> `updated_at` -> `last_observed` -> `registered_at`). Unreadable descriptors/states stay in `skipped`. Nonterminal Runs stay in `kept`.
- Archives atomically to `archive/<run_id>/` (rename on same filesystem / `same filesystem` / `同一文件系统`, else copies and verifies / `复制并校验` across filesystems, fsyncs `descriptor.json` and dirs, removes `runs/<run_id>.json`); JSON output lists `"pruned"`, `"archived"`, `"kept"`, `"skipped"`.

## Optional Playwright verification

Optional browser check (requires external `PLAYWRIGHT_MODULE`; **not the mandatory gate** / **不是强制门禁**): tests `**/snapshot*` across two registered Runs (`two registered Runs` / `两个 Run`) via `tests/progress_dashboard_browser.cjs`. Never required when Playwright is absent.
