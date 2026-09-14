import { readFileSync, renameSync, writeFileSync } from 'node:fs';
import { bindingsPath, ensureHomeDir } from './paths.js';

export interface Binding {
  /** Project root — the identity everything is keyed by. */
  root: string;
  label: string;
  chatId: string;
  /** herdr pane that phone messages are injected into; refreshed on every call. */
  paneId: string | null;
  away: boolean;
  boundAt: string;
}

export class BindingStore {
  private map = new Map<string, Binding>();

  constructor() {
    this.load();
  }

  private load(): void {
    try {
      const raw = JSON.parse(readFileSync(bindingsPath(), 'utf8')) as { bindings?: Binding[] };
      for (const b of raw.bindings ?? []) {
        if (!b || typeof b.root !== 'string' || typeof b.chatId !== 'string') continue;
        this.map.set(b.root, b);
      }
    } catch {
      // no bindings yet
    }
  }

  private persist(): void {
    ensureHomeDir();
    const file = bindingsPath();
    const tmp = `${file}.tmp`;
    const data = { bindings: [...this.map.values()] };
    writeFileSync(tmp, `${JSON.stringify(data, null, 2)}\n`, { mode: 0o600 });
    renameSync(tmp, file);
  }

  get(root: string): Binding | undefined {
    return this.map.get(root);
  }

  byChat(chatId: string): Binding | undefined {
    for (const b of this.map.values()) if (b.chatId === chatId) return b;
    return undefined;
  }

  all(): Binding[] {
    return [...this.map.values()];
  }

  chatIds(): string[] {
    return [...this.map.values()].map((b) => b.chatId);
  }

  set(b: Binding): void {
    this.map.set(b.root, b);
    this.persist();
  }

  /** Refresh the fields a live call carries, without disturbing the binding. */
  touch(root: string, patch: Partial<Pick<Binding, 'paneId' | 'away' | 'label'>>): Binding | undefined {
    const b = this.map.get(root);
    if (!b) return undefined;
    if (patch.paneId !== undefined && patch.paneId !== null) b.paneId = patch.paneId;
    if (patch.away !== undefined) b.away = patch.away;
    if (patch.label) b.label = patch.label;
    this.persist();
    return b;
  }

  remove(root: string): boolean {
    const had = this.map.delete(root);
    if (had) this.persist();
    return had;
  }
}
