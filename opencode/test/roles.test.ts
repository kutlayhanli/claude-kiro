import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

// Project modules are imported inside the test bodies so this file collects
// even before `opencode.roles` exists.

export const PROPERTIES = { "P-28": "tc180_p28 role references are accepted iff valid" };

const ROLES = ["implementer", "test_writer", "reviewer", "reviewer_risky", "fixer", "orchestrator", "resolver"];

interface Fixture {
  dir: string;
  global: string;
  project: string;
  env: { CK_CONFIG_HOME: string; XDG_CONFIG_HOME: string };
  writeGlobal(content: unknown): void;
  writeProject(content: unknown): void;
  cleanup(): void;
}

function fixture(): Fixture {
  const dir = mkdtempSync(join(tmpdir(), "ck-opencode-roles-"));
  const global = join(dir, "global");
  const project = join(dir, "project");
  mkdirSync(join(project, "specs"), { recursive: true });
  mkdirSync(global, { recursive: true });
  return {
    dir,
    global,
    project,
    env: { CK_CONFIG_HOME: global, XDG_CONFIG_HOME: join(dir, "xdg") },
    writeGlobal(content: unknown) {
      writeFileSync(join(global, "config.json"), JSON.stringify(content));
    },
    writeProject(content: unknown) {
      writeFileSync(join(project, "specs", "ck.json"), JSON.stringify(content));
    },
    cleanup() {
      rmSync(dir, { recursive: true, force: true });
    },
  };
}

const REVIEWER_REF = "opencode-go/deepseek-v4.1-flash#medium";

/** A fake OpenCode context with just enough session surface for one plain child. */
function fakeCtx(parentModel: { providerID: string; id: string }) {
  const sessions = new Map<string, Record<string, any>>();
  const created: Array<{ model: any; agent?: string }> = [];
  sessions.set("parent", {
    id: "parent",
    model: parentModel,
    tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } },
    cost: 0,
    time: { created: Date.now(), updated: Date.now() },
    outcome: "succeeded",
    messages: [],
  });
  const ctx = {
    location: { directory: "/tmp" },
    options: {},
    tool: {
      transform: async () => ({ dispose: async () => {} }),
      list: async () => [],
    },
    session: {
      create: async (input: { model: any; agent?: string }) => {
        created.push(input);
        const id = `child-${created.length}`;
        const child = {
          id,
          model: input.model,
          agent: input.agent,
          tokens: { input: 1, output: 1, reasoning: 0, cache: { read: 0, write: 0 } },
          cost: 0,
          time: { created: Date.now(), updated: Date.now() },
          messages: [] as unknown[],
        };
        sessions.set(id, child);
        return child;
      },
      get: async ({ sessionID }: { sessionID: string }) => sessions.get(sessionID),
      context: async ({ sessionID }: { sessionID: string }) => sessions.get(sessionID)?.messages ?? [],
      synthetic: async () => {},
      prompt: async ({ sessionID }: { sessionID: string }) => {
        const child = sessions.get(sessionID)!;
        child.time.busy = Date.now();
        setTimeout(() => {
          child.messages = [{ type: "assistant", content: [{ type: "text", text: "OK" }], finish: "stop" }];
          child.time.idle = Date.now();
          child.outcome = "succeeded";
        }, 15);
      },
      interrupt: async () => ({ interrupted: true }),
    },
  };
  return { ctx, created };
}

async function runOne(
  config: any,
  opts: { role?: string; model: string; effort?: string },
): Promise<{ outcome: any; created: Array<{ model: any }> }> {
  const { runChild } = await import("../src/run-child.ts");
  const { ctx, created } = fakeCtx({ providerID: "fake", id: "parent-model" });
  config.run.pollIntervalMs = 10;
  config.run.startTimeoutSeconds = 5;
  config.run.childTimeoutMinutes = 1;
  config.run.stateDir = join(tmpdir(), "ck-opencode-roles-state");
  const outcome = await runChild({
    ctx: ctx as never,
    parentSessionID: "parent",
    projectDir: "/tmp",
    config,
    catalog: [
      { providerID: "opencode-go", id: "deepseek-v4.1-flash", variants: ["low", "medium", "high"] },
      { providerID: "fake", id: "sonnet-model", variants: ["high"] },
    ],
    seq: 1,
    prompt: "do it",
    label: "review 1",
    ...opts,
  } as never);
  return { outcome, created };
}

test("tc126_ a mapped reviewer role beats the model alias", async () => {
  const f = fixture();
  try {
    f.writeProject({ opencode: { roles: { reviewer: REVIEWER_REF } } });
    const { loadConfig } = await import("../src/config.ts");
    const { resolveRoleRef } = await import("../src/models.ts");
    const { config } = loadConfig({ directory: f.project, env: f.env });
    assert.deepEqual(resolveRoleRef("reviewer", config.roles), {
      provider: "opencode-go",
      model: "deepseek-v4.1-flash",
      variant: "medium",
    });

    const { outcome, created } = await runOne(config, { role: "reviewer", model: "opus" });
    assert.equal(outcome.resolvedModel.providerID, "opencode-go");
    assert.equal(outcome.resolvedModel.id, "deepseek-v4.1-flash");
    assert.equal(outcome.resolvedVariant, "medium");
    assert.equal(outcome.modelSource, "roles");
    assert.equal(created.length, 1);
    assert.equal(created[0]!.model.providerID, "opencode-go");
    assert.equal(created[0]!.model.id, "deepseek-v4.1-flash");
    assert.equal(created[0]!.model.variant, "medium");
  } finally {
    f.cleanup();
  }
});

test("tc127_ an unmapped fixer role resolves through the alias map as before", async () => {
  const f = fixture();
  try {
    f.writeProject({ opencode: { roles: { reviewer: REVIEWER_REF }, models: { sonnet: "fake/sonnet-model" } } });
    const { loadConfig } = await import("../src/config.ts");
    const { resolveRoleRef } = await import("../src/models.ts");
    const { config } = loadConfig({ directory: f.project, env: f.env });
    assert.equal(resolveRoleRef("fixer", config.roles), null);

    const { outcome } = await runOne(config, { role: "fixer", model: "sonnet" });
    assert.equal(outcome.resolvedModel.providerID, "fake");
    assert.equal(outcome.resolvedModel.id, "sonnet-model");
    assert.equal(outcome.modelSource, "alias");

    // No role at all behaves the same.
    const bare = await runOne(config, { model: "sonnet" });
    assert.equal(bare.outcome.resolvedModel.id, "sonnet-model");
    assert.equal(bare.outcome.modelSource, "alias");
  } finally {
    f.cleanup();
  }
});

test("tc128_ the project role map beats the global one and the source names the project file", async () => {
  const f = fixture();
  try {
    f.writeGlobal({ opencode: { roles: { implementer: "opencode-go/global-model#low", fixer: "opencode-go/only-global" } } });
    f.writeProject({ opencode: { roles: { implementer: "opencode-go/project-model#high" } } });
    const { loadConfig } = await import("../src/config.ts");
    const { resolveRoleRef } = await import("../src/models.ts");
    const { config, sources, files } = loadConfig({ directory: f.project, env: f.env });
    assert.deepEqual(resolveRoleRef("implementer", config.roles), {
      provider: "opencode-go",
      model: "project-model",
      variant: "high",
    });
    assert.equal(sources["roles.implementer"], files.project);
    // A role only the global file maps still applies, and names the global file.
    assert.equal(resolveRoleRef("fixer", config.roles)?.model, "only-global");
    assert.equal(sources["roles.fixer"], files.global);
  } finally {
    f.cleanup();
  }
});

test("tc129_ an invalid role reference throws ConfigError and no child session is created", async () => {
  const f = fixture();
  try {
    const { loadConfig, ConfigError } = await import("../src/config.ts");
    const cases: Array<[string, unknown]> = [
      ["reviewer", "p/m#"],
      ["fixer", "nonsense"],
      ["reviewer-risky", "p/m"],
    ];
    let sessions = 0;
    for (const [role, value] of cases) {
      f.writeProject({ opencode: { roles: { [role]: value } } });
      assert.throws(
        () => loadConfig({ directory: f.project, env: f.env }),
        (error: unknown) => {
          assert.ok(error instanceof ConfigError);
          assert.ok(error.message.includes(`opencode.roles.${role}`), error.message);
          assert.ok(error.message.includes(String(value)), error.message);
          return true;
        },
      );
      // The plugin only builds the workflow runtime from a loaded config, so
      // a throwing load means no run starts and no child session exists.
      try {
        loadConfig({ directory: f.project, env: f.env });
        sessions += 1;
      } catch {
        // refused
      }
    }
    assert.equal(sessions, 0);
  } finally {
    f.cleanup();
  }
});

const P28_ROLES = [...ROLES, "Implementer", "test-writer", "reviewer-risky", " reviewer", ""];
const P28_VALUES: unknown[] = [
  "opencode-go/deepseek-v4.1-flash#medium",
  "opencode-go/muse-spark-1.3-contributor",
  "p/m#",
  "/m",
  "p/",
  "p",
  "p/m#a#b",
  " p/m ",
  "",
  42,
  null,
  {},
];
const VALID_VALUES = new Set<unknown>([
  "opencode-go/deepseek-v4.1-flash#medium",
  "opencode-go/muse-spark-1.3-contributor",
]);

test("tc180_p28 role references are accepted iff valid", async () => {
  const f = fixture();
  try {
    const { loadConfig, ConfigError } = await import("../src/config.ts");
    const { validateRoleRef, resolveRoleRef, ROLE_NAMES } = await import("../src/models.ts");
    assert.deepEqual([...ROLE_NAMES].sort(), [...ROLES].sort());

    for (const value of P28_VALUES) {
      const valid = VALID_VALUES.has(value);
      assert.equal(validateRoleRef(value) === null, valid, `validateRoleRef(${JSON.stringify(value)})`);
    }

    for (const role of P28_ROLES) {
      for (const value of P28_VALUES) {
        const accepted = ROLES.includes(role) && VALID_VALUES.has(value);
        const label = `role ${JSON.stringify(role)} value ${JSON.stringify(value)}`;
        f.writeProject({ opencode: { roles: { [role]: value } } });
        if (accepted) {
          const { config } = loadConfig({ directory: f.project, env: f.env });
          assert.notEqual(resolveRoleRef(role, config.roles), null, label);
        } else {
          assert.throws(
            () => loadConfig({ directory: f.project, env: f.env }),
            (error: unknown) => {
              assert.ok(error instanceof ConfigError, label);
              assert.ok(error.message.includes(`opencode.roles.${role}`), `${label}: ${error.message}`);
              const text = typeof value === "string" ? value : JSON.stringify(value);
              assert.ok(error.message.includes(text), `${label}: ${error.message}`);
              return true;
            },
            label,
          );
        }
      }
    }
  } finally {
    f.cleanup();
  }
});
