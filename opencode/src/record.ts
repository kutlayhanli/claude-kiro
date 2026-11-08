/**
 * The native run record (`ck-opencode-run/1`), written outside the worktree at spawn and closed on
 * done/error. `ck` ingests it; no Claude transcripts are involved.
 *
 * A partial record is written atomically after every agent row, so a crashed run still shows what it
 * did instead of disappearing.
 */

import { spawnSync } from "node:child_process";
import { readFileSync, renameSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import type { GuardEvent } from "./guard.ts";
import type { ChildOutcome, ModelSource, TokenUsage } from "./run-child.ts";

export interface RunEnvelope {
  runId: string;
  workflow: "spec-create" | "spec-implement" | "other";
  collectorVersion: string;
  ckVersion: string | null;
  projectRoot: string;
  spec: string | null;
  into: string | null;
  args: Record<string, unknown>;
  startedAt: number;
}

export type AgentStatus =
  | "ok"
  | "failed"
  | "timeout"
  | "agent_error"
  | "result_error"
  | "model_error"
  | "session_error";

export interface AgentRow {
  seq: number;
  label: string;
  phase: string | null;
  role: string | null;
  agent: string;
  parentSessionID: string;
  sessionID: string | null;
  requested: { model: string; effort: string | null };
  resolved: { providerID: string; id: string; variant: string | null } | null;
  requestedEffort: string | null;
  resolvedEffort: string | null;
  variant: string | null;
  /** Where the model came from: the role's `opencode.roles` entry, the alias map, or the parent session. */
  modelSource: ModelSource | null;
  startedAt: number | null;
  endedAt: number | null;
  durationMs: number | null;
  status: AgentStatus;
  submission: "tool" | "text" | "text-empty" | null;
  attempts: number;
  rejections: number;
  tokens: TokenUsage;
  cost: number;
  peakContext: number;
  error: string | null;
}

export interface AgentMeta {
  seq: number;
  label: string;
  phase: string | null;
  role?: string | null;
  agent: string;
  parentSessionID: string;
}

export interface FinishOptions {
  status: "done" | "failed" | "partial";
  result?: unknown;
  endedAt?: number;
  failureLabel?: string | null;
  degraded?: string[];
}

const EMPTY_TOKENS: TokenUsage = { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } };
const THROTTLE_PATTERN = /\b429\b|rate.?limit|retry.?after|quota|free.?limit/i;

/** `ck --version` as `X.Y.Z`, or null when the CLI is missing or unreadable. */
export function detectCkVersion(command = process.env.CK_BIN || "ck"): string | null {
  try {
    const result = spawnSync(command, ["--version"], { encoding: "utf8", timeout: 5000 });
    const text = `${result.stdout ?? ""}\n${result.stderr ?? ""}`;
    const match = /ck, version\s+([0-9]+\.[0-9]+\.[0-9]+)/.exec(text);
    return match?.[1] ?? null;
  } catch {
    return null;
  }
}

export function packageVersion(packageRoot: string): string {
  try {
    const parsed = JSON.parse(readFileSync(join(packageRoot, "package.json"), "utf8")) as { version?: unknown };
    return typeof parsed.version === "string" ? parsed.version : "0.0.0";
  } catch {
    return "0.0.0";
  }
}

export function workflowNameOf(targetName: string): RunEnvelope["workflow"] {
  if (targetName.includes("spec-create")) return "spec-create";
  if (targetName.includes("spec-implement")) return "spec-implement";
  return "other";
}

export function agentRowFromOutcome(outcome: ChildOutcome, meta: AgentMeta): AgentRow {
  return {
    seq: meta.seq,
    label: meta.label,
    phase: meta.phase,
    role: meta.role ?? null,
    agent: outcome.agent,
    parentSessionID: meta.parentSessionID,
    sessionID: outcome.sessionID,
    requested: { model: outcome.requestedModel, effort: outcome.requestedEffort },
    resolved: {
      providerID: outcome.resolvedModel.providerID,
      id: outcome.resolvedModel.id,
      variant: outcome.resolvedVariant,
    },
    requestedEffort: outcome.requestedEffort,
    resolvedEffort: outcome.resolvedVariant,
    variant: outcome.resolvedVariant,
    modelSource: outcome.modelSource,
    startedAt: outcome.startedAt,
    endedAt: outcome.endedAt,
    durationMs: outcome.endedAt - outcome.startedAt,
    status: "ok",
    submission: outcome.submission,
    attempts: outcome.attempts,
    rejections: outcome.rejections,
    tokens: outcome.tokens,
    cost: outcome.cost,
    peakContext: outcome.peakContext,
    error: null,
  };
}

interface ErrorDetails {
  sessionID?: string;
  tokens?: TokenUsage;
  cost?: number;
  startedAt?: number;
  endedAt?: number;
  attempts?: number;
  submission?: "tool" | "text" | "text-empty";
  rejections?: number;
  peakContext?: number;
  guardEvents?: readonly GuardEvent[];
  requestedModel?: string;
  requestedEffort?: string | null;
  requestedVariant?: string | null;
  resolvedModel?: { providerID: string; id: string; variant?: string };
  resolvedVariant?: string | null;
  modelSource?: ModelSource;
}

export function agentRowFromError(
  error: { kind: string; message: string; details?: Record<string, unknown> },
  meta: AgentMeta,
): AgentRow {
  const details = (error.details ?? {}) as ErrorDetails;
  const startedAt = typeof details.startedAt === "number" ? details.startedAt : null;
  const endedAt = typeof details.endedAt === "number" ? details.endedAt : null;
  return {
    seq: meta.seq,
    label: meta.label,
    phase: meta.phase,
    role: meta.role ?? null,
    agent: meta.agent,
    parentSessionID: meta.parentSessionID,
    sessionID: typeof details.sessionID === "string" ? details.sessionID : null,
    requested: {
      model: details.requestedModel ?? "unknown",
      effort: details.requestedEffort ?? null,
    },
    resolved: details.resolvedModel
      ? {
          providerID: details.resolvedModel.providerID,
          id: details.resolvedModel.id,
          variant: details.resolvedVariant ?? null,
        }
      : null,
    requestedEffort: details.requestedEffort ?? null,
    resolvedEffort: details.resolvedVariant ?? null,
    variant: details.resolvedVariant ?? null,
    modelSource: details.modelSource ?? null,
    startedAt,
    endedAt,
    durationMs: startedAt !== null && endedAt !== null ? endedAt - startedAt : null,
    status: statusForError(error.kind),
    submission: details.submission ?? null,
    attempts: details.attempts ?? 1,
    rejections: details.rejections ?? 0,
    tokens: details.tokens ?? EMPTY_TOKENS,
    cost: details.cost ?? 0,
    peakContext: details.peakContext ?? 0,
    error: error.message,
  };
}

export class RunRecordWriter {
  readonly path: string;
  private readonly data: Record<string, unknown>;
  private finished = false;

  constructor(path: string, envelope: RunEnvelope) {
    this.path = path;
    this.data = {
      schema: "ck-opencode-run/1",
      runId: envelope.runId,
      harness: "opencode",
      workflow: envelope.workflow,
      collectorVersion: envelope.collectorVersion,
      ckVersion: envelope.ckVersion,
      claudeCodeVersion: null,
      status: "running",
      startedAt: envelope.startedAt,
      endedAt: null,
      projectRoot: envelope.projectRoot,
      spec: envelope.spec,
      into: envelope.into,
      args: envelope.args,
      result: null,
      throttles: 0,
      retries: 0,
      failureLabel: null,
      degraded: [],
      guard: {
        denials: 0,
        postCheckCatches: 0,
        guardEscalations: 0,
        headlessDenials: 0,
        prompts: 0,
        events: [] as Array<{ at: number; kind: string; agent?: string; reason: string }>,
      },
      agents: [] as AgentRow[],
    };
  }

  start(): void {
    this.write();
  }

  agents(): readonly AgentRow[] {
    return this.data.agents as AgentRow[];
  }

  addAgent(row: AgentRow, guardEvents: readonly GuardEvent[] = []): void {
    (this.data.agents as AgentRow[]).push(row);
    if (row.attempts > 1) {
      this.data.retries = (this.data.retries as number) + (row.attempts - 1);
    }
    if (row.error && THROTTLE_PATTERN.test(row.error)) {
      this.data.throttles = (this.data.throttles as number) + 1;
    }
    this.applyGuard(guardEvents);
    this.write();
  }

  addGuard(events: readonly GuardEvent[]): void {
    this.applyGuard(events);
    this.write();
  }

  private applyGuard(events: readonly GuardEvent[]): void {
    if (events.length === 0) return;
    const guard = this.data.guard as {
      denials: number;
      postCheckCatches: number;
      headlessDenials: number;
      events: Array<{ at: number; kind: string; agent?: string; reason: string }>;
    };
    for (const event of events) {
      if (event.kind === "deny") guard.denials += 1;
      else if (event.kind === "post_check") guard.postCheckCatches += 1;
      else if (event.kind === "headless") guard.headlessDenials += 1;
      guard.events.push({
        at: event.at,
        kind: event.kind,
        ...(event.agent ? { agent: event.agent } : {}),
        reason: event.reason,
      });
    }
    if (guard.events.length > 100) guard.events.splice(0, guard.events.length - 100);
  }

  finish(options: FinishOptions): void {
    if (this.finished) return;
    this.finished = true;
    this.data.status = options.status;
    this.data.endedAt = options.endedAt ?? Date.now();
    if (options.result !== undefined) this.data.result = options.result;
    if (options.failureLabel !== undefined) this.data.failureLabel = options.failureLabel;
    if (options.degraded) this.data.degraded = options.degraded;
    this.write();
  }

  private write(): void {
    const temporary = `${this.path}.tmp`;
    writeFileSync(temporary, `${JSON.stringify(this.data, null, 2)}\n`);
    renameSync(temporary, this.path);
  }
}

/** The shape `ck bench import` accepts (`{result, workflowProgress, durationMs}`). */
export function benchOutput(
  result: unknown,
  rows: readonly AgentRow[],
  durationMs: number,
  status: "done" | "failed" = "done",
): Record<string, unknown> {
  return {
    result,
    status,
    durationMs,
    workflowProgress: rows.map((row) => ({
      type: "workflow_agent",
      agentId: row.sessionID,
      label: row.label,
      model: row.resolved ? `${row.resolved.providerID}/${row.resolved.id}` : row.requested.model,
      tokens: row.peakContext,
      durationMs: row.durationMs,
      state: row.status,
    })),
  };
}

function statusForError(kind: string): AgentStatus {
  switch (kind) {
    case "model":
      return "model_error";
    case "agent":
      return "agent_error";
    case "session":
      return "session_error";
    case "start-timeout":
    case "timeout":
      return "timeout";
    case "result":
      return "result_error";
    default:
      // A guard stop is a failed step; the error text and the guard events carry the detail.
      return "failed";
  }
}
