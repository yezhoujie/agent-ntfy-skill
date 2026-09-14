import { fill, msg } from './texts.js';

export type Lang = 'zh' | 'en';

export interface AskOption {
  id: string;
  label: string;
  consequence: string;
  /**
   * Irreversible or high-cost. Rendered as a red button behind a native
   * confirm dialog, so the choice cannot be made by a single stray tap.
   * The recommendation may never be a danger option.
   */
  danger?: boolean;
}

export interface AskPayload {
  title: string;
  doing: string;
  description: string;
  blocker: string;
  options: AskOption[];
  recommend: string;
  reasoning: string;
  question: string;
  lang?: Lang;
}

export interface NotifyPayload {
  title: string;
  body: string;
  lang?: Lang;
}

/** Feishu accepts a far larger card than ntfy did; these are sanity caps. */
const LIMITS = {
  title: 200,
  field: 4000,
  body: 8000,
  optionLabel: 60,
  optionConsequence: 500,
  minOptions: 2,
  maxOptions: 5,
};

export class ValidationError extends Error {
  constructor(readonly problems: string[]) {
    super(problems.join('\n'));
    this.name = 'ValidationError';
  }
}

function str(v: unknown): string | null {
  return typeof v === 'string' && v.trim() ? v.trim() : null;
}

function checkLang(v: unknown, problems: string[]): Lang | undefined {
  if (v === undefined || v === null) return undefined;
  if (v === 'zh' || v === 'en') return v;
  problems.push(fill(msg.vLang, { value: JSON.stringify(v) }));
  return undefined;
}

/** Reports every problem at once — a round trip per field is intolerable. */
export function validateAsk(raw: unknown): AskPayload {
  const problems: string[] = [];
  const o = (raw ?? {}) as Record<string, unknown>;

  const title = str(o.title);
  if (!title) problems.push(msg.vTitleRequired);
  else if (title.length > LIMITS.title) problems.push(fill(msg.vTitleTooLong, { max: LIMITS.title, n: title.length }));
  else if (title.includes('\n')) problems.push(msg.vTitleNewline);

  const simple: Array<[keyof AskPayload, string]> = [
    ['doing', msg.vHintDoing],
    ['description', msg.vHintDescription],
    ['blocker', msg.vHintBlocker],
    ['reasoning', msg.vHintReasoning],
    ['question', msg.vHintQuestion],
  ];
  const values: Record<string, string> = {};
  for (const [key, hint] of simple) {
    const v = str(o[key]);
    if (!v) problems.push(fill(msg.vFieldRequired, { key, hint }));
    else if (v.length > LIMITS.field) problems.push(fill(msg.vFieldTooLong, { key, max: LIMITS.field, n: v.length }));
    else values[key] = v;
  }

  const options: AskOption[] = [];
  if (!Array.isArray(o.options)) {
    problems.push(msg.vOptionsArray);
  } else {
    if (o.options.length < LIMITS.minOptions)
      problems.push(fill(msg.vOptionsMin, { min: LIMITS.minOptions }));
    if (o.options.length > LIMITS.maxOptions)
      problems.push(fill(msg.vOptionsMax, { max: LIMITS.maxOptions }));
    const seen = new Set<string>();
    o.options.forEach((item, i) => {
      const opt = (item ?? {}) as Record<string, unknown>;
      const id = str(opt.id);
      const label = str(opt.label);
      const consequence = str(opt.consequence);
      if (!id) problems.push(fill(msg.vOptionId, { i }));
      else if (seen.has(id)) problems.push(fill(msg.vOptionIdDup, { i, id }));
      else seen.add(id);
      if (!label) problems.push(fill(msg.vOptionLabel, { i }));
      else if (label.length > LIMITS.optionLabel) problems.push(fill(msg.vOptionLabelLong, { i, max: LIMITS.optionLabel }));
      if (!consequence) problems.push(fill(msg.vOptionConsequence, { i }));
      else if (consequence.length > LIMITS.optionConsequence)
        problems.push(fill(msg.vOptionConsequenceLong, { i, max: LIMITS.optionConsequence }));
      if (id && label && consequence)
        options.push({ id, label, consequence, danger: opt.danger === true });
    });
  }

  const recommend = str(o.recommend);
  if (!recommend) problems.push(msg.vRecommendRequired);
  else if (options.length && !options.some((x) => x.id === recommend))
    problems.push(fill(msg.vRecommendUnknown, { id: recommend }));
  else {
    const rec = options.find((x) => x.id === recommend);
    if (rec?.danger) problems.push(fill(msg.vRecommendDanger, { id: recommend }));
  }

  const lang = checkLang(o.lang, problems);
  if (problems.length) throw new ValidationError(problems);

  return {
    title: title!,
    doing: values.doing!,
    description: values.description!,
    blocker: values.blocker!,
    options,
    recommend: recommend!,
    reasoning: values.reasoning!,
    question: values.question!,
    lang,
  };
}

export function validateNotify(raw: unknown): NotifyPayload {
  const problems: string[] = [];
  const o = (raw ?? {}) as Record<string, unknown>;
  const title = str(o.title);
  const body = str(o.body);
  if (!title) problems.push(msg.vTitleRequired);
  else if (title.length > LIMITS.title) problems.push(fill(msg.vTitleTooLong, { max: LIMITS.title, n: title.length }));
  else if (title.includes('\n')) problems.push(msg.vTitleNewline);
  if (!body) problems.push(msg.vBodyRequired);
  else if (body.length > LIMITS.body) problems.push(fill(msg.vBodyTooLong, { max: LIMITS.body }));
  const lang = checkLang(o.lang, problems);
  if (problems.length) throw new ValidationError(problems);
  return { title: title!, body: body!, lang };
}
