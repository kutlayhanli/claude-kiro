import assert from "node:assert/strict";
import test from "node:test";
import {
  ModelError,
  catalogFromModels,
  formatRef,
  parseRef,
  resolveModel,
  type Catalog,
} from "../src/models.ts";

const ALIASES = {
  haiku: "opencode/claude-haiku-5-5",
  sonnet: "opencode/claude-sonnet-5-5",
  opus: "opencode/claude-opus-5-5",
  fable: "opencode/claude-fable-5-1",
};

const CATALOG: Catalog = [
  { providerID: "opencode", id: "claude-sonnet-5-5", variants: ["low", "medium", "high", "max"] },
  { providerID: "opencode", id: "claude-opus-5-5", variants: ["low", "medium", "high", "xhigh", "max"] },
  { providerID: "opencode", id: "claude-haiku-5-5", variants: ["low", "medium"] },
  { providerID: "opencode", id: "claude-fable-5-1", variants: ["high"] },
  { providerID: "opencode", id: "deepseek-v4.1-flash-free", variants: ["high"] },
  { providerID: "opencode", id: "muse-spark-1.3", variants: [] },
  { providerID: "other", id: "claude-sonnet-5-5", variants: ["high"] },
];

test("parseRef mirrors OpenCode's provider/model#variant parser", () => {
  assert.deepEqual(parseRef("opencode/claude-sonnet-5-5"), {
    providerID: "opencode",
    id: "claude-sonnet-5-5",
  });
  assert.deepEqual(parseRef("opencode/claude-sonnet-5-5#high"), {
    providerID: "opencode",
    id: "claude-sonnet-5-5",
    variant: "high",
  });
  assert.equal(formatRef(parseRef("a/b#c")), "a/b#c");
});

test("parseRef rejects malformed references", () => {
  for (const input of ["claude", "", "/x", "p/", "p/#x", "p/x#", "p/x#a#b", "p#x/y"]) {
    assert.throws(() => parseRef(input), ModelError, input);
  }
});

test("an alias resolves exactly", () => {
  const result = resolveModel("sonnet", { aliases: ALIASES, catalog: CATALOG, effort: "high" });
  assert.deepEqual(result.resolved, {
    providerID: "opencode",
    id: "claude-sonnet-5-5",
    variant: "high",
  });
  assert.equal(result.resolvedEffort, "high");
  assert.deepEqual(result.notes, []);
});

test("an unavailable effort rounds up to the nearest higher variant", () => {
  const result = resolveModel("sonnet", { aliases: ALIASES, catalog: CATALOG, effort: "xhigh" });
  assert.equal(result.variant, "max");
  assert.match(result.notes.join("\n"), /nearest higher/);
});

test("when nothing is higher, it rounds down", () => {
  const result = resolveModel("deepseek-v4.1-flash-free", {
    aliases: ALIASES,
    catalog: CATALOG,
    effort: "max",
  });
  assert.equal(result.variant, "high");
  assert.match(result.notes.join("\n"), /nearest lower/);
});

test("a model with no variants drops the effort instead of failing", () => {
  const result = resolveModel("muse-spark-1.3", {
    aliases: ALIASES,
    catalog: CATALOG,
    effort: "high",
  });
  assert.equal(result.variant, null);
  assert.equal(result.resolvedEffort, null);
  assert.match(result.notes.join("\n"), /dropped/);
  assert.equal(result.resolved?.id, "muse-spark-1.3");
});

test("model inherit resolves to null and sends no variant", () => {
  const result = resolveModel("inherit", { aliases: ALIASES, catalog: CATALOG, effort: "high" });
  assert.equal(result.resolved, null);
  assert.equal(result.resolvedEffort, null);
  assert.match(result.notes.join("\n"), /session model/);
});

test("effort inherit sends no variant, even when the alias pins one", () => {
  const aliases = { ...ALIASES, sonnet: "opencode/claude-sonnet-5-5#high" };
  const result = resolveModel("sonnet", { aliases, catalog: CATALOG, effort: "inherit" });
  assert.deepEqual(result.resolved, { providerID: "opencode", id: "claude-sonnet-5-5" });
  assert.equal(result.variant, null);
  assert.match(result.notes.join("\n"), /sends no variant/);
});

test("an alias's pinned variant is kept when no effort is requested", () => {
  const aliases = { ...ALIASES, sonnet: "opencode/claude-sonnet-5-5#medium" };
  const result = resolveModel("sonnet", { aliases, catalog: CATALOG });
  assert.equal(result.variant, "medium");
  assert.equal(result.requested.effort, null);
});

test("a requested effort overrides an alias's pinned variant", () => {
  const aliases = { ...ALIASES, sonnet: "opencode/claude-sonnet-5-5#high" };
  const result = resolveModel("sonnet", { aliases, catalog: CATALOG, effort: "low" });
  assert.equal(result.variant, "low");
});

test("an unresolvable effort keeps an alias's pinned variant", () => {
  const aliases = { ...ALIASES, sonnet: "opencode/claude-sonnet-5-5#high" };
  const result = resolveModel("sonnet", { aliases, effort: "low" });
  assert.equal(result.variant, "high");
  assert.equal(result.resolvedEffort, "high");
  assert.match(result.notes.join("\n"), /keeping the ref's #high/);
});

test("a per-model effort override wins over the default mapping", () => {
  const result = resolveModel("sonnet", {
    aliases: ALIASES,
    catalog: CATALOG,
    effort: "xhigh",
    efforts: {
      default: { xhigh: "high" },
      models: { "opencode/claude-sonnet-5-5": { xhigh: "max" } },
    },
  });
  assert.equal(result.variant, "max");
  assert.equal(result.resolvedEffort, "max");
});

test("the default effort mapping applies when the model has no override", () => {
  const result = resolveModel("opus", {
    aliases: ALIASES,
    catalog: CATALOG,
    effort: "xhigh",
    efforts: { default: { xhigh: "high" } },
  });
  assert.equal(result.variant, "high");
  assert.match(result.notes.join("\n"), /by config/);
});

test("a configured variant missing from the catalog falls back to nearest", () => {
  const result = resolveModel("sonnet", {
    aliases: ALIASES,
    catalog: CATALOG,
    effort: "medium",
    efforts: { default: { medium: "turbo" } },
  });
  assert.equal(result.variant, "medium");
  assert.match(result.notes.join("\n"), /turbo/);
  assert.match(result.notes.join("\n"), /not available/);
});

test("opaque provider/model refs pass through", () => {
  const result = resolveModel("opencode/deepseek-v4.1-flash-free", {
    aliases: ALIASES,
    catalog: CATALOG,
    effort: "high",
  });
  assert.equal(result.variant, "high");
  assert.equal(result.resolved?.id, "deepseek-v4.1-flash-free");
});

test("an unavailable full ref fails fast with suggestions", () => {
  assert.throws(
    () => resolveModel("opencode/claude-sonnet-9", { aliases: ALIASES, catalog: CATALOG }),
    (error: unknown) =>
      error instanceof ModelError &&
      /not available/.test(error.message) &&
      /opencode\/claude-sonnet-5-5/.test(error.message),
  );
});

test("a bare opaque id resolves when it is unique in the catalog", () => {
  const result = resolveModel("deepseek-v4.1-flash-free", { aliases: ALIASES, catalog: CATALOG });
  assert.equal(result.resolved?.providerID, "opencode");
  assert.equal(result.resolved?.id, "deepseek-v4.1-flash-free");
});

test("a bare id in two providers is rejected as ambiguous", () => {
  assert.throws(
    () => resolveModel("claude-sonnet-5-5", { aliases: ALIASES, catalog: CATALOG }),
    (error: unknown) => error instanceof ModelError && /ambiguous/.test(error.message),
  );
});

test("an unknown model lists aliases and nearest refs", () => {
  assert.throws(
    () => resolveModel("gpt-nope", { aliases: ALIASES, catalog: CATALOG }),
    (error: unknown) =>
      error instanceof ModelError && /aliases: haiku, sonnet, opus, fable/.test(error.message),
  );
});

test("unknown effort vocabulary fails", () => {
  assert.throws(
    () => resolveModel("sonnet", { aliases: ALIASES, catalog: CATALOG, effort: "turbo" }),
    (error: unknown) => error instanceof ModelError && /unknown effort/.test(error.message),
  );
});

test("catalogFromModels accepts string and object variants", () => {
  const catalog = catalogFromModels([
    { providerID: "p", id: "a", variants: ["low", { id: "high" }] },
    { providerID: "p", id: "b" },
  ]);
  assert.deepEqual(catalog, [
    { providerID: "p", id: "a", variants: ["low", "high"] },
    { providerID: "p", id: "b", variants: [] },
  ]);
});
