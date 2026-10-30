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
}

export type ChildErrorKind =
  | "model"
  | "agent"
  | "session"
  | "start-timeout"
  | "timeout"
  | "failed"
  | "result";

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
      };
    } catch (error) {
      const failure = asChildError(error, "failed");
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
        location: { directory: options.projectDir },
      });
    } catch (error) {
      throw new ChildError("session", `could not create a child session: ${message(error)}`);
    }
    sessionID = created.id;

    try {
      await options.ctx.session.prompt({ sessionID, text, delivery: "queue" });
    } catch (error) {
      throw new ChildError("session", `could not prompt child ${sessionID}: ${message(error)}`);
    }

    const info = await observe(options.ctx, sessionID, options.config);
    const messages = (await options.ctx.session.context({ sessionID })) as unknown as MessageInfo[];
    const failure = failureOf(messages, info);
    if (failure) {
      throw new ChildError("failed", `child ${options.label} failed: ${failure}`, {
        sessionID,
        tokens: info.tokens,
      });
    }

    const zeroTokens = (info.tokens?.input ?? 0) === 0 && (info.tokens?.output ?? 0) === 0;
    const producedAssistant = messages.some((entry) => entry.type === "assistant");
    if (zeroTokens && !producedAssistant) {
      throw new ChildError(
        "agent",
        `agent ${JSON.stringify(agent)} produced no assistant message and no tokens; ` +
          `check that .opencode/agents/${agent}.md exists (OpenCode silently accepts unknown agent ids)`,
        { sessionID },
      );
    }

    const endedAt = Date.now();
    if (!validator) {
      const textValue = lastAssistantText(messages);
      return {
        value: textValue,
        sessionID,
        submission: textValue ? "text" : "text-empty",
        rejections: 0,
        info,
        messages,
        startedAt,
        endedAt,
      };
    }

    const accepted = resultTool?.accepted() ?? null;
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
      };
    }

    const textValue = lastAssistantText(messages);
    const parsed = parseJsonCandidate(textValue);
    if (parsed === undefined) {
      throw new ChildError(
        "result",
        `the child finished without calling ${toolName}, and its answer is not JSON`,
        { sessionID, text: textValue.slice(0, 1000) },
      );
    }
    const issues: SchemaIssue[] = validator(parsed);
    if (issues.length > 0) {
      throw new ChildError("result", `the child's JSON answer does not match the schema:\n${formatIssues(issues)}`, {
        sessionID,
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
