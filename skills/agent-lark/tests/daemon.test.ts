// The daemon in-process, with the Feishu channel and herdr replaced by fakes.
import { after, test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { createFakeChannel, type FakeChannelOptions } from './fixtures/fake-channel.js';
import { createFakeHerdr } from './fixtures/fake-herdr.js';

process.env.AGENT_LARK_APP_ID = 'cli_fake';
process.env.AGENT_LARK_APP_SECRET = 'fake-secret';

const { runDaemon, DaemonStartError } = await import('../src/daemon.js');
const { request } = await import('../src/ipc.js');
const { msg } = await import('../src/texts.js');

type Daemon = Awaited<ReturnType<typeof runDaemon>>;
const homes: string[] = [];
const daemons: Daemon[] = [];
function freshHome(): string {
  const dir = mkdtempSync(join(tmpdir(), 'al-daemon-'));
  homes.push(dir);
  process.env.AGENT_LARK_HOME = dir;
  return dir;
}
// A failed assertion must not leave a daemon (IPC server, timers) holding the
// process open: every daemon started here is stopped when the file is done,
// whether or not its test reached its own stop().
after(async () => {
  for (const d of daemons) await d.stop();
  for (const dir of homes) rmSync(dir, { recursive: true, force: true });
});

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
/** Poll until `probe` is truthy; slow runners get time, fast ones do not wait. */
type Truthy<T> = Exclude<NonNullable<T>, false | 0 | ''>;
async function waitFor<T>(probe: () => Promise<T> | T, what: string, timeoutMs = 2000): Promise<Truthy<T>> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const v = await probe();
    if (v) return v as Truthy<T>;
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${what}`);
    await sleep(20);
  }
}
const PER_TEST = { timeout: 10_000 };

async function ping() {
  const res = await request({ type: 'ping' }, { timeoutMs: 1000 });
  return res.ok && res.kind === 'pong' ? res.status : null;
}

async function start(channelOpts: FakeChannelOptions = {}, retryMs = 50) {
  freshHome();
  const fake = createFakeChannel(channelOpts);
  const herdr = createFakeHerdr();
  const daemon = await runDaemon({ createChannel: () => fake.channel, herdr: herdr.deps, connectRetryMs: retryMs });
  daemons.push(daemon);
  return { fake, herdr, daemon };
}

const askPayload = {
  title: 't',
  doing: 'd',
  description: 'x',
  blocker: 'b',
  reasoning: 'r',
  question: 'q',
  recommend: 'a',
  options: [
    { id: 'a', label: 'Keep', consequence: 'c' },
    { id: 'b', label: 'Drop', consequence: 'c' },
  ],
};

test('starts, answers ping as connected, stop() makes ping fail and leaves the process alive', PER_TEST, async () => {
  const { daemon, fake } = await start();
  const status = await waitFor(async () => {
    const s = await ping();
    return s?.connected ? s : null;
  }, 'connected pong');
  assert.equal(status.lastError, null);
  assert.equal(status.connection, 'connected');
  await daemon.stop();
  await daemon.done;
  assert.equal(fake.disconnectCalls, 1);
  const after = await request({ type: 'ping' }, { timeoutMs: 500 });
  assert.equal(after.ok, false);
  if (!after.ok) assert.equal(after.code, 3);
});

test('IPC is up before Feishu is: two failed connects, ask is refused with the last error, then it recovers', PER_TEST, async () => {
  let attempt = 0;
  const { daemon } = await start({
    connect: async () => {
      attempt += 1;
      if (attempt <= 2) throw new Error(`handshake ${attempt} refused`);
    },
  });
  const early = await waitFor(async () => {
    const s = await ping();
    return s && s.lastError ? s : null;
  }, 'first failed handshake to be recorded');
  assert.equal(early.connected, false);
  assert.match(early.lastError ?? '', /handshake 1 refused/);
  assert.equal(early.connection, 'connecting');

  await request({ type: 'bind', root: '/p', label: 'p', paneId: null, chatId: 'oc_x' });
  const refused = await request({ type: 'ask', root: '/p', label: 'p', paneId: null, payload: askPayload, timeoutMs: 1000 });
  assert.equal(refused.ok, false);
  if (!refused.ok) {
    assert.equal(refused.code, 3);
    assert.match(refused.message, /handshake/);
  }

  const later = await waitFor(async () => {
    const s = await ping();
    return s?.connected ? s : null;
  }, 'the third connect attempt to succeed');
  assert.equal(later.lastError, null);
  assert.equal(attempt, 3);
  await daemon.stop();
});

test('stop() returns promptly while connect keeps failing', PER_TEST, async () => {
  const { daemon, fake } = await start({
    connect: async () => {
      throw new Error('never');
    },
  });
  await waitFor(() => fake.connectCalls >= 2, 'a retry');
  const t0 = Date.now();
  await daemon.stop();
  await daemon.done;
  assert.ok(Date.now() - t0 < 1000, 'stop took too long');
  const attempts = fake.connectCalls;
  await sleep(200);
  assert.equal(fake.connectCalls, attempts, 'still retrying after stop');
});

test('stop() during a long retry back-off leaves no timer behind', PER_TEST, async () => {
  const timeouts = () => process.getActiveResourcesInfo().filter((r) => r === 'Timeout').length;
  const before = timeouts();
  const { daemon, fake } = await start(
    {
      connect: async () => {
        throw new Error('never');
      },
    },
    30_000,
  );
  await waitFor(() => fake.connectCalls >= 1, 'the first failed connect');
  await daemon.stop();
  let resolved = false;
  void daemon.done.then(() => (resolved = true));
  await sleep(0);
  assert.equal(resolved, true, 'done did not resolve');
  assert.equal(timeouts(), before, 'a retry timer is still pending after stop()');
});

test('an IPC stop request is acked first, then the daemon winds down and done resolves', PER_TEST, async () => {
  const { daemon } = await start();
  const ack = await request({ type: 'stop' }, { timeoutMs: 1000 });
  assert.deepEqual(ack, { ok: true, kind: 'ack' });
  await daemon.done;
  const after = await request({ type: 'ping' }, { timeoutMs: 500 });
  assert.equal(after.ok, false);
});

test('bind --chat, ask, human types: the reply comes back and the card is rewritten', PER_TEST, async () => {
  const { daemon, fake } = await start();
  await waitFor(async () => (await ping())?.connected, 'connected');
  const bound = await request({ type: 'bind', root: '/p', label: 'p', paneId: null, chatId: 'oc_x' });
  assert.equal(bound.ok && bound.kind, 'bind');
  const notes: string[] = [];
  const asking = request(
    { type: 'ask', root: '/p', label: 'p', paneId: null, payload: askPayload, timeoutMs: 5000 },
    { onNote: (t) => notes.push(t) },
  );
  await waitFor(() => fake.sent.length === 1, 'the question card to be sent');
  const second = await request({ type: 'ask', root: '/p', label: 'p', paneId: null, payload: askPayload, timeoutMs: 1000 });
  assert.equal(second.ok, false);
  if (!second.ok) assert.equal(second.code, 4);
  await fake.message({ chatId: 'oc_x', content: 'neither, wait for me' });
  assert.deepEqual(await asking, { ok: true, kind: 'ask', reply: 'neither, wait for me', via: 'text' });
  assert.equal(notes.length, 1);
  const last = await waitFor(() => {
    const l = fake.sent.at(-1);
    return l && 'update' in l ? l : null;
  }, 'the answered card rewrite');
  assert.equal(last.update, 'om_1');
  await daemon.stop();
});

test('stop() with a question still pending: the asker gets code 3, the card is cancelled, no connection is force-closed', PER_TEST, async () => {
  const { daemon, fake } = await start();
  await waitFor(async () => (await ping())?.connected, 'connected');
  await request({ type: 'bind', root: '/p', label: 'p', paneId: null, chatId: 'oc_x' });
  const asking = request({ type: 'ask', root: '/p', label: 'p', paneId: null, payload: askPayload, timeoutMs: 60_000 });
  await waitFor(() => fake.sent.length === 1, 'the question card to be sent');
  const t0 = Date.now();
  await daemon.stop();
  const elapsed = Date.now() - t0;
  const res = await asking;
  assert.equal(res.ok, false);
  if (!res.ok) {
    assert.equal(res.code, 3);
    assert.equal(res.message, msg.askCancelledStop);
  }
  const last = fake.sent.at(-1);
  assert.ok(last && 'update' in last, 'card was not rewritten');
  assert.match(JSON.stringify(last.card), /grey/);
  assert.ok(elapsed < 1500, `stop took ${elapsed} ms, the close grace period must not have been needed`);
  const log = readFileSync(join(homes.at(-1)!, 'daemon.log'), 'utf8');
  assert.match(log, /daemon\.stopped/);
  assert.doesNotMatch(log, /close-forced/);
});

test('a dropped connection is reported as reconnecting until the SDK reconnects', PER_TEST, async () => {
  const { daemon, fake } = await start();
  await waitFor(async () => (await ping())?.connected, 'connected');
  await request({ type: 'bind', root: '/p', label: 'p', paneId: null, chatId: 'oc_x' });
  fake.handlers.reconnecting?.();
  const dropped = await ping();
  assert.equal(dropped?.connected, false);
  assert.equal(dropped?.lastError, msg.reconnecting);
  const refused = await request({ type: 'ask', root: '/p', label: 'p', paneId: null, payload: askPayload, timeoutMs: 1000 });
  assert.equal(refused.ok, false);
  if (!refused.ok) assert.match(refused.message, new RegExp(msg.reconnecting));
  fake.handlers.reconnected?.();
  const back = await ping();
  assert.equal(back?.connected, true);
  assert.equal(back?.lastError, null);
  await daemon.stop();
});

test('a second daemon on the same home is refused with code 3', PER_TEST, async () => {
  const { daemon } = await start();
  await assert.rejects(
    runDaemon({ createChannel: () => createFakeChannel().channel, herdr: createFakeHerdr().deps }),
    (err: unknown) => err instanceof DaemonStartError && err.code === 3 && err.message === msg.daemonAlready,
  );
  await daemon.stop();
});
