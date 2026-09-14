// The per-project state file and the project label, in temporary directories.
import { after, test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { projectLabel, projectStatePath, readProjectState, writeProjectState } from '../src/paths.js';

const scratch: string[] = [];
function tmp(prefix: string): string {
  const dir = mkdtempSync(join(tmpdir(), prefix));
  scratch.push(dir);
  return dir;
}
process.env.AGENT_LARK_HOME = tmp('al-paths-home-');
after(() => {
  for (const dir of scratch) rmSync(dir, { recursive: true, force: true });
});

test('state.json carries exactly away / chatId / target / updated', () => {
  const root = tmp('al-paths-proj-');
  const written = writeProjectState(root, { away: true, chatId: 'oc_x' }, { create: true });
  assert.ok(written);
  assert.deepEqual(Object.keys(written).sort(), ['away', 'chatId', 'target', 'updated']);
  const onDisk = JSON.parse(readFileSync(projectStatePath(root), 'utf8')) as Record<string, unknown>;
  assert.deepEqual(Object.keys(onDisk).sort(), ['away', 'chatId', 'target', 'updated']);
  assert.equal(onDisk.away, true);
  assert.equal(onDisk.chatId, 'oc_x');
  assert.equal(onDisk.target, root);
  assert.match(String(onDisk.updated), /^\d{4}-\d{2}-\d{2}T/);
  const read = readProjectState(root);
  assert.deepEqual(read, written);
  assert.equal(readFileSync(join(root, '.agent-lark', '.gitignore'), 'utf8'), '*\n');
});

test('a later write keeps the fields it does not name', () => {
  const root = tmp('al-paths-proj-');
  writeProjectState(root, { away: true, chatId: 'oc_x' }, { create: true });
  const next = writeProjectState(root, { away: false });
  assert.equal(next?.chatId, 'oc_x');
  assert.equal(next?.away, false);
});

test('a state file written before paneId left it: the field is dropped on read and gone after the next write', () => {
  const root = tmp('al-paths-proj-');
  writeProjectState(root, { away: false, chatId: null }, { create: true });
  writeFileSync(projectStatePath(root), JSON.stringify({ away: true, chatId: 'oc_x', paneId: 'w1:p9', target: root, updated: '' }));
  const read = readProjectState(root) as unknown as Record<string, unknown>;
  assert.deepEqual(Object.keys(read).sort(), ['away', 'chatId', 'target', 'updated']);
  assert.equal(read.away, true);
  assert.equal(read.chatId, 'oc_x');
  writeProjectState(root, { away: false });
  const onDisk = JSON.parse(readFileSync(projectStatePath(root), 'utf8')) as Record<string, unknown>;
  assert.equal('paneId' in onDisk, false);
});

test('without create, a project that never bound gets no directory planted', () => {
  const root = tmp('al-paths-proj-');
  assert.equal(writeProjectState(root, { away: true }), null);
  assert.equal(existsSync(join(root, '.agent-lark')), false);
  assert.equal(readProjectState(root), null);
});

test('projectLabel is the directory name', () => {
  assert.equal(projectLabel(join('/', 'home', 'me', 'agent-ntfy-skill')), 'agent-ntfy-skill');
});
