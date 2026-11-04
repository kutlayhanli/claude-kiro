/**
 * The JSON Schema subset the spec workflows use.
 *
 * The workflows' `schema:` options only ever use `type`, `properties`, `required`, `items`, `enum`,
 * `additionalProperties` and `description`. This validator implements exactly that subset and
 * refuses anything else at compile time: a future workflow schema that grows a keyword must fail
 * loudly here instead of silently accepting a malformed result.
 *
 * Supported types: "string", "number", "integer", "boolean", "object", "array", "null".
 * `type` may be a string or an array of strings (e.g. `["boolean", "null"]`).
 */

export interface SchemaIssue {
  /** JSON-pointer-ish path into the value, rooted at `$`. */
  path: string;
  message: string;
}

export class UnsupportedSchemaError extends Error {
  readonly keyword: string;
  readonly path: string;

  constructor(keyword: string, path: string) {
    super(`unsupported JSON Schema keyword ${JSON.stringify(keyword)} at ${path}`);
    this.name = "UnsupportedSchemaError";
    this.keyword = keyword;
    this.path = path;
  }
}

export class InvalidSchemaError extends Error {
  readonly path: string;

  constructor(message: string, path: string) {
    super(`invalid schema at ${path}: ${message}`);
    this.name = "InvalidSchemaError";
    this.path = path;
  }
}

type SchemaObject = Record<string, unknown>;
type Schema = boolean | SchemaObject;

const KEYWORDS = new Set([
  "type",
  "properties",
  "required",
  "items",
  "enum",
  "additionalProperties",
  "description",
]);

const TYPES = new Set([
  "string",
  "number",
  "integer",
  "boolean",
  "object",
  "array",
  "null",
]);

export type Validator = (value: unknown) => SchemaIssue[];

/** Compile a schema once. Throws `UnsupportedSchemaError` / `InvalidSchemaError` on a bad schema. */
export function compile(schema: unknown): Validator {
  assertSchema(schema, "$");
  return (value: unknown) => {
    const issues: SchemaIssue[] = [];
    check(schema as Schema, value, "$", issues);
    return issues;
  };
}

/** Convenience wrapper around `compile` for one-off checks. */
export function validate(schema: unknown, value: unknown): SchemaIssue[] {
  return compile(schema)(value);
}

/** One line per issue, suitable for a tool rejection or a thrown error. */
export function formatIssues(issues: readonly SchemaIssue[]): string {
  return issues.map((issue) => `${issue.path}: ${issue.message}`).join("\n");
}

function assertSchema(schema: unknown, path: string): asserts schema is Schema {
  if (typeof schema === "boolean") return;
  if (!isPlainObject(schema)) {
    throw new InvalidSchemaError("expected an object or a boolean", path);
  }

  for (const keyword of Object.keys(schema)) {
    if (!KEYWORDS.has(keyword)) throw new UnsupportedSchemaError(keyword, path);
  }

  if ("description" in schema && typeof schema.description !== "string") {
    throw new InvalidSchemaError('"description" must be a string', path);
  }

  if ("type" in schema) {
    const type = schema.type;
    const types = Array.isArray(type) ? type : [type];
    if (types.length === 0) throw new InvalidSchemaError('"type" must not be empty', path);
    for (const entry of types) {
      if (typeof entry !== "string" || !TYPES.has(entry)) {
        throw new InvalidSchemaError(`unknown type ${JSON.stringify(entry)}`, path);
      }
    }
  }

  if ("properties" in schema) {
    if (!isPlainObject(schema.properties)) {
      throw new InvalidSchemaError('"properties" must be an object', path);
    }
    for (const [key, child] of Object.entries(schema.properties)) {
      assertSchema(child, childPath(path, key));
    }
  }

  if ("required" in schema) {
    if (!Array.isArray(schema.required) || schema.required.some((key) => typeof key !== "string")) {
      throw new InvalidSchemaError('"required" must be an array of strings', path);
    }
  }

  if ("items" in schema) {
    assertSchema(schema.items, `${path}.items`);
  }

  if ("enum" in schema) {
    if (!Array.isArray(schema.enum) || schema.enum.length === 0) {
      throw new InvalidSchemaError('"enum" must be a non-empty array', path);
    }
  }

  if ("additionalProperties" in schema) {
    const extra = schema.additionalProperties;
    if (typeof extra !== "boolean") assertSchema(extra, `${path}.additionalProperties`);
  }
}

function check(schema: Schema, value: unknown, path: string, issues: SchemaIssue[]): void {
  if (schema === true) return;
  if (schema === false) {
    issues.push({ path, message: "is not allowed" });
    return;
  }

  if ("type" in schema && !matchesType(value, schema.type)) {
    issues.push({ path, message: `must be ${describeTypes(schema.type)}` });
    return;
  }

  if ("enum" in schema) {
    const allowed = schema.enum as unknown[];
    if (!allowed.some((entry) => jsonEqual(entry, value))) {
      issues.push({
        path,
        message: `must be one of ${allowed.map((entry) => JSON.stringify(entry)).join(", ")}`,
      });
    }
  }

  if (isPlainObject(value)) {
    const properties = isPlainObject(schema.properties) ? schema.properties : {};
    if ("required" in schema) {
      for (const key of schema.required as string[]) {
        if (value[key] === undefined) {
          issues.push({ path: childPath(path, key), message: "is required" });
        }
      }
    }
    for (const [key, child] of Object.entries(properties)) {
      if (value[key] !== undefined) check(child as Schema, value[key], childPath(path, key), issues);
    }
    if ("additionalProperties" in schema) {
      const extra = schema.additionalProperties as Schema;
      for (const [key, childValue] of Object.entries(value)) {
        if (Object.prototype.hasOwnProperty.call(properties, key)) continue;
        if (extra === false) {
          issues.push({ path: childPath(path, key), message: "is not allowed" });
        } else if (extra !== true) {
          check(extra, childValue, childPath(path, key), issues);
        }
      }
    }
  }

  if (Array.isArray(value) && "items" in schema) {
    const items = schema.items as Schema;
    value.forEach((entry, index) => {
      check(items, entry, `${path}[${index}]`, issues);
    });
  }
}

function matchesType(value: unknown, type: unknown): boolean {
  const types = Array.isArray(type) ? type : [type];
  return types.some((entry) => matchesOneType(value, entry));
}

function matchesOneType(value: unknown, type: unknown): boolean {
  switch (type) {
    case "string":
      return typeof value === "string";
    case "number":
      return typeof value === "number" && Number.isFinite(value);
    case "integer":
      return typeof value === "number" && Number.isInteger(value);
    case "boolean":
      return typeof value === "boolean";
    case "object":
      return isPlainObject(value);
    case "array":
      return Array.isArray(value);
    case "null":
      return value === null;
    default:
      return false;
  }
}

function describeTypes(type: unknown): string {
  const types = Array.isArray(type) ? type : [type];
  const names = types.map((entry) => String(entry));
  if (names.length === 1) return names[0] ?? "a supported type";
  if (names.length === 2) return `${names[0]} or ${names[1]}`;
  return `${names.slice(0, -1).join(", ")}, or ${names[names.length - 1]}`;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function childPath(path: string, key: string): string {
  return /^[A-Za-z_$][A-Za-z0-9_$]*$/.test(key) ? `${path}.${key}` : `${path}[${JSON.stringify(key)}]`;
}

function jsonEqual(a: unknown, b: unknown): boolean {
  if (Object.is(a, b)) return true;
  if (Array.isArray(a) && Array.isArray(b)) {
    return a.length === b.length && a.every((entry, index) => jsonEqual(entry, b[index]));
  }
  if (isPlainObject(a) && isPlainObject(b)) {
    const aKeys = Object.keys(a);
    const bKeys = Object.keys(b);
    return (
      aKeys.length === bKeys.length &&
      aKeys.every((key) => Object.prototype.hasOwnProperty.call(b, key) && jsonEqual(a[key], b[key]))
    );
  }
  return false;
}
