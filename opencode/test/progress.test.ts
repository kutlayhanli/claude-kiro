import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

const SCRIPT = join(import.meta.dirname, "..", "scripts", "progress.mjs");

function project(): string {
  const directory = mkdtempSync(join(tmpdir(), "ck-progress-"));
  const statuses = join(directory, ".opencode", "ck", "status");
  mkdirSync(statuses, { recursive: true });
  const now = Date.now();
  writeFileSync(
    join(statuses, "wf-test.json"),
    `${JSON.stringify({
      schema: "ck-opencode-status/1",
      runId: "wf-test",
      workflow: "spec-implement",
      spec: "demo",
      projectRoot: directory,
      status: "done",
      phase: "Report",
      startedAt: now - 40 * 60_000,
      updatedAt: now,
      endedAt: now,
      total: 198,
      merged: 7,
      failed: 2,
      running: [],
      agents: 52,
      cost: 0,
      last: "report",
      log: join(directory, "run.log"),
    })}\n`,
  );
  return directory;
}

test("progress shows the last finished run, not only a live one", () => {
  const directory = project();
  try {
    const json = JSON.parse(
      execFileSync(process.execPath, [SCRIPT, "--project", directory, "--json"], { encoding: "utf8" }),
    );
    assert.equal(json.runId, "wf-test");
    assert.equal(json.status, "done");
    const compact = execFileSync(process.execPath, [SCRIPT, "--project", directory, "--compact"], {
      encoding: "utf8",
    }).trim();
    assert.match(compact, /^! ck demo · 7\/198 · 2 failed · 40m$/);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("progress says when a project has no workflows", () => {
  const directory = mkdtempSync(join(tmpdir(), "ck-progress-empty-"));
  try {
    const output = execFileSync(process.execPath, [SCRIPT, "--project", directory], { encoding: "utf8" }).trim();
    assert.match(output, /no active ck workflow/);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
