/**
 * The guard shim: translate OpenCode tool events into Claude Code hook JSON, run the real
 * `ck --hook … --plugin-hook`, and map the answer back.
 *
 * OpenCode's code-mode wrapper raises two events for one call: the outer `execute` (a JS string) and
 * the inner real tool. Only the inner event is guarded. A post-hoc block (done-verify) is a latch:
 * the next call in that session fails with the original reason, because OpenCode cannot retroactively
 * block a completed tool. A guard failure halts the next call; it never silently degrades.
 */

import { spawn } from "node:child_process";
import type { Plugin } from "@opencode/plugin";
import type { CkConfig } from "./config.ts";

export interface GuardToolEvent {
  tool: string;
  agent: string;
  sessionID: string;
  messageID?: string;
  id: string;
  input: Record<string, unknown>;
}

export interface ClaudeHookInput {
  cwd: string;
  tool_name: string;
  tool_input: Record<string, unknown>;
  session_id: string;
  agent_id?: string;
  agent_type?: string;
  tool_use_id: string;
}

interface Translation {
  tool_name: "Edit" | "Write" | "MultiEdit" | "Bash";
  tool_input: Record<string, unknown>;
  /** The permission action/resources to ask OpenCode for when ck answers "ask". */
  action: string;
  resource: string;
}

export class GuardError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "GuardError";
  }
}

export interface GuardCleanup {
  dispose: () => Promise<void>;
}

const FILE_TOOLS = new Set(["Edit", "Write", "MultiEdit"]);

export function translateTool(event: GuardToolEvent): Translation | null {
  const input = event.input;
  switch (event.tool) {
    case "edit": {
      const path = stringField(input, "path");
      const oldString = stringField(input, "oldString");
      const newString = stringField(input, "newString");
      if (!path || oldString === null || newString === null) {
        throw new GuardError(
          `[ck guard] cannot guard this edit call: expected {path, oldString, newString}, got ${JSON.stringify(input)}`,
        );
      }
      return {
        tool_name: "Edit",
        tool_input: { file_path: path, old_string: oldString, new_string: newString },
        action: "edit",
        resource: path,
      };
    }
    case "write": {
      const path = stringField(input, "path");
      const content = stringField(input, "content");
      if (!path || content === null) {
        throw new GuardError(
          `[ck guard] cannot guard this write call: expected {path, content}, got ${JSON.stringify(input)}`,
        );
      }
      return {
        tool_name: "Write",
        tool_input: { file_path: path, content },
        action: "edit",
        resource: path,
      };
    }
    case "shell": {
      const command = stringField(input, "command");
      if (command === null) {
        throw new GuardError(
          `[ck guard] cannot guard this shell call: expected {command}, got ${JSON.stringify(input)}`,
        );
      }
      const workdir = stringField(input, "workdir");
      const effectiveCommand =
        workdir && workdir !== "." ? `cd ${shellQuote(workdir)} && ${command}` : command;
      return {
        tool_name: "Bash",
        tool_input: { command: effectiveCommand },
        action: "shell",
        resource: command,
      };
    }
    case "patch":
    case "apply_patch":
      // The patch input shape is not captured; rather than guard it blindly, make the model use
      // edit/write, which are fully translated. This is the D12 "unmappable tool is fatal" rule.
      throw new GuardError(
        `[ck guard] the ${event.tool} tool is not guardable on OpenCode; use edit or write instead`,
      );
    default:
      return null;
  }
}

export function isFileToolName(name: string): boolean {
  return FILE_TOOLS.has(name);
}

interface HookRun {
  code: number | null;
  stdout: string;
  stderr: string;
  timedOut: boolean;
}

export function runCkHook(
  hook: string,
  input: ClaudeHookInput,
  cwd: string,
  timeoutMs: number,
  env: NodeJS.ProcessEnv = process.env,
): Promise<HookRun> {
  const command = env.CK_BIN || "ck";
  return new Promise((resolve, reject) => {
    const child = spawn(command, ["--hook", hook, "--plugin-hook"], {
      cwd,
      env,
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      child.kill("SIGKILL");
    }, timeoutMs);

    child.stdout?.on("data", (chunk: Buffer) => {
      stdout += chunk.toString();
    });
    child.stderr?.on("data", (chunk: Buffer) => {
      stderr += chunk.toString();
    });
    child.on("error", (error) => {
      clearTimeout(timer);
      reject(new GuardError(`[ck guard] could not run ${command} --hook ${hook}: ${message(error)}`));
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      resolve({ code, stdout, stderr, timedOut });
    });
    child.stdin?.end(JSON.stringify(input));
  });
}

interface HookOutput {
  hookSpecificOutput?: {
    permissionDecision?: string;
    permissionDecisionReason?: string;
    additionalContext?: string;
  };
  decision?: string;
  reason?: string;
  systemMessage?: string;
}

export function parseHookOutput(stdout: string, hook: string): HookOutput | null {
  const text = stdout.trim();
  if (!text) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new GuardError(`[ck guard] the ${hook} hook printed invalid JSON: ${text.slice(0, 500)}`);
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new GuardError(`[ck guard] the ${hook} hook printed a non-object: ${text.slice(0, 500)}`);
  }
  return parsed as HookOutput;
}

export async function createGuard(
  ctx: Plugin.Context,
  config: CkConfig,
  projectDir: string,
): Promise<GuardCleanup> {
  if (!config.guard.enabled) return { dispose: async () => {} };

  const latches = new Map<string, string>();
  const sessions = new Map<string, { parentID?: string; directory: string }>();
  const registrations: Array<{ dispose: () => Promise<void> }> = [];
  const headless = process.env.CK_RUN_HEADLESS === "1";

  const sessionMeta = async (sessionID: string): Promise<{ parentID?: string; directory: string }> => {
    const cached = sessions.get(sessionID);
    if (cached) return cached;
    let info;
    try {
      info = (await ctx.session.get({ sessionID })) as unknown as {
        parentID?: string;
        location?: { directory?: string };
      };
    } catch (error) {
      throw new GuardError(`[ck guard] could not read session ${sessionID}: ${message(error)}`);
    }
    const meta = {
      parentID: info.parentID,
      directory: info.location?.directory ?? projectDir,
    };
    sessions.set(sessionID, meta);
    return meta;
  };

  const hookInput = async (
    event: GuardToolEvent,
    translation: Translation,
  ): Promise<{ input: ClaudeHookInput; cwd: string }> => {
    const meta = await sessionMeta(event.sessionID);
    // OpenCode reports `agent` on every call; Claude Code's agent_id exists only for subagents.
    // The ck guard classifies any agent_id as a task agent, so only set it for a child session.
    const input: ClaudeHookInput = {
      cwd: meta.directory,
      tool_name: translation.tool_name,
      tool_input: translation.tool_input,
      session_id: meta.parentID ?? event.sessionID,
      tool_use_id: event.id,
      ...(meta.parentID ? { agent_id: event.agent, agent_type: event.agent } : {}),
    };
    return { input, cwd: meta.directory };
  };

  const latch = (sessionID: string, reason: string): void => {
    latches.set(sessionID, reason);
  };

  const postContext = async (sessionID: string, text: string): Promise<void> => {
    if (!text) return;
    try {
      await ctx.session.synthetic({ sessionID, text });
    } catch {
      // Context is advisory; never fail a completed tool because the note could not be posted.
    }
  };

  const before = async (raw: unknown): Promise<void> => {
    const event = raw as GuardToolEvent;
    if (!event || typeof event !== "object" || typeof event.tool !== "string") {
      throw new GuardError("[ck guard] malformed execute.before event");
    }
    // Code mode: the outer wrapper is never a guarded operation; act on the inner event only.
    if (event.tool === "execute") return;

    const pending = latches.get(event.sessionID);
    if (pending !== undefined) {
      latches.delete(event.sessionID);
      throw new GuardError(pending);
    }

    const translation = translateTool(event);
    if (!translation) return;
    if (!config.guard.hooks.preToolGuard) return;

    const { input, cwd } = await hookInput(event, translation);
    const result = await runCkHook(
      "pre-tool-guard",
      input,
      cwd,
      config.guard.timeouts.preToolGuard * 1000,
    );
    if (result.timedOut) {
      throw new GuardError(
        `[ck guard] the pre-tool guard timed out after ${config.guard.timeouts.preToolGuard}s`,
      );
    }
    if (result.code !== 0) {
      throw new GuardError(
        `[ck guard] the pre-tool guard failed (exit ${result.code ?? "unknown"}): ${result.stderr.trim() || result.stdout.trim()}`,
      );
    }

    const output = parseHookOutput(result.stdout, "pre-tool-guard");
    const decision = output?.hookSpecificOutput?.permissionDecision;
    if (!decision || decision === "allow") return;

    const reason = output?.hookSpecificOutput?.permissionDecisionReason || "[ck guard] this call needs approval";
    if (decision === "deny") throw new GuardError(reason);
    if (decision === "ask") {
      if (headless) throw new GuardError(reason);
      try {
        await ctx.permission.assert({
          sessionID: event.sessionID,
          action: translation.action,
          resources: [translation.resource],
        });
      } catch (error) {
        throw new GuardError(reason || message(error));
      }
      return;
    }
    throw new GuardError(`[ck guard] the pre-tool guard returned an unknown decision: ${JSON.stringify(decision)}`);
  };

  const after = async (raw: unknown): Promise<void> => {
    const event = raw as GuardToolEvent;
    if (!event || typeof event !== "object" || typeof event.tool !== "string") return;
    if (event.tool === "execute") return;

    let translation: Translation | null;
    try {
      translation = translateTool(event);
    } catch (error) {
      latch(event.sessionID, message(error));
      return;
    }
    if (!translation) return;

    const { input, cwd } = await hookInput(event, translation);

    if (config.guard.hooks.postFileOps && isFileToolName(translation.tool_name)) {
      try {
        const result = await runCkHook(
          "post-file-ops",
          input,
          cwd,
          config.guard.timeouts.postFileOps * 1000,
        );
        if (result.timedOut || result.code !== 0) {
          latch(
            event.sessionID,
            `[ck guard] the post-file-ops hook failed: ${
              result.timedOut ? `timed out after ${config.guard.timeouts.postFileOps}s` : result.stderr.trim()
            }`,
          );
        } else {
          const output = parseHookOutput(result.stdout, "post-file-ops");
          const context = output?.hookSpecificOutput?.additionalContext;
          if (context) await postContext(event.sessionID, context);
          if (output?.systemMessage) await postContext(event.sessionID, output.systemMessage);
        }
      } catch (error) {
        latch(event.sessionID, message(error));
      }
    }

    if (config.guard.hooks.doneVerify) {
      // The gate can take minutes; run it in the background and deliver a block as a latch.
      void runCkHook("done-verify", input, cwd, config.guard.timeouts.doneVerify * 1000)
        .then(async (result) => {
          if (result.timedOut) {
            latch(
              event.sessionID,
              `[ck guard] the done-verify hook timed out after ${config.guard.timeouts.doneVerify}s`,
            );
            return;
          }
          if (result.code !== 0) {
            latch(
              event.sessionID,
              `[ck guard] the done-verify hook failed (exit ${result.code ?? "unknown"}): ${result.stderr.trim()}`,
            );
            return;
          }
          const output = parseHookOutput(result.stdout, "done-verify");
          if (output?.decision === "block") {
            latch(event.sessionID, output.reason || "[ck guard] the verification gate failed");
          } else if (output?.systemMessage) {
            await postContext(event.sessionID, output.systemMessage);
          }
        })
        .catch((error: unknown) => {
          latch(event.sessionID, message(error));
        });
    }
  };

  registrations.push(await ctx.tool.hook("execute.before", before));
  registrations.push(await ctx.tool.hook("execute.after", after));

  return {
    dispose: async () => {
      for (const registration of registrations) await registration.dispose().catch(() => {});
    },
  };
}

function stringField(input: Record<string, unknown>, key: string): string | null {
  const value = input[key];
  return typeof value === "string" ? value : null;
}

function shellQuote(value: string): string {
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
