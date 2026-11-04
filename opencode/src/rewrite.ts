/**
 * Claude Code prose -> OpenCode prose.
 *
 * The vendored skills and resources are the source of truth for both harnesses. This module applies
 * the harness-specific substitutions at registration/materialization time: command names, tool names,
 * agent ids, plugin-root paths, and the Claude-only vendor-mode paragraph.
 */

export const CLAUDE_PLUGIN_ROOT = "${CLAUDE_PLUGIN_ROOT}";

export interface RewriteOptions {
  packageRoot: string;
  projectDir: string;
  /** Which workflow this text launches; used to collapse the vendor-mode paragraph. */
  workflowName?: string;
}

export interface Frontmatter {
  data: Record<string, string>;
  body: string;
}

export function parseFrontmatter(markdown: string): Frontmatter {
  const text = markdown.replace(/^\uFEFF/, "");
  if (!text.startsWith("---\n")) return { data: {}, body: text };
  const end = text.indexOf("\n---", 4);
  if (end === -1) return { data: {}, body: text };

  const header = text.slice(4, end);
  const body = text.slice(end + 4).replace(/^(?:\r?\n)+/, "");
  const data: Record<string, string> = {};
  for (const line of header.split(/\r?\n/)) {
    const colon = line.indexOf(":");
    if (colon <= 0) continue;
    const key = line.slice(0, colon).trim();
    let value = line.slice(colon + 1).trim();
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    data[key] = value;
  }
  return { data, body };
}

export function rewriteText(text: string, options: RewriteOptions): string {
  const packageRoot = trimSlash(options.packageRoot);
  const projectDir = trimSlash(options.projectDir);
  const workflowName = options.workflowName ?? "spec:spec-create";
  let out = text;

  // The two resources the plugin materializes into the project, outside the worktree.
  out = out.replaceAll(
    `${CLAUDE_PLUGIN_ROOT}/resources/without-workflow-tool.md`,
    `${projectDir}/.opencode/ck/without-workflow-tool.md`,
  );
  out = out.replaceAll(
    `${CLAUDE_PLUGIN_ROOT}/resources/spec-implement-brief.md`,
    `${projectDir}/.opencode/ck/spec-implement-brief.md`,
  );
  out = out.replaceAll(CLAUDE_PLUGIN_ROOT, `${packageRoot}/vendor`);

  // Commands keep their slash but lose the Claude namespace colon.
  out = out.replaceAll("/spec:", "/spec/");

  // Role agents are flat ck-* ids on OpenCode.
  out = out.replace(/spec:(reviewer|implementer|test-writer|fixer|resolver)\b/g, "ck-$1");

  // Tool vocabulary.
  out = out.replaceAll("AskUserQuestion", "question");
  out = out.replaceAll("WebFetch", "webfetch");
  out = out.replaceAll("WebSearch", "websearch");
  out = out.replaceAll("(TaskCreate/TaskUpdate, or TodoWrite)", "a task-list item");
  out = out.replaceAll("TaskCreate", "the task list");
  out = out.replaceAll("TaskUpdate", "the task list");
  out = out.replaceAll("TodoWrite", "the task list");
  out = out.replaceAll("with SendMessage", "by continuing that agent's subagent session");
  out = out.replaceAll("if SendMessage isn't available", "if continuing that subagent session isn't available");
  out = out.replaceAll("SendMessage", "the subagent session");
  out = out.replaceAll("subagent_type", "agent");
  out = out.replace(/Bash\s+`run_in_background`/g, "the shell tool's background mode");

  // Workflow and Agent tool names.
  out = out.replaceAll("Workflow tool", "workflow tool");
  out = out.replaceAll("Workflow call", "workflow call");
  out = out.replaceAll("**Workflow**", "**workflow**");
  out = out.replace(/\bAgent tool\b/g, "subagent tool");
  out = out.replace(/\bAgent calls?\b/g, (match) => (match.endsWith("s") ? "subagent calls" : "subagent call"));
  out = out.replace(/\bAgent types?\b/g, (match) => (match.endsWith("s") ? "subagent types" : "subagent type"));
  out = out.replace(/\bbackground Agent\b/g, "background subagent");
  out = out.replace(/\bforeground Agent\b/g, "foreground subagent");
  out = out.replace(/\ban Agent\b/g, "a subagent");
  out = out.replace(/\bAgent\b/g, "subagent");
  out = out.replace(/\ban subagent\b/g, "a subagent");
  out = out.replace(/\bWorkflow\b/g, "workflow");

  // Project instructions and the Claude-only setup story.
  out = out.replaceAll("CLAUDE.md", "AGENTS.md");
  out = out.replaceAll("Claude Code's sandbox", "a sandbox");
  out = out.replaceAll("Claude Code", "OpenCode");
  out = collapseVendorParagraph(out, workflowName);
  return out;
}

export function renderSkillBody(body: string, args: string, options: RewriteOptions): string {
  const expanded = body.replaceAll("$ARGUMENTS", args);
  return `${rewriteText(expanded, options).trim()}\n`;
}

function collapseVendorParagraph(text: string, workflowName: string): string {
  const replacement =
    `Use \`name: "${workflowName}"\` (the workflow this plugin ships); ` +
    "do not probe `.claude/ck-manifest.json`.";

  if (workflowName === "spec:spec-implement") {
    return text.replace(
      /picking the script by the project's mode first:[\s\S]*?same args\./,
      `${lowerFirst(replacement)} same args.`,
    );
  }

  let out = text.replace(
    /The script, picked by the project's mode first:[\s\S]*?and stop\./g,
    replacement,
  );
  out = out.replace(
    /picking the script by the project's mode first:[\s\S]*?and stop\./g,
    replacement,
  );
  return out;
}

function trimSlash(value: string): string {
  return value.replace(/\/+$/, "");
}

function lowerFirst(value: string): string {
  return value.charAt(0).toLowerCase() + value.slice(1);
}
