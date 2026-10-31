/**
 * The value shape the server-side workflow writer persists for the TUI status line, and the pure
 * formatting/selection helpers the TUI part uses. Keeping this file free of Node/OpenTUI imports
 * makes it testable with `node --test`.
 */

export const STATUS_SCHEMA = "ck-opencode-status/1";

export interface WorkflowStatus {
  schema: typeof STATUS_SCHEMA;
  runId: string;
  workflow: string;
  spec: string | null;
  projectRoot: string;
  status: "running" | "done" | "failed";
  phase: string | null;
  startedAt: number;
  updatedAt: number;
  endedAt: number | null;
  total: number;
  merged: number;
  failed: number;
  running: string[];
  agents: number;
  cost: number;
  last: string | null;
  log: string;
}

export function isWorkflowStatus(value: unknown): value is WorkflowStatus {
  if (!value || typeof value !== "object") return false;
  const status = value as Partial<WorkflowStatus>;
  return (
    status.schema === STATUS_SCHEMA &&
    typeof status.runId === "string" &&
    (status.status === "running" || status.status === "done" || status.status === "failed") &&
    typeof status.startedAt === "number" &&
    typeof status.updatedAt === "number" &&
    Array.isArray(status.running)
  );
}

/** The latest running run, else the latest finished one within `freshMs`. */
export function pickActiveStatus(
  statuses: readonly WorkflowStatus[],
  now = Date.now(),
  freshMs = 5 * 60 * 1000,
): WorkflowStatus | null {
  const running = statuses
    .filter((status) => status.status === "running")
    .sort((a, b) => b.updatedAt - a.updatedAt);
  if (running.length > 0) return running[0] ?? null;
  const recent = statuses
    .filter((status) => status.status !== "running" && now - status.updatedAt <= freshMs)
    .sort((a, b) => b.updatedAt - a.updatedAt);
  return recent[0] ?? null;
}

function shortLabel(label: string): string {
  return label.replace(/\s*\[[^\]]*\]\s*$/, "").trim();
}

function elapsedLabel(status: WorkflowStatus, now: number): string {
  const ms = Math.max(0, (status.endedAt ?? now) - status.startedAt);
  const minutes = Math.max(1, Math.round(ms / 60_000));
  return minutes >= 60 ? `${Math.floor(minutes / 60)}h${String(minutes % 60).padStart(2, "0")}m` : `${minutes}m`;
}

/** A compact one-line status suitable for OpenCode's prompt footer. */
export function formatWorkflowStatus(status: WorkflowStatus, now = Date.now()): string {
  const icon = status.status === "running" ? "▶" : status.failed > 0 ? "!" : "✓";
  const name = status.spec || status.workflow;
  const tasks = status.total > 0 ? `${status.merged}/${status.total}` : `${status.merged}`;
  const parts = [`${icon} ck ${name}`, tasks];
  if (status.running.length === 1) {
    parts.push(shortLabel(status.running[0] ?? ""));
  } else if (status.running.length > 1) {
    parts.push(`${status.running.length} running`);
  }
  if (status.failed > 0) parts.push(`${status.failed} failed`);
  parts.push(elapsedLabel(status, now));
  if (status.status === "running" && status.agents > 0) parts.push(`${status.agents} agents`);
  return parts.filter(Boolean).join(" · ");
}
