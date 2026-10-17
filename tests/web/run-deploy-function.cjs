const { execFileSync } = require('node:child_process');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const modules = path.join(root, 'packages/web/node_modules');
// `functions/` is not part of the Next.js typecheck graph on Cloudflare, so the
// proxy is compiled here with the same strictness CI uses before it is exercised.
execFileSync(process.execPath, [path.join(modules, 'typescript/bin/tsc'),
  '--outDir', '.cache/deploy-function', '--rootDir', 'packages/web',
  '--module', 'commonjs', '--moduleResolution', 'node', '--target', 'es2022',
  '--lib', 'es2022,dom,dom.iterable', '--strict', '--skipLibCheck',
  'packages/web/functions/api/backend/[[path]].ts',
], { cwd: root, stdio: 'inherit' });
execFileSync(process.execPath, ['--test', 'tests/web/deploy-function.cjs'], {
  cwd: root, stdio: 'inherit',
});
