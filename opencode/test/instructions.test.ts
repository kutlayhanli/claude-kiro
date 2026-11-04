import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import {
  BEGIN_MARKER,
  END_MARKER,
  applyManagedBlock,
  renderInstructionsBlock,
  writeInstructions,
} from "../src/instructions.ts";

const PACKAGE_ROOT = join(import.meta.dirname, "..");
const PROJECT_DIR = "/tmp/ck-instructions-project";

test("the instructions block is rewritten and marker-delimited", () => {
  const block = renderInstructionsBlock(PACKAGE_ROOT, PROJECT_DIR);
  assert.ok(block.startsWith(BEGIN_MARKER));
  assert.ok(block.endsWith(END_MARKER));
  assert.match(block, /\/spec\/plan/);
  assert.doesNotMatch(block, /\/spec:/);
  assert.doesNotMatch(block, /TodoWrite|AskUserQuestion/);
  assert.doesNotMatch(block, /\$\{CLAUDE_PLUGIN_ROOT\}/);
});

test("applyManagedBlock replaces only the marked block", () => {
  const existing = `# My rules\n\nKeep this.\n\n${BEGIN_MARKER}\nold block\n${END_MARKER}\n\nTail stays.\n`;
  const next = applyManagedBlock(existing, `${BEGIN_MARKER}\nnew block\n${END_MARKER}`);
  assert.match(next, /^# My rules\n\nKeep this\.\n\n<!-- ck:begin -->\nnew block\n<!-- ck:end -->\n\nTail stays\.\n$/);
  assert.doesNotMatch(next, /old block/);
});

test("applyManagedBlock appends when the markers are absent", () => {
  const next = applyManagedBlock("# My rules\n", `${BEGIN_MARKER}\nblock\n${END_MARKER}`);
  assert.equal(next, `# My rules\n\n${BEGIN_MARKER}\nblock\n${END_MARKER}\n`);
});

test("applyManagedBlock creates the whole file when there is none", () => {
  assert.equal(applyManagedBlock(null, `${BEGIN_MARKER}\nblock\n${END_MARKER}`), `${BEGIN_MARKER}\nblock\n${END_MARKER}\n`);
});

test("AGENTS.md is created once and user content outside the block survives", () => {
  const dir = mkdtempSync(join(tmpdir(), "ck-agents-md-"));
  try {
    const first = writeInstructions(dir, PACKAGE_ROOT);
    assert.equal(first.changed, true);
    assert.equal(first.appended, false);
    const path = join(dir, "AGENTS.md");
    const created = readFileSync(path, "utf8");
    assert.ok(created.includes(BEGIN_MARKER));

    const withUserText = `# My project\n\nLocal conventions.\n\n${created}`;
    writeFileSync(path, withUserText);
    const second = writeInstructions(dir, PACKAGE_ROOT);
    assert.equal(second.changed, false);
    const refreshed = readFileSync(path, "utf8");
    assert.ok(refreshed.startsWith("# My project\n\nLocal conventions.\n\n"));
    assert.equal(refreshed.split(BEGIN_MARKER).length, 2);

    const third = writeInstructions(dir, PACKAGE_ROOT);
    assert.equal(third.changed, false);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
