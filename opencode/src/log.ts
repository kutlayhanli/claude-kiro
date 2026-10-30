/**
 * Run logs live outside the worktree under test. A model under test once edited a log file it could
 * see; the harness (and this plugin) must never let that happen.
 */

import { appendFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";

export interface RunLogger {
  readonly path: string;
  write(text: string): void;
}

export function createRunLogger(path: string): RunLogger {
  mkdirSync(dirname(path), { recursive: true });
  return {
    path,
    write(text: string) {
      const stamp = new Date().toISOString();
      const lines = String(text).split("\n");
      const body = lines.map((line) => `[${stamp}] ${line}`).join("\n");
      try {
        appendFileSync(path, `${body}\n`);
      } catch {
        // Logging must never take down a run.
      }
    },
  };
}
