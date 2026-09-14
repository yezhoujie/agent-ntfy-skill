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

function optionLines(options: AskOption[], recommend: string, lang: Lang): string {
  const T = t(lang);
  return options
    .map((o, i) => {
      const flag = o.id === recommend ? `　${T.recommended}` : o.danger ? '　⚠️' : '';
      return `${i + 1}. **${o.label}**${flag}\n   ${o.consequence}`;
    })
    .join('\n');
}

export interface AskCardContext {
  payload: AskPayload;
  projectLabel: string;
  reqId: string;
  state: AskState;
  reply?: string;
}

const HEADER: Record<AskState, { template: string; icon: string }> = {
  pending: { template: 'blue', icon: '🤔' },
  answered: { template: 'green', icon: '✅' },
  timedout: { template: 'grey', icon: '⌛' },
  cancelled: { template: 'grey', icon: '⚠️' },
};

export function askCard(ctx: AskCardContext): object {
  const { payload: p, state } = ctx;
  const lang = p.lang ?? 'zh';
  const T = t(lang);
  const head = HEADER[state];
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
    md(`**${T.options}**\n\n${optionLines(p.options, p.recommend, lang)}`),
    hr(),
    md(field(T.recommend, p.reasoning)),
    md(`**${T.question}**　${p.question}`),
  );

  if (state === 'pending') {
    for (const o of p.options) {
      const button: Record<string, unknown> = {
        tag: 'button',
        text: { tag: 'plain_text', content: o.label },
        type: o.danger ? 'danger' : o.id === p.recommend ? 'primary' : 'default',
        // Both the 1.0 `value` field and 2.0 `behaviors` reach the callback as
        // `action.value`; `value` is kept because it is the shorter of the two
        // and was verified to work on a schema-2.0 card.
        value: { reqId: ctx.reqId, optionId: o.id },
      };
      if (o.danger) {
        button.confirm = {
          title: { tag: 'plain_text', content: T.confirmTitle },
          text: { tag: 'plain_text', content: fill(T.confirmText, { label: o.label }) },
        };
      }
      elements.push(button);
    }
    elements.push(note(p.options.some((o) => o.danger) ? T.hintDanger : T.hint));
  }

  return card(
    { icon: head.icon, title: `[${ctx.projectLabel}] ${p.title}${statusWord ? ` · ${statusWord}` : ''}`, template: head.template },
    elements,
  );
}

export function notifyCard(p: NotifyPayload, projectLabel: string): object {
  return card({ icon: '📣', title: `[${projectLabel}] ${p.title}`, template: 'wathet' }, [md(p.body)]);
}

/** Sent into the group when a phone message could not reach the terminal. */
export function receiptCard(projectLabel: string, why: string, lang: Lang = 'zh'): object {
  const T = t(lang);
  return card({ icon: '⚠️', title: `[${projectLabel}] ${T.notDelivered}`, template: 'orange' }, [
    md(fill(T.notDeliveredBody, { why })),
  ]);
}

/** Pushed while remote mode is on and the agent is stuck on a prompt only a human can answer. */
export function statusCard(projectLabel: string, detail: string, lang: Lang = 'zh'): object {
  const T = t(lang);
  return card({ icon: '🔔', title: `[${projectLabel}] ${T.statusBlocked}`, template: 'orange' }, [md(detail)]);
}
