import assert from "node:assert/strict";
import test from "node:test";
import {
  InvalidSchemaError,
  UnsupportedSchemaError,
  compile,
  formatIssues,
  validate,
} from "../src/schema.ts";
import { workflowSchemas } from "./workflow-schemas.ts";

test("accepts a valid nested object", () => {
  const schema = {
    type: "object",
    properties: {
      task: { type: "string" },
      status: { type: "string", enum: ["done", "in_progress", "blocked"] },
      commits: { type: "string" },
      worktree: {
        type: "object",
        properties: { state: { type: "string" }, detail: { type: "string" } },
        required: ["state", "detail"],
      },
    },
    required: ["task", "status"],
  };

  assert.deepEqual(
    validate(schema, {
      task: "3",
      status: "done",
      commits: "",
      worktree: { state: "MERGED", detail: "ok" },
    }),
    [],
  );
});

test("reports missing required fields by path", () => {
  const issues = validate(
    {
      type: "object",
      properties: { task: { type: "string" }, summary: { type: "string" } },
      required: ["task", "summary"],
    },
    { task: "3" },
  );

  assert.equal(issues.length, 1);
  assert.equal(issues[0]?.path, "$.summary");
  assert.match(issues[0]?.message ?? "", /required/);
});

test("supports type arrays including null", () => {
  const schema = {
    type: "object",
    properties: { checkGreen: { type: ["boolean", "null"] } },
    required: ["checkGreen"],
  };

  assert.deepEqual(validate(schema, { checkGreen: true }), []);
  assert.deepEqual(validate(schema, { checkGreen: null }), []);
  const issues = validate(schema, { checkGreen: "yes" });
  assert.equal(issues.length, 1);
  assert.equal(issues[0]?.path, "$.checkGreen");
  assert.match(issues[0]?.message ?? "", /boolean or null/);
});

test("validates arrays of objects and nested arrays", () => {
  const schema = {
    type: "object",
    properties: {
      cycles: { type: "array", items: { type: "array", items: { type: "string" } } },
      gates: {
        type: "array",
        items: {
          type: "object",
          properties: { tasks: { type: "array", items: { type: "string" } }, green: { type: "boolean" } },
          required: ["tasks", "green"],
        },
      },
    },
    required: ["cycles", "gates"],
  };

  assert.deepEqual(
    validate(schema, {
      cycles: [["1", "2"], []],
      gates: [{ tasks: ["1"], green: true }],
    }),
    [],
  );

  const issues = validate(schema, {
    cycles: [["1", 2]],
    gates: [{ tasks: "1", green: true }],
  });
  assert.deepEqual(
    issues.map((issue) => issue.path),
    ["$.cycles[0][1]", "$.gates[0].tasks"],
  );
});

test("additionalProperties: false rejects unknown keys", () => {
  const issues = validate(
    { type: "object", properties: { ok: { type: "boolean" } }, additionalProperties: false },
    { ok: true, surprise: 1 },
  );

  assert.equal(issues.length, 1);
  assert.equal(issues[0]?.path, "$.surprise");
  assert.match(issues[0]?.message ?? "", /not allowed/);
});

test("additionalProperties as a schema validates the values of a map", () => {
  const schema = {
    type: "object",
    properties: {
      deps: {
        type: "object",
        additionalProperties: { type: "array", items: { type: "string" } },
      },
    },
    required: ["deps"],
  };

  assert.deepEqual(validate(schema, { deps: { "2": ["1"], "3": [] } }), []);
  const issues = validate(schema, { deps: { "2": ["1", 7] } });
  assert.equal(issues.length, 1);
  assert.equal(issues[0]?.path, '$.deps["2"][1]');
});

test("enum compares JSON values", () => {
  const schema = { type: "string", enum: ["MERGED", "RED", "CONFLICT"] };
  assert.deepEqual(validate(schema, "RED"), []);
  const issues = validate(schema, "red");
  assert.equal(issues.length, 1);
  assert.match(issues[0]?.message ?? "", /must be one of/);
});

test("a wrong scalar type stops before properties", () => {
  const issues = validate(
    { type: "object", properties: { task: { type: "string" } }, required: ["task"] },
    "not an object",
  );
  assert.equal(issues.length, 1);
  assert.equal(issues[0]?.path, "$");
  assert.match(issues[0]?.message ?? "", /must be object/);
});

test("unknown keywords fail at compile time, with the nested path", () => {
  assert.throws(
    () =>
      compile({
        type: "object",
        properties: {
          findings: {
            type: "array",
            items: { type: "string", oneOf: [{ type: "string" }] },
          },
        },
      }),
    (error: unknown) => {
      assert.ok(error instanceof UnsupportedSchemaError);
      assert.equal(error.keyword, "oneOf");
      assert.equal(error.path, "$.findings.items");
      return true;
    },
  );
});

test("invalid type names fail at compile time", () => {
  assert.throws(
    () => compile({ type: "text" }),
    (error: unknown) => error instanceof InvalidSchemaError && /unknown type/.test(error.message),
  );
});

test("descriptions are allowed but must be strings", () => {
  assert.deepEqual(validate({ type: "string", description: "a task id" }, "3"), []);
  assert.throws(() => compile({ type: "string", description: 3 }), InvalidSchemaError);
});

test("every schema literal in the real workflows compiles", () => {
  const schemas = workflowSchemas();
  assert.ok(schemas.length >= 12, `expected at least 12 workflow schemas, found ${schemas.length}`);
  const names = new Set(schemas.map((entry) => entry.name));
  for (const wanted of ["WRITE_RESULT", "FINDINGS", "VERDICTS", "PLAN", "RESULT", "LAND", "REVIEW_SCHEMA", "VERIFY", "REPORT"]) {
    assert.ok(names.has(wanted), `workflow schema ${wanted} was not extracted`);
  }

  for (const entry of schemas) {
    assert.doesNotThrow(
      () => compile(entry.schema),
      `${entry.file}:${entry.line} ${entry.name} does not compile`,
    );
  }
});

test("the real checkGreen schema accepts null", () => {
  const schemas = workflowSchemas();
  const land = schemas.find((entry) => entry.name === "LAND");
  assert.ok(land, "LAND schema not found");
  const schema = land.schema as {
    properties: { tasks: { items: { properties: { checkGreen: unknown } } } };
  };
  const checkGreen = schema.properties.tasks.items.properties.checkGreen;
  const value = (green: unknown) => ({ tasks: [{ task: "1", state: "MERGED", checkGreen: green }] });

  assert.deepEqual(validate(land.schema, value(null)), []);
  assert.deepEqual(validate(land.schema, value(false)), []);
  assert.equal(validate(land.schema, value("yes")).length, 1);
});

test("formatIssues renders one line per issue", () => {
  assert.equal(formatIssues([]), "");
  assert.equal(
    formatIssues([
      { path: "$.task", message: "is required" },
      { path: '$.deps["2"][1]', message: "must be string" },
    ]),
    '$.task: is required\n$.deps["2"][1]: must be string',
  );
});
