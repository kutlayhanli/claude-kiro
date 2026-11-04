import assert from "node:assert/strict";
import test from "node:test";
import { join } from "node:path";
import { commandDescription, loadSkills, registerCommands, renderCommand } from "../src/commands.ts";

const PACKAGE_ROOT = join(import.meta.dirname, "..");
const PROJECT_DIR = "/tmp/ck-command-project";

test("the five vendored skills load with their argument hints", () => {
  const skills = loadSkills(PACKAGE_ROOT);
  assert.deepEqual(
    skills.map((skill) => skill.name).sort(),
    ["create", "implement", "plan", "review", "spawn-worktree"],
  );
  const plan = skills.find((skill) => skill.name === "plan");
  assert.ok(plan);
  assert.match(plan.description, /Discuss and decide/);
  assert.equal(plan.argumentHint, "[feature-description]");
  assert.match(commandDescription(plan), /args: \[feature-description\]/);
});

test("a rendered command substitutes arguments and rewrites the harness vocabulary", () => {
  const plan = loadSkills(PACKAGE_ROOT).find((skill) => skill.name === "plan");
  assert.ok(plan);
  const text = renderCommand(plan, "auth login", {
    packageRoot: PACKAGE_ROOT,
    projectDir: PROJECT_DIR,
  });

  assert.match(text, /Plan feature: auth login/);
  assert.doesNotMatch(text, /\$ARGUMENTS/);
  assert.doesNotMatch(text, /\$\{CLAUDE_PLUGIN_ROOT\}/);
  assert.doesNotMatch(text, /\/spec:/);
  assert.doesNotMatch(text, /AskUserQuestion|TodoWrite|subagent_type/);
  assert.match(text, /question/);
  assert.doesNotMatch(text, /records `"mode"`/);
});

test("commands are registered as spec/<name> and dispatch the rendered skill body", async () => {
  const added = new Map<string, { name: string; description?: string; execute: (input: unknown) => Promise<void> }>();
  const prompted: Array<{ sessionID: string; text: string; delivery: string }> = [];
  const ctx = {
    command: {
      transform: async (callback: (editor: { add: (definition: never) => void }) => void) => {
        callback({
          add: (definition: never) => {
            const typed = definition as never as {
              name: string;
              description?: string;
              execute: (input: unknown) => Promise<void>;
            };
            added.set(typed.name, typed);
          },
        });
        return {
          dispose: async () => {
            added.clear();
          },
        };
      },
    },
    session: {
      prompt: async (input: { sessionID: string; text: string; delivery: string }) => {
        prompted.push(input);
      },
    },
  };

  const commands = await registerCommands(ctx as never, {
    packageRoot: PACKAGE_ROOT,
    projectDir: PROJECT_DIR,
  });
  assert.deepEqual(commands.names.sort(), [
    "spec/create",
    "spec/implement",
    "spec/plan",
    "spec/review",
    "spec/spawn-worktree",
  ]);

  const implement = added.get("spec/implement");
  assert.ok(implement);
  await implement.execute({
    sessionID: "ses_1",
    prompt: { text: "auth 3 --no-review" },
    delivery: "steer",
  });
  assert.equal(prompted.length, 1);
  assert.equal(prompted[0]?.sessionID, "ses_1");
  assert.equal(prompted[0]?.delivery, "steer");
  assert.match(prompted[0]?.text ?? "", /Implement: auth 3 --no-review/);
  assert.match(prompted[0]?.text ?? "", /subagent tool/);
  assert.doesNotMatch(prompted[0]?.text ?? "", /spec:reviewer|CLAUDE_PLUGIN_ROOT/);

  await commands.dispose();
});
