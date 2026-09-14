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
  problems.push(`lang: 只能是 "zh" 或 "en"，收到 ${JSON.stringify(v)}`);
  return undefined;
}

/** Reports every problem at once — a round trip per field is intolerable. */
export function validateAsk(raw: unknown): AskPayload {
  const problems: string[] = [];
  const o = (raw ?? {}) as Record<string, unknown>;

  const title = str(o.title);
  if (!title) problems.push('title: 必填，且不能为空');
  else if (title.length > LIMITS.title) problems.push(`title: 超过 ${LIMITS.title} 字，当前 ${title.length}`);
  else if (title.includes('\n')) problems.push('title: 不能有换行');

  const simple: Array<[keyof AskPayload, string]> = [
    ['doing', '一句话说明这是哪个任务'],
    ['description', '给没看过任何过程的人的背景'],
    ['blocker', '到底什么卡住了'],
    ['reasoning', '你的倾向 + 最强的反对意见'],
    ['question', '一句话能回答的问题'],
  ];
  const values: Record<string, string> = {};
  for (const [key, hint] of simple) {
    const v = str(o[key]);
    if (!v) problems.push(`${key}: 必填（${hint}）`);
    else if (v.length > LIMITS.field) problems.push(`${key}: 超过 ${LIMITS.field} 字，当前 ${v.length}`);
    else values[key] = v;
  }

  const options: AskOption[] = [];
  if (!Array.isArray(o.options)) {
    problems.push('options: 必须是数组');
  } else {
    if (o.options.length < LIMITS.minOptions)
      problems.push(`options: 至少 ${LIMITS.minOptions} 项（只有一个选项不叫选择）`);
    if (o.options.length > LIMITS.maxOptions)
      problems.push(`options: 最多 ${LIMITS.maxOptions} 项（更多说明问题还没收敛）`);
    const seen = new Set<string>();
    o.options.forEach((item, i) => {
      const opt = (item ?? {}) as Record<string, unknown>;
      const id = str(opt.id);
      const label = str(opt.label);
      const consequence = str(opt.consequence);
      if (!id) problems.push(`options[${i}].id: 必填`);
      else if (seen.has(id)) problems.push(`options[${i}].id: "${id}" 重复`);
      else seen.add(id);
      if (!label) problems.push(`options[${i}].label: 必填（按钮上的字，也是点下去回给你的内容）`);
      else if (label.length > LIMITS.optionLabel)
        problems.push(`options[${i}].label: 超过 ${LIMITS.optionLabel} 字`);
      if (!consequence) problems.push(`options[${i}].consequence: 必填（选它实际会发生什么，含代价）`);
      else if (consequence.length > LIMITS.optionConsequence)
        problems.push(`options[${i}].consequence: 超过 ${LIMITS.optionConsequence} 字`);
      if (id && label && consequence)
        options.push({ id, label, consequence, danger: opt.danger === true });
    });
  }

  const recommend = str(o.recommend);
  if (!recommend) problems.push('recommend: 必填，填某个 option 的 id');
  else if (options.length && !options.some((x) => x.id === recommend))
    problems.push(`recommend: "${recommend}" 不是任何一个 option 的 id`);
  else {
    const rec = options.find((x) => x.id === recommend);
    if (rec?.danger)
      problems.push(
        `recommend: "${recommend}" 标了 danger，不可逆/高代价的选项不能作为推荐项——列出来让人自己选`,
      );
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
  if (!title) problems.push('title: 必填，且不能为空');
  else if (title.length > LIMITS.title) problems.push(`title: 超过 ${LIMITS.title} 字`);
  else if (title.includes('\n')) problems.push('title: 不能有换行');
  if (!body) problems.push('body: 必填，且不能为空');
  else if (body.length > LIMITS.body) problems.push(`body: 超过 ${LIMITS.body} 字`);
  const lang = checkLang(o.lang, problems);
  if (problems.length) throw new ValidationError(problems);
  return { title: title!, body: body!, lang };
}
