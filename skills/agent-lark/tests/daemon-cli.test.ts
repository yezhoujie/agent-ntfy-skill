// The shipped CLI against a daemon process that runs on fakes: start the
// daemon-entry fixture, then drive it with dist/cli.mjs exactly as a user would.
import { after, test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn, spawnSync, type ChildProcess } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const cli = resolve(here, '..', '..', 'dist', 'cli.mjs');
const entry = join(here, 'fixtures', 'daemon-entry.js');
const home = mkdtempSync(join(tmpdir(), 'al-cli-daemon-'));
const env = { ...process.env, AGENT_LARK_HOME: home };

let child: ChildProcess | undefined;
after(() => {
  if (child && child.exitCode === null && !child.killed) child.kill();
  rmSync(home, { recursive: true, force: true });
});

function run(args: string[]) {
  const r = spawnSync(process.execPath, [cli, ...args], { encoding: 'utf8', env, input: '' });
  return { status: r.status, stdout: r.stdout, stderr: r.stderr };
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

test('daemon --status / --stop against a daemon process', async () => {
  assert.ok(existsSync(cli), `dist/cli.mjs not found at ${cli} — run \`npm run build\` first`);
  assert.ok(existsSync(entry), `fixture not compiled: ${entry}`);

  child = spawn(process.execPath, [entry], { env, stdio: 'ignore', detached: true, windowsHide: true });
  child.unref();
  const exited = new Promise<number | null>((r) => child!.on('exit', (code) => r(code)));

  let status = run(['daemon', '--status']);
  for (let i = 0; i < 40 && status.status !== 0; i++) {
    await sleep(250);
    status = run(['daemon', '--status']);
  }
  assert.equal(status.status, 0, `daemon never answered: ${status.stderr}`);
  assert.match(status.stdout, /connected/);
  assert.doesNotMatch(status.stdout, /\p{Script=Han}/u);

  const stop = run(['daemon', '--stop']);
  assert.equal(stop.status, 0, stop.stderr);
  assert.match(stop.stdout, /stopped/);

  let deadline: NodeJS.Timeout | undefined;
  const timeout = new Promise<'timeout'>((r) => {
    deadline = setTimeout(() => r('timeout'), 10_000);
  });
  // The timer must not outlive the race: a pending 10 s timeout would keep this test process alive.
  const code = await Promise.race([exited, timeout]).finally(() => clearTimeout(deadline));
  assert.equal(code, 0, `daemon process did not exit cleanly: ${String(code)}`);
  const gone = run(['daemon', '--status']);
  assert.equal(gone.status, 1);
  assert.match(gone.stderr, /not running/);
});
