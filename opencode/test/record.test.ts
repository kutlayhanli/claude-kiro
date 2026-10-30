import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import {
  RunRecordWriter,
  agentRowFromError,
  agentRowFromOutcome,
  packageVersion,
  workflowNameOf,
} from "../src/record.ts";
import type { ChildOutcome, TokenUsage } from "../src/run-child.ts";

const TOKENS: TokenUsage = { input: 100, output: 10, reasoning: 2, cache: { read: 50, write: 0 } };
const PACKAGE_ROOT = join(import.meta.dirname, "..");

const OUTCOME: ChildOutcome = {
  value: { ok: true },
  sessionID: "ses_child",
  agent: "ck-worker",
  requestedModel: "sonnet",
  requestedEffort: "medium",
  requestedVariant: "high",
  resolvedModel: { providerID: "opencode", id: "claude-sonnet-5-5", variant: "high" },
  resolvedVariant: "high",
  tokens: TOKENS,
  cost: 0,
  startedAt: 1000,
  endedAt: 2500,
  attempts: 2,
  submission: "tool",
  rejections: 1,
  notes: [],
  guardEvents: [],
  peakContext: 150,
};

function writer(dir: string): RunRecordWriter {
  const record = new RunRecordWriter(join(dir, "record.json"), {
    runId: "wf-test",
    workflow: workflowNameOf("spec:spec-implement"),
    collectorVersion: "0.10.3",
    ckVersion: "0.10.3",
    projectRoot: "/proj",
    spec: "auth",
    into: "main",
    args: { spec: "auth" },
    startedAt: 1000,
  });
  record.start();
  return record;
}

test("workflowNameOf maps the shipped workflows and leaves inline as other", () => {
  assert.equal(workflowNameOf("spec:spec-create"), "spec-create");
  assert.equal(workflowNameOf("spec:spec-implement"), "spec-implement");
  assert.equal(workflowNameOf("inline"), "other");
});

test("packageVersion reads the package manifest", () => {
  assert.equal(packageVersion(PACKAGE_ROOT), "0.10.3");
  assert.equal(packageVersion("/does/not/exist"), "0.0.0");
});

test("an outcome becomes a complete ok row", () => {
  const row = agentRowFromOutcome(OUTCOME, {
    seq: 1,
    label: "task 3",
    phase: "Wave 1",
    agent: "ck-worker",
    parentSessionID: "ses_parent",
  });
  assert.equal(row.status, "ok");
  assert.equal(row.durationMs, 1500);
  assert.equal(row.requested.model, "sonnet");
  assert.equal(row.requestedEffort, "medium");
  assert.equal(row.resolved?.providerID, "opencode");
  assert.equal(row.resolvedEffort, "high");
  assert.equal(row.tokens.input, 100);
  assert.equal(row.peakContext, 150);
  assert.equal(row.error, null);
});

test("an error becomes a failed row with the mapped status", () => {
  const base = { seq: 2, label: "task 4", phase: null, agent: "ck-worker", parentSessionID: "ses_parent" };
  const timeout = agentRowFromError(
    {
      kind: "timeout",
      message: "took too long",
      details: {
        sessionID: "ses_child",
        tokens: TOKENS,
        startedAt: 1000,
        endedAt: 4000,
        attempts: 2,
        resolvedModel: { providerID: "opencode", id: "x" },
        requestedModel: "opus",
      },
    },
    base,
  );
  assert.equal(timeout.status, "timeout");
  assert.equal(timeout.durationMs, 3000);
  assert.equal(timeout.error, "took too long");
  assert.equal(timeout.requested.model, "opus");

  for (const [kind, status] of [
    ["model", "model_error"],
    ["agent", "agent_error"],
    ["session", "session_error"],
    ["result", "result_error"],
    ["guard", "failed"],
  ] as const) {
    assert.equal(agentRowFromError({ kind, message: "x" }, base).status, status);
  }
});

test("the writer starts partial, records agents and guard counts, and closes once", () => {
  const dir = mkdtempSync(join(tmpdir(), "ck-record-"));
  try {
    const record = writer(dir);
    const partial = JSON.parse(readFileSync(join(dir, "record.json"), "utf8"));
    assert.equal(partial.schema, "ck-opencode-run/1");
    assert.equal(partial.harness, "opencode");
    assert.equal(partial.workflow, "spec-implement");
    assert.equal(partial.status, "running");
    assert.equal(partial.endedAt, null);
    assert.deepEqual(partial.agents, []);

    const row = agentRowFromOutcome(OUTCOME, {
      seq: 1,
      label: "task 3",
      phase: "Wave 1",
      agent: "ck-worker",
      parentSessionID: "ses_parent",
    });
    record.addAgent(row, [
      { at: 1, kind: "deny", agent: "ck-worker", reason: "[ck guard] denied" },
      { at: 2, kind: "post_check", agent: "ck-worker", reason: "[ck post-check] weakened" },
    ]);
    const withAgent = JSON.parse(readFileSync(join(dir, "record.json"), "utf8"));
    assert.equal(withAgent.agents.length, 1);
    assert.equal(withAgent.agents[0].label, "task 3");
    assert.equal(withAgent.retries, 1);
    assert.equal(withAgent.guard.denials, 1);
    assert.equal(withAgent.guard.postCheckCatches, 1);
    assert.equal(withAgent.guard.events.length, 2);

    record.finish({ status: "done", result: { merged: 1 }, endedAt: 3000 });
    const done = JSON.parse(readFileSync(join(dir, "record.json"), "utf8"));
    assert.equal(done.status, "done");
    assert.equal(done.endedAt, 3000);
    assert.deepEqual(done.result, { merged: 1 });
    assert.equal(existsSync(join(dir, "record.json.tmp")), false);

    // finish is idempotent
    record.finish({ status: "failed", endedAt: 9999 });
    assert.equal(JSON.parse(readFileSync(join(dir, "record.json"), "utf8")).status, "done");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("a provider throttle in a failed row increments the run throttle count", () => {
  const dir = mkdtempSync(join(tmpdir(), "ck-record-"));
  try {
    const record = writer(dir);
    record.addAgent(
      agentRowFromError(
        { kind: "failed", message: "provider 429 rate limit: retryAfterMs 1000" },
        { seq: 1, label: "task 1", phase: null, agent: "ck-worker", parentSessionID: "p" },
      ),
    );
    const data = JSON.parse(readFileSync(join(dir, "record.json"), "utf8"));
    assert.equal(data.throttles, 1);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("headless denials are counted separately", () => {
  const dir = mkdtempSync(join(tmpdir(), "ck-record-"));
  try {
    const record = writer(dir);
    record.addGuard([{ at: 1, kind: "headless", agent: "ck-worker", reason: "needs approval" }]);
    const data = JSON.parse(readFileSync(join(dir, "record.json"), "utf8"));
    assert.equal(data.guard.headlessDenials, 1);
    assert.equal(data.guard.denials, 0);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
