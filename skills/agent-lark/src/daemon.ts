import { appendFileSync, mkdirSync, readFileSync, realpathSync, statSync, writeFileSync, unlinkSync, existsSync } from 'node:fs';
import { createHash, randomUUID } from 'node:crypto';
import { basename, join, sep } from 'node:path';
import { platform, tmpdir } from 'node:os';
import type { Server, Socket } from 'node:net';
import { createLarkChannel, type CardActionEvent, type LarkChannel, type LarkChannelOptions, type NormalizedMessage } from '@larksuite/channel';
import { BindingStore, BindingsFileError, groupName, taskNameProblem, type Binding } from './bindings.js';
import { askCard, notifyCard, receiptCard, statusCard } from './cards.js';
import { resolveCreds } from './creds.js';
import { agentList, findPaneForProject, promptPane } from './herdr.js';
import { isDaemonListening, serve, type Candidate, type Request, type Response } from './ipc.js';
import { ensureHomeDir, homeDir, ipcEndpoint, logPath, pidPath, sockPath } from './paths.js';
import { fill, msg, t } from './texts.js';
import { validateAsk, validateNotify, ValidationError, type AskPayload, type Lang } from './validate.js';

const INJECT_PREFIX = '[agent-lark remote] ';
const POLL_MS = 5_000;
const STATUS_COOLDOWN_MS = 60_000;
const CONNECT_RETRY_MS = 5_000;
const CONNECT_RETRY_MAX_MS = 60_000;
/** How long stop() lets open IPC connections drain before destroying them. */
const CLOSE_GRACE_MS = 2_000;
/** How long stop() waits for Feishu to accept a cancelled card before moving on. */
const CANCEL_CARD_MS = 3_000;

/** The slice of the Feishu channel the daemon actually uses; a test double implements just this. */
export type ChannelLike = Pick<
  LarkChannel,
  | 'on'
  | 'connect'
  | 'disconnect'
  | 'updatePolicy'
  | 'getConnectionStatus'
  | 'send'
  | 'updateCard'
  | 'listChats'
  | 'getChatInfo'
  | 'createChat'
  | 'addReaction'
  | 'downloadResourceToFile'
  | 'rawClient'
>;

/** The slice of the herdr adapter the daemon uses. */
export interface HerdrDeps {
  agentList: typeof agentList;
  promptPane: typeof promptPane;
  findPaneForProject: typeof findPaneForProject;
}

export interface DaemonDeps {
  createChannel?: (opts: LarkChannelOptions) => ChannelLike;
  herdr?: HerdrDeps;
  /** First retry interval after a failed Feishu connect; doubles up to a minute. */
  connectRetryMs?: number;
}

export interface DaemonHandle {
  /** Cancel pending questions, drop Feishu, close IPC, remove pid / socket files. Never exits the process. */
  stop(): Promise<void>;
  /** Resolves once the daemon has fully stopped, whoever asked (stop(), an IPC stop request, a signal). */
  done: Promise<void>;
}

/** The daemon could not start; `code` is the exit code the CLI should use. */
export class DaemonStartError extends Error {
  constructor(
    readonly code: 3 | 4,
    message: string,
  ) {
    super(message);
    this.name = 'DaemonStartError';
  }
}

/** The log records ids and state transitions only — never message bodies. */
function log(event: string, detail: Record<string, unknown> = {}): void {
  const line = `${new Date().toISOString()} ${event} ${JSON.stringify(detail)}\n`;
  try {
    appendFileSync(logPath(), line, { mode: 0o600 });
  } catch {
    // logging must never take the daemon down
  }
}


/** Feishu caps images at 10 MB and files at 30 MB; refuse before uploading. */
const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
const MAX_FILE_BYTES = 30 * 1024 * 1024;

function within(child: string, parent: string): boolean {
  if (child === parent) return true;
  return child.startsWith(parent.endsWith(sep) ? parent : parent + sep);
}

/**
 * A file may only be sent when it resolves (after realpath, so symlinks
 * cannot escape) inside the project that asked, the daemon's own media
 * directory, or a temp dir. Everything else is refused: `send-file` must not
 * become a way to read arbitrary paths out of the machine.
 */
function resolveSendable(
  path: string,
  root: string,
): { real: string; bytes: Buffer } | { error: string } {
  if (!existsSync(path)) return { error: fill(msg.fileMissing, { path }) };
  let real: string;
  try {
    real = realpathSync(path);
  } catch (err) {
    return { error: fill(msg.fileRealpath, { error: String(err) }) };
  }
  const allowed = [root, join(homeDir(), 'media'), tmpdir()]
    .map((d) => {
      try {
        return realpathSync(d);
      } catch {
        return d;
      }
    });
  if (!allowed.some((d) => within(real, d)))
    return { error: fill(msg.fileRefused, { real, root, media: join(homeDir(), 'media'), tmp: tmpdir() }) };
  const st = statSync(real);
  if (!st.isFile()) return { error: fill(msg.fileNotRegular, { real }) };
  const isImage = /\.(png|jpe?g|gif|webp|bmp)$/i.test(real);
  const cap = isImage ? MAX_IMAGE_BYTES : MAX_FILE_BYTES;
  if (st.size > cap)
    return { error: fill(msg.fileTooBig, { size: (st.size / 1024 / 1024).toFixed(1), cap: cap / 1024 / 1024 }) };
  return { real, bytes: readFileSync(real) };
}

interface Pending {
  reqId: string;
  root: string;
  chatId: string;
  messageId: string;
  label: string;
  payload: AskPayload;
  settle: (r: Response) => void;
  timer: NodeJS.Timeout;
  done: boolean;
}

export async function runDaemon(deps: DaemonDeps = {}): Promise<DaemonHandle> {
  const creds = resolveCreds();
  if (!creds) throw new DaemonStartError(4, msg.daemonNoCreds);
  ensureHomeDir();
  // A live daemon must not be duplicated; a dead socket file is not a daemon.
  if (await isDaemonListening(2000)) throw new DaemonStartError(3, msg.daemonAlready);
  const herdr: HerdrDeps = deps.herdr ?? { agentList, promptPane, findPaneForProject };
  const retryMs = deps.connectRetryMs ?? CONNECT_RETRY_MS;

  let bindings: BindingStore;
  try {
    bindings = new BindingStore({
      onRepaired: (info) => log('bindings.repaired', info),
      onDropped: (info) => log('bindings.dropped', info),
    });
  } catch (err) {
    if (err instanceof BindingsFileError) throw new DaemonStartError(4, err.message);
    throw err;
  }
  const pendings = new Map<string, Pending>();
  const lastStatus = new Map<string, string>();
  const lastStatusPush = new Map<string, number>();
  const startedAt = new Date().toISOString();

  const channel: ChannelLike = (deps.createChannel ?? createLarkChannel)({
    appId: creds.appId,
    appSecret: creds.appSecret,
    policy: {
      // Only bound groups are listened to, and inside them no @ is needed —
      // the group IS the project, so every message in it is for this agent.
      groupAllowlist: bindings.activeChatIds(),
      requireMention: false,
      dmMode: creds.ownerOpenId ? 'allowlist' : 'disabled',
      dmAllowlist: creds.ownerOpenId ? [creds.ownerOpenId] : [],
    },
  });

  const refreshPolicy = (): void => {
    channel.updatePolicy({ groupAllowlist: bindings.activeChatIds() });
  };

  const pendingFor = (root: string): Pending | undefined => {
    for (const p of pendings.values()) if (p.root === root && !p.done) return p;
    return undefined;
  };

  /**
   * Close a question: rewrite the card, hand the reply back to the client.
   *
   * `viaCallback` means the tap's own callback response already carries the
   * answered card, so the buttons are gone the moment Feishu renders the
   * reply — no window in which a second tap is possible. The REST update is
   * still issued as a fallback: if the callback response were ever dropped,
   * a card with live buttons on an already-closed question would be worse
   * than a redundant update.
   */
  const answer = async (
    p: Pending,
    reply: string,
    via: 'button' | 'text',
  ): Promise<void> => {
    if (p.done) return;
    p.done = true;
    clearTimeout(p.timer);
    pendings.delete(p.reqId);
    p.settle({ ok: true, kind: 'ask', reply, via });
    log('ask.answered', { reqId: p.reqId, via, root: p.root });
    try {
      await channel.updateCard(
        p.messageId,
        askCard({ payload: p.payload, projectLabel: p.label, reqId: p.reqId, state: 'answered', reply }),
      );
    } catch (err) {
      log('ask.update-failed', { reqId: p.reqId, err: String(err) });
    }
  };

  const closeWithout = async (p: Pending, state: 'timedout' | 'cancelled', res: Response): Promise<void> => {
    if (p.done) return;
    p.done = true;
    clearTimeout(p.timer);
    pendings.delete(p.reqId);
    p.settle(res);
    log(`ask.${state}`, { reqId: p.reqId, root: p.root });
    try {
      await channel.updateCard(
        p.messageId,
        askCard({ payload: p.payload, projectLabel: p.label, reqId: p.reqId, state }),
      );
    } catch (err) {
      log('ask.update-failed', { reqId: p.reqId, err: String(err) });
    }
  };

  const receipt = async (b: Binding, why: string): Promise<void> => {
    try {
      await channel.send(b.chatId, { card: receiptCard(b.label, why) });
    } catch (err) {
      log('receipt.failed', { root: b.root, err: String(err) });
    }
  };

  /** Card-shell wording for the human; the language follows the receipt card. */
  const explainPromptFailure = (code?: string, message?: string, lang: Lang = 'zh'): string => {
    const T = t(lang);
    switch (code) {
      case 'agent_blocked':
        return T.promptAgentBlocked;
      case 'agent_not_found':
      case 'pane_not_found':
        return T.promptPaneGone;
      case 'spawn_failed':
        return T.promptNoHerdr;
      default:
        return fill(T.promptRefused, { code: code ?? '?', message: message ?? '' }).trim();
    }
  };

  /** Deliver a free-standing phone message into the project's pane. */
  const inject = async (b: Binding, text: string): Promise<void> => {
    let paneId = b.paneId;
    if (!paneId) paneId = herdr.findPaneForProject(await herdr.agentList(), b.root);
    if (!paneId) {
      await receipt(b, t('zh').receiptNoPane);
      return;
    }
    const outcome = await herdr.promptPane(paneId, `${INJECT_PREFIX}${text}`);
    log('inject', { root: b.root, paneId, ok: outcome.ok, code: outcome.code });
    if (!outcome.ok) await receipt(b, explainPromptFailure(outcome.code, outcome.message));
  };

  /**
   * Transcribe one voice message. Feishu's file_recognize takes base64 opus
   * and caps at 60 s. Needs the `speech_to_text:speech` scope — without it
   * the call fails and the caller falls back to saying so plainly, which is
   * far better than handing the agent an `<audio .../>` placeholder it cannot
   * read and will silently misinterpret as text.
   */
  const transcribe = async (audioPath: string): Promise<string | null> => {
    try {
      const b64 = readFileSync(audioPath).toString('base64');
      const res = await (channel.rawClient as unknown as {
        speech_to_text: {
          speech: {
            fileRecognize(req: unknown): Promise<{ data?: { recognition_text?: string } }>;
          };
        };
      }).speech_to_text.speech.fileRecognize({
        data: {
          speech: { speech: b64 },
          config: { file_id: createHash('sha1').update(audioPath).digest('hex').slice(0, 16), format: 'opus', engine_type: '16k_auto' },
        },
      });
      const text = res.data?.recognition_text?.trim();
      return text || null;
    } catch (err) {
      log('transcribe.failed', { err: String(err).slice(0, 200) });
      return null;
    }
  };

  /** Save an inbound attachment next to the daemon's state, never in the repo. */
  const saveResources = async (incoming: NormalizedMessage): Promise<{ files: string[]; spoken: string[]; unheard: number }> => {
    const out = { files: [] as string[], spoken: [] as string[], unheard: 0 };
    if (!incoming.resources.length) return out;
    const dir = join(homeDir(), 'media', createHash('sha1').update(incoming.chatId).digest('hex').slice(0, 12));
    mkdirSync(dir, { recursive: true, mode: 0o700 });
    for (const res of incoming.resources) {
      // Only images have their own download type; everything else (files,
      // voice notes, video) comes down the `file` path.
      const kind = res.type === 'image' ? 'image' : 'file';
      const ext = res.type === 'image' ? 'png' : res.type === 'audio' ? 'opus' : 'bin';
      const name = res.fileName ?? `${res.type}-${Date.now()}.${ext}`;
      const dest = join(dir, `${Date.now()}-${name}`);
      try {
        await channel.downloadResourceToFile(incoming.messageId, res.fileKey, kind, dest);
      } catch (err) {
        log('download.failed', { messageId: incoming.messageId, type: res.type, err: String(err).slice(0, 200) });
        continue;
      }
      if (res.type === 'audio') {
        const text = await transcribe(dest);
        if (text) out.spoken.push(text);
        else out.unheard += 1;
        continue;
      }
      out.files.push(dest);
    }
    return out;
  };

  channel.on('message', async (incoming: NormalizedMessage) => {
    if (incoming.senderIsBot) return;
    const b = bindings.activeByChat(incoming.chatId);
    if (!b) return;
    const got = await saveResources(incoming);
    // A voice message arrives as an `<audio .../>` placeholder in `content`.
    // Strip it: either the transcript replaces it, or the human is told
    // plainly that it could not be heard.
    let text = incoming.content.replace(/<audio\b[^>]*\/?>/gi, '').trim();
    if (got.spoken.length) {
      const said = got.spoken.join('\n');
      text = text ? `${text}\n${fill(msg.injectVoice, { text: said })}` : said;
    }
    if (got.unheard) {
      const why = fill(msg.injectUnheard, { n: got.unheard });
      text = text ? `${text}\n${why}` : why;
    }
    if (got.files.length) {
      const list = got.files.map((f) => `  ${f}`).join('\n');
      text = text ? `${text}\n${msg.injectFilesWithText}\n${list}` : `${msg.injectFilesOnly}\n${list}`;
    }
    if (!text) return;
    const p = pendingFor(b.root);
    if (p) {
      await answer(p, text, 'text');
      return;
    }
    await inject(b, text);
  });

  channel.on('cardAction', async (evt: CardActionEvent) => {
    const value = (evt.action.value ?? {}) as { reqId?: string; optionId?: string };
    if (!value.reqId) return;
    const p = pendings.get(value.reqId);
    if (!p || p.done) {
      // A second tap after the question closed: the human is correcting
      // themselves, so it becomes an instruction rather than nothing.
      const b = bindings.activeByChat(evt.chatId);
      if (b) {
        const late = value.optionId ?? '';
        await inject(b, late ? fill(msg.lateTapOption, { id: late }) : msg.lateTapNoOption);
      }
      return { toast: { type: 'info', content: t(p?.payload.lang ?? 'zh').toastClosed } };
    }
    const T = t(p.payload.lang ?? 'zh');
    const opt = p.payload.options.find((o) => o.id === value.optionId);
    if (!opt) return { toast: { type: 'error', content: T.toastBadOption } };
    // Build the closed card before answering, so it can ride back on this very
    // callback: Feishu swaps the card in the same round trip and the buttons
    // are gone before a second tap is possible.
    const closed = askCard({
      payload: p.payload,
      projectLabel: p.label,
      reqId: p.reqId,
      state: 'answered',
      reply: opt.label,
    });
    await answer(p, opt.label, 'button');
    return {
      toast: { type: 'success', content: T.toastAnswered },
      card: { type: 'raw', data: closed },
    };
  });

  // ---- Feishu connection: IPC comes up first, the handshake runs behind it ----
  // A machine that is offline at boot, or a Feishu outage, must not turn the
  // daemon into a crash loop: commands keep answering (with code 3 for the
  // ones that need Feishu) while the connect is retried with backoff.
  let connected = false;
  let lastError: string | null = null;
  let stopping = false;
  let retryTimer: NodeJS.Timeout | undefined;
  let wakeRetry: (() => void) | undefined;

  channel.on('error', (err) => log('channel.error', { code: err.code, message: err.message }));
  channel.on('reconnecting', () => {
    connected = false;
    lastError = msg.reconnecting;
    log('channel.reconnecting');
  });
  channel.on('reconnected', () => {
    connected = true;
    lastError = null;
    log('channel.reconnected');
  });

  const connectLoop = async (): Promise<void> => {
    let delay = retryMs;
    while (!stopping) {
      try {
        await channel.connect();
        if (stopping) return;
        connected = true;
        lastError = null;
        log('daemon.connected', { bindings: bindings.all().length, credSource: creds.source });
        return; // from here on the SDK's own reconnect takes over
      } catch (err) {
        if (stopping) return;
        lastError = err instanceof Error ? err.message : String(err);
        log('channel.connect-failed', { message: lastError, retryMs: delay });
        await new Promise<void>((resolve) => {
          wakeRetry = resolve;
          retryTimer = setTimeout(resolve, delay);
        });
        delay = Math.min(delay * 2, CONNECT_RETRY_MAX_MS);
      }
    }
  };

  /** Feishu answers a refused update with HTTP 200 and a non-zero `code`; the SDK throws only on transport / HTTP errors. */
  const feishuError = (res: unknown): { code: number; msg: string } | null => {
    if (!res || typeof res !== 'object') return null;
    const r = res as { code?: unknown; msg?: unknown };
    if (typeof r.code !== 'number' || r.code === 0) return null;
    return { code: r.code, msg: typeof r.msg === 'string' ? r.msg : '' };
  };

  /**
   * Update a group's name and / or description. The channel SDK has no
   * wrapper for `im.v1.chat.update`, so this goes through the raw client.
   * Only the bot's own groups can be changed freely; a group the human made
   * allows it only when its settings let every member edit group info.
   */
  const updateChat = async (
    chatId: string,
    data: { name?: string; description?: string },
  ): Promise<{ ok: true } | { ok: false; error: string }> => {
    const explain = (e: { code: number; msg: string }): string => {
      const text = fill(msg.renameFailed, { code: e.code, msg: e.msg });
      return [232002, 232016, 232011].includes(e.code) ? `${text}\n${msg.renamePermissionHint}` : text;
    };
    try {
      const res = await (channel.rawClient as unknown as {
        im: { v1: { chat: { update(req: unknown): Promise<unknown> } } };
      }).im.v1.chat.update({ path: { chat_id: chatId }, data });
      const refused = feishuError(res);
      if (refused) {
        log('rename.refused', { chatId, code: refused.code });
        return { ok: false, error: explain(refused) };
      }
      return { ok: true };
    } catch (err) {
      const body = (err as { response?: { data?: unknown } } | null)?.response?.data;
      const refused = feishuError(body) ?? feishuError(err);
      log('rename.failed', { chatId, err: String(err).slice(0, 200) });
      if (refused) return { ok: false, error: explain(refused) };
      return { ok: false, error: fill(msg.renameThrew, { error: err instanceof Error ? err.message : String(err) }) };
    }
  };

  const notConnected = (): Response => ({ ok: false, code: 3, message: fill(msg.notConnected, { error: lastError ?? msg.connecting }) });
  const agents = herdr.agentList;

  // ---- agent state pushes -------------------------------------------------
  const poll = async (): Promise<void> => {
    const away = bindings.activeAll().filter((b) => b.away && b.paneId);
    if (!away.length) return;
    const live = await agents();
    for (const b of away) {
      if (pendingFor(b.root)) continue;
      const a = live.find((x) => x.pane_id === b.paneId);
      if (!a) continue;
      const prev = lastStatus.get(b.root);
      lastStatus.set(b.root, a.agent_status);
      const now = Date.now();
      if (!prev || prev === a.agent_status) continue;
      // Only "stuck on a prompt a human must answer" is worth a push; the end
      // of a turn fires constantly and is noise while the human is at the keyboard.
      if (a.agent_status !== 'blocked') continue;
      if (now - (lastStatusPush.get(b.root) ?? 0) < STATUS_COOLDOWN_MS) continue;
      lastStatusPush.set(b.root, now);
      const detail =
        (a.terminal_title_stripped ? `**${a.terminal_title_stripped}**\n` : '') + fill(t('zh').statusPane, { pane: a.pane_id });
      try {
        await channel.send(b.chatId, { card: statusCard(b.label, detail) });
        log('status.pushed', { root: b.root, kind: 'blocked' });
      } catch (err) {
        log('status.failed', { root: b.root, err: String(err) });
      }
    }
  };
  const pollTimer = setInterval(() => void poll(), POLL_MS);
  pollTimer.unref();

  // ---- IPC ----------------------------------------------------------------
  let server: Server | undefined;
  const sockets = new Set<Socket>();
  let resolveDone!: () => void;
  const done = new Promise<void>((resolve) => {
    resolveDone = resolve;
  });
  const signals = ['SIGINT', 'SIGTERM', 'SIGHUP'] as const;
  const onSignal: Record<(typeof signals)[number], () => void> = {
    SIGINT: () => void stop('SIGINT'),
    SIGTERM: () => void stop('SIGTERM'),
    SIGHUP: () => void stop('SIGHUP'),
  };

  const closeServer = async (): Promise<void> => {
    if (!server) return;
    const srv = server;
    // close() waits for open connections; an `ask` connection ends when its
    // question is cancelled above, but nothing may hang the shutdown forever.
    await new Promise<void>((resolve) => {
      const grace = setTimeout(() => {
        for (const s of sockets) s.destroy();
        log('daemon.close-forced', { sockets: sockets.size });
      }, CLOSE_GRACE_MS);
      srv.close(() => {
        clearTimeout(grace);
        resolve();
      });
    });
  };

  const stop = async (why: string): Promise<void> => {
    if (stopping) return done;
    stopping = true;
    log('daemon.stopping', { why });
    for (const sig of signals) process.off(sig, onSignal[sig]);
    clearInterval(pollTimer);
    if (retryTimer) clearTimeout(retryTimer);
    wakeRetry?.();
    // Release every waiting client first; the card rewrites go out after, each
    // bounded, so a stalled Feishu call cannot hold the shutdown (SIGTERM included).
    const cancelled = [...pendings.values()];
    for (const p of cancelled) {
      p.done = true;
      clearTimeout(p.timer);
      pendings.delete(p.reqId);
      p.settle({ ok: false, code: 3, message: msg.askCancelledStop });
      log('ask.cancelled', { reqId: p.reqId, root: p.root });
    }
    await Promise.all(
      cancelled.map(async (p) => {
        let bound: NodeJS.Timeout | undefined;
        const timeout = new Promise<'timeout'>((resolve) => {
          bound = setTimeout(() => resolve('timeout'), CANCEL_CARD_MS);
        });
        try {
          const outcome = await Promise.race([
            channel.updateCard(p.messageId, askCard({ payload: p.payload, projectLabel: p.label, reqId: p.reqId, state: 'cancelled' })),
            timeout,
          ]);
          if (outcome === 'timeout') log('ask.update-timeout', { reqId: p.reqId });
        } catch (err) {
          log('ask.update-failed', { reqId: p.reqId, err: String(err) });
        } finally {
          clearTimeout(bound);
        }
      }),
    );
    try {
      await channel.disconnect();
    } catch {
      // going down anyway
    }
    await closeServer();
    const leftovers = platform() === 'win32' ? [pidPath()] : [sockPath(), pidPath()];
    for (const f of leftovers) {
      try {
        unlinkSync(f);
      } catch {
        // already gone
      }
    }
    log('daemon.stopped', { why });
    resolveDone();
  };

  server = await serve({
    handle: async (req: Request, ctx): Promise<Response> => {
      switch (req.type) {
        case 'ping':
          return {
            ok: true,
            kind: 'pong',
            status: {
              pid: process.pid,
              connection: channel.getConnectionStatus()?.state ?? 'connecting',
              connected,
              lastError,
              pendingAsks: pendings.size,
              bindings: bindings.activeAll().length,
              startedAt,
            },
          };

        case 'stop':
          // The ack goes out on this connection first; the wind-down starts a tick later.
          setTimeout(() => void stop('stop requested'), 50);
          return { ok: true, kind: 'ack' };

        case 'list':
          return {
            ok: true,
            kind: 'list',
            bindings: bindings.all().map((b) => ({
              root: b.root,
              label: b.label,
              chatId: b.chatId,
              name: b.name,
              paneId: b.paneId,
              away: b.away,
              releasedAt: b.releasedAt,
            })),
          };

        case 'bind': {
          const live = bindings.active(req.root);
          const now = new Date().toISOString();

          // A group named outright: the escape hatch for a group the human made
          // themselves, or for pointing a project back at a group after a reinstall.
          if (req.name !== undefined) {
            const problem = taskNameProblem(req.name);
            if (problem) return { ok: false, code: 1, message: problem };
          }
          const marker = `agent-lark · ${req.root}`;

          if (req.chatId) {
            const switching = live !== undefined && live.chatId !== req.chatId;
            // Switching groups lets go of the live one, and a question waiting
            // there would never get its answer — same rule as unbind.
            if (switching && pendingFor(req.root)) return { ok: false, code: 4, message: msg.unbindPending };
            const holder = bindings.byChat(req.chatId);
            if (holder && holder.releasedAt === null && holder.root !== req.root)
              return { ok: false, code: 1, message: fill(msg.bindChatTaken, { chatId: req.chatId, root: holder.root }) };
            if (switching) bindings.release(req.root);
            // The name is only for showing the group back later; not knowing it is fine.
            let name = holder?.name ?? null;
            if (connected) {
              try {
                name = (await channel.getChatInfo(req.chatId)).name ?? name;
              } catch {
                // keep whatever was on record
              }
            }
            const b: Binding = {
              root: req.root,
              label: req.label,
              chatId: req.chatId,
              name,
              paneId: req.paneId ?? holder?.paneId ?? live?.paneId ?? null,
              away: live?.away ?? false,
              lang: holder?.lang ?? live?.lang ?? null,
              boundAt: now,
              releasedAt: null,
            };
            bindings.set(b);
            refreshPolicy();
            log('bind', { root: req.root, chatId: req.chatId, how: 'chat' });
            // The group now belongs to this project: its description says so
            // (that is how it is found again without local records), and a
            // task name given here is applied in the same call.
            const wanted = req.name !== undefined ? groupName(req.name, req.label) : undefined;
            if (!connected) ctx.note(msg.bindUpdateSkipped);
            else {
              const r = await updateChat(req.chatId, { name: wanted, description: marker });
              if (r.ok && wanted !== undefined) bindings.touch(req.root, { name: wanted });
              else if (!r.ok) ctx.note(fill(msg.bindRenameFailed, { error: r.error }));
            }
            const current = bindings.active(req.root);
            return { ok: true, kind: 'bind', chatId: req.chatId, how: 'chat', name: current?.name ?? req.chatId };
          }

          // The live group stays; a task name given now is applied to it.
          if (live) {
            bindings.touch(req.root, { paneId: req.paneId, label: req.label });
            if (req.mode !== undefined) ctx.note(msg.bindModeIgnored);
            let name = live.name ?? live.chatId;
            if (req.name !== undefined) {
              const wanted = groupName(req.name, req.label);
              const r = await updateChat(live.chatId, { name: wanted });
              if (r.ok) {
                bindings.touch(req.root, { name: wanted });
                name = wanted;
              } else ctx.note(fill(msg.bindRenameFailed, { error: r.error }));
            }
            return { ok: true, kind: 'bind', chatId: live.chatId, how: 'existing', name };
          }

          // No live group. Taking one back or creating one both go through
          // Feishu, so nothing below is attempted while disconnected.
          if (!connected) return notConnected();
          const candidates: Candidate[] = bindings
            .released(req.root)
            .map((b) => ({ chatId: b.chatId, name: b.name, releasedAt: b.releasedAt }));
          // The group may also exist with no local record at all (bindings.json
          // gone with a reinstall). Creating a second group for the same
          // project would split the conversation in two, so the ones the bot
          // made for exactly this project root are offered back too — unless
          // the caller already said which group, or asked for a new one.
          const known = req.mode === 'reuse' && candidates.some((c) => c.chatId === req.reuseChatId);
          if (req.mode !== 'new' && !known) {
            try {
              for (const summary of await channel.listChats()) {
                if (bindings.byChat(summary.id)) continue;
                let info;
                try {
                  info = await channel.getChatInfo(summary.id);
                } catch {
                  continue;
                }
                if (info.description !== marker) continue;
                candidates.push({ chatId: summary.id, name: summary.name || info.name || null, releasedAt: null });
              }
            } catch (err) {
              const error = err instanceof Error ? err.message : String(err);
              log('bind.scan-failed', { root: req.root, err: error });
              ctx.note(fill(msg.bindScanFailed, { error }));
            }
          }

          if (req.mode === 'reuse') {
            const pick = candidates.find((c) => c.chatId === req.reuseChatId);
            if (!pick) return { ok: false, code: 1, message: fill(msg.bindReuseUnknown, { chatId: req.reuseChatId ?? '?' }) };
            const earlier = bindings.byChat(pick.chatId);
            const b: Binding = {
              root: req.root,
              label: req.label,
              chatId: pick.chatId,
              name: pick.name,
              paneId: req.paneId ?? earlier?.paneId ?? null,
              away: false,
              lang: earlier?.lang ?? null,
              boundAt: now,
              releasedAt: null,
            };
            bindings.set(b);
            refreshPolicy();
            log('bind', { root: req.root, chatId: pick.chatId, how: 'reused' });
            // Renamed, and its description pointed at this project again (it
            // may have been adopted from another root in between).
            const wanted = groupName(req.name, req.label);
            const r = await updateChat(pick.chatId, { name: wanted, description: marker });
            if (r.ok) bindings.touch(req.root, { name: wanted });
            else ctx.note(fill(msg.bindRenameFailed, { error: r.error }));
            return { ok: true, kind: 'bind', chatId: pick.chatId, how: 'reused', name: r.ok ? wanted : (pick.name ?? pick.chatId) };
          }

          if (candidates.length && req.mode === undefined)
            return { ok: false, code: 4, message: fill(msg.bindCandidates, { n: candidates.length }), candidates };

          const owner = creds.ownerOpenId;
          if (!owner) return { ok: false, code: 4, message: msg.bindNoOwner };
          const name = groupName(req.name, req.label);
          try {
            const { chatId } = await channel.createChat({
              name,
              description: marker,
              inviteUserIds: [owner],
              userIdType: 'open_id',
            });
            const b: Binding = {
              root: req.root,
              label: req.label,
              chatId,
              name,
              paneId: req.paneId,
              away: false,
              lang: null,
              boundAt: now,
              releasedAt: null,
            };
            bindings.set(b);
            refreshPolicy();
            log('bind', { root: req.root, chatId, how: 'created' });
            return { ok: true, kind: 'bind', chatId, how: 'created', name };
          } catch (err) {
            const detail = err instanceof Error ? err.message : String(err);
            return {
              ok: false,
              code: /permission|99991672|scope/i.test(detail) ? 4 : 3,
              message: fill(msg.bindCreateFailed, { error: detail }),
            };
          }
        }

        case 'unbind': {
          const live = bindings.active(req.root);
          if (!live) return { ok: false, code: 1, message: msg.unbindNone };
          if (pendingFor(req.root)) return { ok: false, code: 4, message: msg.unbindPending };
          bindings.release(req.root);
          refreshPolicy();
          lastStatus.delete(req.root);
          log('unbind', { root: req.root, chatId: live.chatId });
          return { ok: true, kind: 'unbind', chatId: live.chatId, name: live.name ?? live.chatId };
        }

        case 'rename': {
          const live = bindings.active(req.root);
          if (!live) return { ok: false, code: 4, message: msg.renameNotBound };
          const problem = taskNameProblem(req.name);
          if (problem) return { ok: false, code: 1, message: problem };
          if (!connected) return notConnected();
          const wanted = groupName(req.name, live.label);
          const r = await updateChat(live.chatId, { name: wanted });
          if (!r.ok) return { ok: false, code: 3, message: r.error };
          bindings.touch(req.root, { paneId: req.paneId, name: wanted });
          log('rename', { root: req.root, chatId: live.chatId });
          return { ok: true, kind: 'rename', name: wanted };
        }

        case 'setAway': {
          const b = bindings.touch(req.root, { away: req.away, paneId: req.paneId });
          // Switching off with nothing bound is nothing to do, not a mistake.
          if (!b && !req.away) return { ok: true, kind: 'ack' };
          if (!b) return { ok: false, code: 4, message: msg.notBound };
          lastStatus.delete(req.root);
          log('away', { root: req.root, away: req.away });
          return { ok: true, kind: 'ack' };
        }

        case 'notify': {
          const b = bindings.touch(req.root, { paneId: req.paneId, label: req.label });
          if (!b) return { ok: false, code: 4, message: msg.notBound };
          if (!connected) return notConnected();
          let payload;
          try {
            payload = validateNotify(req.payload);
          } catch (err) {
            if (err instanceof ValidationError) return { ok: false, code: 1, message: err.problems.join('\n') };
            throw err;
          }
          if (payload.lang) bindings.touch(req.root, { lang: payload.lang });
          try {
            await channel.send(b.chatId, { card: notifyCard(payload, b.label) });
            log('notify.sent', { root: b.root });
            return { ok: true, kind: 'ack' };
          } catch (err) {
            return { ok: false, code: 3, message: fill(msg.sendFailed, { error: err instanceof Error ? err.message : String(err) }) };
          }
        }

        case 'sendFile': {
          const b = bindings.touch(req.root, { paneId: req.paneId, label: req.label });
          if (!b) return { ok: false, code: 4, message: msg.notBound };
          if (!connected) return notConnected();
          const checked = resolveSendable(req.path, b.root);
          if ('error' in checked) return { ok: false, code: 1, message: checked.error };
          const { real, bytes } = checked;
          const isImage = /\.(png|jpe?g|gif|webp|bmp)$/i.test(real);
          const fileName = basename(real) || 'file';
          try {
            if (req.caption) await channel.send(b.chatId, { markdown: `**[${b.label}]** ${req.caption}` });
            // A Buffer bypasses the SDK's allowedFileDirs allowlist, which can
            // only be fixed at channel creation while bindings change at
            // runtime. The check above is stricter: the file must live inside
            // the calling project (or a temp dir), after realpath.
            await channel.send(
              b.chatId,
              isImage ? { image: { source: bytes } } : { file: { source: bytes, fileName } },
            );
            log('file.sent', { root: b.root, isImage, size: bytes.length });
            return { ok: true, kind: 'ack' };
          } catch (err) {
            return { ok: false, code: 3, message: fill(msg.sendFailed, { error: err instanceof Error ? err.message : String(err) }) };
          }
        }

        case 'ask': {
          const b = bindings.touch(req.root, { paneId: req.paneId, label: req.label });
          if (!b) return { ok: false, code: 4, message: msg.notBound };
          if (pendingFor(req.root)) return { ok: false, code: 4, message: msg.askPending };
          if (!connected) return notConnected();
          let payload: AskPayload;
          try {
            payload = validateAsk(req.payload);
          } catch (err) {
            if (err instanceof ValidationError) return { ok: false, code: 1, message: err.problems.join('\n') };
            throw err;
          }
          if (payload.lang) bindings.touch(req.root, { lang: payload.lang });
          const reqId = randomUUID().replace(/-/g, '').slice(0, 16);
          let messageId: string;
          try {
            const sent = await channel.send(b.chatId, {
              card: askCard({ payload, projectLabel: b.label, reqId, state: 'pending' }),
            });
            messageId = sent.messageId;
          } catch (err) {
            return { ok: false, code: 3, message: fill(msg.sendFailed, { error: err instanceof Error ? err.message : String(err) }) };
          }
          log('ask.sent', { reqId, root: b.root, options: payload.options.length });

          return await new Promise<Response>((resolve) => {
            const p: Pending = {
              reqId,
              root: b.root,
              chatId: b.chatId,
              messageId,
              label: b.label,
              payload,
              settle: resolve,
              done: false,
              timer: setTimeout(() => {
                void closeWithout(p, 'timedout', {
                  ok: false,
                  code: 2,
                  message: fill(msg.askTimedOut, { seconds: Math.round(req.timeoutMs / 1000) }),
                });
              }, req.timeoutMs),
            };
            pendings.set(reqId, p);
            // The client dying (a harness tool timeout) must not leave a dead
            // question on the phone.
            ctx.onClose(() => {
              if (!p.done)
                void closeWithout(p, 'cancelled', {
                  ok: false,
                  code: 3,
                  message: msg.askClientGone,
                });
            });
            ctx.note(fill(msg.askNote, { seconds: Math.round(req.timeoutMs / 1000) }));
          });
        }

        default:
          return { ok: false, code: 1, message: msg.ipcUnknownRequest };
      }
    },
  });

  server.on('connection', (s: Socket) => {
    sockets.add(s);
    s.on('close', () => sockets.delete(s));
  });

  writeFileSync(pidPath(), `${process.pid}\n`, { mode: 0o600 });
  log('daemon.started', { pid: process.pid, endpoint: ipcEndpoint() });
  for (const sig of signals) process.on(sig, onSignal[sig]);
  void connectLoop();

  return { stop: () => stop('stop()'), done };
}
