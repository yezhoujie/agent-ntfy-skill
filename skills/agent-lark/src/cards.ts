import { fill, t } from './texts.js';
import type { AskOption, AskPayload, Lang, NotifyPayload } from './validate.js';

export type AskState = 'pending' | 'answered' | 'timedout' | 'cancelled';

/**
 * Card JSON 2.0. The `markdown` element is a real rich-text component —
 * tables, ordered/unordered lists and fenced code blocks all render — unlike
 * 1.0's `lark_md`, which is only an *inline* formatting tag on a text
 * component and supports none of them. The one breaking change from 1.0:
 * `tag: "action"` is gone, buttons sit directly in `body.elements`.
 */
const md = (content: string): object => ({ tag: 'markdown', content });
const hr = (): object => ({ tag: 'hr' });

/**
 * Small grey caption line. Colour is an inline `<font>` tag in the content:
 * the markdown element has `text_size` and `text_align`, but no `text_color`.
 */
const note = (content: string): object => ({
  tag: 'markdown',
  content: `<font color='grey'>${content}</font>`,
  text_size: 'notation',
});

function card(header: { icon: string; title: string; template: string }, elements: object[]): object {
  return {
    schema: '2.0',
    config: { width_mode: 'fill' },
    header: {
      title: { tag: 'plain_text', content: `${header.icon} ${header.title}` },
      template: header.template,
    },
    body: { elements },
  };
}

function field(label: string, value: string): string {
  return `**${label}**　${value}`;
}

/** The ids the agent leans towards, whether one or several. */
export function recommendedIds(p: AskPayload): string[] {
  return Array.isArray(p.recommend) ? p.recommend : [p.recommend];
}

/**
 * One line per option. Observed on the desktop client: inside a numbered
 * list item, an ideographic space (U+3000) right after a bold run renders
 * with no gap at all; the field lines above (`**label**　value`) are not
 * affected. So label and consequence are joined by a real separator here,
 * and the recommendation mark closes the line.
 */
function optionLines(options: AskOption[], recommended: string[], lang: Lang): string {
  const T = t(lang);
  return options
    .map((o, i) => {
      const danger = o.danger ? '　⚠️' : '';
      const tail = recommended.includes(o.id) ? `　${T.recommended}` : '';
      return `${i + 1}. **${o.label}**${danger}${T.optionSep}${o.consequence}${tail}`;
    })
    .join('\n');
}

export interface AskCardContext {
  payload: AskPayload;
  projectLabel: string;
  reqId: string;
  state: AskState;
  reply?: string;
  /** Flagged in-app to the owner; the pending header turns red. */
  urgent?: boolean;
  /** How many times the pending card has been re-rendered after a refused submit; part of the submit action's value. */
  attempt?: number;
}

const HEADER: Record<AskState, { template: string; icon: string }> = {
  pending: { template: 'blue', icon: '🤔' },
  answered: { template: 'green', icon: '✅' },
  timedout: { template: 'grey', icon: '⌛' },
  cancelled: { template: 'grey', icon: '⚠️' },
};

/** A native confirm dialog on a button. */
function confirm(T: ReturnType<typeof t>, text: string): object {
  return {
    title: { tag: 'plain_text', content: T.confirmTitle },
    text: { tag: 'plain_text', content: text },
  };
}

/**
 * Single choice: one button per option. Both the 1.0 `value` field and 2.0
 * `behaviors` reach the callback as `action.value`; `value` is kept because it
 * is the shorter of the two and was verified to work on a schema-2.0 card.
 */
function optionButtons(p: AskPayload, reqId: string, recommended: string[], T: ReturnType<typeof t>): object[] {
  return p.options.map((o) => {
    const button: Record<string, unknown> = {
      tag: 'button',
      text: { tag: 'plain_text', content: o.label },
      type: o.danger ? 'danger' : recommended.includes(o.id) ? 'primary' : 'default',
      value: { reqId, optionId: o.id },
    };
    if (o.danger) button.confirm = confirm(T, fill(T.confirmText, { label: o.label }));
    return button;
  });
}

/** Form field name of an option's checker; option ids are free-form, the prefix keeps them clear of the form's own names. */
export const checkerName = (optionId: string): string => `opt:${optionId}`;
export const optionIdOf = (checkerName: string): string | null => (checkerName.startsWith('opt:') ? checkerName.slice(4) : null);

/**
 * Multi choice: a form with one checker per option and a submit button. The
 * checkers only hold their state locally; the submit button sends them all in
 * one callback carrying `reqId`. A danger option cannot get its own confirm
 * dialog inside a form, so the dialog sits on the submit button whenever the
 * card holds one.
 */
function optionForm(p: AskPayload, reqId: string, attempt: number, recommended: string[], T: ReturnType<typeof t>): object {
  // The channel SDK drops a repeat of the same action on the same card for
  // twelve hours, keyed on the button's value; a refused submit re-renders
  // the card with the next attempt so the human's retry is a new action.
  const submit: Record<string, unknown> = {
    tag: 'button',
    name: 'submit',
    form_action_type: 'submit',
    type: 'primary',
    text: { tag: 'plain_text', content: T.submit },
    behaviors: [{ type: 'callback', value: { reqId, attempt } }],
  };
  if (p.options.some((o) => o.danger)) submit.confirm = confirm(T, T.confirmMultiText);
  return {
    tag: 'form',
    name: 'ask',
    elements: [
      ...p.options.map((o) => ({
        tag: 'checker',
        name: checkerName(o.id),
        checked: recommended.includes(o.id),
        text: { tag: 'lark_md', content: `**${o.label}**${T.optionSep}${o.consequence}` },
      })),
      submit,
    ],
  };
}

export function askCard(ctx: AskCardContext): object {
  const { payload: p, state } = ctx;
  const lang = p.lang ?? 'zh';
  const T = t(lang);
  const head = HEADER[state];
  const template = state === 'pending' && ctx.urgent ? 'red' : head.template;
  const recommended = recommendedIds(p);
  const statusWord =
    state === 'answered' ? T.answered : state === 'timedout' ? T.timedout : state === 'cancelled' ? T.cancelled : '';

  const elements: object[] = [];
  if (state === 'answered' && ctx.reply) {
    elements.push(md(field(T.yourReply, ctx.reply)), hr(), note(T.theQuestion));
  }

  elements.push(
    md(field(T.doing, p.doing)),
    md(field(T.background, p.description)),
    md(field(T.blocker, p.blocker)),
    hr(),
    md(`**${T.options}**\n\n${optionLines(p.options, recommended, lang)}`),
    hr(),
    md(field(T.recommend, p.reasoning)),
    md(`**${T.question}**　${p.question}`),
  );

  if (state === 'pending') {
    if (p.select === 'multi') {
      elements.push(optionForm(p, ctx.reqId, ctx.attempt ?? 0, recommended, T), note(T.hintMulti));
    } else {
      elements.push(...optionButtons(p, ctx.reqId, recommended, T));
      elements.push(note(p.options.some((o) => o.danger) ? T.hintDanger : T.hint));
    }
  }

  return card(
    { icon: head.icon, title: `[${ctx.projectLabel}] ${p.title}${statusWord ? ` · ${statusWord}` : ''}`, template },
    elements,
  );
}

export function notifyCard(p: NotifyPayload, projectLabel: string): object {
  return card({ icon: '📣', title: `[${projectLabel}] ${p.title}`, template: 'wathet' }, [md(p.body)]);
}

/** Sent into the group when a phone message could not reach the terminal. */
export function receiptCard(projectLabel: string, why: string, lang: Lang = 'en'): object {
  const T = t(lang);
  return card({ icon: '⚠️', title: `[${projectLabel}] ${T.notDelivered}`, template: 'orange' }, [
    md(fill(T.notDeliveredBody, { why })),
  ]);
}

/** Pushed while remote mode is on and the agent is stuck on a prompt only a human can answer. */
export function statusCard(projectLabel: string, detail: string, lang: Lang = 'en'): object {
  const T = t(lang);
  return card({ icon: '🔔', title: `[${projectLabel}] ${T.statusBlocked}`, template: 'orange' }, [md(detail)]);
}
