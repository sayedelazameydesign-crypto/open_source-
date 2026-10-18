/**
 * Optional operator access gate for the `/api/backend/*` BFF twins.
 *
 * The authentication architecture review of 2026-10-18 (Finding 1) noted that
 * the proxies authenticate the *server* to the backend but never the *caller*:
 * anyone who can reach the public proxy rides on the server's bearer tokens,
 * including the private `/cela/v1` namespace. This module is the agreed fix for
 * the single-operator ("جلسة محلية · مستخدم واحد") product shape.
 *
 * Gate modes (decided from configuration at request time):
 *
 * * **enforced** — `CELA_WEB_ACCESS_SECRET` is set: every proxied request must
 *   present a valid operator proof (an `HttpOnly` cookie set via
 *   `POST /api/access`, or an `X-Celia-Access` header carrying the secret for
 *   curl/automation).
 * * **unconfigured** — the secret is missing and the deployment is production
 *   or unknown (`NODE_ENV=production`, no recognizable `NODE_ENV`, or
 *   `CELA_WEB_GATE_REQUIRED=1`). Unknown is treated as production so runtimes
 *   without `NODE_ENV` (for example Workers) fail closed too. The proxies then
 *   refuse with `503 access_gate_unconfigured` instead of silently serving an
 *   open proxy (2026-10-18 review, second condition).
 * * **open** — the secret is missing and the run is explicitly local/test
 *   (`NODE_ENV=development|test`, or `CELA_WEB_GATE_REQUIRED=0`): behavior
 *   unchanged, no gate.
 *
 * Cookie design (2026-10-18 review, first condition): the cookie never carries
 * the secret itself. It carries `v1.<expires-at>.<hex HMAC-SHA256>` — a signed,
 * expiring proof derived from the secret (`crypto.subtle`, identical on Node
 * and Workers). A stolen cookie is still bearer-equivalent *until its embedded
 * expiry*, but it never reveals the secret, cannot be extended (the expiry is
 * covered by the signature), and rotating `CELA_WEB_ACCESS_SECRET` invalidates
 * every outstanding proof at once. `DELETE /api/access` only clears the local
 * browser cookie — suspected theft ⇒ rotate the secret.
 *
 * The `X-Celia-Access` automation header still carries the raw secret; it is
 * compared in constant time and must never be logged (the proxies only log an
 * error *name* on upstream failures — never headers).
 */

export const ACCESS_SECRET_ENV = "CELA_WEB_ACCESS_SECRET";
export const ACCESS_GATE_REQUIRED_ENV = "CELA_WEB_GATE_REQUIRED";
export const ACCESS_COOKIE = "celia_access";
export const ACCESS_HEADER = "x-celia-access";
/** 30 days — the operator re-enters the secret after a month or on rotation. */
export const ACCESS_MAX_AGE = 2_592_000;
const PROOF_VERSION = "v1";

export type GateMode = "enforced" | "unconfigured" | "open";

export function normalizeSecret(raw: string | undefined): string {
  return (raw ?? "").trim();
}

/** `NODE_ENV` via a structural lookup so the same code runs on Node and Workers. */
export function runtimeNodeEnv(): string | undefined {
  const proc = (globalThis as { process?: { env?: Record<string, string | undefined> } }).process;
  return proc?.env?.NODE_ENV;
}

export function gateMode(
  secret: string,
  nodeEnv: string | undefined,
  gateRequired: string | undefined,
): GateMode {
  if (secret) return "enforced";
  const explicit = (gateRequired ?? "").trim();
  if (explicit === "1") return "unconfigured";
  if (explicit === "0") return "open";
  const normalizedNodeEnv = (nodeEnv ?? "").trim();
  if (normalizedNodeEnv === "development" || normalizedNodeEnv === "test") return "open";
  // Production — and any environment we cannot positively identify as
  // local/test, including Workers without NODE_ENV — fails closed.
  return "unconfigured";
}

/** Constant-time comparison of two UTF-8 strings (portable: Node + Workers). */
export function constantTimeEqual(a: string, b: string): boolean {
  const left = new TextEncoder().encode(a);
  const right = new TextEncoder().encode(b);
  let diff = left.length ^ right.length;
  const shared = Math.min(left.length, right.length);
  for (let i = 0; i < shared; i += 1) diff |= left[i] ^ right[i];
  return diff === 0;
}

export function cookieValue(request: Request, name: string): string | null {
  const header = request.headers.get("cookie");
  if (!header) return null;
  for (const part of header.split(";")) {
    const eq = part.indexOf("=");
    if (eq === -1) continue;
    if (part.slice(0, eq).trim() !== name) continue;
    try {
      return decodeURIComponent(part.slice(eq + 1).trim());
    } catch {
      return part.slice(eq + 1).trim();
    }
  }
  return null;
}

/** The proof a caller presents: explicit header (raw secret), then the cookie. */
export function requestAccessProof(request: Request): string | null {
  const header = request.headers.get(ACCESS_HEADER);
  if (header && header.trim()) return header.trim();
  return cookieValue(request, ACCESS_COOKIE);
}

async function hmacHex(secret: string, message: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(message));
  return Array.from(new Uint8Array(signature))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

/**
 * Mint a signed, expiring proof: `v1.<expires-at-epoch-seconds>.<hex hmac>`.
 * `ttlSeconds` is parameterized so tests can mint already-expired proofs.
 */
export async function issueAccessProof(
  secret: string,
  ttlSeconds: number = ACCESS_MAX_AGE,
): Promise<string> {
  const expiresAt = Math.floor(Date.now() / 1000) + ttlSeconds;
  const payload = `${PROOF_VERSION}.${expiresAt}`;
  return `${payload}.${await hmacHex(secret, payload)}`;
}

/** Verify a cookie proof: version, embedded expiry, then HMAC in constant time. */
export async function verifyAccessProof(
  proof: string | null,
  secret: string,
  nowSeconds: number = Math.floor(Date.now() / 1000),
): Promise<boolean> {
  if (!proof || !secret) return false;
  const parts = proof.split(".");
  if (parts.length !== 3) return false;
  const [version, expiresRaw, signature] = parts;
  if (version !== PROOF_VERSION) return false;
  const expiresAt = Number.parseInt(expiresRaw, 10);
  if (!Number.isFinite(expiresAt) || expiresAt <= nowSeconds) return false;
  return constantTimeEqual(signature, await hmacHex(secret, `${version}.${expiresRaw}`));
}

/**
 * Gate decision for one request. The `X-Celia-Access` header accepts the raw
 * secret (automation); the cookie accepts only a signed proof.
 */
export async function hasValidAccess(request: Request, secret: string): Promise<boolean> {
  if (!secret) return true; // caller must check `gateMode` first
  const header = request.headers.get(ACCESS_HEADER);
  if (header && header.trim() && constantTimeEqual(header.trim(), secret)) return true;
  return verifyAccessProof(cookieValue(request, ACCESS_COOKIE), secret);
}

/** Uniform refusal: reveals nothing about backend or token configuration. */
export function accessDeniedResponse(bffMarker: string): Response {
  return new Response(
    JSON.stringify({
      detail: "access requires the operator secret",
      code: "access_required",
      retryable: false,
    }),
    {
      status: 401,
      headers: {
        "Cache-Control": "no-store, no-transform",
        "X-Content-Type-Options": "nosniff",
        "X-Celia-BFF": bffMarker,
        "Content-Type": "application/json; charset=utf-8",
        "WWW-Authenticate": "Cookie realm=cela",
      },
    },
  );
}

/** Refusal used in production when the gate itself is misconfigured. */
export function accessGateUnconfiguredResponse(bffMarker: string): Response {
  return new Response(
    JSON.stringify({
      detail:
        "CELA_WEB_ACCESS_SECRET is not configured; refusing to proxy in production " +
        "(set the secret, or CELA_WEB_GATE_REQUIRED=0 to explicitly open the proxy)",
      code: "access_gate_unconfigured",
      retryable: false,
    }),
    {
      status: 503,
      headers: {
        "Cache-Control": "no-store, no-transform",
        "X-Content-Type-Options": "nosniff",
        "X-Celia-BFF": bffMarker,
        "Content-Type": "application/json; charset=utf-8",
      },
    },
  );
}

export async function accessCookieHeader(secret: string): Promise<string> {
  const proof = await issueAccessProof(secret);
  return `${ACCESS_COOKIE}=${encodeURIComponent(proof)}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=${ACCESS_MAX_AGE}`;
}

export function clearAccessCookieHeader(): string {
  return `${ACCESS_COOKIE}=; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=0`;
}
