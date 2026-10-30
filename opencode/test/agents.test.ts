import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync, writeFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { agentSpecs, materializeAgents, renderAgent } from "../src/agents.ts";

const PACKAGE_ROOT = join(import.meta.dirname, "..");

function digest(text: string): string {
  return createHash("sha256").update(text).digest("hex");
}

test("the five role agents plus the generic worker are generated", () => {
  const specs = agentSpecs(PACKAGE_ROOT);
  assert.deepEqual(
    specs.map((spec) => spec.id),
    ["ck-fixer", "ck-implementer", "ck-resolver", "ck-reviewer", "ck-test-writer", "ck-worker"],
  );
  for (const spec of specs) {
    assert.doesNotMatch(spec.id, /\//, "agent ids must be flat for OpenCode");
    assert.ok(spec.description);
    assert.ok(spec.body.length > 0);
  }
});

test("the reviewer has no edit tools but keeps shell for git diff and ck lint", () => {
  const reviewer = agentSpecs(PACKAGE_ROOT).find((spec) => spec.id === "ck-reviewer");
  assert.ok(reviewer);
  assert.deepEqual(
    reviewer.permissions.map((rule) => rule.action),
    ["edit", "write", "patch", "apply_patch"],
  );
  assert.ok(!reviewer.permissions.some((rule) => rule.action === "shell"));
});

test("rendered agents carry no model and no Claude tools field", () => {
  const reviewer = agentSpecs(PACKAGE_ROOT).find((spec) => spec.id === "ck-reviewer");
  assert.ok(reviewer);
  const text = renderAgent(reviewer);
  assert.match(text, /^---\ndescription: /);
  assert.match(text, /\nmode: subagent\n/);
  assert.match(text, /\npermissions:\n/);
  assert.doesNotMatch(text, /\nmodel:/);
  assert.doesNotMatch(text, /\ntools:/);
  assert.match(text, /\n {2}- action: "edit"\n {4}resource: "\*"\n {4}effect: deny\n/);
});

test("materialization is idempotent and preserves an edited file", () => {
  const dir = mkdtempSync(join(tmpdir(), "ck-agents-"));
  try {
    const first = materializeAgents(dir, PACKAGE_ROOT);
    assert.equal(first.written.length, 6);
    assert.deepEqual(first.unchanged, []);
    assert.deepEqual(first.conflicts, []);

    const worker = join(dir, ".opencode", "agents", "ck-worker.md");
    assert.ok(existsSync(worker));
    const generated = readFileSync(worker, "utf8");

    const second = materializeAgents(dir, PACKAGE_ROOT);
    assert.deepEqual(second.written, []);
    assert.equal(second.unchanged.length, 6);
    assert.deepEqual(second.conflicts, []);
    assert.equal(readFileSync(worker, "utf8"), generated);

    const edited = `${generated}\nMy local note.\n`;
    writeFileSync(worker, edited);
    const third = materializeAgents(dir, PACKAGE_ROOT);
    assert.deepEqual(third.conflicts, ["ck-worker.md"]);
    assert.equal(readFileSync(worker, "utf8"), edited);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("an older generated agent is replaced, not treated as a user edit", () => {
  const dir = mkdtempSync(join(tmpdir(), "ck-agents-"));
  try {
    materializeAgents(dir, PACKAGE_ROOT);
    const fixer = join(dir, ".opencode", "agents", "ck-fixer.md");
    const manifestPath = join(dir, ".opencode", "agents", "ck-agents.json");
    writeFileSync(fixer, "OLD GENERATED CONTENT\n");
    const manifest = JSON.parse(readFileSync(manifestPath, "utf8")) as {
      files: Record<string, string>;
    };
    manifest.files["ck-fixer.md"] = digest("OLD GENERATED CONTENT\n");
    writeFileSync(manifestPath, JSON.stringify(manifest));

    const result = materializeAgents(dir, PACKAGE_ROOT);
    assert.ok(result.written.includes("ck-fixer.md"));
    assert.notEqual(readFileSync(fixer, "utf8"), "OLD GENERATED CONTENT\n");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
