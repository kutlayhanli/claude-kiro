/**
 * Write the live workflow status the TUI status line reads.
 *
 * One file per run under `<project>/.opencode/ck/status/<runId>.json`, written atomically after every
 * phase and agent transition. Status files are tiny and non-critical: a failed write must never fail
 * the workflow, so the writer swallows filesystem errors.
 */

import { mkdirSync, renameSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { STATUS_SCHEMA, type WorkflowStatus } from "./status-format.ts";

export interface StatusEnvelope {
  workflow: string;
  spec: string | null;
  startedAt: number;
  total: number;
  log: string;
}

export interface StatusAgentRow {
  label?: string | null;
  status?: string | null;
  cost?: number | null;
}

export function statusDir(projectDir: string): string {
  return join(projectDir, ".opencode", "ck", "status");
}

export function statusPath(projectDir: string, runId: string): string {
  return join(statusDir(projectDir), `${runId}.json`);
}

export class RunStatusWriter {
  readonly path: string;
  private readonly data: WorkflowStatus;
  private readonly running = new Set<string>();

  constructor(projectDir: string, runId: string, envelope: StatusEnvelope) {
    this.path = statusPath(projectDir, runId);
    this.data = {
      schema: STATUS_SCHEMA,
      runId,
      workflow: envelope.workflow,
      spec: envelope.spec,
      projectRoot: projectDir,
      status: "running",
      phase: null,
      startedAt: envelope.startedAt,
      updatedAt: envelope.startedAt,
      endedAt: null,
      total: envelope.total,
      merged: 0,
      failed: 0,
      running: [],
      agents: 0,
      cost: 0,
      last: null,
      log: envelope.log,
    };
  }

  start(): void {
    this.write();
  }

  phase(name: string): void {
    this.data.phase = name || null;
    this.write();
  }

  agentStart(label: string): void {
    this.running.add(label);
    this.write();
  }

  agentEnd(label: string, rows: readonly StatusAgentRow[]): void {
    this.running.delete(label);
    this.recompute(rows);
    this.write();
  }

  finish(status: "done" | "failed", rows?: readonly StatusAgentRow[]): void {
    if (rows) this.recompute(rows);
    this.running.clear();
    this.data.status = status;
    this.data.endedAt = Date.now();
    this.write();
  }

  private recompute(rows: readonly StatusAgentRow[]): void {
    const merged = new Set<number>();
    const failed = new Set<number>();
    for (const row of rows) {
      const label = typeof row.label === "string" ? row.label : "";
      const match = /(?:^|\s)(?:task|land)\s+(\d+)/i.exec(label);
      if (!match) continue;
      const task = Number(match[1]);
      if (/^land\b/i.test(label) && row.status === "ok") merged.add(task);
      if (/^task\b/i.test(label) && row.status !== "ok") failed.add(task);
    }
    for (const task of merged) failed.delete(task);
    this.data.merged = merged.size;
    this.data.failed = failed.size;
    this.data.agents = rows.length;
    this.data.last = rows.length > 0 ? (rows[rows.length - 1]?.label ?? null) : null;
    const cost = rows.reduce((sum, row) => sum + (typeof row.cost === "number" ? row.cost : 0), 0);
    this.data.cost = Number(cost.toFixed(6));
    this.data.running = [...this.running];
  }

  private write(): void {
    this.data.updatedAt = Date.now();
    this.data.running = [...this.running];
    try {
      mkdirSync(statusDir(this.data.projectRoot), { recursive: true });
      const temporary = `${this.path}.${process.pid}.tmp`;
      writeFileSync(temporary, `${JSON.stringify(this.data, null, 2)}\n`);
      renameSync(temporary, this.path);
    } catch {
      // A status file is a convenience; never turn a display failure into a workflow failure.
    }
  }
}
