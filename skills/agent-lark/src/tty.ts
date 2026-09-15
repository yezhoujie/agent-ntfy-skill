import { createInterface } from 'node:readline/promises';

/**
 * What the interactive `setup` needs from a terminal: a visible question, a
 * hidden one (the App Secret), and a way to tell that the human gave up.
 * Tests hand in a scripted implementation; the real one wraps the terminal.
 */
export interface SetupIO {
  isTTY: boolean;
  question(prompt: string): Promise<string>;
  /** Like `question`, but nothing typed is echoed. */
  questionHidden(prompt: string): Promise<string>;
  close(): void;
}

/** Thrown out of a question when the human pressed Ctrl-C / Ctrl-D or the input ended. */
export class InputInterrupted extends Error {
  constructor() {
    super('input interrupted');
    this.name = 'InputInterrupted';
  }
}

type RawInput = NodeJS.ReadStream & { isRaw?: boolean; setRawMode?: (mode: boolean) => unknown };

/**
 * The real terminal. A visible question is one readline interface, opened for
 * that question and closed after it (so no readline listener is left on stdin
 * while the hidden read runs). A hidden question bypasses readline entirely:
 * stdin is put in raw mode, so the terminal echoes nothing, and the bytes are
 * collected here until Enter — Backspace edits, Ctrl-C / Ctrl-D interrupt.
 * Raw mode is what makes this work the same on Windows consoles, where
 * readline's own echo cannot be switched off reliably.
 */
export function terminalIO(input: NodeJS.ReadStream = process.stdin, output: NodeJS.WriteStream = process.stdout): SetupIO {
  const raw = input as RawInput;
  return {
    isTTY: Boolean(input.isTTY),
    async question(prompt) {
      const rl = createInterface({ input, output, terminal: true });
      const control = new AbortController();
      rl.on('SIGINT', () => control.abort());
      rl.on('close', () => control.abort());
      try {
        return await rl.question(prompt, { signal: control.signal });
      } catch (err) {
        if ((err as { name?: string }).name === 'AbortError') throw new InputInterrupted();
        throw err;
      } finally {
        rl.close();
      }
    },
    questionHidden(prompt) {
      output.write(prompt);
      const wasRaw = raw.isRaw ?? false;
      raw.setRawMode?.(true);
      input.resume();
      return new Promise<string>((resolve, reject) => {
        let typed = '';
        const finish = (): void => {
          input.off('data', onData);
          input.off('end', onEnd);
          raw.setRawMode?.(wasRaw);
          input.pause();
          output.write('\n');
        };
        const onEnd = (): void => {
          finish();
          reject(new InputInterrupted());
        };
        const onData = (chunk: Buffer | string): void => {
          for (const ch of chunk.toString()) {
            if (ch === '\r' || ch === '\n') {
              finish();
              resolve(typed);
              return;
            }
            if (ch === '' || ch === '') {
              finish();
              reject(new InputInterrupted());
              return;
            }
            if (ch === '' || ch === '\b') {
              typed = typed.slice(0, -1);
              continue;
            }
            typed += ch;
          }
        };
        input.on('data', onData);
        input.on('end', onEnd);
      });
    },
    close() {
      // Nothing is held open between questions.
    },
  };
}
