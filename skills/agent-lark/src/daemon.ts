import { appendFileSync, mkdirSync, readFileSync, realpathSync, statSync, writeFileSync, unlinkSync, existsSync } from 'node:fs';
import { createHash, randomUUID } from 'node:crypto';
import { basename, join, sep } from 'node:path';
import { tmpdir } from 'node:os';
import type { Server } from 'node:net';
import { createLarkChannel, type CardActionEvent, type LarkChannel, type NormalizedMessage } from '@larksuite/channel';
import { BindingStore, type Binding } from './bindings.js';
import { askCard, notifyCard, receiptCard, statusCard } from './cards.js';
import { resolveCreds } from './creds.js';
import { agentList, findPaneForProject, promptPane } from './herdr.js';
import { serve, type Request, type Response } from './ipc.js';
import { ensureHomeDir, homeDir, logPath, pidPath, sockPath } from './paths.js';
import { fill, msg, t } from './texts.js';
import { validateAsk, validateNotify, ValidationError, type AskPayload, type Lang } from './validate.js';

const INJECT_PREFIX = '[agent-lark remote] ';
const POLL_MS = 5_000;
const STATUS_COOLDOWN_MS = 60_000;

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

export async function runDaemon(): Promise<void> {
  const creds = resolveCreds();
  if (!creds) {
    process.stderr.write(`${msg.prefix}${msg.daemonNoCreds}\n`);
    process.exit(4);
  }
  ensureHomeDir();
  if (existsSync(sockPath())) {
    // Something may still be answering there; the IPC layer replaces a dead
    // socket file, but a live daemon must not be duplicated.
    const { request } = await import('./ipc.js');
    const probe = await request({ type: 'ping' }, { timeoutMs: 2000 });
    if (probe.ok) {
      process.stderr.write(`${msg.prefix}${msg.daemonAlready}\n`);
      process.exit(3);
    }
  }

  const bindings = new BindingStore();
  const pendings = new Map<string, Pending>();
  const lastStatus = new Map<string, string>();
  const lastStatusPush = new Map<string, number>();
  const startedAt = new Date().toISOString();

  const channel: LarkChannel = createLarkChannel({
    appId: creds.appId,
    appSecret: creds.appSecret,
    policy: {
      // Only bound groups are listened to, and inside them no @ is needed —
      // the group IS the project, so every message in it is for this agent.
      groupAllowlist: bindings.chatIds(),
      requireMention: false,
      dmMode: creds.ownerOpenId ? 'allowlist' : 'disabled',
      dmAllowlist: creds.ownerOpenId ? [creds.ownerOpenId] : [],
    },
  });

  const refreshPolicy = (): void => {
    channel.updatePolicy({ groupAllowlist: bindings.chatIds() });
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
    if (!paneId) paneId = findPaneForProject(await agentList(), b.root);
    if (!paneId) {
      await receipt(b, t('zh').receiptNoPane);
      return;
    }
    const outcome = await promptPane(paneId, `${INJECT_PREFIX}${text}`);
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
    const b = bindings.byChat(incoming.chatId);
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
      const b = bindings.byChat(evt.chatId);
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

  channel.on('error', (err) => log('channel.error', { code: err.code, message: err.message }));
  channel.on('reconnecting', () => log('channel.reconnecting'));
  channel.on('reconnected', () => log('channel.reconnected'));

  await channel.connect();
  log('daemon.connected', { bindings: bindings.all().length, credSource: creds.source });

  // ---- agent state pushes -------------------------------------------------
  const poll = async (): Promise<void> => {
    const away = bindings.all().filter((b) => b.away && b.paneId);
    if (!away.length) return;
    const agents = await agentList();
    for (const b of away) {
      if (pendingFor(b.root)) continue;
      const a = agents.find((x) => x.pane_id === b.paneId);
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
  const shutdown = async (why: string): Promise<void> => {
    log('daemon.stopping', { why });
    clearInterval(pollTimer);
    for (const p of [...pendings.values()]) {
      await closeWithout(p, 'cancelled', {
        ok: false,
        code: 3,
        message: msg.askCancelledStop,
      });
    }
    try {
      await channel.disconnect();
    } catch {
      // going down anyway
    }
    server?.close();
    for (const f of [sockPath(), pidPath()]) {
      try {
        if (existsSync(f)) unlinkSync(f);
      } catch {
        // best effort
      }
    }
    process.exit(0);
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
              connection: channel.getConnectionStatus()?.state ?? 'unknown',
              pendingAsks: pendings.size,
              bindings: bindings.all().length,
              startedAt,
            },
          };

        case 'stop':
          setTimeout(() => void shutdown('stop requested'), 50);
          return { ok: true, kind: 'ack' };

        case 'list':
          return {
            ok: true,
            kind: 'list',
            bindings: bindings.all().map((b) => ({
              root: b.root,
              label: b.label,
              chatId: b.chatId,
              paneId: b.paneId,
              away: b.away,
            })),
          };

        case 'bind': {
          const existing = bindings.get(req.root);
          if (req.chatId) {
            const b: Binding = {
              root: req.root,
              label: req.label,
              chatId: req.chatId,
              paneId: req.paneId ?? existing?.paneId ?? null,
              away: existing?.away ?? false,
              boundAt: new Date().toISOString(),
            };
            bindings.set(b);
            refreshPolicy();
            log('bind', { root: req.root, chatId: req.chatId, created: false });
            return { ok: true, kind: 'bind', chatId: req.chatId, created: false, name: req.label };
          }
          if (existing) {
            bindings.touch(req.root, { paneId: req.paneId, label: req.label });
            return { ok: true, kind: 'bind', chatId: existing.chatId, created: false, name: existing.label };
          }
          // No local binding — but the group may already exist from an earlier
          // install whose bindings.json is gone. Creating a second group for
          // the same project would split the conversation in two, so look for
          // one the bot made for exactly this project root before creating.
          const marker = `agent-lark · ${req.root}`;
          try {
            for (const summary of await channel.listChats()) {
              let info;
              try {
                info = await channel.getChatInfo(summary.id);
              } catch {
                continue;
              }
              if (info.description !== marker) continue;
              const b: Binding = {
                root: req.root,
                label: req.label,
                chatId: summary.id,
                paneId: req.paneId,
                away: false,
                boundAt: new Date().toISOString(),
              };
              bindings.set(b);
              refreshPolicy();
              log('bind.reused', { root: req.root, chatId: summary.id });
              return { ok: true, kind: 'bind', chatId: summary.id, created: false, name: summary.name };
            }
          } catch (err) {
            log('bind.scan-failed', { root: req.root, err: String(err) });
          }
          const owner = creds.ownerOpenId;
          if (!owner) {
            return {
              ok: false,
              code: 4,
              message: msg.bindNoOwner,
            };
          }
          const name = req.name?.trim() || `🤖 ${req.label}`;
          try {
            const { chatId } = await channel.createChat({
              name,
              description: `agent-lark · ${req.root}`,
              inviteUserIds: [owner],
              userIdType: 'open_id',
            });
            // `existing` is undefined here — the bound case returned above.
            const b: Binding = {
              root: req.root,
              label: req.label,
              chatId,
              paneId: req.paneId,
              away: false,
              boundAt: new Date().toISOString(),
            };
            bindings.set(b);
            refreshPolicy();
            log('bind', { root: req.root, chatId, created: true });
            return { ok: true, kind: 'bind', chatId, created: true, name };
          } catch (err) {
            const detail = err instanceof Error ? err.message : String(err);
            return {
              ok: false,
              code: /permission|99991672|scope/i.test(detail)
                ? 4
                : 3,
              message: fill(msg.bindCreateFailed, { error: detail }),
            };
          }
        }

        case 'unbind': {
          const b = bindings.get(req.root);
          if (!b) return { ok: false, code: 1, message: msg.unbindNone };
          const p = pendingFor(req.root);
          if (p) return { ok: false, code: 4, message: msg.unbindPending };
          bindings.remove(req.root);
          refreshPolicy();
          log('unbind', { root: req.root });
          return { ok: true, kind: 'ack' };
        }

        case 'setAway': {
          const b = bindings.touch(req.root, { away: req.away, paneId: req.paneId });
          if (!b) return { ok: false, code: 4, message: msg.notBound };
          lastStatus.delete(req.root);
          log('away', { root: req.root, away: req.away });
          return { ok: true, kind: 'ack' };
        }

        case 'notify': {
          const b = bindings.touch(req.root, { paneId: req.paneId, label: req.label });
          if (!b) return { ok: false, code: 4, message: msg.notBound };
          let payload;
          try {
            payload = validateNotify(req.payload);
          } catch (err) {
            if (err instanceof ValidationError) return { ok: false, code: 1, message: err.problems.join('\n') };
            throw err;
          }
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
          let payload: AskPayload;
          try {
            payload = validateAsk(req.payload);
          } catch (err) {
            if (err instanceof ValidationError) return { ok: false, code: 1, message: err.problems.join('\n') };
            throw err;
          }
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

  writeFileSync(pidPath(), `${process.pid}\n`, { mode: 0o600 });
  log('daemon.started', { pid: process.pid });
  process.stdout.write(`${fill(msg.daemonReady, { pid: process.pid, sock: sockPath() })}\n`);

  for (const sig of ['SIGINT', 'SIGTERM', 'SIGHUP'] as const) {
    process.on(sig, () => void shutdown(sig));
  }
}
