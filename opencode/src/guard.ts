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
import { appendFileSync } from "node:fs";
import type { Plugin } from "@opencode/plugin";
import type { CkConfig } from "./config.ts";

/** Opt-in trace for diagnosing the hook bridge: CK_GUARD_DEBUG=/path/to/log. */
function debug(text: string): void {
  const path = process.env.CK_GUARD_DEBUG;
  if (!path) return;
  try {
    appendFileSync(path, `${new Date().toISOString()} ${text}\n`);
  } catch {
    // tracing must never affect a run
  }
}

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

export interface GuardEvent {
  at: number;
  kind: "deny" | "post_check" | "headless" | "failure";
  agent?: string;
  reason: string;
}

const GUARD_EVENTS = new Map<string, GuardEvent[]>();
const MAX_GUARD_EVENTS = 50;
const PENDING_DONE_VERIFY = new Map<string, Set<Promise<void>>>();

function recordGuardEvent(sessionID: string, event: GuardEvent): void {
  const list = GUARD_EVENTS.get(sessionID) ?? [];
  list.push(event);
  if (list.length > MAX_GUARD_EVENTS) list.shift();
  GUARD_EVENTS.set(sessionID, list);
}

export function guardEventsFor(sessionID: string): readonly GuardEvent[] {
  return GUARD_EVENTS.get(sessionID) ?? [];
}

export function clearGuardEvents(sessionID: string): void {
  GUARD_EVENTS.delete(sessionID);
}

/**
 * Wait for the background done-verify of a session. Without this, a child that ends immediately after
 * a weakening command could be read before its gate result arrives, and pass with a guard event that
 * nobody saw. Bounded by the caller; a timeout is a guard failure, never a pass.
 */
export async function awaitPendingGuard(sessionID: string, timeoutMs: number): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const pending = PENDING_DONE_VERIFY.get(sessionID);
    if (!pending || pending.size === 0) return;
    const remaining = deadline - Date.now();
    if (remaining <= 0) {
      throw new GuardError(`[ck guard] the done-verify hook did not finish within ${timeoutMs}ms`);
    }
    let timer: NodeJS.Timeout | undefined;
    const timeout = new Promise<"timeout">((resolve) => {
      timer = setTimeout(() => resolve("timeout"), remaining);
    });
    const settled = await Promise.race([
      Promise.allSettled([...pending]).then(() => "settled" as const),
      timeout,
    ]);
    if (timer) clearTimeout(timer);
    if (settled === "timeout") {
      throw new GuardError(`[ck guard] the done-verify hook did not finish within ${timeoutMs}ms`);
    }
  }
}

function trackDoneVerify(sessionID: string, task: Promise<void>): void {
  const set = PENDING_DONE_VERIFY.get(sessionID) ?? new Set<Promise<void>>();
  set.add(task);
  PENDING_DONE_VERIFY.set(sessionID, set);
  void task.finally(() => {
    set.delete(task);
    if (set.size === 0 && PENDING_DONE_VERIFY.get(sessionID) === set) {
      PENDING_DONE_VERIFY.delete(sessionID);
    }
  });
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

  const latch = (sessionID: string, reason: string, kind: GuardEvent["kind"] = "post_check"): void => {
    debug(`latch session=${sessionID} reason=${reason.slice(0, 300)}`);
    recordGuardEvent(sessionID, { at: Date.now(), kind, reason });
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

    debug(`before tool=${event.tool} session=${event.sessionID} id=${event.id}`);
    const pending = latches.get(event.sessionID);
    if (pending !== undefined) {
      latches.delete(event.sessionID);
      debug(`before latch fired session=${event.sessionID}`);
      recordGuardEvent(event.sessionID, {
        at: Date.now(),
        kind: "post_check",
        agent: event.agent,
        reason: pending,
      });
      throw new GuardError(pending);
    }

    let translation: Translation | null;
    try {
      translation = translateTool(event);
    } catch (error) {
      const reason = message(error);
      recordGuardEvent(event.sessionID, { at: Date.now(), kind: "deny", agent: event.agent, reason });
      throw error;
    }
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
      const reason = `[ck guard] the pre-tool guard timed out after ${config.guard.timeouts.preToolGuard}s`;
      recordGuardEvent(event.sessionID, { at: Date.now(), kind: "failure", agent: event.agent, reason });
      throw new GuardError(reason);
    }
    if (result.code !== 0) {
      const reason = `[ck guard] the pre-tool guard failed (exit ${result.code ?? "unknown"}): ${result.stderr.trim() || result.stdout.trim()}`;
      recordGuardEvent(event.sessionID, { at: Date.now(), kind: "failure", agent: event.agent, reason });
      throw new GuardError(reason);
    }

    const output = parseHookOutput(result.stdout, "pre-tool-guard");
    const decision = output?.hookSpecificOutput?.permissionDecision;
    if (!decision || decision === "allow") return;

    const reason = output?.hookSpecificOutput?.permissionDecisionReason || "[ck guard] this call needs approval";
    if (decision === "deny") {
      recordGuardEvent(event.sessionID, { at: Date.now(), kind: "deny", agent: event.agent, reason });
      throw new GuardError(reason);
    }
    if (decision === "ask") {
      if (headless) {
        recordGuardEvent(event.sessionID, { at: Date.now(), kind: "headless", agent: event.agent, reason });
        throw new GuardError(reason);
      }
      try {
        await ctx.permission.assert({
          sessionID: event.sessionID,
          action: translation.action,
          resources: [translation.resource],
        });
      } catch (error) {
        recordGuardEvent(event.sessionID, { at: Date.now(), kind: "deny", agent: event.agent, reason });
        throw new GuardError(reason || message(error));
      }
      return;
    }
    const unknown = `[ck guard] the pre-tool guard returned an unknown decision: ${JSON.stringify(decision)}`;
    recordGuardEvent(event.sessionID, { at: Date.now(), kind: "failure", agent: event.agent, reason: unknown });
    throw new GuardError(unknown);
  };

  const after = async (raw: unknown): Promise<void> => {
    const event = raw as GuardToolEvent;
    if (!event || typeof event !== "object" || typeof event.tool !== "string") return;
    if (event.tool === "execute") return;

    debug(`after tool=${event.tool} session=${event.sessionID} id=${event.id}`);
    let translation: Translation | null;
    try {
      translation = translateTool(event);
    } catch (error) {
      latch(event.sessionID, message(error), "failure");
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
            "failure",
          );
        } else {
          const output = parseHookOutput(result.stdout, "post-file-ops");
          const context = output?.hookSpecificOutput?.additionalContext;
          if (context) await postContext(event.sessionID, context);
          if (output?.systemMessage) await postContext(event.sessionID, output.systemMessage);
        }
      } catch (error) {
        latch(event.sessionID, message(error), "failure");
      }
    }

    if (config.guard.hooks.doneVerify) {
      // The gate can take minutes; run it in the background and deliver a block as a latch.
      const task = runCkHook("done-verify", input, cwd, config.guard.timeouts.doneVerify * 1000)
        .then(async (result) => {
          debug(
            `done-verify session=${event.sessionID} code=${result.code} timedOut=${result.timedOut} stdout=${result.stdout.trim().slice(0, 300)}`,
          );
          if (result.timedOut) {
            latch(
              event.sessionID,
              `[ck guard] the done-verify hook timed out after ${config.guard.timeouts.doneVerify}s`,
              "failure",
            );
            return;
          }
          if (result.code !== 0) {
            latch(
              event.sessionID,
              `[ck guard] the done-verify hook failed (exit ${result.code ?? "unknown"}): ${result.stderr.trim()}`,
              "failure",
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
          latch(event.sessionID, message(error), "failure");
        });
      trackDoneVerify(event.sessionID, task);
    }
  };

  registrations.push(await ctx.tool.hook("execute.before", before));
  registrations.push(await ctx.tool.hook("execute.after", after));

  const abort = new AbortController();
  const lastStopWarning = new Map<string, string>();
  let stopVerifyLoop: Promise<void> | null = null;

  const stopVerify = async (sessionID: string, cwd: string): Promise<void> => {
    try {
      const result = await runCkHook(
        "stop-verify",
        {
          cwd,
          tool_name: "",
          tool_input: {},
          session_id: sessionID,
          tool_use_id: `stop-${sessionID}-${Date.now()}`,
        },
        cwd,
        config.guard.timeouts.stopVerify * 1000,
      );
      if (result.timedOut || result.code !== 0) {
        debug(`stop-verify failed session=${sessionID}`);
        return;
      }
      const output = parseHookOutput(result.stdout, "stop-verify");
      const warning = output?.systemMessage;
      if (!warning || lastStopWarning.get(sessionID) === warning) return;
      lastStopWarning.set(sessionID, warning);
      await postContext(sessionID, warning);
    } catch (error) {
      debug(`stop-verify error session=${sessionID}: ${message(error)}`);
    }
  };

  if (config.guard.hooks.stopVerify) {
    const events = (
      ctx as unknown as {
        event?: { subscribe?: (options: unknown) => AsyncIterable<unknown> };
      }
    ).event;
    const subscribe = events?.subscribe;
    if (events && typeof subscribe === "function") {
      stopVerifyLoop = (async () => {
        try {
          for await (const raw of subscribe.call(events, {
            types: ["session.idle"],
            signal: abort.signal,
          })) {
            const event = raw as { type?: string; data?: { sessionID?: string } };
            if (event.type !== "session.idle" || !event.data?.sessionID) continue;
            const meta = await sessionMeta(event.data.sessionID);
            // Child sessions are handled by the workflow's own gate; stop-verify is a main-turn warning.
            if (meta.parentID) continue;
            void stopVerify(event.data.sessionID, meta.directory);
          }
        } catch (error) {
          if (!abort.signal.aborted) debug(`session.idle subscription ended: ${message(error)}`);
        }
      })();
    }
  }

  return {
    dispose: async () => {
      abort.abort();
      if (stopVerifyLoop) await stopVerifyLoop.catch(() => {});
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
