#!/usr/bin/env node
import { spawn } from 'node:child_process';
import { existsSync, openSync, realpathSync, unlinkSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createLarkChannel, registerApp } from '@larksuite/channel';
import QRCode from 'qrcode';
import { clearCreds, credsReport, defaultStore, resolveCreds, writeCreds, type StoreKind } from './creds.js';
import { envFile } from './creds.js';
import { currentPaneId, insideHerdr } from './herdr.js';
import { taskNameProblem } from './bindings.js';
import { isDaemonListening, request, type Request, type Response } from './ipc.js';
import { ensureHomeDir, ipcEndpoint, logPath, pidPath, projectLabel, projectRoot, readProjectState, writeProjectState } from './paths.js';
import { both, en, fill, msg, zh } from './texts.js';
import { validateAsk, validateNotify, ValidationError } from './validate.js';

// Piping into `head` / `less` closes our stdout early; an unhandled EPIPE
// would crash with a stack trace instead of just ending quietly.
for (const stream of [process.stdout, process.stderr]) {
  stream.on('error', (err: NodeJS.ErrnoException) => {
    if (err.code === 'EPIPE') process.exit(0);
    throw err;
  });
}

const HELP = msg.help;

/** Scopes the scan-code confirm page asks for. Override with --scopes. */
const DEFAULT_SCOPES = [
  'im:message',
  'im:message:send_as_bot',
  'im:message.group_msg',
  'im:chat',
  'im:resource',
  // `ask --urgent` flags the owner in-app; without this the flag is refused and only noted.
  'im:message.urgent',
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

/** Socket-level errno codes: the request never reached Feishu, or the connection died under it. */
const TRANSIENT_CODES = new Set(['ECONNRESET', 'ECONNREFUSED', 'ECONNABORTED', 'ETIMEDOUT', 'EAI_AGAIN', 'ENOTFOUND', 'ENETUNREACH', 'EHOSTUNREACH', 'EPIPE']);

/**
 * Was a failed registration call the network's doing rather than Feishu's?
 * Anything Feishu itself answered (a business `code` in the body) and
 * anything that is not a socket error is final; only those are worth a
 * fresh QR code.
 */
export function isTransientNetworkError(err: unknown): boolean {
  if (!err || typeof err !== 'object') return false;
  const e = err as { code?: unknown; message?: unknown; response?: { data?: { code?: unknown } } };
  if (typeof e.response?.data?.code === 'number') return false;
  if (typeof e.code === 'string' && TRANSIENT_CODES.has(e.code)) return true;
  const message = typeof e.message === 'string' ? e.message : '';
  return /socket disconnected|socket hang up|ECONNRESET|ETIMEDOUT|EAI_AGAIN|network error/i.test(message);
}

/** A network hiccup while waiting for the scan costs the QR code; this many are asked for before giving up. */
const SETUP_ATTEMPTS = 3;

function die(code: number, text: string): never {
  process.stderr.write(`${msg.prefix}${text}\n`);
  process.exit(code);
}

/** One `setup` line, zh and en side by side — the human is at the terminal for setup, whatever language they read. */
function bilingual(
  key: Parameters<typeof both>[0],
  vars: Record<string, string | number> = {},
  varsEn: Record<string, string | number> = vars,
): void {
  process.stdout.write(`${both(key, vars, varsEn)}\n`);
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
  if (process.stdin.isTTY) die(1, msg.needStdin);
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
  process.stderr.write(`${msg.prefix}${res.message}\n`);
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
    bilingual('setupHaveCreds', { origin: existing.origin });
    return;
  }
  const storeOpt = opt(args, 'store');
  if (storeOpt && !['keychain', 'file', 'none'].includes(storeOpt))
    die(1, both('setupStoreOption'));
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
    ? (process.env.AGENT_LARK_APP_SECRET ?? process.env.LARK_APP_SECRET ?? '').trim() || fromEnv?.appSecret
    : fromEnv?.appSecret;
  if (manualId && manualSecret && (flagAppId || fromEnv?.source === 'env' || fromEnv?.source === 'env-file' || fromEnv?.source === 'env-generic')) {
    bilingual(
      'setupProbing',
      { origin: flagAppId ? zh.setupSourceFlag : fromEnv!.origin },
      { origin: flagAppId ? en.setupSourceFlag : fromEnv!.origin },
    );
    const probe = createLarkChannel({ appId: manualId, appSecret: manualSecret });
    let ownerOpenId: string | undefined;
    try {
      const info = await probe.getAppInfo();
      ownerOpenId = info.ownerId;
      bilingual('setupProbeOk', { app: info.appName ?? zh.setupUnnamedApp }, { app: info.appName ?? en.setupUnnamedApp });
    } catch (err) {
      die(3, both('setupProbeFailed', { error: describeError(err) }));
    }
    const where = writeCreds({ appId: manualId, appSecret: manualSecret, ownerOpenId }, store);
    bilingual('setupSaved', { where });
    bilingual('setupNext');
    process.stdout.write(msg.setupNextLines);
    return;
  }
  if (flagAppId)
    die(
      4,
      `${both('setupNoSecret')}\n` +
        `  AGENT_LARK_APP_SECRET=... agent-lark setup --app-id ${flagAppId}\n` +
        `  ${both('setupNoSecretEnvFile', { file: envFile() })}`,
    );

  const scopes = (opt(args, 'scopes')?.split(',').map((s) => s.trim()).filter(Boolean)) ?? DEFAULT_SCOPES;
  bilingual('setupRequesting');

  let deadline = 0;
  let lastStatus = '';
  let heartbeat: NodeJS.Timeout | undefined;

  // The SDK polls for the scan itself and cannot resume a user code after a
  // dropped connection, so a network failure mid-wait means a fresh QR code.
  const register = () =>
    registerApp({
      source: 'agent-lark',
      appId: update && existing ? existing.appId : undefined,
      appPreset: {
        name: 'agent-lark',
        desc: both('appDesc'),
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
            bilingual('setupScan');
            process.stdout.write(`${url}\n\n`);
            bilingual('setupScopes');
            process.stdout.write(`  ${scopes.join('\n  ')}\n  ${both('setupEvents')}\n\n`);
            bilingual('setupExpiry', { minutes: Math.round(expireIn / 60), time: new Date(deadline).toLocaleTimeString() });
          });
        // One line a minute instead of one every two seconds.
        heartbeat = setInterval(() => {
          const left = Math.max(0, Math.round((deadline - Date.now()) / 1000));
          bilingual('setupWaiting', { seconds: left });
        }, 60_000);
        heartbeat.unref();
      },
      // 'polling' repeats every couple of seconds; only report real changes.
      onStatusChange: (s) => {
        if (s.status === lastStatus) return;
        lastStatus = s.status;
        bilingual('setupStatus', { status: s.status });
      },
    });

  let result: Awaited<ReturnType<typeof registerApp>> | undefined;
  for (let attempt = 1; !result; attempt++) {
    try {
      result = await register();
    } catch (err) {
      const detail = describeError(err);
      if (deadline && Date.now() >= deadline - 5000) die(4, both('setupExpired', { error: detail }));
      if (attempt >= SETUP_ATTEMPTS || !isTransientNetworkError(err)) die(3, both('setupRegisterFailed', { error: detail }));
      bilingual('setupRetry', { error: detail, n: attempt + 1, max: SETUP_ATTEMPTS });
      deadline = 0;
      lastStatus = '';
    } finally {
      clearInterval(heartbeat);
    }
  }

  const where = writeCreds(
    {
      appId: result.client_id,
      appSecret: result.client_secret,
      ownerOpenId: result.user_info?.open_id,
      brand: result.user_info?.tenant_brand,
    },
    store,
  );
  process.stdout.write('\n');
  bilingual('setupSavedQr', { where });
  bilingual('setupNext');
  process.stdout.write(msg.setupNextLines);
}

// ---------------------------------------------------------------- daemon

/** Is a daemon already answering on this state dir's endpoint? */
const daemonAlive = (): Promise<boolean> => isDaemonListening(2000);

/**
 * Start the daemon in its own session. It must outlive the caller: started as
 * a child of a shell command it would die with it, and every message the human
 * sends afterwards would be lost with no error on their side.
 */
async function startDaemonDetached(): Promise<{ ok: boolean; message: string }> {
  if (await daemonAlive()) return { ok: true, message: msg.daemonAlready };
  ensureHomeDir();
  const out = openSync(logPath(), 'a');
  const self = fileURLToPath(import.meta.url);
  const child = spawn(process.execPath, [self, 'daemon'], { detached: true, stdio: ['ignore', out, out] });
  child.unref();
  const deadline = Date.now() + 10_000;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 250));
    if (await isDaemonListening(1000)) return { ok: true, message: fill(msg.daemonStarted, { pid: child.pid ?? '?', log: logPath() }) };
  }
  return { ok: false, message: fill(msg.daemonNoReply, { log: logPath() }) };
}


async function cmdDaemon(args: string[]): Promise<void> {
  if (flag(args, 'status')) {
    const res = await request({ type: 'ping' }, { timeoutMs: 5000 });
    if (!res.ok) die(1, res.reason === 'down' ? msg.daemonNotRunning : fill(msg.daemonNoAnswer, { message: res.message }));
    if (res.kind !== 'pong') die(1, msg.daemonWeird);
    const s = res.status;
    process.stdout.write(
      `${fill(msg.daemonStatusLine, { pid: s.pid, connected: String(s.connected), connection: s.connection, pending: s.pendingAsks, bindings: s.bindings, startedAt: s.startedAt })}\n`,
    );
    if (s.lastError) process.stdout.write(`${fill(msg.daemonLastError, { error: s.lastError })}\n`);
    const mb = (s.media.bytes / 1024 / 1024).toFixed(1);
    process.stdout.write(
      `${s.media.ttlDays === 0 ? fill(msg.daemonMediaLineOff, { mb, files: s.media.files, at: s.media.at }) : fill(msg.daemonMediaLine, { ttl: s.media.ttlDays, mb, files: s.media.files, at: s.media.at })}\n`,
    );
    return;
  }
  if (flag(args, 'stop')) {
    if (!(await daemonAlive())) {
      if (existsSync(pidPath())) unlinkSync(pidPath());
      process.stdout.write(`${msg.daemonWasNotRunning}\n`);
      return;
    }
    // Stopping cancels every waiting question, which leaves a dead card on
    // someone's phone. Refuse unless the caller says that is what they want.
    if (!flag(args, 'force')) {
      const probe = await request({ type: 'ping' }, { timeoutMs: 5000 });
      if (probe.ok && probe.kind === 'pong' && probe.status.pendingAsks > 0)
        die(4, fill(msg.daemonStopRefused, { n: probe.status.pendingAsks }));
    }
    const res = await request({ type: 'stop' }, { timeoutMs: 5000 });
    if (!res.ok) die(3, res.message);
    // Gone means it stopped answering, not that some file disappeared.
    const deadline = Date.now() + 10_000;
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 250));
      if (!(await isDaemonListening(1000))) {
        process.stdout.write(`${msg.daemonStopped}\n`);
        return;
      }
    }
    die(3, fill(msg.daemonStopStuck, { log: logPath() }));
  }
  if (flag(args, 'detach')) {
    const r = await startDaemonDetached();
    if (!r.ok) die(3, r.message);
    process.stdout.write(`${r.message}\n`);
    return;
  }
  const { runDaemon, DaemonStartError } = await import('./daemon.js');
  let daemon;
  try {
    daemon = await runDaemon();
  } catch (err) {
    if (err instanceof DaemonStartError) die(err.code, err.message);
    throw err;
  }
  process.stdout.write(`${fill(msg.daemonReady, { pid: process.pid, sock: ipcEndpoint() })}\n`);
  await daemon.done;
  process.exit(0);
}

// ---------------------------------------------------------------- bind / unbind / rename

/** The group-choosing flags shared by `bind` and `away on`. */
function bindArgs(args: string[]): Pick<Request & { type: 'bind' }, 'name' | 'mode' | 'reuseChatId'> {
  const name = opt(args, 'name');
  if (name !== undefined) {
    const problem = taskNameProblem(name);
    if (problem) die(1, problem);
  }
  const reuse = opt(args, 'reuse');
  if (reuse && flag(args, 'new')) die(1, msg.bindModeConflict);
  return { name, mode: reuse ? 'reuse' : flag(args, 'new') ? 'new' : undefined, reuseChatId: reuse };
}

/**
 * A bind refused with earlier groups on offer: list them, one per line, and
 * say how to rerun. The choice is the human's — the agent relays it.
 */
function dieBind(res: Extract<Response, { ok: false }>): never {
  if (res.code === 4 && res.candidates?.length) {
    process.stderr.write(`${msg.prefix}${res.message}\n`);
    for (const c of res.candidates)
      process.stderr.write(
        `${fill(msg.bindCandidateLine, { name: c.name ?? msg.bindCandidateUnnamed, time: c.releasedAt ?? msg.bindCandidateNever, chatId: c.chatId })}\n`,
      );
    process.stderr.write(`${msg.bindCandidateHint}\n`);
    process.exit(4);
  }
  die(res.code, res.message);
}

async function cmdBind(args: string[]): Promise<void> {
  const { root, label, paneId } = ctx();
  const res = await request(
    { type: 'bind', root, label, paneId, chatId: opt(args, 'chat'), ...bindArgs(args) },
    { onNote: (text) => process.stderr.write(`note: ${text}\n`) },
  );
  if (!res.ok) dieBind(res);
  finish(res, (r) => {
    if (r.kind !== 'bind') return;
    writeProjectState(root, { chatId: r.chatId }, { create: true });
    const line =
      r.how === 'created'
        ? fill(msg.bindCreated, { name: r.name, root })
        : r.how === 'reused'
          ? fill(msg.bindReused, { name: r.name, root })
          : r.how === 'existing'
            ? fill(msg.bindKept, { name: r.name, root })
            : fill(msg.bindExisting, { chatId: r.chatId, root });
    process.stdout.write(`${line}\n`);
  });
}

async function cmdUnbind(): Promise<void> {
  const { root } = ctx();
  const res = await request({ type: 'unbind', root });
  finish(res, (r) => {
    writeProjectState(root, { chatId: null, away: false });
    process.stdout.write(`${fill(msg.unbound, { name: r.kind === 'unbind' ? r.name : '' })}\n`);
  });
}

async function cmdRename(args: string[]): Promise<void> {
  const name = args.find((a) => !a.startsWith('--'));
  if (!name?.trim()) die(1, msg.renameUsage);
  const problem = taskNameProblem(name);
  if (problem) die(1, problem);
  const { root, paneId } = ctx();
  const res = await request({ type: 'rename', root, paneId, name });
  finish(res, (r) => process.stdout.write(`${fill(msg.renamed, { name: r.kind === 'rename' ? r.name : name })}\n`));
}

// ---------------------------------------------------------------- ask / notify

async function cmdAsk(args: string[]): Promise<void> {
  const { root, label, paneId } = ctx();
  const raw = await readStdin();
  let payload: unknown;
  try {
    payload = JSON.parse(raw);
  } catch (err) {
    die(1, fill(msg.badJson, { error: err instanceof Error ? err.message : String(err) }));
  }
  // Validation runs here, before anything is sent: a malformed question must
  // fail the same way whether or not the daemon happens to be up.
  try {
    validateAsk(payload);
  } catch (err) {
    if (err instanceof ValidationError)
      die(1, `${fill(msg.askProblems, { n: err.problems.length })}\n  ${err.problems.join('\n  ')}`);
    throw err;
  }
  const seconds = Number(opt(args, 'timeout') ?? 43_200);
  if (!Number.isFinite(seconds) || seconds <= 0) die(1, msg.timeoutArg);
  const res = await request(
    { type: 'ask', root, label, paneId, payload, timeoutMs: seconds * 1000, urgent: flag(args, 'urgent') },
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
    die(1, fill(msg.badJson, { error: err instanceof Error ? err.message : String(err) }));
  }
  try {
    validateNotify(payload);
  } catch (err) {
    if (err instanceof ValidationError)
      die(1, `${fill(msg.notifyProblems, { n: err.problems.length })}\n  ${err.problems.join('\n  ')}`);
    throw err;
  }
  const res = await request({ type: 'notify', root, label, paneId, payload });
  finish(res, () => process.stdout.write(`${msg.notifySent}\n`));
}

async function cmdSendFile(args: string[]): Promise<void> {
  const { root, label, paneId } = ctx();
  const path = args.find((a) => !a.startsWith('--'));
  if (!path) die(1, msg.sendFileUsage);
  const res = await request({ type: 'sendFile', root, label, paneId, path, caption: opt(args, 'caption') });
  finish(res, () => process.stdout.write(`${msg.fileSent}\n`));
}

// ---------------------------------------------------------------- away / status

/** How long `away on` waits for the daemon's Feishu handshake before giving up. */
const CONNECT_WAIT_MS = 15_000;

/**
 * Keep pinging until the daemon reports `connected`, or the deadline passes —
 * then the last connect error it gave is returned. The ping is a parameter so
 * the give-up path can be exercised without a daemon.
 */
export async function waitConnected(
  ping: () => Promise<Response>,
  deadlineMs: number,
  pollMs = 250,
): Promise<{ connected: true } | { connected: false; error: string }> {
  const deadline = Date.now() + deadlineMs;
  let error: string = msg.connecting;
  for (;;) {
    const res = await ping();
    if (res.ok && res.kind === 'pong') {
      if (res.status.connected) return { connected: true };
      error = res.status.lastError ?? msg.connecting;
    } else if (!res.ok) error = res.message;
    if (Date.now() >= deadline) return { connected: false, error };
    await new Promise((r) => setTimeout(r, pollMs));
  }
}

async function cmdAway(args: string[]): Promise<void> {
  const sub = args.find((a) => !a.startsWith('--')) ?? 'status';
  const { root, label, paneId } = ctx();
  if (sub === 'status') {
    const state = readProjectState(root);
    if (flag(args, 'json')) {
      process.stdout.write(`${JSON.stringify(state ?? { away: false, chatId: null, target: root, updated: '' })}\n`);
      return;
    }
    if (!state) {
      process.stdout.write(`${msg.awayNeverUsed}\n`);
      return;
    }
    process.stdout.write(`${fill(msg.awayStatusLine, { away: state.away ? msg.on : msg.off, chat: state.chatId ?? msg.awayUnbound })}\n`);
    return;
  }
  if (sub !== 'on' && sub !== 'off') die(1, msg.awayUsage);
  const away = sub === 'on';
  let chatId: string | undefined;
  if (away) {
    // Everything the channel needs, in one command: credentials, a live
    // daemon that reached Feishu, and a group for this project. Asking the
    // user to run three commands in order is how a channel ends up switched
    // half-on.
    const choice = bindArgs(args);
    if (!resolveCreds()) die(4, msg.awayNoCreds);
    const d = await startDaemonDetached();
    if (!d.ok) die(3, d.message);
    process.stdout.write(`${d.message}\n`);
    const link = await waitConnected(() => request({ type: 'ping' }, { timeoutMs: 2000 }), CONNECT_WAIT_MS);
    if (!link.connected) die(3, fill(msg.awayNotConnected, { error: link.error }));
    const bindRes = await request({ type: 'bind', root, label, paneId, ...choice }, { onNote: (text) => process.stderr.write(`note: ${text}\n`) });
    if (!bindRes.ok) dieBind(bindRes);
    if (bindRes.kind === 'bind') {
      chatId = bindRes.chatId;
      const line =
        bindRes.how === 'created'
          ? fill(msg.awayCreated, { name: bindRes.name })
          : bindRes.how === 'reused'
            ? fill(msg.awayReused, { name: bindRes.name })
            : bindRes.how === 'existing'
              ? fill(msg.awayKept, { name: bindRes.name })
              : fill(msg.awayBoundChat, { chatId: bindRes.chatId });
      process.stdout.write(`${line}\n`);
    }
  }
  const res = await request({ type: 'setAway', root, away, paneId });
  finish(res, () => {
    writeProjectState(root, chatId ? { away, chatId } : { away }, { create: away });
    process.stdout.write(`${away ? msg.awayOn : msg.awayOff}\n`);
    if (away && !insideHerdr()) process.stdout.write(`${msg.awayOutsideHerdr}\n`);
  });
}

async function cmdStatus(): Promise<void> {
  const creds = resolveCreds();
  process.stdout.write(`${creds ? fill(msg.statusCredsYes, { origin: creds.origin }) : msg.statusCredsNo}\n`);
  for (const line of credsReport()) process.stdout.write(`  ${line}\n`);
  process.stdout.write(`${insideHerdr() ? fill(msg.statusHerdrIn, { pane: currentPaneId() ?? '?' }) : msg.statusHerdrOut}\n`);
  const ping = await request({ type: 'ping' }, { timeoutMs: 5000 });
  if (!ping.ok) {
    process.stdout.write(`${msg.statusDaemonDown}\n`);
    return;
  }
  if (ping.kind === 'pong') {
    process.stdout.write(
      `${fill(msg.statusDaemonLine, { pid: ping.status.pid, connected: String(ping.status.connected), connection: ping.status.connection, pending: ping.status.pendingAsks })}\n`,
    );
    if (ping.status.lastError) process.stdout.write(`${fill(msg.daemonLastError, { error: ping.status.lastError })}\n`);
  }
  const list = await request({ type: 'list' }, { timeoutMs: 5000 });
  if (list.ok && list.kind === 'list') {
    const live = list.bindings.filter((b) => b.releasedAt === null);
    const released = list.bindings.filter((b) => b.releasedAt !== null);
    const here = projectRoot();
    const mark = (root: string): string => (root === here ? '*' : ' ');
    if (!live.length) process.stdout.write(`${msg.statusNoBindings}\n`);
    else {
      process.stdout.write(`${msg.statusBindings}\n`);
      for (const b of live)
        process.stdout.write(
          `${fill(msg.statusBindingLine, { mark: mark(b.root), root: b.root, name: b.name ?? msg.bindCandidateUnnamed, chatId: b.chatId, away: String(b.away), pane: b.paneId ?? '-' })}\n`,
        );
    }
    if (released.length) {
      process.stdout.write(`${msg.statusReleased}\n`);
      for (const b of released)
        process.stdout.write(
          `${fill(msg.statusReleasedLine, { mark: mark(b.root), root: b.root, name: b.name ?? msg.bindCandidateUnnamed, chatId: b.chatId, time: b.releasedAt ?? '' })}\n`,
        );
    }
  }
}

// ---------------------------------------------------------------- main

/**
 * Global `--home <dir>`: taken out of argv once, here, and handed down as
 * AGENT_LARK_HOME — the daemon started with --detach inherits the environment,
 * so every process on this machine agrees on where the state lives.
 */
function takeHome(argv: string[]): string[] {
  const i = argv.findIndex((a) => a === '--home' || a.startsWith('--home='));
  if (i < 0) return argv;
  const joined = argv[i]!.startsWith('--home=');
  const dir = joined ? argv[i]!.slice('--home='.length) : argv[i + 1];
  if (!dir || (!joined && dir.startsWith('--'))) die(1, msg.homeNeedsDir);
  process.env.AGENT_LARK_HOME = resolve(dir);
  return [...argv.slice(0, i), ...argv.slice(i + (joined ? 1 : 2))];
}

async function main(): Promise<void> {
  const [cmd, ...args] = takeHome(process.argv.slice(2));
  switch (cmd) {
    case 'setup':
      return cmdSetup(args);
    case 'daemon':
      return cmdDaemon(args);
    case 'bind':
      return cmdBind(args);
    case 'unbind':
      return cmdUnbind();
    case 'rename':
      return cmdRename(args);
    case 'ask':
      return cmdAsk(args);
    case 'notify':
      return cmdNotify();
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
      die(1, fill(msg.unknownCommand, { cmd }));
  }
}

// Only run when invoked as the program; importing this module (tests, tooling)
// must not execute a command.
const isEntry = process.argv[1] && fileURLToPath(import.meta.url) === realpathSync(process.argv[1]);
if (isEntry)
  main().catch((err: unknown) => {
    const detail = err instanceof Error ? (err.stack ?? err.message) : describeError(err);
    process.stderr.write(`${msg.prefix}${detail}\n`);
    process.exit(3);
  });
