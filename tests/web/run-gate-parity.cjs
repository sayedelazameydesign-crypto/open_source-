const { execFileSync } = require('node:child_process');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const modules = path.join(root, 'packages/web/node_modules');
// Compile BOTH BFF twins and their shared access-gate module into one tree so
// `gate-parity.cjs` can feed identical requests through each and prove the two
// proxies make identical decisions (2026-10-18 review: twin lockstep).
execFileSync(process.execPath, [path.join(modules, 'typescript/bin/tsc'),
  '--outDir', '.cache/gate-parity', '--rootDir', 'packages/web',
  '--module', 'commonjs', '--moduleResolution', 'node', '--target', 'es2022',
  '--lib', 'es2022,dom,dom.iterable', '--strict', '--skipLibCheck',
  'packages/web/app/api/backend/[...path]/route.ts',
  'packages/web/functions/api/backend/[[path]].ts',
], { cwd: root, stdio: 'inherit' });
execFileSync(process.execPath, ['--test', 'tests/web/gate-parity.cjs'], {
  cwd: root, stdio: 'inherit',
});
