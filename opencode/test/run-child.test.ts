import assert from "node:assert/strict";
import test from "node:test";
import {
  failureOf,
  hasGuardStop,
  lastAssistantText,
  parseJsonCandidate,
  resultInstruction,
  resultToolName,
  retryPrompt,
  type MessageInfo,
  type SessionView,
} from "../src/run-child.ts";

const OK: SessionView = {
  outcome: "succeeded",
  tokens: { input: 1, output: 1, reasoning: 0, cache: { read: 0, write: 0 } },
  cost: 0,
  time: { created: 1, updated: 2, busy: 2, idle: 3 },
};

test("result tool names are unique per call", () => {
  assert.equal(resultToolName(1), "submit_result_1");
  assert.equal(resultToolName(42), "submit_result_42");
});

test("the result instruction names the tool and forbids prose JSON", () => {
  const text = resultInstruction("submit_result_7");
  assert.match(text, /submit_result_7/);
  assert.match(text, /exactly once/);
  assert.match(text, /Do not put the JSON in a prose answer/);
});

test("the retry prompt includes the original prompt, the reason, and the new tool", () => {
  const text = retryPrompt("Do the thing.", "submit_result_2", "the schema rejected ok");
  assert.match(text, /^Your previous attempt did not produce a valid structured result/);
  assert.match(text, /the schema rejected ok/);
  assert.match(text, /Do the thing\./);
  assert.match(text, /submit_result_2/);
});

test("lastAssistantText returns the last non-empty assistant text", () => {
  const messages: MessageInfo[] = [
    { type: "user", content: [{ type: "text", text: "go" }] },
    { type: "assistant", content: [{ type: "text", text: "first" }] },
    { type: "assistant", content: [{ type: "tool", tool: "read" }] },
    { type: "assistant", content: [{ type: "text", text: "final" }] },
    { type: "assistant", content: [{ type: "reasoning", text: "thinking" }] },
  ];
  assert.equal(lastAssistantText(messages), "final");
  assert.equal(lastAssistantText([]), "");
});

test("failureOf reads the last assistant error, finish, and session outcome", () => {
  assert.equal(failureOf([], OK), null);
  assert.equal(
    failureOf([{ type: "assistant", error: { message: "upstream 429" }, finish: "error" }], OK),
    "upstream 429",
  );
  assert.equal(
    failureOf([{ type: "assistant", finish: "error" }], OK),
    "the child's last model call failed",
  );
  assert.equal(
    failureOf([], { ...OK, outcome: "failed" }),
    "the child session failed without recording an error",
  );
  assert.equal(failureOf([], { ...OK, outcome: "interrupted" }), "the child session was interrupted");
});

test("hasGuardStop only accepts a non-null guardStop field", () => {
  assert.equal(hasGuardStop({ guardStop: { file: "tests/x.py" } }), true);
  assert.equal(hasGuardStop({ guardStop: "yes" }), true);
  assert.equal(hasGuardStop({ guardStop: null }), false);
  assert.equal(hasGuardStop({ task: "1" }), false);
  assert.equal(hasGuardStop("guardStop"), false);
  assert.equal(hasGuardStop(null), false);
});

test("parseJsonCandidate handles plain, fenced and prose-wrapped objects", () => {
  assert.deepEqual(parseJsonCandidate('{"ok":true}'), { ok: true });
  assert.deepEqual(parseJsonCandidate('Here is the result:\n```json\n{"ok":true}\n```'), { ok: true });
  assert.deepEqual(parseJsonCandidate('The answer is {"ok":true} — done.'), { ok: true });
  assert.deepEqual(parseJsonCandidate("[1, 2, 3]"), [1, 2, 3]);
  assert.equal(parseJsonCandidate("no json here"), undefined);
  assert.equal(parseJsonCandidate(""), undefined);
});

test("parseJsonCandidate prefers the parsed whole string over a substring", () => {
  assert.deepEqual(parseJsonCandidate('{"a":"b { not a brace }"}'), { a: "b { not a brace }" });
});
