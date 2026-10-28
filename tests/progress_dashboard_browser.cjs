/* Optional real-browser regression check. Runtime packages remain dependency-free.
 * PLAYWRIGHT_MODULE=/external/node_modules/playwright node tests/progress_dashboard_browser.cjs URL EVIDENCE_DIR
 */
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const url = process.argv[2] || "http://127.0.0.1:8766/";
const output = process.argv[3];
const task = (id, status, dependencies = [], extra = {}) => ({
  id,
  title: id,
  status,
  dependencies,
  ...extra,
});
const fixture = () => ({
  run: {
    id: "browser-run",
    title: "可验证的运行目标 / verified objective",
    mode: "DEMO",
    status: "active",
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
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.goto(url);
  await page.waitForSelector(".donut");
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
  let snapshot = fixture(),
    response = "valid";
  await page.route("**/snapshot", async (route) => {
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
  async function refresh() {
    await page.locator("#refresh").click();
    await page.waitForTimeout(180);
  }
  await refresh();
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
  await page.locator("#language").click();
  await refresh();
  assert.equal(await page.locator("html").getAttribute("lang"), "en");
  assert.match(await page.locator("#tasks h2").innerText(), /Tasks/);
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
  response = "valid";
  snapshot = {
    ...fixture(),
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
  snapshot = fixture();
  snapshot.tasks = {};
  snapshot.workers = {};
  snapshot.deliveries = {};
  snapshot.run.phase = "merged";
  await refresh();
  assert.equal(await page.locator(".donut-center strong").innerText(), "0");
  assert.deepEqual(
    await page.locator(".step .count").allTextContents(),
    Array(8).fill("0"),
  );
  snapshot.tasks = { unknown: task("unknown", "unknown", ["missing"]) };
  await refresh();
  assert.match(await page.locator(".scroll svg").textContent(), /missing/);
  assert.equal(await page.locator(".donut-center strong").innerText(), "1");
  results.push(
    "empty and unknown honest; run.phase never backfills workflow facts; unknown dependency stays visible",
  );
  snapshot = fixture();
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
  snapshot = fixture();
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
  snapshot = fixture();
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
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
    true,
  );
  results.push(
    "cycles, missing nodes, long titles and 90 tasks remain scrollable; malicious HTML/URLs stay text",
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
