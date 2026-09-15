// The daemon in-process, with the Feishu channel and herdr replaced by fakes.
import { after, test, type TestContext } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, lstatSync, lutimesSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, symlinkSync, truncateSync, utimesSync, writeFileSync } from 'node:fs';
import { platform, tmpdir } from 'node:os';
import { dirname, join, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

import { createFakeChannel, pageOf, type FakeChannelOptions } from './fixtures/fake-channel.js';
import { createFakeHerdr } from './fixtures/fake-herdr.js';
import { homeOfSockBytes } from './fixtures/long-home.js';

process.env.AGENT_LARK_APP_ID = 'cli_fake';
process.env.AGENT_LARK_APP_SECRET = 'fake-secret';

const { runDaemon, DaemonStartError } = await import('../src/daemon.js');
const { request } = await import('../src/ipc.js');
const { fill, msg, t } = await import('../src/texts.js');
const { readProjectState, writeProjectState, SOCK_PATH_LIMIT, sockPathProblem } = await import('../src/paths.js');

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

async function start(channelOpts: FakeChannelOptions = {}, retryMs = 50, opts: { owner?: string; pollMs?: number } = {}) {
  const home = freshHome();
  // The owner is read from the environment when the daemon starts; each test
  // says whether one is known.
  if (opts.owner) process.env.AGENT_LARK_OWNER_OPEN_ID = opts.owner;
  else delete process.env.AGENT_LARK_OWNER_OPEN_ID;
  const fake = createFakeChannel(channelOpts);
  const herdr = createFakeHerdr();
  const daemon = await runDaemon({ createChannel: () => fake.channel, herdr: herdr.deps, connectRetryMs: retryMs, pollMs: opts.pollMs });
  daemons.push(daemon);
  return { fake, herdr, daemon, home };
}
const connected = async () => waitFor(async () => (await ping())?.connected, 'connected');
const lastAllowlist = (fake: { policy: unknown[] }): string[] =>
  (fake.policy.at(-1) as { groupAllowlist?: string[] } | undefined)?.groupAllowlist ?? [];
async function bindings(home: string): Promise<Array<Record<string, unknown>>> {
  return (JSON.parse(readFileSync(join(home, 'bindings.json'), 'utf8')) as { bindings: Array<Record<string, unknown>> }).bindings;
}
const bindChat = (root: string, chatId: string) => request({ type: 'bind', root, label: 'p', paneId: null, chatId });

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

// ---- group lifecycle ------------------------------------------------------

const MARKER = 'agent-lark · /p';

test('bind with a live group keeps it; with a name it renames the group to "<task> [<dir>]"', PER_TEST, async () => {
  const { daemon, fake } = await start({ chatUpdate: async () => ({ code: 0 }) });
  await connected();
  await bindChat('/p', 'oc_x');
  const again = await request({ type: 'bind', root: '/p', label: 'p', paneId: 'w1:p1', name: '发版准备' });
  assert.deepEqual(again, { ok: true, kind: 'bind', chatId: 'oc_x', how: 'existing', name: '发版准备 [p]' });
  // adopting by --chat wrote the description; keeping the live group only renames
  assert.deepEqual(fake.renames, [
    { chatId: 'oc_x', name: undefined, description: MARKER },
    { chatId: 'oc_x', name: '发版准备 [p]', description: undefined },
  ]);
  const list = await request({ type: 'list' });
  assert.ok(list.ok && list.kind === 'list');
  assert.deepEqual(list.bindings, [
    { root: '/p', label: 'p', chatId: 'oc_x', name: '发版准备 [p]', paneId: 'w1:p1', away: false, releasedAt: null },
  ]);
  await daemon.stop();
});

test('bind with a live group: a rename the bot is not allowed to make still binds, with a note carrying the Feishu error', PER_TEST, async () => {
  const { daemon, fake } = await start({ chatUpdate: async () => ({ code: 232002, msg: 'owner only' }) });
  await connected();
  await bindChat('/p', 'oc_x');
  const notes: string[] = [];
  const res = await request({ type: 'bind', root: '/p', label: 'p', paneId: null, name: 'new task' }, { onNote: (t) => notes.push(t) });
  assert.ok(res.ok && res.kind === 'bind');
  assert.equal(res.how, 'existing');
  assert.equal(notes.length, 1);
  assert.match(notes[0]!, /232002/);
  assert.match(notes[0]!, /owner only/);
  assert.equal(notes[0]!.split('renaming the group failed').length - 1, 1, `phrase repeated: ${notes[0]}`);
  assert.deepEqual(fake.renames.at(-1), { chatId: 'oc_x', name: 'new task [p]', description: undefined });
  await daemon.stop();
});

async function seedReleasedAndRemote(chatUpdate: FakeChannelOptions['chatUpdate'] = async () => ({ code: 0 }), owner?: string) {
  const infoCalls: string[] = [];
  const created: unknown[] = [];
  const ctx = await start(
    {
      chatUpdate,
      chatList: pageOf(['oc_old', 'oc_remote', 'oc_other']),
      getChatInfo: async (id) => {
        infoCalls.push(id);
        return { chatId: id, chatType: 'group', name: id === 'oc_remote' ? 'remote task [p]' : 'someone else', description: id === 'oc_remote' ? MARKER : 'not ours' };
      },
      createChat: async (opts) => {
        created.push(opts);
        return { chatId: 'oc_new' };
      },
    },
    50,
    { owner },
  );
  await connected();
  await bindChat('/p', 'oc_old');
  const gone = await request({ type: 'unbind', root: '/p' });
  assert.ok(gone.ok, JSON.stringify(gone));
  infoCalls.length = 0;
  return { ...ctx, infoCalls, created };
}

test('no live group, one released locally and one marked group in Feishu: bind without a mode is code 4 with both candidates', PER_TEST, async () => {
  const { daemon, infoCalls } = await seedReleasedAndRemote();
  const res = await request({ type: 'bind', root: '/p', label: 'p', paneId: null });
  assert.equal(res.ok, false);
  if (res.ok) return;
  assert.equal(res.code, 4);
  assert.ok(res.candidates, 'no candidates on the refusal');
  assert.equal(res.candidates.length, 2);
  const [local, remote] = res.candidates;
  assert.equal(local?.chatId, 'oc_old');
  assert.match(local?.releasedAt ?? '', /^\d{4}-/);
  assert.deepEqual(remote, { chatId: 'oc_remote', name: 'remote task [p]', releasedAt: null });
  // groups with a local record are not fetched again; unrelated groups are looked at and dropped
  assert.deepEqual(infoCalls.sort(), ['oc_other', 'oc_remote']);
  assert.equal((await ping())?.bindings, 0);
  await daemon.stop();
});

test('bind --reuse brings the released entry back live and renames it; a chat id outside the candidates is code 1', PER_TEST, async () => {
  const { daemon, fake, home } = await seedReleasedAndRemote();
  const bad = await request({ type: 'bind', root: '/p', label: 'p', paneId: null, mode: 'reuse', reuseChatId: 'oc_other' });
  assert.equal(bad.ok, false);
  if (!bad.ok) assert.equal(bad.code, 1);
  const res = await request({ type: 'bind', root: '/p', label: 'p', paneId: 'w1:p2', mode: 'reuse', reuseChatId: 'oc_old', name: 'second run' });
  assert.deepEqual(res, { ok: true, kind: 'bind', chatId: 'oc_old', how: 'reused', name: 'second run [p]' });
  // taking a group back also points its description at this project again
  assert.deepEqual(fake.renames.at(-1), { chatId: 'oc_old', name: 'second run [p]', description: MARKER });
  assert.deepEqual(lastAllowlist(fake), ['oc_old']);
  const live = (await bindings(home)).find((b) => b.chatId === 'oc_old');
  assert.equal(live?.releasedAt, null);
  assert.equal(live?.paneId, 'w1:p2');
  assert.equal(live?.name, 'second run [p]');
  await daemon.stop();
});

test('bind --reuse of a group known only to Feishu records it locally as the live entry', PER_TEST, async () => {
  const { daemon, fake, home } = await seedReleasedAndRemote();
  const res = await request({ type: 'bind', root: '/p', label: 'p', paneId: null, mode: 'reuse', reuseChatId: 'oc_remote' });
  assert.deepEqual(res, { ok: true, kind: 'bind', chatId: 'oc_remote', how: 'reused', name: '[p]' });
  assert.deepEqual(fake.renames.at(-1), { chatId: 'oc_remote', name: '[p]', description: MARKER });
  const all = await bindings(home);
  assert.deepEqual(all.map((b) => [b.chatId, b.releasedAt === null]).sort(), [
    ['oc_old', false],
    ['oc_remote', true],
  ]);
  await daemon.stop();
});

test('bind --new creates a group carrying the marker even though candidates exist', PER_TEST, async () => {
  const { daemon, fake, created } = await seedReleasedAndRemote(undefined, 'ou_owner');
  const updatesBefore = fake.renames.length;
  const res = await request({ type: 'bind', root: '/p', label: 'p', paneId: null, mode: 'new', name: 'fresh' });
  assert.deepEqual(res, { ok: true, kind: 'bind', chatId: 'oc_new', how: 'created', name: 'fresh [p]' });
  assert.deepEqual(created, [{ name: 'fresh [p]', description: MARKER, inviteUserIds: ['ou_owner'], userIdType: 'open_id' }]);
  assert.deepEqual(lastAllowlist(fake), ['oc_new']);
  assert.equal(fake.renames.length, updatesBefore, 'a freshly created group needs no update call');
  await daemon.stop();
});

test('creating a group with no owner recorded is code 4', PER_TEST, async () => {
  const { daemon } = await start({ chatList: pageOf([]) });
  await connected();
  const res = await request({ type: 'bind', root: '/p', label: 'p', paneId: null });
  assert.equal(res.ok, false);
  if (!res.ok) assert.equal(res.code, 4);
  await daemon.stop();
});

test('bind --chat while another group is live releases the old one; a group live for another project is refused', PER_TEST, async () => {
  const { daemon, fake, home } = await start({
    getChatInfo: async (id) => ({ chatId: id, chatType: 'group', name: id === 'oc_2' ? 'their group' : undefined }),
    chatUpdate: async () => ({ code: 0 }),
  });
  await connected();
  await bindChat('/p', 'oc_1');
  const res = await bindChat('/p', 'oc_2');
  assert.deepEqual(res, { ok: true, kind: 'bind', chatId: 'oc_2', how: 'chat', name: 'their group' });
  // the adopted group's description now points at this project; no name was asked for
  assert.deepEqual(fake.renames.at(-1), { chatId: 'oc_2', name: undefined, description: MARKER });
  const all = await bindings(home);
  const old = all.find((b) => b.chatId === 'oc_1');
  assert.match(String(old?.releasedAt), /^\d{4}-/);
  assert.equal(all.find((b) => b.chatId === 'oc_2')?.releasedAt, null);
  assert.deepEqual(lastAllowlist(fake), ['oc_2']);
  const taken = await bindChat('/q', 'oc_2');
  assert.equal(taken.ok, false);
  if (!taken.ok) assert.equal(taken.code, 1);
  await daemon.stop();
});

test('unbind: code 4 while a question is pending; then the group leaves the allowlist, away is off, a second unbind is code 1', PER_TEST, async () => {
  const { daemon, fake, home } = await start();
  await connected();
  await bindChat('/p', 'oc_x');
  await request({ type: 'setAway', root: '/p', away: true, paneId: 'w1:p1' });
  const asking = request({ type: 'ask', root: '/p', label: 'p', paneId: null, payload: askPayload, timeoutMs: 5000 });
  await waitFor(() => fake.sent.length === 1, 'the question card to be sent');
  const refused = await request({ type: 'unbind', root: '/p' });
  assert.equal(refused.ok, false);
  if (!refused.ok) assert.equal(refused.code, 4);
  await fake.message({ chatId: 'oc_x', content: 'Keep' });
  await asking;
  const res = await request({ type: 'unbind', root: '/p' });
  assert.deepEqual(res, { ok: true, kind: 'unbind', chatId: 'oc_x', name: 'oc_x' });
  assert.deepEqual(lastAllowlist(fake), []);
  const entry = (await bindings(home)).find((b) => b.chatId === 'oc_x');
  assert.equal(entry?.away, false);
  assert.match(String(entry?.releasedAt), /^\d{4}-/);
  assert.equal((await ping())?.bindings, 0);
  const again = await request({ type: 'unbind', root: '/p' });
  assert.equal(again.ok, false);
  if (!again.ok) assert.equal(again.code, 1);
  await daemon.stop();
});

// ---- unbind --dissolve --------------------------------------------------------
const dissolve = (root = '/p') => request({ type: 'unbind', root, dissolve: true });

test('unbind --dissolve: Feishu dissolves the group, the record is forgotten, the allowlist and status are refreshed', PER_TEST, async () => {
  const { daemon, fake, home } = await start({ chatDelete: async () => ({ code: 0 }) });
  await connected();
  await bindChat('/p', 'oc_x');
  const res = await dissolve();
  assert.deepEqual(res, { ok: true, kind: 'unbind', chatId: 'oc_x', name: 'oc_x', dissolved: true });
  assert.deepEqual(fake.deletes, ['oc_x']);
  assert.deepEqual(await bindings(home), []);
  assert.deepEqual(lastAllowlist(fake), []);
  assert.equal((await ping())?.bindings, 0);
  assert.match(readFileSync(join(home, 'daemon.log'), 'utf8'), /"dissolved":true/);
  await daemon.stop();
});

test('unbind --dissolve refused by Feishu (code≠0): the record is still forgotten, and the reply carries the code, the scope hint and dissolved false', PER_TEST, async () => {
  const { daemon, fake, home } = await start({ chatDelete: async () => ({ code: 232002, msg: 'no permission' }) });
  await connected();
  await bindChat('/p', 'oc_x');
  const res = await dissolve();
  assert.equal(res.ok, true, JSON.stringify(res));
  if (!res.ok || res.kind !== 'unbind') return;
  assert.equal(res.dissolved, false);
  assert.match(res.problem ?? '', /232002/);
  assert.match(res.problem ?? '', /no permission/);
  assert.match(res.problem ?? '', /im:chat:operate_as_owner/);
  assert.match(res.problem ?? '', /by hand/);
  assert.deepEqual(fake.deletes, ['oc_x']);
  assert.deepEqual(await bindings(home), []);
  assert.deepEqual(lastAllowlist(fake), []);
  await daemon.stop();
});

test('unbind --dissolve refused: the group\'s marker is cleared so it is not offered back, and the reply and log say so', PER_TEST, async () => {
  const { daemon, fake, home } = await start({
    chatDelete: async () => ({ code: 232002, msg: 'no permission' }),
    chatUpdate: async () => ({ code: 0 }),
  });
  await connected();
  await bindChat('/p', 'oc_x');
  const res = await dissolve();
  assert.equal(res.ok, true, JSON.stringify(res));
  if (!res.ok || res.kind !== 'unbind') return;
  assert.equal(res.dissolved, false);
  assert.match(res.problem ?? '', /marker .*cleared/);
  assert.doesNotMatch(res.problem ?? '', /could not be cleared/);
  const last = fake.renames.at(-1);
  assert.equal(last?.chatId, 'oc_x');
  assert.equal(last?.name, undefined);
  assert.ok(last?.description && last.description !== 'agent-lark · /p', JSON.stringify(last));
  assert.match(readFileSync(join(home, 'daemon.log'), 'utf8'), /dissolve\.marker-cleared/);
  assert.deepEqual(await bindings(home), []);
  await daemon.stop();
});

test('unbind --dissolve refused and the marker cannot be cleared either: the reply says the group will be offered back, the log says why', PER_TEST, async () => {
  const { daemon, fake, home } = await start({
    chatDelete: async () => ({ code: 232002, msg: 'no permission' }),
    chatUpdate: async () => ({ code: 232016, msg: 'not allowed' }),
  });
  await connected();
  await bindChat('/p', 'oc_x');
  const before = fake.renames.length;
  const res = await dissolve();
  assert.equal(res.ok, true, JSON.stringify(res));
  if (!res.ok || res.kind !== 'unbind') return;
  assert.equal(res.dissolved, false);
  assert.match(res.problem ?? '', /could not be cleared/);
  assert.match(res.problem ?? '', /232016/);
  assert.match(res.problem ?? '', /offered back/);
  assert.equal(fake.renames.length, before + 1);
  assert.equal(fake.renames.at(-1)?.description, 'released by agent-lark');
  assert.match(readFileSync(join(home, 'daemon.log'), 'utf8'), /dissolve\.marker-failed/);
  assert.deepEqual(await bindings(home), []);
  await daemon.stop();
});

test('unbind --dissolve when the SDK call throws: the record is still forgotten, the reply says what was thrown', PER_TEST, async () => {
  const { daemon, home } = await start({
    chatDelete: async () => {
      throw new Error('socket hang up');
    },
  });
  await connected();
  await bindChat('/p', 'oc_x');
  const res = await dissolve();
  assert.equal(res.ok, true, JSON.stringify(res));
  if (!res.ok || res.kind !== 'unbind') return;
  assert.equal(res.dissolved, false);
  assert.match(res.problem ?? '', /socket hang up/);
  assert.deepEqual(await bindings(home), []);
  await daemon.stop();
});

test('unbind --dissolve before Feishu is connected is code 3 and touches nothing; a plain unbind still works then', PER_TEST, async () => {
  const { daemon, home } = await start({
    connect: async () => {
      throw new Error('offline');
    },
    chatDelete: async () => ({ code: 0 }),
  });
  await waitFor(async () => (await ping())?.lastError, 'the failed handshake');
  await bindChat('/p', 'oc_x');
  const res = await dissolve();
  assert.equal(res.ok, false);
  if (!res.ok) assert.equal(res.code, 3);
  assert.equal((await bindings(home)).find((b) => b.chatId === 'oc_x')?.releasedAt, null);
  const plain = await request({ type: 'unbind', root: '/p' });
  assert.ok(plain.ok);
  await daemon.stop();
});

test('unbind --dissolve while a question is pending is code 4, nothing dissolved; with no live group it is code 1', PER_TEST, async () => {
  const { daemon, fake, home } = await start({ chatDelete: async () => ({ code: 0 }) });
  await connected();
  await bindChat('/p', 'oc_x');
  const asking = request({ type: 'ask', root: '/p', label: 'p', paneId: null, payload: askPayload, timeoutMs: 5000 });
  await waitFor(() => fake.sent.length === 1, 'the question card to be sent');
  const refused = await dissolve();
  assert.equal(refused.ok, false);
  if (!refused.ok) assert.equal(refused.code, 4);
  assert.deepEqual(fake.deletes, []);
  assert.equal((await bindings(home)).length, 1);
  await fake.message({ chatId: 'oc_x', content: 'Keep' });
  await asking;
  const none = await dissolve('/other');
  assert.equal(none.ok, false);
  if (!none.ok) assert.equal(none.code, 1);
  await daemon.stop();
});

test('rename: code 4 without a live group, code 3 carrying the Feishu error when refused, otherwise the name sticks', PER_TEST, async () => {
  let answer: { code?: number; msg?: string } = { code: 232002, msg: 'owner or admin only' };
  const { daemon, fake } = await start({ chatUpdate: async () => answer });
  await connected();
  const none = await request({ type: 'rename', root: '/p', paneId: null, name: 'x' });
  assert.equal(none.ok, false);
  if (!none.ok) assert.equal(none.code, 4);
  await bindChat('/p', 'oc_x');
  const denied = await request({ type: 'rename', root: '/p', paneId: null, name: 'x' });
  assert.equal(denied.ok, false);
  if (!denied.ok) {
    assert.equal(denied.code, 3);
    assert.match(denied.message, /232002/);
    assert.match(denied.message, /owner or admin only/);
  }
  const long = await request({ type: 'rename', root: '/p', paneId: null, name: 'x'.repeat(61) });
  assert.equal(long.ok, false);
  if (!long.ok) assert.equal(long.code, 1);
  answer = { code: 0 };
  const ok = await request({ type: 'rename', root: '/p', paneId: 'w1:p3', name: 'x' });
  assert.deepEqual(ok, { ok: true, kind: 'rename', name: 'x [p]' });
  assert.deepEqual(fake.renames.at(-1), { chatId: 'oc_x', name: 'x [p]', description: undefined });
  const list = await request({ type: 'list' });
  assert.ok(list.ok && list.kind === 'list');
  assert.equal(list.bindings[0]?.name, 'x [p]');
  assert.equal(list.bindings[0]?.paneId, 'w1:p3');
  await daemon.stop();
});

test('rename: a thrown SDK error is code 3 with whatever the error says', PER_TEST, async () => {
  const { daemon } = await start({
    chatUpdate: async () => {
      throw Object.assign(new Error('Request failed with status code 400'), { response: { data: { code: 232011, msg: 'bot not in chat' } } });
    },
  });
  await connected();
  await bindChat('/p', 'oc_x');
  const res = await request({ type: 'rename', root: '/p', paneId: null, name: 'x' });
  assert.equal(res.ok, false);
  if (!res.ok) {
    assert.equal(res.code, 3);
    assert.match(res.message, /232011/);
    assert.match(res.message, /bot not in chat/);
  }
  await daemon.stop();
});

test('before Feishu is connected a bind that has to look at Feishu is code 3; bind --chat goes through', PER_TEST, async () => {
  const { daemon } = await start({
    connect: async () => {
      throw new Error('offline');
    },
  });
  await waitFor(async () => (await ping())?.lastError, 'the failed handshake');
  const scan = await request({ type: 'bind', root: '/p', label: 'p', paneId: null });
  assert.equal(scan.ok, false);
  if (!scan.ok) {
    assert.equal(scan.code, 3);
    assert.match(scan.message, /offline/);
  }
  const direct = await bindChat('/p', 'oc_x');
  assert.ok(direct.ok);
  await daemon.stop();
});

test('bind: the group scan forgets released records whose group is gone, so only groups that exist are offered back', PER_TEST, async () => {
  const { daemon, home } = await start({
    chatList: pageOf(['oc_remote', 'oc_other']),
    getChatInfo: async (id) => ({ chatId: id, chatType: 'group', description: id === 'oc_remote' ? MARKER : 'not ours' }),
  });
  await connected();
  await bindChat('/p', 'oc_old');
  await request({ type: 'unbind', root: '/p' });
  await bindChat('/p', 'oc_gone');
  await request({ type: 'unbind', root: '/p' });
  const res = await request({ type: 'bind', root: '/p', label: 'p', paneId: null });
  assert.equal(res.ok, false);
  if (!res.ok) {
    assert.equal(res.code, 4);
    assert.deepEqual(res.candidates?.map((c) => c.chatId), ['oc_remote']);
  }
  assert.deepEqual(await chatIds(home), []);
  assert.match(logLines(home, 'bindings.swept').at(-1) ?? '', /"removed":2/);
  await daemon.stop();
});

test('a failed group scan is a note, not a refusal: local candidates still count', PER_TEST, async () => {
  const { daemon } = await start({
    chatList: async () => {
      throw new Error('scope missing');
    },
  });
  await connected();
  await bindChat('/p', 'oc_old');
  await request({ type: 'unbind', root: '/p' });
  const notes: string[] = [];
  const res = await request({ type: 'bind', root: '/p', label: 'p', paneId: null }, { onNote: (t) => notes.push(t) });
  assert.equal(res.ok, false);
  if (!res.ok) {
    assert.equal(res.code, 4);
    assert.deepEqual(res.candidates?.map((c) => c.chatId), ['oc_old']);
  }
  assert.equal(notes.length, 1);
  assert.match(notes[0]!, /scope missing/);
  await daemon.stop();
});

test('ask and notify remember the payload language on the live binding', PER_TEST, async () => {
  const { daemon, fake, home } = await start();
  await connected();
  await bindChat('/p', 'oc_x');
  await request({ type: 'notify', root: '/p', label: 'p', paneId: null, payload: { title: 't', body: 'b', lang: 'en' } });
  assert.equal((await bindings(home))[0]?.lang, 'en');
  const asking = request({ type: 'ask', root: '/p', label: 'p', paneId: null, payload: { ...askPayload, lang: 'zh' }, timeoutMs: 5000 });
  await waitFor(() => fake.sent.length === 2, 'the question card');
  await fake.message({ chatId: 'oc_x', content: 'Keep' });
  await asking;
  assert.equal((await bindings(home))[0]?.lang, 'zh');
  await daemon.stop();
});

// ---- send-file: the daemon's own gate ---------------------------------------
// The CLI resolves the path before asking (see the cli tests); what is
// checked here is what the daemon lets through for whatever path a request
// names. Everything under tmpdir() is allowed, so a target outside the
// allowlist has to live elsewhere: this package's own checkout.
const outsideFile = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', 'package.json');
const MB = 1024 * 1024;
const rx = (s: string): string => s.replace(/[\\^$.*+?()[\]{}|]/g, '\\$&');
/** An empty file grown to `bytes` without writing them (sparse where the file system allows). */
function sparse(path: string, bytes: number): string {
  writeFileSync(path, '');
  truncateSync(path, bytes);
  return path;
}
async function sendFileSetup(t: TestContext) {
  const { fake, daemon, home } = await start();
  t.after(() => daemon.stop());
  await connected();
  const project = realpathSync(mkdtempSync(join(tmpdir(), 'al-send-')));
  homes.push(project);
  await bindChat(project, 'oc_x');
  const send = (path: string) => request({ type: 'sendFile', root: project, label: 'p', paneId: null, path });
  return { fake, home, project, send };
}
const refusal = (res: Awaited<ReturnType<typeof request>>): string => {
  assert.equal(res.ok, false, JSON.stringify(res));
  return res.ok ? '' : `${res.code} ${res.message}`;
};

test('send-file: a regular file inside the project is sent as a file under its base name', PER_TEST, async (t) => {
  const { fake, project, send } = await sendFileSetup(t);
  writeFileSync(join(project, 'note.txt'), 'hello');
  const res = await send(join(project, 'note.txt'));
  assert.equal(res.ok, true, JSON.stringify(res));
  const last = fake.sent.at(-1) as { chatId: string; input: { file: { source: Buffer; fileName: string } } };
  assert.equal(last.chatId, 'oc_x');
  assert.equal(last.input.file.fileName, 'note.txt');
  assert.equal(last.input.file.source.toString(), 'hello');
});

test('send-file: a relative path is taken from the daemon\'s own working directory', PER_TEST, async (t) => {
  const { fake } = await sendFileSetup(t);
  const cwd = realpathSync(process.cwd());
  assert.ok(existsSync(join(cwd, 'package.json')), `no package.json in ${cwd}; the runner starts the tests from the package root`);
  await bindChat(cwd, 'oc_cwd');
  const res = await request({ type: 'sendFile', root: cwd, label: 'p', paneId: null, path: 'package.json' });
  assert.equal(res.ok, true, JSON.stringify(res));
  assert.equal((fake.sent.at(-1) as { input: { file: { fileName: string } } }).input.file.fileName, 'package.json');
});

test('send-file: a symlink inside the project pointing outside the allowlist is refused, naming the allowed directories', PER_TEST, async (t) => {
  const { home, project, send } = await sendFileSetup(t);
  const target = realpathSync(outsideFile);
  if (target.startsWith(realpathSync(tmpdir()) + sep)) {
    t.skip(`this checkout is under tmpdir(), which is always allowed: ${target}`);
    return;
  }
  try {
    symlinkSync(outsideFile, join(project, 'link.json'));
  } catch (err) {
    t.skip(`cannot create symlinks here: ${String(err)}`);
    return;
  }
  const why = refusal(await send(join(project, 'link.json')));
  assert.match(why, new RegExp(`^1 refusing to send ${rx(target)}\n`));
  assert.match(why, new RegExp(`^  this project ${rx(project)}$`, 'm'));
  assert.match(why, new RegExp(`^  ${rx(join(home, 'media'))}$`, 'm'));
  assert.match(why, new RegExp(`^  ${rx(tmpdir())}$`, 'm'));
});

test('send-file: a directory is refused as not a regular file', PER_TEST, async (t) => {
  const { project, send } = await sendFileSetup(t);
  mkdirSync(join(project, 'dir'));
  assert.equal(refusal(await send(join(project, 'dir'))), `1 not a regular file: ${join(project, 'dir')}`);
});

test('send-file: an image one byte over 10 MB is refused', PER_TEST, async (t) => {
  const { project, send } = await sendFileSetup(t);
  assert.equal(refusal(await send(sparse(join(project, 'big.png'), 10 * MB + 1))), '1 file too large: 10.0 MB, limit 10 MB');
});

test('send-file: an image of exactly 10 MB is sent as an image', PER_TEST, async (t) => {
  const { fake, project, send } = await sendFileSetup(t);
  const res = await send(sparse(join(project, 'cap.png'), 10 * MB));
  assert.equal(res.ok, true, JSON.stringify(res));
  const last = fake.sent.at(-1) as { input: { image?: { source: Buffer } } };
  assert.equal(last.input.image?.source.length, 10 * MB);
});

test('send-file: any other file one byte over 30 MB is refused', PER_TEST, async (t) => {
  const { project, send } = await sendFileSetup(t);
  assert.equal(refusal(await send(sparse(join(project, 'big.bin'), 30 * MB + 1))), '1 file too large: 30.0 MB, limit 30 MB');
});

test('send-file: a path that does not exist is refused as file not found', PER_TEST, async (t) => {
  const { project, send } = await sendFileSetup(t);
  assert.equal(refusal(await send(join(project, 'nope.txt'))), `1 file not found: ${join(project, 'nope.txt')}`);
});

test('a message in a released group is ignored; the stuck-alert poll only watches live away bindings', PER_TEST, async () => {
  const { daemon, fake, herdr } = await start();
  await connected();
  await bindChat('/p', 'oc_old');
  await request({ type: 'setAway', root: '/p', away: true, paneId: 'w1:p1' });
  await request({ type: 'unbind', root: '/p' });
  herdr.agents = [{ agent: 'claude', agent_status: 'blocked', cwd: '/p', pane_id: 'w1:p1', focused: true }];
  await fake.message({ chatId: 'oc_old', content: 'hello?' });
  assert.equal(herdr.prompts.length, 0);
  assert.equal(fake.sent.length, 0);
  await daemon.stop();
});

test('a bindings.json with two live entries for one project is repaired at start and the repair is logged', PER_TEST, async () => {
  const home = freshHome();
  const entry = (chatId: string, boundAt: string) => ({ root: '/p', label: 'p', chatId, name: null, paneId: null, away: false, lang: null, boundAt, releasedAt: null });
  writeFileSync(join(home, 'bindings.json'), JSON.stringify({ bindings: [entry('oc_older', '2026-01-01T00:00:00.000Z'), entry('oc_newer', '2026-01-05T00:00:00.000Z')] }));
  const daemon = await runDaemon({ createChannel: () => createFakeChannel().channel, herdr: createFakeHerdr().deps, connectRetryMs: 50 });
  daemons.push(daemon);
  const list = await request({ type: 'list' });
  assert.ok(list.ok && list.kind === 'list');
  assert.deepEqual(list.bindings.map((b) => [b.chatId, b.releasedAt === null]).sort(), [
    ['oc_newer', true],
    ['oc_older', false],
  ]);
  const log = readFileSync(join(home, 'daemon.log'), 'utf8');
  const line = log.split('\n').find((l) => l.includes('bindings.repaired'));
  assert.ok(line, 'no bindings.repaired line in the log');
  assert.match(line, /"chatId":"oc_older"/);
  assert.match(line, /"root":"\/p"/);
  await daemon.stop();
});

test('bind --chat while a question is pending on the live group is refused with code 4', PER_TEST, async () => {
  const { daemon, fake } = await start({ chatUpdate: async () => ({ code: 0 }) });
  await connected();
  await bindChat('/p', 'oc_1');
  const asking = request({ type: 'ask', root: '/p', label: 'p', paneId: null, payload: askPayload, timeoutMs: 5000 });
  await waitFor(() => fake.sent.length === 1, 'the question card to be sent');
  const refused = await bindChat('/p', 'oc_2');
  assert.equal(refused.ok, false);
  if (!refused.ok) {
    assert.equal(refused.code, 4);
    assert.equal(refused.message, msg.unbindPending);
  }
  // re-pointing at the same live group is not a switch and goes through
  const same = await bindChat('/p', 'oc_1');
  assert.ok(same.ok);
  await fake.message({ chatId: 'oc_1', content: 'Keep' });
  await asking;
  const after = await bindChat('/p', 'oc_2');
  assert.ok(after.ok);
  await daemon.stop();
});

test('bind --chat --name renames the adopted group in the same update as its description; while disconnected both are skipped with a note', PER_TEST, async () => {
  let attempt = 0;
  const { daemon, fake } = await start({
    chatUpdate: async () => ({ code: 0 }),
    connect: async () => {
      attempt += 1;
      if (attempt <= 2) throw new Error('offline');
    },
  });
  await waitFor(async () => (await ping())?.lastError, 'the failed handshake');
  const notes: string[] = [];
  const offline = await request({ type: 'bind', root: '/p', label: 'p', paneId: null, chatId: 'oc_x', name: 'wanted' }, { onNote: (t) => notes.push(t) });
  assert.deepEqual(offline, { ok: true, kind: 'bind', chatId: 'oc_x', how: 'chat', name: 'oc_x' });
  assert.equal(fake.renames.length, 0);
  assert.equal(notes.length, 1);
  await connected();
  const online = await request({ type: 'bind', root: '/p', label: 'p', paneId: null, chatId: 'oc_x', name: 'wanted' }, { onNote: (t) => notes.push(t) });
  assert.deepEqual(online, { ok: true, kind: 'bind', chatId: 'oc_x', how: 'chat', name: 'wanted [p]' });
  assert.deepEqual(fake.renames, [{ chatId: 'oc_x', name: 'wanted [p]', description: MARKER }]);
  assert.equal(notes.length, 1);
  await daemon.stop();
});

test('with a live group, --reuse / --new are ignored with a note', PER_TEST, async () => {
  const { daemon } = await start();
  await connected();
  await bindChat('/p', 'oc_x');
  const notes: string[] = [];
  const res = await request({ type: 'bind', root: '/p', label: 'p', paneId: null, mode: 'new' }, { onNote: (t) => notes.push(t) });
  assert.ok(res.ok && res.kind === 'bind' && res.how === 'existing');
  assert.equal(notes.length, 1);
  assert.match(notes[0]!, /ignored/);
  await daemon.stop();
});

test('setAway false with no live group is a no-op ack; setAway true still needs a group', PER_TEST, async () => {
  const { daemon } = await start();
  await connected();
  assert.deepEqual(await request({ type: 'setAway', root: '/p', away: false, paneId: null }), { ok: true, kind: 'ack' });
  const on = await request({ type: 'setAway', root: '/p', away: true, paneId: null });
  assert.equal(on.ok, false);
  if (!on.ok) assert.equal(on.code, 4);
  await daemon.stop();
});

test('a state directory whose socket path is over the limit stops the daemon with code 4 and the path sentence, before credentials are looked at', { ...PER_TEST, skip: platform() === 'win32' ? 'named pipes have no sun_path' : false }, async () => {
  const long = homeOfSockBytes(SOCK_PATH_LIMIT + 1, 'al-daemon-long-');
  homes.push(dirname(long));
  process.env.AGENT_LARK_HOME = long;
  const creds = { id: process.env.AGENT_LARK_APP_ID, secret: process.env.AGENT_LARK_APP_SECRET };
  delete process.env.AGENT_LARK_APP_ID;
  delete process.env.AGENT_LARK_APP_SECRET;
  try {
    await assert.rejects(
      runDaemon({ createChannel: () => createFakeChannel().channel, herdr: createFakeHerdr().deps, connectRetryMs: 50 }),
      (err: unknown) => err instanceof DaemonStartError && err.code === 4 && err.message === sockPathProblem() && /over this platform's limit/.test(err.message),
    );
    assert.equal(existsSync(join(long, 'daemon.pid')), false);
  } finally {
    process.env.AGENT_LARK_APP_ID = creds.id;
    process.env.AGENT_LARK_APP_SECRET = creds.secret;
  }
});

test('a damaged bindings.json stops the daemon from starting with code 4 and the file path', PER_TEST, async () => {
  const home = freshHome();
  writeFileSync(join(home, 'bindings.json'), '{"bindings": [');
  await assert.rejects(
    runDaemon({ createChannel: () => createFakeChannel().channel, herdr: createFakeHerdr().deps }),
    (err: unknown) => err instanceof DaemonStartError && err.code === 4 && err.message.includes(join(home, 'bindings.json')),
  );
  assert.equal(readFileSync(join(home, 'bindings.json'), 'utf8'), '{"bindings": [');
});

// ---- cards: multi-choice form, urgent flag, reactions, card language ---------

const multiPayload = {
  ...askPayload,
  select: 'multi',
  recommend: ['a'],
  options: [
    { id: 'a', label: 'Keep', consequence: 'c' },
    { id: 'b', label: 'Drop', consequence: 'c' },
    { id: 'c', label: 'Wipe', consequence: 'c', danger: true },
  ],
};
type Card = { header: { template: string; title: { content: string } }; body: { elements: Array<Record<string, unknown>> } };
const sentCard = (fake: { sent: Array<{ chatId?: string; input?: unknown; update?: string; card?: object }> }, i: number): Card =>
  (fake.sent[i] as { input: { card: Card } }).input.card;
// What Feishu sends back: the form values keyed by checker name, i.e. `opt:<id>`.
const formSubmit = (reqId: string, ticks: Record<string, unknown>, chatId = 'oc_x', attempt = 0) => ({
  messageId: 'om_1',
  chatId,
  operator: { openId: 'ou_human' },
  action: {
    value: { reqId, attempt },
    tag: 'button',
    name: 'submit',
    formValue: Object.fromEntries(Object.entries(ticks).map(([id, v]) => [`opt:${id}`, v])),
  },
});
const submitValue = (card: Card): { reqId: string; attempt: number } => {
  const form = card.body.elements.find((e) => e.tag === 'form') as { elements: Array<Record<string, unknown>> };
  return (form.elements.at(-1) as { behaviors: Array<{ value: { reqId: string; attempt: number } }> }).behaviors[0]!.value;
};
const reqIdOf = (card: Card): string => {
  const form = card.body.elements.find((e) => e.tag === 'form') as { elements: Array<Record<string, unknown>> };
  const submit = form.elements.at(-1) as { behaviors: Array<{ value: { reqId: string } }> };
  return submit.behaviors[0]!.value.reqId;
};

test('multi-choice: the form is sent, a submit with two ticked comes back as "Keep、Drop" via form, the answered card rides the callback', PER_TEST, async () => {
  const { daemon, fake } = await start();
  await connected();
  await bindChat('/p', 'oc_x');
  const asking = request({ type: 'ask', root: '/p', label: 'p', paneId: null, payload: multiPayload, timeoutMs: 5000 });
  await waitFor(() => fake.sent.length === 1, 'the question card to be sent');
  const card = sentCard(fake, 0);
  assert.equal(card.header.template, 'blue');
  const reqId = reqIdOf(card);
  const res = (await fake.cardAction(formSubmit(reqId, { a: true, b: 'true', c: false }))) as { toast: { type: string }; card: { type: string; data: Card } };
  assert.equal(res.toast.type, 'success');
  assert.equal(res.card.type, 'raw');
  assert.equal(res.card.data.header.template, 'green');
  assert.deepEqual(await asking, { ok: true, kind: 'ask', reply: 'Keep、Drop', via: 'form' });
  const last = await waitFor(() => {
    const l = fake.sent.at(-1);
    return l && 'update' in l ? l : null;
  }, 'the answered card rewrite');
  assert.equal(last.update, 'om_1');
  assert.equal((last.card as Card).header.template, 'green');
  await daemon.stop();
});

test('multi-choice: an empty submit is an error toast and the question stays open; a later submit still answers it', PER_TEST, async () => {
  const { daemon, fake } = await start();
  await connected();
  await bindChat('/p', 'oc_x');
  const asking = request({ type: 'ask', root: '/p', label: 'p', paneId: null, payload: multiPayload, timeoutMs: 5000 });
  await waitFor(() => fake.sent.length === 1, 'the question card to be sent');
  const reqId = reqIdOf(sentCard(fake, 0));
  // The SDK drops a second identical action on the same card for 12 h, so the
  // refusal rewrites the card with a new submit value (attempt + 1).
  const empty = (await fake.cardAction(formSubmit(reqId, { a: false, b: 'false', c: 0 }))) as {
    toast: { type: string; content: string };
    card?: { type: string; data: Card };
  };
  assert.equal(empty.toast.type, 'error');
  // multiPayload carries no lang: the toast, like the card, falls back to English.
  assert.equal(empty.toast.content, t('en').pickAtLeastOne);
  assert.equal(empty.card?.type, 'raw');
  assert.equal(empty.card?.data.header.template, 'blue');
  assert.deepEqual(submitValue(empty.card!.data), { reqId, attempt: 1 });
  assert.equal((await ping())?.pendingAsks, 1);
  const again = await request({ type: 'ask', root: '/p', label: 'p', paneId: null, payload: multiPayload, timeoutMs: 1000 });
  assert.equal(again.ok, false);
  const twice = (await fake.cardAction(formSubmit(reqId, {}, 'oc_x', 1))) as { card?: { data: Card } };
  assert.deepEqual(submitValue(twice.card!.data), { reqId, attempt: 2 });
  await fake.cardAction(formSubmit(reqId, { c: true }, 'oc_x', 2));
  assert.deepEqual(await asking, { ok: true, kind: 'ask', reply: 'Wipe', via: 'form' });
  await daemon.stop();
});

test('multi-choice: a submit carrying an older attempt still answers the open question (attempts only defeat the SDK dedup)', PER_TEST, async () => {
  const { daemon, fake } = await start();
  await connected();
  await bindChat('/p', 'oc_x');
  const asking = request({ type: 'ask', root: '/p', label: 'p', paneId: null, payload: multiPayload, timeoutMs: 5000 });
  await waitFor(() => fake.sent.length === 1, 'the question card to be sent');
  const reqId = reqIdOf(sentCard(fake, 0));
  await fake.cardAction(formSubmit(reqId, {}));
  const stale = (await fake.cardAction(formSubmit(reqId, { b: true }, 'oc_x', 0))) as { toast: { type: string } };
  assert.equal(stale.toast.type, 'success');
  assert.deepEqual(await asking, { ok: true, kind: 'ask', reply: 'Drop', via: 'form' });
  await daemon.stop();
});

test('multi-choice: a submit after the question closed is injected as a follow-up with the labels', PER_TEST, async () => {
  const { daemon, fake, herdr } = await start();
  await connected();
  await request({ type: 'bind', root: '/p', label: 'p', paneId: 'w1:p1', chatId: 'oc_x' });
  const asking = request({ type: 'ask', root: '/p', label: 'p', paneId: 'w1:p1', payload: multiPayload, timeoutMs: 5000 });
  await waitFor(() => fake.sent.length === 1, 'the question card to be sent');
  const reqId = reqIdOf(sentCard(fake, 0));
  await fake.message({ chatId: 'oc_x', content: 'wait' });
  await asking;
  const late = (await fake.cardAction(formSubmit(reqId, { b: true, c: true }))) as { toast: { type: string } };
  assert.equal(late.toast.type, 'info');
  await waitFor(() => herdr.prompts.length === 1, 'the follow-up injection');
  assert.equal(herdr.prompts[0]!.paneId, 'w1:p1');
  assert.equal(herdr.prompts[0]!.text, `[agent-lark remote] ${fill(msg.latePick, { labels: 'Drop、Wipe' })}`);
  await daemon.stop();
});

test('multi-choice: typing in the group answers with the whole text', PER_TEST, async () => {
  const { daemon, fake } = await start();
  await connected();
  await bindChat('/p', 'oc_x');
  const asking = request({ type: 'ask', root: '/p', label: 'p', paneId: null, payload: multiPayload, timeoutMs: 5000 });
  await waitFor(() => fake.sent.length === 1, 'the question card to be sent');
  await fake.message({ chatId: 'oc_x', content: 'Keep, and archive the rest' });
  assert.deepEqual(await asking, { ok: true, kind: 'ask', reply: 'Keep, and archive the rest', via: 'text' });
  await daemon.stop();
});

test('ask --urgent: the card is red and the owner is flagged through urgentApp right after the card is sent', PER_TEST, async () => {
  const { daemon, fake, home } = await start({ urgentApp: async () => ({ code: 0, data: { invalid_user_id_list: [] } }) }, 50, { owner: 'ou_owner' });
  await connected();
  await bindChat('/p', 'oc_x');
  const notes: string[] = [];
  const asking = request({ type: 'ask', root: '/p', label: 'p', paneId: null, payload: askPayload, timeoutMs: 5000, urgent: true }, { onNote: (n) => notes.push(n) });
  await waitFor(() => fake.urgents.length === 1, 'the urgent flag');
  assert.equal(sentCard(fake, 0).header.template, 'red');
  assert.deepEqual(fake.urgents, [{ path: { message_id: 'om_1' }, params: { user_id_type: 'open_id' }, data: { user_id_list: ['ou_owner'] } }]);
  await fake.message({ chatId: 'oc_x', content: 'Keep' });
  assert.deepEqual(await asking, { ok: true, kind: 'ask', reply: 'Keep', via: 'text' });
  assert.equal(notes.filter((n) => /urgent/.test(n)).length, 0, notes.join('\n'));
  assert.match(readFileSync(join(home, 'daemon.log'), 'utf8'), /urgent\.sent/);
  await daemon.stop();
});

test('ask --urgent: a refused or thrown urgentApp, or no owner on record, is a note with the reason; the question goes on as usual', PER_TEST, async () => {
  let mode: 'code' | 'throw' = 'code';
  const { daemon, fake } = await start(
    {
      urgentApp: async () => {
        if (mode === 'throw') throw new Error('network down');
        return { code: 230024, msg: 'quota exceeded' };
      },
    },
    50,
    { owner: 'ou_owner' },
  );
  await connected();
  await bindChat('/p', 'oc_x');
  const ask = async (expect: RegExp) => {
    const notes: string[] = [];
    const asking = request({ type: 'ask', root: '/p', label: 'p', paneId: null, payload: askPayload, timeoutMs: 5000, urgent: true }, { onNote: (n) => notes.push(n) });
    await waitFor(() => notes.some((n) => /urgent/i.test(n)), 'the urgent note');
    assert.match(notes.find((n) => /urgent/i.test(n))!, expect);
    await fake.message({ chatId: 'oc_x', content: 'Keep' });
    assert.deepEqual(await asking, { ok: true, kind: 'ask', reply: 'Keep', via: 'text' });
  };
  await ask(/230024/);
  mode = 'throw';
  await ask(/network down/);
  await daemon.stop();
});

test('ask --urgent with no owner on record: note, no urgentApp call', PER_TEST, async () => {
  const { daemon, fake } = await start({ urgentApp: async () => ({ code: 0 }) });
  await connected();
  await bindChat('/p', 'oc_x');
  const notes: string[] = [];
  const asking = request({ type: 'ask', root: '/p', label: 'p', paneId: null, payload: askPayload, timeoutMs: 5000, urgent: true }, { onNote: (n) => notes.push(n) });
  await waitFor(() => notes.some((n) => /urgent/i.test(n)), 'the urgent note');
  assert.equal(fake.urgents.length, 0);
  await fake.message({ chatId: 'oc_x', content: 'Keep' });
  await asking;
  await daemon.stop();
});

test('a message injected into the pane gets a Get reaction; a reply to a question does not', PER_TEST, async () => {
  const { daemon, fake, herdr } = await start();
  await connected();
  await request({ type: 'bind', root: '/p', label: 'p', paneId: 'w1:p1', chatId: 'oc_x' });
  await fake.message({ chatId: 'oc_x', content: 'hello there', messageId: 'om_human_1' });
  await waitFor(() => fake.reactions.length === 1, 'the reaction');
  assert.deepEqual(fake.reactions, [{ messageId: 'om_human_1', emoji: 'Get' }]);
  assert.equal(herdr.prompts.length, 1);
  const asking = request({ type: 'ask', root: '/p', label: 'p', paneId: 'w1:p1', payload: askPayload, timeoutMs: 5000 });
  await waitFor(() => fake.sent.length === 1, 'the question card to be sent');
  await fake.message({ chatId: 'oc_x', content: 'Keep', messageId: 'om_human_2' });
  await asking;
  await sleep(50);
  assert.equal(fake.reactions.length, 1);
  await daemon.stop();
});

test('injection refused by herdr: no reaction, and the receipt card speaks the language the project last used', PER_TEST, async () => {
  const { daemon, fake, herdr } = await start();
  await connected();
  await request({ type: 'bind', root: '/p', label: 'p', paneId: 'w1:p1', chatId: 'oc_x' });
  await request({ type: 'notify', root: '/p', label: 'p', paneId: 'w1:p1', payload: { title: 't', body: 'b', lang: 'en' } });
  herdr.outcome = { ok: false, code: 'agent_blocked', message: 'busy' };
  await fake.message({ chatId: 'oc_x', content: 'hello there', messageId: 'om_human_1' });
  const receipt = await waitFor(() => {
    const l = fake.sent.at(-1);
    return l && 'input' in l && fake.sent.length === 2 ? l : null;
  }, 'the receipt card');
  const card = (receipt.input as { card: Card }).card;
  assert.equal(card.header.template, 'orange');
  assert.equal(card.header.title.content, '⚠️ [p] Not delivered');
  assert.match(String(card.body.elements[0]!.content), /stuck on a prompt only you can answer/);
  assert.equal(fake.reactions.length, 0);
  await daemon.stop();
});

test('a failing addReaction is only logged; the message is injected all the same', PER_TEST, async () => {
  const { daemon, fake, herdr, home } = await start({
    addReaction: async () => {
      throw new Error('231017 unsupported');
    },
  });
  await connected();
  await request({ type: 'bind', root: '/p', label: 'p', paneId: 'w1:p1', chatId: 'oc_x' });
  await fake.message({ chatId: 'oc_x', content: 'hello there', messageId: 'om_human_1' });
  await waitFor(() => /reaction\.failed/.test(readFileSync(join(home, 'daemon.log'), 'utf8')), 'the reaction failure in the log');
  assert.equal(herdr.prompts.length, 1);
  assert.match(herdr.prompts[0]!.text, /hello there/);
  assert.equal(fake.sent.length, 0, 'no receipt card for a reaction failure');
  await daemon.stop();
});

test('with no language on record the receipt card is English (the status card takes its language from the same place)', PER_TEST, async () => {
  const { daemon, fake, herdr } = await start();
  await connected();
  // no pane recorded and no agent in the project: nowhere to deliver
  await bindChat('/p', 'oc_x');
  await fake.message({ chatId: 'oc_x', content: 'anyone?', messageId: 'om_human_1' });
  const receipt = await waitFor(() => (fake.sent.length === 1 ? fake.sent[0] : null), 'the receipt card');
  const card = (receipt as { input: { card: Card } }).input.card;
  assert.equal(card.header.title.content, '⚠️ [p] Not delivered');
  assert.match(String(card.body.elements[0]!.content), /No herdr pane is recorded/);
  assert.equal(herdr.prompts.length, 0);
  await daemon.stop();
});

test('the stuck-on-a-prompt alert takes the language the project last used, English when it never said', PER_TEST, async () => {
  const { daemon, fake, herdr } = await start({}, 50, { pollMs: 20 });
  await connected();
  await request({ type: 'bind', root: '/p', label: 'p', paneId: 'w1:p1', chatId: 'oc_x' });
  await request({ type: 'setAway', root: '/p', away: true, paneId: 'w1:p1' });
  // the alert fires on a transition into blocked, so the pane is seen working first
  herdr.agents = [{ agent: 'claude', agent_status: 'working', cwd: '/p', pane_id: 'w1:p1', focused: true, terminal_title_stripped: 'deploy' }];
  await sleep(80);
  herdr.agents = [{ ...herdr.agents[0]!, agent_status: 'blocked' }];
  const alert = await waitFor(() => (fake.sent.length === 1 ? fake.sent[0] : null), 'the status card');
  const card = (alert as { input: { card: Card } }).input.card;
  assert.equal(card.header.template, 'orange');
  assert.equal(card.header.title.content, '🔔 [p] waiting for you');
  assert.equal(String(card.body.elements[0]!.content), '**deploy**\npane w1:p1');
  await daemon.stop();
});

test('the stuck-on-a-prompt alert in Chinese once the project asked in Chinese', PER_TEST, async () => {
  const { daemon, fake, herdr } = await start({}, 50, { pollMs: 20 });
  await connected();
  await request({ type: 'bind', root: '/p', label: 'p', paneId: 'w1:p1', chatId: 'oc_x' });
  await request({ type: 'notify', root: '/p', label: 'p', paneId: 'w1:p1', payload: { title: 't', body: 'b', lang: 'zh' } });
  await request({ type: 'setAway', root: '/p', away: true, paneId: 'w1:p1' });
  herdr.agents = [{ agent: 'claude', agent_status: 'working', cwd: '/p', pane_id: 'w1:p1', focused: true }];
  await sleep(80);
  herdr.agents = [{ ...herdr.agents[0]!, agent_status: 'blocked' }];
  const alert = await waitFor(() => (fake.sent.length === 2 ? fake.sent[1] : null), 'the status card');
  const card = (alert as { input: { card: Card } }).input.card;
  assert.equal(card.header.title.content, '🔔 [p] 等你输入');
  assert.equal(String(card.body.elements[0]!.content), '窗格 w1:p1');
  await daemon.stop();
});

// ---- review follow-ups: fire-and-forget paths, urgent timing, callback latency

const buttonTap = (reqId: string, optionId: string, chatId = 'oc_x') => ({
  messageId: 'om_1',
  chatId,
  operator: { openId: 'ou_human' },
  action: { value: { reqId, optionId }, tag: 'button' },
});
const buttonReqId = (card: Card): string => (card.body.elements.find((e) => e.tag === 'button') as { value: { reqId: string } }).value.reqId;

test('a promptPane that throws does not take the daemon down: logged as inject.failed, ping still answers', PER_TEST, async () => {
  const { daemon, fake, herdr, home } = await start();
  await connected();
  await request({ type: 'bind', root: '/p', label: 'p', paneId: 'w1:p1', chatId: 'oc_x' });
  herdr.deps.promptPane = async () => {
    throw new Error('herdr exploded');
  };
  await fake.message({ chatId: 'oc_x', content: 'hello?', messageId: 'om_human_1' });
  await waitFor(() => /inject\.failed/.test(readFileSync(join(home, 'daemon.log'), 'utf8')), 'inject.failed in the log');
  assert.match(readFileSync(join(home, 'daemon.log'), 'utf8'), /herdr exploded/);
  assert.ok((await ping())?.connected, 'daemon stopped answering');
  await daemon.stop();
});

test('ask --urgent: a tap that lands while the urgent flag is still in flight answers the question, it is not a late tap', PER_TEST, async () => {
  const { daemon, fake, herdr } = await start(
    { urgentApp: async () => { await sleep(300); return { code: 0 }; } },
    50,
    { owner: 'ou_owner' },
  );
  await connected();
  await request({ type: 'bind', root: '/p', label: 'p', paneId: 'w1:p1', chatId: 'oc_x' });
  const asking = request({ type: 'ask', root: '/p', label: 'p', paneId: 'w1:p1', payload: askPayload, timeoutMs: 5000, urgent: true });
  await waitFor(() => fake.sent.length === 1, 'the question card to be sent');
  const res = (await fake.cardAction(buttonTap(buttonReqId(sentCard(fake, 0)), 'b'))) as { toast: { type: string } };
  assert.equal(res.toast.type, 'success');
  assert.deepEqual(await asking, { ok: true, kind: 'ask', reply: 'Drop', via: 'button' });
  assert.equal(herdr.prompts.length, 0, 'the tap was treated as a late one and injected');
  await daemon.stop();
});

test('a card update that never returns does not hold the callback: button and form both answer within 200 ms, card attached', PER_TEST, async () => {
  const { daemon, fake } = await start({ updateCard: () => new Promise<void>(() => {}) });
  await connected();
  await bindChat('/p', 'oc_x');
  for (const [payload, tap] of [
    [askPayload, (card: Card) => buttonTap(buttonReqId(card), 'a')],
    [multiPayload, (card: Card) => formSubmit(reqIdOf(card), { a: true })],
  ] as const) {
    const asking = request({ type: 'ask', root: '/p', label: 'p', paneId: null, payload, timeoutMs: 5000 });
    const n = fake.sent.length;
    await waitFor(() => fake.sent.length === n + 1, 'the question card to be sent');
    const t0 = Date.now();
    const res = (await fake.cardAction(tap(sentCard(fake, n)))) as { card?: { type: string; data: Card } };
    const took = Date.now() - t0;
    assert.ok(took < 200, `callback took ${took} ms`);
    assert.equal(res.card?.type, 'raw');
    assert.equal(res.card?.data.header.template, 'green');
    assert.deepEqual(await asking, { ok: true, kind: 'ask', reply: 'Keep', via: payload === askPayload ? 'button' : 'form' });
  }
  await daemon.stop();
});

test('single-choice: a tap after the question closed is injected with the label, like a late form submit', PER_TEST, async () => {
  const { daemon, fake, herdr } = await start();
  await connected();
  await request({ type: 'bind', root: '/p', label: 'p', paneId: 'w1:p1', chatId: 'oc_x' });
  const asking = request({ type: 'ask', root: '/p', label: 'p', paneId: 'w1:p1', payload: askPayload, timeoutMs: 5000 });
  await waitFor(() => fake.sent.length === 1, 'the question card to be sent');
  const reqId = buttonReqId(sentCard(fake, 0));
  await fake.message({ chatId: 'oc_x', content: 'wait' });
  await asking;
  const late = (await fake.cardAction(buttonTap(reqId, 'b'))) as { toast: { type: string } };
  assert.equal(late.toast.type, 'info');
  await waitFor(() => herdr.prompts.length === 1, 'the follow-up injection');
  assert.equal(herdr.prompts[0]!.text, `[agent-lark remote] ${fill(msg.latePick, { labels: 'Drop' })}`);
  await daemon.stop();
});

test('attachments: the injected text names where each one was saved, before the closing note', PER_TEST, async () => {
  const downloads: string[] = [];
  const { daemon, fake, herdr, home } = await start({
    downloadResourceToFile: async (_messageId, _fileKey, _type, dest) => {
      downloads.push(dest);
      return { path: dest, bytes: 0 } as never;
    },
  });
  await connected();
  await request({ type: 'bind', root: '/p', label: 'p', paneId: 'w1:p1', chatId: 'oc_x' });
  await fake.message({
    chatId: 'oc_x',
    content: '![image](img_v3_x)',
    messageId: 'om_human_1',
    resources: [
      { type: 'image', fileKey: 'img_v3_x' },
      { type: 'file', fileKey: 'file_y', fileName: 'notes.txt' },
    ],
  });
  await waitFor(() => herdr.prompts.length === 1, 'the injection');
  assert.equal(downloads.length, 2);
  const text = herdr.prompts[0]!.text;
  const lines = text.split('\n');
  assert.equal(lines[0], '[agent-lark remote] ![image](img_v3_x)');
  assert.equal(lines[1], `[saved: ${downloads[0]}]`);
  assert.equal(lines[2], `[saved: ${downloads[1]}]`);
  assert.equal(lines[3], msg.injectFilesWithText);
  assert.equal(lines.length, 4);
  for (const d of downloads) assert.ok(d.startsWith(join(home, 'media')), d);
  assert.match(downloads[1]!, /notes\.txt$/);
  await daemon.stop();
});

// ---- voice: what the agent is told when Feishu will not transcribe ---------

/** A download that leaves a real (empty) file behind, so the transcriber has something to read. */
const downloadLeavingFile: FakeChannelOptions['downloadResourceToFile'] = async (_messageId, _fileKey, _type, dest) => {
  writeFileSync(dest, '');
  return { path: dest, bytes: 0 } as never;
};
const speechThat = (fileRecognize: () => Promise<unknown>) => ({ speech_to_text: { speech: { fileRecognize } } });
const voiceNote = { chatId: 'oc_x', content: '<audio file_key="file_v" />', messageId: 'om_human_1', resources: [{ type: 'audio' as const, fileKey: 'file_v' }] };

test('voice: when Feishu refuses the transcription, the injected note carries its code and msg and names both likely causes', PER_TEST, async () => {
  const refused = Object.assign(new Error('Request failed with status code 400'), {
    response: { status: 400, data: { code: 99991400, msg: 'request trigger frequency limit' } },
  });
  const { daemon, fake, herdr } = await start({ downloadResourceToFile: downloadLeavingFile, rawClient: speechThat(async () => { throw refused; }) });
  await connected();
  await request({ type: 'bind', root: '/p', label: 'p', paneId: 'w1:p1', chatId: 'oc_x' });
  await fake.message(voiceNote);
  await waitFor(() => herdr.prompts.length === 1, 'the injection');
  const lines = herdr.prompts[0]!.text.split('\n');
  assert.equal(lines[0], `[agent-lark remote] ${fill(msg.injectUnheard, { n: 1, code: 99991400, msg: 'request trigger frequency limit' })}`);
  assert.match(lines[0]!, /99991400/);
  assert.match(lines[0]!, /request trigger frequency limit/);
  assert.match(lines[0]!, /speech_to_text:speech/);
  assert.match(lines[0]!, /free/i);
  assert.match(lines[1]!, /^\[saved: .+\]$/);
  assert.equal(lines[2], msg.injectFilesWithText);
  assert.equal(lines.length, 3);
  await daemon.stop();
});

test('voice: a transcription failure with no Feishu body in it is reported with code and msg "unknown"', PER_TEST, async () => {
  const { daemon, fake, herdr } = await start({ downloadResourceToFile: downloadLeavingFile, rawClient: speechThat(async () => { throw new Error('socket hang up'); }) });
  await connected();
  await request({ type: 'bind', root: '/p', label: 'p', paneId: 'w1:p1', chatId: 'oc_x' });
  await fake.message(voiceNote);
  await waitFor(() => herdr.prompts.length === 1, 'the injection');
  const lines = herdr.prompts[0]!.text.split('\n');
  assert.equal(lines[0], `[agent-lark remote] ${fill(msg.injectUnheard, { n: 1, code: 'unknown', msg: 'unknown' })}`);
  assert.match(lines[0]!, /unknown/);
  assert.doesNotMatch(lines[0]!, /socket hang up/);
  assert.equal(lines.length, 3);
  await daemon.stop();
});

test('voice: a transcription that recognised nothing says so, without blaming the scope or the plan', PER_TEST, async () => {
  const { daemon, fake, herdr } = await start({ downloadResourceToFile: downloadLeavingFile, rawClient: speechThat(async () => ({ data: { recognition_text: '  ' } })) });
  await connected();
  await request({ type: 'bind', root: '/p', label: 'p', paneId: 'w1:p1', chatId: 'oc_x' });
  await fake.message(voiceNote);
  await waitFor(() => herdr.prompts.length === 1, 'the injection');
  const lines = herdr.prompts[0]!.text.split('\n');
  assert.equal(lines[0], `[agent-lark remote] ${fill(msg.injectNothingHeard, { n: 1 })}`);
  assert.match(lines[0]!, /recogni[sz]ed/);
  assert.doesNotMatch(lines[0]!, /speech_to_text:speech|free plan|code/);
  assert.match(lines[1]!, /^\[saved: .+\]$/);
  assert.equal(lines[2], msg.injectFilesWithText);
  assert.equal(lines.length, 3);
  await daemon.stop();
});

test('voice: a successful transcription replaces the audio placeholder with the transcript', PER_TEST, async () => {
  const { daemon, fake, herdr } = await start({ downloadResourceToFile: downloadLeavingFile, rawClient: speechThat(async () => ({ data: { recognition_text: ' ship it ' } })) });
  await connected();
  await request({ type: 'bind', root: '/p', label: 'p', paneId: 'w1:p1', chatId: 'oc_x' });
  await fake.message(voiceNote);
  await waitFor(() => herdr.prompts.length === 1, 'the injection');
  const lines = herdr.prompts[0]!.text.split('\n');
  assert.equal(lines[0], '[agent-lark remote] ship it');
  assert.match(lines[1]!, /^\[saved: .+\]$/);
  assert.equal(lines[2], msg.injectFilesWithText);
  assert.equal(lines.length, 3);
  await daemon.stop();
});

// ---- quoting a card: which one the human is replying to --------------------

test('a message quoting an answered question card is injected with "(reply to: …)" on top; rootId works as the quote too', PER_TEST, async () => {
  const { daemon, fake, herdr } = await start();
  await connected();
  await request({ type: 'bind', root: '/p', label: 'p', paneId: 'w1:p1', chatId: 'oc_x' });
  const asking = request({ type: 'ask', root: '/p', label: 'p', paneId: 'w1:p1', payload: askPayload, timeoutMs: 5000 });
  await waitFor(() => fake.sent.length === 1, 'the question card to be sent');
  await fake.message({ chatId: 'oc_x', content: 'Keep' });
  await asking;
  await fake.message({ chatId: 'oc_x', content: 'actually, one more thing', replyToMessageId: 'om_1' });
  await waitFor(() => herdr.prompts.length === 1, 'the injection');
  assert.equal(herdr.prompts[0]!.text, `[agent-lark remote] ${fill(msg.replyTo, { title: 't' })}\nactually, one more thing`);
  await fake.message({ chatId: 'oc_x', content: 'and this', rootId: 'om_1' });
  await waitFor(() => herdr.prompts.length === 2, 'the second injection');
  assert.equal(herdr.prompts[1]!.text, `[agent-lark remote] ${fill(msg.replyTo, { title: 't' })}\nand this`);
  await daemon.stop();
});

test('a message quoting something the daemon never sent, or quoting nothing, is injected as is', PER_TEST, async () => {
  const { daemon, fake, herdr } = await start();
  await connected();
  await request({ type: 'bind', root: '/p', label: 'p', paneId: 'w1:p1', chatId: 'oc_x' });
  await request({ type: 'notify', root: '/p', label: 'p', paneId: 'w1:p1', payload: { title: 'Tests green', body: 'b' } });
  await fake.message({ chatId: 'oc_x', content: 'who said that?', replyToMessageId: 'om_someone_elses' });
  await fake.message({ chatId: 'oc_x', content: 'plain' });
  await waitFor(() => herdr.prompts.length === 2, 'both injections');
  assert.deepEqual(
    herdr.prompts.map((p) => p.text),
    ['[agent-lark remote] who said that?', '[agent-lark remote] plain'],
  );
  await daemon.stop();
});

test('while a question is pending, a quoted message is still that question\'s answer and is not injected', PER_TEST, async () => {
  const { daemon, fake, herdr } = await start();
  await connected();
  await request({ type: 'bind', root: '/p', label: 'p', paneId: 'w1:p1', chatId: 'oc_x' });
  await request({ type: 'notify', root: '/p', label: 'p', paneId: 'w1:p1', payload: { title: 'Tests green', body: 'b' } });
  const asking = request({ type: 'ask', root: '/p', label: 'p', paneId: 'w1:p1', payload: askPayload, timeoutMs: 5000 });
  await waitFor(() => fake.sent.length === 2, 'the question card to be sent');
  await fake.message({ chatId: 'oc_x', content: 'Drop, and about that notify: fine', replyToMessageId: 'om_1' });
  assert.deepEqual(await asking, { ok: true, kind: 'ask', reply: 'Drop, and about that notify: fine', via: 'text' });
  await sleep(50);
  assert.equal(herdr.prompts.length, 0);
  await daemon.stop();
});

test('only the last 200 cards are remembered for quoting; the oldest are forgotten first', PER_TEST, async () => {
  const { daemon, fake, herdr } = await start();
  await connected();
  await request({ type: 'bind', root: '/p', label: 'p', paneId: 'w1:p1', chatId: 'oc_x' });
  for (let i = 1; i <= 201; i++) {
    const res = await request({ type: 'notify', root: '/p', label: 'p', paneId: 'w1:p1', payload: { title: `n${i}`, body: 'b' } });
    assert.ok(res.ok, `notify ${i}`);
  }
  assert.equal(fake.sent.length, 201);
  await fake.message({ chatId: 'oc_x', content: 'first', replyToMessageId: 'om_1' });
  await fake.message({ chatId: 'oc_x', content: 'second', replyToMessageId: 'om_2' });
  await fake.message({ chatId: 'oc_x', content: 'last', replyToMessageId: 'om_201' });
  await waitFor(() => herdr.prompts.length === 3, 'the injections');
  assert.deepEqual(
    herdr.prompts.map((p) => p.text),
    [
      '[agent-lark remote] first',
      `[agent-lark remote] ${fill(msg.replyTo, { title: 'n2' })}\nsecond`,
      `[agent-lark remote] ${fill(msg.replyTo, { title: 'n201' })}\nlast`,
    ],
  );
  await daemon.stop();
});

// ---- media directory sweep ---------------------------------------------------

const DAY = 24 * 60 * 60 * 1000;
/** Three files under <home>/media (8 days old, 1 day old, fresh) plus an empty subdirectory. */
function seedMedia(home: string): { old: string; recent: string; fresh: string; empty: string } {
  const dir = join(home, 'media', 'abc123');
  mkdirSync(dir, { recursive: true });
  const at = (name: string, ageMs: number): string => {
    const f = join(dir, name);
    writeFileSync(f, 'x'.repeat(1024));
    const t = (Date.now() - ageMs) / 1000;
    utimesSync(f, t, t);
    return f;
  };
  const old = at('old.png', 8 * DAY);
  const recent = at('recent.png', 1 * DAY);
  const fresh = at('fresh.png', 0);
  const empty = join(home, 'media', 'empty-dir');
  mkdirSync(empty, { recursive: true });
  return { old, recent, fresh, empty };
}
const swept = (home: string): string | undefined =>
  readFileSync(join(home, 'daemon.log'), 'utf8')
    .split('\n')
    .find((l) => l.includes('media.swept'));
async function withTtl<T>(value: string | undefined, fn: () => Promise<T>): Promise<T> {
  const prev = process.env.AGENT_LARK_MEDIA_TTL_DAYS;
  if (value === undefined) delete process.env.AGENT_LARK_MEDIA_TTL_DAYS;
  else process.env.AGENT_LARK_MEDIA_TTL_DAYS = value;
  try {
    return await fn();
  } finally {
    if (prev === undefined) delete process.env.AGENT_LARK_MEDIA_TTL_DAYS;
    else process.env.AGENT_LARK_MEDIA_TTL_DAYS = prev;
  }
}

// ---- bindings sweep: records whose group is gone from Feishu -----------------
const logLines = (home: string, event: string): string[] =>
  readFileSync(join(home, 'daemon.log'), 'utf8')
    .split('\n')
    .filter((l) => l.includes(` ${event} `));
const sweptLine = (home: string): string | undefined => logLines(home, 'bindings.swept').at(-1);
const chatIds = async (home: string): Promise<string[]> => (await bindings(home)).map((b) => String(b.chatId)).sort();
/** A daemon sweeping every 30 ms, with the given `chat.list` answers; stopped when the test ends, however it ends. */
async function sweeper(t: TestContext, chatList: FakeChannelOptions['chatList'], extra: FakeChannelOptions = {}) {
  const home = freshHome();
  const fake = createFakeChannel({ chatList, ...extra });
  const daemon = await runDaemon({ createChannel: () => fake.channel, herdr: createFakeHerdr().deps, connectRetryMs: 50, sweepMs: 30 });
  daemons.push(daemon);
  t.after(() => daemon.stop());
  return { home, fake, daemon };
}

// The 30 ms timer may tick while a test is still binding: the list holds every
// id until the setup is done, then drops the ones the test wants gone.
test('sweep: a released record whose group Feishu no longer lists is removed; a listed one stays', PER_TEST, async (t) => {
  let ids = ['oc_gone', 'oc_keep'];
  const { home } = await sweeper(t, (req) => pageOf(ids)(req));
  await connected();
  await bindChat('/p', 'oc_gone');
  await request({ type: 'unbind', root: '/p' });
  await bindChat('/q', 'oc_keep');
  ids = ['oc_keep'];
  const line = await waitFor(() => logLines(home, 'bindings.swept').find((l) => l.includes('"removed":1')), 'a sweep that removed one');
  assert.match(line, /"kept":1/);
  assert.match(line, /"roots":\["\/p"\]/);
  assert.deepEqual(await chatIds(home), ['oc_keep']);
});

test('sweep: a live record whose group is gone: removed, allowlist refreshed, the project state loses its chatId but keeps away', PER_TEST, async (t) => {
  let ids = ['oc_live'];
  const { home, fake } = await sweeper(t, (req) => pageOf(ids)(req));
  await connected();
  const project = realpathSync(mkdtempSync(join(tmpdir(), 'al-sweep-proj-')));
  homes.push(project);
  writeProjectState(project, { away: true, chatId: 'oc_live' }, { create: true });
  await bindChat(project, 'oc_live');
  await request({ type: 'setAway', root: project, away: true, paneId: 'w1:p1' });
  assert.deepEqual(lastAllowlist(fake), ['oc_live']);
  ids = [];
  await waitFor(() => logLines(home, 'bindings.swept').find((l) => l.includes('"removed":1')), 'the sweep');
  assert.deepEqual(await chatIds(home), []);
  assert.deepEqual(lastAllowlist(fake), []);
  assert.deepEqual(readProjectState(project), { away: true, chatId: null, target: project, updated: readProjectState(project)?.updated ?? '' });
  assert.equal((await ping())?.bindings, 0);
});

test('sweep: a project that never had a state file does not get one planted by the sweep', PER_TEST, async (t) => {
  const { home } = await sweeper(t, pageOf([]));
  await connected();
  const project = realpathSync(mkdtempSync(join(tmpdir(), 'al-sweep-proj-')));
  homes.push(project);
  await bindChat(project, 'oc_live');
  await waitFor(() => logLines(home, 'bindings.swept').find((l) => l.includes('"removed":1')), 'the sweep');
  assert.equal(existsSync(join(project, '.agent-lark')), false);
});

for (const [what, chatList, reason] of [
  ['a page answered with code≠0', async () => ({ code: 99991400, msg: 'too many requests' }), /99991400/],
  ['has_more without a page_token', async () => ({ code: 0, data: { items: [{ chat_id: 'oc_x' }], has_more: true } }), /page_token/],
  [
    'a throwing chat.list',
    async () => {
      throw new Error('scope missing');
    },
    /scope missing/,
  ],
  ['code 0 with no data at all', async () => ({ code: 0 }), /data/],
] as Array<[string, FakeChannelOptions['chatList'], RegExp]>) {
  test(`sweep: ${what} removes nothing and logs bindings.sweep-skipped with why`, PER_TEST, async (t) => {
    const { home } = await sweeper(t, chatList);
    await connected();
    await bindChat('/p', 'oc_gone');
    await request({ type: 'unbind', root: '/p' });
    await bindChat('/q', 'oc_live');
    const line = await waitFor(() => logLines(home, 'bindings.sweep-skipped').at(-1), 'the skipped line');
    assert.match(line, reason);
    await sleep(100);
    assert.deepEqual(await chatIds(home), ['oc_gone', 'oc_live']);
    assert.equal(sweptLine(home), undefined);
  });
}

test('sweep: a list that keeps saying has_more past 100 pages is not trusted: nothing removed, 100 pages asked for', PER_TEST, async (t) => {
  let endless = false;
  const { home, fake } = await sweeper(t, async (req) =>
    endless
      ? { code: 0, data: { items: [{ chat_id: 'oc_live' }], has_more: true, page_token: String(Number(req.params.page_token ?? 0) + 1) } }
      : pageOf(['oc_gone'])(req),
  );
  await connected();
  await bindChat('/p', 'oc_gone');
  await request({ type: 'unbind', root: '/p' });
  const before = fake.listCalls.length;
  endless = true;
  const line = await waitFor(() => logLines(home, 'bindings.sweep-skipped').at(-1), 'the skipped line');
  assert.match(line, /100 pages/);
  // One endless fetch is exactly 100 pages, tokens 0…99; a further tick can only add whole fetches.
  const tokens = fake.listCalls.slice(before, before + 100).map((c) => c.params.page_token);
  assert.deepEqual(tokens, [undefined, ...Array.from({ length: 99 }, (_, i) => String(i + 1))]);
  assert.equal((fake.listCalls.length - before) % 100, 0);
  assert.deepEqual(await chatIds(home), ['oc_gone']);
});

test('sweep: while Feishu is not connected nothing is removed and the reason is logged', PER_TEST, async (t) => {
  const { home } = await sweeper(t, pageOf([]), {
    connect: async () => {
      throw new Error('offline');
    },
  });
  await waitFor(async () => (await ping())?.lastError, 'the failed handshake');
  await bindChat('/p', 'oc_live');
  const line = await waitFor(() => logLines(home, 'bindings.sweep-skipped').at(-1), 'the skipped line');
  assert.match(line, /not connected/);
  assert.deepEqual(await chatIds(home), ['oc_live']);
});

test('sweep: a live record whose project has a question pending is kept this round and logged; its released records and the others go', PER_TEST, async (t) => {
  let ids = ['oc_prev', 'oc_asking', 'oc_idle'];
  const { home, fake } = await sweeper(t, (req) => pageOf(ids)(req));
  await connected();
  await bindChat('/p', 'oc_prev');
  await request({ type: 'unbind', root: '/p' });
  await bindChat('/p', 'oc_asking');
  await bindChat('/q', 'oc_idle');
  const asking = request({ type: 'ask', root: '/p', label: 'p', paneId: null, payload: askPayload, timeoutMs: 5000 });
  await waitFor(() => fake.sent.length === 1, 'the question card');
  ids = [];
  const line = await waitFor(() => logLines(home, 'bindings.swept').find((l) => l.includes('"removed":2')), 'the sweep');
  assert.match(line, /"kept":1/);
  assert.ok(logLines(home, 'bindings.sweep-pending').some((l) => l.includes('oc_asking')), 'the pending root was not logged');
  assert.ok(!logLines(home, 'bindings.sweep-pending').some((l) => l.includes('oc_prev')), 'the released record was shielded by the pending question');
  assert.deepEqual(await chatIds(home), ['oc_asking']);
  await fake.message({ chatId: 'oc_asking', content: 'Keep' });
  await asking;
});

test('sweep: a list spread over several pages is merged before anything is judged missing', PER_TEST, async (t) => {
  let ids = ['oc_a', 'oc_b', 'oc_c', 'oc_d'];
  const { home, fake } = await sweeper(t, (req) => pageOf(ids, 2)(req));
  await connected();
  for (const [root, id] of [['/a', 'oc_a'], ['/b', 'oc_b'], ['/c', 'oc_c'], ['/d', 'oc_d']] as const) await bindChat(root, id);
  const before = fake.listCalls.length;
  ids = ['oc_a', 'oc_b', 'oc_c'];
  await waitFor(() => logLines(home, 'bindings.swept').find((l) => l.includes('"removed":1')), 'the sweep');
  assert.deepEqual(await chatIds(home), ['oc_a', 'oc_b', 'oc_c']);
  const pages = fake.listCalls.slice(before).map((c) => c.params.page_token);
  assert.ok(pages.includes('2'), `the second page was never asked for: ${JSON.stringify(pages)}`);
});

test('sweep: with AGENT_LARK_MEDIA_TTL_DAYS=0 the bindings are still swept', PER_TEST, async (t) => {
  await withTtl('0', async () => {
    const { home } = await sweeper(t, pageOf([]));
    await connected();
    await bindChat('/p', 'oc_gone');
    await waitFor(() => logLines(home, 'bindings.swept').find((l) => l.includes('"removed":1')), 'the sweep');
    assert.deepEqual(await chatIds(home), []);
    assert.equal(swept(home), undefined);
  });
});

test('sweep: a record that went away while the list was being fetched is left alone, and so is the state written since', PER_TEST, async (t) => {
  const home = freshHome();
  const project = realpathSync(mkdtempSync(join(tmpdir(), 'al-sweep-proj-')));
  homes.push(project);
  writeProjectState(project, { away: true, chatId: 'oc_x' }, { create: true });
  writeFileSync(
    join(home, 'bindings.json'),
    JSON.stringify({
      bindings: [{ root: project, label: 'p', chatId: 'oc_x', name: null, paneId: null, away: true, lang: null, boundAt: '2026-01-01T00:00:00.000Z', releasedAt: null }],
    }),
  );
  let release!: () => void;
  const gate = new Promise<void>((r) => (release = r));
  const fake = createFakeChannel({
    chatList: async (req) => {
      if (fake.listCalls.length === 1) await gate;
      return pageOf([])(req);
    },
    chatDelete: async () => ({ code: 0 }),
  });
  const daemon = await runDaemon({ createChannel: () => fake.channel, herdr: createFakeHerdr().deps, connectRetryMs: 50 });
  daemons.push(daemon);
  t.after(() => daemon.stop());
  await connected();
  await waitFor(() => fake.listCalls.length >= 1, 'the post-handshake fetch to start');
  // While the list is on its way: the group is dissolved and a new one bound.
  const gone = await request({ type: 'unbind', root: project, dissolve: true });
  assert.ok(gone.ok, JSON.stringify(gone));
  await bindChat(project, 'oc_new');
  writeProjectState(project, { chatId: 'oc_new' });
  release();
  const line = await waitFor(() => sweptLine(home), 'the sweep to finish');
  assert.match(line, /"removed":0/);
  assert.deepEqual(await chatIds(home), ['oc_new']);
  assert.equal(readProjectState(project)?.chatId, 'oc_new');
  assert.deepEqual(lastAllowlist(fake), ['oc_new']);
});

test('sweep: the first successful handshake sweeps the records on file at once, before any timer', PER_TEST, async (t) => {
  const home = freshHome();
  writeFileSync(
    join(home, 'bindings.json'),
    JSON.stringify({
      bindings: [
        { root: '/p', label: 'p', chatId: 'oc_gone', name: null, paneId: null, away: false, lang: null, boundAt: '2026-01-01T00:00:00.000Z', releasedAt: '2026-01-02T00:00:00.000Z' },
        { root: '/q', label: 'q', chatId: 'oc_keep', name: null, paneId: null, away: false, lang: null, boundAt: '2026-01-01T00:00:00.000Z', releasedAt: null },
      ],
    }),
  );
  const fake = createFakeChannel({ chatList: pageOf(['oc_keep']) });
  const daemon = await runDaemon({ createChannel: () => fake.channel, herdr: createFakeHerdr().deps, connectRetryMs: 50 });
  daemons.push(daemon);
  await connected();
  const line = await waitFor(() => sweptLine(home), 'the sweep after the handshake');
  assert.match(line, /"removed":1/);
  assert.deepEqual(await chatIds(home), ['oc_keep']);
  await daemon.stop();
});

test('media sweep at start: files older than the default 7 days go, the rest and the log line stay; empty directories go too', PER_TEST, async () => {
  await withTtl(undefined, async () => {
    const home = freshHome();
    const m = seedMedia(home);
    const daemon = await runDaemon({ createChannel: () => createFakeChannel().channel, herdr: createFakeHerdr().deps, connectRetryMs: 50 });
    daemons.push(daemon);
    const line = await waitFor(() => swept(home), 'the media.swept log line');
    assert.match(line, /"removed":1/);
    assert.match(line, /"keptBytes":2048/);
    assert.equal(existsSync(m.old), false);
    assert.equal(existsSync(m.recent), true);
    assert.equal(existsSync(m.fresh), true);
    assert.equal(existsSync(m.empty), false);
    assert.equal(existsSync(join(home, 'media')), true, 'the media root itself must stay');
    const status = await waitFor(async () => (await ping())?.connected && (await ping()), 'pong');
    assert.match(status.media.at, /^\d{4}-\d{2}-\d{2}T/);
    assert.deepEqual({ ...status.media, at: '' }, { ttlDays: 7, files: 2, bytes: 2048, at: '' });
    // ping reports what the last sweep counted, it does not rescan
    writeFileSync(join(home, 'media', 'abc123', 'later.png'), 'y'.repeat(10));
    const again = await ping();
    assert.equal(again?.media.files, 2);
    assert.equal(again?.media.at, status.media.at);
    await daemon.stop();
  });
});

test('AGENT_LARK_MEDIA_TTL_DAYS=0: nothing is swept, and the status says so', PER_TEST, async () => {
  await withTtl('0', async () => {
    const home = freshHome();
    const m = seedMedia(home);
    const daemon = await runDaemon({ createChannel: () => createFakeChannel().channel, herdr: createFakeHerdr().deps, connectRetryMs: 50 });
    daemons.push(daemon);
    const status = await waitFor(async () => (await ping())?.connected && (await ping()), 'pong');
    assert.match(status.media.at, /^\d{4}-\d{2}-\d{2}T/);
    assert.deepEqual({ ...status.media, at: '' }, { ttlDays: 0, files: 3, bytes: 3072, at: '' });
    assert.equal(existsSync(m.old), true);
    assert.equal(existsSync(m.empty), true);
    assert.equal(swept(home), undefined);
    await daemon.stop();
  });
});

test('an unusable AGENT_LARK_MEDIA_TTL_DAYS falls back to 7 days with a warning on stderr and in the log', PER_TEST, async () => {
  await withTtl('abc', async () => {
    const home = freshHome();
    const m = seedMedia(home);
    const written: string[] = [];
    const realWrite = process.stderr.write.bind(process.stderr);
    process.stderr.write = ((chunk: string | Uint8Array) => {
      written.push(String(chunk));
      return true;
    }) as typeof process.stderr.write;
    let daemon: Daemon;
    try {
      daemon = await runDaemon({ createChannel: () => createFakeChannel().channel, herdr: createFakeHerdr().deps, connectRetryMs: 50 });
    } finally {
      process.stderr.write = realWrite;
    }
    daemons.push(daemon);
    assert.ok(written.some((w) => /AGENT_LARK_MEDIA_TTL_DAYS/.test(w) && /abc/.test(w)), written.join(''));
    await waitFor(() => swept(home), 'the media.swept log line');
    assert.equal(existsSync(m.old), false);
    assert.equal(existsSync(m.recent), true);
    assert.match(readFileSync(join(home, 'daemon.log'), 'utf8'), /media\.ttl-invalid/);
    assert.equal((await ping())?.media.ttlDays, 7);
    await daemon.stop();
  });
});

test('stop() clears the sweep timer: sweeps stop with the daemon, and no Timeout is left behind', PER_TEST, async () => {
  // getActiveResourcesInfo() does not list unref'd timers (measured), so the
  // proof is behavioural: with a 30 ms sweep interval, no sweep is logged
  // after stop().
  const timeouts = () => process.getActiveResourcesInfo().filter((r) => r === 'Timeout').length;
  const before = timeouts();
  const home = freshHome();
  const daemon = await runDaemon({ createChannel: () => createFakeChannel().channel, herdr: createFakeHerdr().deps, connectRetryMs: 50, sweepMs: 30 });
  daemons.push(daemon);
  const sweeps = () => readFileSync(join(home, 'daemon.log'), 'utf8').split('\n').filter((l) => l.includes('media.swept')).length;
  await waitFor(() => sweeps() >= 3, 'periodic sweeps');
  await daemon.stop();
  let resolved = false;
  void daemon.done.then(() => (resolved = true));
  await sleep(0);
  assert.equal(resolved, true, 'done did not resolve');
  const after = sweeps();
  await sleep(120);
  assert.equal(sweeps(), after, 'the sweep timer kept firing after stop()');
  assert.equal(timeouts(), before, 'a timer is still pending after stop()');
});

test('media sweep never follows a symlink: what it points at outside the state dir is untouched, the link itself goes by its own mtime', PER_TEST, async (t) => {
  await withTtl(undefined, async () => {
    const home = freshHome();
    const outside = mkdtempSync(join(tmpdir(), 'al-outside-'));
    homes.push(outside);
    const aged = (f: string): void => {
      const ts = (Date.now() - 8 * DAY) / 1000;
      utimesSync(f, ts, ts);
    };
    mkdirSync(join(outside, 'dir'));
    writeFileSync(join(outside, 'old.txt'), 'x');
    aged(join(outside, 'old.txt'));
    writeFileSync(join(outside, 'dir', 'old2.txt'), 'x');
    aged(join(outside, 'dir', 'old2.txt'));
    const media = join(home, 'media');
    mkdirSync(media, { recursive: true });
    try {
      symlinkSync(join(outside, 'dir'), join(media, 'linkdir'), 'dir');
      symlinkSync(join(outside, 'old.txt'), join(media, 'linkfile-fresh'));
      symlinkSync(join(outside, 'old.txt'), join(media, 'linkfile-old'));
    } catch (err) {
      t.skip(`cannot create symlinks here: ${String(err)}`);
      return;
    }
    const ts = (Date.now() - 8 * DAY) / 1000;
    lutimesSync(join(media, 'linkfile-old'), ts, ts);

    const daemon = await runDaemon({ createChannel: () => createFakeChannel().channel, herdr: createFakeHerdr().deps, connectRetryMs: 50 });
    daemons.push(daemon);
    const line = await waitFor(() => swept(home), 'the media.swept log line');
    // nothing behind a link was touched, even though every target is old
    assert.equal(existsSync(join(outside, 'old.txt')), true);
    assert.equal(existsSync(join(outside, 'dir', 'old2.txt')), true);
    // the links themselves: fresh ones stay (a fresh link to an old file included), an old one is unlinked
    assert.equal(lstatSync(join(media, 'linkdir')).isSymbolicLink(), true);
    assert.equal(lstatSync(join(media, 'linkfile-fresh')).isSymbolicLink(), true);
    assert.equal(existsSync(join(media, 'linkfile-old')), false);
    assert.match(line, /"removed":1/);
    await daemon.stop();
  });
});
