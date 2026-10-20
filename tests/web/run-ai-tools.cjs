const { execFileSync } = require("node:child_process");
const path = require("node:path");
const root = path.resolve(__dirname, "../..");
const modules = path.join(root, "packages/web/node_modules");
execFileSync(
  process.execPath,
  [
    path.join(modules, "typescript/bin/tsc"),
    "--outDir",
    ".cache/ui1-ai-tools",
    "--rootDir",
    "packages/web",
    "--module",
    "commonjs",
    "--moduleResolution",
    "node",
    "--target",
    "es2022",
    "--esModuleInterop",
    "--skipLibCheck",
    "packages/web/lib/ai-tools.ts",
  ],
  { cwd: root, stdio: "inherit" },
);
execFileSync(process.execPath, ["--test", "tests/web/ai-tools.cjs"], {
  cwd: root,
  stdio: "inherit",
  env: { ...process.env, NODE_PATH: modules },
});
