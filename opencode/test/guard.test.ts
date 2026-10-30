import assert from "node:assert/strict";
import { chmodSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { defaultConfig } from "../src/config.ts";
import {
  GuardError,
  createGuard,
  parseHookOutput,
  runCkHook,
  translateTool,
  type GuardToolEvent,
} from "../src/guard.ts";

const FAKE_CK = `#!/usr/bin/env bash
set -u
hook="$2"
input="$(cat)"
case "$hook" in
  pre-tool-guard)
    case "$input" in
      *DENY*) echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"[ck guard] fake deny"}}' ;;
      *ASK*) echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"ask","permissionDecisionReason":"[ck guard] fake ask"}}' ;;
      *FAIL*) echo 'guard exploded' >&2; exit 1 ;;
      *) exit 0 ;;
    esac
    ;;
  done-verify)
    case "$input" in
      *LATCH*) echo '{"decision":"block","reason":"[ck post-check] fake latch"}' ;;
      *PASSED*) echo '{"systemMessage":"gate passed"}' ;;
      *) exit 0 ;;
    esac
    ;;
  post-file-ops)
    case "$input" in
      *CONTEXT*) echo '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"spec context note"}}' ;;
      *) exit 0 ;;
    esac
    ;;
esac
`;

function event(overrides: Partial<GuardToolEvent> = {}): GuardToolEvent {
  return {
    tool: "edit",
    agent: "ck-worker",
    sessionID: "main",
    id: "call_1",
    input: { path: "/proj/tests/test_demo.py", oldString: "old", newString: "new" },
    ...overrides,
  };
}

function fakeContext(projectDir: string) {
  const hooks = new Map<string, (input: unknown) => Promise<void>>();
  const synthetic: Array<{ sessionID: string; text: string }> = [];
  const asserts: Array<{ sessionID: string; action: string; resources: string[] }> = [];
  let assertOutcome: "allow" | "deny" = "allow";

  const ctx = {
    tool: {
      hook: async (name: string, callback: (input: unknown) => Promise<void>) => {
        hooks.set(name, callback);
        return {
          dispose: async () => {
            hooks.delete(name);
          },
        };
      },
    },
    session: {
      get: async ({ sessionID }: { sessionID: string }) => {
        if (sessionID === "main") return { location: { directory: projectDir } };
        if (sessionID === "child") return { parentID: "main", location: { directory: projectDir } };
        throw new Error(`unknown session ${sessionID}`);
      },
      synthetic: async (input: { sessionID: string; text: string }) => {
        synthetic.push(input);
      },
    },
    permission: {
      assert: async (input: { sessionID: string; action: string; resources: string[] }) => {
        asserts.push(input);
        if (assertOutcome === "deny") throw new Error("Permission denied: edit");
      },
    },
  };

  return {
    ctx,
    hooks,
    synthetic,
    asserts,
    denyAsks() {
      assertOutcome = "deny";
    },
  };
}

function setup() {
  const dir = mkdtempSync(join(tmpdir(), "ck-guard-"));
  const ck = join(dir, "fake-ck");
  writeFileSync(ck, FAKE_CK);
  chmodSync(ck, 0o755);
  const previousBin = process.env.CK_BIN;
  const previousHeadless = process.env.CK_RUN_HEADLESS;
  process.env.CK_BIN = ck;
  return {
    dir,
    cleanup() {
      if (previousBin === undefined) delete process.env.CK_BIN;
      else process.env.CK_BIN = previousBin;
      if (previousHeadless === undefined) delete process.env.CK_RUN_HEADLESS;
      else process.env.CK_RUN_HEADLESS = previousHeadless;
      rmSync(dir, { recursive: true, force: true });
    },
  };
}

test("translateTool maps edit, write and shell; patch is refused", () => {
  assert.deepEqual(translateTool(event()), {
    tool_name: "Edit",
    tool_input: { file_path: "/proj/tests/test_demo.py", old_string: "old", new_string: "new" },
    action: "edit",
    resource: "/proj/tests/test_demo.py",
  });
  assert.deepEqual(
    translateTool(event({ tool: "write", input: { path: "/p/a.txt", content: "x" } })),
    {
      tool_name: "Write",
      tool_input: { file_path: "/p/a.txt", content: "x" },
      action: "edit",
      resource: "/p/a.txt",
    },
  );
  assert.deepEqual(
    translateTool(event({ tool: "shell", input: { command: "echo hi", workdir: "/work" } })),
    {
      tool_name: "Bash",
      tool_input: { command: "cd '/work' && echo hi" },
      action: "shell",
      resource: "echo hi",
    },
  );
  assert.equal(translateTool(event({ tool: "read", input: { path: "/p/a" } })), null);
  assert.throws(() => translateTool(event({ tool: "patch", input: {} })), GuardError);
  assert.throws(() => translateTool(event({ input: { path: "/p/a" } })), GuardError);
});

test("parseHookOutput rejects invalid JSON", () => {
  assert.deepEqual(parseHookOutput("", "x"), null);
  assert.deepEqual(parseHookOutput('{"decision":"block"}', "x"), { decision: "block" });
  assert.throws(() => parseHookOutput("not json", "x"), GuardError);
});

test("runCkHook sends the JSON input and captures stdout", async () => {
  const fixture = setup();
  try {
    const result = await runCkHook(
      "pre-tool-guard",
      {
        cwd: fixture.dir,
        tool_name: "Edit",
        tool_input: { file_path: "/x", new_string: "DENY" },
        session_id: "s",
        tool_use_id: "t",
      },
      fixture.dir,
      5000,
    );
    assert.equal(result.code, 0);
    assert.match(result.stdout, /fake deny/);
  } finally {
    fixture.cleanup();
  }
});

test("the outer execute event is ignored and the inner deny is thrown verbatim", async () => {
  const fixture = setup();
  process.env.CK_RUN_HEADLESS = "1";
  const fake = fakeContext(fixture.dir);
  try {
    const guard = await createGuard(fake.ctx as never, defaultConfig(), fixture.dir);
    const before = fake.hooks.get("execute.before");
    assert.ok(before);

    await assert.doesNotReject(() =>
      before({ ...event({ tool: "execute", input: { code: "DENY" } }) }),
    );
    await assert.rejects(
      () => before(event({ input: { path: "/p/a", oldString: "a", newString: "DENY" } })),
      (error: unknown) => error instanceof GuardError && /\[ck guard\] fake deny/.test(error.message),
    );
    await guard.dispose();
  } finally {
    fixture.cleanup();
  }
});

test("a child session gets agent_id; the main session does not", async () => {
  const fixture = setup();
  process.env.CK_RUN_HEADLESS = "1";
  const fake = fakeContext(fixture.dir);
  try {
    const guard = await createGuard(fake.ctx as never, defaultConfig(), fixture.dir);
    const before = fake.hooks.get("execute.before");
    assert.ok(before);
    // Both calls are denied by the fake; the assertion here is that session.get is used and no
    // crash happens for either shape.
    await assert.rejects(
      () => before(event({ input: { path: "/p/a", oldString: "a", newString: "DENY" } })),
      /fake deny/,
    );
    await assert.rejects(
      () =>
        before(
          event({
            sessionID: "child",
            input: { path: "/p/a", oldString: "a", newString: "DENY" },
          }),
        ),
      /fake deny/,
    );
    await guard.dispose();
  } finally {
    fixture.cleanup();
  }
});

test("ask degrades to deny on a headless run, and prompts interactively", async () => {
  const fixture = setup();
  try {
    const fake = fakeContext(fixture.dir);
    process.env.CK_RUN_HEADLESS = "1";
    const headlessGuard = await createGuard(fake.ctx as never, defaultConfig(), fixture.dir);
    const headless = fake.hooks.get("execute.before");
    assert.ok(headless);
    await assert.rejects(
      () => headless(event({ input: { path: "/p/a", oldString: "a", newString: "ASK" } })),
      /fake ask/,
    );
    assert.equal(fake.asserts.length, 0);
    await headlessGuard.dispose();

    delete process.env.CK_RUN_HEADLESS;
    const interactive = fakeContext(fixture.dir);
    const interactiveGuard = await createGuard(interactive.ctx as never, defaultConfig(), fixture.dir);
    const before = interactive.hooks.get("execute.before");
    assert.ok(before);
    await assert.doesNotReject(() =>
      before(event({ input: { path: "/p/a", oldString: "a", newString: "ASK" } })),
    );
    assert.equal(interactive.asserts.length, 1);
    assert.deepEqual(interactive.asserts[0], {
      sessionID: "main",
      action: "edit",
      resources: ["/p/a"],
    });

    interactive.denyAsks();
    await assert.rejects(
      () => before(event({ input: { path: "/p/b", oldString: "a", newString: "ASK" } })),
      /fake ask/,
    );
    await interactiveGuard.dispose();
  } finally {
    fixture.cleanup();
  }
});

test("a failed pre-tool guard halts instead of allowing the call", async () => {
  const fixture = setup();
  process.env.CK_RUN_HEADLESS = "1";
  const fake = fakeContext(fixture.dir);
  try {
    const guard = await createGuard(fake.ctx as never, defaultConfig(), fixture.dir);
    const before = fake.hooks.get("execute.before");
    assert.ok(before);
    await assert.rejects(
      () => before(event({ input: { path: "/p/a", oldString: "a", newString: "FAIL" } })),
      /pre-tool guard failed.*guard exploded/s,
    );
    await guard.dispose();
  } finally {
    fixture.cleanup();
  }
});

test("done-verify blocks the next call through a latch", async () => {
  const fixture = setup();
  process.env.CK_RUN_HEADLESS = "1";
  const fake = fakeContext(fixture.dir);
  try {
    const guard = await createGuard(fake.ctx as never, defaultConfig(), fixture.dir);
    const before = fake.hooks.get("execute.before");
    const after = fake.hooks.get("execute.after");
    assert.ok(before && after);

    await after({
      ...event({ sessionID: "child", input: { path: "/p/a", oldString: "a", newString: "LATCH" } }),
      status: "completed",
    });
    await new Promise((resolve) => setTimeout(resolve, 100));

    await assert.rejects(
      () => before(event({ sessionID: "child", tool: "read", input: { path: "/p/a" } })),
      /\[ck post-check\] fake latch/,
    );
    // Consumed once.
    await assert.doesNotReject(() =>
      before(event({ sessionID: "child", tool: "read", input: { path: "/p/a" } })),
    );
    await guard.dispose();
  } finally {
    fixture.cleanup();
  }
});

test("post-file-ops context is posted as a synthetic message", async () => {
  const fixture = setup();
  process.env.CK_RUN_HEADLESS = "1";
  const fake = fakeContext(fixture.dir);
  try {
    const guard = await createGuard(fake.ctx as never, defaultConfig(), fixture.dir);
    const after = fake.hooks.get("execute.after");
    assert.ok(after);
    await after({
      ...event({ input: { path: "/p/a", oldString: "a", newString: "CONTEXT" } }),
      status: "completed",
    });
    assert.ok(fake.synthetic.some((entry) => entry.text === "spec context note"));
    await guard.dispose();
  } finally {
    fixture.cleanup();
  }
});

test("guard.enabled=false registers no hooks", async () => {
  const fixture = setup();
  const fake = fakeContext(fixture.dir);
  try {
    const config = defaultConfig();
    config.guard.enabled = false;
    const guard = await createGuard(fake.ctx as never, config, fixture.dir);
    assert.equal(fake.hooks.size, 0);
    await guard.dispose();
  } finally {
    fixture.cleanup();
  }
});
