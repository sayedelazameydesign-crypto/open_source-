/** Real headless Chromium E2E. Test-only dependencies, no production packages.
 * See docs/MVP_0_BROWSER_VERIFICATION.md for the isolated fixture and commands.
 */
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { resolve, join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { tmpdir } from 'node:os';

const root = resolve(process.env.E2E_BROWSER_PACKAGES ?? '.cache/mvp0-browser');
const require = createRequire(join(root, 'package.json'));
const { chromium, expect } = require('playwright/test');
const binary = (await import(pathToFileURL(join(root,
  'node_modules/@sparticuz/chromium/build/index.js')).href)).default;
const base = process.env.E2E_BASE_URL ?? 'http://127.0.0.1:3000';
const backend = process.env.E2E_BACKEND_URL ?? 'http://127.0.0.1:8080';
const output = resolve(process.env.E2E_OUTPUT ?? '.cache/mvp0-browser/results');
const token = (await readFile(process.env.E2E_TOKEN_FILE ?? join(root, 'token'), 'utf8')).trim();
const source = 'تصميم تجريبي اصطناعي\nاللون أزرق والمقاس متوسط\n';
const fixture = Buffer.from(source, 'utf8');
const records = { startedAt: new Date().toISOString(), browser: '', tests: [], downloads: [], diagnostics: {
  pageErrors: [], console: [], httpErrors: [], externalRequests: [], requestFailures: [],
}, overall: 'RUNNING' };
let phase = 'setup';
const safePath = url => new URL(url).pathname;
// Use the package's bundled Linux libraries, without installing system packages.
const { inflate } = await import(pathToFileURL(join(root,
  'node_modules/@sparticuz/chromium/build/lambdafs.js')).href);
await inflate(join(root, 'node_modules/@sparticuz/chromium/bin/al2023.tar.br'));
binary.setGraphicsMode = false;
const args = binary.args.filter(arg => ![
  '--disable-web-security', '--allow-running-insecure-content',
  '--disable-site-isolation-trials', '--single-process',
].includes(arg)).concat(['--disable-gpu']);
const browser = await chromium.launch({ executablePath: await binary.executablePath(),
  args, headless: true, env: { ...process.env,
    LD_LIBRARY_PATH: join(tmpdir(), 'al2023/lib') + ':' + (process.env.LD_LIBRARY_PATH ?? ''),
  } });
records.browser = browser.version();
await mkdir(output, { recursive: true });

async function check(name, action) {
  try {
    await action();
    records.tests.push({ name, status: 'PASS' });
    console.log(`PASS: ${name}`);
  } catch (e) {
    records.tests.push({ name, status: 'FAIL' });
    // Assertion errors can include document content or request headers. Do not log them.
    console.error(`FAIL: ${name} (${e.name ?? 'Error'}; details deliberately omitted)`);

    throw e;
  }
}

async function context(options) {
  const ctx = await browser.newContext({ acceptDownloads: true, ...options });
  await ctx.route('**/*', route => {
    if (new URL(route.request().url()).origin === new URL(base).origin) return route.continue();
    records.diagnostics.externalRequests.push(safePath(route.request().url()));
    return route.abort();
  });
  const page = await ctx.newPage();
  page.on('pageerror', error => records.diagnostics.pageErrors.push({ phase, name: error.name }));
  page.on('console', msg => {
    const text = msg.text();
    assert(!text.includes(token) && !text.includes(source) && !text.includes('MVP0_SYNTHETIC_FAILURE'),
      'sensitive content reached console');
    if (msg.type() === 'error' || msg.type() === 'warning') {
      records.diagnostics.console.push({ phase, type: msg.type(),
        // Do not persist arbitrary console text.
        networkFailure: text.startsWith('Failed to load resource:') });
    }
  });
  page.on('requestfailed', request => records.diagnostics.requestFailures.push({
    phase, path: safePath(request.url()),
  }));
  page.on('response', response => {
    if (response.status() >= 400) records.diagnostics.httpErrors.push({
      phase, path: safePath(response.url()), status: response.status(),
    });
  });
  page.on('request', request => {
    // Platform credentials must not be present in browser-facing requests.
    assert(!Object.values(request.headers()).some(v => v.includes(token)),
      'platform credential reached browser request');
  });
  return { ctx, page };
}

async function createPlan(page, name = 'synthetic-arabic.txt', buffer = fixture) {
  await page.goto(base + '/documents');
  await page.getByLabel('اختر ملفًا نصيًا').setInputFiles({ name, mimeType: 'text/plain', buffer });
  await page.getByRole('button', { name: 'رفع الملف وعرض الخطة فقط' }).click();
  await expect(page.getByRole('heading', { name: 'بانتظار موافقتك' })).toBeVisible();
  const id = await page.getByLabel('استرجاع مهمة محفوظة بمعرّفها').inputValue();
  assert.match(id, /^doc_[0-9a-f]{32}$/);
  const state = await (await page.request.get(base + '/api/backend/document-runs/' + id)).json();
  assert.equal(state.status, 'AWAITING_CONFIRMATION');
  assert.equal(state.artifact, null);
  assert.equal(state.events.filter(e => e.status === 'RUNNING').length, 0);
  await expect(page.locator('section ol li')).toHaveCount(3);
  await expect(page.getByRole('link', { name: 'تنزيل تقرير Markdown' })).toHaveCount(0);
  return id;
}

async function downloadAndVerify(page, id, label) {
  const completed = await (await page.request.get(base + '/api/backend/document-runs/' + id)).json();
  assert.equal(completed.status, 'SUCCEEDED');
  assert.deepEqual(completed.statistics, { bytes: fixture.length, characters: [...source].length,
    words: source.trim().split(/\s+/u).length, lines: 2 });
  // Start the listener BEFORE the click, then read the actual browser download.
  const pending = page.waitForEvent('download');
  await page.getByRole('link', { name: 'تنزيل تقرير Markdown' }).click();
  const download = await pending;
  assert.equal(await download.failure(), null);
  assert.match(download.suggestedFilename(), /^report_[0-9a-f]{32}\.md$/);
  const file = join(output, label + '-' + download.suggestedFilename());
  await download.saveAs(file);
  const bytes = await readFile(file);
  const hash = createHash('sha256').update(bytes).digest('hex');
  assert.equal(hash, completed.artifact.sha256);
  assert.equal(bytes.length, completed.artifact.size_bytes);
  const text = bytes.toString('utf8');
  assert(text.includes(id) && text.includes('SUCCEEDED'));
  assert(text.includes(`الحجم بالبايت: ${fixture.length}`));
  assert(text.includes(`الأحرف: ${[...source].length}`));
  assert(text.includes(`الكلمات: ${source.trim().split(/\s+/u).length}`));
  assert(text.includes('الأسطر: 2'));
  for (const line of source.trimEnd().split('\n')) assert(text.includes('    ' + line));
  records.downloads.push({ viewport: label, filename: download.suggestedFilename(),
    bytes: bytes.length, sha256: hash, contentVerified: true });
  return completed;
}

async function geometry(page) {
  const result = await page.evaluate(() => {
    const controls = [...document.querySelectorAll('input, button, a')]
      .filter(el => el.getClientRects().length).map(el => {
        const r = el.getBoundingClientRect();
        return { tag: el.tagName, x: r.x, y: r.y, right: r.right, bottom: r.bottom };
      });
    const overlaps = [];
    for (let i = 0; i < controls.length; i++) for (let j = i + 1; j < controls.length; j++) {
      const a = controls[i], b = controls[j];
      if (Math.min(a.right, b.right) - Math.max(a.x, b.x) > 1 &&
          Math.min(a.bottom, b.bottom) - Math.max(a.y, b.y) > 1) overlaps.push([i, j]);
    }
    return { viewport: innerWidth, width: document.documentElement.scrollWidth,
      outside: controls.filter(r => r.x < -1 || r.right > innerWidth + 1).length, overlaps };
  });
  assert(result.width <= result.viewport + 1, 'horizontal overflow');
  assert.equal(result.outside, 0, 'control outside viewport');
  assert.deepEqual(result.overlaps, [], 'overlapping controls');
  return result;
}

try {
  const desktop = await context({ viewport: { width: 1280, height: 900 } });
  const page = desktop.page;
  let id, completed;
  phase = 'desktop';
  await check('A: Arabic RTL, upload, visible plan, no automatic execution', async () => {
    id = await createPlan(page);
    assert.equal(await page.locator('html').getAttribute('dir'), 'rtl');
    assert.equal(await page.locator('html').getAttribute('lang'), 'ar');
    await geometry(page);
    await page.screenshot({ path: join(output, 'desktop-plan.png'), fullPage: true });
  });
  await check('A: explicit click, real download bytes and SHA-256', async () => {
    await page.getByRole('button', { name: 'أوافق على تنفيذ الخطة محليًا' }).click();
    await expect(page.getByRole('heading', { name: 'اكتملت المعالجة' })).toBeVisible();
    completed = await downloadAndVerify(page, id, 'desktop');
  });
  await check('A: reload, restore, duplicate execute returns the same artifact', async () => {
    await page.reload();
    await page.getByLabel('استرجاع مهمة محفوظة بمعرّفها').fill(id);
    await page.getByRole('button', { name: 'استرجاع السجل' }).click();
    await expect(page.getByRole('heading', { name: 'اكتملت المعالجة' })).toBeVisible();
    const url = base + '/api/backend/document-runs/' + id + '/execute';
    const duplicates = await Promise.all([page.request.post(url), page.request.post(url)]);
    for (const response of duplicates) {
      const state = await response.json();
      assert.deepEqual(state.artifact, completed.artifact);
      assert.equal(state.events.filter(e => e.status === 'RUNNING').length, 1);
    }
  });
  phase = 'expected-negative';
  await check('B: oversize upload is rejected visibly without starting a task', async () => {
    await page.goto(base + '/documents');
    await page.getByLabel('اختر ملفًا نصيًا').setInputFiles({ name: 'oversize.txt',
      mimeType: 'text/plain', buffer: Buffer.alloc(1048577, 'x') });
    await page.getByRole('button', { name: 'رفع الملف وعرض الخطة فقط' }).click();
    await expect(page.locator('main [role="alert"]')).toHaveText('حجم الملف يتجاوز 1 MiB');
    await expect(page.getByRole('button', { name: 'أوافق على تنفيذ الخطة محليًا' })).toHaveCount(0);
  });
  await check('B: invalid UTF-8 and extension are rejected by the real backend', async () => {
    for (const [name, buffer, status] of [
      ['invalid.txt', Buffer.from([255]), 422], ['invalid.pdf', Buffer.from('synthetic'), 415],
    ]) {
      await page.getByLabel('اختر ملفًا نصيًا').setInputFiles({ name, mimeType: 'text/plain', buffer });
      const response = page.waitForResponse(r => r.url().includes('/document-runs?') && r.status() === status);
      await page.getByRole('button', { name: 'رفع الملف وعرض الخطة فقط' }).click();
      await response;
      await expect(page.locator('main [role="alert"]')).toContainText(String(status));
    }
  });
  await check('B: unauthenticated and missing-object requests fail safely', async () => {
    const url = backend + '/document-runs/' + id;
    assert.equal((await page.request.get(url)).status(), 401);
    const headers = { Authorization: `Bearer ${token}` };
    assert.equal((await page.request.get(backend + '/document-runs/doc_missing', { headers })).status(), 404);
    assert.equal((await page.request.get(url + '/artifacts/missing/download', { headers })).status(), 404);
  });
  await check('B: real report-creation fault shows failure, never success', async () => {
    const failureId = await createPlan(page, 'failure.txt', Buffer.from('MVP0_SYNTHETIC_FAILURE'));
    await page.getByRole('button', { name: 'أوافق على تنفيذ الخطة محليًا' }).click();
    await expect(page.getByRole('heading', { name: 'فشلت المعالجة' })).toBeVisible();
    await expect(page.locator('main [role="alert"]')).toContainText('لم يُعلن نجاح المهمة');
    await expect(page.getByRole('heading', { name: 'اكتملت المعالجة' })).toHaveCount(0);
    await expect(page.getByRole('link', { name: 'تنزيل تقرير Markdown' })).toHaveCount(0);
    const state = await (await page.request.get(base + '/api/backend/document-runs/' + failureId)).json();
    assert.equal(state.error, 'document processing failed');
  });
  await desktop.ctx.close();
  const mobile = await context({ viewport: { width: 390, height: 844 },
    isMobile: true, hasTouch: true, deviceScaleFactor: 1 });
  phase = 'mobile';
  await check('C: 390x844 mobile emulation, keyboard controls, plan and download', async () => {
    const mobileId = await createPlan(mobile.page);
    records.mobilePlanGeometry = await geometry(mobile.page);
    const file = mobile.page.getByLabel('اختر ملفًا نصيًا');
    await file.focus();
    await expect(file).toBeFocused();
    await mobile.page.keyboard.press('Tab');
    await expect(mobile.page.getByRole('button', { name: 'رفع الملف وعرض الخطة فقط' })).toBeFocused();
    await mobile.page.keyboard.press('Tab');
    await expect(mobile.page.getByLabel('استرجاع مهمة محفوظة بمعرّفها')).toBeFocused();
    await mobile.page.keyboard.press('Tab');
    await expect(mobile.page.getByRole('button', { name: 'استرجاع السجل' })).toBeFocused();
    await mobile.page.keyboard.press('Tab');
    await expect(mobile.page.getByRole('button', { name: 'أوافق على تنفيذ الخطة محليًا' })).toBeFocused();
    await mobile.page.keyboard.press('Enter');
    await expect(mobile.page.getByRole('heading', { name: 'اكتملت المعالجة' })).toBeVisible();
    await downloadAndVerify(mobile.page, mobileId, 'mobile');
    records.mobileSuccessGeometry = await geometry(mobile.page);
    await mobile.page.screenshot({ path: join(output, 'mobile-success.png'), fullPage: true });
    await mobile.page.getByRole('link', { name: 'تنزيل تقرير Markdown' }).focus();
    await expect(mobile.page.getByRole('link', { name: 'تنزيل تقرير Markdown' })).toBeFocused();
  });
  phase = 'expected-negative-mobile';
  await check('C: mobile error remains visible and within viewport', async () => {
    await mobile.page.getByLabel('اختر ملفًا نصيًا').setInputFiles({ name: 'large.txt',
      mimeType: 'text/plain', buffer: Buffer.alloc(1048577, 'x') });
    await mobile.page.getByRole('button', { name: 'رفع الملف وعرض الخطة فقط' }).click();
    await expect(mobile.page.locator('main [role="alert"]')).toHaveText('حجم الملف يتجاوز 1 MiB');
    await mobile.page.locator('main [role="alert"]').scrollIntoViewIfNeeded();
    await expect(mobile.page.locator('main [role="alert"]')).toBeInViewport();
    await geometry(mobile.page);
  });
  await mobile.ctx.close();
  await check('A/B/C: no page errors, external requests or unexpected HTTP/console errors', async () => {
    assert.deepEqual(records.diagnostics.pageErrors, []);
    assert.deepEqual(records.diagnostics.externalRequests, []);
    assert.deepEqual(records.diagnostics.requestFailures, []);
    assert.deepEqual(records.diagnostics.httpErrors.filter(e => !e.phase.startsWith('expected-negative')), []);
    assert.deepEqual(records.diagnostics.console.filter(e => !e.phase.startsWith('expected-negative')), []);
  });
  records.overall = 'PASS';
} catch {
  records.overall = 'FAIL';
  process.exitCode = 1;
} finally {
  await browser.close();
  records.finishedAt = new Date().toISOString();
  await writeFile(join(output, 'browser-results.json'), JSON.stringify(records, null, 2));
  console.log(`RESULT: ${records.overall}; ${records.tests.filter(t => t.status === 'PASS').length} passed; ` +
    `${records.tests.filter(t => t.status === 'FAIL').length} failed. Mobile emulation, not physical hardware.`);
}
