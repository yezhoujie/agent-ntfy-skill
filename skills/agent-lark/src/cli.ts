#!/usr/bin/env node
import { spawn } from 'node:child_process';
import { existsSync, openSync, realpathSync, unlinkSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { createLarkChannel, registerApp } from '@larksuite/channel';
import QRCode from 'qrcode';
import { clearCreds, credsReport, defaultStore, resolveCreds, writeCreds, type StoreKind } from './creds.js';
import { envFile } from './creds.js';
import { currentPaneId, insideHerdr } from './herdr.js';
import { isDaemonListening, request, type Response } from './ipc.js';
import { ensureHomeDir, logPath, pidPath, projectLabel, projectRoot, readProjectState, sockPath, writeProjectState } from './paths.js';
import { validateAsk, validateNotify, ValidationError } from './validate.js';

// Piping into `head` / `less` closes our stdout early; an unhandled EPIPE
// would crash with a stack trace instead of just ending quietly.
for (const stream of [process.stdout, process.stderr]) {
  stream.on('error', (err: NodeJS.ErrnoException) => {
    if (err.code === 'EPIPE') process.exit(0);
    throw err;
  });
}

const HELP = `herdr-lark — 把 herdr 里跑着的 agent 会话接到飞书

  setup [--update] [--scopes a,b]  扫码创建/更新飞书应用，凭据存进钥匙串
  setup --app-id cli_xxx [--store]  用已有应用；secret 从环境变量/env 文件读，绝不走 argv
  daemon [--detach|--status|--stop]  常驻进程：连飞书长连接（有问题挂着时 --stop 会被拦，除非 --force）
  bind [--chat <id>] [--name <名>]   把当前项目绑到一个飞书群（默认新建一个）
  unbind                             解绑当前项目
  ask [--timeout <秒>]               stdin 读 JSON，推一张提问卡，阻塞等答复
  notify                             stdin 读 JSON，推一条带标题的通知卡（重大事项）
  say [--title <一句话>]             stdin 读 markdown，把终端回复同步到群（远程模式下每次回复都发）
  send-file <路径> [--caption <说明>] 把图片或文件发到项目群
  away on [--idle [分钟]] | off | status   远程模式；默认只推「卡住了」，不推「干完了」
  status                             daemon 与绑定概览

退出码：0 成功 · 1 输入有问题 · 2 超时没人回答 · 3 通道故障 · 4 需要人动手
`;

/** Scopes the scan-code confirm page asks for. Override with --scopes. */
const DEFAULT_SCOPES = [
  'im:message',
  'im:message:send_as_bot',
  'im:message.group_msg',
  'im:chat',
  'im:resource',
  // Voice messages arrive as an opaque `<audio/>` placeholder without this.
  'speech_to_text:speech',
];

/** Non-Error throws are common in SDKs; never let them print [object Object]. */
export function describeError(err: unknown): string {
  if (err instanceof Error) return err.message || err.name;
  if (typeof err === 'string') return err;
  if (err && typeof err === 'object') {
    const o = err as Record<string, unknown>;
    const parts = [o.code, o.msg, o.message, o.error, o.error_description]
      .filter((v) => typeof v === 'string' || typeof v === 'number')
      .map(String);
    if (parts.length) return parts.join(' · ');
    try {
      return JSON.stringify(err);
    } catch {
      return Object.prototype.toString.call(err);
    }
  }
  return String(err);
}

function die(code: number, msg: string): never {
  process.stderr.write(`herdr-lark: ${msg}\n`);
  process.exit(code);
}

function flag(args: string[], name: string): boolean {
  return args.includes(`--${name}`);
}

function opt(args: string[], name: string): string | undefined {
  const i = args.indexOf(`--${name}`);
  if (i < 0) return undefined;
  const v = args[i + 1];
  return v && !v.startsWith('--') ? v : undefined;
}

async function readStdin(): Promise<string> {
  if (process.stdin.isTTY) die(1, '这个命令要从 stdin 读一段 JSON。用 heredoc 喂给它。');
  const chunks: Buffer[] = [];
  for await (const chunk of process.stdin) chunks.push(chunk as Buffer);
  return Buffer.concat(chunks).toString('utf8');
}

function ctx(): { root: string; label: string; paneId: string | null } {
  const root = projectRoot();
  return { root, label: projectLabel(root), paneId: currentPaneId() };
}

/** Turn a daemon response into this process's exit. */
function finish(res: Response, onOk: (r: Extract<Response, { ok: true }>) => void): never {
  if (res.ok) {
    onOk(res);
    process.exit(0);
  }
  process.stderr.write(`herdr-lark: ${res.message}\n`);
  process.exit(res.code);
}

// ---------------------------------------------------------------- setup

async function cmdSetup(args: string[]): Promise<void> {
  const update = flag(args, 'update');
  if (flag(args, 'reset')) clearCreds();
  const existing = resolveCreds();
  // Credentials sitting only in the environment are not yet *configured* —
  // setup's job is to verify and persist them, so only a store short-circuits.
  const alreadyPersisted = existing?.source === 'keychain' || existing?.source === 'file';
  if (alreadyPersisted && !update && !flag(args, 'reset') && !opt(args, 'app-id')) {
    process.stdout.write(
      `已经有凭据了（来自 ${existing.origin}）。想重新授权或补权限，加 --update；想换一个应用，先 herdr-lark setup --reset。\n`,
    );
    return;
  }
  const storeOpt = opt(args, 'store');
  if (storeOpt && !['keychain', 'file', 'none'].includes(storeOpt))
    die(1, '--store 只能是 keychain / file / none');
  const store = (storeOpt as StoreKind | undefined) ?? defaultStore();

  // Binding an app that already exists on the open platform. The secret is
  // read from the environment and never travels through argv, a file we
  // write, or any output — the same rule as every other credential here.
  // Adopting an app that already exists on the open platform: the secret is
  // taken from the environment or an env file, never from argv (argv is
  // visible to every process on the box via `ps`).
  const flagAppId = opt(args, 'app-id');
  const fromEnv = resolveCreds();
  const manualId = flagAppId ?? fromEnv?.appId;
  const manualSecret = flagAppId
    ? (process.env.HERDR_LARK_APP_SECRET ?? process.env.LARK_APP_SECRET ?? '').trim() || fromEnv?.appSecret
    : fromEnv?.appSecret;
  if (manualId && manualSecret && (flagAppId || fromEnv?.source === 'env' || fromEnv?.source === 'env-file' || fromEnv?.source === 'env-generic')) {
    process.stdout.write(`正在用这对凭据连一次飞书确认可用（来源：${flagAppId ? '--app-id + 环境变量' : fromEnv!.origin}）……\n`);
    const probe = createLarkChannel({ appId: manualId, appSecret: manualSecret });
    let ownerOpenId: string | undefined;
    try {
      const info = await probe.getAppInfo();
      ownerOpenId = info.ownerId;
      process.stdout.write(`✅ 凭据可用，应用名「${info.appName ?? '(未命名)'}」\n`);
    } catch (err) {
      die(3, `这对凭据连不上飞书：${describeError(err)}`);
    }
    const where = writeCreds({ appId: manualId, appSecret: manualSecret, ownerOpenId }, store);
    process.stdout.write(
      `凭据已保存到：${where}\n下一步：\n  herdr-lark daemon --detach\n  cd <你的项目> && herdr-lark bind\n`,
    );
    return;
  }
  if (flagAppId)
    die(
      4,
      'App Secret 没找到。不要写在命令行里（argv 全机器可见），用下面任一种：\n' +
        '  HERDR_LARK_APP_SECRET=... herdr-lark setup --app-id ' + flagAppId + '\n' +
        `  或写进 ${envFile()}：HERDR_LARK_APP_ID=... / HERDR_LARK_APP_SECRET=...`,
    );

  const scopes = (opt(args, 'scopes')?.split(',').map((s) => s.trim()).filter(Boolean)) ?? DEFAULT_SCOPES;
  process.stdout.write('正在向飞书申请扫码注册……\n');

  let deadline = 0;
  let lastStatus = '';
  let heartbeat: NodeJS.Timeout | undefined;

  let result;
  try {
    result = await registerApp({
      source: 'herdr-lark',
      appId: update && existing ? existing.appId : undefined,
      appPreset: {
        name: 'herdr-lark 值班',
        desc: '把终端里跑着的编程 agent 的提问推到手机，答复注入回终端',
      },
      addons: {
        scopes: { tenant: scopes },
        events: { items: { tenant: ['im.message.receive_v1'] } },
        callbacks: { items: ['card.action.trigger'] },
      },
      onQRCodeReady: ({ url, expireIn }) => {
        deadline = Date.now() + expireIn * 1000;
        const art = QRCode.toString(url, { type: 'terminal', small: true }) as unknown as Promise<string>;
        void art
          .then((s) => process.stdout.write(`\n${s}\n`))
          .catch(() => undefined)
          .finally(() => {
            process.stdout.write(`用飞书扫上面的二维码（扫不到就打开这个链接）：\n${url}\n\n`);
            process.stdout.write(
              `确认页会列出要授权的权限：\n  ${scopes.join('\n  ')}\n  事件 im.message.receive_v1 · 回调 card.action.trigger\n\n`,
            );
            process.stdout.write(`⏳ 二维码 ${Math.round(expireIn / 60)} 分钟内有效（${new Date(deadline).toLocaleTimeString()} 过期），过期就重跑 setup。\n`);
          });
        // One line a minute instead of one every two seconds.
        heartbeat = setInterval(() => {
          const left = Math.max(0, Math.round((deadline - Date.now()) / 1000));
          process.stdout.write(`  还在等你扫……剩 ${left} 秒\n`);
        }, 60_000);
        heartbeat.unref();
      },
      // 'polling' repeats every couple of seconds; only report real changes.
      onStatusChange: (s) => {
        if (s.status === lastStatus) return;
        lastStatus = s.status;
        process.stdout.write(`  状态：${s.status}\n`);
      },
    });
  } catch (err) {
    clearInterval(heartbeat);
    const detail = describeError(err);
    if (deadline && Date.now() >= deadline - 5000)
      die(4, `二维码过期了，没等到扫码。重跑一次：herdr-lark setup\n（原始错误：${detail}）`);
    die(3, `扫码注册失败：${detail}`);
  }
  clearInterval(heartbeat);

  const where = writeCreds(
    {
      appId: result.client_id,
      appSecret: result.client_secret,
      ownerOpenId: result.user_info?.open_id,
      brand: result.user_info?.tenant_brand,
    },
    store,
  );
  process.stdout.write(
    `\n✅ 应用已绑定，凭据保存到：${where}（明文不会出现在任何输出里）。\n下一步：\n  herdr-lark daemon --detach\n  cd <你的项目> && herdr-lark bind\n`,
  );
}

// ---------------------------------------------------------------- daemon

/** Is a daemon already answering on the socket? */
async function daemonAlive(): Promise<boolean> {
  if (!isDaemonListening()) return false;
  const probe = await request({ type: 'ping' }, { timeoutMs: 2000 });
  return probe.ok;
}

/**
 * Start the daemon in its own session. It must outlive the caller: started as
 * a child of a shell command it would die with it, and every message the human
 * sends afterwards would be lost with no error on their side.
 */
async function startDaemonDetached(): Promise<{ ok: boolean; message: string }> {
  if (await daemonAlive()) return { ok: true, message: 'daemon 已经在跑了' };
  ensureHomeDir();
  const out = openSync(logPath(), 'a');
  const self = fileURLToPath(import.meta.url);
  const child = spawn(process.execPath, [self, 'daemon'], { detached: true, stdio: ['ignore', out, out] });
  child.unref();
  for (let i = 0; i < 20; i++) {
    await new Promise((r) => setTimeout(r, 500));
    if (await daemonAlive()) return { ok: true, message: `daemon: 已在后台启动，pid ${child.pid}（日志 ${logPath()}）` };
  }
  return { ok: false, message: `daemon 起来了但 10 秒内没应答，看日志：${logPath()}` };
}


async function cmdDaemon(args: string[]): Promise<void> {
  if (flag(args, 'status')) {
    if (!isDaemonListening()) die(1, 'daemon: 没在跑');
    const res = await request({ type: 'ping' }, { timeoutMs: 5000 });
    if (!res.ok) die(1, `daemon: 无应答（${res.message}）`);
    if (res.kind !== 'pong') die(1, 'daemon: 回了个看不懂的东西');
    const s = res.status;
    process.stdout.write(
      `daemon: pid ${s.pid}  长连接 ${s.connection}  挂起的提问 ${s.pendingAsks}  绑定项目 ${s.bindings}  启动于 ${s.startedAt}\n`,
    );
    return;
  }
  if (flag(args, 'stop')) {
    if (!isDaemonListening()) {
      if (existsSync(pidPath())) unlinkSync(pidPath());
      process.stdout.write('daemon: 本来就没在跑\n');
      return;
    }
    // Stopping cancels every waiting question, which leaves a dead card on
    // someone's phone. Refuse unless the caller says that is what they want.
    if (!flag(args, 'force')) {
      const probe = await request({ type: 'ping' }, { timeoutMs: 5000 });
      if (probe.ok && probe.kind === 'pong' && probe.status.pendingAsks > 0)
        die(
          4,
          `还有 ${probe.status.pendingAsks} 个问题挂在手机上。现在停 daemon 会把这些卡片变成「⚠️ 已取消」，` +
            '人看到的是一张死卡。\n先等回答，或者明知故犯：herdr-lark daemon --stop --force',
        );
    }
    const res = await request({ type: 'stop' }, { timeoutMs: 5000 });
    if (!res.ok) die(3, res.message);
    for (let i = 0; i < 60; i++) {
      if (!existsSync(sockPath())) break;
      await new Promise((r) => setTimeout(r, 500));
    }
    process.stdout.write('daemon: 已停止\n');
    return;
  }
  if (flag(args, 'detach')) {
    const r = await startDaemonDetached();
    if (!r.ok) die(3, r.message);
    process.stdout.write(`${r.message}\n`);
    return;
  }
  const { runDaemon } = await import('./daemon.js');
  await runDaemon();
}

// ---------------------------------------------------------------- bind

async function cmdBind(args: string[]): Promise<void> {
  const { root, label } = ctx();
  const res = await request({
    type: 'bind',
    root,
    label,
    paneId: currentPaneId(),
    chatId: opt(args, 'chat'),
    name: opt(args, 'name'),
  });
  finish(res, (r) => {
    if (r.kind !== 'bind') return;
    writeProjectState(root, { chatId: r.chatId, paneId: currentPaneId() }, { create: true });
    process.stdout.write(
      r.created
        ? `✅ 已新建飞书群「${r.name}」并绑定到 ${root}\n   打开飞书就能看到这个群；以后这个项目的提问都发在里面。\n`
        : `✅ 已绑定到已有群 ${r.chatId}（${root}）\n`,
    );
  });
}

async function cmdUnbind(): Promise<void> {
  const { root } = ctx();
  const res = await request({ type: 'unbind', root });
  finish(res, () => {
    writeProjectState(root, { chatId: null, away: false });
    process.stdout.write('已解绑。飞书群还在，需要的话自己归档。\n');
  });
}

// ---------------------------------------------------------------- ask / notify

async function cmdAsk(args: string[]): Promise<void> {
  const { root, label, paneId } = ctx();
  const raw = await readStdin();
  let payload: unknown;
  try {
    payload = JSON.parse(raw);
  } catch (err) {
    die(1, `stdin 不是合法 JSON：${err instanceof Error ? err.message : String(err)}`);
  }
  // Validation runs here, before anything is sent: a malformed question must
  // fail the same way whether or not the daemon happens to be up.
  try {
    validateAsk(payload);
  } catch (err) {
    if (err instanceof ValidationError)
      die(1, `这张提问卡有 ${err.problems.length} 处问题，一条都没发出去：\n  ${err.problems.join('\n  ')}`);
    throw err;
  }
  const seconds = Number(opt(args, 'timeout') ?? 43_200);
  if (!Number.isFinite(seconds) || seconds <= 0) die(1, '--timeout 要是正整数秒');
  const res = await request(
    { type: 'ask', root, label, paneId, payload, timeoutMs: seconds * 1000 },
    { onNote: (text) => process.stderr.write(`note: ${text}\n`) },
  );
  finish(res, (r) => {
    if (r.kind === 'ask') process.stdout.write(`${r.reply}\n`);
  });
}

async function cmdNotify(): Promise<void> {
  const { root, label, paneId } = ctx();
  const raw = await readStdin();
  let payload: unknown;
  try {
    payload = JSON.parse(raw);
  } catch (err) {
    die(1, `stdin 不是合法 JSON：${err instanceof Error ? err.message : String(err)}`);
  }
  try {
    validateNotify(payload);
  } catch (err) {
    if (err instanceof ValidationError)
      die(1, `这条通知有 ${err.problems.length} 处问题，没有发出去：\n  ${err.problems.join('\n  ')}`);
    throw err;
  }
  const res = await request({ type: 'notify', root, label, paneId, payload });
  finish(res, () => process.stdout.write('通知已发出（对方如果回消息，会作为指令注入到这个窗格）\n'));
}

/** Mirror one terminal reply into the project's group while the human is away. */
async function cmdSay(args: string[]): Promise<void> {
  const { root, label, paneId } = ctx();
  const text = await readStdin();
  if (!text.trim()) die(1, '没有内容可发（从 stdin 读正文）');
  const res = await request({ type: 'say', root, label, paneId, text, title: opt(args, 'title') });
  finish(res, () => process.stdout.write('已同步到飞书群\n'));
}

async function cmdSendFile(args: string[]): Promise<void> {
  const { root, label, paneId } = ctx();
  const path = args.find((a) => !a.startsWith('--'));
  if (!path) die(1, '用法：herdr-lark send-file <路径> [--caption <说明>]');
  const res = await request({ type: 'sendFile', root, label, paneId, path, caption: opt(args, 'caption') });
  finish(res, () => process.stdout.write('已发到项目群\n'));
}

// ---------------------------------------------------------------- away / status

async function cmdAway(args: string[]): Promise<void> {
  const sub = args.find((a) => !a.startsWith('--')) ?? 'status';
  const { root, paneId } = ctx();
  if (sub === 'status') {
    const state = readProjectState(root);
    if (flag(args, 'json')) {
      process.stdout.write(`${JSON.stringify(state ?? { away: false, chatId: null, paneId: null, target: root, updated: '' })}\n`);
      return;
    }
    if (!state) {
      process.stdout.write('这个项目还没用过 herdr-lark（没有 .herdr-lark/state.json）\n');
      return;
    }
    process.stdout.write(
      `远程模式：${state.away ? '开' : '关'}　群：${state.chatId ?? '未绑定'}　窗格：${state.paneId ?? '无'}\n`,
    );
    return;
  }
  if (sub !== 'on' && sub !== 'off') die(1, '用法：herdr-lark away on|off|status');
  const away = sub === 'on';
  const idleFlag = args.includes('--idle');
  if (away) {
    // Everything the channel needs, in one command: credentials, a live
    // daemon, and a group for this project. Asking the user to run three
    // commands in order is how a channel ends up switched half-on.
    if (!resolveCreds()) die(4, '还没有飞书应用凭据。先跑一次：herdr-lark setup');
    const d = await startDaemonDetached();
    if (!d.ok) die(3, d.message);
    process.stdout.write(`${d.message}\n`);
    const bindRes = await request({
      type: 'bind',
      root,
      label: projectLabel(root),
      paneId,
    });
    if (!bindRes.ok) die(bindRes.code, bindRes.message);
    if (bindRes.kind === 'bind') {
      writeProjectState(root, { chatId: bindRes.chatId, paneId }, { create: true });
      process.stdout.write(
        bindRes.created
          ? `已新建飞书群「${bindRes.name}」\n`
          : `已连到飞书群「${bindRes.name}」\n`,
      );
    }
  }
  const idleMinutes = Number(opt(args, 'idle') ?? 10);
  if (idleFlag && (!Number.isFinite(idleMinutes) || idleMinutes <= 0))
    die(1, '--idle 后面要么不带值（默认 10 分钟），要么是正整数分钟');
  const res = await request({
    type: 'setAway',
    root,
    away,
    paneId,
    notifyIdle: away ? idleFlag : false,
    idleMinMinutes: idleFlag ? idleMinutes : undefined,
  });
  finish(res, () => {
    writeProjectState(root, { away, paneId }, { create: true });
    if (!away) {
      process.stdout.write('远程模式已关闭。\n');
      return;
    }
    process.stdout.write(
      '远程模式已开启：要拍板的事、以及 agent 卡在需要你确认的提示上时，会推到这个项目的飞书群。\n' +
        (idleFlag
          ? `「干完了」也推，但只在这一轮跑满 ${idleMinutes} 分钟时（--idle ${idleMinutes}）。\n`
          : '「干完了」默认不推——每轮对话结束都会触发，你在键盘前时纯属噪音。要的话：away on --idle [分钟]\n'),
    );
  });
}

async function cmdStatus(): Promise<void> {
  const creds = resolveCreds();
  process.stdout.write(`凭据：${creds ? `已配置，来自 ${creds.origin}` : '未配置，先跑 herdr-lark setup'}\n`);
  for (const line of credsReport()) process.stdout.write(`  ${line}\n`);
  process.stdout.write(`herdr：${insideHerdr() ? `在 herdr 里，当前窗格 ${currentPaneId()}` : '不在 herdr 里（手机消息将无处注入）'}\n`);
  if (!isDaemonListening()) {
    process.stdout.write('daemon：没在跑（herdr-lark daemon --detach）\n');
    return;
  }
  const ping = await request({ type: 'ping' }, { timeoutMs: 5000 });
  if (ping.ok && ping.kind === 'pong')
    process.stdout.write(`daemon：pid ${ping.status.pid}，长连接 ${ping.status.connection}，挂起提问 ${ping.status.pendingAsks}\n`);
  const list = await request({ type: 'list' }, { timeoutMs: 5000 });
  if (list.ok && list.kind === 'list') {
    if (!list.bindings.length) process.stdout.write('绑定：还没有项目绑定\n');
    else {
      process.stdout.write('绑定：\n');
      const here = projectRoot();
      for (const b of list.bindings)
        process.stdout.write(
          `  ${b.root === here ? '*' : ' '} ${b.label}  群 ${b.chatId}  窗格 ${b.paneId ?? '-'}  远程 ${b.away ? '开' : '关'}  干完了通知 ${b.notifyIdle ? `开(≥${b.idleMinMinutes}分)` : '关'}\n`,
        );
    }
  }
}

// ---------------------------------------------------------------- main

async function main(): Promise<void> {
  const [cmd, ...args] = process.argv.slice(2);
  switch (cmd) {
    case 'setup':
      return cmdSetup(args);
    case 'daemon':
      return cmdDaemon(args);
    case 'bind':
      return cmdBind(args);
    case 'unbind':
      return cmdUnbind();
    case 'ask':
      return cmdAsk(args);
    case 'notify':
      return cmdNotify();
    case 'say':
      return cmdSay(args);
    case 'send-file':
      return cmdSendFile(args);
    case 'away':
      return cmdAway(args);
    case 'status':
      return cmdStatus();
    case undefined:
    case '--help':
    case '-h':
    case 'help':
      process.stdout.write(HELP);
      return;
    default:
      die(1, `不认识的命令 "${cmd}"。herdr-lark --help 看用法。`);
  }
}

// Only run when invoked as the program; importing this module (tests, tooling)
// must not execute a command.
const isEntry = process.argv[1] && fileURLToPath(import.meta.url) === realpathSync(process.argv[1]);
if (isEntry)
  main().catch((err: unknown) => {
    const detail = err instanceof Error ? (err.stack ?? err.message) : describeError(err);
    process.stderr.write(`herdr-lark: ${detail}\n`);
    process.exit(3);
  });
