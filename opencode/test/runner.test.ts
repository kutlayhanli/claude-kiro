import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

const RUNNER = join(import.meta.dirname, "..", "src", "runner.mjs");

interface Message {
  t: string;
  [key: string]: unknown;
}

interface RunOutcome {
  messages: Message[];
  result?: unknown;
  error?: { message: string; stack?: string };
  exitCode: number | null;
  stderr: string;
}

async function runWorkflow(
  source: string,
  args: Record<string, unknown>,
  answer: (prompt: string, opts: Record<string, unknown>) => unknown,
  env: Record<string, string> = {},
): Promise<RunOutcome> {
  const dir = mkdtempSync(join(tmpdir(), "ck-runner-"));
  const workflowPath = join(dir, "workflow.js");
  const argsPath = join(dir, "args.json");
  writeFileSync(workflowPath, source);
  writeFileSync(argsPath, JSON.stringify(args));

  try {
    return await new Promise<RunOutcome>((resolve, reject) => {
      const child = spawn(process.execPath, [RUNNER, workflowPath, argsPath], {
        env: { ...process.env, ...env },
        stdio: ["ignore", "ignore", "pipe", "ipc"],
      });
      const messages: Message[] = [];
      let stderr = "";
      let result: unknown;
      let error: { message: string; stack?: string } | undefined;
      let settled = false;
      const timer = setTimeout(() => {
        child.kill("SIGKILL");
        reject(new Error(`runner timed out; messages: ${JSON.stringify(messages)}`));
      }, 20000);

      child.stderr?.on("data", (chunk: Buffer) => {
        stderr += chunk.toString();
      });
      child.on("message", (raw) => {
        const message = raw as Message;
        messages.push(message);
        if (message.t === "agent") {
          const prompt = String(message.prompt ?? "");
          const opts = (message.opts ?? {}) as Record<string, unknown>;
          let value: unknown;
          try {
            value = answer(prompt, opts);
          } catch (failure) {
            child.send({ t: "result", id: message.id, error: String(failure) });
            return;
          }
          child.send({ t: "result", id: message.id, value });
          return;
        }
        if (message.t === "done") result = message.result;
        if (message.t === "error") error = message as unknown as { message: string; stack?: string };
        if ((message.t === "done" || message.t === "error") && !settled) {
          settled = true;
          clearTimeout(timer);
          child.once("exit", (code) => resolve({ messages, result, error, exitCode: code, stderr }));
        }
      });
      child.on("error", (failure) => {
        clearTimeout(timer);
        reject(failure);
      });
    });
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

test("runner compiles a workflow body, strips meta, and drives all helpers", async () => {
  const source = `export const meta = {
  name: 'fixture',
  description: 'runner fixture',
  whenToUse: 'test',
  phases: [{ title: 'P' }],
}

log('starting ' + JSON.stringify(args))
phase('P')
const one = await agent('one', { label: 'one', schema: { type: 'object' } })
const [two, three] = await parallel([
  () => agent('two'),
  () => agent('three'),
])
const four = await pipeline([1, 2], n => agent('p' + n), (r, n) => ({ r, n }))
return { one, two, three, four }
`;
  const outcome = await runWorkflow(source, { hello: "world" }, (prompt) => {
    if (prompt === "one") return { ok: true };
    return prompt.toUpperCase();
  });

  assert.equal(outcome.exitCode, 0, outcome.stderr);
  assert.equal(outcome.error, undefined);

  const ready = outcome.messages.find((message) => message.t === "ready");
  assert.deepEqual(ready?.meta, {
    name: "fixture",
    description: "runner fixture",
    whenToUse: "test",
    phases: [{ title: "P" }],
  });

  const logs = outcome.messages.filter((message) => message.t === "log").map((message) => message.text);
  assert.ok(logs.includes('starting {"hello":"world"}'), JSON.stringify(logs));
  assert.ok(outcome.messages.some((message) => message.t === "phase" && message.name === "P"));

  const calls = outcome.messages.filter((message) => message.t === "agent");
  assert.deepEqual(
    calls.map((message) => message.prompt),
    ["one", "two", "three", "p1", "p2"],
  );
  assert.deepEqual(calls[0]?.opts, { label: "one", schema: { type: "object" } });

  assert.deepEqual(outcome.result, {
    one: { ok: true },
    two: "TWO",
    three: "THREE",
    four: [
      { r: "P1", n: 1 },
      { r: "P2", n: 2 },
    ],
  });
});

test("runner supports an inline body with no meta and a top-level return", async () => {
  const source = `const answer = await agent('q')
return { answer, args }
`;
  const outcome = await runWorkflow(source, { inline: true }, () => "A");
  assert.equal(outcome.exitCode, 0, outcome.stderr);
  assert.equal(outcome.messages.find((message) => message.t === "ready")?.meta, null);
  assert.deepEqual(outcome.result, { answer: "A", args: { inline: true } });
});

test("runner reports a workflow failure with its message and stack", async () => {
  const source = `phase('Boom')
throw new Error('workflow exploded')
`;
  const outcome = await runWorkflow(source, {}, () => "unused");
  assert.equal(outcome.exitCode, 1);
  assert.equal(outcome.error?.message, "workflow exploded");
  assert.match(outcome.error?.stack ?? "", /workflow exploded/);
});

test("the real spec-create workflow compiles and reaches its first agent", async () => {
  const source = readFileSync(
    join(import.meta.dirname, "..", "..", "plugin", "workflows", "spec-create.js"),
    "utf8",
  );
  const outcome = await runWorkflow(
    source,
    {
      mode: "review",
      feature: "fixture",
      projectRoot: "/tmp/opencode/runner-fixture",
      specDir: "/tmp/opencode/runner-fixture/specs/fixture",
      description: "runner fixture",
      planPath: null,
      evolvesFrom: null,
      context: "",
      date: "2026-10-30",
    },
    () => {
      throw new Error("stop after the first agent");
    },
  );

  const ready = outcome.messages.find((message) => message.t === "ready");
  assert.equal((ready?.meta as { name?: string } | undefined)?.name, "spec-create");
  assert.ok(outcome.messages.some((message) => message.t === "agent"));
  assert.doesNotMatch(outcome.error?.message ?? "", /SyntaxError|Unexpected token|Unexpected identifier/);
});

test("the real spec-implement workflow compiles and reaches its first agent", async () => {
  const source = readFileSync(
    join(import.meta.dirname, "..", "..", "plugin", "workflows", "spec-implement.js"),
    "utf8",
  );
  const outcome = await runWorkflow(
    source,
    {
      spec: "fixture",
      root: "/tmp/opencode/runner-fixture",
      into: "main",
      targetDir: "/tmp/opencode/runner-fixture",
      // One real task: on the OpenCode runner plan, gate and report are direct ck calls, so only a task needs an agent.
      deps: { "1": [] },
      remaining: ["1"],
      wave: { "1": 1 },
      titles: { "1": "t" },
      tracks: { "1": "impl" },
      risk: {},
      complexity: {},
    },
    () => {
      throw new Error("stop after the first agent");
    },
  );

  const ready = outcome.messages.find((message) => message.t === "ready");
  assert.equal((ready?.meta as { name?: string } | undefined)?.name, "spec-implement");
  assert.ok(outcome.messages.some((message) => message.t === "agent"));
  assert.doesNotMatch(outcome.error?.message ?? "", /SyntaxError|Unexpected token|Unexpected identifier/);
});

test("runner rejects an agent call when the parent reports an error", async () => {
  const source = `try {
  await agent('fail')
  return 'not reached'
} catch (error) {
  return { caught: error.message }
}
`;
  const dir = mkdtempSync(join(tmpdir(), "ck-runner-"));
  const workflowPath = join(dir, "workflow.js");
  const argsPath = join(dir, "args.json");
  writeFileSync(workflowPath, source);
  writeFileSync(argsPath, "{}");
  try {
    const outcome = await new Promise<RunOutcome>((resolve, reject) => {
      const child = spawn(process.execPath, [RUNNER, workflowPath, argsPath], {
        stdio: ["ignore", "ignore", "pipe", "ipc"],
      });
      const messages: Message[] = [];
      let stderr = "";
      const timer = setTimeout(() => {
        child.kill("SIGKILL");
        reject(new Error("runner timed out"));
      }, 20000);
      child.stderr?.on("data", (chunk: Buffer) => {
        stderr += chunk.toString();
      });
      child.on("message", (raw) => {
        const message = raw as Message;
        messages.push(message);
        if (message.t === "agent") {
          child.send({ t: "result", id: message.id, error: "child failed on purpose" });
        }
        if (message.t === "done") {
          clearTimeout(timer);
          child.once("exit", (code) =>
            resolve({ messages, result: message.result, exitCode: code, stderr }),
          );
        }
      });
      child.on("error", reject);
    });
    assert.deepEqual(outcome.result, { caught: "child failed on purpose" });
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

/** A fake ck: prints {argv, cwd} as JSON (or a canned plan for `plan`), exits with $FAKE_CK_EXIT. */
function fakeCk(dir: string): string {
  const bin = join(dir, "fake-ck.mjs");
  writeFileSync(
    bin,
    `#!${process.execPath}\n` +
      `const argv = process.argv.slice(2);\n` +
      `if (argv.includes("plan")) { process.stdout.write(JSON.stringify({ done: [], remaining: [], ready: [], deps: {}, cycles: [], verifyCycles: [] })); process.exit(0); }\n` +
      `process.stderr.write("fake stderr\\n");\n` +
      `process.stdout.write("note: a log line first\\n" + JSON.stringify({ argv, cwd: process.cwd() }));\n` +
      `process.exit(Number(process.env.FAKE_CK_EXIT || 0));\n`,
    { mode: 0o755 },
  );
  return bin;
}

test("the ck global runs $CK_BIN with an argument list and parses the JSON it prints", async () => {
  const dir = mkdtempSync(join(tmpdir(), "ck-runner-ck-"));
  try {
    const bin = fakeCk(dir);
    const outcome = await runWorkflow(
      "const r = await ck(['worktree', 'land', 'demo', '1', '--json', 'a b; rm -rf /'], { cwd: args.dir })\nreturn r",
      { dir },
      () => null,
      { CK_BIN: bin, FAKE_CK_EXIT: "2" },
    );
    const r = outcome.result as { code: number; json: { argv: string[]; cwd: string }; stderr: string };
    assert.equal(r.code, 2);
    assert.deepEqual(r.json.argv, ["worktree", "land", "demo", "1", "--json", "a b; rm -rf /"], "one argument, never a shell");
    assert.equal(r.json.cwd, dir);
    assert.match(r.stderr, /fake stderr/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("the ck global reports a missing binary as a failed call with no JSON", async () => {
  const outcome = await runWorkflow("return await ck(['plan', 'x', '--json'])", {}, () => null, { CK_BIN: "/nonexistent/ck-binary" });
  const r = outcome.result as { code: number; json: unknown; stderr: string };
  assert.notEqual(r.code, 0);
  assert.equal(r.json, null);
  assert.match(r.stderr, /ENOENT/);
});

test("the real spec-implement workflow plans through ck directly, with no plan agent", async () => {
  const dir = mkdtempSync(join(tmpdir(), "ck-runner-direct-"));
  try {
    const bin = fakeCk(dir);
    const source = readFileSync(join(import.meta.dirname, "..", "..", "plugin", "workflows", "spec-implement.js"), "utf8");
    const labels: string[] = [];
    const outcome = await runWorkflow(
      source,
      { spec: "fixture", root: dir, into: "main", targetDir: dir, deps: {}, remaining: [], wave: {}, titles: {}, tracks: {}, risk: {}, complexity: {} },
      (_prompt, opts) => {
        labels.push(String(opts.label));
        return { merged: [], failed: [], blocked: [], unstarted: [], done: [], needsInput: [] };
      },
      { CK_BIN: bin },
    );
    assert.ok(!labels.some((l) => l.startsWith("plan")), `no plan agent: ${labels.join(", ")}`);
    const result = outcome.result as { directCk: { plan: number } };
    assert.ok(result.directCk.plan >= 1, JSON.stringify(result.directCk));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("tc164_runner_calls_the_attributed_gate_directly", async () => {
  const dir = mkdtempSync(join(tmpdir(), "ck-runner-shim-"));
  try {
    const shimLog = join(dir, "shim.jsonl");
    const source = readFileSync(join(import.meta.dirname, "..", "..", "plugin", "workflows", "spec-implement.js"), "utf8");
    const labels: string[] = [];
    const outcome = await runWorkflow(
      source,
      {
        spec: "fixture",
        root: dir,
        into: "main",
        targetDir: dir,
        deps: { "1": [] },
        remaining: ["1"],
        wave: {},
        titles: { "1": "one" },
        tracks: {},
        risk: {},
        complexity: {},
      },
      (_prompt, opts) => {
        labels.push(String(opts.label));
        const props = ((opts.schema ?? {}) as { properties?: Record<string, unknown> }).properties ?? {};
        if ("status" in props) {
          return { task: "1", status: "done", gatePassed: true, summary: "done", commits: "abc1234" };
        }
        if ("green" in props) return { green: true };
        return { ok: true };
      },
      { CK_BIN: join(import.meta.dirname, "fixtures", "ck-shim.mjs"), CK_SHIM_LOG: shimLog },
    );
    assert.equal(outcome.error, undefined, JSON.stringify(outcome.error));

    const calls = readFileSync(shimLog, "utf8")
      .split("\n")
      .filter(Boolean)
      .map((line) => JSON.parse(line) as { argv: string[] });
    assert.ok(calls.some((call) => call.argv.includes("land")), "the run landed through the shim");
    const gate = calls.find((call) => call.argv.includes("gate"));
    assert.ok(gate, `a direct ck gate call: ${calls.map((call) => call.argv.join(" ")).join(" | ")}`);
    assert.ok(gate.argv.includes("--attribute"), gate.argv.join(" "));
    assert.ok(gate.argv.includes("--json"), gate.argv.join(" "));
    assert.ok(!labels.some((label) => /gate/.test(label)), `no gate agent: ${labels.join(", ")}`);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
