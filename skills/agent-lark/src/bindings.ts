import { readFileSync, renameSync, writeFileSync } from 'node:fs';
import { bindingsPath, ensureHomeDir } from './paths.js';
import { fill, msg } from './texts.js';
import type { Lang } from './validate.js';

/** Feishu only recommends a cap on group names; this one is enforced on the task part. */
export const MAX_TASK_NAME = 60;

export interface Binding {
  /** Project root the group belongs to. */
  root: string;
  label: string;
  /** Feishu group — the key; one group is never shared by two projects. */
  chatId: string;
  /** Group name as last set here; null when it was never set by us. */
  name: string | null;
  /** herdr pane that phone messages are injected into; refreshed on every call. */
  paneId: string | null;
  away: boolean;
  /** Language of the last ask / notify payload from this project; cards sent on the daemon's own initiative follow it. */
  lang: Lang | null;
  boundAt: string;
  /** null while this is the project's live group; the unbind time once it was let go of (kept so it can be offered back). */
  releasedAt: string | null;
}

/** `<task> [<dir>]`, or just `[<dir>]` when there is no task name. */
export function groupName(task: string | undefined, label: string): string {
  const t = task?.trim();
  return t ? `${t} [${label}]` : `[${label}]`;
}

/** Why a task name cannot be used, or null when it can. Length is counted in code points, as Feishu counts. */
export function taskNameProblem(task: string): string | null {
  const n = Array.from(task).length;
  return n > MAX_TASK_NAME ? fill(msg.taskNameTooLong, { max: MAX_TASK_NAME, n }) : null;
}

function str(v: unknown): string | null {
  return typeof v === 'string' ? v : null;
}

/** Accept whatever shape an earlier version wrote; anything missing gets its neutral value. */
function normalize(raw: unknown): Binding | null {
  if (!raw || typeof raw !== 'object') return null;
  const r = raw as Record<string, unknown>;
  if (typeof r.root !== 'string' || typeof r.chatId !== 'string') return null;
  const lang = r.lang === 'zh' || r.lang === 'en' ? r.lang : null;
  return {
    root: r.root,
    label: str(r.label) ?? r.root,
    chatId: r.chatId,
    name: str(r.name),
    paneId: str(r.paneId),
    away: r.away === true,
    lang,
    boundAt: str(r.boundAt) ?? '',
    releasedAt: str(r.releasedAt),
  };
}

export interface BindingStoreOptions {
  /** Called for every entry demoted while loading a file that held two live groups for one project. */
  onRepaired?: (info: { root: string; chatId: string }) => void;
  /** Called for every entry discarded while loading a file that listed one group under two projects. */
  onDropped?: (info: { root: string; chatId: string }) => void;
}

/** bindings.json exists but cannot be read as a list of bindings; it is left untouched. */
export class BindingsFileError extends Error {
  constructor(
    readonly path: string,
    cause: unknown,
  ) {
    super(fill(msg.bindingsFileBad, { path, error: cause instanceof Error ? cause.message : String(cause) }));
    this.name = 'BindingsFileError';
  }
}

export class BindingStore {
  private map = new Map<string, Binding>();

  constructor(private readonly opts: BindingStoreOptions = {}) {
    this.load();
  }

  /**
   * Read the file. A missing file is an empty store; anything else that
   * cannot be read is an error, so a damaged file is never silently replaced
   * by an empty one on the next write.
   */
  private load(): void {
    const file = bindingsPath();
    let text: string;
    try {
      text = readFileSync(file, 'utf8');
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code === 'ENOENT') return;
      throw new BindingsFileError(file, err);
    }
    let entries: unknown[];
    try {
      const parsed = JSON.parse(text) as { bindings?: unknown } | null;
      const list = parsed && typeof parsed === 'object' ? parsed.bindings : undefined;
      if (list !== undefined && !Array.isArray(list)) throw new Error('"bindings" is not an array');
      entries = list ?? [];
    } catch (err) {
      throw new BindingsFileError(file, err);
    }
    let dirty = false;
    // A file from before chat ids became the key may list one group under two
    // projects; the project that bound it last keeps it.
    for (const raw of entries) {
      const b = normalize(raw);
      if (!b) continue;
      const held = this.map.get(b.chatId);
      if (held) {
        const loser = held.boundAt <= b.boundAt ? held : b;
        this.opts.onDropped?.({ root: loser.root, chatId: loser.chatId });
        dirty = true;
        if (loser === b) continue;
      }
      this.map.set(b.chatId, b);
    }
    // A hand-edited or damaged file may hold two live entries for one project;
    // the one bound last stays live, the rest are treated as let go of.
    const liveByRoot = new Map<string, Binding>();
    for (const b of this.map.values()) {
      if (b.releasedAt !== null) continue;
      const other = liveByRoot.get(b.root);
      if (!other) {
        liveByRoot.set(b.root, b);
        continue;
      }
      const older = other.boundAt <= b.boundAt ? other : b;
      older.releasedAt = new Date().toISOString();
      if (older === other) liveByRoot.set(b.root, b);
      this.opts.onRepaired?.({ root: older.root, chatId: older.chatId });
      dirty = true;
    }
    // Written back once, so the next start does not repeat the repair.
    if (dirty) this.persist();
  }

  private persist(): void {
    ensureHomeDir();
    const file = bindingsPath();
    const tmp = `${file}.tmp`;
    const data = { bindings: [...this.map.values()] };
    writeFileSync(tmp, `${JSON.stringify(data, null, 2)}\n`, { mode: 0o600 });
    renameSync(tmp, file);
  }

  /** The project's live group, if it has one. */
  active(root: string): Binding | undefined {
    for (const b of this.map.values()) if (b.root === root && b.releasedAt === null) return b;
    return undefined;
  }

  /** Groups the project let go of, oldest release first. */
  released(root: string): Binding[] {
    return [...this.map.values()]
      .filter((b) => b.root === root && b.releasedAt !== null)
      .sort((a, b) => (a.releasedAt ?? '').localeCompare(b.releasedAt ?? ''));
  }

  byChat(chatId: string): Binding | undefined {
    return this.map.get(chatId);
  }

  /** Only a live group is listened to; a released one is still on record but no longer ours to act on. */
  activeByChat(chatId: string): Binding | undefined {
    const b = this.map.get(chatId);
    return b && b.releasedAt === null ? b : undefined;
  }

  all(): Binding[] {
    return [...this.map.values()];
  }

  activeAll(): Binding[] {
    return [...this.map.values()].filter((b) => b.releasedAt === null);
  }

  activeChatIds(): string[] {
    return this.activeAll().map((b) => b.chatId);
  }

  /** Add or replace an entry. A project has at most one live group; a second one is refused before anything is written. */
  set(b: Binding): void {
    if (b.releasedAt === null) {
      const live = this.active(b.root);
      if (live && live.chatId !== b.chatId) throw new Error(fill(msg.bindingsTwoActive, { root: b.root, chatId: live.chatId }));
    }
    this.map.set(b.chatId, b);
    this.persist();
  }

  /** Refresh the fields a live call carries on the project's live group, without disturbing the binding. */
  touch(root: string, patch: Partial<Pick<Binding, 'paneId' | 'away' | 'label' | 'lang' | 'name'>>): Binding | undefined {
    const b = this.active(root);
    if (!b) return undefined;
    if (patch.paneId !== undefined && patch.paneId !== null) b.paneId = patch.paneId;
    if (patch.away !== undefined) b.away = patch.away;
    if (patch.label) b.label = patch.label;
    if (patch.lang) b.lang = patch.lang;
    if (patch.name !== undefined) b.name = patch.name;
    this.persist();
    return b;
  }

  /** Let go of the project's live group: it stays on record, remote mode goes off with it. */
  release(root: string): Binding | undefined {
    const b = this.active(root);
    if (!b) return undefined;
    b.releasedAt = new Date().toISOString();
    b.away = false;
    this.persist();
    return b;
  }

  /** Forget a group altogether, live or released: for one that no longer exists in Feishu. */
  remove(chatId: string): Binding | undefined {
    const b = this.map.get(chatId);
    if (!b) return undefined;
    this.map.delete(chatId);
    this.persist();
    return b;
  }
}
