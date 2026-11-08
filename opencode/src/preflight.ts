/**
 * Startup preflight for the OpenCode port (D12).
 *
 * Every check is read-only, synchronous underneath, and returns an actionable message plus a fix:
 * a missing binary, an old `ck`, an incomplete `vendor/` tree, an unresolvable model ref, an agent
 * file OpenCode would silently ignore, a dead guard hook, or a dirty tree. The runner binary is
 * `node` from PATH (never `process.execPath`, which is the `opencode` binary), so the first check is
 * what makes the workflow bridge safe to start.
 *
 * The guard hook check is a liveness test only: a real deny self-test belongs to guard.ts. Model
 * variants are warnings (D5 says OpenCode hard-errors on an unknown variant, so the bridge must fix
 * or drop it), missing models are errors, and a dirty tree is a warning because the caller decides
 * whether a clean branch is required (benchmark runs) or not (interactive work).
 */

import { spawnSync } from "node:child_process";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import type { CkConfig } from "./config.ts";
import {
  ROLE_NAMES,
  baseRef,
  isRoleName,
  parseRef,
  resolveRoleRef,
  validateRoleRef,
  type Catalog,
  type CatalogModel,
  type ModelRef,
  type RoleRef,
} from "./models.ts";
import { parseFrontmatter } from "./rewrite.ts";

export interface PreflightExecOptions {
  cwd: string;
  input?: string;
  env?: NodeJS.ProcessEnv;
}

export interface PreflightExecResult {
  status: number | null;
  stdout: string;
  stderr: string;
}

export type PreflightExec = (
  command: string,
  args: string[],
  options: PreflightExecOptions,
) => PreflightExecResult;

export interface PreflightOptions {
  projectDir: string;
  packageRoot: string;
  config: CkConfig;
  catalog?: Catalog;
  env?: NodeJS.ProcessEnv;
  exec?: PreflightExec;
}

export interface PreflightCheck {
  id: string;
  ok: boolean;
  severity: "error" | "warn";
  detail: string;
  fix?: string;
}

export interface PreflightResult {
  ok: boolean;
  checks: PreflightCheck[];
}

export const MIN_NODE_MAJOR = 20;
export const MIN_CK_VERSION = "0.11.1";

export const VENDOR_GROUPS = [
  "workflows",
  "skills",
  "agents",
  "resources",
  "output-styles",
] as const;

/** The flat role agents setup materializes; OpenCode cannot discover nested agent ids (spike 12). */
export const EXPECTED_AGENT_IDS = [
  "ck-worker",
  "ck-implementer",
  "ck-test-writer",
  "ck-reviewer",
  "ck-fixer",
  "ck-resolver",
] as const;

interface Version {
  major: number;
  minor: number;
  patch: number;
}

interface CheckContext {
  options: PreflightOptions;
  env: NodeJS.ProcessEnv;
  exec: PreflightExec;
  ckBin: string;
}

interface Outcome {
  ok: boolean;
  severity?: "error" | "warn";
  detail: string;
  fix?: string;
}

const CK_FIX =
  'Install or upgrade the ck CLI: uv tool install --force "git+https://github.com/kutlayhanli/claude-kiro"';

const MIN_CK: Version = { major: 0, minor: 11, patch: 0 };

export async function runPreflight(options: PreflightOptions): Promise<PreflightResult> {
  const env = options.env ?? process.env;
  const ctx: CheckContext = {
    options,
    env,
    exec: options.exec ?? spawnExec,
    ckBin: env.CK_BIN || "ck",
  };

  const checks: PreflightCheck[] = [
    checkNode(ctx),
    checkCkVersion(ctx),
    checkVendor(ctx),
    ...checkModels(ctx),
    checkRoles(ctx),
    ...EXPECTED_AGENT_IDS.map((id) => checkAgent(ctx, id)),
  ];
  if (options.config.guard.enabled) checks.push(checkGuardHook(ctx));
  checks.push(checkGit(ctx));

  return {
    ok: !checks.some((check) => !check.ok && check.severity === "error"),
    checks,
  };
}

/**
 * Render a result for the plugin log or `ck doctor`-style output: errors and warnings only, each
 * with its fix on the following line. Passing checks stay quiet so startup logs remain short.
 */
export function formatPreflight(result: PreflightResult): string {
  const errors = result.checks.filter((check) => !check.ok && check.severity === "error");
  const warnings = result.checks.filter((check) => !check.ok && check.severity === "warn");
  const lines: string[] = [];

  if (result.ok) {
    lines.push(
      warnings.length === 0
        ? `preflight ok (${plural(result.checks.length, "check")})`
        : `preflight ok with ${plural(warnings.length, "warning")} (${plural(result.checks.length, "check")})`,
    );
  } else {
    lines.push(
      `preflight failed: ${plural(errors.length, "error")}, ${plural(warnings.length, "warning")}`,
    );
  }

  // The role lines show where a mapped role's model comes from (14.8). With no `opencode.roles` every
  // role is on the alias map, so the block stays out of the passing report, like the other passing checks.
  // It prints before the problems so each role's line is the first line that names it.
  const roles = result.checks.find((check) => check.id === "roles" && check.ok);
  if (roles && roles.detail.includes("(source: roles)")) {
    lines.push("  roles (resolved model per role):");
    for (const line of roles.detail.split("\n")) lines.push(`    ${line}`);
  }

  for (const check of [...errors, ...warnings]) {
    lines.push(`  ${check.severity} ${check.id}: ${check.detail}`);
    if (check.fix) lines.push(`    fix: ${check.fix}`);
  }
  return lines.join("\n");
}

function checkNode(ctx: CheckContext): PreflightCheck {
  return safeCheck("node", "error", () => {
    const result = ctx.exec("node", ["--version"], { cwd: ctx.options.projectDir, env: ctx.env });
    if (result.status !== 0) {
      return {
        ok: false,
        detail: commandFailure("`node --version`", result),
        fix: "Install Node.js 20 or newer and make sure `node` is on PATH; the workflow runner spawns `node`, not bun or the opencode binary.",
      };
    }

    const output = combinedOutput(result);
    const version = parseSemver(output);
    if (!version) {
      return {
        ok: false,
        detail: `could not read a version from \`node --version\` output: ${quote(output)}`,
        fix: "Check that `node` on PATH is a real Node.js binary; the workflow runner needs Node.js 20 or newer.",
      };
    }
    if (version.major < MIN_NODE_MAJOR) {
      return {
        ok: false,
        detail: `node ${formatVersion(version)} is older than the required ${MIN_NODE_MAJOR}`,
        fix: `Install Node.js ${MIN_NODE_MAJOR} or newer and make sure that binary is first on PATH.`,
      };
    }
    return { ok: true, detail: `node ${formatVersion(version)} (>= ${MIN_NODE_MAJOR})` };
  });
}

function checkCkVersion(ctx: CheckContext): PreflightCheck {
  return safeCheck("ck", "error", () => {
    const label = `\`${ctx.ckBin} --version\``;
    const result = ctx.exec(ctx.ckBin, ["--version"], {
      cwd: ctx.options.projectDir,
      env: ctx.env,
    });
    if (result.status !== 0) {
      return { ok: false, detail: commandFailure(label, result), fix: CK_FIX };
    }

    const output = combinedOutput(result);
    const version = parseCkVersion(output);
    if (!version) {
      return {
        ok: false,
        detail: `could not read a version from ${label} output: ${quote(output)} (expected "ck, version X.Y.Z")`,
        fix: `Check the output of \`${ctx.ckBin} --version\`; if it is not a claude-kiro release, put the real ck CLI first on PATH. ${CK_FIX}.`,
      };
    }
    if (compareVersions(version, MIN_CK) < 0) {
      return {
        ok: false,
        detail: `ck ${formatVersion(version)} is older than the required ${MIN_CK_VERSION}`,
        fix: CK_FIX,
      };
    }
    return {
      ok: true,
      detail: `${ctx.ckBin} ${formatVersion(version)} (>= ${MIN_CK_VERSION})`,
    };
  });
}

function checkVendor(ctx: CheckContext): PreflightCheck {
  return safeCheck("vendor", "error", () => {
    const problems: string[] = [];
    let files = 0;

    for (const group of VENDOR_GROUPS) {
      const inspected = inspectVendorGroup(ctx.options.packageRoot, group);
      if (inspected.problem) problems.push(inspected.problem);
      files += inspected.files;
    }

    if (problems.length > 0) {
      return {
        ok: false,
        detail: `vendored sources missing or empty: ${problems.join("; ")}`,
        fix: "Run `npm run sync-vendor` from opencode/ during development, or reinstall opencode-claude-kiro; vendor/ ships in the npm package.",
      };
    }
    return {
      ok: true,
      detail: `vendor/ complete: ${VENDOR_GROUPS.length} groups, ${plural(files, "file")}`,
    };
  });
}

function checkModels(ctx: CheckContext): PreflightCheck[] {
  const { config, catalog } = ctx.options;
  const parsed: Array<{ alias: string; ref: ModelRef }> = [];
  const modelProblems: string[] = [];

  const entries = Object.entries(config.models).sort(([left], [right]) =>
    left.localeCompare(right),
  );
  for (const [alias, text] of entries) {
    try {
      parsed.push({ alias, ref: parseRef(text) });
    } catch {
      modelProblems.push(
        `models.${alias} = ${JSON.stringify(text)} is not a provider/model[#variant] reference`,
      );
    }
  }

  interface BaseEntry {
    ref: ModelRef;
    aliases: string[];
    model?: CatalogModel;
  }
  const bases = new Map<string, BaseEntry>();
  for (const { alias, ref } of parsed) {
    const key = baseRef(ref);
    const entry = bases.get(key);
    if (entry) entry.aliases.push(alias);
    else bases.set(key, { ref, aliases: [alias] });
  }

  if (catalog) {
    for (const [key, entry] of [...bases].sort(([left], [right]) => left.localeCompare(right))) {
      const model = catalog.find(
        (candidate) => candidate.providerID === entry.ref.providerID && candidate.id === entry.ref.id,
      );
      if (model) {
        entry.model = model;
        continue;
      }
      const nearest = nearestRefs(catalog, entry.ref.id);
      modelProblems.push(
        `${entry.aliases.map((alias) => `models.${alias}`).join(", ")} point at ${key}, which is not ` +
          `in the model catalog; nearest available: ${nearest.join(", ") || "none"}`,
      );
    }
  }

  const modelsCheck = safeCheck("models", "error", () => {
    if (modelProblems.length > 0) {
      return {
        ok: false,
        detail: modelProblems.join("; "),
        fix: "Point specs/ck.json models.<alias> at an installed model (run `opencode models` for the ids), or install that provider/model in OpenCode.",
      };
    }
    return {
      ok: true,
      detail: catalog
        ? `${plural(parsed.length, "configured model ref")} parse and exist in the catalog`
        : `${plural(parsed.length, "configured model ref")} parse (no catalog supplied; existence not checked)`,
    };
  });

  if (!catalog) return [modelsCheck];

  const variantProblems: string[] = [];
  for (const entry of bases.values()) {
    const model = entry.model;
    if (!model) continue;
    const key = baseRef(entry.ref);
    const available = variantList(model.variants);

    for (const { alias, ref } of parsed) {
      if (baseRef(ref) !== key || !ref.variant) continue;
      if (!model.variants.includes(ref.variant)) {
        variantProblems.push(
          `models.${alias} pins #${ref.variant}, which ${key} does not offer (${available})`,
        );
      }
    }

    const specific = config.efforts.models[key] ?? {};
    for (const [effort, variant] of Object.entries(specific).sort(([left], [right]) =>
      left.localeCompare(right),
    )) {
      if (!model.variants.includes(variant)) {
        variantProblems.push(
          `${key} maps effort ${effort} to #${variant}, which the model does not offer (${available})`,
        );
      }
    }

    for (const [effort, variant] of Object.entries(config.efforts.default).sort(
      ([left], [right]) => left.localeCompare(right),
    )) {
      if (effort in specific) continue;
      if (!model.variants.includes(variant)) {
        variantProblems.push(
          `${key} inherits the default effort ${effort} -> #${variant}, which the model does not offer (${available})`,
        );
      }
    }
  }

  const variantsCheck = safeCheck("model-variants", "warn", () => {
    if (variantProblems.length > 0) {
      return {
        ok: false,
        detail: variantProblems.join("; "),
        fix: "Remove the #variant or change specs/ck.json opencode.efforts so the effort resolves to an offered variant; OpenCode rejects an unknown variant when the child is created.",
      };
    }
    return { ok: true, detail: "all configured variants exist on their models" };
  });

  return [modelsCheck, variantsCheck];
}

/**
 * List every workflow role with the model it runs on and where that model came from (14.8): the
 * `opencode.roles` map, or the alias map when the role is unmapped (the workflow passes an alias per
 * call, so the concrete model is only known at call time). The detail has one line per role.
 */
function checkRoles(ctx: CheckContext): PreflightCheck {
  return safeCheck("roles", "error", () => {
    const configured = ctx.options.config.roles as Readonly<Record<string, string | undefined>>;
    const problems: string[] = [];
    for (const [role, value] of Object.entries(configured)) {
      if (value === undefined) continue;
      if (!isRoleName(role)) {
        problems.push(`unknown role ${JSON.stringify(role)} (known: ${ROLE_NAMES.join(", ")})`);
        continue;
      }
      const problem = validateRoleRef(value);
      if (problem) problems.push(`${role} = ${JSON.stringify(value)}: ${problem}`);
    }
    if (problems.length > 0) {
      return {
        ok: false,
        detail: problems.join("; "),
        fix: "Fix opencode.roles in specs/ck.json (or the global ck config) to use known role names and provider/model[#variant] references.",
      };
    }

    const lines = ROLE_NAMES.map((role) => {
      const resolved: RoleRef | null = resolveRoleRef(role, configured);
      const padded = role.padEnd(16);
      if (resolved) {
        const variant = resolved.variant ? `#${resolved.variant}` : "";
        return `${padded} ${resolved.provider}/${resolved.model}${variant} (source: roles)`;
      }
      return `${padded} via the alias map, resolved per call (source: alias)`;
    });
    return { ok: true, detail: lines.join("\n") };
  });
}

function checkAgent(ctx: CheckContext, id: string): PreflightCheck {
  return safeCheck(`agent.${id}`, "error", () => {
    const relative = `.opencode/agents/${id}.md`;
    const path = join(ctx.options.projectDir, ".opencode", "agents", `${id}.md`);

    let text: string;
    try {
      text = readFileSync(path, "utf8");
    } catch {
      return {
        ok: false,
        detail: `${relative} is missing; OpenCode accepts unknown agent ids silently, so a child would run with no output`,
        fix: "Restart OpenCode with the claude-kiro plugin enabled (setup materializes the flat role agents), or restore the file from the package's vendor/agents sources.",
      };
    }

    const { data } = parseFrontmatter(text);
    const mode = (data.mode ?? "").trim();
    const pinned = (data.model ?? "").trim();
    const problems: string[] = [];

    if (!mode) {
      problems.push(`${relative} has no mode: frontmatter (expected mode: subagent)`);
    } else if (mode !== "subagent") {
      problems.push(`${relative} has mode ${JSON.stringify(mode)}, expected "subagent"`);
    }

    if (problems.length > 0) {
      if (pinned) {
        problems.push(`it also pins model ${JSON.stringify(pinned)}; role agents should not pin one`);
      }
      return {
        ok: false,
        detail: problems.join("; "),
        fix: `Restore the generated frontmatter (\`mode: subagent\`) in ${relative}, or delete the file and restart OpenCode so setup regenerates it.`,
      };
    }

    if (pinned) {
      return {
        ok: false,
        severity: "warn",
        detail: `${relative} pins model ${JSON.stringify(pinned)}; role agents should not pin one (per-call models and specs/ck.json models.<alias> are the source of truth)`,
        fix: `Remove the \`model:\` line from ${relative}; set the model in specs/ck.json or let the workflow pass it per call.`,
      };
    }

    return { ok: true, detail: `${relative} present with mode: subagent` };
  });
}

function checkGuardHook(ctx: CheckContext): PreflightCheck {
  return safeCheck("guard.hook", "error", () => {
    const label = `\`${ctx.ckBin} --hook stop-verify --plugin-hook\``;
    const result = ctx.exec(ctx.ckBin, ["--hook", "stop-verify", "--plugin-hook"], {
      cwd: ctx.options.projectDir,
      input: "{}",
      env: ctx.env,
    });
    if (result.status !== 0) {
      return {
        ok: false,
        detail: `${commandFailure(label, result)}; the guard must not silently degrade (D12)`,
        fix: "Check that the hook runs in this project (`ck doctor`); if it fails there too, reinstall ck. A dead guard hook must halt a run, not disable the guard.",
      };
    }
    return { ok: true, detail: `${label} exited 0` };
  });
}

function checkGit(ctx: CheckContext): PreflightCheck {
  return safeCheck("git", "warn", () => {
    const result = ctx.exec("git", ["status", "--porcelain"], {
      cwd: ctx.options.projectDir,
      env: ctx.env,
    });
    if (result.status !== 0) {
      return {
        ok: false,
        detail: commandFailure("`git status --porcelain`", result),
        fix: "Run preflight in the project's git repository (or install git); a benchmark run needs a clean, known base commit.",
      };
    }

    const changes = result.stdout
      .split(/\r?\n/)
      .map((line) => line.trimEnd())
      .filter((line) => line.length > 0);
    if (changes.length === 0) return { ok: true, detail: "working tree clean" };

    const preview = changes.slice(0, 5).join(", ");
    const more = changes.length > 5 ? ` (+${changes.length - 5} more)` : "";
    return {
      ok: false,
      detail: `${plural(changes.length, "uncommitted change")}: ${preview}${more}`,
      fix: "Commit or stash the changes before a benchmark run; D12 keeps the target branch clean. Interactive runs may proceed at the caller's discretion.",
    };
  });
}

function inspectVendorGroup(
  packageRoot: string,
  group: string,
): { problem: string | null; files: number } {
  const relative = `vendor/${group}`;
  const directory = join(packageRoot, "vendor", group);

  let files: string[];
  try {
    if (!statSync(directory).isDirectory()) {
      return { problem: `${relative} is not a directory`, files: 0 };
    }
    files = walkFiles(directory);
  } catch {
    return { problem: `${relative} is missing or unreadable`, files: 0 };
  }

  if (files.length === 0) return { problem: `${relative} is empty`, files: 0 };

  const nonEmpty = files.filter((file) => statSync(file).size > 0).length;
  if (nonEmpty === 0) return { problem: `${relative} has no non-empty files`, files: files.length };
  return { problem: null, files: files.length };
}

function walkFiles(root: string): string[] {
  const files: string[] = [];
  for (const entry of readdirSync(root, { withFileTypes: true })) {
    const path = join(root, entry.name);
    if (entry.isDirectory()) {
      files.push(...walkFiles(path));
    } else if (entry.isFile()) {
      files.push(path);
    }
  }
  return files;
}

function spawnExec(
  command: string,
  args: string[],
  options: PreflightExecOptions,
): PreflightExecResult {
  const result = spawnSync(command, args, {
    cwd: options.cwd,
    env: options.env ?? process.env,
    input: options.input,
    encoding: "utf8",
    timeout: 60_000,
    stdio: ["pipe", "pipe", "pipe"],
  });
  const stderr = toText(result.stderr) || (result.error ? message(result.error) : "");
  return {
    status: result.status,
    stdout: toText(result.stdout),
    stderr,
  };
}

function safeCheck(
  id: string,
  severity: "error" | "warn",
  run: () => Outcome,
): PreflightCheck {
  try {
    const outcome = run();
    const check: PreflightCheck = {
      id,
      ok: outcome.ok,
      severity: outcome.severity ?? severity,
      detail: outcome.detail,
    };
    if (outcome.fix) check.fix = outcome.fix;
    return check;
  } catch (error) {
    return {
      id,
      ok: false,
      severity,
      detail: `preflight check failed unexpectedly: ${message(error)}`,
      fix: "Report this with the output of `ck --version` and `node --version`.",
    };
  }
}

function commandFailure(label: string, result: PreflightExecResult): string {
  const reason = clip(result.stderr.trim() || result.stdout.trim());
  if (result.status === null) {
    return `could not run ${label}: ${reason || "the process did not start"}`;
  }
  return `${label} exited ${result.status}: ${reason || "no output"}`;
}

function parseSemver(text: string): Version | null {
  const match = /(\d+)\.(\d+)\.(\d+)/.exec(text);
  if (!match) return null;
  return { major: Number(match[1]), minor: Number(match[2]), patch: Number(match[3]) };
}

function parseCkVersion(text: string): Version | null {
  const labeled = /\bck,\s*version\s+v?(\d+)\.(\d+)\.(\d+)/i.exec(text);
  if (labeled) {
    return {
      major: Number(labeled[1]),
      minor: Number(labeled[2]),
      patch: Number(labeled[3]),
    };
  }
  // A wrapper around ck may print a bare version; accept that, but not a number embedded in prose.
  const bare = /^\s*v?(\d+)\.(\d+)\.(\d+)\s*$/m.exec(text);
  if (bare) {
    return { major: Number(bare[1]), minor: Number(bare[2]), patch: Number(bare[3]) };
  }
  return null;
}

function compareVersions(left: Version, right: Version): number {
  return (
    left.major - right.major || left.minor - right.minor || left.patch - right.patch
  );
}

function formatVersion(version: Version): string {
  return `${version.major}.${version.minor}.${version.patch}`;
}

function variantList(variants: readonly string[]): string {
  return variants.length > 0 ? `available: ${variants.join(", ")}` : "the model has no variants";
}

function nearestRefs(catalog: Catalog, wanted: string): string[] {
  return catalog
    .map((model) => ({
      ref: `${model.providerID}/${model.id}`,
      score: commonPrefix(wanted, model.id),
    }))
    .sort((left, right) => right.score - left.score || left.ref.localeCompare(right.ref))
    .slice(0, 5)
    .map((entry) => entry.ref);
}

function commonPrefix(left: string, right: string): number {
  const length = Math.min(left.length, right.length);
  let index = 0;
  while (index < length && left[index] === right[index]) index += 1;
  return index;
}

function combinedOutput(result: PreflightExecResult): string {
  return `${result.stdout}\n${result.stderr}`.trim();
}

function toText(value: string | Buffer | null | undefined): string {
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return "";
  return value.toString("utf8");
}

function clip(text: string, limit = 300): string {
  const flattened = text.replace(/\s+/g, " ").trim();
  return flattened.length > limit ? `${flattened.slice(0, limit)}…` : flattened;
}

function quote(text: string): string {
  return JSON.stringify(clip(text));
}

function plural(count: number, singular: string): string {
  return `${count} ${singular}${count === 1 ? "" : "s"}`;
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
