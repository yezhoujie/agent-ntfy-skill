// The wire between CLI and daemon: a real endpoint (unix socket or Windows
// named pipe) with a hand-written handler on the daemon side.
import { after, test } from 'node:test';
import assert from 'node:assert/strict';
import { createConnection } from 'node:net';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const home = mkdtempSync(join(tmpdir(), 'al-ipc-'));
process.env.AGENT_LARK_HOME = home;
after(() => rmSync(home, { recursive: true, force: true }));

const { ipcEndpoint } = await import('../src/paths.js');
const { isDaemonListening, request, serve } = await import('../src/ipc.js');
const { msg } = await import('../src/texts.js');

type Handle = Parameters<typeof serve>[0]['handle'];
const pong = { ok: true, kind: 'pong', status: { pid: 1, connection: 'fake', connected: true, lastError: null, pendingAsks: 0, bindings: 0, startedAt: '', media: { ttlDays: 7, files: 0, bytes: 0, at: '' } } } as const;

async function withServer<T>(handle: Handle, body: () => Promise<T>): Promise<T> {
  const server = await serve({ handle });
  try {
    return await body();
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
}

test('nothing listening: request fails with code 3 and the daemon-down hint, isDaemonListening is false', async () => {
  const res = await request({ type: 'ping' }, { timeoutMs: 1000 });
  assert.equal(res.ok, false);
  if (res.ok) return;
  assert.equal(res.code, 3);
  assert.equal(res.message, msg.ipcDaemonDown);
  assert.equal(res.reason, 'down');
  assert.equal(await isDaemonListening(), false);
});

test('ping is answered with pong and isDaemonListening becomes true', async () => {
  await withServer(
    async () => pong,
    async () => {
      const res = await request({ type: 'ping' }, { timeoutMs: 1000 });
      assert.deepEqual(res, pong);
      assert.equal(await isDaemonListening(), true);
    },
  );
});

test('note frames arrive before the result, in order', async () => {
  await withServer(
    async (_req, ctx) => {
      ctx.note('first');
      ctx.note('second');
      return { ok: true, kind: 'ack' };
    },
    async () => {
      const notes: string[] = [];
      const res = await request({ type: 'list' }, { onNote: (t) => notes.push(t) });
      assert.deepEqual(res, { ok: true, kind: 'ack' });
      assert.deepEqual(notes, ['first', 'second']);
    },
  );
});

test('ask blocks until the handler settles', async () => {
  let settle!: () => void;
  const released = new Promise<void>((r) => (settle = r));
  await withServer(
    async () => {
      await released;
      return { ok: true, kind: 'ask', reply: 'keep', via: 'text' };
    },
    async () => {
      let done = false;
      const pending = request({ type: 'ask', root: '/p', label: 'p', paneId: null, payload: {}, timeoutMs: 5000 }).then((r) => {
        done = true;
        return r;
      });
      await new Promise((r) => setTimeout(r, 150));
      assert.equal(done, false, 'resolved before the handler settled');
      settle();
      assert.deepEqual(await pending, { ok: true, kind: 'ask', reply: 'keep', via: 'text' });
    },
  );
});

test('client-side timeoutMs turns a silent daemon into code 3', async () => {
  await withServer(
    () => new Promise(() => {}),
    async () => {
      const res = await request({ type: 'ping' }, { timeoutMs: 200 });
      assert.equal(res.ok, false);
      if (res.ok) return;
      assert.equal(res.code, 3);
      assert.equal(res.message, msg.ipcTimeout);
      assert.equal(res.reason, 'timeout');
    },
  );
});

test('unparseable request line gets a code 1 result frame', async () => {
  await withServer(
    async () => pong,
    async () => {
      const raw = await new Promise<string>((resolve, reject) => {
        const sock = createConnection(ipcEndpoint());
        let buf = '';
        sock.on('connect', () => sock.write('this is not json\n'));
        sock.on('data', (c) => (buf += c.toString('utf8')));
        sock.on('close', () => resolve(buf));
        sock.on('error', reject);
      });
      assert.deepEqual(JSON.parse(raw.trim()), { frame: 'result', body: { ok: false, code: 1, message: msg.ipcBadRequest, reason: 'parse' } });
    },
  );
});

test('a client that hangs up mid-request triggers the handler onClose hook', async () => {
  let resolveClosed!: () => void;
  const closedSeen = new Promise<void>((r) => (resolveClosed = r));
  await withServer(
    (_req, ctx) =>
      new Promise(() => {
        ctx.onClose(resolveClosed);
      }),
    async () => {
      const sock = createConnection(ipcEndpoint());
      await new Promise<void>((ok) => sock.on('connect', () => ok()));
      sock.write(`${JSON.stringify({ type: 'ping' })}\n`);
      await new Promise((t) => setTimeout(t, 100));
      sock.destroy();
      await closedSeen;
    },
  );
});
