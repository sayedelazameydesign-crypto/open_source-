const { execFileSync } = require('node:child_process');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const modules = path.join(root, 'packages/web/node_modules');
// The Next.js BFF twin is compiled here with the same strictness CI uses before
// it is exercised, exactly like `run-deploy-function.cjs` does for the Pages
// Function — so both proxies of the security contract (ADR-0005) are checked
// against the same hostile request suite.
execFileSync(process.execPath, [path.join(modules, 'typescript/bin/tsc'),
  '--outDir', '.cache/nextjs-proxy', '--rootDir', 'packages/web',
  '--module', 'commonjs', '--moduleResolution', 'node', '--target', 'es2022',
  '--lib', 'es2022,dom,dom.iterable', '--strict', '--skipLibCheck',
  'packages/web/app/api/backend/[...path]/route.ts',
  'packages/web/app/api/access/route.ts',
], { cwd: root, stdio: 'inherit' });
execFileSync(process.execPath, ['--test', 'tests/web/nextjs-proxy.cjs'], {
  cwd: root, stdio: 'inherit',
});
