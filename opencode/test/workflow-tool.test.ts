import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { defaultConfig, type CkConfig } from "../src/config.ts";
import { registerWorkflowTool, parseWorkflowInput, resolveWorkflowTarget } from "../src/workflow-tool.ts";

test("parseWorkflowInput requires exactly one target and an object args", () => {
  assert.deepEqual(parseWorkflowInput({ name: "spec:spec-create", args: { a: 1 } }), {
    name: "spec:spec-create",
    args: { a: 1 },
  });
  assert.deepEqual(parseWorkflowInput({ script: "return 1" }), { script: "return 1" });

  assert.throws(() => parseWorkflowInput({}), /exactly one/);
  assert.throws(() => parseWorkflowInput({ name: "x", script: "return 1" }), /exactly one/);
  assert.throws(() => parseWorkflowInput({ name: "" }), /non-empty/);
  assert.throws(() => parseWorkflowInput({ script: "return 1", args: [] }), /args must be an object/);
  assert.throws(() => parseWorkflowInput(null), /must be an object/);
});

test("resolveWorkflowTarget maps names and rejects unknown or traversal names", () => {
  const root = join(import.meta.dirname, "..");
  assert.deepEqual(resolveWorkflowTarget({ name: "spec:spec-create" }, root, root), {
    kind: "file",
    path: join(root, "vendor", "workflows", "spec-create.js"),
    name: "spec:spec-create",
  });
  assert.equal(
    resolveWorkflowTarget({ name: "spec-implement" }, root, root).name,
    "spec:spec-implement",
  );
  assert.throws(() => resolveWorkflowTarget({ name: "spec:nope" }, root, root), /unknown workflow/);
  assert.throws(
    () => resolveWorkflowTarget({ name: "../../etc/passwd" }, root, root),
    /invalid workflow name/,
  );
});

test("resolveWorkflowTarget honours scriptPath and inline scripts", () => {
  const root = join(import.meta.dirname, "..");
  assert.deepEqual(resolveWorkflowTarget({ script: "return 1" }, root, root), {
    kind: "inline",
    source: "return 1",
    name: "inline",
  });
  assert.throws(
    () => resolveWorkflowTarget({ scriptPath: "does-not-exist.js" }, root, root),
    /not found/,
  );
});

test("a bad submission is rejected, a refusal retries once, then the workflow sees the error", async () => {
  const dir = mkdtempSync(join(tmpdir(), "ck-workflow-tool-"));
  try {
    const fake = fakeRuntime(dir);
    const dispose = await registerWorkflowTool(fake.runtime);
    const tool = fake.tools.get("workflow");
    assert.ok(tool, "workflow tool missing");

    const script = `const fixed = await agent('invalid then valid', { label: 'task 1', schema: { type: 'object', properties: { ok: { type: 'boolean' } }, required: ['ok'] } })
let caught = null
try {
  await agent('never submit', { label: 'task 2', schema: { type: 'object', properties: { ok: { type: 'boolean' } }, required: ['ok'] } })
} catch (error) {
  caught = error.message
}
return { fixed, caught }
`;
    await tool.execute({ script, args: {} }, { sessionID: "parent" });
    await waitFor(() => fake.posted.some((text) => text.includes("finished.")));
    const final = fake.posted.find((text) => text.includes("finished.")) ?? "";
    assert.match(final, /"fixed":\s*\{\s*"ok":\s*true\s*\}/);
    assert.match(final, /"caught":\s*"the child finished without calling submit_result_2/);
    assert.equal(fake.children.length, 3, "expected the refused child to be retried exactly once");
    assert.ok(fake.toolReplies[0]?.includes("rejected"));
    assert.ok(fake.toolReplies[1]?.includes("accepted"));

    await dispose();
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("the workflow tool drives a real runner through a fake OpenCode session", async () => {
  const dir = mkdtempSync(join(tmpdir(), "ck-workflow-tool-"));
  try {
    const fake = fakeRuntime(dir);
    const dispose = await registerWorkflowTool(fake.runtime);

    const tool = fake.tools.get("workflow");
    assert.ok(tool, "workflow tool missing");
    const script = `phase('P')
const structured = await agent('structured step', { label: 'task 1', schema: { type: 'object', properties: { ok: { type: 'boolean' } }, required: ['ok'] } })
const plain = await agent('plain step', { label: 'review 1' })
return { structured, plain }
`;
    const started = await tool.execute({ script, args: {} }, { sessionID: "parent" });
    assert.match(String(started.content), /started in the background/);

    await waitFor(() => fake.posted.some((text) => text.includes("finished.")));
    const final = fake.posted.find((text) => text.includes("finished.")) ?? "";
    assert.match(final, /"structured":\s*\{\s*"ok":\s*true\s*\}/);
    assert.match(final, /"plain":\s*"PLAIN_OK"/);
    assert.ok(fake.posted.some((text) => text.includes("phase: P")));
    assert.equal(fake.children.length, 2);
    assert.ok(fake.calls.includes("submit_result_1"));
    assert.ok(!fake.tools.has("submit_result_1"), "the result tool was not disposed");

    const runDir = fake.runDir();
    assert.match(readFileSync(join(runDir, "result.json"), "utf8"), /PLAIN_OK/);
    assert.match(readFileSync(join(runDir, "run.log"), "utf8"), /workflow finished/);
    const record = JSON.parse(readFileSync(join(runDir, "record.json"), "utf8"));
    assert.equal(record.schema, "ck-opencode-run/1");
    assert.equal(record.harness, "opencode");
    assert.equal(record.workflow, "other");
    assert.equal(record.status, "done");
    assert.equal(record.agents.length, 2);
    assert.equal(record.agents[0].status, "ok");
    assert.equal(record.agents[1].status, "ok");
    assert.equal(record.agents[0].label, "task 1");
    assert.equal(record.agents[1].label, "review 1");
    assert.ok(record.agents[0].tokens.input > 0);
    assert.ok(record.agents[0].peakContext >= 0);
    const status = JSON.parse(readFileSync(join(dir, ".opencode", "ck", "status", `${record.runId}.json`), "utf8"));
    assert.equal(status.schema, "ck-opencode-status/1");
    assert.equal(status.status, "done");
    assert.equal(status.phase, "P");
    assert.equal(status.agents, 2);
    assert.deepEqual(status.running, []);
    const bench = JSON.parse(readFileSync(join(runDir, "bench-output.json"), "utf8"));
    assert.equal(bench.workflowProgress.length, 2);
    assert.equal(bench.workflowProgress[0].type, "workflow_agent");
    assert.equal(bench.workflowProgress[0].label, "task 1");
    assert.equal(bench.workflowProgress[0].state, "ok");
    assert.match(bench.workflowProgress[0].model, /fake\/model/);
    assert.ok(bench.durationMs >= 0);

    await dispose();
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

interface FakeChild {
  id: string;
  model: unknown;
  agent?: string;
  tokens: { input: number; output: number; reasoning: number; cache: { read: number; write: number } };
  cost: number;
  time: { created: number; updated: number; busy?: number; idle?: number };
  outcome?: "succeeded" | "failed" | "interrupted";
  messages: unknown[];
}

interface FakeTool {
  id: string;
  execute: (input: unknown, ctx?: unknown) => Promise<{ content?: string }>;
}

function fakeRuntime(dir: string) {
  const config: CkConfig = defaultConfig();
  config.agents.default = "general";
  config.agents.rules = [];
  config.models.sonnet = "fake/model";
  config.run.pollIntervalMs = 10;
  config.run.startTimeoutSeconds = 5;
  config.run.childTimeoutMinutes = 1;
  config.run.stateDir = join(dir, "state");

  const tools = new Map<string, FakeTool>();
  const sessions = new Map<string, FakeChild>();
  const posted: string[] = [];
  const calls: string[] = [];
  const toolReplies: string[] = [];
  const children: FakeChild[] = [];
  let childSeq = 0;

  const parent: FakeChild = {
    id: "parent",
    model: { providerID: "fake", id: "model" },
    tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } },
    cost: 0,
    time: { created: Date.now(), updated: Date.now() },
    outcome: "succeeded",
    messages: [],
  };
  sessions.set("parent", parent);

  const editor = {
    list: () => [...tools.values()],
    get: (id: string) => tools.get(id),
    add: (definition: { name: string; execute: FakeTool["execute"] }) => {
      tools.set(definition.name, { id: definition.name, execute: definition.execute });
    },
    update: () => {},
    remove: (id: string) => tools.delete(id),
    namespace: () => {},
  };

  const ctx = {
    location: { directory: dir },
    options: {},
    model: {
      list: async () => ({
        location: { directory: dir },
        data: [{ providerID: "fake", id: "model", variants: [{ id: "high" }] }],
      }),
    },
    tool: {
      transform: async (
        callback: (input: typeof editor) => void,
      ): Promise<{ dispose: () => Promise<void> }> => {
        const before = new Set(tools.keys());
        callback(editor);
        return {
          dispose: async () => {
            for (const name of tools.keys()) {
              if (!before.has(name)) tools.delete(name);
            }
          },
        };
      },
      list: async () => [...tools.values()],
    },
    session: {
      create: async (input: { model: unknown; agent?: string; title?: string }) => {
        childSeq += 1;
        const id = `child-${childSeq}`;
        const child: FakeChild = {
          id,
          model: input.model,
          agent: input.agent,
          tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } },
          cost: 0,
          time: { created: Date.now(), updated: Date.now() },
          messages: [],
        };
        sessions.set(id, child);
        children.push(child);
        return child;
      },
      get: async ({ sessionID }: { sessionID: string }) => {
        const session = sessions.get(sessionID);
        if (!session) throw new Error(`no session ${sessionID}`);
        return session;
      },
      context: async ({ sessionID }: { sessionID: string }) => {
        const session = sessions.get(sessionID);
        if (!session) throw new Error(`no session ${sessionID}`);
        return session.messages;
      },
      synthetic: async ({ sessionID, text }: { sessionID: string; text: string }) => {
        if (sessionID === "parent") {
          posted.push(text);
          return;
        }
        throw new Error(`unexpected synthetic message for ${sessionID}`);
      },
      prompt: async ({ sessionID, text }: { sessionID: string; text: string }) => {
        if (sessionID === "parent") {
          posted.push(text);
          return;
        }
        const child = sessions.get(sessionID);
        if (!child) throw new Error(`no child ${sessionID}`);
        child.time.busy = Date.now();
        const toolName = [...tools.keys()].find((name) => name.startsWith("submit_result_"));
        const promptText = text;
        setTimeout(() => {
          void (async () => {
            const tool = toolName ? tools.get(toolName) : undefined;
            const structured = promptText.includes("structured step");
            const invalidThenValid = promptText.includes("invalid then valid");
            const never = promptText.includes("never submit");
            if (tool && invalidThenValid) {
              calls.push(toolName!);
              const rejected = await tool.execute({ ok: "yes" });
              toolReplies.push(String(rejected.content ?? ""));
              calls.push(toolName!);
              const accepted = await tool.execute({ ok: true });
              toolReplies.push(String(accepted.content ?? ""));
              child.messages = [
                { type: "assistant", content: [{ type: "text", text: "submitted" }], finish: "stop" },
              ];
            } else if (tool && structured) {
              calls.push(toolName!);
              await tool.execute({ ok: true });
              child.messages = [
                { type: "assistant", content: [{ type: "text", text: "submitted" }], finish: "stop" },
              ];
            } else if (structured) {
              child.messages = [
                { type: "assistant", content: [{ type: "text", text: '{"ok":true}' }], finish: "stop" },
              ];
            } else if (never) {
              child.messages = [
                { type: "assistant", content: [{ type: "text", text: "I refuse to use tools." }], finish: "stop" },
              ];
            } else {
              child.messages = [
                { type: "assistant", content: [{ type: "text", text: "PLAIN_OK" }], finish: "stop" },
              ];
            }
            child.tokens = { input: 100, output: 10, reasoning: 0, cache: { read: 0, write: 0 } };
            child.time.idle = Date.now();
            child.outcome = "succeeded";
          })();
        }, 20);
      },
      interrupt: async () => ({ interrupted: true }),
    },
  };

  return {
    runtime: {
      ctx: ctx as never,
      config,
      catalog: [{ providerID: "fake", id: "model", variants: ["high"] }],
      packageRoot: join(import.meta.dirname, ".."),
      projectDir: dir,
    },
    tools,
    posted,
    calls,
    toolReplies,
    children,
    runDir(): string {
      const runs = join(dir, "state", "runs");
      return join(runs, readdirSync(runs)[0]!);
    },
  };
}

async function waitFor(predicate: () => boolean, timeoutMs = 5000): Promise<void> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  throw new Error(`timed out after ${timeoutMs}ms`);
}
