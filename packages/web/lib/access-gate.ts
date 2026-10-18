/**
 * Optional operator access gate for the `/api/backend/*` BFF twins.
 *
 * The authentication architecture review of 2026-10-18 (Finding 1) noted that
 * the proxies authenticate the *server* to the backend but never the *caller*:
 * anyone who can reach the public proxy rides on the server's bearer tokens,
 * including the private `/cela/v1` namespace. This module is the agreed fix for
 * the single-operator ("جلسة محلية · مستخدم واحد") product shape: when
 * `CELA_WEB_ACCESS_SECRET` is configured, every proxied request must present
 * the operator secret — as an `HttpOnly` cookie (set via `POST /api/access`)
 * or as an `X-Celia-Access` header (for curl/automation).
 *
 * When the secret is *not* configured the gate is disabled and behavior is
 * unchanged, so local development keeps working without setup.
 *
 * The cookie carries the secret itself: `HttpOnly` keeps it out of page JS,
 * `Secure` confines it to TLS, `SameSite=Lax` blocks cross-site form posts,
 * and rotation of `CELA_WEB_ACCESS_SECRET` invalidates every cookie at once.
 * No session store, no cookies readable by scripts, and the identical code
 * runs on Node (Next.js route) and Workers (Cloudflare Pages Function).
 */

export const ACCESS_SECRET_ENV = "CELA_WEB_ACCESS_SECRET";
export const ACCESS_COOKIE = "celia_access";
export const ACCESS_HEADER = "x-celia-access";
/** 30 days — the operator re-enters the secret after a month or on rotation. */
export const ACCESS_MAX_AGE = 2_592_000;

export function normalizeSecret(raw: string | undefined): string {
  return (raw ?? "").trim();
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

/** The proof a caller presents: explicit header wins, then the HttpOnly cookie. */
export function requestAccessProof(request: Request): string | null {
  const header = request.headers.get(ACCESS_HEADER);
  if (header && header.trim()) return header.trim();
  return cookieValue(request, ACCESS_COOKIE);
}

/** Empty secret ⇒ gate disabled ⇒ everyone passes (local, single-user mode). */
export function hasValidAccess(request: Request, secret: string): boolean {
  if (!secret) return true;
  const proof = requestAccessProof(request);
  return proof !== null && constantTimeEqual(proof, secret);
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

export function accessCookieHeader(secret: string): string {
  return `${ACCESS_COOKIE}=${encodeURIComponent(secret)}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=${ACCESS_MAX_AGE}`;
}

export function clearAccessCookieHeader(): string {
  return `${ACCESS_COOKIE}=; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=0`;
}
