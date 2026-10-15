/** UI-1: real local journeys, responsive/keyboard checks, explicitly mocked faults. */
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { resolve, join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { tmpdir } from 'node:os';
import { mkdir, writeFile } from 'node:fs/promises';

const packages = resolve(process.env.E2E_BROWSER_PACKAGES ?? '.cache/ui1-browser');
const require = createRequire(join(packages, 'package.json'));
const { chromium, expect } = require('playwright/test');
const modulePath = join(packages, 'node_modules/@sparticuz/chromium');
const binary = (await import(pathToFileURL(join(modulePath, 'build/index.js')).href)).default;
const { inflate } = await import(pathToFileURL(join(modulePath, 'build/lambdafs.js')).href);
await inflate(join(modulePath, 'bin/al2023.tar.br'));
binary.setGraphicsMode = false;
const args = binary.args.filter(arg => ![
  '--disable-web-security', '--allow-running-insecure-content', '--disable-site-isolation-trials',
  '--single-process',
].includes(arg)).concat(['--disable-gpu']);
const base = process.env.E2E_BASE_URL ?? 'http://127.0.0.1:3000';
const output = resolve(process.env.E2E_OUTPUT ?? '.cache/ui1-browser/results');
await mkdir(output, { recursive: true });
const record = { tests: [], pageErrors: [], unexpectedHttpErrors: [], externalRequests: [],
  startedAt: new Date().toISOString(), overall: 'RUNNING' };
const browser = await chromium.launch({ executablePath: await binary.executablePath(), args,
  env: { ...process.env, LD_LIBRARY_PATH: join(tmpdir(), 'al2023/lib') }, headless: true });
record.browser = browser.version();
const context = await browser.newContext({ viewport: { width: 1440, height: 1050 } });
await context.route('**/*', route => {
  if (new URL(route.request().url()).origin === new URL(base).origin) return route.continue();
  record.externalRequests.push('blocked');
  return route.abort();
});
const page = await context.newPage();
page.on('pageerror', e => record.pageErrors.push(e.name));
page.on('response', response => {
  if (response.status() >= 400) record.unexpectedHttpErrors.push({
    status: response.status(), path: new URL(response.url()).pathname,
  });
});

async function check(name, kind, action) {
  try {
    await action();
    record.tests.push({ name, kind, status: 'PASS' });
    console.log(`PASS [${kind}]: ${name}`);
  } catch (error) {
    console.error("Failure category:", error.name, String(error.message).split("\n")[0].slice(0, 180));
    record.tests.push({ name, kind, status: 'FAIL' });
    console.error(`FAIL: ${name}; no response bodies or credentials logged`);
    throw new Error('UI acceptance failed');
  }
}


async function capture(name) {
  // Capture a normal page view, not a retained keyboard-focus/scroll position.
  await page.evaluate(() => { document.activeElement?.blur?.(); window.scrollTo(0, 0); });
  await page.screenshot({ path: join(output, name), fullPage: true });
}
const noOverflow = async p => assert(await p.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
const json = (route, body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
async function mockPage(overrides = {}) {
  const p = await context.newPage();
  p.on('pageerror', e => record.pageErrors.push(e.name));
  let body = { run_id: 'run_fixture', status: 'completed', final_answer: 'Fixture only — not a verified goal.', pending_approvals: [] };
  await p.route('**/api/backend/**', async route => {
    const u = new URL(route.request().url());
    const key = u.pathname.replace('/api/backend', '');
    if (overrides[key]) return overrides[key](route);
    if (key === '/health') return json(route, { status: 'ok' });
    if (key === '/runtime-info') return json(route, {});
    if (key === '/runs') return json(route, { run_id: 'run_fixture' }, 202);
    if (key.endsWith('/events')) return route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' });
    return json(route, body);
  });
  await p.goto(base);
  return { p, state: value => { body = value; } };
}
async function begin(p = page, task = 'Sum 0..100', mode = 'react') {
  await p.getByLabel('ما الذي تريد العمل عليه؟').fill(task);
  await p.getByLabel('نمط التنفيذ').selectOption(mode);
  await p.getByRole('button', { name: 'تشغيل ▶', exact: true }).click();
}
try {
  for (const width of [1440, 1024, 390]) {
    await check(`RTL idle layout ${width}px without horizontal overflow`, 'live-browser', async () => {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1050 });
      await page.goto(base);
      await expect(page.locator('.backend-indicator')).toContainText('الخلفية متصلة');
      assert.equal(await page.locator('html').getAttribute('dir'), 'rtl');
      await expect(page.getByRole('heading', { name: 'مساحة العمل', exact: true })).toBeVisible();
      await noOverflow(page);
      await capture(`workspace-${width}.png`);
    });
  }
  await check('390px drawer traps focus, Escape closes it, RTL tabs use keyboard', 'live-browser', async () => {
    const trigger = page.getByRole('button', { name: 'فتح التنقل' });
    await trigger.click();
    await expect(page.getByRole('dialog')).toBeVisible();
    for (let i = 0; i < 7; i++) {
      await page.keyboard.press('Tab');
      assert(await page.evaluate(() => Boolean(document.activeElement?.closest('dialog'))));
    }
    await page.keyboard.press('Escape');
    await expect(page.getByRole('dialog')).not.toBeVisible();
    await expect(trigger).toBeFocused();
    await page.getByRole('tab', { name: 'المهمة', exact: true }).focus();
    await page.keyboard.press('ArrowLeft');
    await expect(page.getByRole('tab', { name: 'النشاط', exact: true })).toBeFocused();
    await page.keyboard.press('End');
    await expect(page.getByRole('tab', { name: 'الأدلة والملفات' })).toHaveAttribute('aria-selected', 'true');
    await expect(page.locator('.mobile-evidence .evidence-files')).toBeVisible();
    await expect(page.locator('.composer')).not.toBeVisible();
    await noOverflow(page);
    await capture('evidence-390.png');
    await page.setViewportSize({ width: 1440, height: 1050 });
    await expect(page.getByRole('tab', { name: 'المهمة', exact: true })).toHaveAttribute('aria-selected', 'true');
    await page.getByRole('tab', { name: 'المهمة', exact: true }).focus();
    await page.keyboard.press('End');
    await expect(page.getByRole('tab', { name: /النشاط/ })).toBeFocused();
    await page.setViewportSize({ width: 390, height: 844 });
    await page.keyboard.press('Home');
    await expect(page.getByRole('tab', { name: 'المهمة', exact: true })).toBeFocused();
  });
  await check('keyboard skip link and suggestion fill a request without executing it', 'live-browser', async () => {
    await page.goto(base);
    await page.keyboard.press('Tab');
    await expect(page.getByRole('link', { name: 'تخطّ إلى المحتوى' })).toBeFocused();
    await page.keyboard.press('Enter');
    await expect(page.locator('#main-content')).toBeFocused();
    await page.getByRole('button', { name: /جرّب خطوات التنفيذ/ }).click();
    await expect(page.getByLabel('ما الذي تريد العمل عليه؟')).toHaveValue('Sum 0..100');
    await expect(page.getByLabel('ما الذي تريد العمل عليه؟')).toBeFocused();
    await expect(page.locator('.task-overview')).toHaveCount(0);
  });
  await check('real ReAct SSE, failed-tool honesty, activity and new task reset', 'live-backend-scripted-model', async () => {
    await page.setViewportSize({ width: 1440, height: 1050 });
    const created = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/backend/runs');
    await begin();
    const id = (await (await created).json()).run_id;
    await expect(page.locator('.verification-note')).toContainText('completed');
    await expect(page.locator('.conn')).toContainText('انتهى البث');
    const snap = await (await page.request.get(base + '/api/backend/runs/' + id)).json();
    assert.equal(snap.execution.goal_verification, 'not_verified');
    record.actualToolSummary = snap.tool_results_summary;
    if (snap.tool_results_summary.unsuccessful > 0) {
      await expect(page.locator('.tool-result-warning')).toContainText('لا تُعتبر هذه نتيجة ناجحة');
      await expect(page.locator('.answer')).not.toContainText('5050');
    }
    await noOverflow(page);
    await capture('execution-1440.png');
    await page.getByRole('tab', { name: /النشاط/ }).click();
    await expect(page.locator('.activity-list')).toBeVisible();
    await expect(page.locator('.activity-list')).not.toContainText('import sys');
    await page.getByRole('button', { name: 'إعادة اتصال البث' }).click();
    await page.getByRole('tab', { name: 'المهمة', exact: true }).click();
    await expect(page.locator('.conn')).toContainText('انتهى البث');
    await page.getByRole('button', { name: 'مهمة جديدة', exact: true }).click();
    await expect(page.locator('.answer')).toHaveCount(0);
    await expect(page.getByLabel('ما الذي تريد العمل عليه؟')).toHaveValue('');
  });
  await check('real TXT upload, approval, Markdown download, hash and restore at 390px', 'live-local-deterministic', async () => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(base + '/documents');
    await page.locator('#document').setInputFiles({ name: 'ui1-public-fixture.txt', mimeType: 'text/plain', buffer: Buffer.from('Public UI acceptance fixture.\nArabic: تجربة محلية.\nNo secrets.\n') });
    await page.getByRole('button', { name: 'رفع الملف وعرض الخطة فقط' }).click();
    await expect(page.getByRole('button', { name: 'أوافق على تنفيذ الخطة محليًا' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'تنزيل تقرير Markdown' })).toHaveCount(0);
    const id = await page.locator('#restore').inputValue();
    await page.getByRole('button', { name: 'أوافق على تنفيذ الخطة محليًا' }).click();
    const download = page.getByRole('link', { name: 'تنزيل تقرير Markdown' });
    await expect(download).toBeVisible();
    const response = await page.request.get(base + await download.getAttribute('href'));
    assert.equal(response.status(), 200);
    const bytes = await response.body();
    const { createHash } = await import('node:crypto');
    const digest = createHash('sha256').update(bytes).digest('hex');
    await expect(page.locator('.checksum')).toContainText(digest);
    assert.match(response.headers()['content-disposition'], /attachment/);
    const downloadedEvent = page.waitForEvent('download');
    await download.click();
    const downloaded = await downloadedEvent;
    assert.equal(await downloaded.failure(), null);
    const { readFile } = await import('node:fs/promises');
    const saved = await readFile(await downloaded.path());
    assert.equal(createHash('sha256').update(saved).digest('hex'), digest);
    await noOverflow(page);
    await capture('documents-390.png');
    await page.reload();
    await page.locator('#restore').fill(id);
    await page.getByRole('button', { name: 'استرجاع السجل' }).click();
    await expect(page.getByRole('link', { name: 'تنزيل تقرير Markdown' })).toBeVisible();
    await page.setViewportSize({ width: 1024, height: 1050 });
    await noOverflow(page);
    await capture('documents-1024.png');
  });
  await check('unreachable backend and invalid create response have friendly errors', 'mocked-api-faults', async () => {
    let fixture = await mockPage({ '/health': r => r.abort() });
    await expect(fixture.p.locator('.notice[role=alert]')).toContainText('الخلفية غير متاحة');
    await fixture.p.close();
    fixture = await mockPage({ '/runs': r => json(r, { run_id: 42 }, 202) });
    await begin(fixture.p);
    await expect(fixture.p.locator('.notice[role=alert]')).toContainText('لم نؤكد بدء التنفيذ');
    await expect(fixture.p.getByRole('button', { name: 'تشغيل ▶', exact: true })).toBeEnabled();
    await fixture.p.close();
    fixture = await mockPage({ '/runs': r => r.abort() });
    await begin(fixture.p);
    await expect(fixture.p.locator('.notice[role=alert]')).toContainText('لم نؤكد بدء التنفيذ');
    await fixture.p.close();
  });
  await check('creating state and duplicate submits issue exactly one POST', 'mocked-api-delay', async () => {
    let count = 0;
    const { p } = await mockPage({ '/runs': async r => { count++; await new Promise(resolve => setTimeout(resolve, 350)); return json(r, { run_id: 'run_fixture' }, 202); } });
    await p.getByLabel('ما الذي تريد العمل عليه؟').fill('test');
    await p.locator('.composer').evaluate(el => { el.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); el.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); });
    await expect(p.getByText('جارٍ إنشاء المهمة في الخادم…')).toBeVisible();
    await expect(p.locator('.verification-note')).toBeVisible();
    assert.equal(count, 1);
    await p.close();
  });
  await check('disconnected stream reconnects and EOF does not fabricate completion', 'mocked-sse-fault', async () => {
    let calls = 0;
    const { p, state } = await mockPage({ '/runs/run_fixture/events': route => ++calls === 1 ? route.abort() : route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' }) });
    state({ run_id: 'run_fixture', status: 'running' });
    await begin(p);
    await expect(p.locator('.notice[role=alert]')).toContainText('انقطع بث النشاط');
    await expect(p.locator('.task-overview')).toContainText('قيد التنفيذ');
    await p.getByRole('tab', { name: /النشاط/ }).click();
    await p.getByRole('button', { name: 'إعادة اتصال البث' }).click();
    await p.getByRole('tab', { name: 'المهمة', exact: true }).click();
    await expect(p.locator('.conn')).toContainText('انتهى البث');
    await expect(p.locator('.task-overview')).toContainText('قيد التنفيذ');
    await expect(p.locator('.answer')).toHaveCount(0);
    await expect(p.locator('.notice[role=alert]')).toHaveCount(0);
    await p.close();
  });
  await check('malformed snapshot and SSE fail safely without raw diagnostics', 'mocked-invalid-contract', async () => {
    const { p, state } = await mockPage({ '/runs/run_fixture/events': route => route.fulfill({ status: 200, contentType: 'text/event-stream', body: 'data: {"private":"DO_NOT_RENDER"}\n\n' }) });
    state({ run_id: 'wrong', status: 'completed' });
    await begin(p);
    await expect(p.locator('.notice[role=alert]')).toBeVisible();
    await expect(p.locator('body')).not.toContainText('DO_NOT_RENDER');
    await expect(p.locator('.answer')).toHaveCount(0);
    await p.close();
  });
  await check('failed, cancelled and max_steps remain textual terminal states', 'mocked-terminal-states', async () => {
    for (const [status, text] of [['failed', 'فشل التنفيذ'], ['cancelled', 'أُلغيت المهمة'], ['max_steps', 'توقف عند الحد المسموح']]) {
      const { p, state } = await mockPage();
      state({ run_id: 'run_fixture', status, error: status === 'failed' ? 'PRIVATE_BACKEND_TRACE' : null });
      await begin(p);
      await expect(p.locator('.task-overview')).toContainText(text);
      await expect(p.locator('body')).not.toContainText('PRIVATE_BACKEND_TRACE');
      await expect(p.getByRole('button', { name: 'مهمة جديدة', exact: true })).toBeEnabled();
      await p.close();
    }
  });
  await check('SSE duplicates, CRLF frames and EOF tail preserve sequence without raw payload', 'mocked-sse-frames', async () => {
    const one = JSON.stringify({ seq: 1, type: 'thought', data: { text: 'PRIVATE_FIXTURE' } });
    const two = JSON.stringify({ seq: 2, type: 'tool_result', data: { name: 'fixture_tool', status: 'error', output: 'PRIVATE_FIXTURE' } });
    const { p } = await mockPage({ '/runs/run_fixture/events': route => route.fulfill({ status: 200, contentType: 'text/event-stream', body: `data: ${one}\r\n\r\ndata: ${one}\r\n\r\ndata: ${two}` }) });
    await begin(p);
    await expect(p.locator('.verification-note')).toBeVisible();
    await p.getByRole('tab', { name: /النشاط/ }).click();
    await expect(p.locator('.activity-list > li')).toHaveCount(2);
    await expect(p.locator('.activity-list')).toContainText('فشلت الأداة');
    await expect(p.locator('.activity-list')).not.toContainText('PRIVATE_FIXTURE');
    await p.close();
  });
  await check('document invalid status is rejected without raw error or fabricated report', 'mocked-document-contract', async () => {
    const { p } = await mockPage({ '/document-runs': route => json(route, { run_id: 'doc_' + 'a'.repeat(32), status: 'toString', plan: [], error: 'PRIVATE_TRACE', statistics: null, artifact: null }) });
    await p.goto(base + '/documents');
    await p.locator('#document').setInputFiles({ name: 'public.txt', mimeType: 'text/plain', buffer: Buffer.from('Public fixture.') });
    await p.getByRole('button', { name: 'رفع الملف وعرض الخطة فقط' }).click();
    await expect(p.locator('.notice[role=alert]')).toContainText('لم نؤكد نجاح المعالجة');
    await expect(p.locator('body')).not.toContainText('PRIVATE_TRACE');
    await expect(p.getByRole('link', { name: 'تنزيل تقرير Markdown' })).toHaveCount(0);
    await p.close();
  });
  assert.deepEqual(record.pageErrors, []);
  assert.deepEqual(record.unexpectedHttpErrors, []);
  assert.deepEqual(record.externalRequests, []);
  record.physicalDeviceTested = false;
  record.overall = 'PASS';
} catch {
  record.overall = 'FAIL'; process.exitCode = 1;
} finally {
  await browser.close(); record.finishedAt = new Date().toISOString();
  await writeFile(join(output, 'browser-results.json'), JSON.stringify(record, null, 2) + '\n');
  console.log(`RESULT: ${record.overall}; ${record.tests.filter(t => t.status === 'PASS').length} passed.`);
}
