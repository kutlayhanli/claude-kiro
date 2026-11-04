#!/usr/bin/env node
/**
 * The child half of the workflow bridge.
 *
 *   node runner.mjs <workflow.js> <args.json>
 *
 * The workflow sources are function bodies, not modules: they open with `export const meta = {…}`
 * and close with a top-level `return {…}`. This runner strips and evaluates `meta`, then compiles
 * the remainder with `AsyncFunction` and supplies the six workflow globals, plus `ck`: it runs the
 * ck CLI ($CK_BIN, default `ck`) with an argument list (never a shell) and resolves to
 * {code, stdout, stderr, json}. The workflows use it for their mechanical plan and land steps,
 * which in Claude Code's Workflow runtime (no exec) need an agent that copies the command's JSON.
 *
 * IPC, one object per message:
 *   child -> parent  {t:"ready", meta} {t:"agent", id, prompt, opts} {t:"phase", name}
 *                    {t:"log", text} {t:"done", result} {t:"error", message, stack}
 *   parent -> child  {t:"result", id, value} | {t:"result", id, error} | {t:"cancel"}
 *
 * When there is no IPC channel (running the runner by hand), the result is printed as JSON.
 */

import { execFile } from "node:child_process";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const [, , workflowPath, argsPath] = process.argv;
if (!workflowPath || !argsPath) {
  console.error("usage: runner.mjs <workflow.js> <args.json>");
  process.exit(2);
}

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const pending = new Map();
let sequence = 0;
let currentPhase = null;

process.on("message", (message) => {
  if (!message || typeof message !== "object") return;
  if (message.t === "result" && typeof message.id === "string") {
    const entry = pending.get(message.id);
    if (!entry) return;
    pending.delete(message.id);
    if (message.error !== undefined) entry.reject(new Error(String(message.error)));
    else entry.resolve(message.value);
    return;
  }
  if (message.t === "cancel") {
    for (const [id, entry] of pending) {
      pending.delete(id);
      entry.reject(new Error("workflow cancelled"));
    }
  }
});

function send(message) {
  if (typeof process.send !== "function") return Promise.resolve();
  return new Promise((resolve, reject) => {
    try {
      process.send(message, (error) => (error ? reject(error) : resolve()));
    } catch (error) {
      reject(error);
    }
  });
}

function agent(prompt, options = {}) {
  const id = `a${++sequence}`;
  let opts;
  try {
    opts = JSON.parse(JSON.stringify(options ?? {}));
  } catch (error) {
    return Promise.reject(new Error(`agent options are not serializable: ${message(error)}`));
  }
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    send({ t: "agent", id, prompt: String(prompt ?? ""), opts }).catch((error) => {
      pending.delete(id);
      reject(error);
    });
  });
}

const CK_TIMEOUT_MS = 4 * 60 * 60 * 1000; // a land step runs the gate: hours on a large suite

/** The JSON a `ck ... --json` command printed: the whole stdout, else its last top-level object. */
export function parseCkJson(stdout) {
  const text = String(stdout ?? "").trim();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    const start = text.indexOf("{");
    const end = text.lastIndexOf("}");
    if (start === -1 || end <= start) return null;
    try {
      return JSON.parse(text.slice(start, end + 1));
    } catch {
      return null;
    }
  }
}

export function ck(argv, options = {}) {
  const bin = process.env.CK_BIN || "ck";
  const args = Array.isArray(argv) ? argv.map(String) : [];
  return new Promise((resolve) => {
    execFile(
      bin,
      args,
      { cwd: options.cwd, env: process.env, maxBuffer: 64 * 1024 * 1024, timeout: options.timeoutMs ?? CK_TIMEOUT_MS },
      (error, stdout, stderr) => {
        const code = error ? (typeof error.code === "number" ? error.code : 1) : 0;
        const spawnError = error && typeof error.code === "string" ? `${error.code}: ${error.message}` : null;
        resolve({
          code,
          stdout: String(stdout ?? ""),
          stderr: (spawnError ? `${spawnError}\n` : "") + String(stderr ?? "").slice(-4000),
          json: parseCkJson(stdout),
        });
      },
    );
  });
}

function phase(name) {
  currentPhase = String(name ?? "");
  void send({ t: "phase", name: currentPhase }).catch(() => {});
}

function log(text) {
  void send({ t: "log", text: String(text ?? "") }).catch(() => {});
}

async function parallel(thunks) {
  return Promise.all((thunks ?? []).map((run) => Promise.resolve().then(() => run())));
}

async function pipeline(items, ...stages) {
  return Promise.all(
    (items ?? []).map(async (item, index) => {
      let value = item;
      for (const stage of stages) value = await stage(value, item, index);
      return value;
    }),
  );
}

/** Extract `export const meta = {…}` and return the body with the declaration blanked out. */
export function loadSource(source) {
  const match = /^\s*export\s+const\s+meta\s*=\s*\{/.exec(source);
  if (!match) return { meta: null, body: source };

  const open = match.index + match[0].lastIndexOf("{");
  const close = matchBrace(source, open);
  const literal = source.slice(open, close);
  const meta = Function(`"use strict"; return (${literal});`)();
  const removedLines = source.slice(match.index, close).split("\n").length - 1;
  const body = `${source.slice(0, match.index)}${"\n".repeat(removedLines)}${source.slice(close)}`;
  return { meta, body };
}

function matchBrace(source, open) {
  const stack = [{ kind: "code", depth: 0 }];
  for (let index = open; index < source.length; index += 1) {
    const frame = stack[stack.length - 1];
    const char = source[index];
    const next = source[index + 1];

    if (frame.kind === "template") {
      if (char === "\\") index += 1;
      else if (char === "`") stack.pop();
      else if (char === "$" && next === "{") {
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
      const newline = source.indexOf("\n", index);
      index = newline === -1 ? source.length : newline;
      continue;
    }
    if (char === "/" && next === "*") {
      const closeComment = source.indexOf("*/", index + 2);
      index = closeComment === -1 ? source.length : closeComment + 1;
      continue;
    }
    if (char === "{") {
      frame.depth += 1;
      continue;
    }
    if (char === "}") {
      if (stack.length === 1) {
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
  throw new Error(`unbalanced meta object literal at offset ${open}`);
}

function skipString(source, start, quote) {
  for (let index = start + 1; index < source.length; index += 1) {
    if (source[index] === "\\") index += 1;
    else if (source[index] === quote) return index;
  }
  return source.length;
}

function message(error) {
  return error instanceof Error ? error.message : String(error);
}

async function main() {
  const source = readFileSync(workflowPath, "utf8");
  const { meta, body } = loadSource(source);
  const args = JSON.parse(readFileSync(argsPath, "utf8"));

  await send({ t: "ready", meta });
  const run = new AsyncFunction("args", "agent", "phase", "parallel", "pipeline", "log", "ck", body);
  const result = await run(args, agent, phase, parallel, pipeline, log, ck);
  const value = result === undefined ? null : result;
  await send({ t: "done", result: value });
  if (typeof process.send !== "function") {
    process.stdout.write(`${JSON.stringify({ meta, result: value }, null, 2)}\n`);
  }
  return value;
}

const invokedDirectly =
  process.argv[1] !== undefined && resolve(process.argv[1]) === resolve(fileURLToPath(import.meta.url));
if (invokedDirectly) {
  main().then(
    () => process.exit(0),
    async (error) => {
      await send({
        t: "error",
        message: message(error),
        stack: error instanceof Error ? error.stack : undefined,
      }).catch(() => {});
      if (typeof process.send !== "function") console.error(error);
      process.exit(1);
    },
  );
}
