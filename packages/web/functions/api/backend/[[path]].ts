/**
 * Cloudflare Pages Function — server-side BFF for `/api/backend/*`.
 *
 * Deployment mode: `packages/web` exported statically to Cloudflare Pages, with
 * this Function as the *only* route to the backend. It is the edge twin of the
 * Next.js route handler (`app/api/backend/[...path]/route.ts`) and keeps exactly
 * the same security contract (ADR-0005):
 *
 * 1. `CELA_API_TOKEN`, `CELA_PRIVATE_API_TOKEN` and `CELA_BACKEND_URL` are read
 *    from Worker secrets and never appear in a response — the browser only ever
 *    sees same-origin `/api/backend/...` URLs.
 * 2. `/api/backend/cela/*` is the private namespace and is signed with the
 *    private token; every other path uses the public token.
 * 3. SSE (`GET /runs/{id}/events?after_seq=N`) is streamed straight through:
 *    the response body is handed to the client untouched, `Cache-Control` is
 *    forced to `no-store, no-transform`, and the query string (including
 *    `after_seq`) is preserved so a reconnect resumes instead of replaying.
 * 4. A sleeping Render free-tier instance answers late or drops the connection.
 *    Both cases become a *retryable* JSON status (`503 backend_starting` /
 *    `502 backend_unreachable`) instead of a raw Cloudflare 502 page, so the
 *    UI can show "backend is waking up" and retry.
 *
 * Deliberate refusals (each covered by `tests/web/deploy-function.cjs`):
 * * `http://` backends outside loopback — bearer tokens must not cross the
 *   network in cleartext;
 * * credentials embedded in `CELA_BACKEND_URL` (`https://user:pass@host`) —
 *   the URL is logged by intermediaries far more often than a header is;
 * * `..` / encoded-dot path segments — the target URL is built by concatenation,
 *   so a traversal would let a caller reach arbitrary backend paths;
 * * redirects off the configured backend origin — a redirect is the classic way
 *   to have a proxy re-attach an `Authorization` header somewhere else.
 *
 * The Cloudflare Pages Functions `onRequest` signature is typed structurally
 * below so this file needs no `@cloudflare/workers-types` dependency.
 */

export interface CeliaPagesEnv {
  CELA_BACKEND_URL?: string;
  CELA_API_TOKEN?: string;
  CELA_PRIVATE_API_TOKEN?: string;
  /** Wall-clock budget for the backend's *response headers*, in milliseconds. */
  CELA_BACKEND_TIMEOUT_MS?: string;
}

interface PagesContext {
  request: Request;
  env: CeliaPagesEnv;
}

const PATH_PREFIX = "/api/backend/";
const PRIVATE_PREFIX = "cela/";
const DEFAULT_TIMEOUT_MS = 25_000;
const MIN_TIMEOUT_MS = 100;
const MAX_TIMEOUT_MS = 60_000;
const MAX_REDIRECTS = 2;
const MAX_PATH_LENGTH = 2_048;
const ALLOWED_METHODS = new Set(["GET", "HEAD", "POST"]);
const REDIRECT_STATUSES = new Set([301, 302, 303, 307, 308]);
const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "::1", "[::1]"]);
const BFF_MARKER = "cloudflare-pages";

function noStore(extra: Record<string, string> = {}): Headers {
  const headers = new Headers({
    "Cache-Control": "no-store, no-transform",
    "X-Content-Type-Options": "nosniff",
    "X-Celia-BFF": BFF_MARKER,
  });
  for (const [key, value] of Object.entries(extra)) headers.set(key, value);
  return headers;
}

function jsonError(
  status: number,
  code: string,
  detail: string,
  extra: Record<string, string> = {},
): Response {
  // The payload is built by hand rather than `Response.json` so the file stays
  // compatible with older TypeScript DOM libs and older Node test runtimes.
  return new Response(JSON.stringify({ detail, code, retryable: status >= 500 }), {
    status,
    headers: noStore({ "Content-Type": "application/json; charset=utf-8", ...extra }),
  });
}

function resolveBackend(raw: string | undefined): { base: string } | { error: string } {
  const candidate = (raw ?? "").trim();
  if (!candidate) return { error: "CELA_BACKEND_URL is not configured" };
  let url: URL;
  try {
    url = new URL(candidate);
  } catch {
    return { error: "CELA_BACKEND_URL is not a valid absolute URL" };
  }
  if (url.protocol !== "https:" && url.protocol !== "http:") {
    return { error: "CELA_BACKEND_URL must use http or https" };
  }
  if (url.username || url.password) {
    return { error: "CELA_BACKEND_URL must not embed credentials" };
  }
  if (url.protocol === "http:" && !LOOPBACK_HOSTS.has(url.hostname)) {
    return {
      error: "CELA_BACKEND_URL must use https outside loopback: tokens must not travel in cleartext",
    };
  }
  const basePath = url.pathname.replace(/\/+$/, "");
  return { base: `${url.origin}${basePath}` };
}

/** Return the backend-relative path, or `null` when the request must be refused. */
function backendPath(request: Request): string | null {
  const { pathname } = new URL(request.url);
  if (!pathname.startsWith(PATH_PREFIX)) return null;
  const joined = pathname.slice(PATH_PREFIX.length);
  if (!joined || joined.length > MAX_PATH_LENGTH) return null;
  const segments = joined.split("/");
  // A single trailing slash is a legitimate request (`/runs/`): FastAPI answers
  // it with a same-origin 307 that this proxy follows. An empty segment in the
  // middle (`/runs//events`) is not.
  if (segments.length > 1 && segments[segments.length - 1] === "") segments.pop();
  for (const rawSegment of segments) {
    if (!rawSegment) return null;
    let segment: string;
    try {
      segment = decodeURIComponent(rawSegment);
    } catch {
      return null;
    }
    // `.` / `..` survive URL normalization only when percent-encoded, and an
    // encoded slash or backslash would smuggle a second segment past the split.
    if (segment === "." || segment === "..") return null;
    if (segment.includes("/") || segment.includes("\\") || segment.includes("\0")) return null;
  }
  return joined;
}

function timeoutFromEnv(raw: string | undefined): number {
  if (!raw) return DEFAULT_TIMEOUT_MS;
  const parsed = Number.parseInt(raw, 10);
  if (!Number.isFinite(parsed)) return DEFAULT_TIMEOUT_MS;
  return Math.min(MAX_TIMEOUT_MS, Math.max(MIN_TIMEOUT_MS, parsed));
}

function upstreamHeaders(request: Request, token: string): Headers {
  // An explicit allow-list: cookies, `cf-*` and client `Authorization` headers
  // must never reach the backend, and the browser's token (if any) never wins.
  const headers = new Headers({ Authorization: `Bearer ${token}`, "X-Celia-BFF": BFF_MARKER });
  const accept = request.headers.get("accept");
  if (accept) headers.set("Accept", accept);
  const contentType = request.headers.get("content-type");
  if (contentType) headers.set("Content-Type", contentType);
  const idempotencyKey = request.headers.get("x-celia-idempotency-key");
  if (idempotencyKey) headers.set("X-Celia-Idempotency-Key", idempotencyKey);
  return headers;
}

function hasBody(request: Request): boolean {
  return request.method !== "GET" && request.method !== "HEAD" && request.body !== null;
}

/** Strip a body where HTTP forbids one (Workers rejects 204/304 bodies). */
function bodiless(status: number): boolean {
  return status === 204 || status === 205 || status === 304;
}

function passthrough(response: Response): Response {
  const headers = noStore({
    "Content-Type": response.headers.get("content-type") ?? "application/json",
  });
  const contentDisposition = response.headers.get("content-disposition");
  if (contentDisposition) headers.set("Content-Disposition", contentDisposition);
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.toLowerCase().startsWith("text/event-stream")) {
    // Belt and braces for proxies that buffer unless told not to (nginx/Cloudflare).
    headers.set("X-Accel-Buffering", "no");
  }
  return new Response(bodiless(response.status) ? null : response.body, {
    status: response.status,
    headers,
  });
}

export async function onRequest({ request, env }: PagesContext): Promise<Response> {
  if (!ALLOWED_METHODS.has(request.method)) {
    return jsonError(405, "method_not_allowed", `method ${request.method} is not proxied`, {
      Allow: "GET, HEAD, POST",
    });
  }

  const path = backendPath(request);
  if (path === null) {
    return jsonError(400, "invalid_path", "request path is not a valid /api/backend/* path");
  }

  const backend = resolveBackend(env.CELA_BACKEND_URL);
  if ("error" in backend) {
    return jsonError(503, "backend_not_configured", backend.error);
  }

  const isPrivate = path.startsWith(PRIVATE_PREFIX);
  const tokenVariable = isPrivate ? "CELA_PRIVATE_API_TOKEN" : "CELA_API_TOKEN";
  const token = (isPrivate ? env.CELA_PRIVATE_API_TOKEN : env.CELA_API_TOKEN)?.trim() ?? "";
  if (!token) {
    return jsonError(503, "token_not_configured", `server is missing ${tokenVariable}`);
  }

  const backendOrigin = new URL(backend.base).origin;
  const search = new URL(request.url).search;
  const deadline = Date.now() + timeoutFromEnv(env.CELA_BACKEND_TIMEOUT_MS);
  let target = `${backend.base}/${path}${search}`;
  let response: Response | null = null;

  try {
    for (let hop = 0; ; hop += 1) {
      const remaining = Math.max(1, deadline - Date.now());
      response = await fetch(target, {
        method: request.method,
        headers: upstreamHeaders(request, token),
        body: hasBody(request) ? request.body : undefined,
        redirect: "manual",
        signal: AbortSignal.timeout(remaining),
      });

      const location = response.headers.get("location");
      if (!REDIRECT_STATUSES.has(response.status) || !location) break;
      const next = new URL(location, target);
      const replayable = request.method === "GET" || request.method === "HEAD";
      if (hop >= MAX_REDIRECTS || !replayable || next.origin !== backendOrigin) {
        return jsonError(502, "backend_redirect_refused", "backend answered with a redirect that is not proxied");
      }
      target = next.toString();
    }
  } catch (error) {
    const name = error instanceof Error ? error.name : "UnknownError";
    console.error(`celia-bff: backend request failed (${name})`);
    if (name === "TimeoutError" || name === "AbortError") {
      return jsonError(
        503,
        "backend_starting",
        "backend did not answer in time; free-tier instances wake slowly, retry shortly",
        { "Retry-After": "5" },
      );
    }
    return jsonError(502, "backend_unreachable", "backend is unreachable; retry shortly", {
      "Retry-After": "5",
    });
  }

  if (response === null) {
    return jsonError(502, "backend_unreachable", "backend is unreachable; retry shortly");
  }
  return passthrough(response);
}
