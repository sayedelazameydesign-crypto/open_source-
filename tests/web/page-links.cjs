const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const repoRoot = path.resolve(__dirname, "../..");
const compiledRoot = path.resolve(
  process.env.PAGE_LINKS_OUTPUT || path.join(repoRoot, ".cache/page-links"),
);
const pageLinks = require(path.join(compiledRoot, "lib/page-links.js"));

function collectPageRoutes(appDir, prefix = "") {
  const routes = [];
  for (const entry of fs.readdirSync(appDir, { withFileTypes: true })) {
    const full = path.join(appDir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === "api") continue;
      routes.push(...collectPageRoutes(full, `${prefix}/${entry.name}`));
    } else if (entry.isFile() && entry.name === "page.tsx") {
      routes.push(prefix || "/");
    }
  }
  return routes.sort();
}

function collectFiles(dir, predicate) {
  const files = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      files.push(...collectFiles(full, predicate));
    } else if (entry.isFile() && predicate(full)) {
      files.push(full);
    }
  }
  return files;
}

test("every app route in packages/web/app has a sidebar navigation link and an explicit title", () => {
  const appRoutes = collectPageRoutes(path.join(repoRoot, "packages/web/app"));
  const navHrefs = [
    ...pageLinks.WORKSPACE_NAV_ITEMS.map((item) => item.href),
    ...pageLinks.CONSOLE_NAV_ITEMS.map((item) => item.href),
  ].sort();

  assert.deepEqual(
    navHrefs,
    appRoutes,
    "Every page.tsx under packages/web/app must be reachable from AppShell Navigation",
  );

  for (const route of appRoutes) {
    const title = pageLinks.resolvePageTitle(route);
    assert.notEqual(
      title,
      pageLinks.FALLBACK_PAGE_TITLE,
      `Route ${route} should have an explicit title instead of fallback`,
    );
  }
  assert.equal(
    pageLinks.resolvePageTitle("/unknown-route"),
    pageLinks.FALLBACK_PAGE_TITLE,
  );
});

test("buildWorkspaceHref and parseWorkspaceParams round-trip Arabic, run_id, and special URL characters safely", () => {
  const complexTask =
    "اربط الصفحتين في المتصفح: ?a=1&b=2 #قسم + نسبة 100% / مسار";
  const href = pageLinks.buildWorkspaceHref({
    runId: "run_abc-123_XYZ",
    task: `  ${complexTask}\0  `,
    mode: "flow",
  });
  assert.ok(href.startsWith("/?"));
  const parsed = pageLinks.parseWorkspaceParams(href.slice(1));
  assert.equal(parsed.runId, "run_abc-123_XYZ");
  assert.equal(parsed.task, complexTask);
  assert.equal(parsed.mode, "flow");
});

test("workspace URL params reject invalid run_id, unknown execution modes, and bound oversized input", () => {
  assert.equal(pageLinks.buildWorkspaceHref({}), "/");
  assert.equal(
    pageLinks.buildWorkspaceHref({
      runId: "../etc/passwd",
      task: "   ",
      mode: "bogus",
    }),
    "/",
  );

  const parsedInvalid = pageLinks.parseWorkspaceParams(
    "?run_id=doc_wrongprefix&task=Sum%200..100&mode=arbitrary_exec",
  );
  assert.equal(parsedInvalid.runId, null);
  assert.equal(parsedInvalid.task, "Sum 0..100");
  assert.equal(parsedInvalid.mode, null);

  assert.deepEqual(pageLinks.parseWorkspaceParams("?mode=__proto__"), {
    runId: null,
    task: null,
    mode: null,
  });

  const oversized = "أ".repeat(pageLinks.MAX_TASK_QUERY_LENGTH + 500);
  const boundedHref = pageLinks.buildWorkspaceHref({ task: oversized });
  const boundedParsed = pageLinks.parseWorkspaceParams(boundedHref.slice(1));
  assert.equal(boundedParsed.task.length, pageLinks.MAX_TASK_QUERY_LENGTH);
});

test("buildDocumentsHref and parseDocumentsParams accept only canonical doc_<32hex> IDs", () => {
  const validId = "doc_0123456789abcdef0123456789abcdef";
  const href = pageLinks.buildDocumentsHref({ runId: validId });
  assert.equal(href, `/documents?run_id=${validId}`);
  assert.deepEqual(pageLinks.parseDocumentsParams(href.split("?")[1]), {
    runId: validId,
  });

  for (const invalid of [
    "",
    "run_0123456789abcdef0123456789abcdef",
    "doc_0123456789ABCDEF0123456789abcdef",
    "doc_short",
    "../etc/passwd",
  ]) {
    assert.equal(pageLinks.buildDocumentsHref({ runId: invalid }), "/documents");
    assert.deepEqual(
      pageLinks.parseDocumentsParams(`?run_id=${encodeURIComponent(invalid)}`),
      { runId: null },
    );
  }
});

test("all TSX files across packages/web/{app,components} use next/link instead of raw <a href='/...'> for pages", () => {
  const tsxFiles = [
    ...collectFiles(path.join(repoRoot, "packages/web/app"), (f) =>
      f.endsWith(".tsx"),
    ),
    ...collectFiles(path.join(repoRoot, "packages/web/components"), (f) =>
      f.endsWith(".tsx"),
    ),
  ];
  for (const file of tsxFiles) {
    const content = fs.readFileSync(file, "utf8");
    const matches = [...content.matchAll(/<a\b[^>]*\bhref=(["'])(\/[^"']*)\1/g)];
    for (const match of matches) {
      const target = match[2];
      assert.ok(
        target.startsWith("/api/"),
        `${path.relative(repoRoot, file)} uses raw <a href="${target}"> instead of <Link>`,
      );
    }
  }
});
