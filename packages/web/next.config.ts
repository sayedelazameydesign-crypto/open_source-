import type { NextConfig } from "next";

// The browser never talks to the backend directly (and never sees a token):
// app/api/backend/* proxies server-side to CELA_BACKEND_URL.
const config: NextConfig = {};
export default config;
