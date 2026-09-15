// The pane side of the herdr adapter: quoting for `pane run`, and the three
// calls the interactive setup hand-off makes, against a scripted `herdr`.
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { closePane, quoteForPaneShell, runInPane, splitPane, type HerdrRun, type HerdrRunner } from '../src/herdr.js';

function recorder(answer: (args: string[]) => HerdrRun): { run: HerdrRunner; calls: string[][] } {
  const calls: string[][] = [];
  return {
    calls,
    run: async (args) => {
      calls.push(args);
      return answer(args);
    },
  };
}

test('POSIX quoting: plain words pass, anything else is single-quoted, a word starting with = is always quoted (zsh expands =cmd)', () => {
  assert.equal(quoteForPaneShell(['/usr/bin/node', '/a/b.mjs', 'setup', '--reuse'], 'darwin'), '/usr/bin/node /a/b.mjs setup --reuse');
  assert.equal(quoteForPaneShell(['x y', "it's", '$HOME', ''], 'linux'), `'x y' 'it'\\''s' '$HOME' ''`);
  assert.equal(quoteForPaneShell(['=ls', 'a=b'], 'linux'), `'=ls' a=b`);
});

test('Windows quoting follows CommandLineToArgvW as list2cmdline applies it: spaces force quoting, a quote is escaped, a trailing backslash inside quotes is doubled', () => {
  assert.equal(quoteForPaneShell(['C:\\node.exe', 'a b', 'say "hi"', 'C:\\dir\\', 'x y\\'], 'win32'), 'C:\\node.exe "a b" "say \\"hi\\"" C:\\dir\\ "x y\\\\"');
});

test('splitPane: the split is asked below the caller with cwd and no focus; the new pane id comes out of result.pane.pane_id', async () => {
  const ok = recorder(() => ({ ok: true, stdout: JSON.stringify({ id: 'x', result: { pane: { pane_id: 'w1:p7' } } }) }));
  assert.equal(await splitPane('/work', 'w1:p2', ok.run), 'w1:p7');
  assert.deepEqual(ok.calls, [['pane', 'split', '--pane', 'w1:p2', '--direction', 'down', '--cwd', '/work', '--no-focus']]);
  const refused = recorder(() => ({ ok: false, stdout: '', error: 'herdr: not found' }));
  assert.equal(await splitPane('/work', 'w1:p2', refused.run), null);
  const odd = recorder(() => ({ ok: true, stdout: '{"result":{}}' }));
  assert.equal(await splitPane('/work', 'w1:p2', odd.run), null);
});

test('runInPane types one quoted command line into the pane; closePane closes it', async () => {
  const rec = recorder(() => ({ ok: true, stdout: '{}' }));
  assert.equal(await runInPane('w1:p7', ['/usr/bin/node', '/skill/cli.mjs', '--home', '/tmp/h', 'setup', '--reuse'], rec.run), true);
  assert.deepEqual(rec.calls[0], ['pane', 'run', 'w1:p7', quoteForPaneShell(['/usr/bin/node', '/skill/cli.mjs', '--home', '/tmp/h', 'setup', '--reuse'])]);
  assert.equal((await closePane('w1:p7', rec.run)).ok, true);
  assert.deepEqual(rec.calls[1], ['pane', 'close', 'w1:p7']);
});
