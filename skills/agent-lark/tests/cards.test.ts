// The card JSON this skill sends, checked field by field: header colours and
// icons, the button row of a single-choice question, the form of a
// multi-choice one, and the language each card shell is rendered in.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { askCard, notifyCard, receiptCard, statusCard } from '../src/cards.js';
import type { AskPayload } from '../src/validate.js';

interface Card {
  schema: string;
  header: { title: { tag: string; content: string }; template: string };
  body: { elements: Array<Record<string, unknown>> };
}
const asCard = (c: object): Card => c as Card;
const tags = (c: Card): string[] => c.body.elements.map((e) => String(e.tag));

const single: AskPayload = {
  title: 'Keep the scratch dir?',
  doing: 'wiring',
  description: 'bg',
  blocker: 'blk',
  options: [
    { id: 'keep', label: 'Keep', consequence: 'stays' },
    { id: 'drop', label: 'Drop', consequence: 'gone' },
    { id: 'wipe', label: 'Wipe', consequence: 'all gone', danger: true },
  ],
  recommend: 'keep',
  reasoning: 'why',
  question: 'keep or drop?',
  lang: 'zh',
  select: 'single',
};
const multi: AskPayload = { ...single, select: 'multi', recommend: ['keep', 'drop'] };

test('pending single-choice: blue header, one button per option (primary = recommended, danger = confirm), hint last, no form', () => {
  const c = asCard(askCard({ payload: single, projectLabel: 'proj', reqId: 'r1', state: 'pending' }));
  assert.equal(c.schema, '2.0');
  assert.equal(c.header.template, 'blue');
  assert.equal(c.header.title.content, '🤔 [proj] Keep the scratch dir?');
  assert.deepEqual(tags(c), ['markdown', 'markdown', 'markdown', 'hr', 'markdown', 'hr', 'markdown', 'markdown', 'button', 'button', 'button', 'markdown']);
  const buttons = c.body.elements.filter((e) => e.tag === 'button');
  assert.deepEqual(
    buttons.map((b) => [(b.text as { content: string }).content, b.type, 'confirm' in b, b.value]),
    [
      ['Keep', 'primary', false, { reqId: 'r1', optionId: 'keep' }],
      ['Drop', 'default', false, { reqId: 'r1', optionId: 'drop' }],
      ['Wipe', 'danger', true, { reqId: 'r1', optionId: 'wipe' }],
    ],
  );
  const options = c.body.elements[4]!.content as string;
  assert.match(options, /1\. \*\*Keep\*\*　← 我推荐/);
  assert.match(options, /3\. \*\*Wipe\*\*　⚠️/);
  const hint = c.body.elements.at(-1)!;
  assert.match(String(hint.content), /红色按钮会二次确认/);
});

test('pending multi-choice with a danger option: one form of checkers plus a submit button that asks for confirmation', () => {
  const c = asCard(askCard({ payload: multi, projectLabel: 'proj', reqId: 'r2', state: 'pending' }));
  assert.equal(c.header.template, 'blue');
  assert.deepEqual(tags(c), ['markdown', 'markdown', 'markdown', 'hr', 'markdown', 'hr', 'markdown', 'markdown', 'form', 'markdown']);
  const options = c.body.elements[4]!.content as string;
  assert.match(options, /1\. \*\*Keep\*\*　← 我推荐/);
  assert.match(options, /2\. \*\*Drop\*\*　← 我推荐/);
  assert.match(options, /3\. \*\*Wipe\*\*　⚠️/);
  const form = c.body.elements[8]!;
  assert.deepEqual(form, {
    tag: 'form',
    name: 'ask',
    elements: [
      { tag: 'checker', name: 'opt:keep', checked: true, text: { tag: 'lark_md', content: '**Keep**　stays' } },
      { tag: 'checker', name: 'opt:drop', checked: true, text: { tag: 'lark_md', content: '**Drop**　gone' } },
      { tag: 'checker', name: 'opt:wipe', checked: false, text: { tag: 'lark_md', content: '**Wipe**　all gone' } },
      {
        tag: 'button',
        name: 'submit',
        form_action_type: 'submit',
        type: 'primary',
        text: { tag: 'plain_text', content: '提交' },
        behaviors: [{ type: 'callback', value: { reqId: 'r2' } }],
        confirm: {
          title: { tag: 'plain_text', content: '确认执行' },
          text: { tag: 'plain_text', content: '所选项里有不可逆或高代价的操作。确定提交？' },
        },
      },
    ],
  });
  assert.match(String(c.body.elements.at(-1)!.content), /勾选后点提交/);
});

test('checker names are prefixed, so an option id of "submit" or "ask" cannot collide with the form or its button', () => {
  const payload: AskPayload = {
    ...multi,
    options: [
      { id: 'submit', label: 'S', consequence: 'c' },
      { id: 'ask', label: 'A', consequence: 'c' },
    ],
    recommend: ['ask'],
  };
  const c = asCard(askCard({ payload, projectLabel: 'proj', reqId: 'r9', state: 'pending' }));
  const form = c.body.elements.find((e) => e.tag === 'form') as { name: string; elements: Array<Record<string, unknown>> };
  const names = form.elements.map((e) => e.name);
  assert.deepEqual(names, ['opt:submit', 'opt:ask', 'submit']);
  assert.equal(new Set([form.name, ...names]).size, 4, 'a name is used twice on the card');
});

test('pending multi-choice without danger: the submit button carries no confirm; en shell wording', () => {
  const payload: AskPayload = {
    ...multi,
    lang: 'en',
    options: multi.options.filter((o) => !o.danger),
  };
  const c = asCard(askCard({ payload, projectLabel: 'proj', reqId: 'r3', state: 'pending' }));
  const form = c.body.elements.find((e) => e.tag === 'form')!;
  const submit = (form.elements as Array<Record<string, unknown>>).at(-1)!;
  assert.equal((submit.text as { content: string }).content, 'Submit');
  assert.equal('confirm' in submit, false);
  assert.match(String(c.body.elements.at(-1)!.content), /Tick what applies, then Submit/);
});

test('pending --urgent: red header; the same card without urgent is blue', () => {
  const urgent = asCard(askCard({ payload: single, projectLabel: 'proj', reqId: 'r4', state: 'pending', urgent: true }));
  assert.equal(urgent.header.template, 'red');
  assert.equal(urgent.header.title.content, '🤔 [proj] Keep the scratch dir?');
  const calm = asCard(askCard({ payload: single, projectLabel: 'proj', reqId: 'r4', state: 'pending', urgent: false }));
  assert.equal(calm.header.template, 'blue');
  assert.deepEqual(tags(urgent), tags(calm));
});

test('answered / timed out / cancelled: green / grey / grey, reply pinned on top, no buttons and no form — for single and multi alike', () => {
  for (const payload of [single, multi]) {
    const answered = asCard(askCard({ payload, projectLabel: 'proj', reqId: 'r5', state: 'answered', reply: 'Keep、Drop', urgent: true }));
    assert.equal(answered.header.template, 'green');
    assert.equal(answered.header.title.content, '✅ [proj] Keep the scratch dir? · 已回答');
    assert.deepEqual(tags(answered).slice(0, 3), ['markdown', 'hr', 'markdown']);
    assert.equal(String(answered.body.elements[0]!.content), '**你的回复**　Keep、Drop');
    assert.equal(tags(answered).includes('button'), false);
    assert.equal(tags(answered).includes('form'), false);
    const timedout = asCard(askCard({ payload, projectLabel: 'proj', reqId: 'r5', state: 'timedout' }));
    assert.equal(timedout.header.template, 'grey');
    assert.match(timedout.header.title.content, /^⌛ .* · 已超时$/);
    const cancelled = asCard(askCard({ payload, projectLabel: 'proj', reqId: 'r5', state: 'cancelled' }));
    assert.equal(cancelled.header.template, 'grey');
    assert.match(cancelled.header.title.content, /^⚠️ .* · 已取消$/);
  }
});

test('notify: wathet header with the megaphone, one markdown body', () => {
  const c = asCard(notifyCard({ title: 'Tests green', body: 'moving on', lang: 'en' }, 'proj'));
  assert.equal(c.header.template, 'wathet');
  assert.equal(c.header.title.content, '📣 [proj] Tests green');
  assert.deepEqual(c.body.elements, [{ tag: 'markdown', content: 'moving on' }]);
});

test('receipt card: orange, shell wording follows the language it is asked for', () => {
  const zh = asCard(receiptCard('proj', 'nowhere to go', 'zh'));
  assert.equal(zh.header.template, 'orange');
  assert.equal(zh.header.title.content, '⚠️ [proj] 没能送达');
  assert.match(String(zh.body.elements[0]!.content), /没能送进终端：nowhere to go/);
  const en = asCard(receiptCard('proj', 'nowhere to go', 'en'));
  assert.equal(en.header.title.content, '⚠️ [proj] Not delivered');
  assert.match(String(en.body.elements[0]!.content), /never reached the terminal: nowhere to go/);
});

test('receipt and status cards default to English when no language is given', () => {
  assert.equal(asCard(receiptCard('proj', 'why')).header.title.content, '⚠️ [proj] Not delivered');
  assert.equal(asCard(statusCard('proj', 'pane w1:p1')).header.title.content, '🔔 [proj] waiting for you');
});

test('status card: orange, shell wording follows the language it is asked for', () => {
  const zh = asCard(statusCard('proj', '窗格 w1:p1', 'zh'));
  assert.equal(zh.header.template, 'orange');
  assert.equal(zh.header.title.content, '🔔 [proj] 等你输入');
  const en = asCard(statusCard('proj', 'pane w1:p1', 'en'));
  assert.equal(en.header.title.content, '🔔 [proj] waiting for you');
  assert.deepEqual(en.body.elements, [{ tag: 'markdown', content: 'pane w1:p1' }]);
});
