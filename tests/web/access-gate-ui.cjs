/**
 * Behavioural contract for the browser `AccessGate` overlay.
 *
 * The 2026-10-18 review found a mismatch: the proxies fail closed when the
 * gate secret is missing in production, but the UI could fall through to an
 * "open" state when `GET /api/access` itself failed. These tests mount the
 * real component in JSDOM and stub only `fetch`.
 */
const { test, beforeEach, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const React = require('react');
const { createRoot } = require('react-dom/client');
const { AccessGate } = require(path.resolve('.cache/access-gate-ui/components/access-gate.js'));
const { act } = React;

let dom;
let root;
let originals;
let fetchImpl;
let fetchCalls;

const jsonResponse = (data, status = 200) => new Response(JSON.stringify(data), {
  status,
  headers: { 'Content-Type': 'application/json' },
});

const openStatus = {
  required: false,
  authorized: true,
  configured: false,
  mode: 'open',
};

beforeEach(() => {
  originals = Object.fromEntries(
    ['window', 'document', 'fetch', 'IS_REACT_ACT_ENVIRONMENT'].map((key) => [
      key,
      Object.getOwnPropertyDescriptor(globalThis, key),
    ]),
  );
  dom = new JSDOM('<div id="root"></div>', { url: 'https://workspace.test' });
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  fetchCalls = [];
  fetchImpl = async () => jsonResponse(openStatus);
  globalThis.fetch = async (...args) => {
    fetchCalls.push(args);
    return fetchImpl(...args);
  };
  root = createRoot(document.getElementById('root'));
});

afterEach(async () => {
  await act(async () => root.unmount());
  dom.window.close();
  for (const [key, descriptor] of Object.entries(originals)) {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor);
    else delete globalThis[key];
  }
});

async function renderGate() {
  await act(async () => root.render(React.createElement(AccessGate)));
}

function dialog() {
  return document.querySelector('[role="dialog"]');
}

function retryButton() {
  return [...document.querySelectorAll('button')].find((button) =>
    button.textContent.includes('إعادة المحاولة'),
  );
}

test('a failed status request shows an error with retry, never an open workspace', async () => {
  fetchImpl = async () => { throw new TypeError('network down'); };
  await renderGate();

  assert.ok(dialog());
  assert.match(document.body.textContent, /تعذّر التحقق من حالة الوصول/);
  assert.equal(document.querySelector('input[type="password"]'), null);
  assert.ok(retryButton());

  fetchImpl = async () => jsonResponse(openStatus);
  await act(async () => {
    retryButton().click();
    await Promise.resolve();
    await Promise.resolve();
  });
  assert.equal(dialog(), null);
  assert.ok(fetchCalls.length >= 2);
});

test('a malformed status response is an error, not an implicit unlock', async () => {
  fetchImpl = async () => new Response('not-json', { status: 200 });
  await renderGate();

  assert.ok(dialog());
  assert.match(document.body.textContent, /تعذّر التحقق من حالة الوصول/);
  assert.ok(retryButton());
});

test('unconfigured production gate is explicit and offers no useless password prompt', async () => {
  fetchImpl = async () => jsonResponse({
    required: true,
    authorized: false,
    configured: false,
    mode: 'unconfigured',
  });
  await renderGate();

  assert.ok(dialog());
  assert.match(document.body.textContent, /إعداد الوصول غير مكتمل/);
  assert.match(document.body.textContent, /CELA_WEB_ACCESS_SECRET/);
  assert.equal(document.querySelector('input[type="password"]'), null);
  assert.ok(retryButton());
});

test('an enforced, unauthorized gate still shows the operator password prompt', async () => {
  fetchImpl = async () => jsonResponse({
    required: true,
    authorized: false,
    configured: true,
    mode: 'enforced',
  });
  await renderGate();

  assert.ok(dialog());
  assert.match(document.body.textContent, /الوصول محمي/);
  assert.ok(document.querySelector('input[type="password"]'));
});

test('an authorized or explicitly open gate renders no overlay', async () => {
  fetchImpl = async () => jsonResponse(openStatus);
  await renderGate();
  assert.equal(dialog(), null);
});
