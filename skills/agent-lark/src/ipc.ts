import { createConnection, createServer, type Server, type Socket } from 'node:net';
import { existsSync, unlinkSync } from 'node:fs';
import { ensureHomeDir, sockPath } from './paths.js';

/** Every request a thin client can make of the daemon. */
export type Request =
  | { type: 'ping' }
  | { type: 'stop' }
  | { type: 'list' }
  | { type: 'bind'; root: string; label: string; paneId: string | null; chatId?: string; name?: string }
  | { type: 'unbind'; root: string }
  | { type: 'setAway'; root: string; away: boolean; paneId: string | null; notifyIdle?: boolean; idleMinMinutes?: number }
  | { type: 'ask'; root: string; label: string; paneId: string | null; payload: unknown; timeoutMs: number }
  | { type: 'notify'; root: string; label: string; paneId: string | null; payload: unknown }
  | { type: 'say'; root: string; label: string; paneId: string | null; text: string; title?: string }
  | { type: 'sendFile'; root: string; label: string; paneId: string | null; path: string; caption?: string };

export interface DaemonStatus {
  pid: number;
  connection: string;
  pendingAsks: number;
  bindings: number;
  startedAt: string;
}

export type Response =
  | { ok: true; kind: 'pong'; status: DaemonStatus }
  | { ok: true; kind: 'ask'; reply: string; via: 'button' | 'text' }
  | { ok: true; kind: 'bind'; chatId: string; created: boolean; name: string }
  | { ok: true; kind: 'list'; bindings: Array<{ root: string; label: string; chatId: string; paneId: string | null; away: boolean; notifyIdle: boolean; idleMinMinutes: number }> }
  | { ok: true; kind: 'ack' }
  /** code maps 1:1 onto the CLI exit code the client should use. */
  | { ok: false; code: 1 | 2 | 3 | 4; message: string };

/**
 * Frames the daemon may push before the final one. The wrapper uses `frame`,
 * not `kind`: `Response` already discriminates on `kind`, and sharing the name
 * made the client strip the response's own discriminator.
 */
export type Frame = { frame: 'note'; text: string } | { frame: 'result'; body: Response };

export function isDaemonListening(): boolean {
  return existsSync(sockPath());
}

/**
 * One request, one connection. The connection stays open until the daemon
 * sends its result frame — that is what makes `ask` block, and what makes a
 * dead daemon surface immediately as a dropped connection instead of a hang.
 */
export function request(
  req: Request,
  opts: { onNote?: (text: string) => void; timeoutMs?: number } = {},
): Promise<Response> {
  return new Promise((resolve) => {
    let settled = false;
    const done = (r: Response): void => {
      if (settled) return;
      settled = true;
      try {
        sock.end();
      } catch {
        // already gone
      }
      resolve(r);
    };

    const sock = createConnection(sockPath());
    let buf = '';

    sock.on('connect', () => {
      sock.write(`${JSON.stringify(req)}\n`);
    });
    sock.on('data', (chunk) => {
      buf += chunk.toString('utf8');
      let nl: number;
      while ((nl = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, nl);
        buf = buf.slice(nl + 1);
        if (!line.trim()) continue;
        let frame: Frame;
        try {
          frame = JSON.parse(line) as Frame;
        } catch {
          continue;
        }
        if (frame.frame === 'note') {
          opts.onNote?.(frame.text);
          continue;
        }
        done(frame.body);
      }
    });
    sock.on('error', (err: NodeJS.ErrnoException) => {
      const hint =
        err.code === 'ENOENT' || err.code === 'ECONNREFUSED'
          ? 'daemon 没在跑。先执行：herdr-lark daemon --detach'
          : `无法连接 daemon: ${err.message}`;
      done({ ok: false, code: 3, message: hint });
    });
    sock.on('close', () => {
      done({ ok: false, code: 3, message: 'daemon 在回答之前断开了连接（它可能崩溃或被停止了）' });
    });
    if (opts.timeoutMs) {
      sock.setTimeout(opts.timeoutMs, () => {
        done({ ok: false, code: 3, message: 'daemon 没有在预期时间内响应' });
      });
    }
  });
}

export interface ServeHandlers {
  /** Resolve with the final response; call `note` to push progress first. */
  handle(req: Request, ctx: { note: (text: string) => void; onClose: (fn: () => void) => void }): Promise<Response>;
}

export function serve(handlers: ServeHandlers): Promise<Server> {
  ensureHomeDir();
  const path = sockPath();
  // A socket file nobody answers on is a crash leftover: replace it.
  if (existsSync(path)) {
    try {
      unlinkSync(path);
    } catch {
      // fall through; listen() will report the real problem
    }
  }
  return new Promise((resolve, reject) => {
    const server = createServer((sock: Socket) => {
      let buf = '';
      const closeFns: Array<() => void> = [];
      sock.on('close', () => {
        for (const fn of closeFns) fn();
      });
      sock.on('error', () => {
        /* client vanished mid-request */
      });
      sock.on('data', async (chunk) => {
        buf += chunk.toString('utf8');
        const nl = buf.indexOf('\n');
        if (nl < 0) return;
        const line = buf.slice(0, nl);
        buf = '';
        let req: Request;
        try {
          req = JSON.parse(line) as Request;
        } catch {
          sock.write(`${JSON.stringify({ frame: 'result', body: { ok: false, code: 1, message: '无法解析的请求' } })}\n`);
          sock.end();
          return;
        }
        const note = (text: string): void => {
          if (!sock.destroyed) sock.write(`${JSON.stringify({ frame: 'note', text })}\n`);
        };
        const onClose = (fn: () => void): void => {
          closeFns.push(fn);
        };
        let res: Response;
        try {
          res = await handlers.handle(req, { note, onClose });
        } catch (err) {
          res = { ok: false, code: 3, message: err instanceof Error ? err.message : String(err) };
        }
        if (!sock.destroyed) {
          sock.write(`${JSON.stringify({ frame: 'result', body: res })}\n`);
          sock.end();
        }
      });
    });
    server.on('error', reject);
    server.listen(path, () => resolve(server));
  });
}
