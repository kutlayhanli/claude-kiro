/**
 * Materialize the five role agents plus the generic worker as flat `.opencode/agents/ck-*.md` files.
 *
 * OpenCode only discovers agents at `.opencode/agents/<name>.md`; nested directories are silently
 * ignored (spike 12). It also cannot see custom agents through `agent.list`, so these files are the
 * only delivery mechanism. Writes are digest-guarded: a file the user edited is never overwritten.
 */

import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { parseFrontmatter } from "./rewrite.ts";

export interface PermissionRule {
  action: string;
  resource: string;
  effect: "allow" | "deny" | "ask";
}

export interface AgentSpec {
  id: string;
  description: string;
  body: string;
  permissions: PermissionRule[];
}

export interface MaterializeResult {
  written: string[];
  unchanged: string[];
  conflicts: string[];
}

const REVIEWER_PERMISSIONS: PermissionRule[] = ["edit", "write", "patch", "apply_patch"].map((action) => ({
  action,
  resource: "*",
  effect: "deny",
}));

const WORKER_BODY = `You are a claude-kiro workflow worker. The prompt names one step of a spec run; follow it exactly, use the tools it needs, and return the structured result it asks for. Do not edit anything outside the files the prompt names.`;

const MANIFEST_NAME = "ck-agents.json";

export function agentSpecs(packageRoot: string): AgentSpec[] {
  const directory = join(packageRoot, "vendor", "agents");
  let files: string[];
  try {
    files = readdirSync(directory)
      .filter((file) => file.endsWith(".md"))
      .sort();
  } catch {
    throw new Error(`vendor/agents is missing under ${packageRoot}; run \`npm run sync-vendor\` from opencode/`);
  }

  const specs: AgentSpec[] = [];
  for (const file of files) {
    const path = join(directory, file);
    const { data, body } = parseFrontmatter(readFileSync(path, "utf8"));
    const name = data.name || file.slice(0, -3);
    if (!name) throw new Error(`${path}: missing name in frontmatter`);
    specs.push({
      id: `ck-${name}`,
      description: data.description || `claude-kiro ${name} agent`,
      body: body.trim(),
      // The source reviewer has Bash for `git diff` / `ck lint`; only the edit tools are removed.
      permissions: name === "reviewer" ? REVIEWER_PERMISSIONS : [],
    });
  }

  specs.push({
    id: "ck-worker",
    description: "claude-kiro workflow worker with full tools (default for workflow agent calls)",
    body: WORKER_BODY,
    permissions: [],
  });

  const seen = new Set<string>();
  for (const spec of specs) {
    if (seen.has(spec.id)) throw new Error(`duplicate agent id ${spec.id}`);
    seen.add(spec.id);
  }
  return specs.sort((a, b) => a.id.localeCompare(b.id));
}

export function renderAgent(spec: AgentSpec): string {
  const lines = [
    "---",
    `description: ${JSON.stringify(spec.description)}`,
    "mode: subagent",
  ];
  if (spec.permissions.length > 0) {
    lines.push("permissions:");
    for (const rule of spec.permissions) {
      lines.push(
        `  - action: ${JSON.stringify(rule.action)}`,
        `    resource: ${JSON.stringify(rule.resource)}`,
        `    effect: ${rule.effect}`,
      );
    }
  }
  lines.push("---", "", spec.body.trim(), "");
  return lines.join("\n");
}

export function materializeAgents(projectDir: string, packageRoot: string): MaterializeResult {
  const directory = join(projectDir, ".opencode", "agents");
  mkdirSync(directory, { recursive: true });
  const manifestPath = join(directory, MANIFEST_NAME);
  const manifest = readManifest(manifestPath);

  const result: MaterializeResult = { written: [], unchanged: [], conflicts: [] };
  for (const spec of agentSpecs(packageRoot)) {
    const file = `${spec.id}.md`;
    const path = join(directory, file);
    const content = renderAgent(spec);
    const digest = sha256(content);

    if (!existsSync(path)) {
      writeFileSync(path, content);
      manifest.files[file] = digest;
      result.written.push(file);
      continue;
    }

    const existing = readFileSync(path, "utf8");
    if (existing === content) {
      manifest.files[file] = digest;
      result.unchanged.push(file);
      continue;
    }

    const generated = manifest.files[file];
    if (generated && generated === sha256(existing)) {
      // An older generated version: safe to replace.
      writeFileSync(path, content);
      manifest.files[file] = digest;
      result.written.push(file);
      continue;
    }

    // The user edited it (or it predates the manifest): keep their file.
    result.conflicts.push(file);
  }

  writeManifest(manifestPath, manifest);
  return result;
}

interface AgentManifest {
  generatedBy: string;
  files: Record<string, string>;
}

function readManifest(path: string): AgentManifest {
  try {
    const parsed = JSON.parse(readFileSync(path, "utf8")) as Partial<AgentManifest>;
    if (parsed && typeof parsed.files === "object" && parsed.files !== null) {
      return { generatedBy: parsed.generatedBy ?? "opencode-claude-kiro", files: parsed.files };
    }
  } catch {
    // missing or unreadable: start fresh
  }
  return { generatedBy: "opencode-claude-kiro", files: {} };
}

function writeManifest(path: string, manifest: AgentManifest): void {
  writeFileSync(path, `${JSON.stringify(manifest, null, 2)}\n`);
}

function sha256(text: string): string {
  return createHash("sha256").update(text).digest("hex");
}
