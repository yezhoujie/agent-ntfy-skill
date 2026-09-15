#!/usr/bin/env node
import { spawn } from 'node:child_process';
import { existsSync, openSync, realpathSync, unlinkSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createLarkChannel, registerApp } from '@larksuite/channel';
import QRCode from 'qrcode';
import { clearCreds, credsReport, defaultStore, resolveCreds, writeCreds, type StoreKind } from './creds.js';
import { closePane, currentPaneId, insideHerdr, promptPane, quoteForPaneShell, runInPane, splitPane, type HerdrRun } from './herdr.js';
import { InputInterrupted, terminalIO, type SetupIO } from './tty.js';
import { taskNameProblem } from './bindings.js';
import { isDaemonListening, request, type Request, type Response } from './ipc.js';
import { ensureHomeDir, homeDir, ipcEndpoint, logPath, pidPath, projectLabel, projectRoot, readProjectState, writeProjectState } from './paths.js';
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

/** Replace every occurrence of `secret` in `text` with `***`; an empty secret masks nothing. */
export function maskSecret(text: string, secret: string): string {
  return secret ? text.split(secret).join('***') : text;
}

/**
 * Why a credential probe was refused, for the human: Feishu's own code and
 * message when the SDK carried a response body (that is where they live),
 * otherwise whatever the error says. Never the secret — it is not in the error.
 */
export function describeProbeError(err: unknown): string {
  const data = (err as { response?: { data?: { code?: unknown; msg?: unknown } } } | null)?.response?.data;
  if (data && typeof data.code === 'number' && data.code !== 0) return `Feishu error ${data.code} ${typeof data.msg === 'string' ? data.msg : ''}`.trim();
  return describeError(err);
}

function die(code: number, text: string): never {
  process.stderr.write(`${msg.prefix}${text}\n`);
  process.exit(code);
}

/**
 * Every `--option` a subcommand accepts, by name. Anything else on its
 * command line is refused before the command does a thing: a misspelt or
 * unknown option is never silently ignored (`--app-id`, for one, is not an
 * option of `setup`).
 */
const OPTIONS: Record<string, { flags: string[]; opts: string[] }> = {
  setup: { flags: ['update', 'reset', 'reuse', 'close-pane'], opts: ['scopes', 'report-to'] },
  daemon: { flags: ['detach', 'status', 'stop', 'force'], opts: [] },
  bind: { flags: ['new'], opts: ['chat', 'name', 'reuse'] },
  unbind: { flags: ['dissolve'], opts: [] },
  rename: { flags: [], opts: [] },
  ask: { flags: ['urgent'], opts: ['timeout'] },
  notify: { flags: [], opts: [] },
  'send-file': { flags: [], opts: ['caption'] },
  'away on': { flags: ['new'], opts: ['name', 'reuse'] },
  'away off': { flags: [], opts: [] },
  'away status': { flags: ['json'], opts: [] },
  status: { flags: [], opts: [] },
  help: { flags: [], opts: [] },
};
// Consulted to find `away`'s subcommand itself (`away --name x on`), before
// the subcommand's own entry — the one that gets enforced — is known.
OPTIONS.away = {
  flags: [],
  opts: [...new Set(Object.entries(OPTIONS).flatMap(([k, v]) => (k.startsWith('away ') ? v.opts : [])))],
};

export interface Argv {
  /** `--name` given as a flag. */
  flag(name: string): boolean;
  /** The value given to `--name`; undefined when the option is absent or the last token. */
  opt(name: string): string | undefined;
  /** The first argument that is neither an option nor an option's value. */
  positional(): string | undefined;
}

/**
 * `command`'s arguments read by one rule: an option the command takes a value
 * for (`OPTIONS[command].opts`) owns the next token, whatever it looks like;
 * every other `--x` is a flag; the rest are positionals. Reading through this
 * everywhere is what keeps `away on --name --new` naming the group `--new`
 * instead of also switching modes.
 */
export function argv(command: string, args: string[]): Argv {
  const valued = OPTIONS[command]?.opts ?? [];
  const flags = new Set<string>();
  const opts = new Map<string, string | undefined>();
  const positionals: string[] = [];
  for (let i = 0; i < args.length; i++) {
    const a = args[i]!;
    if (!a.startsWith('--')) {
      positionals.push(a);
      continue;
    }
    const name = a.slice(2);
    if (valued.includes(name)) {
      if (!opts.has(name)) opts.set(name, args[i + 1]);
      i += 1;
    } else flags.add(name);
  }
  return { flag: (name) => flags.has(name), opt: (name) => opts.get(name), positional: () => positionals[0] };
}

/** Exit 1 on the first `--option` that `command` does not know (`--home` was taken out of argv already). */
function rejectUnknownOptions(command: string, args: string[]): void {
  const known = OPTIONS[command]?.flags ?? [];
  const a = argv(command, args);
  for (const token of args) {
    const name = token.slice(2);
    if (token.startsWith('--') && a.flag(name) && !known.includes(name)) die(1, fill(msg.unknownOption, { option: token }));
  }
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

/** App IDs are `cli_` plus letters and digits; anything else is a typo, not a candidate for the probe. */
const APP_ID_RE = /^cli_[A-Za-z0-9]+$/;
/** How many failed credential probes the reuse branch tolerates before giving up. */
const REUSE_ATTEMPTS = 3;
/** Prefix of the one line `--report-to` injects back; protocol, never translated. */
const SETUP_REPORT_PREFIX = '[agent-lark] setup:';

/** What the interactive setup borrows from the outside world; tests inject every piece. */
export interface SetupDeps {
  io: SetupIO;
  /** Check a credential pair against Feishu (`getAppInfo`); resolves with what the app says about itself. */
  probe: (appId: string, appSecret: string) => Promise<{ appName?: string; ownerId?: string }>;
  register: typeof registerApp;
  /** `AGENT_LARK_OFFLINE=1`: never contact Feishu — the test suite sets it, so no test can register an app or probe credentials by accident. */
  offline: boolean;
  herdr: {
    insideHerdr: typeof insideHerdr;
    currentPaneId: typeof currentPaneId;
    splitPane: (cwd: string, pane: string) => Promise<string | null>;
    runInPane: (pane: string, argv: string[]) => Promise<boolean>;
    promptPane: typeof promptPane;
    closePane: (pane: string) => Promise<HerdrRun>;
  };
  out: (text: string) => void;
  err: (text: string) => void;
  /** The interpreter and this CLI's own path: what a new pane is told to run. */
  execPath: string;
  cliPath: string;
  cwd: string;
}

/** Ends `runSetup` from anywhere inside it with this exit code; `reported` means the pane has already sent its report line. */
class SetupExit extends Error {
  constructor(
    readonly code: number,
    readonly reason: string,
    readonly reported = false,
  ) {
    super(`setup exit ${code}: ${reason}`);
  }
}

/**
 * `setup`: create the Feishu app by QR code, or take over an app that already
 * exists. Returns the exit code instead of exiting, so tests can run it in
 * process with a scripted terminal.
 *
 * Every wording line is printed in both languages: a human is at the
 * terminal for setup, whatever language they read.
 */
export async function runSetup(args: string[], deps: SetupDeps): Promise<number> {
  const say = (key: Parameters<typeof both>[0], vars: Record<string, string | number> = {}, varsEn = vars): void =>
    deps.out(`${both(key, vars, varsEn)}\n`);
  const fail = (code: number, text: string): never => {
    deps.err(`${msg.prefix}${text}\n`);
    throw new SetupExit(code, text);
  };
  // The secret typed so far, so that whatever text leaves through the report
  // line can be masked — including an error nobody anticipated.
  const typed = { secret: '' };
  const a = argv('setup', args);
  const update = a.flag('update');
  const reuse = a.flag('reuse');
  const reportTo = a.opt('report-to');
  const closeAfter = a.flag('close-pane');
  const scopes = (a.opt('scopes')?.split(',').map((s) => s.trim()).filter(Boolean)) ?? DEFAULT_SCOPES;

  // The result reaches the agent that opened this pane as one line, whatever
  // the outcome — a pane it cannot see is otherwise a black box to it.
  const report = async (line: string): Promise<void> => {
    if (!reportTo) return;
    const r = await deps.herdr.promptPane(reportTo, line);
    if (!r.ok) deps.err(`${fill(msg.setupReportNotDelivered, { pane: reportTo, why: `${r.code ?? '?'} ${r.message ?? ''}`.trim() })}\n`);
  };

  try {
    if (a.flag('reset')) clearCreds();
    const existing = resolveCreds();
    // Credentials sitting only in the environment are not yet *configured* —
    // setup's job is to verify and persist them, so only a store short-circuits.
    const alreadyPersisted = existing?.source === 'keychain' || existing?.source === 'file';
    if (alreadyPersisted && !update && !a.flag('reset')) {
      say('setupHaveCreds', { origin: existing.origin });
      await report(fill(msg.setupReportExists, { origin: existing.origin }));
      return 0;
    }
    const store = defaultStore();

    let branch: 'qr' | 'reuse' = reuse ? 'reuse' : 'qr';
    if (!reuse && deps.io.isTTY) {
      // A menu only where someone can answer it; an agent's captured stdin
      // goes straight to the QR code, as before.
      say('setupMenu');
      for (;;) {
        const pick = (await deps.io.question(both('setupMenuPrompt'))).trim();
        if (pick === '1' || pick === '2') {
          branch = pick === '1' ? 'qr' : 'reuse';
          break;
        }
        say('setupMenuBad');
      }
    }

    if (branch === 'reuse') {
      if (!deps.io.isTTY) return handOff(deps);
      await runReuse(deps, store, say, fail, report, closeAfter, scopes, typed);
      return 0;
    }
    if (deps.offline) fail(3, msg.offline);
    await runQr(deps, store, say, fail, update ? existing?.appId : undefined, scopes);
    return 0;
  } catch (err) {
    // Whatever ended the run, the caller's pane gets one line: a report that
    // never comes leaves the agent waiting for it.
    if (err instanceof SetupExit) {
      if (err.code === 130) await report(msg.setupReportInterrupted);
      else if (!err.reported) await report(fill(msg.setupReportFailed, { why: maskSecret(err.reason, typed.secret) }));
      return err.code;
    }
    if (err instanceof InputInterrupted) {
      deps.out('\n');
      await report(msg.setupReportInterrupted);
      return 130;
    }
    await report(fill(msg.setupReportFailed, { why: maskSecret(describeError(err), typed.secret) }));
    throw err;
  } finally {
    deps.io.close();
  }
}

/**
 * The reuse branch cannot ask anything without a terminal. Inside herdr the
 * CLI opens a pane below the caller's and runs itself there, so the secret is
 * typed where the agent cannot read it; elsewhere the human has to run it.
 */
async function handOff(deps: SetupDeps): Promise<number> {
  const argv = [deps.execPath, deps.cliPath, '--home', homeDir(), 'setup', '--reuse'];
  const pane = deps.herdr.insideHerdr() ? deps.herdr.currentPaneId() : null;
  const command = quoteForPaneShell(argv);
  if (!pane) {
    deps.err(`${msg.prefix}${fill(msg.setupReuseNeedsTerminal, { command })}\n`);
    return 4;
  }
  const opened = await deps.herdr.splitPane(deps.cwd, pane);
  if (!opened) {
    deps.err(`${msg.prefix}${fill(msg.setupHandoffFailed, { why: 'pane split failed', command })}\n`);
    return 3;
  }
  const typed = await deps.herdr.runInPane(opened, [...argv, '--report-to', pane, '--close-pane']);
  if (!typed) {
    deps.err(`${msg.prefix}${fill(msg.setupHandoffFailed, { why: `pane run failed in ${opened}`, command })}\n`);
    return 3;
  }
  deps.out(`${fill(msg.setupHandoffStarted, { pane: opened })}\n`);
  return 0;
}

async function runReuse(
  deps: SetupDeps,
  store: StoreKind,
  say: (key: Parameters<typeof both>[0], vars?: Record<string, string | number>, varsEn?: Record<string, string | number>) => void,
  fail: (code: number, text: string) => never,
  report: (line: string) => Promise<void>,
  closeAfter: boolean,
  scopes: string[],
  typed: { secret: string },
): Promise<void> {
  for (let attempt = 1; ; attempt++) {
    let appId: string;
    for (;;) {
      appId = (await deps.io.question(both('setupAppIdPrompt'))).trim();
      if (APP_ID_RE.test(appId)) break;
      say('setupAppIdBad');
    }
    // The secret is read hidden and travels only into the probe and the
    // store: never argv, never a log line, never an error message.
    const appSecret = (await deps.io.questionHidden(both('setupSecretPrompt'))).trim();
    typed.secret = appSecret;
    if (deps.offline) fail(3, msg.offline);
    say('setupProbing');
    let info: { appName?: string; ownerId?: string };
    try {
      info = await deps.probe(appId, appSecret);
    } catch (err) {
      // Whatever the SDK put in its error text, the secret does not leave this
      // function through it: not to the terminal, not into the report line.
      const why = maskSecret(describeProbeError(err), appSecret);
      say('setupProbeFailed', { error: why });
      if (attempt >= REUSE_ATTEMPTS) {
        const line = both('setupReuseGaveUp', { n: attempt });
        await report(fill(msg.setupReportFailed, { why: `${attempt} probes refused (${why})` }));
        deps.err(`${msg.prefix}${line}\n`);
        throw new SetupExit(1, line, true);
      }
      continue;
    }
    const app = info.appName ?? '';
    say('setupProbeOk', { app: app || zh.setupUnnamedApp }, { app: app || en.setupUnnamedApp });
    const where = writeCreds({ appId, appSecret, ownerOpenId: info.ownerId }, store);
    say('setupSaved', { where });
    say('setupManualScopes');
    deps.out(`  ${scopes.join('\n  ')}\n`);
    say('setupManualEvents');
    say('setupManualPublish');
    say('setupNext');
    await report(fill(msg.setupReportOk, { appId, app: app || en.setupUnnamedApp }));
    if (closeAfter) await offerClosePane(deps, say);
    return;
  }
}

/** In a pane the CLI opened for the human: ask whether to close it now that setup is done. */
async function offerClosePane(
  deps: SetupDeps,
  say: (key: Parameters<typeof both>[0], vars?: Record<string, string | number>) => void,
): Promise<void> {
  const pane = deps.herdr.insideHerdr() ? deps.herdr.currentPaneId() : null;
  if (!pane) return;
  let answer: string;
  try {
    answer = (await deps.io.question(both('closePanePrompt'))).trim().toLowerCase();
  } catch (err) {
    if (err instanceof InputInterrupted) answer = 'n';
    else throw err;
  }
  if (answer === '' || answer === 'y' || answer === 'yes') {
    const r = await deps.herdr.closePane(pane);
    if (!r.ok) say('paneCloseFailed', { error: r.error ?? 'herdr refused' });
  } else say('paneKept');
}

async function runQr(
  deps: SetupDeps,
  store: StoreKind,
  say: (key: Parameters<typeof both>[0], vars?: Record<string, string | number>, varsEn?: Record<string, string | number>) => void,
  fail: (code: number, text: string) => never,
  updateAppId: string | undefined,
  scopes: string[],
): Promise<void> {
  say('setupRequesting');

  let deadline = 0;
  let lastStatus = '';
  let heartbeat: NodeJS.Timeout | undefined;

  // The SDK polls for the scan itself and cannot resume a user code after a
  // dropped connection, so a network failure mid-wait means a fresh QR code.
  const register = () =>
    deps.register({
      source: 'agent-lark',
      appId: updateAppId,
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
          .then((s) => deps.out(`\n${s}\n`))
          .catch(() => undefined)
          .finally(() => {
            say('setupScan');
            deps.out(`${url}\n\n`);
            say('setupScopes');
            deps.out(`  ${scopes.join('\n  ')}\n  ${both('setupEvents')}\n\n`);
            say('setupExpiry', { minutes: Math.round(expireIn / 60), time: new Date(deadline).toLocaleTimeString() });
          });
        // One line a minute instead of one every two seconds.
        heartbeat = setInterval(() => {
          const left = Math.max(0, Math.round((deadline - Date.now()) / 1000));
          say('setupWaiting', { seconds: left });
        }, 60_000);
        heartbeat.unref();
      },
      // 'polling' repeats every couple of seconds; only report real changes.
      onStatusChange: (s) => {
        if (s.status === lastStatus) return;
        lastStatus = s.status;
        say('setupStatus', { status: s.status });
      },
    });

  let result: Awaited<ReturnType<typeof registerApp>> | undefined;
  for (let attempt = 1; !result; attempt++) {
    try {
      result = await register();
    } catch (err) {
      const detail = describeError(err);
      if (deadline && Date.now() >= deadline - 5000) fail(4, both('setupExpired', { error: detail }));
      if (attempt >= SETUP_ATTEMPTS || !isTransientNetworkError(err)) fail(3, both('setupRegisterFailed', { error: detail }));
      say('setupRetry', { error: detail, n: attempt + 1, max: SETUP_ATTEMPTS });
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
  deps.out('\n');
  say('setupSavedQr', { where });
  say('setupNext');
}

/** The CLI entry: the real terminal, the real Feishu SDK, the real herdr. */
async function cmdSetup(args: string[]): Promise<void> {
  const io: SetupIO = process.stdin.isTTY
    ? terminalIO()
    : {
        isTTY: false,
        question: () => Promise.reject(new InputInterrupted()),
        questionHidden: () => Promise.reject(new InputInterrupted()),
        close: () => undefined,
      };
  const code = await runSetup(args, {
    io,
    probe: async (appId, appSecret) => {
      const info = await createLarkChannel({ appId, appSecret }).getAppInfo();
      return { appName: info.appName, ownerId: info.ownerId };
    },
    register: registerApp,
    offline: process.env.AGENT_LARK_OFFLINE === '1',
    herdr: { insideHerdr, currentPaneId, splitPane, runInPane, promptPane, closePane },
    out: (t) => process.stdout.write(t),
    err: (t) => process.stderr.write(t),
    execPath: process.execPath,
    cliPath: fileURLToPath(import.meta.url),
    cwd: process.cwd(),
  });
  process.exit(code);
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
  const a = argv('daemon', args);
  if (a.flag('status')) {
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
  if (a.flag('stop')) {
    if (!(await daemonAlive())) {
      if (existsSync(pidPath())) unlinkSync(pidPath());
      process.stdout.write(`${msg.daemonWasNotRunning}\n`);
      return;
    }
    // Stopping cancels every waiting question, which leaves a dead card on
    // someone's phone. Refuse unless the caller says that is what they want.
    if (!a.flag('force')) {
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
  if (a.flag('detach')) {
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
function bindArgs(a: Argv): Pick<Request & { type: 'bind' }, 'name' | 'mode' | 'reuseChatId'> {
  const name = a.opt('name');
  if (name !== undefined) {
    const problem = taskNameProblem(name);
    if (problem) die(1, problem);
  }
  const reuse = a.opt('reuse');
  if (reuse && a.flag('new')) die(1, msg.bindModeConflict);
  return { name, mode: reuse ? 'reuse' : a.flag('new') ? 'new' : undefined, reuseChatId: reuse };
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
  const a = argv('bind', args);
  const res = await request(
    { type: 'bind', root, label, paneId, chatId: a.opt('chat'), ...bindArgs(a) },
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

async function cmdUnbind(args: string[]): Promise<void> {
  const { root } = ctx();
  const dissolve = argv('unbind', args).flag('dissolve');
  const res = await request({ type: 'unbind', root, dissolve });
  // A daemon started before this bundle answers the plain way and has let the
  // group go without dissolving it; say so rather than report the wrong outcome.
  if (dissolve && res.ok && res.kind === 'unbind' && res.dissolved === undefined) {
    writeProjectState(root, { chatId: null, away: false });
    die(3, msg.dissolveOldDaemon);
  }
  // Feishu kept the group although the record is gone: the human has to
  // dissolve it there, so this ends like the other "a human must act" cases.
  if (res.ok && res.kind === 'unbind' && res.dissolved === false) {
    writeProjectState(root, { chatId: null, away: false });
    die(4, res.problem ?? '');
  }
  finish(res, (r) => {
    writeProjectState(root, { chatId: null, away: false });
    if (r.kind !== 'unbind') return;
    process.stdout.write(`${r.dissolved ? fill(msg.dissolved, { name: r.name }) : fill(msg.unbound, { name: r.name })}\n`);
  });
}

async function cmdRename(args: string[]): Promise<void> {
  const name = argv('rename', args).positional();
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
  const a = argv('ask', args);
  const seconds = Number(a.opt('timeout') ?? 43_200);
  if (!Number.isFinite(seconds) || seconds <= 0) die(1, msg.timeoutArg);
  const res = await request(
    { type: 'ask', root, label, paneId, payload, timeoutMs: seconds * 1000, urgent: a.flag('urgent') },
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
  const a = argv('send-file', args);
  const given = a.positional();
  if (!given) die(1, msg.sendFileUsage);
  // The daemon checks the path from its own working directory, so a relative
  // one must be resolved here, where the caller meant it.
  const path = resolve(given);
  const res = await request({ type: 'sendFile', root, label, paneId, path, caption: a.opt('caption') });
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
  const sub = argv('away', args).positional() ?? 'status';
  const a = argv(`away ${sub}`, args);
  const { root, label, paneId } = ctx();
  if (sub === 'status') {
    const state = readProjectState(root);
    if (a.flag('json')) {
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
    const choice = bindArgs(a);
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
  if (!away && !res.ok && res.reason === 'down') {
    // Switching off must not need the daemon: the file is what the agent's
    // rule reads, and the daemon's own copy is realigned by the next away on / off.
    const state = writeProjectState(root, { away: false });
    if (!state) process.stdout.write(`${msg.awayNeverUsed}\n`);
    else process.stdout.write(`${msg.awayOff}\n${msg.awayOffLocal}\n`);
    return;
  }
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
  if (cmd === 'away') {
    const sub = argv('away', args).positional() ?? 'status';
    rejectUnknownOptions(`away ${sub}`, args);
  } else if (cmd === undefined || cmd === '--help' || cmd === '-h' || cmd === 'help') rejectUnknownOptions('help', args);
  else if (cmd in OPTIONS) rejectUnknownOptions(cmd, args);
  switch (cmd) {
    case 'setup':
      return cmdSetup(args);
    case 'daemon':
      return cmdDaemon(args);
    case 'bind':
      return cmdBind(args);
    case 'unbind':
      return cmdUnbind(args);
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
