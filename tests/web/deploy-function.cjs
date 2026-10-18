/**
 * Behavioural contract for the Cloudflare Pages BFF (`functions/api/backend/[[path]].ts`).
 *
 * The proxy is the only component that holds `CELA_API_TOKEN` in the Pages
 * deployment, so these tests attack it the way a hostile browser would: try to
 * read the token back, smuggle a path, re-point the backend, or force a redirect
 * that carries the `Authorization` header somewhere else. `fetch` is stubbed —
 * nothing here touches the network.
 */
const { test, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const { onRequest } = require(path.join(
  __dirname, '../../.cache/deploy-function/functions/api/backend/[[path]].js'));
const access = require(path.join(
  __dirname, '../../.cache/deploy-function/functions/api/access.js'));

const realFetch = globalThis.fetch;
const PUBLIC_TOKEN = 'pub-token-123';
const PRIVATE_TOKEN = 'priv-token-456';
const ENV = {
  CELA_BACKEND_URL: 'https://celia-api.onrender.com',
  CELA_API_TOKEN: PUBLIC_TOKEN,
  CELA_PRIVATE_API_TOKEN: PRIVATE_TOKEN,
};

let calls = [];
afterEach(() => { globalThis.fetch = realFetch; calls = []; });

function stubFetch(handler) {
  calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    return handler(String(url), init, calls.length);
  };
}

function call(pathname, { method = 'GET', env = ENV, headers, body } = {}) {
  const url = pathname.startsWith('http') ? pathname : `https://celia.pages.dev${pathname}`;
  return onRequest({ request: new Request(url, { method, headers, body }), env });
}

async function quiet(run) {
  const original = console.error;
  console.error = () => {};
  try { return await run(); } finally { console.error = original; }
}

function jsonResponse(payload, status = 200, headers = {}) {
  return new Response(JSON.stringify(payload), {
    status, headers: { 'content-type': 'application/json', ...headers },
  });
}

test('public path is forwarded with the public token and the body is untouched', async () => {
  stubFetch(() => jsonResponse({ status: 'ok' }));
  const res = await call('/api/backend/health');

  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, 'https://celia-api.onrender.com/health');
  assert.equal(calls[0].init.headers.get('authorization'), `Bearer ${PUBLIC_TOKEN}`);
  assert.equal(calls[0].init.redirect, 'manual');
  assert.equal(res.status, 200);
  assert.equal(await res.text(), '{"status":"ok"}');
  assert.equal(res.headers.get('authorization'), null);
  assert.equal(res.headers.get('cache-control'), 'no-store, no-transform');
  const text = await (await call('/api/backend/health')).text();
  assert.ok(!text.includes(PUBLIC_TOKEN) && !text.includes(PRIVATE_TOKEN));
});

test('private namespace uses the private token and never the public one', async () => {
  stubFetch(() => jsonResponse({ surface: 'private' }));
  await call('/api/backend/cela/v1/whoami');

  assert.equal(calls[0].url, 'https://celia-api.onrender.com/cela/v1/whoami');
  assert.equal(calls[0].init.headers.get('authorization'), `Bearer ${PRIVATE_TOKEN}`);
});

test('a browser-supplied Authorization header cannot override the injected token', async () => {
  stubFetch(() => jsonResponse({ status: 'ok' }));
  await call('/api/backend/health', { headers: { authorization: 'Bearer attacker-token' } });

  assert.equal(calls[0].init.headers.get('authorization'), `Bearer ${PUBLIC_TOKEN}`);
});

test('after_seq survives the proxy so SSE reconnects resume instead of replaying', async () => {
  stubFetch(() => jsonResponse({}));
  await call('/api/backend/runs/run_abc/events?after_seq=42');

  assert.equal(
    calls[0].url,
    'https://celia-api.onrender.com/runs/run_abc/events?after_seq=42',
  );
});

test('SSE streams through: event-stream type, no-transform, identical chunk bytes', async () => {
  const encoder = new TextEncoder();
  const chunks = ['data: {"type":"run_started","seq":1}\n\n', 'data: {"type":"step_started","seq":2}\n\n'];
  stubFetch(() => new Response(new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  }), { status: 200, headers: { 'content-type': 'text/event-stream' } }));

  const res = await call('/api/backend/runs/run_abc/events?after_seq=1');
  assert.equal(res.headers.get('content-type'), 'text/event-stream');
  assert.equal(res.headers.get('cache-control'), 'no-store, no-transform');
  assert.equal(res.headers.get('x-accel-buffering'), 'no');
  assert.notEqual(res.body, null);
  assert.equal(await res.text(), chunks.join(''));
});

test('POST body and JSON content type are forwarded', async () => {
  stubFetch(() => jsonResponse({ run_id: 'run_1' }, 201));
  const res = await call('/api/backend/runs', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ task: 'sum 0..100' }),
  });

  assert.equal(calls[0].init.method, 'POST');
  assert.equal(calls[0].init.headers.get('content-type'), 'application/json');
  // The body is buffered, not streamed: `duplex` is not portable across
  // Workers and Node's undici, and buffering is what makes the size cap real.
  assert.ok(calls[0].init.body instanceof ArrayBuffer);
  assert.equal(new TextDecoder().decode(calls[0].init.body), '{"task":"sum 0..100"}');
  assert.equal(res.status, 201);
  assert.equal(await res.text(), '{"run_id":"run_1"}');
});

test('missing token is a named 503 and never echoes the other token', async () => {
  stubFetch(() => jsonResponse({}));
  const res = await call('/api/backend/cela/v1/whoami', {
    env: { CELA_BACKEND_URL: ENV.CELA_BACKEND_URL, CELA_API_TOKEN: PUBLIC_TOKEN },
  });

  assert.equal(res.status, 503);
  const body = await res.text();
  assert.match(body, /CELA_PRIVATE_API_TOKEN/);
  assert.ok(!body.includes(PUBLIC_TOKEN));
  assert.equal(calls.length, 0);
  assert.equal(JSON.parse(body).retryable, true);
});

test('http backends are refused only outside loopback', async () => {
  stubFetch(() => jsonResponse({ status: 'ok' }));
  const cleartext = await call('/api/backend/health', {
    env: { ...ENV, CELA_BACKEND_URL: 'http://celia-api.example.com' },
  });
  assert.equal(cleartext.status, 503);
  assert.equal(JSON.parse(await cleartext.text()).code, 'backend_not_configured');
  assert.equal(calls.length, 0);

  const loopback = await call('/api/backend/health', {
    env: { ...ENV, CELA_BACKEND_URL: 'http://127.0.0.1:8080' },
  });
  assert.equal(loopback.status, 200);
  assert.equal(calls[0].url, 'http://127.0.0.1:8080/health');
});

test('non-http schemes and embedded credentials are refused', async () => {
  stubFetch(() => jsonResponse({}));
  for (const backend of ['file:///etc/passwd', 'https://user:pass@celia-api.onrender.com']) {
    const res = await call('/api/backend/health', { env: { ...ENV, CELA_BACKEND_URL: backend } });
    assert.equal(res.status, 503, backend);
    assert.equal(calls.length, 0, backend);
  }
});

test('encoded dot segments cannot escape the backend prefix', async () => {
  stubFetch(() => jsonResponse({}));
  const res = await call('https://celia.pages.dev/api/backend/%2e%2e/%2e%2e/admin');
  assert.equal(res.status, 400);
  assert.equal(calls.length, 0);
});

test('a sleeping backend becomes a retryable 503, an unreachable one a 502', async () => {
  // `AbortSignal.timeout` uses an unref'd timer, so the stub holds a real timer
  // of its own: without it Node would empty the event loop and cancel the test.
  stubFetch((_url, init) => new Promise((_resolve, reject) => {
    const keepAlive = setTimeout(() => reject(new Error('stub never fired')), 5_000);
    init.signal.addEventListener('abort', () => {
      clearTimeout(keepAlive);
      reject(Object.assign(new Error('timed out'), { name: 'TimeoutError' }));
    });
  }));
  const timeout = await quiet(() => call('/api/backend/health', {
    env: { ...ENV, CELA_BACKEND_TIMEOUT_MS: '100' },
  }));
  assert.equal(timeout.status, 503);
  assert.equal(timeout.headers.get('retry-after'), '5');
  assert.equal(JSON.parse(await timeout.text()).code, 'backend_starting');

  stubFetch(() => { throw new TypeError('connection refused'); });
  const down = await quiet(() => call('/api/backend/health'));
  assert.equal(down.status, 502);
  assert.equal(JSON.parse(await down.text()).code, 'backend_unreachable');
});

test('same-origin redirects are followed, cross-origin ones are refused', async () => {
  stubFetch((url, _init, n) => (n === 1
    ? new Response(null, { status: 307, headers: { location: '/runs' } })
    : jsonResponse({ ok: true })));
  const followed = await call('/api/backend/runs/');
  assert.equal(followed.status, 200);
  assert.equal(calls.length, 2);
  assert.equal(calls[1].url, 'https://celia-api.onrender.com/runs');
  assert.equal(calls[1].init.headers.get('authorization'), `Bearer ${PUBLIC_TOKEN}`);

  stubFetch(() => new Response(null, {
    status: 302, headers: { location: 'https://credential-harvester.example/collect' },
  }));
  const refused = await quiet(() => call('/api/backend/health'));
  assert.equal(refused.status, 502);
  assert.equal(JSON.parse(await refused.text()).code, 'backend_redirect_refused');
  assert.equal(calls.length, 1);
});

test('oversized bodies are refused before reaching the backend', async () => {
  stubFetch(() => jsonResponse({}));
  const res = await call('/api/backend/runs', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: 'x'.repeat(1_048_577),
  });
  assert.equal(res.status, 413);
  assert.equal(JSON.parse(await res.text()).code, 'payload_too_large');
  assert.equal(calls.length, 0);
});

test('unsupported methods and empty paths are rejected before any fetch', async () => {
  stubFetch(() => jsonResponse({}));
  const deleted = await call('/api/backend/runs/run_1', { method: 'DELETE' });
  assert.equal(deleted.status, 405);
  assert.equal(deleted.headers.get('allow'), 'GET, HEAD, POST');

  const empty = await call('/api/backend/');
  assert.equal(empty.status, 400);
  assert.equal(calls.length, 0);
});

test('204 responses keep no body (Workers rejects bodyful 204s)', async () => {
  stubFetch(() => new Response(null, { status: 204 }));
  const res = await call('/api/backend/runs/run_1/approve', { method: 'POST', body: '{}' });
  assert.equal(res.status, 204);
  assert.equal(res.body, null);
});

// ---- Operator access gate (auth review Finding 1) ---------------------------

const GATE_SECRET = 'gate-secret-789';
const GATED_ENV = { ...ENV, CELA_WEB_ACCESS_SECRET: GATE_SECRET };

test('gate: without the operator secret the proxy refuses before any backend or token state', async () => {
  stubFetch(() => jsonResponse({}));
  const denied = await call('/api/backend/health', { env: GATED_ENV });
  assert.equal(denied.status, 401);
  const body = JSON.parse(await denied.text());
  assert.equal(body.code, 'access_required');
  assert.equal(body.retryable, false);
  assert.equal(calls.length, 0);
});

test('gate: a valid cookie or header unlocks the proxy; a wrong one does not', async () => {
  stubFetch(() => jsonResponse({ status: 'ok' }));

  const viaCookie = await call('/api/backend/health', {
    env: GATED_ENV,
    headers: { cookie: `celia_access=${GATE_SECRET}` },
  });
  assert.equal(viaCookie.status, 200);
  assert.equal(calls[0].init.headers.get('authorization'), `Bearer ${PUBLIC_TOKEN}`);

  const viaHeader = await call('/api/backend/cela/v1/whoami', {
    env: GATED_ENV,
    headers: { 'x-celia-access': GATE_SECRET },
  });
  assert.equal(viaHeader.status, 200);
  assert.equal(calls[1].init.headers.get('authorization'), `Bearer ${PRIVATE_TOKEN}`);

  const wrong = await call('/api/backend/health', {
    env: GATED_ENV,
    headers: { cookie: 'celia_access=not-the-secret' },
  });
  assert.equal(wrong.status, 401);
  assert.equal(calls.length, 2);
});

test('access function twin: GET/POST/DELETE keep the same contract', async () => {
  const env = { CELA_WEB_ACCESS_SECRET: GATE_SECRET };

  const status = await access.onRequest({ request: new Request('https://celia.pages.dev/api/access'), env });
  assert.deepEqual(await status.json(), { required: true, authorized: false });

  const wrong = await access.onRequest({
    request: new Request('https://celia.pages.dev/api/access', { method: 'POST', body: '{"secret":"guess"}' }),
    env,
  });
  assert.equal(wrong.status, 401);
  assert.equal(wrong.headers.get('set-cookie'), null);

  const right = await access.onRequest({
    request: new Request('https://celia.pages.dev/api/access', { method: 'POST', body: JSON.stringify({ secret: GATE_SECRET }) }),
    env,
  });
  assert.equal(right.status, 200);
  const cookie = right.headers.get('set-cookie');
  assert.match(cookie, /celia_access=/);
  assert.match(cookie, /HttpOnly/);

  const locked = await access.onRequest({ request: new Request('https://celia.pages.dev/api/access', { method: 'DELETE' }), env });
  assert.equal(locked.status, 200);
  assert.match(locked.headers.get('set-cookie'), /Max-Age=0/);
});
