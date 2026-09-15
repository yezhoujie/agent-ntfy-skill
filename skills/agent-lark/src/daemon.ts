import { appendFileSync, lstatSync, mkdirSync, readdirSync, readFileSync, realpathSync, rmdirSync, statSync, writeFileSync, unlinkSync, existsSync } from 'node:fs';
import { createHash, randomUUID } from 'node:crypto';
import { basename, join, sep } from 'node:path';
import { platform, tmpdir } from 'node:os';
import type { Server, Socket } from 'node:net';
import { createLarkChannel, type CardActionEvent, type LarkChannel, type LarkChannelOptions, type NormalizedMessage } from '@larksuite/channel';
import { BindingStore, BindingsFileError, groupName, taskNameProblem, type Binding } from './bindings.js';
import { askCard, checkerName, notifyCard, optionIdOf, receiptCard, statusCard } from './cards.js';
import { resolveCreds } from './creds.js';
import { agentList, findPaneForProject, promptPane } from './herdr.js';
import { isDaemonListening, serve, type Candidate, type Request, type Response } from './ipc.js';
import { ensureHomeDir, homeDir, ipcEndpoint, logPath, mediaDir, pidPath, sockPath } from './paths.js';
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
/** Inbound attachments are kept this many days unless AGENT_LARK_MEDIA_TTL_DAYS says otherwise. */
const MEDIA_TTL_DAYS = 7;
const MEDIA_SWEEP_MS = 24 * 60 * 60 * 1000;
const DAY_MS = 24 * 60 * 60 * 1000;

/**
 * Retention for the media directory, in days; 0 switches the sweep off. A
 * value that is not a whole number falls back to the default with a warning,
 * rather than silently keeping or deleting everything.
 */
function mediaTtlDays(): { days: number; invalid?: string } {
  const raw = process.env.AGENT_LARK_MEDIA_TTL_DAYS?.trim();
  if (raw === undefined || raw === '') return { days: MEDIA_TTL_DAYS };
  if (/^\d+$/.test(raw)) return { days: Number(raw) };
  return { days: MEDIA_TTL_DAYS, invalid: raw };
}

/** Files and bytes under a directory, following no symlinks. */
function measureDir(dir: string): { files: number; bytes: number } {
  const out = { files: 0, bytes: 0 };
  const walk = (d: string): void => {
    let entries;
    try {
      entries = readdirSync(d, { withFileTypes: true });
    } catch {
      return;
    }
    for (const e of entries) {
      const p = join(d, e.name);
      if (e.isSymbolicLink()) continue;
      if (e.isDirectory()) walk(p);
      else if (e.isFile()) {
        out.files += 1;
        try {
          out.bytes += lstatSync(p).size;
        } catch {
          // gone in between
        }
      }
    }
  };
  walk(dir);
  return out;
}

/**
 * Delete files older than the cutoff under the media directory, then the
 * directories that ended up empty (never the root). A symlink is never
 * followed — nothing behind it is read or removed — and is itself dropped
 * by its own mtime like a file; a file that cannot be removed is logged and
 * skipped.
 */
function sweepDir(root: string, cutoffMs: number): { removed: number; keptFiles: number; keptBytes: number; failed: number } {
  const out = { removed: 0, keptFiles: 0, keptBytes: 0, failed: 0 };
  const walk = (d: string, isRoot: boolean): void => {
    let entries;
    try {
      entries = readdirSync(d, { withFileTypes: true });
    } catch {
      return;
    }
    for (const e of entries) {
      const p = join(d, e.name);
      const link = e.isSymbolicLink();
      if (!link && e.isDirectory()) {
        walk(p, false);
        continue;
      }
      if (!link && !e.isFile()) continue;
      try {
        const st = lstatSync(p);
        if (st.mtimeMs < cutoffMs) {
          unlinkSync(p);
          out.removed += 1;
        } else if (!link) {
          out.keptFiles += 1;
          out.keptBytes += st.size;
        }
      } catch (err) {
        out.failed += 1;
        log('media.remove-failed', { path: p, err: String(err).slice(0, 200) });
      }
    }
    if (isRoot) return;
    try {
      if (readdirSync(d).length === 0) rmdirSync(d);
    } catch {
      // not empty after all, or already gone
    }
  };
  walk(root, true);
  return out;
}

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
  /** How often the panes of away projects are polled for a stuck agent. */
  pollMs?: number;
  /** How often the media directory is swept (the first sweep runs at start regardless). */
  sweepMs?: number;
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
  const allowed = [root, mediaDir(), tmpdir()]
    .map((d) => {
      try {
        return realpathSync(d);
      } catch {
        return d;
      }
    });
  if (!allowed.some((d) => within(real, d)))
    return { error: fill(msg.fileRefused, { real, root, media: mediaDir(), tmp: tmpdir() }) };
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
  /** Carries `select` and the option order the reply of a form submit follows. */
  payload: AskPayload;
  urgent: boolean;
  /** Times the pending card was re-rendered after a refused submit (see the submit button's value). */
  attempt: number;
  settle: (r: Response) => void;
  timer: NodeJS.Timeout;
  done: boolean;
}

/** How many closed questions are remembered, so a late form submit can still be read as labels. */
const CLOSED_KEEP = 50;
/** How many sent cards are remembered, so a message quoting one can say which. */
const SENT_CARDS_KEEP = 200;

/**
 * A checker inside a form reports its state in a shape the card docs do not
 * pin down; anything that plausibly means "ticked" counts, so a true value in
 * whatever spelling is not lost. The raw form is logged for the record.
 */
function isTicked(v: unknown): boolean {
  if (v === true || v === 1 || v === 'true' || v === '1' || v === 'checked') return true;
  if (Array.isArray(v)) return v.length > 0;
  return false;
}

export async function runDaemon(deps: DaemonDeps = {}): Promise<DaemonHandle> {
  const creds = resolveCreds();
  if (!creds) throw new DaemonStartError(4, msg.daemonNoCreds);
  ensureHomeDir();
  // A live daemon must not be duplicated; a dead socket file is not a daemon.
  if (await isDaemonListening(2000)) throw new DaemonStartError(3, msg.daemonAlready);
  const herdr: HerdrDeps = deps.herdr ?? { agentList, promptPane, findPaneForProject };
  const retryMs = deps.connectRetryMs ?? CONNECT_RETRY_MS;
  const ttl = mediaTtlDays();
  if (ttl.invalid !== undefined) {
    process.stderr.write(`${fill(msg.mediaTtlInvalid, { value: ttl.invalid, fallback: ttl.days })}\n`);
    log('media.ttl-invalid', { value: ttl.invalid, fallback: ttl.days });
  }

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
  /** Questions already closed, newest last; a late submit on one is still read as labels. */
  const closed = new Map<string, AskPayload>();
  /** Cards this daemon sent, newest last, so a phone message quoting one can name it to the agent. */
  const sentCards = new Map<string, { title: string; kind: 'ask' | 'notify' | 'status' | 'receipt' }>();
  const rememberCard = (messageId: string, kind: 'ask' | 'notify' | 'status' | 'receipt', title: string): void => {
    sentCards.set(messageId, { title, kind });
    while (sentCards.size > SENT_CARDS_KEEP) sentCards.delete(sentCards.keys().next().value!);
  };
  const remember = (p: Pending): void => {
    closed.set(p.reqId, p.payload);
    while (closed.size > CLOSED_KEEP) closed.delete(closed.keys().next().value!);
  };
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
    via: 'button' | 'form' | 'text',
  ): Promise<void> => {
    if (p.done) return;
    p.done = true;
    clearTimeout(p.timer);
    pendings.delete(p.reqId);
    remember(p);
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
    remember(p);
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

  /** The language of cards the daemon sends on its own: the project's last ask / notify, else English. */
  const langOf = (b: Binding | undefined): Lang => b?.lang ?? 'en';

  const receipt = async (b: Binding, why: string): Promise<void> => {
    try {
      const sent = await channel.send(b.chatId, { card: receiptCard(b.label, why, langOf(b)) });
      rememberCard(sent.messageId, 'receipt', t(langOf(b)).notDelivered);
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

  /**
   * Deliver a free-standing phone message into the project's pane. `reactTo`
   * is the human's message: once delivered it gets a "Get" reaction, the one
   * sign on the phone that the terminal has it.
   */
  const inject = async (b: Binding, text: string, reactTo?: string): Promise<void> => {
    try {
      let paneId = b.paneId;
      if (!paneId) paneId = herdr.findPaneForProject(await herdr.agentList(), b.root);
      if (!paneId) {
        await receipt(b, t(langOf(b)).receiptNoPane);
        return;
      }
      const outcome = await herdr.promptPane(paneId, `${INJECT_PREFIX}${text}`);
      log('inject', { root: b.root, paneId, ok: outcome.ok, code: outcome.code });
      if (!outcome.ok) {
        await receipt(b, explainPromptFailure(outcome.code, outcome.message, langOf(b)));
        return;
      }
      if (!reactTo) return;
      try {
        await channel.addReaction(reactTo, 'Get');
      } catch (err) {
        log('reaction.failed', { messageId: reactTo, err: String(err).slice(0, 200) });
      }
    } catch (err) {
      // The adapter promises not to throw; if it ever does, the daemon must not die of it.
      log('inject.failed', { root: b.root, err: String(err).slice(0, 200) });
    }
  };

  /**
   * Transcribe one voice message. Feishu's file_recognize takes base64 opus
   * and caps at 60 s. It needs the `speech_to_text:speech` scope, and a
   * free-plan tenant cannot call it at all (Feishu answers 99991400 "request
   * trigger frequency limit" even with the scope granted). A failure comes
   * back as Feishu's code and msg so the note handed to the agent says which
   * — far better than an `<audio .../>` placeholder it cannot read and would
   * silently misinterpret as text. `null` is the third outcome: the call went
   * through but nothing was recognised, which is not a failure to explain.
   */
  type Unheard = { code: string; msg: string };
  const transcribe = async (audioPath: string): Promise<string | null | Unheard> => {
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
      // The Feishu code and message live in the response body, not in the error text.
      const res = (err as { response?: { status?: number; data?: unknown } } | null)?.response;
      const refused = feishuError(res?.data) ?? feishuError(err);
      log('transcribe.failed', { status: res?.status, code: refused?.code, msg: refused?.msg, err: String(err).slice(0, 200) });
      return refused ? { code: String(refused.code), msg: refused.msg || 'unknown' } : { code: 'unknown', msg: 'unknown' };
    }
  };

  /** Save an inbound attachment next to the daemon's state, never in the repo. */
  const saveResources = async (
    incoming: NormalizedMessage,
  ): Promise<{ saved: string[]; spoken: string[]; unheard: Unheard[]; silent: number }> => {
    const out = { saved: [] as string[], spoken: [] as string[], unheard: [] as Unheard[], silent: 0 };
    if (!incoming.resources.length) return out;
    const dir = join(mediaDir(), createHash('sha1').update(incoming.chatId).digest('hex').slice(0, 12));
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
      out.saved.push(dest);
      if (res.type === 'audio') {
        const heard = await transcribe(dest);
        if (typeof heard === 'string') out.spoken.push(heard);
        else if (heard === null) out.silent += 1;
        else out.unheard.push(heard);
      }
    }
    return out;
  };

  // Whatever the SDK does with a rejecting event handler, nothing in here may
  // become an unhandled rejection: each handler logs its own failure.
  const guarded = <A extends unknown[], R>(name: string, fn: (...args: A) => Promise<R>) =>
    async (...args: A): Promise<R | undefined> => {
      try {
        return await fn(...args);
      } catch (err) {
        log(`${name}.failed`, { err: String(err).slice(0, 200) });
        return undefined;
      }
    };

  channel.on('message', guarded('message', async (incoming: NormalizedMessage) => {
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
    if (got.unheard.length) {
      // One note for all of them; the first failure's reason stands in for the rest.
      const why = fill(msg.injectUnheard, { n: got.unheard.length, ...got.unheard[0]! });
      text = text ? `${text}\n${why}` : why;
    }
    if (got.silent) {
      const why = fill(msg.injectNothingHeard, { n: got.silent });
      text = text ? `${text}\n${why}` : why;
    }
    if (got.saved.length) {
      // One line per file with its absolute path, so the agent can open it as is.
      const list = got.saved.map((f) => fill(msg.injectSaved, { path: f })).join('\n');
      text = text ? `${text}\n${list}\n${msg.injectFilesWithText}` : `${list}\n${msg.injectFilesOnly}`;
    }
    if (!text) return;
    const p = pendingFor(b.root);
    if (p) {
      await answer(p, text, 'text');
      return;
    }
    // A message written as a reply to one of our cards: say which, so the
    // agent knows what "yes, do that" refers to.
    const quoted = sentCards.get(incoming.replyToMessageId ?? incoming.rootId ?? '');
    if (quoted) text = `${fill(msg.replyTo, { title: quoted.title })}\n${text}`;
    await inject(b, text, incoming.messageId);
  }));

  // Feishu waits three seconds for the callback response, so nothing in here
  // waits on a Feishu REST call or on herdr: the card rewrite rides back on
  // the response itself, and the REST fallback / the injection run behind it.
  channel.on('cardAction', guarded('card-action', async (evt: CardActionEvent) => {
    const value = (evt.action.value ?? {}) as { reqId?: string; optionId?: string };
    if (!value.reqId) return;
    const p = pendings.get(value.reqId);
    const form = evt.action.formValue;
    if (form) log('form.submitted', { reqId: value.reqId, formValue: form });
    if (!p || p.done) {
      // A second tap or submit after the question closed: the human is
      // correcting themselves, so it becomes an instruction rather than nothing.
      const b = bindings.activeByChat(evt.chatId);
      if (b) {
        let text: string;
        if (form) {
          const was = closed.get(value.reqId);
          const picked = was
            ? was.options.filter((o) => isTicked(form[checkerName(o.id)])).map((o) => o.label)
            : Object.keys(form)
                .filter((k) => isTicked(form[k]))
                .map((k) => optionIdOf(k) ?? k);
          text = fill(msg.latePick, { labels: picked.join('、') || '?' });
        } else {
          const late = value.optionId ?? '';
          const label = closed.get(value.reqId)?.options.find((o) => o.id === late)?.label ?? late;
          text = label ? fill(msg.latePick, { labels: label }) : msg.lateTapNoOption;
        }
        void inject(b, text).catch((err) => log('inject.failed', { err: String(err).slice(0, 200) }));
      }
      return { toast: { type: 'info', content: t(closed.get(value.reqId)?.lang ?? langOf(b)).toastClosed } };
    }
    const T = t(p.payload.lang ?? 'en');
    let reply: string;
    let via: 'button' | 'form';
    if (form) {
      const picked = p.payload.options.filter((o) => isTicked(form[checkerName(o.id)]));
      if (!picked.length) {
        // Refused, and the card is rewritten with the next attempt so the
        // human's corrected submit is not deduplicated away by the SDK.
        p.attempt += 1;
        const retry = askCard({ payload: p.payload, projectLabel: p.label, reqId: p.reqId, state: 'pending', urgent: p.urgent, attempt: p.attempt });
        void channel.updateCard(p.messageId, retry).catch((err) => log('ask.update-failed', { reqId: p.reqId, err: String(err).slice(0, 200) }));
        return { toast: { type: 'error', content: T.pickAtLeastOne }, card: { type: 'raw', data: retry } };
      }
      reply = picked.map((o) => o.label).join('、');
      via = 'form';
    } else {
      const opt = p.payload.options.find((o) => o.id === value.optionId);
      if (!opt) return { toast: { type: 'error', content: T.toastBadOption } };
      reply = opt.label;
      via = 'button';
    }
    // Build the closed card before answering, so it can ride back on this very
    // callback: Feishu swaps the card in the same round trip and the buttons
    // are gone before a second tap is possible.
    const closedCard = askCard({
      payload: p.payload,
      projectLabel: p.label,
      reqId: p.reqId,
      state: 'answered',
      reply,
    });
    void answer(p, reply, via).catch((err) => log('answer.failed', { reqId: p.reqId, err: String(err).slice(0, 200) }));
    return {
      toast: { type: 'success', content: T.toastAnswered },
      card: { type: 'raw', data: closedCard },
    };
  }));

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

  /**
   * Flag a card to the owner in-app. Only the bot's own messages can be
   * flagged, and the app needs the urgent scope; a refusal never touches the
   * question itself, it is only noted to the caller.
   */
  const flagUrgent = async (messageId: string): Promise<{ ok: true } | { ok: false; error: string }> => {
    const owner = creds.ownerOpenId;
    if (!owner) {
      log('urgent.failed', { messageId, reason: 'no-owner' });
      return { ok: false, error: msg.urgentNoOwner };
    }
    try {
      const res = await channel.rawClient.im.v1.message.urgentApp({
        path: { message_id: messageId },
        params: { user_id_type: 'open_id' },
        data: { user_id_list: [owner] },
      });
      const refused = feishuError(res);
      if (refused) {
        log('urgent.failed', { messageId, code: refused.code, msg: refused.msg });
        return { ok: false, error: fill(msg.urgentRefused, { code: refused.code, msg: refused.msg }) };
      }
      log('urgent.sent', { messageId });
      return { ok: true };
    } catch (err) {
      const body = (err as { response?: { data?: unknown } } | null)?.response?.data;
      const refused = feishuError(body) ?? feishuError(err);
      log('urgent.failed', { messageId, err: String(err).slice(0, 200) });
      return { ok: false, error: refused ? fill(msg.urgentRefused, { code: refused.code, msg: refused.msg }) : err instanceof Error ? err.message : String(err) };
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
      const lang = langOf(b);
      const detail =
        (a.terminal_title_stripped ? `**${a.terminal_title_stripped}**\n` : '') + fill(t(lang).statusPane, { pane: a.pane_id });
      try {
        const sent = await channel.send(b.chatId, { card: statusCard(b.label, detail, lang) });
        rememberCard(sent.messageId, 'status', t(lang).statusBlocked);
        log('status.pushed', { root: b.root, kind: 'blocked' });
      } catch (err) {
        log('status.failed', { root: b.root, err: String(err) });
      }
    }
  };
  const pollTimer = setInterval(() => void poll().catch((err) => log('poll.failed', { err: String(err).slice(0, 200) })), deps.pollMs ?? POLL_MS);
  pollTimer.unref();

  // ---- media retention -------------------------------------------------------
  // What the directory holds is counted while sweeping and kept for `ping`;
  // the count is as of the last sweep, never a rescan on every status call.
  let mediaSeen: { files: number; bytes: number; at: string } = { files: 0, bytes: 0, at: '' };
  const sweepMedia = (): void => {
    if (ttl.days === 0) {
      mediaSeen = { ...measureDir(mediaDir()), at: new Date().toISOString() };
      return;
    }
    const r = sweepDir(mediaDir(), Date.now() - ttl.days * DAY_MS);
    mediaSeen = { files: r.keptFiles, bytes: r.keptBytes, at: new Date().toISOString() };
    log('media.swept', { removed: r.removed, keptFiles: r.keptFiles, keptBytes: r.keptBytes, failed: r.failed, ttlDays: ttl.days });
  };
  const sweepTimer = ttl.days === 0 ? undefined : setInterval(sweepMedia, deps.sweepMs ?? MEDIA_SWEEP_MS);
  sweepTimer?.unref();

  // ---- IPC ----------------------------------------------------------------
  let server: Server | undefined;
  const sockets = new Set<Socket>();
  let resolveDone!: () => void;
  const done = new Promise<void>((resolve) => {
    resolveDone = resolve;
  });
  const signals = ['SIGINT', 'SIGTERM', 'SIGHUP'] as const;
  const onSignal: Record<(typeof signals)[number], () => void> = {
    SIGINT: () => void stopSafely('SIGINT'),
    SIGTERM: () => void stopSafely('SIGTERM'),
    SIGHUP: () => void stopSafely('SIGHUP'),
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
    if (sweepTimer) clearInterval(sweepTimer);
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

  /** stop() never rejects by design; this keeps a future slip out of the unhandled-rejection path. */
  const stopSafely = (why: string): Promise<void> => stop(why).catch((err) => log('stop.failed', { why, err: String(err).slice(0, 200) }));

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
              media: { ttlDays: ttl.days, ...mediaSeen },
            },
          };

        case 'stop':
          // The ack goes out on this connection first; the wind-down starts a tick later.
          setTimeout(() => void stopSafely('stop requested'), 50);
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
            const sent = await channel.send(b.chatId, { card: notifyCard(payload, b.label) });
            rememberCard(sent.messageId, 'notify', payload.title);
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
          const urgent = req.urgent === true;
          let messageId: string;
          try {
            const sent = await channel.send(b.chatId, {
              card: askCard({ payload, projectLabel: b.label, reqId, state: 'pending', urgent }),
            });
            messageId = sent.messageId;
          } catch (err) {
            return { ok: false, code: 3, message: fill(msg.sendFailed, { error: err instanceof Error ? err.message : String(err) }) };
          }
          rememberCard(messageId, 'ask', payload.title);
          log('ask.sent', { reqId, root: b.root, options: payload.options.length, select: payload.select, urgent });
          // Registered before anything else goes over the wire: the card is on
          // the phone from `send` on, and a tap that lands while the urgent flag
          // is still in flight must find its question.
          let settle!: (r: Response) => void;
          const result = new Promise<Response>((resolve) => {
            settle = resolve;
          });
          const closeLater = (state: 'timedout' | 'cancelled', res: Response): void => {
            void closeWithout(p, state, res).catch((err) => log('close.failed', { reqId, state, err: String(err).slice(0, 200) }));
          };
          const p: Pending = {
            reqId,
            root: b.root,
            chatId: b.chatId,
            messageId,
            label: b.label,
            payload,
            urgent,
            attempt: 0,
            settle,
            done: false,
            timer: setTimeout(() => {
              closeLater('timedout', {
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
            if (!p.done) closeLater('cancelled', { ok: false, code: 3, message: msg.askClientGone });
          });
          if (urgent) {
            const r = await flagUrgent(messageId);
            if (!r.ok) ctx.note(fill(msg.urgentNotSent, { error: r.error }));
          }
          ctx.note(fill(msg.askNote, { seconds: Math.round(req.timeoutMs / 1000) }));
          return await result;
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
  setImmediate(sweepMedia);
  for (const sig of signals) process.on(sig, onSignal[sig]);
  void connectLoop().catch((err) => log('connect-loop.failed', { err: String(err).slice(0, 200) }));

  return { stop: () => stop('stop()'), done };
}
