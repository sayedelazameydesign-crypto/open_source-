import {
  ACCESS_HEADER,
  accessCookieHeader,
  clearAccessCookieHeader,
  constantTimeEqual,
  hasValidAccess,
  normalizeSecret,
} from "../../../lib/access-gate";

/**
 * Operator unlock for the optional access gate (`CELA_WEB_ACCESS_SECRET`).
 *
 * * `GET`    → `{ required, authorized }` so the UI can show the gate overlay.
 * * `POST`   → body `{ "secret": "…" }` (or an `X-Celia-Access` header); a match
 *   sets the `HttpOnly` cookie that the `/api/backend/*` proxies accept.
 * * `DELETE` → clears the cookie ("lock this browser").
 *
 * Comparison is constant-time and a wrong secret gets the same shape of answer
 * as a right one minus the cookie — the endpoint never echoes any secret.
 */
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function baseHeaders(): Record<string, string> {
  return {
    "Cache-Control": "no-store, no-transform",
    "X-Content-Type-Options": "nosniff",
    "Content-Type": "application/json; charset=utf-8",
  };
}

async function status(secret: string, request: Request): Promise<Response> {
  return new Response(
    JSON.stringify({
      required: Boolean(secret),
      authorized: await hasValidAccess(request, secret),
    }),
    { status: 200, headers: baseHeaders() },
  );
}

export async function GET(request: Request): Promise<Response> {
  return status(normalizeSecret(process.env.CELA_WEB_ACCESS_SECRET), request);
}

export async function POST(request: Request): Promise<Response> {
  const secret = normalizeSecret(process.env.CELA_WEB_ACCESS_SECRET);
  if (!secret) {
    return new Response(
      JSON.stringify({ detail: "access gate is not configured" }),
      { status: 409, headers: baseHeaders() },
    );
  }

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
    return new Response(JSON.stringify({ detail: "invalid access secret" }), {
      status: 401,
      headers: baseHeaders(),
    });
  }

  // The cookie carries a signed, expiring proof — never the secret itself.
  return new Response(JSON.stringify({ ok: true }), {
    status: 200,
    headers: { ...baseHeaders(), "Set-Cookie": await accessCookieHeader(secret) },
  });
}

export async function DELETE(): Promise<Response> {
  return new Response(JSON.stringify({ ok: true }), {
    status: 200,
    headers: { ...baseHeaders(), "Set-Cookie": clearAccessCookieHeader() },
  });
}
