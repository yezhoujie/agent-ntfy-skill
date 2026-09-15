// The interactive `setup` in process: a scripted terminal, a fake credential
// probe, a fake herdr. Credentials land in a temporary file store.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, readFileSync, rmSync, statSync } from 'node:fs';
import { platform, tmpdir } from 'node:os';
import { join } from 'node:path';

import type { HerdrRun, PromptOutcome } from '../src/herdr.js';
import type { SetupIO } from '../src/tty.js';
import { InputInterrupted } from '../src/tty.js';

// The keychain service name is read once at import time: point it at a
// service that never holds anything before the module loads.
process.env.AGENT_LARK_KEYCHAIN = 'agent-lark-test-never-stored';
const { runSetup } = await import('../src/cli.js');
const { msg } = await import('../src/texts.js');
type SetupDeps = Parameters<typeof runSetup>[1];

const scratch: string[] = [];
function isolate(): string {
  const dir = mkdtempSync(join(tmpdir(), 'al-setup-'));
  scratch.push(dir);
  process.env.XDG_CONFIG_HOME = dir;
  process.env.AGENT_LARK_HOME = join(dir, 'home');
  process.env.AGENT_LARK_STORE = 'file';
  process.env.AGENT_LARK_KEYCHAIN = 'agent-lark-test-never-stored';
  for (const k of ['AGENT_LARK_APP_ID', 'AGENT_LARK_APP_SECRET', 'AGENT_LARK_OWNER_OPEN_ID']) delete process.env[k];
  return dir;
}
process.on('exit', () => {
  for (const d of scratch) rmSync(d, { recursive: true, force: true });
});
const credsFile = (dir: string) => join(dir, 'agent-lark', 'credentials.json');

/** A terminal that answers from a script; `''` after the script ends is a closed stdin. */
function scripted(answers: string[], isTTY = true): SetupIO & { prompts: string[]; hidden: string[]; closed: number } {
  const io = {
    isTTY,
    prompts: [] as string[],
    hidden: [] as string[],
    closed: 0,
    async question(prompt: string) {
      io.prompts.push(prompt);
      if (!answers.length) throw new InputInterrupted();
      return answers.shift()!;
    },
    async questionHidden(prompt: string) {
      io.hidden.push(prompt);
      if (!answers.length) throw new InputInterrupted();
      return answers.shift()!;
    },
    close() {
      io.closed += 1;
    },
  };
  return io;
}

interface Fake {
  deps: SetupDeps;
  out: string[];
  err: string[];
  probes: Array<[string, string]>;
  herdr: string[][];
  prompts: Array<[string, string]>;
  registered: number;
}

function fake(io: SetupIO, opts: { probe?: SetupDeps['probe']; inHerdr?: string | null; splitOk?: boolean; runOk?: boolean; closeOk?: boolean; offline?: boolean } = {}): Fake {
  const f: Fake = { out: [], err: [], probes: [], herdr: [], prompts: [], registered: 0, deps: undefined as unknown as SetupDeps };
  f.deps = {
    io,
    probe:
      opts.probe ??
      (async (id, secret) => {
        f.probes.push([id, secret]);
        return { appName: 'Test App', ownerId: 'ou_owner' };
      }),
    register: (async () => {
      f.registered += 1;
      return { client_id: 'cli_scanned', client_secret: 'qr-secret', user_info: { open_id: 'ou_qr' } } as never;
    }) as never,
    offline: opts.offline ?? false,
    herdr: {
      insideHerdr: () => opts.inHerdr !== undefined && opts.inHerdr !== null,
      currentPaneId: () => opts.inHerdr ?? null,
      splitPane: async (cwd, pane) => {
        f.herdr.push(['split', cwd, pane]);
        return opts.splitOk === false ? null : 'w1:p9';
      },
      runInPane: async (pane, argv) => {
        f.herdr.push(['run', pane, ...argv]);
        return opts.runOk !== false;
      },
      promptPane: async (pane, text): Promise<PromptOutcome> => {
        f.prompts.push([pane, text]);
        return { ok: true };
      },
      closePane: async (pane): Promise<HerdrRun> => {
        f.herdr.push(['close', pane]);
        return { ok: opts.closeOk !== false, stdout: '', error: opts.closeOk === false ? 'no such pane' : undefined };
      },
    },
    out: (t) => f.out.push(t),
    err: (t) => f.err.push(t),
    execPath: '/usr/bin/node',
    cliPath: '/skill/dist/cli.mjs',
    cwd: '/work/proj',
  };
  return f;
}
const text = (chunks: string[]) => chunks.join('');

test('menu: choosing 1 goes to the QR-code registration and stores what came back', async () => {
  const dir = isolate();
  const f = fake(scripted(['1']));
  const rc = await runSetup([], f.deps);
  assert.equal(rc, 0, text(f.err));
  assert.equal(f.registered, 1);
  assert.equal(f.probes.length, 0);
  assert.equal(JSON.parse(readFileSync(credsFile(dir), 'utf8')).appId, 'cli_scanned');
  assert.match(text(f.out), /1\) /);
});

test('menu: choosing 2 goes to the reuse branch; the secret is read hidden, probed, stored 0600, and the scope list is printed', async () => {
  const dir = isolate();
  const io = scripted(['2', 'cli_abc123', 'the-secret']);
  const f = fake(io);
  const rc = await runSetup([], f.deps);
  assert.equal(rc, 0, text(f.err));
  assert.equal(f.registered, 0);
  assert.deepEqual(f.probes, [['cli_abc123', 'the-secret']]);
  assert.equal(io.hidden.length, 1, 'the secret prompt must go through questionHidden');
  assert.doesNotMatch(text(f.out) + text(f.err), /the-secret/, 'the secret must never be printed');
  // POSIX mode bits mean nothing on Windows (the file reads 0666 there); the same split as the warning in creds.ts.
  if (platform() !== 'win32') assert.equal(statSync(credsFile(dir)).mode & 0o777, 0o600);
  const stored = JSON.parse(readFileSync(credsFile(dir), 'utf8')) as { appId: string; appSecret: string; ownerOpenId: string };
  assert.deepEqual(stored, { appId: 'cli_abc123', appSecret: 'the-secret', ownerOpenId: 'ou_owner' });
  const out = text(f.out);
  assert.match(out, /speech_to_text:speech/);
  assert.match(out, /im:message\.urgent/);
  assert.match(out, /im\.message\.receive_v1/);
  assert.match(out, /card\.action\.trigger/);
  assert.match(out, /Test App/);
  assert.equal(io.closed, 1);
});

test('menu: anything but 1 or 2 is asked again and does not count against anything', async () => {
  isolate();
  const io = scripted(['3', '', 'x', '2', 'cli_ok1', 's']);
  const f = fake(io);
  assert.equal(await runSetup([], f.deps), 0, text(f.err));
  assert.equal(io.prompts.filter((p) => /\[1\/2\]/.test(p)).length, 4);
  assert.equal(f.probes.length, 1);
});

test('no terminal and no --reuse: straight to the QR code, no menu', async () => {
  isolate();
  const io = scripted([], false);
  const f = fake(io);
  assert.equal(await runSetup([], f.deps), 0, text(f.err));
  assert.equal(f.registered, 1);
  assert.equal(io.prompts.length, 0);
});

test('reuse: a malformed App ID is asked again without counting as an attempt', async () => {
  isolate();
  const io = scripted(['nope', 'cli-x', 'cli_good', 's']);
  const f = fake(io);
  assert.equal(await runSetup(['--reuse'], f.deps), 0, text(f.err));
  assert.equal(io.prompts.filter((p) => /App ID/.test(p)).length, 3);
  assert.deepEqual(f.probes, [['cli_good', 's']]);
});

test('reuse: three refused probes exit 1 with the Feishu code and message shown, and nothing stored', async () => {
  const dir = isolate();
  const io = scripted(['cli_a', 's1', 'cli_a', 's2', 'cli_a', 's3']);
  const refused = Object.assign(new Error('Request failed with status code 400'), { response: { data: { code: 10003, msg: 'invalid app_secret' } } });
  const f = fake(io, {
    probe: async (id, secret) => {
      f.probes.push([id, secret]);
      throw refused;
    },
  });
  assert.equal(await runSetup(['--reuse'], f.deps), 1);
  assert.equal(f.probes.length, 3);
  assert.equal(existsSync(credsFile(dir)), false);
  const all = text(f.out) + text(f.err);
  assert.match(all, /10003/);
  assert.match(all, /invalid app_secret/);
  assert.doesNotMatch(all, /s1|s2|s3/);
  assert.match(text(f.err), /^agent-lark: .*3/m);
});

test('reuse: a probe that fails twice then succeeds stores the third pair', async () => {
  const dir = isolate();
  const io = scripted(['cli_a', 'bad1', 'cli_a', 'bad2', 'cli_a', 'good']);
  const f = fake(io, {
    probe: async (id, secret) => {
      f.probes.push([id, secret]);
      if (secret !== 'good') throw new Error('refused');
      return { appName: 'App', ownerId: 'ou_1' };
    },
  });
  assert.equal(await runSetup(['--reuse'], f.deps), 0, text(f.err));
  assert.equal(JSON.parse(readFileSync(credsFile(dir), 'utf8')).appSecret, 'good');
});

test('reuse without a terminal, inside herdr: a pane is split below the caller and the CLI is run there with --report-to <caller> --close-pane', async () => {
  isolate();
  const f = fake(scripted([], false), { inHerdr: 'w1:p2' });
  assert.equal(await runSetup(['--reuse'], f.deps), 0, text(f.err));
  assert.deepEqual(f.herdr[0], ['split', '/work/proj', 'w1:p2']);
  const run = f.herdr[1]!;
  assert.equal(run[0], 'run');
  assert.equal(run[1], 'w1:p9');
  const argv = run.slice(2);
  assert.deepEqual(argv.slice(0, 2), ['/usr/bin/node', '/skill/dist/cli.mjs']);
  assert.equal(argv[2], '--home');
  assert.equal(argv[3], process.env.AGENT_LARK_HOME);
  assert.deepEqual(argv.slice(4), ['setup', '--reuse', '--report-to', 'w1:p2', '--close-pane']);
  assert.match(text(f.out), /pane w1:p9/);
  assert.match(text(f.out), /\[agent-lark\] setup:/);
  assert.equal(f.probes.length, 0);
});

test('reuse without a terminal, outside herdr: exit 4 and stderr carries the command for the user to run', async () => {
  isolate();
  const f = fake(scripted([], false), { inHerdr: null });
  assert.equal(await runSetup(['--reuse'], f.deps), 4);
  assert.equal(f.herdr.length, 0);
  const err = text(f.err);
  assert.match(err, /^agent-lark: /m);
  assert.match(err, /\/usr\/bin\/node \/skill\/dist\/cli\.mjs --home \S+ setup --reuse$/m);
  assert.equal(text(f.out), '');
});

test('reuse without a terminal, inside herdr but the pane cannot be opened: exit 3 with the same command as a fallback', async () => {
  isolate();
  const f = fake(scripted([], false), { inHerdr: 'w1:p2', splitOk: false });
  assert.equal(await runSetup(['--reuse'], f.deps), 3);
  assert.match(text(f.err), /setup --reuse$/m);
});

test('--report-to: success sends one line naming the app id, then --close-pane asks and closes on Enter', async () => {
  isolate();
  const io = scripted(['cli_ok1', 's', '']);
  const f = fake(io, { inHerdr: 'w1:p9' });
  assert.equal(await runSetup(['--reuse', '--report-to', 'w1:p2', '--close-pane'], f.deps), 0, text(f.err));
  assert.equal(f.prompts.length, 1);
  assert.equal(f.prompts[0]![0], 'w1:p2');
  assert.match(f.prompts[0]![1], /^\[agent-lark\] setup: credentials stored for cli_ok1/);
  assert.doesNotMatch(f.prompts[0]![1], /\bs\b/);
  assert.deepEqual(f.herdr.at(-1), ['close', 'w1:p9']);
});

test('--report-to: failure sends one "failed:" line with the reason and the pane is left open', async () => {
  isolate();
  const io = scripted(['cli_a', 'x', 'cli_a', 'x', 'cli_a', 'x']);
  const f = fake(io, {
    inHerdr: 'w1:p9',
    probe: async () => {
      throw new Error('refused');
    },
  });
  assert.equal(await runSetup(['--reuse', '--report-to', 'w1:p2', '--close-pane'], f.deps), 1);
  assert.equal(f.prompts.length, 1);
  assert.match(f.prompts[0]![1], /^\[agent-lark\] setup: failed: /);
  assert.ok(!f.herdr.some((h) => h[0] === 'close'), 'the pane must stay open after a failure');
});

test('--close-pane: "n" keeps the pane; run by hand outside herdr the question is skipped', async () => {
  isolate();
  const f = fake(scripted(['cli_ok1', 's', 'n']), { inHerdr: 'w1:p9' });
  assert.equal(await runSetup(['--reuse', '--close-pane'], f.deps), 0);
  assert.ok(!f.herdr.some((h) => h[0] === 'close'));
  const g = fake(scripted(['cli_ok1', 's']), { inHerdr: null });
  assert.equal(await runSetup(['--reuse', '--close-pane'], g.deps), 0);
  assert.equal(g.herdr.length, 0);
});

test('Ctrl-C (or a closed stdin) during the questions: exit 130, nothing stored, the report line says interrupted', async () => {
  const dir = isolate();
  const f = fake(scripted(['cli_ok1']), { inHerdr: 'w1:p9' });
  assert.equal(await runSetup(['--reuse', '--report-to', 'w1:p2'], f.deps), 130);
  assert.equal(existsSync(credsFile(dir)), false);
  assert.deepEqual(f.prompts, [['w1:p2', msg.setupReportInterrupted]]);
});

test('existing stored credentials: setup (and setup --reuse) only says so and exits 0; --reset clears them first', async () => {
  const dir = isolate();
  assert.equal(await runSetup(['--reuse'], fake(scripted(['cli_first', 's'])).deps), 0);
  const again = fake(scripted(['2', 'cli_second', 's']));
  assert.equal(await runSetup([], again.deps), 0);
  assert.match(text(again.out), /already exist/);
  assert.equal(again.probes.length, 0);
  assert.equal(JSON.parse(readFileSync(credsFile(dir), 'utf8')).appId, 'cli_first');
  const reset = fake(scripted(['cli_second', 's']));
  assert.equal(await runSetup(['--reset', '--reuse'], reset.deps), 0, text(reset.err));
  assert.equal(JSON.parse(readFileSync(credsFile(dir), 'utf8')).appId, 'cli_second');
});

test('AGENT_LARK_OFFLINE: both network paths are refused with exit 3 and the SDK is never called', async () => {
  const dir = isolate();
  const qr = fake(scripted([], false), { offline: true });
  assert.equal(await runSetup([], qr.deps), 3);
  assert.equal(qr.registered, 0);
  assert.match(text(qr.err), /^agent-lark: offline: refusing to contact Feishu/m);
  const reuse = fake(scripted(['cli_ok1', 's']), { offline: true });
  assert.equal(await runSetup(['--reuse'], reuse.deps), 3);
  assert.equal(reuse.probes.length, 0);
  assert.equal(existsSync(credsFile(dir)), false);
});

test('a probe error that quotes the secret: the secret is masked everywhere it could surface — stdout, stderr and the report line', async () => {
  isolate();
  const io = scripted(['cli_a', 'hunter2', 'cli_a', 'hunter2', 'cli_a', 'hunter2']);
  const f = fake(io, {
    inHerdr: 'w1:p9',
    probe: async (id, secret) => {
      throw new Error(`POST /auth failed for app_id=${id} app_secret=${secret} (400)`);
    },
  });
  assert.equal(await runSetup(['--reuse', '--report-to', 'w1:p2'], f.deps), 1);
  const everything = text(f.out) + text(f.err) + f.prompts.map((p) => p[1]).join('\n');
  assert.doesNotMatch(everything, /hunter2/, everything);
  assert.match(everything, /app_secret=\*\*\*/);
  assert.equal(f.prompts.length, 1);
});

test('--report-to when credentials already exist: the pane still reports one line, and nothing is asked', async () => {
  isolate();
  assert.equal(await runSetup(['--reuse'], fake(scripted(['cli_first', 's'])).deps), 0);
  const io = scripted(['cli_second', 's']);
  const f = fake(io, { inHerdr: 'w1:p9' });
  assert.equal(await runSetup(['--reuse', '--report-to', 'w1:p2', '--close-pane'], f.deps), 0);
  assert.equal(io.prompts.length + io.hidden.length, 0);
  assert.equal(f.prompts.length, 1);
  assert.equal(f.prompts[0]![0], 'w1:p2');
  assert.match(f.prompts[0]![1], /^\[agent-lark\] setup: .*already/);
  assert.match(f.prompts[0]![1], /--reset/);
  assert.ok(!f.herdr.some((h) => h[0] === 'close'), 'nothing was set up, so the pane is left for the human to read');
});
