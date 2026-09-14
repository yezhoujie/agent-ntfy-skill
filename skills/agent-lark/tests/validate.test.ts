// Payload validation: the single-choice rules stay, the multi-choice ones are new.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { validateAsk, ValidationError } from '../src/validate.js';

const base = {
  title: 'Keep or delete the scratch directory',
  doing: 'Wiring up the test runner',
  description: 'A minimal payload that satisfies every required field.',
  blocker: 'Nothing is blocked.',
  options: [
    { id: 'keep', label: 'Keep it', consequence: 'Nothing changes' },
    { id: 'drop', label: 'Drop it', consequence: 'The directory is gone' },
    { id: 'wipe', label: 'Wipe everything', consequence: 'All gone, for good', danger: true },
  ],
  reasoning: 'Keeping is reversible; dropping is not.',
  question: 'Keep or drop?',
};

function problemsOf(raw: unknown): string[] {
  try {
    validateAsk(raw);
  } catch (err) {
    if (err instanceof ValidationError) return err.problems;
    throw err;
  }
  return [];
}

test('single choice (the default): recommend is one option id, and select is normalized to "single"', () => {
  const p = validateAsk({ ...base, recommend: 'keep' });
  assert.equal(p.select, 'single');
  assert.equal(p.recommend, 'keep');
  assert.equal(validateAsk({ ...base, recommend: 'keep', select: 'single' }).select, 'single');
});

test('single choice: an unknown or danger recommend, or an array, is refused', () => {
  assert.match(problemsOf({ ...base, recommend: 'nope' }).join('\n'), /"nope" is not the id of any option/);
  assert.match(problemsOf({ ...base, recommend: 'wipe' }).join('\n'), /marked danger/);
  assert.match(problemsOf({ ...base, recommend: ['keep'] }).join('\n'), /single/);
  assert.match(problemsOf({ ...base, recommend: ['keep'] }).join('\n'), /recommend/);
});

test('multi choice: recommend is a non-empty list of distinct, existing, non-danger option ids', () => {
  const p = validateAsk({ ...base, select: 'multi', recommend: ['drop', 'keep'] });
  assert.equal(p.select, 'multi');
  assert.deepEqual(p.recommend, ['drop', 'keep']);
});

test('multi choice: every way the recommend list can be wrong, each reported', () => {
  const notArray = problemsOf({ ...base, select: 'multi', recommend: 'keep' }).join('\n');
  assert.match(notArray, /recommend/);
  assert.match(notArray, /array/);
  assert.match(problemsOf({ ...base, select: 'multi', recommend: [] }).join('\n'), /non-empty/);
  assert.match(problemsOf({ ...base, select: 'multi', recommend: ['keep', 'keep'] }).join('\n'), /"keep" is listed twice/);
  assert.match(problemsOf({ ...base, select: 'multi', recommend: ['keep', 'nope'] }).join('\n'), /"nope" is not the id of any option/);
  assert.match(problemsOf({ ...base, select: 'multi', recommend: ['keep', 'wipe'] }).join('\n'), /"wipe" is marked danger/);
  assert.match(problemsOf({ ...base, select: 'multi', recommend: ['keep', 42] }).join('\n'), /array/);
  // one bad item does not hide the others: two problems, both named
  const two = problemsOf({ ...base, select: 'multi', recommend: ['nope', 'wipe'] });
  assert.equal(two.length, 2, two.join('\n'));
});

test('select must be "single" or "multi"', () => {
  const p = problemsOf({ ...base, recommend: 'keep', select: 'both' });
  assert.equal(p.length, 1, p.join('\n'));
  assert.match(p[0]!, /select: must be "single" or "multi", got "both"/);
});
