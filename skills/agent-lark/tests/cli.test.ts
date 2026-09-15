// Runs the shipped bundle (dist/cli.mjs) as a child process, the way an agent
// would, and checks the paths that end before any daemon is contacted.
import { after, before, describe, test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn, spawnSync, type ChildProcess } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { isTransientNetworkError, waitConnected } from '../src/cli.js';
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

// Every spawned CLI is pointed away from the machine's real credentials: a
// keychain service that never holds anything, and a throwaway config dir.
const isolatedEnv = (): NodeJS.ProcessEnv => {
  const env: NodeJS.ProcessEnv = { ...process.env, AGENT_LARK_KEYCHAIN: 'agent-lark-test-never-stored', XDG_CONFIG_HOME: tmp('agent-lark-cfg-') };
  for (const k of ['AGENT_LARK_APP_ID', 'AGENT_LARK_APP_SECRET', 'AGENT_LARK_OWNER_OPEN_ID', 'AGENT_LARK_STORE']) delete env[k];
  return env;
};

function run(args: string[], opts: { input?: string; home?: string } = {}) {
  assert.ok(existsSync(cli), `dist/cli.mjs not found at ${cli} — run \`npm run build\` first`);
  const home = opts.home ?? tmp('agent-lark-cli-');
  // A bounded run: a command that reaches out to Feishu by mistake must fail
  // the test in seconds, not sit there polling for a scan.
  const r = spawnSync(process.execPath, [cli, ...args], {
    input: opts.input ?? '',
    encoding: 'utf8',
    env: { ...isolatedEnv(), AGENT_LARK_HOME: home },
    timeout: 10_000,
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

test('help: setup has a menu / QR line, a --reuse line and a hand-off line; --app-id and --store are gone', () => {
  const r = run(['help']);
  assert.equal(r.status, 0, r.stderr);
  assert.match(r.stdout, /^  setup \[--update\] \[--reset\] \[--scopes a,b\]/m, r.stdout);
  assert.match(r.stdout, /^  setup --reuse\s{2,}\S.*App ID.*Secret/m, r.stdout);
  assert.match(r.stdout, /--report-to <pane>.*--close-pane/m, r.stdout);
  assert.doesNotMatch(r.stdout, /--app-id|--store/);
});

test('the test suite runs offline: `setup` on the shipped bundle exits 3 without contacting Feishu', () => {
  assert.equal(process.env.AGENT_LARK_OFFLINE, '1', 'scripts/test.mjs must set AGENT_LARK_OFFLINE');
  const r = run(['setup']);
  assert.equal(r.status, 3, r.stdout + r.stderr);
  assert.match(r.stderr, /^agent-lark: offline: refusing to contact Feishu/m);
});

// ---- unknown options: every subcommand refuses them before doing anything ----

const UNKNOWN: Array<[string, string[], string]> = [
  ['help', ['help', '--verbose'], '--verbose'],
  ['setup', ['setup', '--app-id', 'cli_x'], '--app-id'],
  ['setup --store (gone)', ['setup', '--store', 'file'], '--store'],
  ['daemon', ['daemon', '--status', '--json'], '--json'],
  ['bind', ['bind', '--chat', 'oc_x', '--force'], '--force'],
  ['unbind', ['unbind', '--all'], '--all'],
  ['rename', ['rename', 'x', '--new'], '--new'],
  ['ask', ['ask', '--timeout', '5', '--idle'], '--idle'],
  ['notify', ['notify', '--urgent'], '--urgent'],
  ['send-file', ['send-file', 'a.png', '--title', 't'], '--title'],
  ['away on', ['away', 'on', '--chat', 'oc_x'], '--chat'],
  ['away off', ['away', 'off', '--json'], '--json'],
  ['away status', ['away', 'status', '--name', 'x'], '--name'],
  ['status', ['status', '--json'], '--json'],
];
for (const [name, argv, bad] of UNKNOWN) {
  test(`unknown option on ${name}: exit 1 naming the option, nothing else attempted`, () => {
    const r = run(argv, { input: '{}' });
    assert.equal(r.status, 1, `${name}: ${r.stdout}${r.stderr}`);
    assert.match(r.stderr, new RegExp(`^agent-lark: unknown option ${bad}`, 'm'), r.stderr);
    assert.equal(r.stdout, '');
  });
}

test('away off with no daemon: exit 0, the local state file is switched off, and stdout says the daemon was not asked', () => {
  const project = realpathSync(tmp('agent-lark-off-'));
  mkdirSync(join(project, '.agent-lark'));
  writeFileSync(join(project, '.agent-lark', 'state.json'), JSON.stringify({ away: true, chatId: 'oc_x', target: project, updated: '' }));
  const r = spawnSync(process.execPath, [cli, 'away', 'off'], { encoding: 'utf8', env: { ...isolatedEnv(), AGENT_LARK_HOME: tmp('agent-lark-home-') }, input: '', cwd: project });
  assert.equal(r.status, 0, r.stderr);
  assert.match(r.stdout, /^Remote mode is off\.$/m);
  assert.match(r.stdout, /daemon is not running; local state cleared/);
  const state = JSON.parse(readFileSync(join(project, '.agent-lark', 'state.json'), 'utf8')) as { away: boolean; chatId: string | null };
  assert.equal(state.away, false);
  assert.equal(state.chatId, 'oc_x');
});

test('away off with no daemon and no state file: exit 0, "never used", nothing created', () => {
  const project = realpathSync(tmp('agent-lark-off-'));
  const r = spawnSync(process.execPath, [cli, 'away', 'off'], { encoding: 'utf8', env: { ...isolatedEnv(), AGENT_LARK_HOME: tmp('agent-lark-home-') }, input: '', cwd: project });
  assert.equal(r.status, 0, r.stderr);
  assert.match(r.stdout, /never used agent-lark/);
  assert.equal(existsSync(join(project, '.agent-lark')), false);
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
  const env: NodeJS.ProcessEnv = { ...isolatedEnv(), AGENT_LARK_HOME: home, AGENT_LARK_APP_ID: 'cli_fake', AGENT_LARK_APP_SECRET: 'fake-secret' };
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

  // The daemon runs in its own directory; a relative path only means anything
  // where the CLI was invoked, so the CLI must resolve it before asking.
  test('send-file with a relative path: resolved against the caller\'s directory, not the daemon\'s, and sent', () => {
    mkdirSync(join(project, 'out'));
    writeFileSync(join(project, 'out', 'note.txt'), 'hello');
    const r = cmd(['send-file', 'out/note.txt', '--caption', 'a note']);
    assert.equal(r.status, 0, r.stdout + r.stderr);
    assert.match(r.stdout, /^Sent to the project group$/m);
  });

  test('send-file with a relative path to a missing file: exit 1 naming the resolved absolute path', () => {
    const r = cmd(['send-file', 'out/missing.txt']);
    assert.equal(r.status, 1, r.stdout + r.stderr);
    assert.ok(r.stderr.includes(`file not found: ${join(project, 'out', 'missing.txt')}`), r.stderr);
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
    ...isolatedEnv(),
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
  status: { pid: 1, connection: connected ? 'connected' : 'connecting', connected, lastError, pendingAsks: 0, bindings: 0, startedAt: '', media: { ttlDays: 7, files: 0, bytes: 0, at: '' } },
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

// ---- setup: which registration failures are worth a fresh QR code -----------

test('isTransientNetworkError: socket-level trouble is transient; expiry and Feishu refusals are not', () => {
  for (const err of [
    Object.assign(new Error('read ECONNRESET'), { code: 'ECONNRESET' }),
    Object.assign(new Error('connect ETIMEDOUT 1.2.3.4:443'), { code: 'ETIMEDOUT' }),
    Object.assign(new Error('getaddrinfo EAI_AGAIN open.feishu.cn'), { code: 'EAI_AGAIN' }),
    new Error('Client network socket disconnected before secure TLS connection was established'),
    new Error('socket hang up'),
    { code: 'ECONNREFUSED', message: 'connect ECONNREFUSED' },
  ])
    assert.equal(isTransientNetworkError(err), true, String((err as Error).message));
  for (const err of [
    new Error('QR code expired'),
    Object.assign(new Error('Request failed with status code 400'), { response: { data: { code: 99991663, msg: 'app not found' } } }),
    { code: 20001, msg: 'invalid param' },
    new Error('boom'),
    undefined,
    'a string',
  ])
    assert.equal(isTransientNetworkError(err), false, String(err));
});
