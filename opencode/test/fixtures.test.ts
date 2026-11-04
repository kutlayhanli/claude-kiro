import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

interface HookEvent {
  tool: string;
  agent: string;
  sessionID: string;
  messageID: string;
  id: string;
  input: Record<string, unknown>;
  status?: string;
  result?: unknown;
  error?: { message: string };
}

interface Fixtures {
  before: {
    outerExecute: HookEvent;
    read: HookEvent;
    edit: HookEvent;
    shell: HookEvent;
    spikeEcho: HookEvent;
    spikeChild: HookEvent;
  };
  after: {
    outerExecute: HookEvent;
    read: HookEvent;
    shell: HookEvent;
    editDenied: HookEvent;
  };
  modelVisible: { deny: { error: { message: string } }; latch: { error: { message: string } } };
  latch: { armedIn: { id: string }; deliveredOnNextCall: HookEvent };
}

const fixtures = JSON.parse(
  readFileSync(join(import.meta.dirname, "fixtures", "tool-events.json"), "utf8"),
) as Fixtures;

test("outer execute and inner tool events share one call id", () => {
  // The captured pair for one call: the outer `execute` code string and the inner real tool.
  assert.equal(fixtures.after.outerExecute.id, fixtures.before.spikeChild.id);
  assert.equal(fixtures.after.outerExecute.tool, "execute");
  assert.equal(fixtures.before.spikeChild.tool, "spike_child");
  assert.deepEqual(Object.keys(fixtures.after.outerExecute.input), ["code"]);
  assert.deepEqual(Object.keys(fixtures.before.spikeChild.input), ["prompt"]);
});

test("every captured before event carries the fields the guard reads", () => {
  for (const [name, event] of Object.entries(fixtures.before) as [string, HookEvent][]) {
    assert.equal(typeof event.tool, "string", name);
    assert.equal(typeof event.agent, "string", name);
    assert.equal(typeof event.sessionID, "string", name);
    assert.equal(typeof event.id, "string", name);
    assert.equal(typeof event.input, "object", name);
  }
});

test("inner tool shapes are the camelCase OpenCode ones", () => {
  assert.deepEqual(Object.keys(fixtures.before.read.input), ["path"]);
  assert.deepEqual(Object.keys(fixtures.before.edit.input), ["path", "oldString", "newString"]);
  assert.deepEqual(Object.keys(fixtures.before.shell.input), ["command"]);
  assert.equal(fixtures.before.shell.input.command, "echo SHELL_OK");
});

test("a completed read carries a structured result", () => {
  const result = fixtures.after.read.result as {
    output: { type: string; uri: string };
    content: Array<{ type: string; text: string }>;
  };
  assert.equal(result.output.type, "file");
  assert.equal(result.output.uri, "file:///tmp/opencode/spike/hello.txt");
  assert.equal(result.content[0]?.type, "text");
});

test("a denied inner call is visible as an error with the guard text verbatim", () => {
  assert.equal(fixtures.after.editDenied.status, "error");
  assert.match(fixtures.after.editDenied.error?.message ?? "", /^\[ck guard\]/);
  assert.equal(fixtures.modelVisible.deny.error.message, fixtures.after.editDenied.error?.message);
});

test("the latch reason survives as the next call's error", () => {
  assert.match(fixtures.modelVisible.latch.error.message, /^\[ck post-check\]/);
  // Armed on one completed call, delivered on the next call of the same session.
  assert.notEqual(fixtures.latch.armedIn.id, fixtures.latch.deliveredOnNextCall.id);
  assert.equal(fixtures.latch.deliveredOnNextCall.sessionID, fixtures.before.outerExecute.sessionID);
});
