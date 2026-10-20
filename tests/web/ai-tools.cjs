const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const lib = require(path.resolve(
  __dirname,
  "../../.cache/ui1-ai-tools/lib/ai-tools.js",
));

test("catalog lists the eight tools from the source document", () => {
  assert.equal(lib.AI_TOOLS.length, 8);
  assert.deepEqual(
    lib.AI_TOOLS.map((t) => t.name),
    [
      "Claude Code",
      "GitHub Copilot",
      "Cursor",
      "Windsurf",
      "Replit",
      "v0.dev",
      "Bolt.new",
      "Snyk",
    ],
  );
  for (const tool of lib.AI_TOOLS) {
    assert.ok(tool.url.startsWith("https://"), tool.id);
    assert.ok(tool.blurb.length > 10, tool.id);
  }
});

test("filterTools matches name, tags and category", () => {
  assert.deepEqual(
    lib.filterTools(lib.AI_TOOLS, "cursor", "All").map((t) => t.id),
    ["cursor"],
  );
  assert.deepEqual(
    lib.filterTools(lib.AI_TOOLS, "", "AI IDE").map((t) => t.id),
    ["cursor", "windsurf"],
  );
  // multi-word query: every word must appear somewhere in the haystack
  assert.deepEqual(
    lib.filterTools(lib.AI_TOOLS, "security scanning", "All").map((t) => t.id),
    ["snyk"],
  );
  assert.deepEqual(lib.filterTools(lib.AI_TOOLS, "nope-nothing", "All"), []);
});

test("sortTools orders by featured, price and name without mutating input", () => {
  const before = lib.AI_TOOLS.map((t) => t.id);
  assert.equal(lib.sortTools(lib.AI_TOOLS, "featured")[0].id, "claude-code");
  const byPrice = lib.sortTools(lib.AI_TOOLS, "price-asc");
  assert.equal(byPrice[0].id, "github-copilot"); // $10 — cheapest paid tier
  assert.equal(byPrice[byPrice.length - 1].id, "snyk"); // contact pricing last
  assert.equal(lib.sortTools(lib.AI_TOOLS, "name")[0].id, "bolt-new");
  assert.deepEqual(
    lib.AI_TOOLS.map((t) => t.id),
    before,
  );
});

test("summarizeStack totals known prices and flags custom pricing", () => {
  const summary = lib.summarizeStack(
    lib.AI_TOOLS,
    new Set(["claude-code", "cursor", "snyk"]),
  );
  assert.equal(summary.count, 3);
  assert.equal(summary.monthlyUsd, 40);
  assert.deepEqual(summary.customPriced, ["Snyk"]);
  assert.deepEqual(summary.freeTierAvailable, ["Cursor", "Snyk"]);
  assert.equal(lib.summarizeStack(lib.AI_TOOLS, new Set()).monthlyUsd, 0);
});

test("recommended stack (learning-path step 2) costs $65/mo", () => {
  assert.equal(lib.recommendedStackMonthlyUsd(), 65);
});

test("estimateSavings reproduces the source's build-an-app example shape", () => {
  // 4 devs × 3 months vs 1 dev in ~1 month + $65/mo stack
  const est = lib.estimateSavings({
    developers: 4,
    hourlyRateUsd: 100,
    monthsBefore: 3,
  });
  assert.equal(est.beforeCostUsd, 4 * 3 * 160 * 100);
  assert.equal(est.monthsAfter, 1);
  assert.equal(est.afterToolsUsd, 65);
  assert.ok(est.savedUsd > 0);
  assert.ok(est.savedPct > 80 && est.savedPct < 100);
  assert.equal(est.monthsSaved, 2);
});

test("estimateSavings clamps invalid inputs instead of going negative", () => {
  const est = lib.estimateSavings({
    developers: 0,
    hourlyRateUsd: -5,
    monthsBefore: 0,
  });
  assert.ok(est.beforeCostUsd >= 0);
  assert.ok(est.monthsAfter >= 0.5);
});

test("formatUsd renders whole-dollar amounts", () => {
  assert.equal(lib.formatUsd(150000), "$150,000");
});
