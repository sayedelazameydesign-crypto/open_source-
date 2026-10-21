const { execFileSync } = require("node:child_process");
const path = require("node:path");

const root = path.resolve(__dirname, "../..");
const modules = path.join(root, "packages/web/node_modules");
const outDir = ".cache/page-links";

execFileSync(
  process.execPath,
  [
    path.join(modules, "typescript/bin/tsc"),
    "--outDir",
    outDir,
    "--rootDir",
    "packages/web",
    "--module",
    "commonjs",
    "--moduleResolution",
    "node",
    "--target",
    "es2022",
    "--jsx",
    "react-jsx",
    "--esModuleInterop",
    "--skipLibCheck",
    "packages/web/lib/page-links.ts",
  ],
  { cwd: root, stdio: "inherit" },
);

execFileSync(process.execPath, ["--test", "tests/web/page-links.cjs"], {
  cwd: root,
  stdio: "inherit",
  env: {
    ...process.env,
    NODE_PATH: modules,
    PAGE_LINKS_OUTPUT: path.join(root, outDir),
  },
});
