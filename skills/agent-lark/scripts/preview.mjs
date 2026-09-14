// Dev-only: render every card this skill can send into one local HTML page,
// so the layout can be judged before a Feishu app exists. Not shipped.
import { writeFileSync } from 'node:fs';
import { askCard, notifyCard, receiptCard, statusCard } from '../dist/cards.js';

const payload = {
  title: 'scratch 目录用完删不删',
  doing: '让需求助手在没有代码检出的情况下先跑起来',
  description:
    '之前助手要求本地必须有代码目录，这个限制去掉了，所以要决定它的临时子进程在没有检出时跑在哪。这里的“临时子进程”指助手为了解析需求单独起的一次性进程，它需要一个当前工作目录。',
  blocker: '没有代码目录，就没有一个天然的工作目录给那个子进程。',
  options: [
    { id: 'keep', label: '保留固定目录', consequence: '一个项目一个目录。出问题有现场可看；代价是目录越攒越多没人清' },
    { id: 'temp', label: '用完即删', consequence: '干净，但崩溃后没有现场，排查只能靠日志' },
    { id: 'wipe', label: '连同历史一起清掉', consequence: '磁盘最干净，但之前所有项目的现场一并没了，不可恢复', danger: true },
  ],
  recommend: 'keep',
  reasoning:
    '倾向保留固定目录：走到这条路径的用户本来就最可能环境是坏的，留个现场值。最强反对：磁盘上垃圾目录会累积，没人负责清理。',
  question: '保留固定目录，还是用完即删？',
  lang: 'zh',
};

const esc = (s) => String(s).replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' })[c]);

// lark_md subset actually used by cards.ts: **bold** and line breaks.
const larkMd = (s) =>
  esc(s)
    .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
    .replace(/\n/g, '<br>');

const TEMPLATE_COLORS = {
  blue: '#3370ff',
  green: '#34c724',
  grey: '#8f959e',
  orange: '#ff8800',
  red: '#f54a45',
  wathet: '#1cafff',
};

function renderElements(elements) {
  return elements
    .map((el) => {
      if (el.tag === 'hr') return '<div class="hr"></div>';
      if (el.tag === 'div') return `<div class="div">${larkMd(el.text.content)}</div>`;
      if (el.tag === 'note')
        return `<div class="note">${el.elements.map((e) => esc(e.content)).join(' ')}</div>`;
      if (el.tag === 'action')
        return `<div class="actions">${el.actions
          .map(
            (b) =>
              `<button class="btn ${b.type}">${esc(b.text.content)}${b.confirm ? '<span class="lock">⚠</span>' : ''}</button>`,
          )
          .join('')}</div>`;
      return '';
    })
    .join('');
}

function renderCard(card) {
  const color = TEMPLATE_COLORS[card.header?.template] ?? '#3370ff';
  return `<div class="card">
    <div class="header" style="background:${color}">${esc(card.header.title.content)}</div>
    <div class="body">${renderElements(card.elements)}</div>
  </div>`;
}

const samples = [
  ['待回答（三个选项，第三个是不可逆项）', askCard({ payload, projectLabel: 'herdr-lark', reqId: 'r1', state: 'pending' })],
  ['已回答（按钮点了推荐项）', askCard({ payload, projectLabel: 'herdr-lark', reqId: 'r1', state: 'answered', reply: '保留固定目录' })],
  ['已回答（手打的自由文本）', askCard({ payload, projectLabel: 'herdr-lark', reqId: 'r1', state: 'answered', reply: '都不要，改成放到 /tmp 下按天分目录，第二天自动过期' })],
  ['超时', askCard({ payload, projectLabel: 'herdr-lark', reqId: 'r1', state: 'timedout' })],
  ['取消（提问方被杀）', askCard({ payload, projectLabel: 'herdr-lark', reqId: 'r1', state: 'cancelled' })],
  ['单向通知 notify', notifyCard({ title: '测试全绿，开始迁移', body: '三个 CI runner 全部通过。\n\n下一步：**staging 库的 schema 迁移**，大约 10 分钟。完事再通知你。', lang: 'zh' }, 'ctle-job')],
  ['注入失败回执', receiptCard('ctle-job', '终端里的 agent 正卡在一个需要你本人确认的提示上，收不了新输入。回电脑前处理一下。')],
  ['agent 卡住了', statusCard('ctle-job', 'blocked', '**宜兴网站内容安全任务数据丢失**\n窗格 w3:p1')],
  ['agent 干完了', statusCard('herdr-lark', 'idle', '**Feishu remote interaction skill**\n窗格 w4:p1')],
];

const html = `<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<title>herdr-lark 卡片预览</title>
<style>
  :root { color-scheme: light; }
  body { margin:0; padding:32px; background:#f5f6f7; font:14px/1.6 -apple-system,"PingFang SC","Helvetica Neue",sans-serif; color:#1f2329; }
  h1 { font-size:20px; margin:0 0 4px; }
  .sub { color:#646a73; margin-bottom:28px; font-size:13px; }
  .grid { display:flex; flex-wrap:wrap; gap:28px; align-items:flex-start; }
  .item { width:420px; max-width:100%; }
  .cap { font-size:12px; color:#646a73; margin-bottom:8px; font-weight:600; }
  .card { border-radius:8px; overflow:hidden; background:#fff; box-shadow:0 2px 8px rgba(31,35,41,.1); }
  .header { padding:12px 16px; color:#fff; font-weight:600; font-size:15px; line-height:1.4; }
  .body { padding:16px; }
  .div { margin:0 0 12px; word-break:break-word; }
  .div:last-child { margin-bottom:0; }
  .hr { height:1px; background:#dee0e3; margin:12px 0; }
  .note { color:#8f959e; font-size:12px; margin-top:12px; }
  .actions { display:flex; gap:8px; flex-wrap:wrap; margin-top:14px; }
  .btn { font:inherit; font-size:14px; padding:6px 16px; border-radius:6px; cursor:default; border:1px solid #d0d3d6; background:#fff; color:#1f2329; }
  .btn.primary { background:#3370ff; border-color:#3370ff; color:#fff; }
  .btn.danger { background:#f54a45; border-color:#f54a45; color:#fff; }
  .lock { margin-left:6px; font-size:12px; }
  @media (max-width:480px) { body{padding:16px} .item{width:100%} }
</style></head><body>
<h1>herdr-lark 卡片预览</h1>
<div class="sub">这是 <code>src/cards.ts</code> 渲染出的真实卡片 JSON，按飞书的样式画出来。飞书实际渲染会有细微差别（字体、圆角），结构与文案一致。</div>
<div class="grid">
${samples.map(([cap, card]) => `<div class="item"><div class="cap">${esc(cap)}</div>${renderCard(card)}</div>`).join('\n')}
</div>
</body></html>`;

const out = new URL('../card-preview.html', import.meta.url);
writeFileSync(out, html);
console.log(`写好了：${out.pathname}`);
