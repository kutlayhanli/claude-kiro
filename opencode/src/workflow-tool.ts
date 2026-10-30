/**
 * The `workflow` tool: the bridge between an OpenCode session and the vendored claude-kiro
 * workflow scripts.
 *
 * `execute` validates the target, starts a runner in the background, and returns a run id at once.
 * Progress and the final result arrive as queued messages in the invoking session, which is what
 * lets `/spec/spawn-worktree` start several runs without waiting.
 */

import { spawn, type ChildProcess } from "node:child_process";
import { randomBytes } from "node:crypto";
import { existsSync, mkdirSync, readdirSync, writeFileSync } from "node:fs";
import { basename, isAbsolute, join, resolve } from "node:path";
import type { Plugin } from "@opencode/plugin";
import type { ToolEditor } from "@opencode/plugin/promise/tool";
import { resolveStateDir, type CkConfig } from "./config.ts";
import { createRunLogger, type RunLogger } from "./log.ts";
import type { Catalog } from "./models.ts";
import { ChildError, runChild } from "./run-child.ts";

export interface WorkflowRuntime {
  ctx: Plugin.Context;
  config: CkConfig;
  catalog?: Catalog;
  packageRoot: string;
  projectDir: string;
}

export interface WorkflowInput {
  name?: string;
  scriptPath?: string;
  script?: string;
  args?: Record<string, unknown>;
}

export type WorkflowTarget =
  | { kind: "file"; path: string; name: string }
  | { kind: "inline"; source: string; name: string };

interface RunnerMessage {
  t?: string;
  id?: string;
  prompt?: string;
  opts?: unknown;
  name?: string;
  text?: string;
  result?: unknown;
  message?: string;
  stack?: string;
  meta?: unknown;
}

const WORKFLOW_INPUT = {
  type: "object",
  properties: {
    name: {
      type: "string",
      description:
        'The shipped workflow name: "spec:spec-create" or "spec:spec-implement" (also accepts "spec-create" / "spec-implement").',
    },
    scriptPath: {
      type: "string",
      description: "Absolute path, or a path relative to the project, to a workflow script. Used instead of name.",
    },
    script: {
      type: "string",
      description: "Inline workflow body for the one-task flow. Used instead of name or scriptPath.",
    },
    args: {
      type: "object",
      description: "The workflow's args object, passed through unchanged.",
    },
  },
  additionalProperties: false,
};

export async function registerWorkflowTool(runtime: WorkflowRuntime): Promise<() => Promise<void>> {
  const children = new Set<ChildProcess>();

  const registration = await runtime.ctx.tool.transform((editor: ToolEditor) => {
    const definition: Parameters<ToolEditor["add"]>[0] = {
      name: "workflow",
      description:
        "Run a claude-kiro workflow in the background. Pass exactly one of name (a shipped workflow), " +
        "scriptPath (a workflow file), or script (an inline body), plus args. Returns a run id immediately; " +
        "progress and the final result arrive as messages in this session.",
      input: WORKFLOW_INPUT as Parameters<ToolEditor["add"]>[0]["input"],
      execute: async (input: unknown, context) => {
        const parsed = parseWorkflowInput(input);
        const target = resolveWorkflowTarget(parsed, runtime.packageRoot, runtime.projectDir);
        const started = await startWorkflow(runtime, target, parsed.args ?? {}, context.sessionID, children);
        const detail = target.kind === "file" ? target.name : `${target.name} (inline)`;
        return {
          content:
            `Workflow ${detail} started in the background.\n` +
            `runId: ${started.runId}\n` +
            `log: ${started.logPath}\n` +
            "Progress and the final result will arrive as messages in this session. Do not start it again.",
        };
      },
    };
    editor.add(definition);
  });

  return async () => {
    await registration.dispose().catch(() => {});
    for (const child of children) {
      try {
        child.kill("SIGTERM");
      } catch {
        // already gone
      }
    }
    children.clear();
  };
}

export function parseWorkflowInput(input: unknown): WorkflowInput {
  if (!isPlainObject(input)) throw new Error("workflow input must be an object");
  const parsed: WorkflowInput = {};
  if (input.name !== undefined) {
    if (typeof input.name !== "string" || !input.name.trim()) throw new Error("name must be a non-empty string");
    parsed.name = input.name.trim();
  }
  if (input.scriptPath !== undefined) {
    if (typeof input.scriptPath !== "string" || !input.scriptPath.trim()) {
      throw new Error("scriptPath must be a non-empty string");
    }
    parsed.scriptPath = input.scriptPath.trim();
  }
  if (input.script !== undefined) {
    if (typeof input.script !== "string" || !input.script.trim()) throw new Error("script must be a non-empty string");
    parsed.script = input.script;
  }
  if (input.args !== undefined) {
    if (!isPlainObject(input.args)) throw new Error("args must be an object");
    parsed.args = input.args;
  }
  const provided = [parsed.name, parsed.scriptPath, parsed.script].filter((entry) => entry !== undefined);
  if (provided.length !== 1) {
    throw new Error("pass exactly one of name, scriptPath, or script");
  }
  return parsed;
}

export function resolveWorkflowTarget(
  input: WorkflowInput,
  packageRoot: string,
  projectDir: string,
): WorkflowTarget {
  if (input.script !== undefined) return { kind: "inline", source: input.script, name: "inline" };

  if (input.scriptPath !== undefined) {
    const path = isAbsolute(input.scriptPath) ? input.scriptPath : resolve(projectDir, input.scriptPath);
    if (!existsSync(path)) throw new Error(`workflow script not found: ${path}`);
    return { kind: "file", path, name: basename(path, ".js") };
  }

  const requested = input.name ?? "";
  const aliases: Record<string, string> = {
    "spec:spec-create": "spec-create",
    "spec-create": "spec-create",
    "spec:create": "spec-create",
    "spec:spec-implement": "spec-implement",
    "spec-implement": "spec-implement",
    "spec:implement": "spec-implement",
  };
  const stem = aliases[requested] ?? requested.replace(/^spec:/, "");
  if (!/^[A-Za-z0-9._-]+$/.test(stem)) throw new Error(`invalid workflow name: ${JSON.stringify(requested)}`);

  const vendor = join(packageRoot, "vendor", "workflows");
  const path = join(vendor, `${stem}.js`);
  if (!existsSync(path)) {
    let available: string[] = [];
    try {
      available = readdirSync(vendor)
        .filter((file) => file.endsWith(".js"))
        .map((file) => `spec:${file.slice(0, -3)}`);
    } catch {
      // the message below still names the missing workflow
    }
    throw new Error(
      `unknown workflow ${JSON.stringify(requested)}` +
        (available.length ? `; available: ${available.join(", ")}` : "; vendor/workflows is missing"),
    );
  }
  return { kind: "file", path, name: `spec:${stem}` };
}

interface StartedRun {
  runId: string;
  logPath: string;
}

async function startWorkflow(
  runtime: WorkflowRuntime,
  target: WorkflowTarget,
  args: Record<string, unknown>,
  sessionID: string,
  children: Set<ChildProcess>,
): Promise<StartedRun> {
  const runId = newRunId();
  const base = runtime.config.run.stateDir ?? resolveStateDir(runtime.config);
  const stateDir = isAbsolute(base) ? base : resolve(runtime.projectDir, base);
  const runDir = join(stateDir, "runs", runId);
  mkdirSync(runDir, { recursive: true });

  const logger = createRunLogger(join(runDir, "run.log"));
  logger.write(`run ${runId} ${target.name} project=${runtime.projectDir}`);

  const workflowPath = target.kind === "inline" ? join(runDir, "inline.mjs") : target.path;
  if (target.kind === "inline") writeFileSync(workflowPath, target.source);
  const argsPath = join(runDir, "args.json");
  writeFileSync(argsPath, `${JSON.stringify(args, null, 2)}\n`);

  const runnerPath = join(runtime.packageRoot, "src", "runner.mjs");
  if (!existsSync(runnerPath)) throw new Error(`workflow runner not found: ${runnerPath}`);

  const child = spawn("node", [runnerPath, workflowPath, argsPath], {
    cwd: runtime.projectDir,
    env: {
      ...process.env,
      CK_RUN_ID: runId,
      CK_WORKFLOW_NAME: target.name,
      CK_PLUGIN_ROOT: runtime.packageRoot,
    },
    stdio: ["ignore", "pipe", "pipe", "ipc"],
  });
  children.add(child);

  child.stdout?.on("data", (chunk: Buffer) => logger.write(`[stdout] ${chunk.toString().trimEnd()}`));
  child.stderr?.on("data", (chunk: Buffer) => logger.write(`[stderr] ${chunk.toString().trimEnd()}`));

  let finished = false;
  let seq = 0;

  const send = (message: Record<string, unknown>): void => {
    try {
      if (child.connected) child.send(message);
    } catch (error) {
      logger.write(`could not send ${String(message.t)} to the runner: ${messageOf(error)}`);
    }
  };

  const finish = async (text: string): Promise<void> => {
    if (finished) return;
    finished = true;
    logger.write(text);
    await post(runtime, sessionID, runId, logger, text, true);
  };

  child.on("message", (raw: unknown) => {
    const message = (raw ?? {}) as RunnerMessage;
    if (typeof message.t !== "string") return;
    if (message.t === "ready") {
      logger.write(`runner ready${message.meta ? ` meta=${JSON.stringify(message.meta).slice(0, 500)}` : ""}`);
      return;
    }
    if (message.t === "phase") {
      logger.write(`phase: ${String(message.name ?? "")}`);
      // Progress is a synthetic transcript entry. With no delivery it does not start a model turn
      // (verified live: two phases produced zero extra turns). Only the final result or an error
      // wakes the parent, once.
      void post(runtime, sessionID, runId, logger, `phase: ${String(message.name ?? "")}`, false);
      return;
    }
    if (message.t === "log") {
      logger.write(String(message.text ?? ""));
      return;
    }
    if (message.t === "agent") {
      const id = typeof message.id === "string" ? message.id : "";
      if (!id) return;
      seq += 1;
      const number = seq;
      void handleAgent(runtime, sessionID, logger, message, number)
        .then((result) => {
          if (result.ok) send({ t: "result", id, value: result.value });
          else send({ t: "result", id, error: result.error });
        })
        .catch((error: unknown) => {
          logger.write(`agent ${number} failed outside runChild: ${messageOf(error)}`);
          send({ t: "result", id, error: messageOf(error) });
        });
      return;
    }
    if (message.t === "done") {
      const result = message.result ?? null;
      finished = true;
      try {
        writeFileSync(join(runDir, "result.json"), `${JSON.stringify({ runId, workflow: target.name, result }, null, 2)}\n`);
      } catch (error) {
        logger.write(`could not write result.json: ${messageOf(error)}`);
      }
      logger.write("workflow finished");
      void post(runtime, sessionID, runId, logger, finalMessage(runId, target.name, result, runDir), true);
      return;
    }
    if (message.t === "error") {
      finished = true;
      logger.write(`workflow error: ${String(message.message ?? "unknown")}`);
      if (message.stack) logger.write(String(message.stack));
      void post(
        runtime,
        sessionID,
        runId,
        logger,
        `workflow failed: ${String(message.message ?? "unknown error")}\nlog: ${logger.path}`,
        true,
      );
    }
  });

  child.on("error", (error) => {
    void finish(`could not start the workflow process: ${error.message}\nlog: ${logger.path}`);
  });
  child.on("exit", (code, signal) => {
    children.delete(child);
    logger.write(`runner exited (${code ?? signal ?? "unknown"})`);
    if (!finished) {
      void finish(
        `workflow process exited (${code ?? signal ?? "unknown"}) before reporting a result\nlog: ${logger.path}`,
      );
    }
  });

  return { runId, logPath: logger.path };
}

async function handleAgent(
  runtime: WorkflowRuntime,
  parentSessionID: string,
  logger: RunLogger,
  message: RunnerMessage,
  seq: number,
): Promise<{ ok: true; value: unknown } | { ok: false; error: string }> {
  const opts = isPlainObject(message.opts) ? message.opts : {};
  const label = typeof opts.label === "string" && opts.label ? opts.label : `agent ${seq}`;
  const phase = typeof opts.phase === "string" ? opts.phase : null;
  const model = typeof opts.model === "string" ? opts.model : undefined;
  const effort = typeof opts.effort === "string" ? opts.effort : undefined;
  const schema = Object.prototype.hasOwnProperty.call(opts, "schema") ? opts.schema : undefined;

  try {
    const outcome = await runChild({
      ctx: runtime.ctx,
      parentSessionID,
      projectDir: runtime.projectDir,
      config: runtime.config,
      catalog: runtime.catalog,
      seq,
      prompt: typeof message.prompt === "string" ? message.prompt : "",
      label,
      phase,
      model,
      effort,
      schema,
    });
    logger.write(
      `agent ${seq} ${label}: ${outcome.submission} in ${outcome.attempts} attempt(s), ` +
        `${outcome.endedAt - outcome.startedAt}ms, cost=$${outcome.cost}, ` +
        `${outcome.tokens.input}+${outcome.tokens.cache.read} in / ${outcome.tokens.output} out, ` +
        `model=${outcome.resolvedModel.providerID}/${outcome.resolvedModel.id}` +
        `${outcome.resolvedVariant ? `#${outcome.resolvedVariant}` : ""}` +
        `${outcome.notes.length ? ` (${outcome.notes.join("; ")})` : ""}`,
    );
    return { ok: true, value: outcome.value };
  } catch (error) {
    if (error instanceof ChildError) {
      logger.write(`agent ${seq} ${label}: ${error.kind}: ${error.message}`);
      return { ok: false, error: error.message };
    }
    logger.write(`agent ${seq} ${label}: unexpected failure: ${messageOf(error)}`);
    return { ok: false, error: messageOf(error) };
  }
}

async function post(
  runtime: WorkflowRuntime,
  sessionID: string,
  runId: string,
  logger: RunLogger,
  text: string,
  wake: boolean,
): Promise<void> {
  const message = `[ck workflow ${runId}] ${text}`;
  try {
    if (wake) {
      await runtime.ctx.session.prompt({ sessionID, text: message, delivery: "queue" });
    } else {
      await runtime.ctx.session.synthetic({ sessionID, text: message });
    }
  } catch (error) {
    logger.write(`could not post to the parent session: ${messageOf(error)}`);
  }
}

function finalMessage(runId: string, workflow: string, result: unknown, runDir: string): string {
  let json: string;
  try {
    json = JSON.stringify(result, null, 2) ?? "null";
  } catch {
    json = String(result);
  }
  const limit = 24_000;
  const body = json.length > limit ? `${json.slice(0, limit)}\n… (truncated)` : json;
  return (
    `${workflow} finished.\n\n${body}\n\n` +
    `Full result: ${join(runDir, "result.json")}\n` +
    `Log: ${join(runDir, "run.log")}`
  );
}

function newRunId(): string {
  const stamp = new Date().toISOString().replace(/[-:.TZ]/g, "").slice(0, 14);
  return `wf-${stamp}-${randomBytes(3).toString("hex")}`;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
