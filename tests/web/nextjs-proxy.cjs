/**
 * Behavioural contract for the Next.js BFF (`app/api/backend/[...path]/route.ts`).
 *
 * This suite is the Node twin of `tests/web/deploy-function.cjs`: it attacks the
 * Next.js proxy the same hostile way a browser would — try to read the token
 * back, smuggle a path, re-point the backend, or force a redirect that carries
 * the `Authorization` header somewhere else — so both proxies of the security
 * contract (ADR-0005) stay in lockstep. `fetch` is stubbed; nothing touches the
 * network. Env vars are used per test and restored in `afterEach`.
 */
const { test, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const { GET, POST, HEAD } = require(path.join(
  __dirname, '../../.cache/nextjs-proxy/app/api/backend/[...path]/route.js'));
const access = require(path.join(
  __dirname, '../../.cache/nextjs-proxy/app/api/access/route.js'));
const gate = require(path.join(__dirname, '../../.cache/nextjs-proxy/lib/access-gate.js'));

const realFetch = globalThis.fetch;
const PUBLIC_TOKEN = 'pub-token-123';
const PRIVATE_TOKEN = 'priv-token-456';
const ENV = {
  CELA_BACKEND_URL: 'https://celia-api.onrender.com',
  CELA_API_TOKEN: PUBLIC_TOKEN,
  CELA_PRIVATE_API_TOKEN: PRIVATE_TOKEN,
  // The default fixture models local development, where the optional access
  // gate remains open when no operator secret is configured.
  NODE_ENV: 'development',
};
const ENV_KEYS = ['CELA_BACKEND_URL', 'CELA_API_TOKEN', 'CELA_PRIVATE_API_TOKEN', 'CELA_BACKEND_TIMEOUT_MS', 'CELA_WEB_ACCESS_SECRET', 'CELA_WEB_GATE_REQUIRED', 'NODE_ENV'];

const originalEnv = {};
for (const key of ENV_KEYS) originalEnv[key] = process.env[key];

let calls = [];
afterEach(() => {
  globalThis.fetch = realFetch;
  calls = [];
  for (const key of ENV_KEYS) {
    if (originalEnv[key] === undefined) delete process.env[key];
    else process.env[key] = originalEnv[key];
  }
});

function setEnv(env) {
  for (const key of ENV_KEYS) delete process.env[key];
  for (const [key, value] of Object.entries(env)) process.env[key] = value;
}

function stubFetch(handler) {
  calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    return handler(String(url), init, calls.length);
  };
}

function call(pathname, { method = 'GET', env = ENV, headers, body } = {}) {
  setEnv(env);
  const url = pathname.startsWith('http') ? pathname : `https://celia.example${pathname}`;
  const request = new Request(url, { method, headers, body });
  const ctx = { params: Promise.resolve({ path: pathname.split('?')[0].split('/').filter(Boolean).slice(2) }) };
  const handler = method === 'POST' ? POST : method === 'HEAD' ? HEAD : GET;
  return handler(request, ctx);
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
  assert.equal(res.headers.get('x-celia-bff'), 'nextjs-route');
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

test('cookies and hop-by-hop browser headers are never forwarded upstream', async () => {
  stubFetch(() => jsonResponse({ status: 'ok' }));
  await call('/api/backend/health', {
    headers: {
      cookie: 'session=stolen',
      'x-forwarded-for': '1.2.3.4',
      'x-celia-idempotency-key': 'idem-1',
    },
  });

  assert.equal(calls[0].init.headers.get('cookie'), null);
  assert.equal(calls[0].init.headers.get('x-forwarded-for'), null);
  assert.equal(calls[0].init.headers.get('x-celia-idempotency-key'), 'idem-1');
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
  // The body is buffered, not streamed: that is what makes the size cap real.
  assert.ok(calls[0].init.body instanceof ArrayBuffer);
  assert.equal(new TextDecoder().decode(calls[0].init.body), '{"task":"sum 0..100"}');
  assert.equal(res.status, 201);
  assert.equal(await res.text(), '{"run_id":"run_1"}');
});

test('missing token is a named 503 and never echoes the other token', async () => {
  stubFetch(() => jsonResponse({}));
  const res = await call('/api/backend/cela/v1/whoami', {
    env: { CELA_BACKEND_URL: ENV.CELA_BACKEND_URL, CELA_API_TOKEN: PUBLIC_TOKEN, NODE_ENV: 'development' },
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
  const res = await call('https://celia.example/api/backend/%2e%2e/%2e%2e/admin');
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

test('methods outside GET/HEAD/POST are refused with an Allow header', async () => {
  stubFetch(() => jsonResponse({}));
  const res = await call('/api/backend/health', { method: 'DELETE' });
  assert.equal(res.status, 405);
  assert.equal(res.headers.get('allow'), 'GET, HEAD, POST');
  assert.equal(calls.length, 0);
});

test('204 responses keep no body', async () => {
  stubFetch(() => new Response(null, { status: 204 }));
  const res = await call('/api/backend/runs/run_1/approve', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: '{"decision":"approve"}',
  });
  assert.equal(res.status, 204);
  assert.equal(await res.text(), '');
});

// ---- Operator access gate (auth review Finding 1) ---------------------------

const GATE_SECRET = 'gate-secret-789';
const GATED_ENV = { ...ENV, CELA_WEB_ACCESS_SECRET: GATE_SECRET };

async function gateCookie(secret = GATE_SECRET, ttlSeconds) {
  const proof = await gate.issueAccessProof(secret, ttlSeconds);
  return `celia_access=${encodeURIComponent(proof)}`;
}

test('gate: without the operator secret the proxy refuses before any backend or token state', async () => {
  stubFetch(() => jsonResponse({}));
  const denied = await call('/api/backend/health', { env: GATED_ENV });
  assert.equal(denied.status, 401);
  const body = JSON.parse(await denied.text());
  assert.equal(body.code, 'access_required');
  assert.equal(body.retryable, false);
  assert.equal(calls.length, 0);

  // The gate is checked before token configuration: an unauthenticated caller
  // cannot learn whether CELA_API_TOKEN is set by watching status codes.
  const masked = await call('/api/backend/health', {
    env: { CELA_BACKEND_URL: ENV.CELA_BACKEND_URL, CELA_WEB_ACCESS_SECRET: GATE_SECRET },
  });
  assert.equal(masked.status, 401);
  assert.equal(JSON.parse(await masked.text()).code, 'access_required');
});

test('gate: a valid signed cookie or header unlocks the proxy; a wrong one does not', async () => {
  stubFetch(() => jsonResponse({ status: 'ok' }));

  const viaCookie = await call('/api/backend/health', {
    env: GATED_ENV,
    headers: { cookie: await gateCookie() },
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

test('gate: the cookie carries a signed proof, never the secret itself', async () => {
  const cookie = await gateCookie();
  assert.ok(!cookie.includes(GATE_SECRET));
  assert.match(cookie, /celia_access=v1\.\d+\.[0-9a-f]{64}/);
});

test('gate: a stolen proof can be replayed until expiry, and secret rotation revokes it', async () => {
  stubFetch(() => jsonResponse({ status: 'ok' }));
  const stolen = await gateCookie();

  const first = await call('/api/backend/health', { env: GATED_ENV, headers: { cookie: stolen } });
  assert.equal(first.status, 200);
  const replay = await call('/api/backend/health', { env: GATED_ENV, headers: { cookie: stolen } });
  assert.equal(replay.status, 200);
  assert.equal(calls.length, 2);

  // DELETE /api/access only clears one browser. Rotation is the revocation
  // mechanism for every outstanding signed proof.
  const rotated = await call('/api/backend/health', {
    env: { ...ENV, CELA_WEB_ACCESS_SECRET: 'rotated-secret-999' },
    headers: { cookie: stolen },
  });
  assert.equal(rotated.status, 401);
  assert.equal(calls.length, 2);
});

test('gate: expired or tampered proofs are rejected (the expiry is signed)', async () => {
  stubFetch(() => jsonResponse({ status: 'ok' }));

  const expired = await call('/api/backend/health', {
    env: GATED_ENV,
    headers: { cookie: await gateCookie(GATE_SECRET, -10) },
  });
  assert.equal(expired.status, 401);

  const proof = await gate.issueAccessProof(GATE_SECRET);
  const [version, exp, sig] = proof.split('.');
  const tampered = `${version}.${Number(exp) + 86_400}.${sig}`;
  const extended = await call('/api/backend/health', {
    env: GATED_ENV,
    headers: { cookie: `celia_access=${encodeURIComponent(tampered)}` },
  });
  assert.equal(extended.status, 401);

  const forged = `${version}.${exp}.${'0'.repeat(64)}`;
  const forgedRes = await call('/api/backend/health', {
    env: GATED_ENV,
    headers: { cookie: `celia_access=${encodeURIComponent(forged)}` },
  });
  assert.equal(forgedRes.status, 401);
  assert.equal(calls.length, 0);
});

test('gate: production without a secret fails closed with a named 503', async () => {
  stubFetch(() => jsonResponse({}));

  const production = await call('/api/backend/health', {
    env: { CELA_BACKEND_URL: ENV.CELA_BACKEND_URL, CELA_API_TOKEN: PUBLIC_TOKEN, NODE_ENV: 'production' },
  });
  assert.equal(production.status, 503);
  const body = JSON.parse(await production.text());
  assert.equal(body.code, 'access_gate_unconfigured');
  assert.equal(calls.length, 0);

  // Explicit escape hatch: an operator may open the proxy on purpose.
  const opened = await call('/api/backend/health', {
    env: { ...ENV, NODE_ENV: 'production', CELA_WEB_GATE_REQUIRED: '0' },
  });
  assert.equal(opened.status, 200);

  // And an explicit strict flag also fails closed outside NODE_ENV=production.
  const strict = await call('/api/backend/health', {
    env: { ...ENV, CELA_WEB_GATE_REQUIRED: '1' },
  });
  assert.equal(strict.status, 503);
  assert.equal(JSON.parse(await strict.text()).code, 'access_gate_unconfigured');
});

test('gateMode: unknown runtimes fail closed; only recognized local/test runs stay open', () => {
  assert.equal(gate.gateMode(GATE_SECRET, 'production', '0'), 'enforced');
  assert.equal(gate.gateMode('', 'production', undefined), 'unconfigured');
  assert.equal(gate.gateMode('', undefined, undefined), 'unconfigured');
  assert.equal(gate.gateMode('', '', undefined), 'unconfigured');
  assert.equal(gate.gateMode('', 'development', undefined), 'open');
  assert.equal(gate.gateMode('', 'test', undefined), 'open');
  assert.equal(gate.gateMode('', 'production', '0'), 'open');
  assert.equal(gate.gateMode('', 'development', '1'), 'unconfigured');
});

test('gate: without CELA_WEB_ACCESS_SECRET the gate stays open (local single-user mode)', async () => {
  stubFetch(() => jsonResponse({ status: 'ok' }));
  const res = await call('/api/backend/health');
  assert.equal(res.status, 200);
});

test('gate: upstream failure logs never contain the access secret', async () => {
  stubFetch(() => { throw new TypeError('connection refused'); });
  const logged = [];
  const original = console.error;
  console.error = (...args) => { logged.push(args.join(' ')); };
  try {
    const res = await call('/api/backend/health', {
      env: GATED_ENV,
      headers: { 'x-celia-access': GATE_SECRET },
    });
    assert.equal(res.status, 502);
  } finally {
    console.error = original;
  }
  assert.ok(logged.length > 0);
  for (const line of logged) assert.ok(!line.includes(GATE_SECRET), line);
});

test('access endpoint: GET reports required, authorized, configured and mode', async () => {
  setEnv({ NODE_ENV: 'development' });
  let res = await access.GET(new Request('https://celia.example/api/access'));
  assert.deepEqual(await res.json(), {
    required: false,
    authorized: true,
    configured: false,
    mode: 'open',
  });

  // The status endpoint must agree with the proxy's fail-closed behavior:
  // production without a secret is not "not required and authorized".
  setEnv({ NODE_ENV: 'production' });
  res = await access.GET(new Request('https://celia.example/api/access'));
  assert.deepEqual(await res.json(), {
    required: true,
    authorized: false,
    configured: false,
    mode: 'unconfigured',
  });

  setEnv({ CELA_WEB_ACCESS_SECRET: GATE_SECRET });
  res = await access.GET(new Request('https://celia.example/api/access'));
  assert.deepEqual(await res.json(), {
    required: true,
    authorized: false,
    configured: true,
    mode: 'enforced',
  });

  res = await access.GET(new Request('https://celia.example/api/access', {
    headers: { cookie: await gateCookie() },
  }));
  assert.deepEqual(await res.json(), {
    required: true,
    authorized: true,
    configured: true,
    mode: 'enforced',
  });

  res = await access.GET(new Request('https://celia.example/api/access', {
    headers: { cookie: await gateCookie(GATE_SECRET, -5) },
  }));
  assert.deepEqual(await res.json(), {
    required: true,
    authorized: false,
    configured: true,
    mode: 'enforced',
  });
});

test('access endpoint: POST sets the signed HttpOnly cookie only for the right secret', async () => {
  setEnv({ CELA_WEB_ACCESS_SECRET: GATE_SECRET });

  const wrong = await access.POST(new Request('https://celia.example/api/access', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ secret: 'guess' }),
  }));
  assert.equal(wrong.status, 401);
  assert.equal(wrong.headers.get('set-cookie'), null);

  const right = await access.POST(new Request('https://celia.example/api/access', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ secret: GATE_SECRET }),
  }));
  assert.equal(right.status, 200);
  const cookie = right.headers.get('set-cookie');
  assert.match(cookie, /celia_access=v1\.\d+\.[0-9a-f]{64}/);
  assert.ok(!cookie.includes(GATE_SECRET));
  assert.match(cookie, /HttpOnly/);
  assert.match(cookie, /Secure/);
  assert.match(cookie, /SameSite=Lax/);
  assert.match(cookie, /Path=\//);
  assert.match(cookie, /Max-Age=\d+/);

  // The minted cookie is accepted by the gate itself.
  const proof = /celia_access=([^;]+)/.exec(cookie)[1];
  assert.equal(await gate.verifyAccessProof(proof, GATE_SECRET), true);

  const viaHeader = await access.POST(new Request('https://celia.example/api/access', {
    method: 'POST',
    headers: { 'x-celia-access': GATE_SECRET },
  }));
  assert.equal(viaHeader.status, 200);

  setEnv({});
  const unconfigured = await access.POST(new Request('https://celia.example/api/access', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ secret: GATE_SECRET }),
  }));
  assert.equal(unconfigured.status, 409);
});

test('access endpoint: DELETE locks the browser again', async () => {
  setEnv({ CELA_WEB_ACCESS_SECRET: GATE_SECRET });
  const res = await access.DELETE();
  assert.equal(res.status, 200);
  assert.match(res.headers.get('set-cookie'), /Max-Age=0/);
});
