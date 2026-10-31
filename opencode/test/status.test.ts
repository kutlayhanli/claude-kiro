import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { RunStatusWriter, statusPath } from "../src/status.ts";
import {
  formatWorkflowStatus,
  isWorkflowStatus,
  pickActiveStatus,
  type WorkflowStatus,
} from "../src/status-format.ts";

function read(path: string): WorkflowStatus {
  return JSON.parse(readFileSync(path, "utf8")) as WorkflowStatus;
}

function status(overrides: Partial<WorkflowStatus> = {}): WorkflowStatus {
  return {
    schema: "ck-opencode-status/1",
    runId: "wf-1",
    workflow: "spec-implement",
    spec: "demo",
    projectRoot: "/proj",
    status: "running",
    phase: null,
    startedAt: 0,
    updatedAt: 0,
    endedAt: null,
    total: 10,
    merged: 0,
    failed: 0,
    running: [],
    agents: 0,
    cost: 0,
    last: null,
    log: "/tmp/run.log",
    ...overrides,
  };
}

test("the status writer tracks phases, running agents, merges and failures", () => {
  const dir = mkdtempSync(join(tmpdir(), "ck-status-"));
  try {
    const writer = new RunStatusWriter(dir, "wf-1", {
      workflow: "spec-implement",
      spec: "demo",
      startedAt: 1000,
      total: 10,
      log: "/tmp/run.log",
    });
    writer.start();
    let data = read(statusPath(dir, "wf-1"));
    assert.equal(data.status, "running");
    assert.equal(data.total, 10);
    assert.deepEqual(data.running, []);

    writer.phase("Wave 1");
    writer.agentStart("task 1 [test]");
    writer.agentStart("task 2 [test]");
    writer.agentEnd("task 1 [test]", [
      { label: "task 1 [test]", status: "ok", cost: 0.01 },
      { label: "land 1", status: "ok", cost: 0.02 },
      { label: "task 2 [test]", status: "failed", cost: 0.03 },
    ]);
    data = read(statusPath(dir, "wf-1"));
    assert.equal(data.phase, "Wave 1");
    assert.equal(data.merged, 1);
    assert.equal(data.failed, 1);
    assert.deepEqual(data.running, ["task 2 [test]"]);
    assert.equal(data.agents, 3);
    assert.equal(data.cost, 0.06);

    writer.agentEnd("task 2 [test]", [
      { label: "task 1 [test]", status: "ok", cost: 0.01 },
      { label: "land 1", status: "ok", cost: 0.02 },
      { label: "task 2 [test]", status: "ok", cost: 0.03 },
      { label: "land 2", status: "ok", cost: 0.04 },
    ]);
    data = read(statusPath(dir, "wf-1"));
    assert.equal(data.merged, 2);
    assert.equal(data.failed, 0);
    assert.deepEqual(data.running, []);

    writer.finish("done", [
      { label: "task 1 [test]", status: "ok", cost: 0.01 },
      { label: "land 1", status: "ok", cost: 0.02 },
      { label: "task 2 [test]", status: "ok", cost: 0.03 },
      { label: "land 2", status: "ok", cost: 0.04 },
    ]);
    data = read(statusPath(dir, "wf-1"));
    assert.equal(data.status, "done");
    assert.equal(data.endedAt !== null, true);
    assert.deepEqual(data.running, []);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("the status line is compact and reports the running task", () => {
  const line = formatWorkflowStatus(
    status({
      merged: 12,
      total: 210,
      failed: 1,
      running: ["task 5 [test]"],
      agents: 14,
      startedAt: 0,
      updatedAt: 0,
    }),
    8 * 60_000,
  );
  assert.equal(line, "▶ ck demo · 12/210 · task 5 · 1 failed · 8m · 14 agents");
});

test("a finished status line switches icon and time", () => {
  assert.equal(
    formatWorkflowStatus(
      status({ status: "done", merged: 2, total: 2, failed: 0, endedAt: 60 * 60_000 }),
      2 * 60 * 60_000,
    ),
    "✓ ck demo · 2/2 · 1h00m",
  );
  assert.equal(
    formatWorkflowStatus(
      status({ status: "failed", merged: 1, total: 2, failed: 1, endedAt: 5 * 60_000 }),
      10 * 60_000,
    ),
    "! ck demo · 1/2 · 1 failed · 5m",
  );
});

test("pickActiveStatus prefers a running run and expires old finished ones", () => {
  const running = status({ runId: "running", status: "running", updatedAt: 100 });
  const done = status({ runId: "done", status: "done", updatedAt: 200 });
  assert.equal(pickActiveStatus([done, running], 1_000)?.runId, "running");
  assert.equal(pickActiveStatus([done], 1_000)?.runId, "done");
  assert.equal(pickActiveStatus([done], 400_000), null);
});

test("isWorkflowStatus rejects another schema or a missing run id", () => {
  assert.equal(isWorkflowStatus(status()), true);
  assert.equal(isWorkflowStatus({ ...status(), schema: "other" }), false);
  assert.equal(isWorkflowStatus({ ...status(), runId: 7 }), false);
});
