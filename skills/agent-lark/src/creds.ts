import { execFileSync } from 'node:child_process';
import { chmodSync, existsSync, mkdirSync, readFileSync, renameSync, statSync, unlinkSync, writeFileSync } from 'node:fs';
import { homedir, platform } from 'node:os';
import { dirname, join } from 'node:path';

export interface AppCreds {
  appId: string;
  appSecret: string;
  /** open_id of the app owner; needed to seed a new group chat. */
  ownerOpenId?: string;
  brand?: string;
}

/** Which layer the credentials actually came from. Shown by `status`. */
export type CredSource = 'env' | 'env-file' | 'keychain' | 'file' | 'env-generic';

export interface ResolvedCreds extends AppCreds {
  source: CredSource;
  /** Human-readable origin, safe to print. Never contains a secret. */
  origin: string;
}

export type StoreKind = 'keychain' | 'file' | 'none';

const SERVICE = process.env.HERDR_LARK_KEYCHAIN?.trim() || 'herdr-lark';
const ACCOUNT = 'app';

/** `$XDG_CONFIG_HOME/herdr-lark`, i.e. `~/.config/herdr-lark` by default. */
export function configDir(): string {
  const xdg = process.env.XDG_CONFIG_HOME?.trim();
  const base = xdg || join(homedir(), platform() === 'win32' ? 'AppData/Roaming' : '.config');
  return join(base, 'herdr-lark');
}

export const credentialsFile = (): string => join(configDir(), 'credentials.json');
export const envFile = (): string => process.env.HERDR_LARK_ENV_FILE?.trim() || join(configDir(), '.env');

/**
 * Which secret store `setup` writes to. Defaults to the OS keychain where one
 * is reachable, a 0600 file otherwise. `HERDR_LARK_STORE` overrides.
 */
export function defaultStore(): StoreKind {
  const forced = process.env.HERDR_LARK_STORE?.trim();
  if (forced === 'keychain' || forced === 'file' || forced === 'none') return forced;
  return keychainAvailable() ? 'keychain' : 'file';
}

/** Name of the per-platform secure store, for messages. */
export function keychainName(): string {
  switch (platform()) {
    case 'darwin':
      return 'macOS 钥匙串';
    case 'win32':
      return 'Windows DPAPI（当前用户加密）';
    default:
      return 'libsecret (secret-tool)';
  }
}

function has(cmd: string): boolean {
  try {
    execFileSync(platform() === 'win32' ? 'where' : 'sh', platform() === 'win32' ? [cmd] : ['-c', `command -v ${cmd}`], {
      stdio: 'ignore',
    });
    return true;
  } catch {
    return false;
  }
}

function keychainAvailable(): boolean {
  if (platform() === 'darwin') return true;
  if (platform() === 'win32') return has('powershell');
  return has('secret-tool');
}

/** Windows has no keychain CLI; DPAPI-encrypting a file is the no-native-dep
 * equivalent — the blob is readable only by this user on this machine. */
const dpapiFile = (): string => join(configDir(), 'credentials.dpapi');

function psRun(script: string, input: string): string {
  return execFileSync('powershell', ['-NoProfile', '-NonInteractive', '-Command', script], {
    input,
    encoding: 'utf8',
    stdio: ['pipe', 'pipe', 'ignore'],
  }).trim();
}

function dpapiWrite(blob: string): void {
  mkdirSync(configDir(), { recursive: true, mode: 0o700 });
  const enc = psRun(
    '$p = [Console]::In.ReadToEnd(); ConvertFrom-SecureString -SecureString (ConvertTo-SecureString -String $p -AsPlainText -Force)',
    blob,
  );
  writeFileSync(dpapiFile(), `${enc}\n`, { mode: 0o600 });
}

function dpapiRead(): string | null {
  try {
    const enc = readFileSync(dpapiFile(), 'utf8').trim();
    if (!enc) return null;
    return psRun(
      '$e = [Console]::In.ReadToEnd().Trim(); ' +
        '$s = ConvertTo-SecureString -String $e; ' +
        '[Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))',
      enc,
    );
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------- keychain

function keychainRead(): AppCreds | null {
  try {
    if (platform() === 'win32') {
      const raw = dpapiRead();
      return raw ? (JSON.parse(raw) as AppCreds) : null;
    }
    if (platform() === 'darwin') {
      const raw = execFileSync('security', ['find-generic-password', '-s', SERVICE, '-a', ACCOUNT, '-w'], {
        encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'ignore'],
      }).trim();
      return raw ? (JSON.parse(raw) as AppCreds) : null;
    }
    const raw = execFileSync('secret-tool', ['lookup', 'service', SERVICE, 'account', ACCOUNT], {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    }).trim();
    return raw ? (JSON.parse(raw) as AppCreds) : null;
  } catch {
    return null;
  }
}

function keychainWrite(creds: AppCreds): void {
  const blob = JSON.stringify(creds);
  if (platform() === 'win32') {
    dpapiWrite(blob);
    return;
  }
  if (platform() === 'darwin') {
    // -U updates in place. The blob travels in argv of `security` only; it is
    // never written to a file we control and never printed.
    execFileSync('security', ['add-generic-password', '-U', '-s', SERVICE, '-a', ACCOUNT, '-w', blob], {
      stdio: ['ignore', 'ignore', 'pipe'],
    });
    return;
  }
  execFileSync('secret-tool', ['store', '--label', 'herdr-lark', 'service', SERVICE, 'account', ACCOUNT], {
    input: blob,
    stdio: ['pipe', 'ignore', 'pipe'],
  });
}

function keychainClear(): void {
  try {
    if (platform() === 'win32') {
      if (existsSync(dpapiFile())) unlinkSync(dpapiFile());
      return;
    }
    if (platform() === 'darwin')
      execFileSync('security', ['delete-generic-password', '-s', SERVICE, '-a', ACCOUNT], { stdio: 'ignore' });
    else execFileSync('secret-tool', ['clear', 'service', SERVICE, 'account', ACCOUNT], { stdio: 'ignore' });
  } catch {
    // nothing stored
  }
}

// ---------------------------------------------------------------- file store

function fileRead(): AppCreds | null {
  const f = credentialsFile();
  try {
    const st = statSync(f);
    // 0600 is the contract; anything looser is a real exposure, not a nit.
    if (platform() !== 'win32' && (st.mode & 0o077) !== 0)
      process.stderr.write(`herdr-lark: 警告 ${f} 权限过宽（${(st.mode & 0o777).toString(8)}），建议 chmod 600\n`);
    return JSON.parse(readFileSync(f, 'utf8')) as AppCreds;
  } catch {
    return null;
  }
}

function fileWrite(creds: AppCreds): void {
  const f = credentialsFile();
  mkdirSync(dirname(f), { recursive: true, mode: 0o700 });
  const tmp = `${f}.tmp`;
  writeFileSync(tmp, `${JSON.stringify(creds, null, 2)}\n`, { mode: 0o600 });
  chmodSync(tmp, 0o600);
  renameSync(tmp, f);
}

// ---------------------------------------------------------------- dotenv

/** Minimal dotenv reader: KEY=VALUE, optional quotes, `#` comments. */
export function readEnvFile(path: string): Record<string, string> {
  const out: Record<string, string> = {};
  let text: string;
  try {
    text = readFileSync(path, 'utf8');
  } catch {
    return out;
  }
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#')) continue;
    const eq = line.indexOf('=');
    if (eq <= 0) continue;
    const key = line.slice(0, eq).trim().replace(/^export\s+/, '');
    let value = line.slice(eq + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'")))
      value = value.slice(1, -1);
    if (key) out[key] = value;
  }
  return out;
}

// ---------------------------------------------------------------- resolution

function pair(id: string | undefined, secret: string | undefined): { appId: string; appSecret: string } | null {
  const a = id?.trim();
  const s = secret?.trim();
  return a && s ? { appId: a, appSecret: s } : null;
}

/**
 * Resolution order, highest first. Documented in README — users on a shared
 * machine need to know which layer wins.
 *
 *   1. HERDR_LARK_APP_ID / HERDR_LARK_APP_SECRET       (prefixed env)
 *   2. the env file (HERDR_LARK_ENV_FILE, else <config>/.env)
 *   3. the OS keychain                                  (what `setup` writes)
 *   4. <config>/credentials.json, mode 0600
 *   5. LARK_APP_ID / LARK_APP_SECRET                    (unprefixed, last resort)
 *
 * The unprefixed pair is last deliberately: several Lark tools read those
 * names, so a machine running more than one would otherwise cross-wire.
 */
export function resolveCreds(): ResolvedCreds | null {
  const prefixed = pair(process.env.HERDR_LARK_APP_ID, process.env.HERDR_LARK_APP_SECRET);
  if (prefixed)
    return { ...prefixed, ownerOpenId: process.env.HERDR_LARK_OWNER_OPEN_ID?.trim(), source: 'env', origin: '环境变量 HERDR_LARK_APP_ID/SECRET' };

  const ef = envFile();
  if (existsSync(ef)) {
    const vars = readEnvFile(ef);
    const fromFile =
      pair(vars.HERDR_LARK_APP_ID, vars.HERDR_LARK_APP_SECRET) ?? pair(vars.LARK_APP_ID, vars.LARK_APP_SECRET);
    if (fromFile)
      return { ...fromFile, ownerOpenId: vars.HERDR_LARK_OWNER_OPEN_ID, source: 'env-file', origin: ef };
  }

  const kc = keychainRead();
  if (kc?.appId && kc.appSecret) return { ...kc, source: 'keychain', origin: `${keychainName()} (service: ${SERVICE})` };

  const f = fileRead();
  if (f?.appId && f.appSecret) return { ...f, source: 'file', origin: credentialsFile() };

  const generic = pair(process.env.LARK_APP_ID, process.env.LARK_APP_SECRET);
  if (generic)
    return { ...generic, source: 'env-generic', origin: '环境变量 LARK_APP_ID/SECRET（通用名，可能与其他飞书工具冲突）' };

  return null;
}

export function writeCreds(creds: AppCreds, store: StoreKind = defaultStore()): string {
  if (store === 'none') return '没有落盘（这次只在内存里用）';
  if (store === 'keychain') {
    keychainWrite(creds);
    return `${keychainName()} (service: ${SERVICE})`;
  }
  fileWrite(creds);
  return credentialsFile();
}

export function clearCreds(): void {
  keychainClear();
  try {
    if (existsSync(credentialsFile())) unlinkSync(credentialsFile());
    if (existsSync(dpapiFile())) unlinkSync(dpapiFile());
  } catch {
    // best effort
  }
}

/** Every place credentials could come from, for `status` / diagnostics. */
export function credsReport(): string[] {
  const lines: string[] = [];
  const mark = (ok: boolean): string => (ok ? '✓' : '·');
  lines.push(`${mark(!!pair(process.env.HERDR_LARK_APP_ID, process.env.HERDR_LARK_APP_SECRET))} 环境变量 HERDR_LARK_APP_ID / HERDR_LARK_APP_SECRET`);
  const ef = envFile();
  const vars = existsSync(ef) ? readEnvFile(ef) : {};
  lines.push(`${mark(!!(pair(vars.HERDR_LARK_APP_ID, vars.HERDR_LARK_APP_SECRET) ?? pair(vars.LARK_APP_ID, vars.LARK_APP_SECRET)))} env 文件 ${ef}`);
  lines.push(`${mark(!!keychainRead())} ${keychainName()} (service: ${SERVICE})${keychainAvailable() ? '' : '（本机不可用）'}`);
  lines.push(`${mark(!!fileRead())} ${credentialsFile()}`);
  lines.push(`${mark(!!pair(process.env.LARK_APP_ID, process.env.LARK_APP_SECRET))} 环境变量 LARK_APP_ID / LARK_APP_SECRET（通用名，最后兜底）`);
  return lines;
}
