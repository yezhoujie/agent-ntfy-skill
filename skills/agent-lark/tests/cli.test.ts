// Runs the shipped bundle (dist/cli.mjs) as a child process, the way an agent
// would, and checks the paths that end before any daemon is contacted.
import { after, test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const cli = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', 'dist', 'cli.mjs');

const scratch: string[] = [];
function tmp(prefix: string): string {
  const dir = mkdtempSync(join(tmpdir(), prefix));
  scratch.push(dir);
  return dir;
}
after(() => {
  for (const dir of scratch) rmSync(dir, { recursive: true, force: true });
});

function run(args: string[], opts: { input?: string; home?: string } = {}) {
  assert.ok(existsSync(cli), `dist/cli.mjs not found at ${cli} — run \`npm run build\` first`);
  const home = opts.home ?? tmp('agent-lark-cli-');
  const r = spawnSync(process.execPath, [cli, ...args], {
    input: opts.input ?? '',
    encoding: 'utf8',
    env: { ...process.env, AGENT_LARK_HOME: home },
  });
  return { status: r.status, stdout: r.stdout, stderr: r.stderr, home };
}

const hasHan = (s: string): boolean => /\p{Script=Han}/u.test(s);

test('help: exit 0, English, names agent-lark and never herdr-lark', () => {
  const r = run(['help']);
  assert.equal(r.status, 0, r.stderr);
  assert.match(r.stdout, /agent-lark/);
  assert.doesNotMatch(r.stdout, /herdr-lark/);
  assert.equal(hasHan(r.stdout), false, r.stdout);
});

test('unknown command: exit 1, English hint on stderr with the agent-lark prefix', () => {
  const r = run(['nope']);
  assert.equal(r.status, 1);
  assert.match(r.stderr, /^agent-lark: /);
  assert.match(r.stderr, /nope/);
  assert.equal(hasHan(r.stderr), false, r.stderr);
});

test('say is gone: exit 1 as an unknown command', () => {
  const r = run(['say']);
  assert.equal(r.status, 1);
  assert.match(r.stderr, /say/);
});

test('ask with malformed JSON on stdin: exit 1, English, nothing sent', () => {
  const r = run(['ask'], { input: '{' });
  assert.equal(r.status, 1);
  assert.match(r.stderr, /JSON/);
  assert.equal(hasHan(r.stderr), false, r.stderr);
});

// `daemon --stop` with no daemon listening removes a stale pid file — only
// visible in the directory --home named.
function stalePid(): string {
  const pid = join(tmp('agent-lark-home-'), 'daemon.pid');
  writeFileSync(pid, '12345\n');
  return pid;
}

test('--home <dir> points the CLI at that state directory', () => {
  const pid = stalePid();
  const r = run(['--home', dirname(pid), 'daemon', '--stop'], { home: tmp('agent-lark-other-') });
  assert.equal(r.status, 0, r.stderr);
  assert.equal(hasHan(r.stdout), false, r.stdout);
  assert.equal(existsSync(pid), false, 'stale daemon.pid under --home was not removed');
});

test('--home=<dir> is accepted too', () => {
  const pid = stalePid();
  const r = run([`--home=${dirname(pid)}`, 'daemon', '--stop'], { home: tmp('agent-lark-other-') });
  assert.equal(r.status, 0, r.stderr);
  assert.equal(existsSync(pid), false, 'stale daemon.pid under --home= was not removed');
});
