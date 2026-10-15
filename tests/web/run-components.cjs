const { execFileSync } = require('node:child_process');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const modules = path.join(root, 'packages/web/node_modules');
execFileSync(process.execPath, [path.join(modules, 'typescript/bin/tsc'),
  '--outDir', '.cache/ui1-components', '--rootDir', 'packages/web', '--module', 'commonjs',
  '--moduleResolution', 'node', '--jsx', 'react-jsx', '--target', 'es2022', '--esModuleInterop',
  '--skipLibCheck', 'packages/web/components/workspace/panels.tsx', 'packages/web/lib/contracts.ts',
], { cwd: root, stdio: 'inherit' });
execFileSync(process.execPath, ['--test', 'tests/web/components.cjs'], {
  cwd: root, stdio: 'inherit', env: { ...process.env, NODE_PATH: modules },
});
