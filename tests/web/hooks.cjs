const { test, beforeEach, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const React = require('react');
const { createRoot } = require('react-dom/client');
const { useAgentRun } = require(path.resolve('.cache/web-hooks/hooks/use-agent-run.js'));
const { act } = React;

let dom, root, run, intervals, intervalCount, requests, streams, nextRun;
let snapshotResponse;
let originals;
const json = (data) => new Response(JSON.stringify(data), {
  headers: { 'Content-Type': 'application/json' },
});
const snapshot = (id, status = 'running') => json({ run_id: id, status });
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
function Probe() { run = useAgentRun(); return null; }

beforeEach(async () => {
  originals = Object.fromEntries(['window', 'document', 'sessionStorage', 'fetch',
    'setInterval', 'clearInterval', 'IS_REACT_ACT_ENVIRONMENT',
  ].map((key) => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  dom = new JSDOM('<div id="root"></div>', { url: 'https://workspace.test' });
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.sessionStorage = dom.window.sessionStorage;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  intervals = new Map(); intervalCount = 0; requests = []; streams = new Map(); nextRun = 0;
  globalThis.setInterval = (fn, ms) => {
    assert.equal(ms, 1500, 'the polling clock must be 1.5 seconds');
    intervals.set(++intervalCount, fn);
    return intervalCount;
  };
  globalThis.clearInterval = (id) => intervals.delete(id);
  snapshotResponse = (id) => snapshot(id);
  globalThis.fetch = async (url, options = {}) => {
    if (url.endsWith('/health')) return json({ status: 'ok' });
    if (url.endsWith('/runtime-info')) return json({});
    if (url === '/api/backend/runs') return json({ run_id: `run_${++nextRun}` });
    if (url.endsWith('/approve')) return json({ delivered: true });
    if (url.includes('/events?')) {
      const id = url.split('/')[4];
      const body = new ReadableStream({ start(controller) {
        streams.set(id, controller);
        options.signal.addEventListener('abort', () => controller.close(), { once: true });
      } });
      return new Response(body);
    }
    const id = url.split('/').pop();
    assert.match(url, /^\/api\/backend\/runs\/run_\d+$/);
    requests.push(id);
    return snapshotResponse(id);
  };
  root = createRoot(document.getElementById('root'));
  await act(async () => root.render(React.createElement(React.StrictMode, null,
    React.createElement(Probe))));
});
afterEach(async () => {
  await act(async () => root.unmount());
  assert.equal(intervals.size, 0, 'unmount must clear the polling clock');
  dom.window.close();
  for (const [key, descriptor] of Object.entries(originals)) {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor);
    else delete globalThis[key];
  }
});
const start = () => act(async () => run.start('a long running task', 'react'));
const tick = () => act(async () => { for (const fn of intervals.values()) fn(); });
const emit = (type, seq) => act(async () => {
  streams.get(run.runId).enqueue(new TextEncoder().encode(
    `data: ${JSON.stringify({ type, seq, data: {} })}\n\n`));
});

test('one stable 1500ms clock per active run, even after snapshots and SSE updates', async () => {
  await start();
  assert.deepEqual(requests, ['run_1']);
  const clock = [...intervals.keys()];
  for (let i = 0; i < 4; i++) {
    await tick();
    assert.equal(requests.length, i + 2);
    assert.deepEqual([...intervals.keys()], clock, 'snapshot updates must not restart the interval');
  }
  await emit('plan_created', 1);
  assert.equal(requests.length, 6, 'SSE may request a snapshot between ticks');
  await tick();
  assert.equal(requests.length, 7);
  assert.equal(intervalCount, 1);
});

test('slow snapshots never overlap across polling, SSE and approvals', async () => {
  const pending = deferred();
  snapshotResponse = () => pending.promise;
  await start();
  await tick(); await tick();
  await emit('approval_requested', 1);
  await act(async () => run.approve('tool_1', 'approved'));
  assert.equal(requests.length, 1, 'all refresh paths share the in-flight guard');
  await act(async () => pending.resolve(snapshot('run_1')));
  snapshotResponse = (id) => snapshot(id);
  await tick();
  assert.equal(requests.length, 2, 'guard releases after completion');
});

test('request failures release the guard and the next tick retries', async () => {
  snapshotResponse = async () => { throw new Error('offline'); };
  await start();
  assert.match(run.notice, /تعذر تحديث/);
  snapshotResponse = (id) => snapshot(id);
  await tick();
  assert.equal(requests.length, 2);
  assert.equal(run.snapshot.status, 'running');
});

test('invalid snapshots release the guard and do not assert success', async () => {
  snapshotResponse = () => json({ run_id: 'wrong', status: 'completed' });
  await start();
  assert.equal(run.snapshot, null);
  snapshotResponse = (id) => snapshot(id);
  await tick();
  assert.equal(requests.length, 2);
  assert.equal(run.snapshot.status, 'running');
});

for (const terminal of ['completed', 'failed', 'cancelled', 'max_steps']) {
  test(`periodic requests stop at terminal status ${terminal}`, async () => {
    await start();
    snapshotResponse = (id) => snapshot(id, terminal);
    await tick();
    assert.equal(run.snapshot.status, terminal);
    await tick(); await tick();
    assert.equal(requests.length, 2);
  });
}

test('a new run is not blocked or overwritten by a stale in-flight snapshot', async () => {
  const old = deferred();
  snapshotResponse = () => old.promise;
  await start();
  await act(async () => run.reset());
  assert.equal(intervals.size, 0);
  snapshotResponse = (id) => snapshot(id);
  await start();
  assert.equal(run.runId, 'run_2');
  await act(async () => old.resolve(snapshot('run_1', 'completed')));
  assert.equal(run.snapshot.run_id, 'run_2');
  assert.equal(run.snapshot.status, 'running');
  await tick();
  assert.deepEqual(requests, ['run_1', 'run_2', 'run_2']);
});

test('reset ignores late responses and stops all periodic requests', async () => {
  const old = deferred();
  snapshotResponse = () => old.promise;
  await start();
  await act(async () => run.reset());
  await act(async () => old.resolve(snapshot('run_1', 'completed')));
  await tick();
  assert.equal(run.snapshot, null);
  assert.equal(run.runId, null);
  assert.equal(intervals.size, 0);
  assert.equal(requests.length, 1);
});

test('start persists ?run_id= in URL and loadRun restores a run by id', async () => {
  await start();
  assert.equal(new URL(window.location.href).searchParams.get('run_id'), 'run_1');
  await act(async () => run.loadRun('run_9'));
  assert.equal(run.runId, 'run_9');
  assert.equal(new URL(window.location.href).searchParams.get('run_id'), 'run_9');
  assert.equal(run.snapshot.run_id, 'run_9');
  await act(async () => run.reset());
  assert.equal(new URL(window.location.href).searchParams.get('run_id'), null);
});
