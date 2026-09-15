import { execFile, execFileSync } from 'node:child_process';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFile);

export type AgentStatus = 'idle' | 'working' | 'blocked' | 'done' | 'unknown';

export interface AgentInfo {
  agent: string;
  agent_status: AgentStatus;
  cwd: string;
  foreground_cwd?: string;
  pane_id: string;
  focused: boolean;
  terminal_title_stripped?: string;
  workspace_id?: string;
}

/** herdr answers with `{error:{code,message}}` on stdout and still exits 0. */
interface HerdrEnvelope<T> {
  id?: string;
  result?: T;
  error?: { code: string; message: string };
}

export function insideHerdr(): boolean {
  return process.env.HERDR_ENV === '1';
}

/** The pane this process was started from, when herdr set it. */
export function currentPaneId(): string | null {
  const id = process.env.HERDR_PANE_ID?.trim();
  return id || null;
}

function parse<T>(stdout: string): HerdrEnvelope<T> {
  try {
    return JSON.parse(stdout) as HerdrEnvelope<T>;
  } catch {
    return { error: { code: 'bad_output', message: stdout.slice(0, 200) } };
  }
}

export async function agentList(): Promise<AgentInfo[]> {
  try {
    const { stdout } = await execFileAsync('herdr', ['agent', 'list'], { timeout: 10_000 });
    const env = parse<{ agents?: AgentInfo[] }>(stdout);
    return env.result?.agents ?? [];
  } catch {
    return [];
  }
}

export function agentListSync(): AgentInfo[] {
  try {
    const stdout = execFileSync('herdr', ['agent', 'list'], {
      encoding: 'utf8',
      timeout: 10_000,
      stdio: ['ignore', 'pipe', 'ignore'],
    });
    return parse<{ agents?: AgentInfo[] }>(stdout).result?.agents ?? [];
  } catch {
    return [];
  }
}

export interface PromptOutcome {
  ok: boolean;
  /** herdr's own error code when the submission was refused. */
  code?: string;
  message?: string;
}

/**
 * Inject one line into a pane's agent. herdr refuses the submission outright
 * when the agent is already blocked (`agent_blocked`) — that is a real
 * outcome the caller must report to the phone, not a transport failure.
 */
export async function promptPane(paneId: string, text: string): Promise<PromptOutcome> {
  try {
    const { stdout } = await execFileAsync('herdr', ['agent', 'prompt', paneId, text], {
      timeout: 20_000,
      maxBuffer: 1024 * 1024,
    });
    const env = parse<unknown>(stdout);
    if (env.error) return { ok: false, code: env.error.code, message: env.error.message };
    return { ok: true };
  } catch (err) {
    return { ok: false, code: 'spawn_failed', message: err instanceof Error ? err.message : String(err) };
  }
}

/** Best-effort: the pane currently running an agent in this project. */
export function findPaneForProject(agents: AgentInfo[], root: string): string | null {
  const inProject = agents.filter((a) => a.cwd === root || a.foreground_cwd === root);
  if (inProject.length === 0) return null;
  const focused = inProject.find((a) => a.focused);
  return (focused ?? inProject[0])!.pane_id;
}

// ---------------------------------------------------------------- panes (CLI side: the interactive setup handed to a new pane)

/** What one `herdr …` invocation came back with; `ok` is a zero exit, `stdout` the raw text either way. */
export interface HerdrRun {
  ok: boolean;
  stdout: string;
  error?: string;
}
export type HerdrRunner = (args: string[]) => Promise<HerdrRun>;

/** The default runner: `herdr <args>` with a 10 s limit; a missing binary is `ok: false` like any other failure. */
export const runHerdr: HerdrRunner = async (args) => {
  try {
    const { stdout } = await execFileAsync('herdr', args, { timeout: 10_000, maxBuffer: 1024 * 1024 });
    return { ok: true, stdout };
  } catch (err) {
    const e = err as { stdout?: string; message?: string };
    return { ok: false, stdout: e.stdout ?? '', error: e.message ?? String(err) };
  }
};

/**
 * Open a new pane below `pane` (cwd set; the new pane takes focus, since the
 * human types there next) and return its id; null when it could not be
 * opened or herdr's answer is not the expected shape.
 */
export async function splitPane(cwd: string, pane: string, run: HerdrRunner = runHerdr): Promise<string | null> {
  const r = await run(['pane', 'split', '--pane', pane, '--direction', 'down', '--cwd', cwd]);
  if (!r.ok) return null;
  const env = parse<{ pane?: { pane_id?: unknown } }>(r.stdout);
  const id = env.result?.pane?.pane_id;
  return typeof id === 'string' && id ? id : null;
}

/**
 * `herdr pane run` types its argument into the pane's shell as is, with no
 * quoting of its own, so every argv element is quoted here by that shell's
 * rules and the result handed over as one argument. POSIX: single quotes
 * (a word starting with `=` is always quoted — zsh expands `=cmd`); Windows:
 * the CommandLineToArgvW rules, as Python's list2cmdline applies them.
 */
export function quoteForPaneShell(argv: string[], platform: NodeJS.Platform = process.platform): string {
  if (platform === 'win32') return argv.map(quoteWindows).join(' ');
  return argv.map(quotePosix).join(' ');
}

function quotePosix(a: string): string {
  if (a !== '' && !a.startsWith('=') && /^[A-Za-z0-9_@%+=:,./-]+$/.test(a)) return a;
  return `'${a.replace(/'/g, `'\\''`)}'`;
}

function quoteWindows(a: string): string {
  if (a !== '' && !/[\s"]/.test(a)) return a;
  let out = '"';
  let backslashes = 0;
  for (const ch of a) {
    if (ch === '\\') {
      backslashes += 1;
      continue;
    }
    if (ch === '"') {
      out += '\\'.repeat(backslashes * 2 + 1) + '"';
      backslashes = 0;
      continue;
    }
    out += '\\'.repeat(backslashes) + ch;
    backslashes = 0;
  }
  return `${out}${'\\'.repeat(backslashes * 2)}"`;
}

/** Type a command (argv, quoted for the pane's shell) into a pane and press Enter. */
export async function runInPane(pane: string, argv: string[], run: HerdrRunner = runHerdr): Promise<boolean> {
  return (await run(['pane', 'run', pane, quoteForPaneShell(argv)])).ok;
}

/** Close a pane (the one the interactive setup ran in, once it is done). */
export async function closePane(pane: string, run: HerdrRunner = runHerdr): Promise<HerdrRun> {
  return run(['pane', 'close', pane]);
}
