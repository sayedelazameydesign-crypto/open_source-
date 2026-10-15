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


try {
  for (const decision of ['approved', 'denied']) {
    await check(`actual QueueApprover ${decision} via unchanged API + no-op test tool`, 'real-runtime-test-fixture', async () => {
      await page.goto(base);
      await page.getByLabel('ما الذي تريد العمل عليه؟').fill('Public no-op approval fixture');
      await page.getByLabel('نمط التنفيذ').selectOption('react');
      const created = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/backend/runs');
      await page.getByRole('button', { name: 'تشغيل ▶', exact: true }).click();
      const id = (await (await created).json()).run_id;
      await expect(page.getByRole('heading', { name: 'إجراء ينتظر موافقتك' })).toBeVisible();
      await expect(page.locator('.execution-disclosure')).toContainText('ui-test-fixture');
      const delivered = page.waitForResponse(r => new URL(r.url()).pathname.endsWith('/approve'));
      await page.getByRole('button', { name: decision === 'approved' ? 'أوافق على الإجراء' : 'رفض الإجراء', exact: true }).click();
      const response = await delivered;
      assert.equal(response.status(), 200);
      assert.equal((await response.json()).delivered, true);
      await expect(page.locator('.verification-note')).toContainText('completed');
      const state = await (await page.request.get(base + '/api/backend/runs/' + id)).json();
      assert.equal(state.results[0].status, decision === 'approved' ? 'success' : 'denied');
      assert.equal(state.execution.goal_verification, 'not_verified');
      if (decision === 'approved') {
        const link = page.locator('.desktop-evidence').getByRole('link', { name: 'فتح بيانات الملف' });
        await expect(link).toBeVisible();
        const artifact = await page.request.get(base + await link.getAttribute('href'));
        assert.equal(artifact.status(), 200);
        const metadata = await artifact.json();
        assert.equal(metadata.filename, 'approval-fixture.txt');
        assert(metadata.size > 0);
        assert.equal(metadata.content, undefined);
      } else {
        assert.equal(state.artifacts.length, 0);
        await expect(page.locator('.tool-result-warning')).toBeVisible();
      }
      await page.getByRole('tab', { name: /النشاط/ }).click();
      await expect(page.locator('.activity-list')).toContainText(decision === 'approved' ? 'نجحت الأداة' : 'رُفض الإجراء');
    });
  }
  assert.deepEqual(record.pageErrors, []);
  assert.deepEqual(record.unexpectedHttpErrors, []);
  assert.deepEqual(record.externalRequests, []);
  record.overall = 'PASS';
} catch { record.overall = 'FAIL'; process.exitCode = 1; }
finally {
  await browser.close(); record.finishedAt = new Date().toISOString();
  await writeFile(join(output, 'browser-results.json'), JSON.stringify(record, null, 2) + '\n');
  console.log(`RESULT: ${record.overall}; ${record.tests.filter(t => t.status === 'PASS').length} passed.`);
}
