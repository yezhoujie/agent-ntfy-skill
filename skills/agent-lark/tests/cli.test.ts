// Runs the shipped bundle (dist/cli.mjs) as a child process, the way an agent
// would, and checks the paths that end before any daemon is contacted.
import { after, before, describe, test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn, spawnSync, type ChildProcess } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { waitConnected } from '../src/cli.js';
import type { Response } from '../src/ipc.js';

const here = dirname(fileURLToPath(import.meta.url));
const cli = resolve(here, '..', '..', 'dist', 'cli.mjs');

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

// ---- against a daemon process running on fakes ------------------------------
// The commands below need a daemon to answer; the daemon-entry fixture is one
// that never touches Feishu or herdr. Each project is a temporary directory
// outside any git checkout, so state.json lands there and nowhere else.

describe('with a fake daemon', () => {
  const home = mkdtempSync(join(tmpdir(), 'al-cli-home-'));
  // Not inside herdr as far as the CLI can tell, whatever spawned the tests.
  // `away on` checks for credentials before talking to the daemon; the fake
  // pair keeps it off the real keychain.
  const env: NodeJS.ProcessEnv = { ...process.env, AGENT_LARK_HOME: home, AGENT_LARK_APP_ID: 'cli_fake', AGENT_LARK_APP_SECRET: 'fake-secret' };
  delete env.HERDR_ENV;
  delete env.HERDR_PANE_ID;
  const entry = join(here, 'fixtures', 'daemon-entry.js');
  const project = realpathSync(mkdtempSync(join(tmpdir(), 'al-cli-proj-')));
  const other = realpathSync(mkdtempSync(join(tmpdir(), 'al-cli-proj-')));
  let child: ChildProcess | undefined;

  const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
  function cmd(args: string[], cwd = project) {
    const r = spawnSync(process.execPath, [cli, ...args], { encoding: 'utf8', env, input: '', cwd });
    return { status: r.status, stdout: r.stdout, stderr: r.stderr };
  }
  const state = (): Record<string, unknown> => JSON.parse(readFileSync(join(project, '.agent-lark', 'state.json'), 'utf8')) as Record<string, unknown>;

  before(async () => {
    assert.ok(existsSync(cli), `dist/cli.mjs not found at ${cli} — run \`npm run build\` first`);
    assert.ok(existsSync(entry), `fixture not compiled: ${entry}`);
    // A group this project let go of earlier: what `away on` must offer back.
    writeFileSync(
      join(home, 'bindings.json'),
      JSON.stringify({
        bindings: [
          {
            root: project,
            label: 'proj',
            chatId: 'oc_old',
            name: 'old task [proj]',
            paneId: null,
            away: false,
            lang: null,
            boundAt: '2026-01-01T00:00:00.000Z',
            releasedAt: '2026-01-02T03:04:05.000Z',
          },
        ],
      }),
    );
    child = spawn(process.execPath, [entry], { env, stdio: 'ignore', detached: true, windowsHide: true });
    child.unref();
    let status = cmd(['daemon', '--status']);
    for (let i = 0; i < 40 && status.status !== 0; i++) {
      await sleep(250);
      status = cmd(['daemon', '--status']);
    }
    assert.equal(status.status, 0, `daemon never answered: ${status.stderr}`);
  });
  after(() => {
    cmd(['daemon', '--stop', '--force']);
    if (child && child.exitCode === null && !child.killed) child.kill();
    for (const dir of [home, project, other]) rmSync(dir, { recursive: true, force: true });
  });

  test('away status --json before any use: the four fields, nothing else', () => {
    const r = cmd(['away', 'status', '--json']);
    assert.equal(r.status, 0, r.stderr);
    assert.deepEqual(JSON.parse(r.stdout), { away: false, chatId: null, target: project, updated: '' });
  });

  test('away on with a group to offer back: exit 4, one candidate per line on stderr, then how to rerun', () => {
    const r = cmd(['away', 'on', '--name', 'next task']);
    assert.equal(r.status, 4, r.stdout + r.stderr);
    assert.match(r.stderr, /^old task \[proj\] {2}released 2026-01-02T03:04:05\.000Z {2}oc_old$/m);
    assert.match(r.stderr, /rerun with --reuse <chatId> or --new/);
    assert.equal(existsSync(join(project, '.agent-lark')), false, 'state was written although nothing was bound');
  });

  test('away on --reuse: bound, state.json written, and outside herdr the extra line is printed', () => {
    const r = cmd(['away', 'on', '--reuse', 'oc_old', '--name', 'next task']);
    assert.equal(r.status, 0, r.stdout + r.stderr);
    assert.match(r.stdout, /Remote mode is on/);
    assert.match(r.stdout, /not inside herdr/i);
    assert.doesNotMatch(r.stdout, /\p{Script=Han}/u);
    const s = state();
    assert.deepEqual(Object.keys(s).sort(), ['away', 'chatId', 'target', 'updated']);
    assert.equal(s.away, true);
    assert.equal(s.chatId, 'oc_old');
    const st = cmd(['away', 'status', '--json']);
    assert.deepEqual(Object.keys(JSON.parse(st.stdout) as object).sort(), ['away', 'chatId', 'target', 'updated']);
    assert.equal((JSON.parse(st.stdout) as { away: boolean }).away, true);
    const text = cmd(['away', 'status']);
    assert.match(text.stdout, /remote mode: on {2}group: oc_old/);
    assert.doesNotMatch(text.stdout, /pane/);
  });

  test('rename with a task name over 60 code points: exit 1 before the daemon is asked', () => {
    const r = cmd(['rename', '😀'.repeat(61)]);
    assert.equal(r.status, 1, r.stdout + r.stderr);
    assert.match(r.stderr, /60/);
    assert.match(r.stderr, /61/);
    const usage = cmd(['rename']);
    assert.equal(usage.status, 1);
    assert.match(usage.stderr, /Usage/);
  });

  test('away off keeps the group and only flips the switch', () => {
    const r = cmd(['away', 'off']);
    assert.equal(r.status, 0, r.stderr);
    assert.equal(state().away, false);
    assert.equal(state().chatId, 'oc_old');
  });

  test('unbind: exit 0 names the group and clears chatId; a project with no live group gets exit 1', () => {
    const r = cmd(['unbind']);
    assert.equal(r.status, 0, r.stderr);
    assert.match(r.stdout, /oc_old|old task|next task/);
    assert.equal(state().chatId, null);
    const none = cmd(['unbind'], other);
    assert.equal(none.status, 1, none.stdout + none.stderr);
    assert.match(none.stderr, /not bound/);
  });

  test('away off after unbind (no live group) is still exit 0 and leaves away=false', () => {
    const r = cmd(['away', 'off']);
    assert.equal(r.status, 0, r.stdout + r.stderr);
    assert.equal(state().away, false);
    const never = cmd(['away', 'off'], other);
    assert.equal(never.status, 0, never.stdout + never.stderr);
    assert.equal(existsSync(join(other, '.agent-lark')), false);
  });

  test('status lists released groups apart from live ones, the current project marked with *', () => {
    const r = cmd(['status']);
    assert.equal(r.status, 0, r.stderr);
    assert.match(r.stdout, /herdr: not inside herdr/);
    assert.match(r.stdout, /released/);
    assert.match(r.stdout, /^ {2}\* .*oc_old/m);
    const elsewhere = cmd(['status'], other);
    assert.match(elsewhere.stdout, /^ {4}.*oc_old/m);
  });
});

// `away on` must wait for the daemon's Feishu handshake, not just for the
// daemon: a daemon whose first handshakes fail answers IPC long before it is
// connected, and a bind that has to look at Feishu would be refused then.
describe('with a fake daemon whose first handshakes fail', () => {
  const home = mkdtempSync(join(tmpdir(), 'al-cli-late-home-'));
  // Six failures at a doubling 50 ms interval: connected about 3 s in.
  const env: NodeJS.ProcessEnv = {
    ...process.env,
    AGENT_LARK_HOME: home,
    AGENT_LARK_APP_ID: 'cli_fake',
    AGENT_LARK_APP_SECRET: 'fake-secret',
    AGENT_LARK_FAKE_CONNECT: 'fail:6',
  };
  delete env.HERDR_ENV;
  delete env.HERDR_PANE_ID;
  const entry = join(here, 'fixtures', 'daemon-entry.js');
  const project = realpathSync(mkdtempSync(join(tmpdir(), 'al-cli-late-proj-')));
  let child: ChildProcess | undefined;
  const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
  function cmd(args: string[]) {
    const r = spawnSync(process.execPath, [cli, ...args], { encoding: 'utf8', env, input: '', cwd: project });
    return { status: r.status, stdout: r.stdout, stderr: r.stderr };
  }

  before(async () => {
    writeFileSync(
      join(home, 'bindings.json'),
      JSON.stringify({
        bindings: [
          { root: project, label: 'proj', chatId: 'oc_old', name: 'old task [proj]', paneId: null, away: false, lang: null, boundAt: '2026-01-01T00:00:00.000Z', releasedAt: '2026-01-02T00:00:00.000Z' },
        ],
      }),
    );
    child = spawn(process.execPath, [entry], { env, stdio: 'ignore', detached: true, windowsHide: true });
    child.unref();
    let status = cmd(['daemon', '--status']);
    for (let i = 0; i < 40 && status.status !== 0; i++) {
      await sleep(250);
      status = cmd(['daemon', '--status']);
    }
    assert.equal(status.status, 0, `daemon never answered: ${status.stderr}`);
    assert.match(status.stdout, /connected false/);
  });
  after(() => {
    cmd(['daemon', '--stop', '--force']);
    if (child && child.exitCode === null && !child.killed) child.kill();
    for (const dir of [home, project]) rmSync(dir, { recursive: true, force: true });
  });

  test('away on --reuse waits for the handshake, then takes the group back; a rename the fake cannot do is a note, not a failure', () => {
    const on = cmd(['away', 'on', '--reuse', 'oc_old', '--name', 'again']);
    assert.equal(on.status, 0, on.stdout + on.stderr);
    assert.match(on.stdout, /Took back Feishu group "old task \[proj\]"/);
    assert.match(on.stdout, /Remote mode is on/);
    assert.match(on.stderr, /^note: bound, but renaming the group failed/m);
    const s = JSON.parse(readFileSync(join(project, '.agent-lark', 'state.json'), 'utf8')) as { away: boolean; chatId: string };
    assert.equal(s.away, true);
    assert.equal(s.chatId, 'oc_old');
  });
});

// ---- waiting for the handshake, in-process ---------------------------------
// The wait is a function of a ping and a deadline, so the branch that gives up
// is covered without a 15 s sleep or a knob in the shipped code.

const pong = (connected: boolean, lastError: string | null): Response => ({
  ok: true,
  kind: 'pong',
  status: { pid: 1, connection: connected ? 'connected' : 'connecting', connected, lastError, pendingAsks: 0, bindings: 0, startedAt: '' },
});

test('waitConnected gives up at the deadline with the daemon\'s last connect error', async () => {
  let pings = 0;
  const t0 = Date.now();
  const r = await waitConnected(async () => (pings += 1, pong(false, 'handshake refused')), 100, 10);
  assert.deepEqual(r, { connected: false, error: 'handshake refused' });
  assert.ok(pings >= 2, `only ${pings} ping(s)`);
  assert.ok(Date.now() - t0 < 1000, 'did not give up at the deadline');
});

test('waitConnected reports "still connecting" when no attempt has failed yet, and the IPC error when the daemon is gone', async () => {
  const quiet = await waitConnected(async () => pong(false, null), 50, 10);
  assert.deepEqual(quiet, { connected: false, error: 'still connecting' });
  const gone = await waitConnected(async () => ({ ok: false, code: 3, message: 'daemon went away', reason: 'down' }), 50, 10);
  assert.deepEqual(gone, { connected: false, error: 'daemon went away' });
});

test('waitConnected returns as soon as a ping says connected', async () => {
  let pings = 0;
  const r = await waitConnected(async () => (pings += 1, pong(pings >= 3, pings >= 3 ? null : 'not yet')), 5000, 10);
  assert.deepEqual(r, { connected: true });
  assert.equal(pings, 3);
});
