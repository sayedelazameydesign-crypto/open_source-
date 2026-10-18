/**
 * Twin-parity contract: the Next.js BFF (`app/api/backend/[...path]/route.ts`)
 * and the Cloudflare Pages BFF (`functions/api/backend/[[path]].ts`) must make
 * identical gate decisions for identical inputs — same status, same error code,
 * same upstream URL and Authorization header. The 2026-10-18 review asked for
 * this explicitly: both proxies are one security contract, so neither may drift.
 *
 * Compiled by `tests/web/run-gate-parity.cjs` into one tree; `fetch` is stubbed.
 */
const { test, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const tree = path.join(__dirname, '../../.cache/gate-parity');
const nextProxy = require(path.join(tree, 'app/api/backend/[...path]/route.js'));
const cfProxy = require(path.join(tree, 'functions/api/backend/[[path]].js'));
const gate = require(path.join(tree, 'lib/access-gate.js'));

const realFetch = globalThis.fetch;
const PUBLIC_TOKEN = 'pub-token-123';
const PRIVATE_TOKEN = 'priv-token-456';
const GATE_SECRET = 'gate-secret-789';
const BASE_ENV = {
  CELA_BACKEND_URL: 'https://celia-api.onrender.com',
  CELA_API_TOKEN: PUBLIC_TOKEN,
  CELA_PRIVATE_API_TOKEN: PRIVATE_TOKEN,
  // Local/open only when the test explicitly says so; secret-bearing cases
  // still enter enforced mode because a configured secret wins.
  NODE_ENV: 'development',
  CELA_WEB_GATE_REQUIRED: '0',
};
const ENV_KEYS = ['CELA_BACKEND_URL', 'CELA_API_TOKEN', 'CELA_PRIVATE_API_TOKEN', 'CELA_WEB_ACCESS_SECRET', 'CELA_WEB_GATE_REQUIRED', 'NODE_ENV'];

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

function request(pathname, { method = 'GET', headers, body } = {}) {
  return new Request(`https://celia.example${pathname}`, { method, headers, body });
}

async function runNext(req, env) {
  setEnv(env);
  calls = [];
  const res = await nextProxy.GET(req, { params: Promise.resolve({ path: [] }) });
  return {
    status: res.status,
    body: JSON.parse(await res.text()),
    upstream: calls[0] ? { url: calls[0].url, auth: calls[0].init.headers.get('authorization') } : null,
  };
}

async function runCf(req, env) {
  // Mirror NODE_ENV into the Node test process for parity with the Next twin;
  // the Function itself receives its configuration through `env`.
  setEnv(env);
  calls = [];
  const res = await cfProxy.onRequest({ request: req, env });
  return {
    status: res.status,
    body: JSON.parse(await res.text()),
    upstream: calls[0] ? { url: calls[0].url, auth: calls[0].init.headers.get('authorization') } : null,
  };
}

async function assertParity(req, env) {
  const viaNext = await runNext(req, env);
  const viaCf = await runCf(req, env);
  assert.deepEqual(viaCf, viaNext);
  return viaNext;
}

async function cookieHeader(secret = GATE_SECRET, ttlSeconds) {
  const proof = await gate.issueAccessProof(secret, ttlSeconds);
  return { cookie: `celia_access=${encodeURIComponent(proof)}` };
}

test('parity: missing proof is 401 access_required on both twins', async () => {
  stubFetch(() => new Response('{}', { status: 200 }));
  const result = await assertParity(request('/api/backend/health'), { ...BASE_ENV, CELA_WEB_ACCESS_SECRET: GATE_SECRET });
  assert.equal(result.status, 401);
  assert.equal(result.body.code, 'access_required');
  assert.equal(result.upstream, null);
});

test('parity: wrong, expired and forged proofs all fail identically', async () => {
  stubFetch(() => new Response('{}', { status: 200 }));
  for (const headers of [
    { cookie: 'celia_access=nope' },
    await cookieHeader(GATE_SECRET, -10),
    { 'x-celia-access': 'wrong-secret' },
  ]) {
    const result = await assertParity(request('/api/backend/health', { headers }), {
      ...BASE_ENV,
      CELA_WEB_ACCESS_SECRET: GATE_SECRET,
    });
    assert.equal(result.status, 401, JSON.stringify(headers));
  }
});

test('parity: a valid cookie and a valid header forward identically', async () => {
  stubFetch(() => new Response('{"status":"ok"}', { status: 200, headers: { 'content-type': 'application/json' } }));
  const viaCookie = await assertParity(request('/api/backend/health', { headers: await cookieHeader() }), {
    ...BASE_ENV,
    CELA_WEB_ACCESS_SECRET: GATE_SECRET,
  });
  assert.equal(viaCookie.status, 200);
  assert.equal(viaCookie.upstream.url, 'https://celia-api.onrender.com/health');
  assert.equal(viaCookie.upstream.auth, `Bearer ${PUBLIC_TOKEN}`);

  const viaHeader = await assertParity(request('/api/backend/cela/v1/whoami', { headers: { 'x-celia-access': GATE_SECRET } }), {
    ...BASE_ENV,
    CELA_WEB_ACCESS_SECRET: GATE_SECRET,
  });
  assert.equal(viaHeader.status, 200);
  assert.equal(viaHeader.upstream.auth, `Bearer ${PRIVATE_TOKEN}`);
});

test('parity: production without a secret fails closed identically', async () => {
  stubFetch(() => new Response('{}', { status: 200 }));
  const result = await assertParity(request('/api/backend/health'), {
    ...BASE_ENV,
    NODE_ENV: 'production',
    CELA_WEB_GATE_REQUIRED: '1',
  });
  assert.equal(result.status, 503);
  assert.equal(result.body.code, 'access_gate_unconfigured');
  assert.equal(result.upstream, null);
});

test('parity: open local mode passes identically', async () => {
  stubFetch(() => new Response('{"status":"ok"}', { status: 200, headers: { 'content-type': 'application/json' } }));
  const result = await assertParity(request('/api/backend/health'), BASE_ENV);
  assert.equal(result.status, 200);
  assert.equal(result.upstream.url, 'https://celia-api.onrender.com/health');
});
