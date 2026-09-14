import { test } from 'node:test';
import assert from 'node:assert/strict';
import { validateAsk } from '../src/validate.js';

test('validateAsk accepts a minimal valid payload', () => {
  const payload = validateAsk({
    title: 'Keep or delete the scratch directory',
    doing: 'Wiring up the test runner',
    description: 'A minimal payload that satisfies every required field.',
    blocker: 'Nothing is blocked; this only proves the module loads and runs.',
    options: [
      { id: 'keep', label: 'Keep it', consequence: 'Nothing changes' },
      { id: 'drop', label: 'Drop it', consequence: 'The directory is gone' },
    ],
    recommend: 'keep',
    reasoning: 'Keeping is reversible; dropping is not.',
    question: 'Keep or drop?',
  });
  assert.equal(payload.recommend, 'keep');
  assert.equal(payload.options.length, 2);
  assert.equal(payload.lang, undefined);
});
