/* Optional real-browser regression check. Runtime packages remain dependency-free.
 *
 * This script is NOT the mandatory gate: the mandatory gate is the CLI/HTTP contract in
 * tests/test_progress_dashboard.py. Run this only in an environment that already provides
 * Playwright and Chromium; when they are absent the script is simply not run.
 *
 * It drives one singleton service with TWO registered Runs:
 * PLAYWRIGHT_MODULE=/external/node_modules/playwright \
 *   node tests/progress_dashboard_browser.cjs 'http://127.0.0.1:PORT/?run=alpha&second=beta' EVIDENCE_DIR
 */
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const url =
  process.argv[2] || "http://127.0.0.1:8766/?run=alpha&second=beta";
const output = process.argv[3];
const base = new URL(url);
const SELECTED = base.searchParams.get("run") || "alpha";
const SECOND = base.searchParams.get("second") || "beta";
const task = (id, status, dependencies = [], extra = {}) => ({
  id,
  title: id,
  status,
  dependencies,
  ...extra,
});
const fixture = (runId = "browser-run") => ({
  run: {
    id: runId,
    title: "可验证的运行目标 / verified objective",
    mode: "DEMO",
    status: "active",
    target_repo: "/fixtures/target-repo",
    phase: "acceptance",
    freshness: "fresh",
    last_observed: "2026-01-01T12:00:00Z",
    last_effective_progress: null,
  },
  tasks: {
    a: task("a", "accepted"),
    b: task("b", "running", ["a"]),
    c: task("c", "waiting_user", [], {
      pending_decision: "Choose release date",
    }),
    d: task("d", "rejected"),
  },
  workers: {
    root: {
      id: "root",
      task_id: "b",
      parent_worker_id: null,
      lifecycle: "running",
      reported_lifecycle: "reported_done",
      start_confirmation: "observed",
      freshness: "fresh",
    },
    child: {
      id: "child",
      task_id: "b",
      parent_worker_id: "root",
      lifecycle: "lost",
      reported_lifecycle: "unknown",
      start_confirmation: "unobserved",
      freshness: "stale",
    },
    grandchild: {
      id: "grandchild",
      task_id: "b",
      parent_worker_id: "child",
      lifecycle: "unknown",
      start_confirmation: "unknown",
      freshness: "unknown",
    },
  },
  deliveries: {
    a: {
      task_id: "a",
      implemented: true,
      integrated: true,
      merged: false,
      tracker_closed: null,
    },
  },
  artifacts: {},
  events: [],
});
let browser;
(async () => {
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  const errors = [];
  const requests = [];
  page.on("request", (request) => requests.push(request.url()));
  page.on("pageerror", (e) => errors.push(String(e)));
  // The fake clock is installed before the page runs so every timer the page schedules —
  // the three-second poll and the 8-second read timeout — is controlled here. It is then
  // paused, so only an explicit runFor advances the page's time and request counts stay
  // deterministic.
  await page.clock.install();
  await page.goto(url);
  await page.waitForSelector(".donut");
  await page.clock.pauseAt(new Date(Date.now()));
  const results = ["Chromium " + browser.version()];
  for (const [width, height] of [
    [1440, 900],
    [1920, 1080],
    [768, 1024],
    [375, 812],
  ]) {
    await page.setViewportSize({ width, height });
    assert.equal(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
      true,
      "horizontal overflow at " + width,
    );
    if (output) {
      fs.mkdirSync(output, { recursive: true });
      await page.screenshot({
        path: path.join(output, `demo-${width}-first.png`),
      });
      await page.screenshot({
        path: path.join(output, `demo-${width}-full.png`),
        fullPage: true,
      });
    }
    results.push(`viewport ${width}x${height}: no page overflow`);
  }
  let snapshot = fixture(SELECTED),
    response = "valid";
  // A "hang" answer holds the response until the test releases it, so the page really is
  // left waiting while the test observes whether it can still recover on its own.
  const held = [];
  await page.route("**/snapshot*", async (route) => {
    if (response === "hang") {
      await new Promise((resolve) => held.push(resolve));
      try {
        return await route.fulfill({
          contentType: "application/json",
          body: JSON.stringify(snapshot),
        });
      } catch {
        return undefined; // the page already aborted this request
      }
    }
    if (response === "error")
      return route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ error: "test observation failure" }),
      });
    return route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(snapshot),
    });
  });
  // The singleton lists every registered Run; only the selected one is rendered.
  // The selected Run carries a readable label; the second one has none and must fall
  // back to its Run ID, which stays available on hover for a labelled chip.
  let healthLabels = { [SELECTED]: "可验证的 Run 标签 / readable Run label" };
  let healthStates = { [SELECTED]: "active", [SECOND]: "completed" };
  await page.route("**/health", async (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        ok: true,
        protocol_version: 1,
        pid: 1,
        started_at: "2026-01-01T00:00:00Z",
        runs: [SELECTED, SECOND],
        run_labels: healthLabels,
        run_states: healthStates,
      }),
    }),
  );
  async function refresh() {
    await page.locator("#refresh").click();
    await page.waitForTimeout(180);
  }
  await refresh();
  // The Run list lives in the left sidebar, after the brand and before the navigation.
  assert.equal(
    await page.locator(".sidebar #runs").count(),
    1,
    "the Run list sits inside the sidebar",
  );
  assert.equal(
    await page.evaluate(() => {
      const sidebar = document.querySelector(".sidebar");
      const brand = sidebar && sidebar.querySelector(".brand");
      const runs = sidebar && sidebar.querySelector(".runs");
      const nav = sidebar && sidebar.querySelector("nav");
      return Boolean(
        brand &&
          runs &&
          nav &&
          brand.compareDocumentPosition(runs) &
            Node.DOCUMENT_POSITION_FOLLOWING &&
          runs.compareDocumentPosition(nav) &
            Node.DOCUMENT_POSITION_FOLLOWING,
      );
    }),
    true,
    "the Run list sits after the brand and before the navigation",
  );
  // Ended Runs are folded by default; the selected Run never is.
  assert.equal(await page.locator(".run-chip").count(), 1);
  assert.equal(
    await page.locator("#run-toggle").innerText(),
    "显示全部 (1)",
  );
  assert.equal(
    await page.locator("#run-toggle").getAttribute("aria-expanded"),
    "false",
  );
  assert.equal(
    await page
      .locator('.run-chip[aria-current="true"] .run-dot')
      .getAttribute("data-state"),
    "active",
  );
  assert.match(
    await page
      .locator('.run-chip[aria-current="true"] .run-dot')
      .getAttribute("class"),
    /tone-success/,
  );
  assert.equal(
    await page.locator('.run-chip[aria-current="true"]').innerText(),
    "可验证的 Run 标签 / readable Run label",
  );
  assert.equal(
    await page.locator('.run-chip[aria-current="true"]').getAttribute("title"),
    SELECTED,
    "the full Run ID stays reachable on hover",
  );
  await page.locator("#run-toggle").click();
  assert.equal(await page.locator(".run-chip").count(), 2);
  assert.equal(await page.locator("#run-toggle").innerText(), "收起");
  assert.equal(
    await page.locator("#run-toggle").getAttribute("aria-expanded"),
    "true",
  );
  assert.equal(
    await page.locator('.run-chip[title="' + SECOND + '"]').innerText(),
    SECOND,
    "a Run without a label falls back to its Run ID",
  );
  assert.equal(
    await page
      .locator('.run-chip[title="' + SECOND + '"] .run-dot')
      .getAttribute("data-state"),
    "completed",
  );
  // A long label is truncated with an ellipsis instead of widening the page.
  healthLabels = { [SELECTED]: "Long readable label ".repeat(12) };
  await refresh();
  assert.equal(
    await page
      .locator('.run-chip[aria-current="true"]')
      .evaluate((node) => node.getBoundingClientRect().height < 30),
    true,
    "a long label stays on one line",
  );
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
    true,
    "a long label does not widen the page",
  );
  // The toggle speaks the page's language.
  await page.locator("#language").click();
  assert.equal(await page.locator("#run-toggle").innerText(), "Collapse");
  await page.locator("#language").click();
  assert.equal(await page.locator("#run-toggle").innerText(), "收起");
  // The selected Run stays visible even after it ends; collapsing hides only others.
  healthStates = { [SELECTED]: "completed", [SECOND]: "completed" };
  await refresh();
  assert.equal(await page.locator("#run-toggle").innerText(), "收起");
  await page.locator("#run-toggle").click();
  assert.equal(await page.locator(".run-chip").count(), 1);
  assert.equal(
    await page.locator('.run-chip[aria-current="true"]').getAttribute("title"),
    SELECTED,
    "the selected Run stays visible even when it has ended",
  );
  assert.equal(await page.locator("#run-toggle").innerText(), "显示全部 (1)");
  // Without run_states (an older service) nothing is folded away.
  healthStates = null;
  await refresh();
  assert.equal(await page.locator("#run-toggle").isHidden(), true);
  assert.equal(await page.locator(".run-chip").count(), 2);
  healthStates = { [SELECTED]: "active", [SECOND]: "completed" };
  await refresh();
  healthLabels = { [SELECTED]: "可验证的 Run 标签 / readable Run label" };
  await refresh();
  assert.match(
    await page.locator("#notice").innerText(),
    /不是启动失败[\s\S]*not a start failure/,
  );
  assert.match(
    await page.locator(".run-meta").innerText(),
    new RegExp("Run " + SELECTED),
  );
  assert.equal(
    await page
      .locator(".step .count")
      .allTextContents()
      .then((x) => x.join(",")),
    "4,1,1,1,1,1,0,0",
  );
  assert.match(
    await page.locator("#content").innerText(),
    /Choose release date/,
  );
  assert.equal(await page.locator(".worker-row").count(), 3);
  assert.equal(
    await page
      .locator(".worker-row")
      .nth(2)
      .evaluate((n) => n.style.marginLeft),
    "28px",
  );
  assert.deepEqual(
    await page
      .locator("#content section[id]")
      .evaluateAll((nodes) => nodes.map((node) => node.id)),
    [
      "overview",
      "attention",
      "stats",
      "workflow",
      "graph",
      "tasks",
      "delivery",
      "events",
    ],
    "section order",
  );
  assert.equal(await page.locator("#nav a").count(), 8);
  assert.match(
    await page.locator(".run-meta").innerText(),
    /目标仓库\s*\/fixtures\/target-repo/,
  );
  assert.equal(
    await page.locator(".attention-count").innerText(),
    "共 2 项",
  );
  assert.equal(await page.locator("#attention .attention-card").count(), 4);
  assert.match(
    await page.locator('[data-group="decision"]').innerText(),
    /Choose release date/,
  );
  assert.match(await page.locator('[data-group="lost"]').innerText(), /child/);
  assert.match(
    await page.locator(".time-tile.primary-time").innerText(),
    /最近有效进展[\s\S]*未知/,
  );
  assert.doesNotMatch(
    await page.locator("#content").innerText(),
    /%|剩余时间|remaining time/,
  );
  await page.locator(".legend button").filter({ hasText: "实施中" }).click();
  await page.locator('[data-key="task:b"] summary').click();
  snapshot.run.freshness = "stale";
  await refresh();
  assert.equal(await page.locator(".task-row").count(), 1);
  assert.equal(
    await page.locator('[data-key="task:b"]').evaluate((n) => n.open),
    true,
  );
  assert.match(await page.locator(".filterbar").innerText(), /筛选/);
  await page.locator(".filterbar button").click();
  assert.equal(await page.locator(".task-row").count(), 4);
  assert.match(await page.locator(".filterbar").innerText(), /全部任务/);
  await page.locator(".legend button").filter({ hasText: "实施中" }).click();
  await page.locator("#stats .panel-head button").click();
  assert.equal(await page.locator(".task-row").count(), 4);
  await page.locator("#language").click();
  await refresh();
  assert.equal(await page.locator("html").getAttribute("lang"), "en");
  assert.match(await page.locator("#tasks h2").innerText(), /Tasks/);
  assert.match(
    await page.locator(".run-meta").innerText(),
    /Target repository\s*\/fixtures\/target-repo/,
  );
  assert.equal(
    await page.locator("#nav .nav-label").first().innerText(),
    "Overview",
  );
  assert.match(
    await page.locator("#attention h2").innerText(),
    /Needs attention/,
  );
  await page.locator("#language").click();
  results.push(
    "mixed states, explicit counts, nested lost/stale workers, filter/details/language survive refresh",
  );
  const valid = await page.locator("#content").innerHTML();
  response = "error";
  await refresh();
  assert.equal(await page.locator("#content").innerHTML(), valid);
  assert.match(
    await page.locator("#health").innerText(),
    /test observation failure/,
  );
  assert.match(await page.locator("#health").innerText(), /最后有效快照/);
  response = "valid";
  snapshot = {
    ...fixture(SELECTED),
    tasks: { bad: { id: "bad", dependencies: null } },
  };
  await refresh();
  assert.equal(await page.locator("#content").innerHTML(), valid);
  assert.match(
    await page.locator("#health").innerText(),
    /Invalid dependencies/,
  );
  results.push(
    "HTTP and malformed snapshots preserve complete last valid view",
  );
  snapshot = fixture(SELECTED);
  snapshot.tasks = {};
  snapshot.workers = {};
  snapshot.deliveries = {};
  snapshot.run.phase = "merged";
  delete snapshot.run.target_repo;
  await refresh();
  assert.equal(await page.locator(".donut-center strong").innerText(), "0");
  assert.deepEqual(
    await page.locator(".step .count").allTextContents(),
    Array(8).fill("0"),
  );
  assert.match(await page.locator(".run-meta").innerText(), /目标仓库\s*未知/);
  assert.deepEqual(
    await page.locator(".attention-num").allTextContents(),
    ["0", "0", "0", "0"],
    "empty attention counts",
  );
  snapshot.run.status = "waiting_user";
  await refresh();
  assert.equal(
    await page.locator(".attention-count").innerText(),
    "共 1 项",
  );
  assert.match(
    await page.locator('[data-group="decision"]').innerText(),
    /Run 已标记等待用户/,
  );
  snapshot.run.status = "active";
  snapshot.tasks = { unknown: task("unknown", "unknown", ["missing"]) };
  await refresh();
  assert.match(await page.locator(".scroll svg").textContent(), /missing/);
  assert.equal(await page.locator(".donut-center strong").innerText(), "1");
  results.push(
    "empty and unknown honest; run.phase never backfills workflow facts; unknown dependency stays visible",
  );
  snapshot = fixture(SELECTED);
  snapshot.tasks = {
    bad: task("bad", "failed"),
    held: task("held", "blocked", [], {
      blocked_reason: "Waiting on review",
      pending_decision: "Approve the review scope",
    }),
    warned: task("warned", "running", [], {
      blocked_reason: "Recorded block was not cleared",
    }),
  };
  snapshot.workers = {
    gone: {
      id: "gone",
      task_id: "bad",
      lifecycle: "failed",
      reported_lifecycle: "unknown",
      start_confirmation: "unknown",
      freshness: "stale",
    },
  };
  snapshot.deliveries = {};
  await refresh();
  assert.deepEqual(
    await page.locator(".attention-num").allTextContents(),
    ["1", "2", "2", "0"],
  );
  assert.match(
    await page.locator('[data-group="decision"]').innerText(),
    /Approve the review scope/,
  );
  assert.match(
    await page.locator('[data-group="blocked"]').innerText(),
    /Waiting on review/,
  );
  assert.match(await page.locator('[data-group="failed"]').innerText(), /gone/);
  results.push(
    "attention covers blocked, failed and lost records without inventing others",
  );
  snapshot = fixture(SELECTED);
  snapshot.tasks = { a: task("a", "accepted"), b: task("b", "accepted") };
  snapshot.deliveries = {
    a: {
      task_id: "a",
      implemented: true,
      integrated: true,
      merged: false,
      tracker_closed: false,
    },
    b: {
      task_id: "b",
      implemented: true,
      integrated: null,
      merged: null,
      tracker_closed: null,
    },
  };
  await refresh();
  assert.equal(await page.locator(".step .count").nth(4).innerText(), "2");
  assert.equal(await page.locator(".step .count").nth(6).innerText(), "0");
  snapshot.deliveries.a.merged = true;
  await refresh();
  assert.equal(await page.locator(".step .count").nth(6).innerText(), "1");
  assert.equal(await page.locator(".step .count").nth(7).innerText(), "0");
  assert.match(await page.locator(".delivery-grid").innerText(), /未核验/);
  results.push(
    "all accepted is not merged; merged is not tracker closed; false and null stay distinct",
  );
  snapshot = fixture(SELECTED);
  snapshot.events = [
    {
      at: "2026-01-01T12:00:00Z",
      source: "worker",
      type: "worker_done",
      task_id: "b",
      summary: "Historic unverified completion",
      evidence: ["https://example.com/report", "javascript:unsafe"],
    },
  ];
  for (let i = 0; i < 7; i++)
    snapshot.events.push({
      at: "2026-01-01T12:01:00Z",
      source: "coordinator",
      type: "observation",
      task_id: "a",
      summary: "Newer observation " + i,
    });
  await refresh();
  await page.locator('[data-key="older-events"] summary').click();
  const older = page.locator('[data-key="older-events"]');
  assert.match(await older.innerText(), /worker 自报/);
  assert.match(await older.innerText(), /worker_done · b/);
  assert.equal(await older.locator("a").count(), 1);
  assert.equal(
    await older.locator("a").getAttribute("href"),
    "https://example.com/report",
  );
  assert.match(await older.innerText(), /javascript:unsafe/);
  results.push(
    "older events retain self-report source, entity and safe evidence after six newer events",
  );
  const attack = '<img src=x onerror="window.injected=true">';
  snapshot = fixture(SELECTED);
  snapshot.tasks.a.title = attack + "长标题".repeat(100);
  snapshot.tasks.a.dependencies = ["b", "missing"];
  snapshot.tasks.b.dependencies = ["a"];
  snapshot.artifacts = {
    bad: {
      kind: "pr",
      label: attack,
      reference: "javascript:window.injected=true",
    },
    auth: {
      kind: "report",
      label: "credentials",
      reference: "https://user:pass@example.com",
    },
    good: { kind: "pr", label: "safe", reference: "https://example.com/pr/1" },
  };
  snapshot.events = [
    { at: "now", source: "worker", type: "worker_done", summary: attack },
  ];
  await refresh();
  assert.equal(await page.locator("#content img").count(), 0);
  assert.equal(await page.evaluate(() => window.injected), undefined);
  assert.equal(await page.locator("#content a").count(), 1);
  assert.match(await page.locator("#content").innerText(), /javascript:/);
  assert.equal(await page.locator(".scroll svg rect").count(), 5);
  await page.setViewportSize({ width: 375, height: 812 });
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
    true,
  );
  snapshot.tasks = {};
  for (let i = 0; i < 90; i++)
    snapshot.tasks["n" + i] = task(
      "n" + i,
      "unknown",
      i ? ["n" + (i - 1)] : [],
      { title: "Very long task title ".repeat(12) },
    );
  await refresh();
  assert.equal(await page.locator(".scroll svg rect").count(), 90);
  const graph = page.locator(".scroll");
  await graph.evaluate((element) => {
    element.scrollLeft = 420;
  });
  snapshot.tasks.n0.title = "Changed after scroll";
  await refresh();
  assert.equal(
    await graph.evaluate((element) => element.scrollLeft),
    420,
    "graph scroll survives refresh",
  );
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
    true,
  );
  results.push(
    "cycles, missing nodes, long titles and 90 tasks stay scrollable; graph scroll survives refresh; malicious HTML/URLs stay text",
  );
  // A hung read must not lock the page. The route holds the response so the page is really
  // waiting; the paused clock then runs past the page's own 8-second read timeout.
  const snapshotRequests = () =>
    requests.filter((requestUrl) => requestUrl.includes("/snapshot")).length;
  const heldBefore = snapshotRequests();
  response = "hang";
  await refresh();
  assert.equal(snapshotRequests(), heldBefore + 1, "the click issued a request");
  await page.clock.runFor(8100);
  await page.waitForTimeout(200);
  assert.match(
    await page.locator("#health").innerText(),
    /读取超时|Read timed out/,
    "the hung read times out and says so",
  );
  assert.equal(
    await page.locator(".scroll svg rect").count(),
    90,
    "the last valid view survives the timeout",
  );
  // Recovering after the timeout: the next read must land. The still-held request is
  // released late and must not overwrite the newer result.
  response = "valid";
  for (const release of held.splice(0)) release();
  await page.waitForTimeout(100);
  snapshot = fixture(SELECTED);
  snapshot.tasks = { after: task("after", "accepted") };
  const heldAfterTimeout = snapshotRequests();
  await refresh();
  assert.equal(snapshotRequests(), heldAfterTimeout + 1, "a real read follows the timeout");
  assert.equal(await page.locator(".task-row").count(), 1);
  assert.match(await page.locator(".task-row").innerText(), /after/);
  results.push(
    "a hung snapshot times out at 8s, keeps the last valid view, and the next read still lands",
  );
  // A forced refresh wins immediately instead of waiting for the timeout to expire.
  response = "hang";
  await refresh();
  const heldDuringForce = snapshotRequests();
  snapshot = { ...fixture(SELECTED), tasks: { forced: task("forced", "running") } };
  response = "valid";
  await page.locator("#refresh").click();
  await page.waitForTimeout(300);
  assert.equal(
    snapshotRequests(),
    heldDuringForce + 1,
    "the refresh button does not queue behind the stuck read",
  );
  assert.equal(await page.locator(".task-row").count(), 1);
  assert.match(await page.locator(".task-row").innerText(), /forced/);
  for (const release of held.splice(0)) release();
  results.push("the refresh button aborts a stuck read and re-reads immediately");
  // Returning to the foreground reads at once — the page is visible and unthrottled here, so
  // a read can only come from the events themselves. A burst of three collapses into one.
  snapshot = fixture(SELECTED);
  await refresh();
  const foregroundBefore = snapshotRequests();
  await page.evaluate(() => {
    let state = "visible";
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => state,
    });
    state = "hidden";
    document.dispatchEvent(new Event("visibilitychange"));
    state = "visible";
    document.dispatchEvent(new Event("visibilitychange"));
    window.dispatchEvent(new Event("pageshow"));
    window.dispatchEvent(new Event("focus"));
  });
  await page.clock.runFor(60);
  await page.waitForTimeout(300);
  assert.equal(
    snapshotRequests(),
    foregroundBefore + 1,
    "one foreground burst is exactly one read",
  );
  results.push(
    "visibilitychange, pageshow and focus collapse into one immediate read",
  );
  await page.clock.resume();
  assert.deepEqual(
    requests.filter((requestUrl) => !requestUrl.startsWith(base.origin + "/")),
    [],
    "no remote requests",
  );
  assert.deepEqual(errors, []);
  if (output)
    fs.writeFileSync(
      path.join(output, "browser-results.json"),
      JSON.stringify(results, null, 2) + "\n",
    );
  console.log(results.join("\n"));
  await browser.close();
})().catch(async (error) => {
  console.error(error);
  if (browser) await browser.close();
  process.exitCode = 1;
});
