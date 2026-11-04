import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import {
  ConfigError,
  DEFAULT_MODELS,
  defaultConfig,
  globalConfigPath,
  loadConfig,
  resolveStateDir,
  selectAgent,
} from "../src/config.ts";

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
  const dir = mkdtempSync(join(tmpdir(), "ck-opencode-config-"));
  const global = join(dir, "global");
  const project = join(dir, "project");
  mkdirSync(join(project, "specs"), { recursive: true });
  mkdirSync(global, { recursive: true });
  const env = { CK_CONFIG_HOME: global, XDG_CONFIG_HOME: join(dir, "xdg") };
  return {
    dir,
    global,
    project,
    env,
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

test("defaults are usable without any config file", () => {
  const f = fixture();
  try {
    const { config, sources, files } = loadConfig({ directory: f.project, env: f.env });
    assert.deepEqual(config.models, DEFAULT_MODELS);
    assert.equal(config.agents.default, "ck-worker");
    assert.deepEqual(config.agents.rules, [
      { match: "^(review|refute|cross-test)", agent: "ck-reviewer" },
    ]);
    assert.equal(config.guard.enabled, true);
    assert.equal(config.run.childTimeoutMinutes, 90);
    assert.equal(config.run.startTimeoutSeconds, 60);
    assert.equal(config.run.resultRepairAttempts, 1);
    assert.equal(config.run.stateDir, null);
    assert.equal(sources["models.sonnet"], "default");
    assert.equal(files.project, join(f.project, "specs", "ck.json"));
    assert.equal(globalConfigPath(f.env), join(f.global, "config.json"));
  } finally {
    f.cleanup();
  }
});

test("project config overrides defaults and records its source", () => {
  const f = fixture();
  try {
    f.writeProject({
      opencode: {
        models: { sonnet: "opencode/claude-sonnet-5-5#high" },
        efforts: { default: { high: "high" } },
        agents: { default: "ck-worker", rules: [{ match: "^fix", agent: "ck-fixer" }] },
        guard: { timeouts: { doneVerify: 120 } },
        run: { childTimeoutMinutes: 30, pollIntervalMs: 100 },
      },
    });
    const { config, sources } = loadConfig({ directory: f.project, env: f.env });
    assert.equal(config.models.sonnet, "opencode/claude-sonnet-5-5#high");
    assert.equal(config.efforts.default.high, "high");
    assert.deepEqual(config.agents.rules, [{ match: "^fix", agent: "ck-fixer" }]);
    assert.equal(config.guard.timeouts.doneVerify, 120);
    assert.equal(config.guard.timeouts.preToolGuard, 30);
    assert.equal(config.run.childTimeoutMinutes, 30);
    assert.equal(config.run.pollIntervalMs, 100);
    assert.equal(sources["models.sonnet"], join(f.project, "specs", "ck.json"));
    assert.equal(sources["guard.timeouts.preToolGuard"], "default");
  } finally {
    f.cleanup();
  }
});

test("precedence is default < global < project < plugin options", () => {
  const f = fixture();
  try {
    f.writeGlobal({
      opencode: {
        models: { sonnet: "opencode/global-sonnet" },
        run: { pollIntervalMs: 50, childTimeoutMinutes: 60 },
      },
    });
    f.writeProject({
      opencode: {
        models: { sonnet: "opencode/project-sonnet" },
        run: { childTimeoutMinutes: 45 },
      },
    });
    const { config, sources } = loadConfig({
      directory: f.project,
      env: f.env,
      options: { models: { sonnet: "opencode/option-sonnet" }, run: { childTimeoutMinutes: 20 } },
    });
    assert.equal(config.models.sonnet, "opencode/option-sonnet");
    assert.equal(config.run.pollIntervalMs, 50);
    assert.equal(config.run.childTimeoutMinutes, 20);
    assert.equal(sources["models.sonnet"], "options");
    assert.equal(sources["run.pollIntervalMs"], join(f.global, "config.json"));
    assert.equal(sources["run.childTimeoutMinutes"], "options");
  } finally {
    f.cleanup();
  }
});

test("invalid JSON names the file", () => {
  const f = fixture();
  try {
    writeFileSync(join(f.project, "specs", "ck.json"), "{ nope");
    assert.throws(
      () => loadConfig({ directory: f.project, env: f.env }),
      (error: unknown) => error instanceof ConfigError && error.message.includes("invalid JSON"),
    );
  } finally {
    f.cleanup();
  }
});

test("unknown keys fail loudly", () => {
  const f = fixture();
  try {
    f.writeProject({ opencode: { model: {} } });
    assert.throws(
      () => loadConfig({ directory: f.project, env: f.env }),
      (error: unknown) => error instanceof ConfigError && /unknown key "model"/.test(error.message),
    );
  } finally {
    f.cleanup();
  }
});

test("an unknown model alias is rejected", () => {
  const f = fixture();
  try {
    f.writeProject({ opencode: { models: { fast: "opencode/x" } } });
    assert.throws(
      () => loadConfig({ directory: f.project, env: f.env }),
      (error: unknown) => error instanceof ConfigError && /unknown model alias/.test(error.message),
    );
  } finally {
    f.cleanup();
  }
});

test("a malformed model reference is rejected", () => {
  const f = fixture();
  try {
    f.writeProject({ opencode: { models: { sonnet: "claude-sonnet-5-5" } } });
    assert.throws(
      () => loadConfig({ directory: f.project, env: f.env }),
      (error: unknown) => error instanceof ConfigError && /not a valid model reference/.test(error.message),
    );
  } finally {
    f.cleanup();
  }
});

test("nested agent ids are rejected because OpenCode ignores them", () => {
  const f = fixture();
  try {
    f.writeProject({ opencode: { agents: { default: "spec/worker" } } });
    assert.throws(
      () => loadConfig({ directory: f.project, env: f.env }),
      (error: unknown) =>
        error instanceof ConfigError && /flat agent id/.test(error.message) && /nested/.test(error.message),
    );
  } finally {
    f.cleanup();
  }
});

test("an invalid agent rule regex is rejected", () => {
  const f = fixture();
  try {
    f.writeProject({ opencode: { agents: { rules: [{ match: "(", agent: "ck-worker" }] } } });
    assert.throws(
      () => loadConfig({ directory: f.project, env: f.env }),
      (error: unknown) => error instanceof ConfigError && /not a valid regular expression/.test(error.message),
    );
  } finally {
    f.cleanup();
  }
});

test("an unknown effort key is rejected", () => {
  const f = fixture();
  try {
    f.writeProject({ opencode: { efforts: { default: { turbo: "high" } } } });
    assert.throws(
      () => loadConfig({ directory: f.project, env: f.env }),
      (error: unknown) => error instanceof ConfigError && /unknown effort "turbo"/.test(error.message),
    );
  } finally {
    f.cleanup();
  }
});

test("ck's own run.notify_command is the notification fallback", () => {
  const f = fixture();
  try {
    f.writeProject({ agents: { run: { notify_command: "notify-send ck" } } });
    const { config, sources } = loadConfig({ directory: f.project, env: f.env });
    assert.equal(config.run.notifyCommand, "notify-send ck");
    assert.equal(sources["run.notifyCommand"], join(f.project, "specs", "ck.json"));
  } finally {
    f.cleanup();
  }
});

test("opencode.run.notifyCommand wins over the ck fallback", () => {
  const f = fixture();
  try {
    f.writeProject({
      agents: { run: { notify_command: "notify-send ck" } },
      opencode: { run: { notifyCommand: "echo done" } },
    });
    const { config } = loadConfig({ directory: f.project, env: f.env });
    assert.equal(config.run.notifyCommand, "echo done");
  } finally {
    f.cleanup();
  }
});

test("run state lives outside the project unless configured", () => {
  const config = defaultConfig();
  assert.equal(
    resolveStateDir(config, { XDG_STATE_HOME: "/var/tmp/state" }),
    "/var/tmp/state/claude-kiro/opencode",
  );
  config.run.stateDir = "/var/tmp/ck-runs";
  assert.equal(resolveStateDir(config, { XDG_STATE_HOME: "/var/tmp/state" }), "/var/tmp/ck-runs");
});

test("selectAgent applies the default review mapping and falls through to the worker", () => {
  const mapping = defaultConfig().agents;
  assert.equal(selectAgent("review 3", mapping), "ck-reviewer");
  assert.equal(selectAgent("refute: grounding", mapping), "ck-reviewer");
  assert.equal(selectAgent("cross-test wave 2", mapping), "ck-reviewer");
  assert.equal(selectAgent("task 3 [test]", mapping), "ck-worker");
  assert.equal(selectAgent("fix 2", mapping), "ck-worker");
});

test("a custom rule overrides the default mapping", () => {
  const f = fixture();
  try {
    f.writeProject({
      opencode: { agents: { rules: [{ match: "^fix", agent: "ck-fixer" }] } },
    });
    const { config } = loadConfig({ directory: f.project, env: f.env });
    assert.equal(selectAgent("fix 2", config.agents), "ck-fixer");
    assert.equal(selectAgent("review 3", config.agents), "ck-worker");
  } finally {
    f.cleanup();
  }
});
