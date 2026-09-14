import { createConnection, createServer, type Server, type Socket } from 'node:net';
import { unlinkSync } from 'node:fs';
import { platform } from 'node:os';
import { ensureHomeDir, ipcEndpoint, sockPath } from './paths.js';
import { fill, msg } from './texts.js';

/** Every request a thin client can make of the daemon. */
export type Request =
  | { type: 'ping' }
  | { type: 'stop' }
  | { type: 'list' }
  | { type: 'bind'; root: string; label: string; paneId: string | null; chatId?: string; name?: string }
  | { type: 'unbind'; root: string }
  | { type: 'setAway'; root: string; away: boolean; paneId: string | null }
  | { type: 'ask'; root: string; label: string; paneId: string | null; payload: unknown; timeoutMs: number }
  | { type: 'notify'; root: string; label: string; paneId: string | null; payload: unknown }
  | { type: 'sendFile'; root: string; label: string; paneId: string | null; path: string; caption?: string };

export interface DaemonStatus {
  pid: number;
  /** The SDK's own WebSocket state; 'connecting' until the first handshake succeeds. */
  connection: string;
  /** True once Feishu is reachable; requests that need Feishu are refused (code 3) until then. */
  connected: boolean;
  /** Why the last connect attempt failed, null once connected. */
  lastError: string | null;
  pendingAsks: number;
  bindings: number;
  startedAt: string;
}

export type Response =
  | { ok: true; kind: 'pong'; status: DaemonStatus }
  | { ok: true; kind: 'ask'; reply: string; via: 'button' | 'text' }
  | { ok: true; kind: 'bind'; chatId: string; created: boolean; name: string }
  | { ok: true; kind: 'list'; bindings: Array<{ root: string; label: string; chatId: string; paneId: string | null; away: boolean }> }
  | { ok: true; kind: 'ack' }
  /**
   * code maps 1:1 onto the CLI exit code the client should use. `reason` is
   * set by the client library for failures it produced itself, so callers can
   * branch on it instead of on wording.
   */
  | { ok: false; code: 1 | 2 | 3 | 4; message: string; reason?: FailureReason };

/** down: nothing listening · connect: other connect error · closed: dropped before the result · timeout: client-side timeoutMs · parse: the daemon could not parse the request */
export type FailureReason = 'down' | 'connect' | 'closed' | 'timeout' | 'parse';

/**
 * Frames the daemon may push before the final one. The wrapper uses `frame`,
 * not `kind`: `Response` already discriminates on `kind`, and sharing the name
 * made the client strip the response's own discriminator.
 */
export type Frame = { frame: 'note'; text: string } | { frame: 'result'; body: Response };

/** Is a daemon answering on this state dir's endpoint? Decided by a ping, never by a file: a socket file can outlive its process, a pipe cannot be probed any other way. */
export async function isDaemonListening(timeoutMs = 1000): Promise<boolean> {
  const res = await request({ type: 'ping' }, { timeoutMs });
  return res.ok && res.kind === 'pong';
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

    const sock = createConnection(ipcEndpoint());
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
      const down = err.code === 'ENOENT' || err.code === 'ECONNREFUSED';
      done({
        ok: false,
        code: 3,
        message: down ? msg.ipcDaemonDown : fill(msg.ipcConnect, { message: err.message }),
        reason: down ? 'down' : 'connect',
      });
    });
    sock.on('close', () => {
      done({ ok: false, code: 3, message: msg.ipcClosed, reason: 'closed' });
    });
    if (opts.timeoutMs) {
      sock.setTimeout(opts.timeoutMs, () => {
        done({ ok: false, code: 3, message: msg.ipcTimeout, reason: 'timeout' });
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
  // A socket file nobody answers on is a crash leftover: replace it. Only a
  // thing on Unix — a Windows pipe name disappears with its process.
  if (platform() !== 'win32') {
    try {
      unlinkSync(sockPath());
    } catch {
      // nothing there, or not ours to remove; listen() will report the real problem
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
          sock.write(`${JSON.stringify({ frame: 'result', body: { ok: false, code: 1, message: msg.ipcBadRequest, reason: 'parse' } })}\n`);
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
    server.listen(ipcEndpoint(), () => resolve(server));
  });
}
