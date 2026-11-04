/**
 * Run one workflow `agent()` call in an OpenCode child session and bring back its result.
 *
 * Completion is observed, never trusted to a single `session.wait` (which can resolve before the
 * run starts). A child that produces zero tokens is reported as a dead agent id immediately: an
 * unknown id is silently accepted by `session.create`, so it would otherwise look like an empty
 * answer twenty minutes later.
 */

import type { Plugin } from "@opencode/plugin";
import { selectAgent, type CkConfig } from "./config.ts";
import {
  INHERIT,
  ModelError,
  formatRef,
  resolveModel,
  type Catalog,
  type ModelRef,
  type Resolution,
} from "./models.ts";
import { awaitPendingGuard, clearGuardEvents, guardEventsFor, type GuardEvent } from "./guard.ts";
import { compile, formatIssues, type SchemaIssue, type Validator } from "./schema.ts";
import { registerResultTool } from "./result-tool.ts";

export interface TokenUsage {
  input: number;
  output: number;
  reasoning: number;
  cache: { read: number; write: number };
}

export interface SessionView {
  outcome?: "succeeded" | "failed" | "interrupted";
  tokens: TokenUsage;
  cost: number;
  time: { created: number; updated: number; busy?: number; idle?: number };
  model?: ModelRef;
}

export interface MessagePart {
  type: string;
  text?: string;
  tool?: string;
  status?: string;
  input?: unknown;
}

export interface MessageInfo {
  type: string;
  content?: MessagePart[];
  finish?: string;
  error?: { message?: string };
  tokens?: TokenUsage;
}

export type ChildErrorKind =
  | "model"
  | "agent"
  | "session"
  | "start-timeout"
  | "timeout"
  | "failed"
  | "result"
  | "guard";

export class ChildError extends Error {
  readonly kind: ChildErrorKind;
  readonly details: Record<string, unknown>;

  constructor(kind: ChildErrorKind, message: string, details: Record<string, unknown> = {}) {
    super(message);
    this.name = "ChildError";
    this.kind = kind;
    this.details = details;
  }
}

export interface RunChildOptions {
  ctx: Plugin.Context;
  /** The session that invoked the workflow; progress and results are posted here. */
  parentSessionID: string;
  projectDir: string;
  config: CkConfig;
  catalog?: Catalog;
  /** Unique per agent call; becomes the `submit_result_<seq>` name. */
  seq: number;
  prompt: string;
  label: string;
  phase?: string | null;
  model?: string | null;
  effort?: string | null;
  schema?: unknown;
}

export interface ChildOutcome {
  value: unknown;
  sessionID: string;
  agent: string;
  requestedModel: string;
  requestedEffort: string | null;
  requestedVariant: string | null;
  resolvedModel: ModelRef;
  resolvedVariant: string | null;
  tokens: TokenUsage;
  cost: number;
  startedAt: number;
  endedAt: number;
  attempts: number;
  submission: "tool" | "text" | "text-empty";
  rejections: number;
  notes: string[];
  guardEvents: readonly GuardEvent[];
  peakContext: number;
}

interface AttemptResult {
  value: unknown;
  sessionID: string;
  submission: "tool" | "text" | "text-empty";
  rejections: number;
  info: SessionView;
  messages: MessageInfo[];
  startedAt: number;
  endedAt: number;
  guardEvents: readonly GuardEvent[];
  peakContext: number;
}

const EMPTY_TOKENS: TokenUsage = { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } };

export async function runChild(options: RunChildOptions): Promise<ChildOutcome> {
  const resolved = await resolveChildModel(options);
  const agent = selectAgent(options.label || "agent", options.config.agents);
  const validator: Validator | null = options.schema === undefined ? null : compile(options.schema);
  const attemptsAllowed = validator ? 1 + options.config.run.resultRepairAttempts : 1;

  let lastFailure: ChildError | null = null;
  for (let attempt = 1; attempt <= attemptsAllowed; attempt += 1) {
    const prompt =
      attempt === 1
        ? options.prompt
        : retryPrompt(options.prompt, resultToolName(options.seq), lastFailure?.message ?? "no valid result");

    try {
      const result = await runAttempt(options, resolved, agent, validator, prompt);
      return {
        value: result.value,
        sessionID: result.sessionID,
        agent,
        requestedModel: resolved.requested.model,
        requestedEffort: resolved.requested.effort,
        requestedVariant: resolved.requested.variant,
        resolvedModel: resolved.resolved,
        resolvedVariant: resolved.resolvedEffort,
        tokens: result.info.tokens ?? EMPTY_TOKENS,
        cost: result.info.cost,
        startedAt: result.startedAt,
        endedAt: result.endedAt,
        attempts: attempt,
        submission: result.submission,
        rejections: result.rejections,
        notes: resolved.notes,
        guardEvents: result.guardEvents,
        peakContext: result.peakContext,
      };
    } catch (error) {
      const failure = asChildError(error, "failed");
      failure.details.requestedModel ??= resolved.requested.model;
      failure.details.requestedEffort ??= resolved.requested.effort;
      failure.details.requestedVariant ??= resolved.requested.variant;
      failure.details.resolvedModel ??= resolved.resolved;
      failure.details.resolvedVariant ??= resolved.resolvedEffort;
      failure.details.attempts ??= attempt;
      if (failure.kind !== "result" || attempt === attemptsAllowed) throw failure;
      lastFailure = failure;
    }
  }
  throw lastFailure ?? new ChildError("result", "the child produced no result");
}

export function resultToolName(seq: number): string {
  return `submit_result_${seq}`;
}

export function resultInstruction(toolName: string): string {
  return (
    `\n\n---\nWhen you are finished, call the \`${toolName}\` tool exactly once with your final ` +
    "structured result. Do not put the JSON in a prose answer; the tool call is the deliverable."
  );
}

export function retryPrompt(original: string, toolName: string, reason: string): string {
  return (
    `Your previous attempt did not produce a valid structured result.\n` +
    `Reason: ${reason}\n\n` +
    `${original}${resultInstruction(toolName)}`
  );
}

/** Last non-empty assistant text; earlier turns are tool calls, not the answer. */
export function lastAssistantText(messages: readonly MessageInfo[]): string {
  let text = "";
  for (const message of messages) {
    if (message.type !== "assistant") continue;
    const parts = (message.content ?? []).filter((part) => part.type === "text" && typeof part.text === "string");
    const joined = parts
      .map((part) => part.text ?? "")
      .join("\n")
      .trim();
    if (joined) text = joined;
  }
  return text;
}

/** Peak context the child held: the largest input + cache read on one assistant message. */
export function peakContextOf(messages: readonly MessageInfo[]): number {
  let peak = 0;
  for (const message of messages) {
    if (message.type !== "assistant" || !message.tokens) continue;
    const context = (message.tokens.input ?? 0) + (message.tokens.cache?.read ?? 0);
    if (context > peak) peak = context;
  }
  return peak;
}

/** Whether the child's structured result tells the workflow about a guard stop. */
export function hasGuardStop(value: unknown): boolean {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const guardStop = (value as Record<string, unknown>).guardStop;
  return guardStop !== undefined && guardStop !== null;
}

export function failureOf(messages: readonly MessageInfo[], info: SessionView): string | null {
  const assistants = messages.filter((message) => message.type === "assistant");
  const last = assistants[assistants.length - 1];
  if (last?.error?.message) return last.error.message;
  if (last?.finish === "error") return "the child's last model call failed";
  if (info.outcome === "interrupted") return "the child session was interrupted";
  if (info.outcome === "failed") return "the child session failed without recording an error";
  return null;
}

/** Parse a plain, fenced, or prose-wrapped JSON value; `undefined` when there is none. */
export function parseJsonCandidate(text: string): unknown {
  const trimmed = text.trim();
  if (!trimmed) return undefined;

  const candidates: string[] = [trimmed];
  const fenced = /```(?:json)?\s*([\s\S]*?)```/i.exec(trimmed);
  if (fenced?.[1]) candidates.push(fenced[1].trim());
  const objectOpen = trimmed.indexOf("{");
  const objectClose = trimmed.lastIndexOf("}");
  if (objectOpen >= 0 && objectClose > objectOpen) {
    candidates.push(trimmed.slice(objectOpen, objectClose + 1));
  }
  const arrayOpen = trimmed.indexOf("[");
  const arrayClose = trimmed.lastIndexOf("]");
  if (arrayOpen >= 0 && arrayClose > arrayOpen) {
    candidates.push(trimmed.slice(arrayOpen, arrayClose + 1));
  }

  for (const candidate of candidates) {
    try {
      return JSON.parse(candidate);
    } catch {
      // try the next shape
    }
  }
  return undefined;
}

async function runAttempt(
  options: RunChildOptions,
  resolved: ChildResolution,
  agent: string,
  validator: Validator | null,
  prompt: string,
): Promise<AttemptResult> {
  const toolName = resultToolName(options.seq);
  const resultTool = validator
    ? await registerResultTool(options.ctx, {
        name: toolName,
        label: options.label,
        schema: options.schema,
        validate: validator,
      })
    : null;

  const startedAt = Date.now();
  let sessionID: string | null = null;
  try {
    const text = validator ? `${prompt}${resultInstruction(toolName)}` : prompt;
    let created;
    try {
      created = await options.ctx.session.create({
        title: `ck: ${options.label}`,
        model: resolved.resolved,
        agent,
        parentID: options.parentSessionID,
        location: { directory: options.projectDir },
      });
    } catch (error) {
      throw new ChildError("session", `could not create a child session: ${message(error)}`, {
        startedAt,
        endedAt: Date.now(),
      });
    }
    sessionID = created.id;

    try {
      await options.ctx.session.prompt({ sessionID, text, delivery: "queue" });
    } catch (error) {
      throw new ChildError("session", `could not prompt child ${sessionID}: ${message(error)}`, {
        sessionID,
        startedAt,
        endedAt: Date.now(),
      });
    }

    let info: SessionView;
    try {
      info = await observe(options.ctx, sessionID, options.config);
    } catch (error) {
      if (error instanceof ChildError) {
        error.details.sessionID ??= sessionID;
        error.details.startedAt ??= startedAt;
        error.details.endedAt ??= Date.now();
      }
      throw error;
    }
    // A child can finish right after a weakening command; wait for its background gate before
    // deciding the result, or the post-check would arrive after the child was already accepted.
    try {
      await awaitPendingGuard(
        sessionID,
        Math.max(5000, options.config.guard.timeouts.doneVerify * 1000 + 5000),
      );
    } catch (error) {
      throw new ChildError("guard", message(error), {
        sessionID,
        startedAt,
        endedAt: Date.now(),
        tokens: info.tokens,
      });
    }
    const messages = (await options.ctx.session.context({ sessionID })) as unknown as MessageInfo[];
    const peakContext = peakContextOf(messages);
    const endedAt = Date.now();
    const details = { sessionID, tokens: info.tokens, cost: info.cost, startedAt, endedAt, peakContext };
    const failure = failureOf(messages, info);
    if (failure) {
      throw new ChildError("failed", `child ${options.label} failed: ${failure}`, details);
    }

    const zeroTokens = (info.tokens?.input ?? 0) === 0 && (info.tokens?.output ?? 0) === 0;
    const producedAssistant = messages.some((entry) => entry.type === "assistant");
    if (zeroTokens && !producedAssistant) {
      throw new ChildError(
        "agent",
        `agent ${JSON.stringify(agent)} produced no assistant message and no tokens; ` +
          `check that .opencode/agents/${agent}.md exists (OpenCode silently accepts unknown agent ids)`,
        details,
      );
    }

    const textValue = lastAssistantText(messages);
    const accepted = validator ? (resultTool?.accepted() ?? null) : null;
    const parsed = validator && !accepted ? parseJsonCandidate(textValue) : undefined;

    const value: unknown = !validator
      ? textValue
      : accepted
        ? accepted.value
        : parsed;
    const submission: "tool" | "text" | "text-empty" = !validator
      ? textValue
        ? "text"
        : "text-empty"
      : accepted
        ? "tool"
        : "text";

    // A guard denial means the child tried something it was not allowed to do. If it did not report
    // that as `guardStop`, the step must fail: a green result after a blocked workaround is a
    // measurement of a different product (D12).
    const guardEvents = guardEventsFor(sessionID);
    clearGuardEvents(sessionID);
    if (guardEvents.length > 0 && !hasGuardStop(value)) {
      throw new ChildError(
        "guard",
        `the child hit the guard and did not report guardStop: ${guardEvents[0]?.reason ?? "guard event"}`,
        { ...details, guardEvents, submission, rejections: resultTool?.rejections() ?? 0 },
      );
    }

    if (!validator) {
      return {
        value,
        sessionID,
        submission,
        rejections: 0,
        info,
        messages,
        startedAt,
        endedAt,
        guardEvents,
        peakContext,
      };
    }

    if (accepted) {
      return {
        value: accepted.value,
        sessionID,
        submission: "tool",
        rejections: resultTool?.rejections() ?? 0,
        info,
        messages,
        startedAt,
        endedAt,
        guardEvents,
        peakContext,
      };
    }

    if (parsed === undefined) {
      throw new ChildError(
        "result",
        `the child finished without calling ${toolName}, and its answer is not JSON`,
        { ...details, submission, rejections: resultTool?.rejections() ?? 0, text: textValue.slice(0, 1000) },
      );
    }
    const issues: SchemaIssue[] = validator(parsed);
    if (issues.length > 0) {
      throw new ChildError("result", `the child's JSON answer does not match the schema:\n${formatIssues(issues)}`, {
        ...details,
        submission,
        rejections: resultTool?.rejections() ?? 0,
        text: textValue.slice(0, 1000),
      });
    }
    return {
      value: parsed,
      sessionID,
      submission: "text",
      rejections: resultTool?.rejections() ?? 0,
      info,
      messages,
      startedAt,
      endedAt,
      guardEvents,
      peakContext,
    };
  } finally {
    if (resultTool) await resultTool.registration.dispose().catch(() => {});
  }
}

async function observe(ctx: Plugin.Context, sessionID: string, config: CkConfig): Promise<SessionView> {
  const now = Date.now();
  const deadline = now + config.run.childTimeoutMinutes * 60_000;
  const startDeadline = now + config.run.startTimeoutSeconds * 1000;
  let started = false;
  let info: SessionView | null = null;

  while (Date.now() < deadline) {
    try {
      info = (await ctx.session.get({ sessionID })) as unknown as SessionView;
    } catch (error) {
      throw new ChildError("session", `could not read child ${sessionID}: ${message(error)}`);
    }

    if (info.time.busy !== undefined) started = true;
    if (info.outcome !== undefined) return info;
    if (
      started &&
      info.time.busy !== undefined &&
      info.time.idle !== undefined &&
      info.time.idle >= info.time.busy
    ) {
      return info;
    }
    if (!started && Date.now() > startDeadline) {
      await ctx.session.interrupt({ sessionID }).catch(() => {});
      throw new ChildError(
        "start-timeout",
        `the child did not start within ${config.run.startTimeoutSeconds}s; ` +
          `the agent id may not exist or the provider may be unavailable`,
        { sessionID },
      );
    }
    await sleep(config.run.pollIntervalMs);
  }

  await ctx.session.interrupt({ sessionID }).catch(() => {});
  throw new ChildError(
    "timeout",
    `the child exceeded the ${config.run.childTimeoutMinutes} minute limit`,
    { sessionID, tokens: info?.tokens },
  );
}

interface ChildResolution {
  requested: { model: string; effort: string | null; variant: string | null };
  resolved: ModelRef;
  requestedEffort: string | null;
  resolvedEffort: string | null;
  notes: string[];
}

async function resolveChildModel(options: RunChildOptions): Promise<ChildResolution> {
  const requestedModel = options.model && options.model !== "" ? options.model : INHERIT;
  let resolution: Resolution;
  try {
    resolution = resolveModel(requestedModel, {
      aliases: options.config.models,
      catalog: options.catalog,
      effort: options.effort ?? null,
      efforts: options.config.efforts,
    });
  } catch (error) {
    if (error instanceof ModelError) throw new ChildError("model", error.message);
    throw error;
  }

  if (resolution.resolved) {
    return {
      requested: {
        model: requestedModel,
        effort: resolution.requested.effort,
        variant: resolution.variant,
      },
      resolved: resolution.resolved,
      requestedEffort: resolution.requestedEffort,
      resolvedEffort: resolution.resolvedEffort,
      notes: resolution.notes,
    };
  }

  // `inherit`: use the parent's explicit model. An explicit effort still maps onto that model.
  const parent = (await options.ctx.session.get({ sessionID: options.parentSessionID })) as unknown as SessionView;
  const parentModel = parent.model;
  if (!parentModel) {
    throw new ChildError("model", "model is inherit, but the parent session has no model; pass an explicit model");
  }
  const parentRef: ModelRef = {
    providerID: parentModel.providerID,
    id: parentModel.id,
    ...(parentModel.variant ? { variant: parentModel.variant } : {}),
  };

  const explicitEffort =
    resolution.requestedEffort !== null && resolution.requestedEffort !== INHERIT
      ? resolution.requestedEffort
      : null;
  if (explicitEffort) {
    let inherited: Resolution;
    try {
      inherited = resolveModel(formatRef(parentRef), {
        aliases: options.config.models,
        catalog: options.catalog,
        effort: explicitEffort,
        efforts: options.config.efforts,
      });
    } catch (error) {
      if (error instanceof ModelError) throw new ChildError("model", error.message);
      throw error;
    }
    return {
      requested: { model: requestedModel, effort: resolution.requested.effort, variant: inherited.variant },
      resolved: inherited.resolved as ModelRef,
      requestedEffort: resolution.requestedEffort,
      resolvedEffort: inherited.resolvedEffort,
      notes: [...resolution.notes, ...inherited.notes, `inherited ${formatRef(parentRef)} from the parent session`],
    };
  }

  const model: ModelRef =
    resolution.requestedEffort === INHERIT
      ? { providerID: parentModel.providerID, id: parentModel.id }
      : parentRef;
  return {
    requested: {
      model: requestedModel,
      effort: resolution.requested.effort,
      variant: resolution.variant,
    },
    resolved: model,
    requestedEffort: resolution.requestedEffort,
    resolvedEffort: model.variant ?? null,
    notes: [...resolution.notes, `inherited ${formatRef(model)} from the parent session`],
  };
}

function asChildError(error: unknown, fallback: ChildErrorKind): ChildError {
  if (error instanceof ChildError) return error;
  return new ChildError(fallback, message(error));
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
