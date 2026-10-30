/**
 * The `AGENTS.md` managed block.
 *
 * The output style is the project's standing instructions. It is installed into AGENTS.md between
 * markers so a user's own instructions survive: only the block is replaced.
 */

import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { parseFrontmatter, rewriteText } from "./rewrite.ts";

export const BEGIN_MARKER = "<!-- ck:begin -->";
export const END_MARKER = "<!-- ck:end -->";

export interface InstructionsResult {
  path: string;
  changed: boolean;
  appended: boolean;
}

export function renderInstructionsBlock(packageRoot: string, projectDir: string): string {
  const path = join(packageRoot, "vendor", "output-styles", "spec-driven.md");
  let source: string;
  try {
    source = readFileSync(path, "utf8");
  } catch {
    throw new Error(`vendor/output-styles/spec-driven.md is missing; run \`npm run sync-vendor\` from opencode/`);
  }
  const { body } = parseFrontmatter(source);
  const rewritten = rewriteText(body, {
    packageRoot,
    projectDir,
    workflowName: "spec:spec-create",
  }).trim();
  if (!rewritten) throw new Error(`${path}: the output style has no body`);
  return `${BEGIN_MARKER}\n${rewritten}\n${END_MARKER}`;
}

/** Replace only the managed block; append it when no markers exist; create the file when missing. */
export function applyManagedBlock(existing: string | null, block: string): string {
  if (existing === null || existing.trim() === "") return `${block}\n`;

  const begin = existing.indexOf(BEGIN_MARKER);
  const end = existing.indexOf(END_MARKER);
  if (begin !== -1 && end !== -1 && end > begin) {
    return `${existing.slice(0, begin)}${block}${existing.slice(end + END_MARKER.length)}`;
  }

  const separator = existing.endsWith("\n") ? "\n" : "\n\n";
  return `${existing}${separator}${block}\n`;
}

export function writeInstructions(projectDir: string, packageRoot: string): InstructionsResult {
  const path = join(projectDir, "AGENTS.md");
  const existing = existsSync(path) ? readFileSync(path, "utf8") : null;
  const block = renderInstructionsBlock(packageRoot, projectDir);
  const next = applyManagedBlock(existing, block);
  const appended = existing !== null && !existing.includes(BEGIN_MARKER);
  if (next === existing) return { path, changed: false, appended: false };
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, next);
  return { path, changed: true, appended };
}
