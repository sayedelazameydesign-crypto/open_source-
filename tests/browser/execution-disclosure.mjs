/** UI-0: real preview regressions plus explicitly mocked old-API fallback. */
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { resolve, join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { tmpdir } from 'node:os';
import { mkdir, writeFile } from 'node:fs/promises';

const packages = resolve(process.env.E2E_BROWSER_PACKAGES ?? '.cache/ui0-browser');
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
const output = resolve(process.env.E2E_OUTPUT ?? '.cache/ui0-browser/results');
await mkdir(output, { recursive: true });
const record = { tests: [], pageErrors: [], unexpectedHttpErrors: [], externalRequests: [],
  startedAt: new Date().toISOString(), overall: 'RUNNING' };
const browser = await chromium.launch({ executablePath: await binary.executablePath(), args,
  env: { ...process.env, LD_LIBRARY_PATH: join(tmpdir(), 'al2023/lib') }, headless: true });
record.browser = browser.version();
const context = await browser.newContext({ viewport: { width: 1280, height: 950 } });
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
  } catch {
    record.tests.push({ name, kind, status: 'FAIL' });
    console.error(`FAIL: ${name}; no response bodies or credentials logged`);
    throw new Error('UI acceptance failed');
  }
}

async function run(mode, task, executor) {
  if (await page.getByRole('button', { name: 'مهمة جديدة', exact: true }).count()) await page.getByRole('button', { name: 'مهمة جديدة', exact: true }).click();
  await page.locator('.composer textarea').fill(task);
  await page.locator('.composer select').selectOption(mode);
  const created = page.waitForResponse(r => r.request().method() === 'POST'
    && new URL(r.url()).pathname === '/api/backend/runs');
  await page.getByRole('button', { name: 'تشغيل ▶' }).click();
  const response = await created;
  assert.equal(response.status(), 202);
  const id = (await response.json()).run_id;
  await expect(page.locator('.execution-disclosure')).toContainText(executor);
  await expect(page.locator('.answer')).toBeVisible();
  await expect(page.locator('.verification-note')).toContainText('completed');
  await expect(page.locator('.conn')).toContainText('انتهى البث');
  const state = await (await page.request.get(base + '/api/backend/runs/' + id)).json();
  assert.equal(state.execution.kind, 'scripted');
  assert.equal(state.execution.model_name, executor);
  assert.equal(state.execution.goal_verification, 'not_verified');
  assert(state.recorded_tool_results > 0);
  assert(!state.final_answer.trimStart().startsWith('[{'));
  await expect(page.locator('.execution-disclosure')).toContainText('وجهة التوجيه المقترحة ليست دليلًا');
  await expect(page.locator('.verification-note')).toContainText('غير متحقق');
  await expect(page.locator('.notice')).toHaveCount(0);
  return state;
}

try {
  await check('reference executor is disclosed before any task', 'live', async () => {
    await page.goto(base);
    await expect(page.locator('.execution-disclosure')).toContainText('وضع تجريبي — نموذج محاكاة');
    await expect(page.locator('.execution-disclosure')).toContainText('demo-react');
  });
  await check('original ReAct request invokes demo tools, not a raw planner answer', 'live', async () => {
    const state = await run('react', 'Navigate to example.org and check the model list', 'demo-react');
    record.reactToolResults = state.tool_results_summary;
    record.goalVerification = state.execution.goal_verification;
    if (state.tool_results_summary.unsuccessful > 0) {
      assert(state.final_answer.includes('not verified'));
      assert(!state.final_answer.includes('5050'));
      await expect(page.locator('.tool-result-warning')).toContainText('غير ناجحة');
    } else {
      assert(state.final_answer.includes('5050'));
    }
    assert(!state.results.some(r => r.tool.startsWith('browser')));
    await expect(page.locator('.execution-disclosure')).toContainText('الطلب لا يضمن سلوكًا عامًا أو تصفحًا حقيقيًا');
    await page.locator('.desktop-evidence .routing-details summary').click();
    await expect(page.locator('.desktop-evidence').getByRole('heading', { name: 'توجيه مقترح — لا يثبت استدعاء النموذج' })).toBeVisible();
    await page.screenshot({ path: join(output, 'react-disclosure.png'), fullPage: true });
  });
  await check('flow and auto each bind the planner independently, then ReAct resets correctly', 'live', async () => {
    assert.equal((await run('flow', 'Execute the plan', 'demo-planner')).mode, 'flow');
    assert.equal((await run('auto', 'Plan a multi-step research comparison', 'demo-planner')).mode, 'flow');
    assert.equal((await run('react', 'Sum 0..100', 'demo-react')).mode, 'react');
  });
  await check('completed stream reconnect does not change completion or invent an error', 'live', async () => {
    await page.getByRole('tab', { name: /النشاط/ }).click();
    await page.getByRole('button', { name: 'إعادة اتصال البث' }).click();
    await page.getByRole('tab', { name: 'المهمة', exact: true }).click();
    await expect(page.locator('.conn')).toContainText('انتهى البث');
    await expect(page.locator('.verification-note')).toContainText('completed');
    await expect(page.locator('.notice')).toHaveCount(0);
  });
  await check('missing runtime metadata remains unknown, not inferred from routing', 'mocked-old-api', async () => {
    const legacy = await context.newPage();
    await legacy.route('**/api/backend/runtime-info', route => route.fulfill({
      status: 200, contentType: 'application/json', body: JSON.stringify({}),
    }));
    await legacy.goto(base);
    await expect(legacy.locator('.execution-disclosure')).toContainText('نوع المنفذ غير متحقق');
    await expect(legacy.locator('.execution-disclosure')).not.toContainText('demo-react');
    await legacy.close();
  });
  await check('document page states deterministic local processing without an AI model', 'live', async () => {
    await page.goto(base + '/documents');
    await expect(page.getByRole('note')).toContainText('معالجة محلية حتمية — بلا نموذج');
  });
  assert.deepEqual(record.pageErrors, []);
  assert.deepEqual(record.unexpectedHttpErrors, []);
  assert.deepEqual(record.externalRequests, []);
  record.overall = 'PASS';
} catch {
  record.overall = 'FAIL';
  process.exitCode = 1;
} finally {
  await browser.close();
  record.finishedAt = new Date().toISOString();
  await writeFile(join(output, 'browser-results.json'), JSON.stringify(record, null, 2) + '\n');
  console.log(`RESULT: ${record.overall}; ${record.tests.filter(t => t.status === 'PASS').length} passed.`);
}
