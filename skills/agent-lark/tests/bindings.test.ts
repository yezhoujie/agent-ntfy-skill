// The binding store on disk: one live group per project plus the groups it
// let go of, keyed by chat id, in a temporary AGENT_LARK_HOME.
import { after, test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { BindingStore, BindingsFileError, groupName, taskNameProblem, type Binding } from '../src/bindings.js';

const homes: string[] = [];
function freshHome(): string {
  const dir = mkdtempSync(join(tmpdir(), 'al-bindings-'));
  homes.push(dir);
  process.env.AGENT_LARK_HOME = dir;
  return dir;
}
after(() => {
  for (const dir of homes) rmSync(dir, { recursive: true, force: true });
});

const entry = (over: Partial<Binding> & { root: string; chatId: string }): Binding => ({
  label: 'p',
  name: null,
  paneId: null,
  away: false,
  lang: null,
  boundAt: '2026-01-01T00:00:00.000Z',
  releasedAt: null,
  ...over,
});

test('a bindings.json from before releases (keyed by root, no name/lang/releasedAt) loads whole and is rewritten in the new shape', () => {
  const home = freshHome();
  const legacy = {
    bindings: [
      { root: '/a', label: 'a', chatId: 'oc_a', paneId: 'w1:p1', away: true, boundAt: '2025-12-01T00:00:00.000Z' },
      { root: '/b', label: 'b', chatId: 'oc_b', paneId: null, away: false, boundAt: '2025-12-02T00:00:00.000Z' },
      { root: 42, chatId: 'oc_bad' },
    ],
  };
  writeFileSync(join(home, 'bindings.json'), JSON.stringify(legacy));
  const store = new BindingStore();
  assert.equal(store.all().length, 2);
  const a = store.active('/a');
  assert.ok(a);
  assert.deepEqual(a, {
    root: '/a',
    label: 'a',
    chatId: 'oc_a',
    name: null,
    paneId: 'w1:p1',
    away: true,
    lang: null,
    boundAt: '2025-12-01T00:00:00.000Z',
    releasedAt: null,
  });
  assert.equal(store.byChat('oc_b')?.root, '/b');
  // the first write lands in the new shape
  store.touch('/a', { paneId: 'w1:p2' });
  const written = JSON.parse(readFileSync(join(home, 'bindings.json'), 'utf8')) as { bindings: Binding[] };
  assert.equal(written.bindings.length, 2);
  for (const b of written.bindings) {
    assert.equal(b.releasedAt, null);
    assert.equal(b.name, null);
    assert.equal(b.lang, null);
  }
});

test('one live entry and two released ones for the same root: active / released / activeChatIds tell them apart', () => {
  freshHome();
  const store = new BindingStore();
  store.set(entry({ root: '/p', chatId: 'oc_old1', releasedAt: '2026-01-02T00:00:00.000Z' }));
  store.set(entry({ root: '/p', chatId: 'oc_old2', releasedAt: '2026-01-03T00:00:00.000Z' }));
  store.set(entry({ root: '/p', chatId: 'oc_live', name: 'task [p]' }));
  store.set(entry({ root: '/q', chatId: 'oc_q' }));
  assert.equal(store.active('/p')?.chatId, 'oc_live');
  assert.deepEqual(store.released('/p').map((b) => b.chatId).sort(), ['oc_old1', 'oc_old2']);
  assert.deepEqual(store.activeChatIds().sort(), ['oc_live', 'oc_q']);
  assert.equal(store.all().length, 4);
  assert.equal(store.activeByChat('oc_old1'), undefined);
  assert.equal(store.activeByChat('oc_live')?.root, '/p');
});

test('a second live entry for a root is refused before anything is written', () => {
  const home = freshHome();
  const store = new BindingStore();
  store.set(entry({ root: '/p', chatId: 'oc_1' }));
  assert.throws(() => store.set(entry({ root: '/p', chatId: 'oc_2' })), /one active/);
  const written = JSON.parse(readFileSync(join(home, 'bindings.json'), 'utf8')) as { bindings: Binding[] };
  assert.deepEqual(written.bindings.map((b) => b.chatId), ['oc_1']);
  // re-setting the same live entry is fine
  store.set(entry({ root: '/p', chatId: 'oc_1', name: 'renamed [p]' }));
  assert.equal(store.active('/p')?.name, 'renamed [p]');
});

test('touch only reaches the live entry; released ones keep their pane, away and label', () => {
  freshHome();
  const store = new BindingStore();
  store.set(entry({ root: '/p', chatId: 'oc_old', paneId: 'w1:p9', away: true, releasedAt: '2026-01-02T00:00:00.000Z' }));
  store.set(entry({ root: '/p', chatId: 'oc_live' }));
  const touched = store.touch('/p', { paneId: 'w1:p1', away: true, label: 'proj', lang: 'en', name: 'x [proj]' });
  assert.equal(touched?.chatId, 'oc_live');
  assert.equal(touched?.paneId, 'w1:p1');
  assert.equal(touched?.away, true);
  assert.equal(touched?.lang, 'en');
  assert.equal(touched?.name, 'x [proj]');
  const old = store.byChat('oc_old');
  assert.equal(old?.paneId, 'w1:p9');
  assert.equal(old?.label, 'p');
  assert.equal(store.touch('/nope', { paneId: 'w1:p1' }), undefined);
});

test('release stamps releasedAt and switches away off; set() with releasedAt null makes the entry live again', () => {
  freshHome();
  const store = new BindingStore();
  store.set(entry({ root: '/p', chatId: 'oc_live', away: true }));
  const released = store.release('/p');
  assert.ok(released);
  assert.equal(released.away, false);
  assert.match(released.releasedAt ?? '', /^\d{4}-\d{2}-\d{2}T/);
  assert.equal(store.active('/p'), undefined);
  assert.equal(store.release('/p'), undefined);
  store.set({ ...released, releasedAt: null });
  assert.equal(store.active('/p')?.chatId, 'oc_live');
});

test('remove drops the entry by chat id and writes the file; removing an unknown chat id touches nothing', () => {
  const home = freshHome();
  const store = new BindingStore();
  store.set(entry({ root: '/a', chatId: 'oc_live' }));
  store.set(entry({ root: '/a', chatId: 'oc_gone', releasedAt: '2026-01-02T00:00:00.000Z' }));
  const file = join(home, 'bindings.json');
  const gone = store.remove('oc_gone');
  assert.equal(gone?.chatId, 'oc_gone');
  assert.deepEqual(store.all().map((b) => b.chatId), ['oc_live']);
  assert.deepEqual((JSON.parse(readFileSync(file, 'utf8')) as { bindings: Binding[] }).bindings.map((b) => b.chatId), ['oc_live']);
  const before = readFileSync(file, 'utf8');
  writeFileSync(file, before.replace(/\n$/, '\n\n'));
  assert.equal(store.remove('oc_nope'), undefined);
  assert.equal(readFileSync(file, 'utf8'), before.replace(/\n$/, '\n\n'), 'the file was rewritten although nothing changed');
  assert.deepEqual(store.all().map((b) => b.chatId), ['oc_live']);
  // a live entry goes the same way
  assert.equal(store.remove('oc_live')?.root, '/a');
  assert.equal(store.active('/a'), undefined);
});

test('two live entries for one root on disk: the newer stays live, the older is treated as released', () => {
  const home = freshHome();
  writeFileSync(
    join(home, 'bindings.json'),
    JSON.stringify({
      bindings: [
        entry({ root: '/p', chatId: 'oc_older', boundAt: '2026-01-01T00:00:00.000Z' }),
        entry({ root: '/p', chatId: 'oc_newer', boundAt: '2026-01-05T00:00:00.000Z' }),
      ],
    }),
  );
  const repaired: Array<{ root: string; chatId: string }> = [];
  const store = new BindingStore({ onRepaired: (info) => repaired.push(info) });
  assert.equal(store.active('/p')?.chatId, 'oc_newer');
  assert.deepEqual(store.released('/p').map((b) => b.chatId), ['oc_older']);
  assert.deepEqual(repaired, [{ root: '/p', chatId: 'oc_older' }]);
  // the repair is written back, so the next daemon start does not repeat it
  const onDisk = JSON.parse(readFileSync(join(home, 'bindings.json'), 'utf8')) as { bindings: Binding[] };
  assert.match(String(onDisk.bindings.find((b) => b.chatId === 'oc_older')?.releasedAt), /^\d{4}-/);
  assert.equal(onDisk.bindings.find((b) => b.chatId === 'oc_newer')?.releasedAt, null);
  const again: unknown[] = [];
  new BindingStore({ onRepaired: (info) => again.push(info) });
  assert.deepEqual(again, []);
});

test('one chat id under two roots on disk: the newer binding keeps it, the older is dropped, reported and written back', () => {
  const home = freshHome();
  writeFileSync(
    join(home, 'bindings.json'),
    JSON.stringify({
      bindings: [
        { root: '/b', label: 'b', chatId: 'oc_shared', paneId: null, away: false, boundAt: '2025-12-01T00:00:00.000Z' },
        { root: '/c', label: 'c', chatId: 'oc_shared', paneId: null, away: false, boundAt: '2025-12-02T00:00:00.000Z' },
        { root: '/d', label: 'd', chatId: 'oc_shared2', paneId: null, away: false, boundAt: '2025-12-03T00:00:00.000Z' },
        { root: '/e', label: 'e', chatId: 'oc_shared2', paneId: null, away: false, boundAt: '2025-12-02T00:00:00.000Z' },
      ],
    }),
  );
  const dropped: Array<{ root: string; chatId: string }> = [];
  const store = new BindingStore({ onDropped: (info) => dropped.push(info) });
  assert.deepEqual(store.all().map((b) => [b.root, b.chatId]).sort(), [
    ['/c', 'oc_shared'],
    ['/d', 'oc_shared2'],
  ]);
  assert.deepEqual(dropped.sort((x, y) => x.root.localeCompare(y.root)), [
    { root: '/b', chatId: 'oc_shared' },
    { root: '/e', chatId: 'oc_shared2' },
  ]);
  const onDisk = JSON.parse(readFileSync(join(home, 'bindings.json'), 'utf8')) as { bindings: Binding[] };
  assert.deepEqual(onDisk.bindings.map((b) => b.root).sort(), ['/c', '/d']);
});

test('a missing bindings.json is an empty store; a damaged one is refused with its path, not wiped', () => {
  const home = freshHome();
  assert.equal(new BindingStore().all().length, 0);
  writeFileSync(join(home, 'bindings.json'), '{"bindings": [');
  assert.throws(() => new BindingStore(), (err: unknown) => err instanceof BindingsFileError && err.message.includes(join(home, 'bindings.json')));
  writeFileSync(join(home, 'bindings.json'), '{"bindings": "nope"}');
  assert.throws(() => new BindingStore(), BindingsFileError);
  assert.equal(readFileSync(join(home, 'bindings.json'), 'utf8'), '{"bindings": "nope"}');
});

test('groupName is "<task> [<dir>]", or "[<dir>]" without a task; the task name is capped at 60 code points', () => {
  assert.equal(groupName('发版准备', 'agent-ntfy-skill'), '发版准备 [agent-ntfy-skill]');
  assert.equal(groupName(undefined, 'agent-ntfy-skill'), '[agent-ntfy-skill]');
  assert.equal(groupName('   ', 'agent-ntfy-skill'), '[agent-ntfy-skill]');
  assert.equal(taskNameProblem('x'.repeat(60)), null);
  assert.equal(taskNameProblem('😀'.repeat(60)), null);
  assert.match(taskNameProblem('x'.repeat(61)) ?? '', /60/);
  assert.match(taskNameProblem('😀'.repeat(61)) ?? '', /61/);
});
