import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { defaultConfig } from "../src/config.ts";
import type { Catalog } from "../src/models.ts";
import {
  EXPECTED_AGENT_IDS,
  VENDOR_GROUPS,
  formatPreflight,
  runPreflight,
  type PreflightCheck,
  type PreflightExec,
  type PreflightExecOptions,
  type PreflightExecResult,
  type PreflightOptions,
  type PreflightResult,
} from "../src/preflight.ts";

const CATALOG: Catalog = [
  { providerID: "opencode", id: "claude-haiku-5-5", variants: ["low", "medium", "high"] },
  { providerID: "opencode", id: "claude-sonnet-5-5", variants: ["low", "high", "max"] },
  { providerID: "opencode", id: "claude-opus-5-5", variants: ["low", "medium", "high", "max"] },
  { providerID: "opencode", id: "claude-fable-5-1", variants: ["low", "high"] },
];

interface Fixture {
  root: string;
  projectDir: string;
  packageRoot: string;
  cleanup: () => void;
}

function createFixture(): Fixture {
  const root = mkdtempSync(join(tmpdir(), "ck-preflight-"));
  const projectDir = join(root, "project");
  const packageRoot = join(root, "package");

  mkdirSync(projectDir, { recursive: true });
  for (const group of VENDOR_GROUPS) {
    const directory = join(packageRoot, "vendor", group);
    mkdirSync(directory, { recursive: true });
    writeFileSync(join(directory, "fixture.txt"), `${group}\n`);
  }
  // A nested file proves the check walks below the group root.
  mkdirSync(join(packageRoot, "vendor", "skills", "demo"), { recursive: true });
  writeFileSync(join(packageRoot, "vendor", "skills", "demo", "SKILL.md"), "# demo\n");

  mkdirSync(join(projectDir, ".opencode", "agents"), { recursive: true });
  for (const id of EXPECTED_AGENT_IDS) {
    writeAgent(projectDir, id, { mode: "subagent" });
  }

  return {
    root,
    projectDir,
    packageRoot,
    cleanup: () => rmSync(root, { recursive: true, force: true }),
  };
}

function writeAgent(
  projectDir: string,
  id: string,
  frontmatter: { mode?: string; model?: string; description?: string },
): void {
  const lines = ["---", `description: ${JSON.stringify(frontmatter.description ?? id)}`];
  if (frontmatter.mode !== undefined) lines.push(`mode: ${frontmatter.mode}`);
  if (frontmatter.model !== undefined) lines.push(`model: ${frontmatter.model}`);
  lines.push("---", "", "Agent body.", "");
  writeFileSync(join(projectDir, ".opencode", "agents", `${id}.md`), lines.join("\n"));
}

type Responder = PreflightExecResult | (() => PreflightExecResult);

interface FakeExec {
  exec: PreflightExec;
  calls: Array<{ command: string; args: string[]; options: PreflightExecOptions }>;
  called: (key: string) => boolean;
}

function fakeExec(overrides: Record<string, Responder> = {}): FakeExec {
  const calls: FakeExec["calls"] = [];
  const exec: PreflightExec = (command, args, options) => {
    calls.push({ command, args, options });
    const key = [command, ...args].join(" ");
    const override = overrides[key];
    if (override !== undefined) {
      return typeof override === "function" ? override() : override;
    }
    if (key === "node --version") return { status: 0, stdout: "v22.22.3\n", stderr: "" };
    if (key === "ck --version") return { status: 0, stdout: "ck, version 0.11.0\n", stderr: "" };
    if (key === "ck --hook stop-verify --plugin-hook") {
      return { status: 0, stdout: "", stderr: "" };
    }
    if (key === "git status --porcelain") return { status: 0, stdout: "", stderr: "" };
    return { status: 1, stdout: "", stderr: `unexpected command: ${key}` };
  };
  return {
    exec,
    calls,
    called: (key) => calls.some((call) => [call.command, ...call.args].join(" ") === key),
  };
}

function optionsFor(
  fixture: Fixture,
  exec: PreflightExec,
  overrides: Partial<PreflightOptions> = {},
): PreflightOptions {
  return {
    projectDir: fixture.projectDir,
    packageRoot: fixture.packageRoot,
    config: defaultConfig(),
    catalog: CATALOG,
    env: {},
    exec,
    ...overrides,
  };
}

function findCheck(result: PreflightResult, id: string): PreflightCheck {
  const check = result.checks.find((entry) => entry.id === id);
  assert.ok(check, `expected check ${id} in ${result.checks.map((entry) => entry.id).join(", ")}`);
  return check;
}

test("happy path: every check passes", async (t) => {
  const fixture = createFixture();
  t.after(fixture.cleanup);
  const fake = fakeExec();

  const result = await runPreflight(optionsFor(fixture, fake.exec));

  assert.equal(result.ok, true);
  assert.ok(result.checks.every((check) => check.ok));
  for (const id of ["node", "ck", "vendor", "models", "model-variants", "guard.hook", "git"]) {
    assert.equal(findCheck(result, id).ok, true, id);
  }
  for (const id of EXPECTED_AGENT_IDS) {
    assert.equal(findCheck(result, `agent.${id}`).ok, true, id);
  }

  assert.equal(fake.calls[0]?.command, "node", "the runner check must use node from PATH");
  const hookCall = fake.calls.find(
    (call) => call.command === "ck" && call.args[0] === "--hook",
  );
  assert.ok(hookCall);
  assert.equal(hookCall.options.input, "{}");
  assert.equal(hookCall.options.cwd, fixture.projectDir);
  for (const call of fake.calls) assert.deepEqual(call.options.env, {});

  assert.match(formatPreflight(result), /^preflight ok \(\d+ checks\)$/);
});

test("node older than 20 is an error", async (t) => {
  const fixture = createFixture();
  t.after(fixture.cleanup);
  const fake = fakeExec({
    "node --version": { status: 0, stdout: "v18.20.4\n", stderr: "" },
  });

  const result = await runPreflight(optionsFor(fixture, fake.exec));
  const check = findCheck(result, "node");

  assert.equal(result.ok, false);
  assert.equal(check.ok, false);
  assert.equal(check.severity, "error");
  assert.match(check.detail, /18\.20\.4/);
  assert.match(check.detail, /20/);
  assert.match(check.fix ?? "", /Node\.js 20/);
  assert.match(formatPreflight(result), /preflight failed: 1 error/);
  assert.match(formatPreflight(result), /fix: /);
});

test("a missing node is reported, not thrown", async (t) => {
  const fixture = createFixture();
  t.after(fixture.cleanup);
  const fake = fakeExec({
    "node --version": { status: null, stdout: "", stderr: "spawnSync node ENOENT" },
  });

  const result = await runPreflight(optionsFor(fixture, fake.exec));
  const check = findCheck(result, "node");

  assert.equal(result.ok, false);
  assert.equal(check.ok, false);
  assert.match(check.detail, /could not run `node --version`/);
  assert.match(check.detail, /ENOENT/);
});

test("a missing ck is an error", async (t) => {
  const fixture = createFixture();
  t.after(fixture.cleanup);
  const fake = fakeExec({
    "ck --version": { status: null, stdout: "", stderr: "spawnSync ck ENOENT" },
  });

  const result = await runPreflight(optionsFor(fixture, fake.exec));
  const check = findCheck(result, "ck");

  assert.equal(result.ok, false);
  assert.equal(check.ok, false);
  assert.equal(check.severity, "error");
  assert.match(check.detail, /could not run `ck --version`/);
  assert.match(check.fix ?? "", /uv tool install/);
});

test("an old ck is an error", async (t) => {
  const fixture = createFixture();
  t.after(fixture.cleanup);
  const fake = fakeExec({
    "ck --version": { status: 0, stdout: "ck, version 0.10.4\n", stderr: "" },
  });

  const result = await runPreflight(optionsFor(fixture, fake.exec));
  const check = findCheck(result, "ck");

  assert.equal(result.ok, false);
  assert.equal(check.ok, false);
  assert.match(check.detail, /0\.10\.4/);
  assert.match(check.detail, /0\.11\.0/);
});

test("an unreadable ck version is an error", async (t) => {
  const fixture = createFixture();
  t.after(fixture.cleanup);
  const fake = fakeExec({
    "ck --version": { status: 0, stdout: "built from a local checkout\n", stderr: "" },
  });

  const result = await runPreflight(optionsFor(fixture, fake.exec));
  const check = findCheck(result, "ck");

  assert.equal(check.ok, false);
  assert.match(check.detail, /could not read a version/);
});

test("a date in ck output is not mistaken for a version", async (t) => {
  const fixture = createFixture();
  t.after(fixture.cleanup);
  const fake = fakeExec({
    "ck --version": { status: 0, stdout: "built 2024.10.3\n", stderr: "" },
  });

  const result = await runPreflight(optionsFor(fixture, fake.exec));

  assert.equal(findCheck(result, "ck").ok, false);
});

test("a wrapper that prints a bare version is accepted", async (t) => {
  const fixture = createFixture();
  t.after(fixture.cleanup);
  const fake = fakeExec({
    "ck --version": { status: 0, stdout: "0.11.2\n", stderr: "" },
  });

  const result = await runPreflight(optionsFor(fixture, fake.exec));

  assert.equal(result.ok, true);
  assert.match(findCheck(result, "ck").detail, /0\.11\.2/);
});

test("missing and empty vendor groups are errors", async (t) => {
  const fixture = createFixture();
  t.after(fixture.cleanup);
  rmSync(join(fixture.packageRoot, "vendor", "workflows"), { recursive: true, force: true });
  writeFileSync(join(fixture.packageRoot, "vendor", "output-styles", "fixture.txt"), "");
  const fake = fakeExec();

  const result = await runPreflight(optionsFor(fixture, fake.exec));
  const check = findCheck(result, "vendor");

  assert.equal(result.ok, false);
  assert.equal(check.ok, false);
  assert.equal(check.severity, "error");
  assert.match(check.detail, /vendor\/workflows is missing/);
  assert.match(check.detail, /vendor\/output-styles has no non-empty files/);
  assert.match(check.fix ?? "", /sync-vendor/);
});

test("an unparseable model ref is an error", async (t) => {
  const fixture = createFixture();
  t.after(fixture.cleanup);
  const config = defaultConfig();
  config.models.sonnet = "sonnet";
  const fake = fakeExec();

  const result = await runPreflight(optionsFor(fixture, fake.exec, { config }));
  const check = findCheck(result, "models");

  assert.equal(result.ok, false);
  assert.equal(check.ok, false);
  assert.equal(check.severity, "error");
  assert.match(check.detail, /models\.sonnet/);
  assert.match(check.detail, /provider\/model/);
});

test("a model missing from the catalog is an error with a nearest suggestion", async (t) => {
  const fixture = createFixture();
  t.after(fixture.cleanup);
  const config = defaultConfig();
  config.models.sonnet = "opencode/claude-sonnet-9";
  const fake = fakeExec();

  const result = await runPreflight(optionsFor(fixture, fake.exec, { config }));
  const check = findCheck(result, "models");

  assert.equal(result.ok, false);
  assert.equal(check.ok, false);
  assert.match(check.detail, /not in the model catalog/);
  assert.match(check.detail, /nearest available: .*opencode\/claude-sonnet-5-5/);
});

test("opaque provider/model ids are accepted (D4)", async (t) => {
  const fixture = createFixture();
  t.after(fixture.cleanup);
  const config = defaultConfig();
  config.models.fable = "acme/opaque-model-7#fast";
  const catalog: Catalog = [
    ...CATALOG,
    { providerID: "acme", id: "opaque-model-7", variants: ["fast", "slow"] },
  ];
  const fake = fakeExec();

  const result = await runPreflight(optionsFor(fixture, fake.exec, { config, catalog }));

  assert.equal(result.ok, true);
  assert.equal(findCheck(result, "models").ok, true);
  assert.equal(findCheck(result, "model-variants").ok, true);
});

test("without a catalog only the model ref syntax is checked", async (t) => {
  const fixture = createFixture();
  t.after(fixture.cleanup);
  const fake = fakeExec();

  const result = await runPreflight(optionsFor(fixture, fake.exec, { catalog: undefined }));

  assert.equal(result.ok, true);
  assert.match(findCheck(result, "models").detail, /no catalog supplied/);
  assert.equal(
    result.checks.some((check) => check.id === "model-variants"),
    false,
  );
});

test("a configured #variant the model lacks is a warning", async (t) => {
  const fixture = createFixture();
  t.after(fixture.cleanup);
  const config = defaultConfig();
  config.models.sonnet = "opencode/claude-sonnet-5-5#turbo";
  const fake = fakeExec();

  const result = await runPreflight(optionsFor(fixture, fake.exec, { config }));
  const check = findCheck(result, "model-variants");

  assert.equal(result.ok, true, "a missing variant must not abort the run");
  assert.equal(check.ok, false);
  assert.equal(check.severity, "warn");
  assert.match(check.detail, /models\.sonnet pins #turbo/);
  assert.match(check.detail, /available: low, high, max/);
  assert.match(formatPreflight(result), /preflight ok with 1 warning/);
});

test("a configured effort variant the model lacks is a warning", async (t) => {
  const fixture = createFixture();
  t.after(fixture.cleanup);
  const config = defaultConfig();
  config.efforts.models["opencode/claude-sonnet-5-5"] = { medium: "medium" };
  config.efforts.default.xhigh = "ultra";
  const fake = fakeExec();

  const result = await runPreflight(optionsFor(fixture, fake.exec, { config }));
  const check = findCheck(result, "model-variants");

  assert.equal(result.ok, true);
  assert.equal(check.ok, false);
  assert.equal(check.severity, "warn");
  assert.match(check.detail, /maps effort medium to #medium/);
  assert.match(check.detail, /inherits the default effort xhigh -> #ultra/);
});

test("a missing agent file is an error", async (t) => {
  const fixture = createFixture();
  t.after(fixture.cleanup);
  rmSync(join(fixture.projectDir, ".opencode", "agents", "ck-reviewer.md"));
  const fake = fakeExec();

  const result = await runPreflight(optionsFor(fixture, fake.exec));
  const check = findCheck(result, "agent.ck-reviewer");

  assert.equal(result.ok, false);
  assert.equal(check.ok, false);
  assert.equal(check.severity, "error");
  assert.match(check.detail, /\.opencode\/agents\/ck-reviewer\.md is missing/);
  assert.match(check.fix ?? "", /Restart OpenCode/);
});

test("an agent with the wrong mode is an error", async (t) => {
  const fixture = createFixture();
  t.after(fixture.cleanup);
  writeAgent(fixture.projectDir, "ck-worker", { mode: "primary" });
  const fake = fakeExec();

  const result = await runPreflight(optionsFor(fixture, fake.exec));
  const check = findCheck(result, "agent.ck-worker");

  assert.equal(result.ok, false);
  assert.equal(check.ok, false);
  assert.equal(check.severity, "error");
  assert.match(check.detail, /mode "primary", expected "subagent"/);
});

test("an agent that pins a model is only a warning", async (t) => {
  const fixture = createFixture();
  t.after(fixture.cleanup);
  writeAgent(fixture.projectDir, "ck-fixer", {
    mode: "subagent",
    model: "opencode/claude-opus-5-5",
  });
  const fake = fakeExec();

  const result = await runPreflight(optionsFor(fixture, fake.exec));
  const check = findCheck(result, "agent.ck-fixer");

  assert.equal(result.ok, true, "a pinned model must not abort the run");
  assert.equal(check.ok, false);
  assert.equal(check.severity, "warn");
  assert.match(check.detail, /pins model "opencode\/claude-opus-5-5"/);
  assert.match(check.fix ?? "", /Remove the `model:` line/);
});

test("a dirty working tree is a warning", async (t) => {
  const fixture = createFixture();
  t.after(fixture.cleanup);
  const fake = fakeExec({
    "git status --porcelain": {
      status: 0,
      stdout: " M src/a.ts\n?? notes.txt\n",
      stderr: "",
    },
  });

  const result = await runPreflight(optionsFor(fixture, fake.exec));
  const check = findCheck(result, "git");

  assert.equal(result.ok, true, "a dirty tree is the caller's decision");
  assert.equal(check.ok, false);
  assert.equal(check.severity, "warn");
  assert.match(check.detail, /2 uncommitted changes/);
  assert.match(check.detail, /src\/a\.ts/);
  assert.match(check.detail, /notes\.txt/);
  assert.match(check.fix ?? "", /Commit or stash/);
});

test("a dead guard hook is an error", async (t) => {
  const fixture = createFixture();
  t.after(fixture.cleanup);
  const fake = fakeExec({
    "ck --hook stop-verify --plugin-hook": {
      status: 1,
      stdout: "",
      stderr: "hook exploded\n",
    },
  });

  const result = await runPreflight(optionsFor(fixture, fake.exec));
  const check = findCheck(result, "guard.hook");

  assert.equal(result.ok, false);
  assert.equal(check.ok, false);
  assert.equal(check.severity, "error");
  assert.match(check.detail, /stop-verify/);
  assert.match(check.detail, /exited 1/);
  assert.match(check.detail, /hook exploded/);
  assert.match(check.detail, /D12/);
});

test("guard.enabled=false skips the hook check", async (t) => {
  const fixture = createFixture();
  t.after(fixture.cleanup);
  const config = defaultConfig();
  config.guard.enabled = false;
  const fake = fakeExec();

  const result = await runPreflight(optionsFor(fixture, fake.exec, { config }));

  assert.equal(result.ok, true);
  assert.equal(
    fake.called("ck --hook stop-verify --plugin-hook"),
    false,
    "a disabled guard must not spawn the hook",
  );
  assert.equal(
    result.checks.some((check) => check.id === "guard.hook"),
    false,
  );
});

test("formatPreflight lists errors and warnings in that order, with fixes", async (t) => {
  const fixture = createFixture();
  t.after(fixture.cleanup);
  const fake = fakeExec({
    "node --version": { status: 0, stdout: "v18.20.4\n", stderr: "" },
    "git status --porcelain": { status: 0, stdout: " M src/a.ts\n", stderr: "" },
  });

  const result = await runPreflight(optionsFor(fixture, fake.exec));
  const text = formatPreflight(result);

  assert.match(text, /^preflight failed: 1 error, 1 warning/);
  assert.ok(text.indexOf("error node:") < text.indexOf("warn git:"));
  assert.match(text, /fix: /);
});

test("ck --version output may carry a build suffix", async (t) => {
  const fixture = createFixture();
  t.after(fixture.cleanup);
  const fake = fakeExec({
    "ck --version": { status: 0, stdout: "ck, version 0.11.10 (3cdc751)\n", stderr: "" },
  });

  const result = await runPreflight(optionsFor(fixture, fake.exec));

  assert.equal(result.ok, true);
  assert.match(findCheck(result, "ck").detail, /0\.11\.10/);
});
