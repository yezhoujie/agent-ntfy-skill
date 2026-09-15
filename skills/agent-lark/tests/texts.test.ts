import { test } from 'node:test';
import assert from 'node:assert/strict';
import { both, en, fill, msg, zh } from '../src/texts.js';

const placeholders = (s: string): string[] => [...new Set(s.match(/\{[a-zA-Z_]+\}/g) ?? [])].sort();

test('zh and en share the same key set', () => {
  assert.deepEqual(Object.keys(en).sort(), Object.keys(zh).sort());
});

test('every zh/en pair carries the same placeholders', () => {
  for (const key of Object.keys(zh) as Array<keyof typeof zh>) {
    assert.deepEqual(placeholders(en[key]), placeholders(zh[key]), `key "${key}"`);
  }
});

test('no wording is empty and nothing English-only contains Han characters', () => {
  for (const [key, value] of Object.entries(zh)) assert.ok(value.trim(), `zh.${key} is empty`);
  for (const [key, value] of Object.entries(en)) {
    assert.ok(value.trim(), `en.${key} is empty`);
    assert.doesNotMatch(value, /\p{Script=Han}/u, `en.${key}`);
  }
  for (const [key, value] of Object.entries(msg)) {
    assert.ok(value.trim(), `msg.${key} is empty`);
    assert.doesNotMatch(value, /\p{Script=Han}/u, `msg.${key}`);
  }
});

test('agent-facing wording uses ASCII whitespace only', () => {
  for (const [key, value] of Object.entries(msg)) {
    assert.doesNotMatch(value, /[\u00a0\u3000\u2000-\u200b]/, `msg.${key} contains a non-ASCII space`);
  }
});

test('fill substitutes every placeholder and leaves unknown braces alone', () => {
  assert.equal(fill('"{label}" is final ({label}); {n} left', { label: 'Drop', n: 2 }), '"Drop" is final (Drop); 2 left');
  assert.equal(fill('{a} {b}', { a: 'x' }), 'x {b}');
});

test('the four herdr prompt failure codes have card wording in both languages', () => {
  for (const key of ['promptAgentBlocked', 'promptPaneGone', 'promptNoHerdr', 'promptRefused'] as const) {
    assert.ok(zh[key].trim());
    assert.ok(en[key].trim());
  }
  assert.deepEqual(placeholders(zh.promptRefused), ['{code}', '{message}']);
});

test('both(): a single-line value is zh and en side by side; a multi-line value is the zh block, then the en block, never joined on one line', () => {
  assert.equal(both('setupMenuPrompt'), `${zh.setupMenuPrompt}　/　${en.setupMenuPrompt}`);
  const menu = both('setupMenu');
  assert.equal(menu, `${zh.setupMenu}\n${en.setupMenu}`);
  assert.doesNotMatch(menu, /　\/　/);
  const lines = menu.split('\n');
  assert.equal(lines.length, 6);
  assert.deepEqual(lines.slice(0, 3), zh.setupMenu.split('\n'));
  assert.deepEqual(lines.slice(3), en.setupMenu.split('\n'));
});
