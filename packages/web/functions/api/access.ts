/**
 * Cloudflare Pages Function twin of `app/api/access/route.ts`.
 *
 * Same contract (see `lib/access-gate.ts`): `GET` reports the full gate state
 * (`required`, `authorized`, `configured`, `mode`), `POST` unlocks by setting
 * the `HttpOnly` operator cookie, `DELETE` locks again. Kept in lockstep with
 * the Next.js twin so either deployment shape offers the same operator access
 * gate (authentication review Finding 1).
 */

import {
  ACCESS_HEADER,
  accessCookieHeader,
  accessStatus,
  clearAccessCookieHeader,
  constantTimeEqual,
  normalizeSecret,
  runtimeNodeEnv,
} from "../../lib/access-gate";

export interface CeliaAccessEnv {
  CELA_WEB_ACCESS_SECRET?: string;
  /** `1` = require the secret explicitly; `0` = intentionally open local mode. */
  CELA_WEB_GATE_REQUIRED?: string;
}

interface PagesContext {
  request: Request;
  env: CeliaAccessEnv;
}

function json(body: unknown, status: number, extra: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Cache-Control": "no-store, no-transform",
      "X-Content-Type-Options": "nosniff",
      "X-Celia-BFF": "cloudflare-pages",
      "Content-Type": "application/json; charset=utf-8",
      ...extra,
    },
  });
}

export async function onRequest({ request, env }: PagesContext): Promise<Response> {
  const secret = normalizeSecret(env.CELA_WEB_ACCESS_SECRET);

  if (request.method === "GET" || request.method === "HEAD") {
    return json(
      await accessStatus(request, secret, runtimeNodeEnv(), env.CELA_WEB_GATE_REQUIRED),
      200,
    );
  }

  if (request.method === "POST") {
    if (!secret) return json({ detail: "access gate is not configured" }, 409);
    let supplied = request.headers.get(ACCESS_HEADER)?.trim() ?? "";
    if (!supplied) {
      try {
        const body = (await request.json()) as { secret?: unknown };
        if (typeof body?.secret === "string") supplied = body.secret;
      } catch {
        supplied = "";
      }
    }
    if (!supplied || !constantTimeEqual(supplied, secret)) {
      return json({ detail: "invalid access secret" }, 401);
    }
    // The cookie carries a signed, expiring proof — never the secret itself.
    return json({ ok: true }, 200, { "Set-Cookie": await accessCookieHeader(secret) });
  }

  if (request.method === "DELETE") {
    return json({ ok: true }, 200, { "Set-Cookie": clearAccessCookieHeader() });
  }

  return json({ detail: `method ${request.method} is not supported` }, 405, {
    Allow: "GET, HEAD, POST, DELETE",
  });
}
