import type { NextConfig } from "next";

// Two deployment shapes share one app:
//
// * Node/Vercel/preview (default) — `app/api/backend/[...path]/route.ts` is the
//   server-side BFF that injects the tokens and streams SSE.
// * Cloudflare Pages (`CELA_STATIC_EXPORT=1 npm run build:pages`) — the site is
//   exported to `out/` and `functions/api/backend/[[path]].ts` takes over as the
//   edge BFF. Static export cannot contain a dynamic Route Handler, and the
//   handler must not be stubbed away in the default build either, so the export
//   narrows `pageExtensions` to the extensions actually used by pages/layouts.
//   That makes `route.ts` invisible to the router for this build only.
const staticExport = process.env.CELA_STATIC_EXPORT === "1";

const config: NextConfig = staticExport
  ? {
      output: "export",
      pageExtensions: ["tsx", "jsx"],
      // The edge BFF already serves exactly the endpoints the UI calls; no
      // Next image optimizer (and therefore no sharp) is part of that surface.
      images: { unoptimized: true },
    }
  : {};

export default config;
