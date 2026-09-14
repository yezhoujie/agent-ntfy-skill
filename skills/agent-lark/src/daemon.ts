import { appendFileSync, mkdirSync, readFileSync, realpathSync, statSync, writeFileSync, unlinkSync, existsSync } from 'node:fs';
import { createHash, randomUUID } from 'node:crypto';
import { basename, join, sep } from 'node:path';
import { tmpdir } from 'node:os';
import type { Server } from 'node:net';
import { createLarkChannel, type CardActionEvent, type LarkChannel, type NormalizedMessage } from '@larksuite/channel';
import { BindingStore, type Binding } from './bindings.js';
import { askCard, notifyCard, receiptCard, sayCard, statusCard } from './cards.js';
import { resolveCreds } from './creds.js';
import { agentList, findPaneForProject, promptPane } from './herdr.js';
import { serve, type Request, type Response } from './ipc.js';
import { ensureHomeDir, homeDir, logPath, pidPath, sockPath } from './paths.js';
import { validateAsk, validateNotify, ValidationError, type AskPayload } from './validate.js';

const INJECT_PREFIX = '[herdr-lark remote] ';
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
  if (!existsSync(path)) return { error: `文件不存在：${path}` };
  let real: string;
  try {
    real = realpathSync(path);
  } catch (err) {
    return { error: `路径解析失败：${String(err)}` };
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
    return {
      error:
        `拒绝发送 ${real}\n只能发这些目录下的文件：\n  本项目 ${root}\n  ${join(homeDir(), 'media')}\n  ${tmpdir()}\n` +
        '（这是防止 send-file 被用来把机器上任意文件读走）',
    };
  const st = statSync(real);
  if (!st.isFile()) return { error: `不是普通文件：${real}` };
  const isImage = /\.(png|jpe?g|gif|webp|bmp)$/i.test(real);
  const cap = isImage ? MAX_IMAGE_BYTES : MAX_FILE_BYTES;
  if (st.size > cap)
    return { error: `文件太大：${(st.size / 1024 / 1024).toFixed(1)} MB，上限 ${cap / 1024 / 1024} MB` };
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
    process.stderr.write('herdr-lark: 找不到飞书应用凭据。先跑一次：herdr-lark setup（或设好 HERDR_LARK_APP_ID / HERDR_LARK_APP_SECRET）\n');
    process.exit(4);
  }
  ensureHomeDir();
  if (existsSync(sockPath())) {
    // Something may still be answering there; the IPC layer replaces a dead
    // socket file, but a live daemon must not be duplicated.
    const { request } = await import('./ipc.js');
    const probe = await request({ type: 'ping' }, { timeoutMs: 2000 });
    if (probe.ok) {
      process.stderr.write('herdr-lark: daemon 已经在跑了\n');
      process.exit(3);
    }
  }

  const bindings = new BindingStore();
  const pendings = new Map<string, Pending>();
  const lastStatus = new Map<string, string>();
  const lastStatusPush = new Map<string, number>();
  const workingSince = new Map<string, number>();
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

  const explainPromptFailure = (code?: string, message?: string): string => {
    switch (code) {
      case 'agent_blocked':
        return '终端里的 agent 正卡在一个需要你本人确认的提示上，收不了新输入。回电脑前处理一下。';
      case 'agent_not_found':
      case 'pane_not_found':
        return '记录的 herdr 窗格已经不在了。到项目里跑一次 herdr-lark bind 或任意 herdr-lark 命令，重新记录窗格。';
      case 'spawn_failed':
        return `herdr 命令没跑起来：${message ?? '未知原因'}`;
      default:
        return `herdr 拒绝了这次注入：${code ?? '未知'} ${message ?? ''}`.trim();
    }
  };

  /** Deliver a free-standing phone message into the project's pane. */
  const inject = async (b: Binding, text: string): Promise<void> => {
    let paneId = b.paneId;
    if (!paneId) paneId = findPaneForProject(await agentList(), b.root);
    if (!paneId) {
      await receipt(b, '这个项目还没有记录到 herdr 窗格，消息没处可送。');
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
  const saveResources = async (msg: NormalizedMessage): Promise<{ files: string[]; spoken: string[]; unheard: number }> => {
    const out = { files: [] as string[], spoken: [] as string[], unheard: 0 };
    if (!msg.resources.length) return out;
    const dir = join(homeDir(), 'media', createHash('sha1').update(msg.chatId).digest('hex').slice(0, 12));
    mkdirSync(dir, { recursive: true, mode: 0o700 });
    for (const res of msg.resources) {
      // Only images have their own download type; everything else (files,
      // voice notes, video) comes down the `file` path.
      const kind = res.type === 'image' ? 'image' : 'file';
      const ext = res.type === 'image' ? 'png' : res.type === 'audio' ? 'opus' : 'bin';
      const name = res.fileName ?? `${res.type}-${Date.now()}.${ext}`;
      const dest = join(dir, `${Date.now()}-${name}`);
      try {
        await channel.downloadResourceToFile(msg.messageId, res.fileKey, kind, dest);
      } catch (err) {
        log('download.failed', { messageId: msg.messageId, type: res.type, err: String(err).slice(0, 200) });
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

  channel.on('message', async (msg: NormalizedMessage) => {
    if (msg.senderIsBot) return;
    const b = bindings.byChat(msg.chatId);
    if (!b) return;
    const got = await saveResources(msg);
    // A voice message arrives as an `<audio .../>` placeholder in `content`.
    // Strip it: either the transcript replaces it, or the human is told
    // plainly that it could not be heard.
    let text = msg.content.replace(/<audio\b[^>]*\/?>/gi, '').trim();
    if (got.spoken.length) {
      const said = got.spoken.join('\n');
      text = text ? `${text}\n（语音转文字）${said}` : said;
    }
    if (got.unheard) {
      const why =
        '（收到 ' + got.unheard + ' 条语音，但转文字失败——多半是应用还没开 speech_to_text:speech 权限。' +
        '请告诉用户：跑一次 herdr-lark setup --update 重新扫码补上这个权限，或者这次先打字。）';
      text = text ? `${text}\n${why}` : why;
    }
    if (got.files.length) {
      const list = got.files.map((f) => `  ${f}`).join('\n');
      text = text ? `${text}\n（附件已存到本机）\n${list}` : `（我发了附件，已存到本机）\n${list}`;
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
        await inject(b, late ? `（补充）我选 ${late}` : '（补充）我又点了一下上面那张卡');
      }
      return { toast: { type: 'info', content: '这个问题已经结束了，刚才那下当成新指令发过去了' } };
    }
    const opt = p.payload.options.find((o) => o.id === value.optionId);
    if (!opt) return { toast: { type: 'error', content: '这个选项对不上，再试一次' } };
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
      toast: { type: 'success', content: '已回复' },
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
      if (a.agent_status === 'working' && prev !== 'working') workingSince.set(b.root, now);
      if (!prev || prev === a.agent_status) continue;

      let kind: 'blocked' | 'idle' | null = null;
      let ranMs = 0;
      if (a.agent_status === 'blocked') {
        // Stuck on a prompt only a human can answer: always worth a push.
        kind = 'blocked';
      } else if (prev === 'working' && (a.agent_status === 'idle' || a.agent_status === 'done')) {
        // "Finished" fires at the end of every conversational turn, which is
        // pure noise while the human is at the keyboard. Opt-in, and only
        // when the turn actually ran long enough to be worth interrupting for.
        if (!b.notifyIdle) continue;
        ranMs = now - (workingSince.get(b.root) ?? now);
        if (ranMs < b.idleMinMinutes * 60_000) continue;
        kind = 'idle';
      }
      if (!kind) continue;
      if (now - (lastStatusPush.get(b.root) ?? 0) < STATUS_COOLDOWN_MS) continue;
      lastStatusPush.set(b.root, now);
      const ranFor = ranMs ? `\n跑了 ${Math.round(ranMs / 60_000)} 分钟` : '';
      const detail =
        (a.terminal_title_stripped ? `**${a.terminal_title_stripped}**\n` : '') + `窗格 ${a.pane_id}${ranFor}`;
      try {
        await channel.send(b.chatId, { card: statusCard(b.label, kind, detail) });
        log('status.pushed', { root: b.root, kind });
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
        message: 'daemon 正在停止；问题已经发出去了，但这次拿不到答复了',
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
              notifyIdle: b.notifyIdle,
              idleMinMinutes: b.idleMinMinutes,
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
              notifyIdle: existing?.notifyIdle ?? false,
              idleMinMinutes: existing?.idleMinMinutes ?? 10,
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
          const marker = `herdr-lark · ${req.root}`;
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
                notifyIdle: false,
                idleMinMinutes: 10,
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
              message: '不知道该把谁拉进新群（没有记录应用 owner）。用 --chat <chat_id> 绑定一个你自己建好的群。',
            };
          }
          const name = req.name?.trim() || `🤖 ${req.label}`;
          try {
            const { chatId } = await channel.createChat({
              name,
              description: `herdr-lark · ${req.root}`,
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
              notifyIdle: false,
              idleMinMinutes: 10,
              boundAt: new Date().toISOString(),
            };
            bindings.set(b);
            refreshPolicy();
            log('bind', { root: req.root, chatId, created: true });
            return { ok: true, kind: 'bind', chatId, created: true, name };
          } catch (err) {
            const msg = err instanceof Error ? err.message : String(err);
            return {
              ok: false,
              code: /permission|99991672|scope/i.test(msg)
                ? 4
                : 3,
              message: `建群失败：${msg}\n若是权限问题，应用缺 im:chat（建群）权限，跑 herdr-lark setup --update 补授权，或用 --chat <chat_id> 绑已有群。`,
            };
          }
        }

        case 'unbind': {
          const b = bindings.get(req.root);
          if (!b) return { ok: false, code: 1, message: '这个项目本来就没绑定' };
          const p = pendingFor(req.root);
          if (p) return { ok: false, code: 4, message: '还有一个问题挂在手机上，先回答或等它超时' };
          bindings.remove(req.root);
          refreshPolicy();
          log('unbind', { root: req.root });
          return { ok: true, kind: 'ack' };
        }

        case 'setAway': {
          const b = bindings.touch(req.root, {
            away: req.away,
            paneId: req.paneId,
            notifyIdle: req.notifyIdle,
            idleMinMinutes: req.idleMinMinutes,
          });
          if (!b) return { ok: false, code: 4, message: '这个项目还没 bind，先跑 herdr-lark bind' };
          lastStatus.delete(req.root);
          workingSince.delete(req.root);
          log('away', { root: req.root, away: req.away, notifyIdle: b.notifyIdle });
          return { ok: true, kind: 'ack' };
        }

        case 'notify': {
          const b = bindings.touch(req.root, { paneId: req.paneId, label: req.label });
          if (!b) return { ok: false, code: 4, message: '这个项目还没 bind，先跑 herdr-lark bind' };
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
            return { ok: false, code: 3, message: `发送失败：${err instanceof Error ? err.message : String(err)}` };
          }
        }

        case 'say': {
          const b = bindings.touch(req.root, { paneId: req.paneId, label: req.label });
          if (!b) return { ok: false, code: 4, message: '这个项目还没 bind，先跑 herdr-lark away on' };
          const text = req.text.trim();
          if (!text) return { ok: false, code: 1, message: '没有内容可发' };
          try {
            await channel.send(b.chatId, { card: sayCard(text, b.label, req.title) });
            log('say.sent', { root: b.root, chars: text.length });
            return { ok: true, kind: 'ack' };
          } catch (err) {
            return { ok: false, code: 3, message: `发送失败：${err instanceof Error ? err.message : String(err)}` };
          }
        }

        case 'sendFile': {
          const b = bindings.touch(req.root, { paneId: req.paneId, label: req.label });
          if (!b) return { ok: false, code: 4, message: '这个项目还没 bind，先跑 herdr-lark bind' };
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
            return { ok: false, code: 3, message: `发送失败：${err instanceof Error ? err.message : String(err)}` };
          }
        }

        case 'ask': {
          const b = bindings.touch(req.root, { paneId: req.paneId, label: req.label });
          if (!b) return { ok: false, code: 4, message: '这个项目还没 bind，先跑 herdr-lark bind' };
          if (pendingFor(req.root))
            return { ok: false, code: 4, message: '这个项目已经有一个问题挂在手机上了；一次只能问一个' };
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
            return { ok: false, code: 3, message: `发送失败：${err instanceof Error ? err.message : String(err)}` };
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
                  message: `等了 ${Math.round(req.timeoutMs / 1000)} 秒没人回答`,
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
                  message: '提问方断开了',
                });
            });
            ctx.note(`已发到飞书群，等你回答（最长 ${Math.round(req.timeoutMs / 1000)} 秒）`);
          });
        }

        default:
          return { ok: false, code: 1, message: '不认识的请求' };
      }
    },
  });

  writeFileSync(pidPath(), `${process.pid}\n`, { mode: 0o600 });
  log('daemon.started', { pid: process.pid });
  process.stdout.write(`herdr-lark daemon: pid ${process.pid}，已连上飞书，socket ${sockPath()}\n`);

  for (const sig of ['SIGINT', 'SIGTERM', 'SIGHUP'] as const) {
    process.on(sig, () => void shutdown(sig));
  }
}
