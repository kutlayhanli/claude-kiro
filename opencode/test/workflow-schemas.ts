/**
 * Extract the schema object literals from the real workflow sources.
 *
 * The point is coverage: if a future workflow grows a keyword the validator does not implement, the
 * test fails on the actual source, not on a hand-copied sample. The schemas are plain data, but some
 * descriptions interpolate constants (`${NOW}`), so they are evaluated with a proxy that answers any
 * free identifier with a placeholder. A free identifier naming an object constant defined earlier in
 * the same file (a shared field schema, e.g. `eta: ETA`) resolves to that constant's value.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import vm from "node:vm";

export interface WorkflowSchema {
  file: string;
  name: string;
  line: number;
  schema: unknown;
}

const WORKFLOWS = ["spec-create.js", "spec-implement.js"] as const;

export function workflowSchemas(): WorkflowSchema[] {
  const root = join(import.meta.dirname, "..", "..", "plugin", "workflows");
  const found: WorkflowSchema[] = [];
  for (const file of WORKFLOWS) {
    const source = readFileSync(join(root, file), "utf8");
    const known = new Map<string, unknown>();
    for (const candidate of candidates(source)) {
      let value: unknown;
      try {
        value = evaluate(candidate.literal, known);
      } catch (error) {
        throw new Error(`could not evaluate schema ${candidate.name} in ${file}:${candidate.line}: ${message(error)}`);
      }
      if (!candidate.name.startsWith("inline@")) known.set(candidate.name, value);
      if (!isSchemaObject(value)) continue;
      found.push({ file, name: candidate.name, line: candidate.line, schema: value });
    }
  }
  return found;
}

interface Candidate {
  name: string;
  line: number;
  literal: string;
}

function candidates(source: string): Candidate[] {
  const patterns = [
    /^const\s+([A-Z][A-Z0-9_]*)\s*=\s*\{/gm,
    /^[ \t]*schema\s*:\s*\{/gm,
  ];
  const found: Candidate[] = [];
  for (const pattern of patterns) {
    for (const match of source.matchAll(pattern)) {
      const open = (match.index ?? 0) + match[0].lastIndexOf("{");
      const close = matchBrace(source, open);
      const name = match[1] ?? `inline@${lineOf(source, open)}`;
      found.push({
        name,
        line: lineOf(source, open),
        literal: source.slice(open, close),
      });
    }
  }
  return found;
}

/**
 * The index one past the `}` matching the `{` at `open`, aware of strings, template literals
 * (including nested `${}`) and comments. Schema literals contain descriptions with braces in text.
 */
function matchBrace(source: string, open: number): number {
  type Frame = { kind: "code"; depth: number } | { kind: "template" };
  const stack: Frame[] = [{ kind: "code", depth: 0 }];

  for (let index = open; index < source.length; index += 1) {
    const frame = stack[stack.length - 1]!;
    const char = source[index]!;
    const next = source[index + 1];

    if (frame.kind === "template") {
      if (char === "\\") {
        index += 1;
      } else if (char === "`") {
        stack.pop();
      } else if (char === "$" && next === "{") {
        stack.push({ kind: "code", depth: 0 });
        index += 1;
      }
      continue;
    }

    if (char === "'" || char === '"') {
      index = skipString(source, index, char);
      continue;
    }
    if (char === "`") {
      stack.push({ kind: "template" });
      continue;
    }
    if (char === "/" && next === "/") {
      index = skipLineComment(source, index);
      continue;
    }
    if (char === "/" && next === "*") {
      index = skipBlockComment(source, index);
      continue;
    }
    if (char === "{") {
      frame.depth += 1;
      continue;
    }
    if (char === "}") {
      if (stack.length === 1) {
        // The root object's own `{` was counted, so depth 1 is its closing brace.
        if (frame.depth === 1) return index + 1;
        frame.depth -= 1;
        continue;
      }
      if (frame.depth === 0) {
        stack.pop();
        continue;
      }
      frame.depth -= 1;
    }
  }
  throw new Error(`unbalanced object literal starting at offset ${open}`);
}

function skipString(source: string, start: number, quote: string): number {
  for (let index = start + 1; index < source.length; index += 1) {
    const char = source[index];
    if (char === "\\") {
      index += 1;
    } else if (char === quote) {
      return index;
    }
  }
  return source.length;
}

function skipLineComment(source: string, start: number): number {
  const newline = source.indexOf("\n", start);
  return newline === -1 ? source.length : newline;
}

function skipBlockComment(source: string, start: number): number {
  const close = source.indexOf("*/", start + 2);
  return close === -1 ? source.length : close + 1;
}

function evaluate(literal: string, known: Map<string, unknown> = new Map()): unknown {
  const script = new vm.Script(`(function (__stub) { with (__stub) { return (${literal}); } })`);
  const wrapper = script.runInNewContext({}) as (stub: unknown) => unknown;
  const stub = new Proxy(Object.create(null), {
    has: () => true,
    get: (_target, key) => (key === Symbol.unscopables ? undefined : typeof key === "string" && known.has(key) ? known.get(key) : "stub"),
  });
  return wrapper(stub);
}

function isSchemaObject(value: unknown): boolean {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    ("type" in value || "properties" in value || "required" in value)
  );
}

function lineOf(source: string, offset: number): number {
  return source.slice(0, offset).split("\n").length;
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
