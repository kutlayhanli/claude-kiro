import assert from "node:assert/strict";
import test from "node:test";
import { parseFrontmatter, renderSkillBody, rewriteText } from "../src/rewrite.ts";

const OPTIONS = { packageRoot: "/pkg", projectDir: "/proj" };

test("frontmatter parses the skill keys and returns the body", () => {
  const { data, body } = parseFrontmatter(
    "---\ndescription: Do the thing\nargument-hint: [spec]\nallowed-tools: Read, Write\n---\n\nHello $ARGUMENTS\n",
  );
  assert.deepEqual(data, {
    description: "Do the thing",
    "argument-hint": "[spec]",
    "allowed-tools": "Read, Write",
  });
  assert.equal(body, "Hello $ARGUMENTS\n");
});

test("frontmatter leaves a file without a header alone", () => {
  assert.deepEqual(parseFrontmatter("body only"), { data: {}, body: "body only" });
});

test("commands lose the Claude namespace colon", () => {
  assert.equal(rewriteText("Run /spec:plan then /spec:implement auth.", OPTIONS), "Run /spec/plan then /spec/implement auth.");
});

test("role agent ids become flat ck-* ids", () => {
  assert.equal(
    rewriteText("use spec:implementer, spec:test-writer and spec:reviewer", OPTIONS),
    "use ck-implementer, ck-test-writer and ck-reviewer",
  );
});

test("plugin-root paths point at the package or the project resources", () => {
  const text = rewriteText(
    "${CLAUDE_PLUGIN_ROOT}/resources/without-workflow-tool.md " +
      "${CLAUDE_PLUGIN_ROOT}/resources/spec-implement-brief.md " +
      "${CLAUDE_PLUGIN_ROOT}/skills/implement/SKILL.md",
    OPTIONS,
  );
  assert.equal(
    text,
    "/proj/.opencode/ck/without-workflow-tool.md " +
      "/proj/.opencode/ck/spec-implement-brief.md " +
      "/pkg/vendor/skills/implement/SKILL.md",
  );
});

test("tool names follow the OpenCode vocabulary", () => {
  const text = rewriteText(
    "AskUserQuestion WebFetch WebSearch TodoWrite TaskCreate TaskUpdate SendMessage subagent_type",
    OPTIONS,
  );
  assert.match(text, /question/);
  assert.match(text, /webfetch/);
  assert.match(text, /websearch/);
  assert.doesNotMatch(text, /TodoWrite|TaskCreate|TaskUpdate|SendMessage|subagent_type/);
});

test("agent prose uses the subagent tool without mangling workflow names", () => {
  const text = rewriteText(
    "Use the Agent tool with an Agent call; background Agent and an Agent. The Workflow tool.",
    OPTIONS,
  );
  assert.equal(
    text,
    "Use the subagent tool with a subagent call; background subagent and a subagent. The workflow tool.",
  );
  assert.equal(rewriteText('name: "spec:spec-create"', OPTIONS), 'name: "spec:spec-create"');
});

test("the create vendor paragraph collapses to the plugin workflow name", () => {
  const input =
    "- The script, picked by the project's mode first: if `.claude/ck-manifest.json` records `\"mode\": \"vendor\"`, use `scriptPath`. Otherwise use `name: \"spec:spec-create\"`; only if no workflow by that name is found, fall back to that `scriptPath`. If the file is missing too, tell me to run `ck setup` (installs the plugin) and stop.";
  const text = rewriteText(input, OPTIONS);
  assert.doesNotMatch(text, /records|ck setup|scriptPath/);
  assert.match(text, /Use `name: "spec:spec-create"`/);
});

test("the implement vendor paragraph collapses and keeps 'same args'", () => {
  const input =
    "launch the Workflow tool with `args`, picking the script by the project's mode first: if `.claude/ck-manifest.json` records vendor, use `scriptPath`. Otherwise use `name: \"spec:spec-implement\"`, same args.";
  const text = rewriteText(input, { ...OPTIONS, workflowName: "spec:spec-implement" });
  assert.doesNotMatch(text, /records|scriptPath/);
  assert.match(text, /use `name: "spec:spec-implement"`/);
  assert.match(text, /same args\./);
});

test("renderSkillBody substitutes every $ARGUMENTS and trims", () => {
  const text = renderSkillBody("Plan $ARGUMENTS.\n\nRun /spec:plan $ARGUMENTS\n", "auth login", OPTIONS);
  assert.equal(text, "Plan auth login.\n\nRun /spec/plan auth login\n");
});
