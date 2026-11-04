/**
 * Register the five `/spec/*` commands from the vendored SKILL.md files.
 *
 * Commands are registered at runtime, not copied into `.opencode/commands`: the skill text stays the
 * single source, and the raw argument string (`inv.prompt.text`) substitutes `$ARGUMENTS` directly.
 */

import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import type { Plugin } from "@opencode/plugin";
import type { CommandEditor } from "@opencode/plugin/promise/command";
import { parseFrontmatter, renderSkillBody, type RewriteOptions } from "./rewrite.ts";

export interface SkillCommand {
  /** Directory name under vendor/skills, e.g. "plan". */
  name: string;
  description: string;
  argumentHint: string | null;
  body: string;
}

export interface RegisterCommandsOptions {
  packageRoot: string;
  projectDir: string;
}

export interface RegisteredCommands {
  names: string[];
  dispose: () => Promise<void>;
}

export function loadSkills(packageRoot: string): SkillCommand[] {
  const directory = join(packageRoot, "vendor", "skills");
  let entries;
  try {
    entries = readdirSync(directory, { withFileTypes: true });
  } catch {
    throw new Error(`vendor/skills is missing under ${packageRoot}; run \`npm run sync-vendor\` from opencode/`);
  }

  const skills: SkillCommand[] = [];
  for (const entry of entries.filter((candidate) => candidate.isDirectory()).sort((a, b) => a.name.localeCompare(b.name))) {
    const path = join(directory, entry.name, "SKILL.md");
    const { data, body } = parseFrontmatter(readFileSync(path, "utf8"));
    if (!data.description) throw new Error(`${path}: missing description in frontmatter`);
    skills.push({
      name: entry.name,
      description: data.description,
      argumentHint: data["argument-hint"] ?? null,
      body,
    });
  }
  if (skills.length === 0) throw new Error(`${directory}: no skill directories found`);
  return skills;
}

export function commandDescription(skill: SkillCommand): string {
  return skill.argumentHint ? `${skill.description} — args: ${skill.argumentHint}` : skill.description;
}

export function renderCommand(skill: SkillCommand, args: string, options: RewriteOptions): string {
  return renderSkillBody(skill.body, args, options);
}

export async function registerCommands(
  ctx: Plugin.Context,
  options: RegisterCommandsOptions,
): Promise<RegisteredCommands> {
  const skills = loadSkills(options.packageRoot);
  const rewrite: RewriteOptions = {
    packageRoot: options.packageRoot,
    projectDir: options.projectDir,
  };

  const registration = await ctx.command.transform((editor: CommandEditor) => {
    for (const skill of skills) {
      editor.add({
        name: `spec/${skill.name}`,
        description: commandDescription(skill),
        execute: async (invocation) => {
          const text = renderCommand(skill, invocation.prompt.text ?? "", rewrite);
          await ctx.session.prompt({
            sessionID: invocation.sessionID,
            text,
            delivery: invocation.delivery,
          });
        },
      });
    }
  });

  return {
    names: skills.map((skill) => `spec/${skill.name}`),
    dispose: () => registration.dispose(),
  };
}
