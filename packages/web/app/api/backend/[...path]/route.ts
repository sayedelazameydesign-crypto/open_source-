import type { NextRequest } from "next/server";

// Server-side proxy: tokens live only in this process's environment
// (CELA_API_TOKEN / CELA_PRIVATE_API_TOKEN) and are never shipped to the
// browser. SSE responses are streamed straight through, so `after_seq`
// resume semantics of the backend are preserved end to end.
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const BACKEND = process.env.CELA_BACKEND_URL ?? "http://127.0.0.1:8080";

async function proxy(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const { path } = await ctx.params;
  const joined = path.join("/");
  const isPrivate = joined.startsWith("cela/");
  const token = isPrivate ? process.env.CELA_PRIVATE_API_TOKEN : process.env.CELA_API_TOKEN;
  if (!token) {
    return Response.json(
      { detail: `server is missing ${isPrivate ? "CELA_PRIVATE_API_TOKEN" : "CELA_API_TOKEN"}` },
      { status: 503 },
    );
  }
  const url = `${BACKEND}/${joined}${req.nextUrl.search}`;
  const res = await fetch(url, {
    method: req.method,
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: req.method === "GET" || req.method === "HEAD" ? undefined : await req.text(),
    // @ts-expect-error Node fetch streaming for SSE passthrough
    duplex: "half",
  });
  return new Response(res.body, {
    status: res.status,
    headers: {
      "Content-Type": res.headers.get("content-type") ?? "application/json",
      "Cache-Control": "no-cache, no-transform",
    },
  });
}

export { proxy as GET, proxy as POST };
