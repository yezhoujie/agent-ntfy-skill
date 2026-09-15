import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { homedir, platform } from 'node:os';
import { basename, join, resolve } from 'node:path';
import { fill, msg } from './texts.js';

/** Daemon-side state directory. Everything the daemon owns lives here. */
export function homeDir(): string {
  const override = process.env.AGENT_LARK_HOME?.trim();
  return override ? resolve(override) : join(homedir(), '.agent-lark');
}

export function ensureHomeDir(): string {
  const dir = homeDir();
  mkdirSync(dir, { recursive: true, mode: 0o700 });
  return dir;
}

/** Unix socket file; only meaningful off Windows (see ipcEndpoint). */
export const sockPath = (): string => join(homeDir(), 'daemon.sock');

/**
 * Longest Unix socket path the platform binds: `sizeof(sun_path)`, every byte
 * usable (libuv copies the name without a terminating NUL). A deeper state
 * directory makes every bind and connect fail with EINVAL.
 */
export const SOCK_PATH_LIMIT = ['darwin', 'freebsd', 'openbsd', 'netbsd'].includes(platform()) ? 104 : 108;

/** Why the socket path cannot be used, or null; always null on Windows (a named pipe has no such limit). */
export function sockPathProblem(): string | null {
  if (platform() === 'win32') return null;
  const path = sockPath();
  const bytes = Buffer.byteLength(path, 'utf8');
  return bytes > SOCK_PATH_LIMIT ? fill(msg.sockPathTooLong, { path, bytes, limit: SOCK_PATH_LIMIT }) : null;
}

/**
 * Where the CLI meets the daemon. Unix: the socket file in the state dir.
 * Windows: a named pipe in the `\\.\pipe\` namespace — there is no file,
 * and the name vanishes with the process, so one pipe per state dir is
 * derived from the dir's path.
 */
export function ipcEndpoint(): string {
  if (platform() === 'win32') return `\\\\.\\pipe\\agent-lark-${createHash('sha1').update(homeDir()).digest('hex').slice(0, 12)}`;
  return sockPath();
}

export const pidPath = (): string => join(homeDir(), 'daemon.pid');
export const logPath = (): string => join(homeDir(), 'daemon.log');
export const bindingsPath = (): string => join(homeDir(), 'bindings.json');
/** Where inbound attachments are saved (per group, under the state dir); swept by the daemon after a while. */
export const mediaDir = (): string => join(homeDir(), 'media');

/**
 * Lease holder identity: the git toplevel, else the cwd. A worktree or a
 * submodule is its own project, exactly as in agent-ntfy — every pane and
 * every context reset inside the same checkout shares one Feishu group.
 */
export function projectRoot(cwd = process.cwd()): string {
  try {
    const out = execFileSync('git', ['rev-parse', '--show-toplevel'], {
      cwd,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    }).trim();
    if (out) return out;
  } catch {
    // not a git repository — the cwd is the project
  }
  return resolve(cwd);
}

/** Short human label for a project, used in card titles and group names. */
export function projectLabel(root: string): string {
  return basename(root) || root;
}

export const projectStateDir = (root: string): string => join(root, '.agent-lark');
export const projectStatePath = (root: string): string => join(projectStateDir(root), 'state.json');

/**
 * What a project's `.agent-lark/state.json` holds. The injection target (the
 * herdr pane) is deliberately not here: the daemon keeps it on the binding.
 */
export interface ProjectState {
  /** The human is away and wants decisions on the phone. */
  away: boolean;
  /** Feishu group this project is bound to right now; null until bound, and again after `unbind`. */
  chatId: string | null;
  target: string;
  updated: string;
}

export function readProjectState(root: string): ProjectState | null {
  try {
    const raw = readFileSync(projectStatePath(root), 'utf8');
    const parsed = JSON.parse(raw) as Partial<ProjectState>;
    return {
      away: parsed.away === true,
      chatId: typeof parsed.chatId === 'string' ? parsed.chatId : null,
      target: typeof parsed.target === 'string' ? parsed.target : root,
      updated: typeof parsed.updated === 'string' ? parsed.updated : '',
    };
  } catch {
    return null;
  }
}

/**
 * Write the project state file. `create: false` (the default) only updates a
 * file that already exists, so a project that never ran `bind` never gets a
 * directory planted in it.
 */
export function writeProjectState(
  root: string,
  patch: Partial<Omit<ProjectState, 'updated'>>,
  opts: { create?: boolean } = {},
): ProjectState | null {
  const dir = projectStateDir(root);
  const exists = existsSync(dir);
  if (!exists && !opts.create) return null;
  if (!exists) {
    mkdirSync(dir, { recursive: true, mode: 0o700 });
    // Keep the directory out of the project's own git history without
    // touching the project's .gitignore.
    writeFileSync(join(dir, '.gitignore'), '*\n', { mode: 0o600 });
  }
  const current = readProjectState(root) ?? {
    away: false,
    chatId: null,
    target: root,
    updated: '',
  };
  const next: ProjectState = {
    ...current,
    ...patch,
    target: root,
    updated: new Date().toISOString(),
  };
  const tmp = `${projectStatePath(root)}.tmp`;
  writeFileSync(tmp, `${JSON.stringify(next, null, 2)}\n`, { mode: 0o600 });
  renameSync(tmp, projectStatePath(root));
  return next;
}
