const { execFileSync } = require('node:child_process');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const modules = path.join(root, 'packages/web/node_modules');
const outDir = process.env.PLATFORM_CONSOLE_OUTPUT || '.cache/platform-console';
execFileSync(process.execPath, [path.join(modules, 'typescript/bin/tsc'),
  '--outDir', outDir, '--rootDir', 'packages/web', '--module', 'commonjs',
  '--moduleResolution', 'node', '--jsx', 'react-jsx', '--target', 'es2022', '--esModuleInterop',
  '--skipLibCheck', 'packages/web/lib/platform.ts',
  'packages/web/components/platform/console.tsx',
], { cwd: root, stdio: 'inherit' });
execFileSync(process.execPath, ['--test', 'tests/web/platform-console.cjs'], {
  cwd: root, stdio: 'inherit', env: { ...process.env, NODE_PATH: modules },
});
