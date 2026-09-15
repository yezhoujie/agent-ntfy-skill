// The per-project state file and the project label, in temporary directories.
import { after, test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { createConnection, createServer } from 'node:net';
import { platform, tmpdir } from 'node:os';
import { dirname, join } from 'node:path';

import { SOCK_PATH_LIMIT, projectLabel, projectStatePath, readProjectState, sockPath, sockPathProblem, writeProjectState } from '../src/paths.js';
import { homeOfSockBytes as longHome, withHome } from './fixtures/long-home.js';

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

// ---- the socket path and the platform's sun_path limit -----------------------
// A state directory deep enough pushes `<home>/daemon.sock` past what a Unix
// socket path may be; the daemon must say so instead of failing with EINVAL.

function homeOfSockBytes(bytes: number): string {
  const home = longHome(bytes);
  scratch.push(dirname(home));
  return home;
}
/** Bind a Unix socket at `path` for real and connect to it; resolves with the failing side's error code, or null when both worked. */
const listenAt = (path: string): Promise<string | null> =>
  new Promise((resolve) => {
    const srv = createServer((conn) => conn.end());
    srv.once('error', (err: NodeJS.ErrnoException) => resolve(err.code ?? String(err)));
    srv.listen(path, () => {
      const client = createConnection(path);
      client.once('error', (err: NodeJS.ErrnoException) => srv.close(() => resolve(`connect:${err.code ?? String(err)}`)));
      client.once('connect', () => client.end(() => srv.close(() => resolve(null))));
    });
  });

test('sockPathProblem: null for the usual short path', () => {
  assert.equal(sockPathProblem(), null);
});

test('sockPathProblem: a socket path over the limit names the path, its length, the limit and AGENT_LARK_HOME; win32 has no limit', () => {
  const home = homeOfSockBytes(SOCK_PATH_LIMIT + 40);
  const problem = withHome(home, () => sockPathProblem());
  if (platform() === 'win32') {
    assert.equal(problem, null);
    return;
  }
  assert.ok(problem, 'no problem reported');
  assert.equal(problem, `socket path ${join(home, 'daemon.sock')} is ${SOCK_PATH_LIMIT + 40} bytes, over this platform's limit of ${SOCK_PATH_LIMIT}; set AGENT_LARK_HOME to a shorter directory`);
});

test('the limit is real: a socket path of exactly the limit listens, one byte more is EINVAL — and sockPathProblem agrees on both', { skip: platform() === 'win32' ? 'named pipes have no sun_path' : false }, async () => {
  const atLimit = homeOfSockBytes(SOCK_PATH_LIMIT);
  assert.equal(withHome(atLimit, () => sockPathProblem()), null);
  assert.equal(await listenAt(withHome(atLimit, () => sockPath())), null, `${SOCK_PATH_LIMIT} bytes did not listen`);
  const overLimit = homeOfSockBytes(SOCK_PATH_LIMIT + 1);
  assert.match(withHome(overLimit, () => sockPathProblem()) ?? '', /over this platform's limit/);
  assert.equal(await listenAt(withHome(overLimit, () => sockPath())), 'EINVAL', `${SOCK_PATH_LIMIT + 1} bytes should be EINVAL`);
});
