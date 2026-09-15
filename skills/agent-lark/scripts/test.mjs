// Test runner: compile src/ + tests/ with tsc, then hand every compiled test
// file to `node --test` as an explicit path. Two things a bare
// `tsc && node --test "<glob>"` script gets wrong:
//   - a glob that matches nothing is reported by `node --test` as "0 tests,
//     pass" (exit 0), so a broken outDir / include / file suffix would turn
//     the whole test step into a green no-op;
//   - tsc never deletes stale files from outDir, so a renamed or removed test
//     would keep running from its old compiled copy.
// Explicit file paths also keep the argument list free of shell glob rules,
// which differ between sh and cmd.exe.
import { spawnSync } from 'node:child_process';
import { readdirSync, rmSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const outDir = join(root, '.test-out');
const testsDir = join(outDir, 'tests');

rmSync(outDir, { recursive: true, force: true });

// No test may reach Feishu, whatever it spawns: the CLI refuses both of
// setup's network paths (QR registration, credential probe) under this.
process.env.AGENT_LARK_OFFLINE = '1';

// Run tsc through the current Node binary rather than `npx`/`tsc` so no shell
// lookup (or `.cmd` shim on Windows) is involved.
const tsc = createRequire(import.meta.url).resolve('typescript/bin/tsc');
const compiled = spawnSync(process.execPath, [tsc, '-p', join(root, 'tsconfig.test.json')], {
  cwd: root,
  stdio: 'inherit',
});
if (compiled.error) console.error(compiled.error);
if (compiled.status !== 0) process.exit(compiled.status ?? 1);

// The CLI tests spawn the shipped bundle, so it is rebuilt from the sources
// just compiled: a stale dist/cli.mjs would make them pass against old code.
const built = spawnSync(process.execPath, [join(root, 'scripts', 'build.mjs')], { cwd: root, stdio: 'inherit' });
if (built.error) console.error(built.error);
if (built.status !== 0) process.exit(built.status ?? 1);

let files;
try {
  files = readdirSync(testsDir, { recursive: true })
    .map(String)
    .filter((rel) => rel.endsWith('.test.js'))
    .sort()
    .map((rel) => join(testsDir, rel));
} catch (err) {
  // tsc emitted nothing under tests/ — same outcome as an empty list. Any
  // other failure to read the directory is a real error.
  if (err?.code !== 'ENOENT') throw err;
  files = [];
}
if (files.length === 0) {
  process.stderr.write(
    `scripts/test.mjs: no test files under ${testsDir} — tsc compiled nothing from tests/**/*.test.ts; check the include list in tsconfig.test.json and the test file names\n`,
  );
  process.exit(1);
}

// A test that never settles must not hang the run, and a daemon or timer a
// failed test leaves behind must not keep the process alive after the last
// result: cap each test, and exit once all are done. Anything after
// `npm test --` (e.g. --test-name-pattern=x, or another --test-timeout) goes
// to node --test after these, so it wins.
//
// --test-force-exit only from Node 23 on. On Node 22 it makes the run drop
// the last tests of a file from both the report and the totals — measured
// on the same tree: 226 tests with the flag counted as 226, 218, 224, 220
// over four runs, always "fail 0", and 226 four times without it — so a red
// test there could go unseen. Without the flag a handle a test leaves
// behind keeps the process alive instead, which is loud rather than silent;
// every test that starts a daemon stops it itself.
const major = Number(process.versions.node.split('.')[0]);
const forceExit = major >= 23 ? ['--test-force-exit'] : [];
const run = spawnSync(process.execPath, ['--test', '--test-timeout=30000', ...forceExit, ...process.argv.slice(2), ...files], {
  cwd: root,
  stdio: 'inherit',
});
if (run.error) console.error(run.error);
process.exit(run.status ?? 1);
