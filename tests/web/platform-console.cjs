const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const root = path.resolve(process.env.PLATFORM_CONSOLE_OUTPUT || '.cache/platform-console');
const React = require('react');
const { renderToStaticMarkup: render } = require('react-dom/server');
const platform = require(path.join(root, 'lib/platform.js'));
const consoleUi = require(path.join(root, 'components/platform/console.js'));

const infoPayload = () => ({
  status: 'ok',
  product: 'Celia.sh — Mission OS',
  version: '0.1.0',
  generated_at: '2026-10-20T08:00:00Z',
  process: {
    started_at: '2026-10-20T07:00:00Z',
    uptime_seconds: 3600.5,
    python_version: '3.11.2',
    python_implementation: 'cpython',
    platform: 'linux',
    machine: 'x86_64',
  },
  deployment: {
    mode: 'demo',
    app_name: 'celia-mission-os',
    listen_port: 8080,
    region: null,
    public_url_configured: false,
  },
  execution: { kind: 'scripted', model_name: null, goal_verification: 'not_verified' },
  sandbox: {
    code_exec_mode: 'disabled',
    requested_backend: null,
    backend: 'DisabledBackend',
    configuration_error: null,
    executes_untrusted_code: false,
    isolation: 'none',
    network_isolation: 'n/a',
  },
  store: {
    backend: 'InMemoryRunStore',
    scope: 'process',
    durable: false,
    target: null,
    ledger: { enabled: true, path: '.cela/ledger.jsonl', schema_version: 1, entries: 2, corrupt_lines: 0 },
  },
  registry: {
    configured: true,
    source: 'CELA_IMAGE_REF',
    ignored_invalid_reference: false,
    reference: 'ghcr.io/owner/open_source:edge',
    host: 'ghcr.io',
    repository: 'owner/open_source',
    tag: 'edge',
    digest: null,
    build_sha: 'deadbeef',
    build_time: '2026-10-20T07:00:00Z',
  },
  tools: { count: 2, registered: ['inspect_public_url', 'python_execute'] },
});

const ledgerPayload = () => ({
  ledger: { enabled: true, path: '.cela/ledger.jsonl', schema_version: 1, entries: 1, corrupt_lines: 0 },
  entries: [
    {
      seq: 1,
      run_id: 'run_abc123',
      task: 'Sum 0..100',
      mode: 'react',
      status: 'completed',
      outcome: 'success',
      started_at: '2026-10-20T07:00:00Z',
      finished_at: '2026-10-20T07:00:02Z',
      duration_ms: 2000,
      steps_used: 2,
      tool_calls: 1,
      tool_failures: 0,
      artifacts: [],
      error: null,
      recorded_at: '2026-10-20T07:00:03Z',
    },
  ],
  summary: {
    total: 1,
    outcomes: { success: 1, failed: 0, budget: 0, aborted: 0 },
    modes: { react: 1 },
    statuses: { completed: 1 },
    success_rate: 1,
    totals: { steps_used: 2, tokens_used: 0, tool_calls: 1, tool_failures: 0, replans: 0, reviews: 0 },
    duration_ms: { count: 1, mean: 2000, p50: 2000, p95: 2000, max: 2000 },
    first_recorded_at: '2026-10-20T07:00:03Z',
    last_recorded_at: '2026-10-20T07:00:03Z',
    corrupt_lines: 0,
  },
});

test('platform info parses a complete payload', () => {
  const parsed = platform.parsePlatformInfo(infoPayload());
  assert.equal(parsed.version, '0.1.0');
  assert.equal(parsed.store.durable, false);
  assert.equal(parsed.registry.tag, 'edge');
  assert.equal(parsed.execution.model_name, null);
  assert.deepEqual(parsed.tools.registered, ['inspect_public_url', 'python_execute']);
});

test('malformed platform info is rejected, not rendered as healthy', () => {
  for (const value of [
    null,
    {},
    { ...infoPayload(), status: 'degraded' },
    { ...infoPayload(), store: undefined },
    { ...infoPayload(), tools: { count: 1 } },
    { ...infoPayload(), registry: { ...infoPayload().registry, configured: 'yes' } },
    { ...infoPayload(), process: { ...infoPayload().process, uptime_seconds: 'soon' } },
  ]) {
    assert.throws(() => platform.parsePlatformInfo(value), /invalid_api_response/);
  }
});

test('missing values stay explicit nulls rather than guessed strings', () => {
  const parsed = platform.parsePlatformInfo(infoPayload());
  assert.equal(parsed.deployment.region, null);
  assert.equal(parsed.registry.digest, null);
  assert.equal(parsed.execution.model_name, null);
});

test('ledger report parses entries and summary', () => {
  const parsed = platform.parseLedgerReport(ledgerPayload());
  assert.equal(parsed.entries.length, 1);
  assert.equal(parsed.entries[0].status, 'completed');
  assert.equal(parsed.summary.success_rate, 1);
});

test('ledger report rejects a malformed summary or entry list', () => {
  const brokenSummary = ledgerPayload();
  brokenSummary.summary.duration_ms = null;
  assert.throws(() => platform.parseLedgerReport(brokenSummary), /invalid_api_response/);
  const brokenEntries = { ...ledgerPayload(), entries: {} };
  assert.throws(() => platform.parseLedgerReport(brokenEntries), /invalid_api_response/);
});

test('formatters describe time without over-claiming precision', () => {
  assert.equal(platform.formatUptime(0), '00:00:00');
  assert.equal(platform.formatUptime(90061), '1d 01:01:01');
  assert.equal(platform.formatUptime(-5), 'غير معروف');
  assert.equal(platform.formatDuration(null), '—');
  assert.equal(platform.formatDuration(500), '500 ms');
  assert.equal(platform.formatDuration(1500), '1.5 s');
  assert.equal(platform.formatPercent(0.5), '50.0%');
});

test('code execution is only "real" for the documented modes', () => {
  assert.equal(platform.executesRealCode('disabled'), false);
  assert.equal(platform.executesRealCode('unknown-mode'), false);
  assert.equal(platform.executesRealCode('local'), true);
  assert.equal(platform.executesRealCode('e2b'), true);
});

test('a fact renders its value and an explicit absence', () => {
  const html = render(
    React.createElement(consoleUi.Fact, {
      label: 'الوسم',
      value: React.createElement(consoleUi.NotConfigured, null, 'بلا وسم صريح'),
    }),
  );
  assert.match(html, /الوسم/);
  assert.match(html, /بلا وسم صريح/);
  assert.match(html, /fact-missing/);
});

test('a console panel renders its eyebrow, title and status', () => {
  const html = render(
    React.createElement(
      consoleUi.ConsolePanel,
      {
        eyebrow: 'الهوية والإصدار',
        title: 'celia-mission-os',
        status: React.createElement('span', null, 'وضع تجريبي'),
      },
      React.createElement('p', null, 'FACTS'),
    ),
  );
  assert.match(html, /الهوية والإصدار/);
  assert.match(html, /celia-mission-os/);
  assert.match(html, /وضع تجريبي/);
  assert.match(html, /FACTS/);
});

test('copy is disabled when there is nothing real to copy', () => {
  const html = render(
    React.createElement(consoleUi.CopyButton, { value: '', label: 'نسخ أمر السحب' }),
  );
  assert.match(html, /disabled/);
});

test('an empty copy target renders no invented pull command', () => {
  const html = render(
    React.createElement(consoleUi.CopyButton, {
      value: 'docker pull ghcr.io/owner/open_source:edge',
      label: 'نسخ أمر السحب',
    }),
  );
  assert.doesNotMatch(html, /docker pull/);
  assert.match(html, /نسخ أمر السحب/);
});
