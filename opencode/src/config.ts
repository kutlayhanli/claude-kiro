/**
 * Plugin configuration: built-in defaults, then the global ck config, then the project's
 * `specs/ck.json`, then the plugin options object that OpenCode hands to `setup(ctx)`.
 *
 * Only the `opencode` key is read from the ck config files. A setting that exists for Claude Code
 * (agents, verify commands, gates) stays exactly where it is; this layer only answers "which
 * OpenCode model/agent/hook should that become?". Unknown keys fail loudly: a typo that silently
 * does nothing would surface as a wrong model twenty minutes into a run.
 */

import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { EFFORTS, MODELS, isAlias, isEffort, parseRef } from "./models.ts";

export interface AgentRule {
  /** Regular expression source matched (case-insensitively) against the workflow's `label`. */
  match: string;
  agent: string;
}

export interface AgentMapping {
  default: string;
  rules: readonly AgentRule[];
}

export interface GuardConfig {
  enabled: boolean;
  hooks: {
    preToolGuard: boolean;
    postFileOps: boolean;
    doneVerify: boolean;
    stopVerify: boolean;
  };
  timeouts: {
    preToolGuard: number;
    postFileOps: number;
    doneVerify: number;
    stopVerify: number;
  };
}

export interface RunConfig {
  childTimeoutMinutes: number;
  /** How long a child may sit without starting before it is treated as a dead agent id. */
  startTimeoutSeconds: number;
  pollIntervalMs: number;
  /** In-child result rejections allowed before one whole-child retry. */
  resultRepairAttempts: number;
  /** Where run records and logs live; must stay outside the worktree under test. */
  stateDir: string | null;
  notifyCommand: string | null;
}

export interface EffortConfig {
  default: Record<string, string>;
  models: Record<string, Record<string, string>>;
}

export interface CkConfig {
  models: Record<string, string>;
  efforts: EffortConfig;
  agents: AgentMapping;
  guard: GuardConfig;
  run: RunConfig;
}

export interface LoadedConfig {
  config: CkConfig;
  /** Dotted path -> "default", "options", or the file it came from. */
  sources: Record<string, string>;
  files: { global: string; project: string };
}

export class ConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ConfigError";
  }
}

export const CONFIG_KEY = "opencode";

export const DEFAULT_MODELS: Readonly<Record<string, string>> = Object.freeze({
  haiku: "opencode/claude-haiku-5-5",
  sonnet: "opencode/claude-sonnet-5-5",
  opus: "opencode/claude-opus-5-5",
  fable: "opencode/claude-fable-5-1",
});

export function defaultConfig(): CkConfig {
  return {
    models: { ...DEFAULT_MODELS },
    efforts: { default: {}, models: {} },
    agents: {
      default: "ck-worker",
      rules: [{ match: "^(review|refute|cross-test)", agent: "ck-reviewer" }],
    },
    guard: {
      enabled: true,
      hooks: { preToolGuard: true, postFileOps: true, doneVerify: true, stopVerify: true },
      timeouts: { preToolGuard: 30, postFileOps: 10, doneVerify: 600, stopVerify: 30 },
    },
    run: {
      childTimeoutMinutes: 90,
      startTimeoutSeconds: 60,
      pollIntervalMs: 250,
      resultRepairAttempts: 1,
      stateDir: null,
      notifyCommand: null,
    },
  };
}

export interface LoadConfigInput {
  /** Project directory (the OpenCode session directory). */
  directory: string;
  /** The plugin options object from `opencode.json`; highest precedence. */
  options?: Readonly<Record<string, unknown>> | null;
  env?: Readonly<Record<string, string | undefined>>;
}

export function globalConfigPath(env: Readonly<Record<string, string | undefined>> = process.env): string {
  const base =
    env.CK_CONFIG_HOME ||
    join(env.XDG_CONFIG_HOME || join(homedir(), ".config"), "claude-kiro");
  return join(base, "config.json");
}

export function projectConfigPath(directory: string): string {
  return join(directory, "specs", "ck.json");
}

/** Where run logs and records live. Always outside the worktree under test. */
export function resolveStateDir(
  config: CkConfig,
  env: Readonly<Record<string, string | undefined>> = process.env,
): string {
  if (config.run.stateDir) return config.run.stateDir;
  return join(env.XDG_STATE_HOME || join(homedir(), ".local", "state"), "claude-kiro", "opencode");
}

export function loadConfig(input: LoadConfigInput): LoadedConfig {
  const env = input.env ?? process.env;
  const files = { global: globalConfigPath(env), project: projectConfigPath(input.directory) };
  const config = defaultConfig();
  const sources: Record<string, string> = {};
  for (const key of Object.keys(config.models)) sources[`models.${key}`] = "default";
  sources["agents.default"] = "default";
  sources["agents.rules"] = "default";
  sources["guard.enabled"] = "default";
  for (const key of Object.keys(config.guard.hooks)) sources[`guard.hooks.${key}`] = "default";
  for (const key of Object.keys(config.guard.timeouts)) sources[`guard.timeouts.${key}`] = "default";
  for (const key of Object.keys(config.run)) sources[`run.${key}`] = "default";

  const globalJson = readJsonFile(files.global);
  const projectJson = readJsonFile(files.project);
  mergeLayer(config, globalJson[CONFIG_KEY], files.global, sources);
  mergeLayer(config, projectJson[CONFIG_KEY], files.project, sources);
  if (input.options) mergeLayer(config, input.options, "options", sources);

  // ck's own notification setting is the fallback when `opencode.run.notifyCommand` is unset.
  if (config.run.notifyCommand === null) {
    const projectNotify = readCkNotifyCommand(projectJson);
    const globalNotify = readCkNotifyCommand(globalJson);
    if (projectNotify !== null) {
      config.run.notifyCommand = projectNotify;
      sources["run.notifyCommand"] = files.project;
    } else if (globalNotify !== null) {
      config.run.notifyCommand = globalNotify;
      sources["run.notifyCommand"] = files.global;
    }
  }

  return { config, sources, files };
}

/** Pick the OpenCode agent for one workflow `agent()` label. First matching rule wins. */
export function selectAgent(label: string, mapping: AgentMapping): string {
  for (const rule of mapping.rules) {
    if (new RegExp(rule.match, "i").test(label)) return rule.agent;
  }
  return mapping.default;
}

function mergeLayer(
  target: CkConfig,
  layer: unknown,
  source: string,
  sources: Record<string, string>,
): void {
  if (layer === undefined || layer === null) return;
  if (!isPlainObject(layer)) throw new ConfigError(`${source}: ${CONFIG_KEY} must be an object`);

  for (const key of Object.keys(layer)) {
    if (!["models", "efforts", "agents", "guard", "run"].includes(key)) {
      throw new ConfigError(
        `${source}: unknown key ${JSON.stringify(key)} (known: models, efforts, agents, guard, run)`,
      );
    }
  }

  if (layer.models !== undefined) mergeModels(target, layer.models, source, sources);
  if (layer.efforts !== undefined) mergeEfforts(target, layer.efforts, source, sources);
  if (layer.agents !== undefined) mergeAgents(target, layer.agents, source, sources);
  if (layer.guard !== undefined) mergeGuard(target, layer.guard, source, sources);
  if (layer.run !== undefined) mergeRun(target, layer.run, source, sources);
}

function mergeModels(
  target: CkConfig,
  value: unknown,
  source: string,
  sources: Record<string, string>,
): void {
  if (!isPlainObject(value)) throw new ConfigError(`${source}: models must be an object`);
  for (const [alias, ref] of Object.entries(value)) {
    if (!isAlias(alias)) {
      throw new ConfigError(
        `${source}: unknown model alias ${JSON.stringify(alias)} (known: ${MODELS.join(", ")})`,
      );
    }
    if (typeof ref !== "string" || !ref.trim()) {
      throw new ConfigError(`${source}: models.${alias} must be a non-empty provider/model reference`);
    }
    try {
      parseRef(ref);
    } catch {
      throw new ConfigError(
        `${source}: models.${alias} is not a valid model reference: ${JSON.stringify(ref)}`,
      );
    }
    target.models[alias] = ref;
    sources[`models.${alias}`] = source;
  }
}

function mergeEfforts(
  target: CkConfig,
  value: unknown,
  source: string,
  sources: Record<string, string>,
): void {
  if (!isPlainObject(value)) throw new ConfigError(`${source}: efforts must be an object`);
  for (const key of Object.keys(value)) {
    if (key !== "default" && key !== "models") {
      throw new ConfigError(`${source}: unknown key efforts.${key} (known: default, models)`);
    }
  }

  if (value.default !== undefined) {
    if (!isPlainObject(value.default)) throw new ConfigError(`${source}: efforts.default must be an object`);
    for (const [effort, variant] of Object.entries(value.default)) {
      assertEffortKey(effort, `${source}: efforts.default`);
      if (typeof variant !== "string" || !variant.trim()) {
        throw new ConfigError(`${source}: efforts.default.${effort} must be a non-empty variant name`);
      }
      target.efforts.default[effort] = variant;
      sources[`efforts.default.${effort}`] = source;
    }
  }

  if (value.models !== undefined) {
    if (!isPlainObject(value.models)) throw new ConfigError(`${source}: efforts.models must be an object`);
    for (const [ref, mapping] of Object.entries(value.models)) {
      try {
        parseRef(ref);
      } catch {
        throw new ConfigError(`${source}: efforts.models key is not a valid model reference: ${JSON.stringify(ref)}`);
      }
      if (!isPlainObject(mapping)) {
        throw new ConfigError(`${source}: efforts.models[${JSON.stringify(ref)}] must be an object`);
      }
      target.efforts.models[ref] ??= {};
      for (const [effort, variant] of Object.entries(mapping)) {
        assertEffortKey(effort, `${source}: efforts.models[${JSON.stringify(ref)}]`);
        if (typeof variant !== "string" || !variant.trim()) {
          throw new ConfigError(
            `${source}: efforts.models[${JSON.stringify(ref)}].${effort} must be a non-empty variant name`,
          );
        }
        target.efforts.models[ref]![effort] = variant;
        sources[`efforts.models.${ref}.${effort}`] = source;
      }
    }
  }
}

function mergeAgents(
  target: CkConfig,
  value: unknown,
  source: string,
  sources: Record<string, string>,
): void {
  if (!isPlainObject(value)) throw new ConfigError(`${source}: agents must be an object`);
  for (const key of Object.keys(value)) {
    if (key !== "default" && key !== "rules") {
      throw new ConfigError(`${source}: unknown key agents.${key} (known: default, rules)`);
    }
  }

  if (value.default !== undefined) {
    assertAgentId(value.default, `${source}: agents.default`);
    target.agents.default = value.default;
    sources["agents.default"] = source;
  }

  if (value.rules !== undefined) {
    if (!Array.isArray(value.rules)) throw new ConfigError(`${source}: agents.rules must be an array`);
    const rules: AgentRule[] = [];
    value.rules.forEach((rule, index) => {
      if (!isPlainObject(rule)) throw new ConfigError(`${source}: agents.rules[${index}] must be an object`);
      for (const key of Object.keys(rule)) {
        if (key !== "match" && key !== "agent") {
          throw new ConfigError(`${source}: unknown key agents.rules[${index}].${key} (known: match, agent)`);
        }
      }
      if (typeof rule.match !== "string" || !rule.match) {
        throw new ConfigError(`${source}: agents.rules[${index}].match must be a non-empty regular expression`);
      }
      try {
        new RegExp(rule.match);
      } catch (error) {
        throw new ConfigError(`${source}: agents.rules[${index}].match is not a valid regular expression (${message(error)})`);
      }
      assertAgentId(rule.agent, `${source}: agents.rules[${index}].agent`);
      rules.push({ match: rule.match, agent: rule.agent });
    });
    target.agents.rules = rules;
    sources["agents.rules"] = source;
  }
}

function mergeGuard(
  target: CkConfig,
  value: unknown,
  source: string,
  sources: Record<string, string>,
): void {
  if (!isPlainObject(value)) throw new ConfigError(`${source}: guard must be an object`);
  for (const key of Object.keys(value)) {
    if (!["enabled", "hooks", "timeouts"].includes(key)) {
      throw new ConfigError(`${source}: unknown key guard.${key} (known: enabled, hooks, timeouts)`);
    }
  }

  if (value.enabled !== undefined) {
    if (typeof value.enabled !== "boolean") throw new ConfigError(`${source}: guard.enabled must be true or false`);
    target.guard.enabled = value.enabled;
    sources["guard.enabled"] = source;
  }

  if (value.hooks !== undefined) {
    if (!isPlainObject(value.hooks)) throw new ConfigError(`${source}: guard.hooks must be an object`);
    for (const key of Object.keys(value.hooks)) {
      if (!(key in target.guard.hooks)) {
        throw new ConfigError(
          `${source}: unknown key guard.hooks.${key} (known: ${Object.keys(target.guard.hooks).join(", ")})`,
        );
      }
      const flag = value.hooks[key];
      if (typeof flag !== "boolean") throw new ConfigError(`${source}: guard.hooks.${key} must be true or false`);
      target.guard.hooks[key as keyof GuardConfig["hooks"]] = flag;
      sources[`guard.hooks.${key}`] = source;
    }
  }

  if (value.timeouts !== undefined) {
    if (!isPlainObject(value.timeouts)) throw new ConfigError(`${source}: guard.timeouts must be an object`);
    for (const key of Object.keys(value.timeouts)) {
      if (!(key in target.guard.timeouts)) {
        throw new ConfigError(
          `${source}: unknown key guard.timeouts.${key} (known: ${Object.keys(target.guard.timeouts).join(", ")})`,
        );
      }
      const seconds = value.timeouts[key];
      if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds <= 0) {
        throw new ConfigError(`${source}: guard.timeouts.${key} must be a positive number of seconds`);
      }
      target.guard.timeouts[key as keyof GuardConfig["timeouts"]] = seconds;
      sources[`guard.timeouts.${key}`] = source;
    }
  }
}

function mergeRun(
  target: CkConfig,
  value: unknown,
  source: string,
  sources: Record<string, string>,
): void {
  if (!isPlainObject(value)) throw new ConfigError(`${source}: run must be an object`);
  for (const key of Object.keys(value)) {
    if (!(key in target.run)) {
      throw new ConfigError(`${source}: unknown key run.${key} (known: ${Object.keys(target.run).join(", ")})`);
    }
  }

  if (value.childTimeoutMinutes !== undefined) {
    target.run.childTimeoutMinutes = positiveNumber(value.childTimeoutMinutes, `${source}: run.childTimeoutMinutes`, true);
    sources["run.childTimeoutMinutes"] = source;
  }
  if (value.startTimeoutSeconds !== undefined) {
    target.run.startTimeoutSeconds = positiveNumber(value.startTimeoutSeconds, `${source}: run.startTimeoutSeconds`, true);
    sources["run.startTimeoutSeconds"] = source;
  }
  if (value.pollIntervalMs !== undefined) {
    const poll = positiveNumber(value.pollIntervalMs, `${source}: run.pollIntervalMs`, true);
    if (poll < 10) throw new ConfigError(`${source}: run.pollIntervalMs must be at least 10`);
    target.run.pollIntervalMs = poll;
    sources["run.pollIntervalMs"] = source;
  }
  if (value.resultRepairAttempts !== undefined) {
    const attempts = value.resultRepairAttempts;
    if (typeof attempts !== "number" || !Number.isInteger(attempts) || attempts < 0) {
      throw new ConfigError(`${source}: run.resultRepairAttempts must be a whole number of 0 or more`);
    }
    target.run.resultRepairAttempts = attempts;
    sources["run.resultRepairAttempts"] = source;
  }
  if (value.stateDir !== undefined) {
    const dir = value.stateDir;
    if (dir !== null && (typeof dir !== "string" || !dir.trim())) {
      throw new ConfigError(`${source}: run.stateDir must be a non-empty path or null`);
    }
    target.run.stateDir = dir;
    sources["run.stateDir"] = source;
  }
  if (value.notifyCommand !== undefined) {
    const command = value.notifyCommand;
    if (command !== null && (typeof command !== "string" || !command.trim())) {
      throw new ConfigError(`${source}: run.notifyCommand must be a non-empty command or null`);
    }
    target.run.notifyCommand = command;
    sources["run.notifyCommand"] = source;
  }
}

function assertEffortKey(effort: string, where: string): void {
  if (!isEffort(effort)) {
    throw new ConfigError(`${where}: unknown effort ${JSON.stringify(effort)} (known: ${EFFORTS.join(", ")})`);
  }
}

function assertAgentId(value: unknown, where: string): asserts value is string {
  if (typeof value !== "string" || !value.trim()) {
    throw new ConfigError(`${where} must be a non-empty agent id`);
  }
  // OpenCode only discovers .opencode/agents/<name>.md; a nested id silently runs no model at all.
  if (value.includes("/")) {
    throw new ConfigError(
      `${where} must be a flat agent id (no "/"): OpenCode ignores nested .opencode/agents directories`,
    );
  }
}

function positiveNumber(value: unknown, where: string, allowFraction: boolean): number {
  if (
    typeof value !== "number" ||
    !Number.isFinite(value) ||
    value <= 0 ||
    (!allowFraction && !Number.isInteger(value))
  ) {
    throw new ConfigError(`${where} must be a positive ${allowFraction ? "number" : "whole number"}`);
  }
  return value;
}

function readJsonFile(path: string): Record<string, unknown> {
  let text: string;
  try {
    text = readFileSync(path, "utf8");
  } catch {
    return {};
  }
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch (error) {
    throw new ConfigError(`${path}: invalid JSON (${message(error)})`);
  }
  if (!isPlainObject(data)) throw new ConfigError(`${path}: the root must be a JSON object`);
  return data;
}

function readCkNotifyCommand(json: Record<string, unknown>): string | null {
  const agents = json.agents;
  if (!isPlainObject(agents)) return null;
  const run = agents.run;
  if (!isPlainObject(run)) return null;
  const command = run.notify_command;
  return typeof command === "string" && command.trim() ? command : null;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
