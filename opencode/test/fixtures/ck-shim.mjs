#!/usr/bin/env node
// A recording `$CK_BIN` for runner tests: appends {argv, cwd} as one JSON line to $CK_SHIM_LOG and
// prints canned JSON for the commands the spec-implement workflow runs directly. It is stateful only
// through that log: once a `worktree land` call is logged, `plan` reports task 1 done.
import { appendFileSync, existsSync, readFileSync } from "node:fs";

const argv = process.argv.slice(2);
const log = process.env.CK_SHIM_LOG;
let landed = false;
if (log) {
  if (existsSync(log)) {
    landed = readFileSync(log, "utf8")
      .split("\n")
      .filter(Boolean)
      .some((line) => JSON.parse(line).argv.includes("land"));
  }
  appendFileSync(log, `${JSON.stringify({ argv, cwd: process.cwd() })}\n`);
}

const has = (word) => argv.includes(word);
let out;
if (has("plan")) {
  out = landed
    ? { done: ["1"], remaining: [], deps: { 1: [] }, ready: [], cycles: [], verifyCycles: [] }
    : { done: [], remaining: ["1"], deps: { 1: [] }, ready: ["1"], cycles: [], verifyCycles: [] };
} else if (has("land")) {
  out = { tasks: [{ task: "1", state: "MERGED", detail: "merged" }], gates: [], bisected: false };
} else if (has("gate")) {
  out = { schema: "ck-gate/1", state: "green", green: true, regressions: [], excusedByTask: [], flaky: [] };
} else {
  out = { ok: true };
}
process.stdout.write(`${JSON.stringify(out)}\n`);
