/**
 * The per-call structured-result tool.
 *
 * Each `agent(..., { schema })` registers `submit_result_<seq>` just before its child session starts
 * and disposes it afterwards. The tool's input schema is the step's schema, so the child sees the
 * shape directly; invalid input comes back as field-level text and the child can correct itself.
 */

import type { Plugin } from "@opencode/plugin";
import type { ToolEditor } from "@opencode/plugin/promise/tool";
import { formatIssues, type Validator } from "./schema.ts";

export interface ResultTool {
  readonly name: string;
  readonly registration: { dispose: () => Promise<void> };
  /** The first valid submission, if the child made one. */
  accepted: () => { value: unknown } | null;
  /** How many submissions the validator rejected. */
  rejections: () => number;
}

export interface RegisterResultToolOptions {
  name: string;
  label: string;
  schema: unknown;
  validate: Validator;
}

export async function registerResultTool(
  ctx: Plugin.Context,
  options: RegisterResultToolOptions,
): Promise<ResultTool> {
  let accepted: unknown;
  let hasValue = false;
  let rejections = 0;

  const registration = await ctx.tool.transform((editor: ToolEditor) => {
    const definition: Parameters<ToolEditor["add"]>[0] = {
      name: options.name,
      description:
        `Submit the final structured result for "${options.label}" once. ` +
        "The input must match this step's result schema; the tool call is the deliverable.",
      input: options.schema as Parameters<ToolEditor["add"]>[0]["input"],
      execute: async (input: unknown) => {
        if (hasValue) {
          return { content: `A result was already accepted for "${options.label}". Stop now.` };
        }
        const issues = options.validate(input);
        if (issues.length > 0) {
          rejections += 1;
          return {
            content:
              `The result was rejected:\n${formatIssues(issues)}\n` +
              `Fix those fields and call ${options.name} again with the complete result.`,
          };
        }
        accepted = input;
        hasValue = true;
        return { content: `Result accepted for "${options.label}". Stop now; do not call ${options.name} again.` };
      },
    };
    editor.add(definition);
  });

  return {
    name: options.name,
    registration,
    accepted: () => (hasValue ? { value: accepted } : null),
    rejections: () => rejections,
  };
}
