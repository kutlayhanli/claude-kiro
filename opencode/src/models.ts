/**
 * Model and effort resolution between the ck vocabulary and OpenCode's `Model.Ref`.
 *
 * ck passes model *aliases* (`haiku`, `sonnet`, `opus`, `fable`) and an *effort*
 * (`low`, `medium`, `high`, `xhigh`, `max`, `inherit`). OpenCode wants
 * `{ providerID, id, variant? }`, where the variant is a model-specific name.
 *
 * Effort resolution is "nearest, prefer up": an exact variant wins; otherwise the
 * closest higher variant, then the closest lower, then no variant at all. It never
 * throws for an unavailable effort; it records what it did. Unknown vocabulary does
 * throw, because that is a config or ck-version error and must fail before a child runs.
 */

export const MODELS = ["haiku", "sonnet", "opus", "fable"] as const;
export type ModelAlias = (typeof MODELS)[number];

export const EFFORTS = ["low", "medium", "high", "xhigh", "max"] as const;
export type Effort = (typeof EFFORTS)[number];

export const INHERIT = "inherit";

export interface ModelRef {
  providerID: string;
  id: string;
  variant?: string;
}

export interface CatalogModel {
  providerID: string;
  id: string;
  variants: readonly string[];
}

export type Catalog = readonly CatalogModel[];

/** A model as `ctx.model.list()` returns it. Variants may be strings or `{ id }` objects. */
export interface CatalogSource {
  id: string;
  providerID: string;
  variants?: readonly (string | { id: string })[];
}

export interface EffortMapping {
  /** effort -> variant name, used when a model has no more specific entry. */
  default?: Readonly<Record<string, string>>;
  /** base model ref (`provider/model`) -> effort -> variant name. */
  models?: Readonly<Record<string, Readonly<Record<string, string>>>>;
}

export interface ResolveModelOptions {
  /** ck alias -> `provider/model[#variant]`. */
  aliases: Readonly<Record<string, string>>;
  /** Known models and their variants. When present, the resolved model must exist in it. */
  catalog?: Catalog;
  /** Effort requested by ck. `null`/absent means "no effort", `inherit` means "send no variant". */
  effort?: string | null;
  efforts?: EffortMapping;
}

export interface Resolution {
  requested: { model: string; effort: string | null };
  /** `null` only for `inherit`: the caller uses the session's model. */
  resolved: ModelRef | null;
  requestedEffort: string | null;
  /** The variant actually placed on the model ref, or `null`. */
  resolvedEffort: string | null;
  variant: string | null;
  notes: string[];
}

export class ModelError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ModelError";
  }
}

const EFFORT_RANK = new Map<string, number>(EFFORTS.map((effort, index) => [effort, index]));

export function isAlias(value: string): value is ModelAlias {
  return (MODELS as readonly string[]).includes(value);
}

export function isEffort(value: string): value is Effort {
  return (EFFORTS as readonly string[]).includes(value);
}

/** The roles a workflow agent can be pinned to through `opencode.roles`. */
export const ROLE_NAMES = [
  "implementer",
  "test_writer",
  "reviewer",
  "reviewer_risky",
  "fixer",
  "orchestrator",
  "resolver",
] as const;
export type RoleName = (typeof ROLE_NAMES)[number];

export function isRoleName(value: string): value is RoleName {
  return (ROLE_NAMES as readonly string[]).includes(value);
}

/** A role's model reference, split into its parts. `variant` is null when the reference has none. */
export interface RoleRef {
  provider: string;
  model: string;
  variant: string | null;
}

/**
 * Check one `opencode.roles` value. Returns `null` when it is a valid `provider/model[#variant]`
 * reference, otherwise a short description of what is wrong with it.
 */
export function validateRoleRef(value: unknown): string | null {
  if (typeof value !== "string") return "expected a provider/model[#variant] string";
  if (!value.trim() || value !== value.trim()) return "must not be empty or padded with spaces";
  const slash = value.indexOf("/");
  if (slash === -1) return "missing model (expected provider/model[#variant])";
  if (slash === 0) return "missing provider (expected provider/model[#variant])";
  const provider = value.slice(0, slash);
  if (provider.includes("#")) return "missing provider (expected provider/model[#variant])";
  const hash = value.indexOf("#", slash + 1);
  const model = value.slice(slash + 1, hash === -1 ? undefined : hash);
  if (!model) return "missing model (expected provider/model[#variant])";
  if (hash !== -1) {
    const variant = value.slice(hash + 1);
    if (!variant) return "empty variant after # (expected provider/model[#variant])";
    if (variant.includes("#")) return "more than one # (expected provider/model[#variant])";
  }
  return null;
}

/**
 * The model a role is mapped to, or `null` when the role is unmapped (the caller then uses the
 * alias map). Values were checked by `validateRoleRef` when the config loaded.
 */
export function resolveRoleRef(role: string, roles: Partial<Record<string, string>>): RoleRef | null {
  const value = roles[role];
  if (value === undefined) return null;
  const ref = parseRef(value);
  return { provider: ref.providerID, model: ref.id, variant: ref.variant ?? null };
}

/** Mirror of OpenCode's `Model.Ref.parse` (packages/schema/src/model.ts). */
export function parseRef(input: string): ModelRef {
  const providerEnd = input.indexOf("/");
  if (providerEnd <= 0) throw new ModelError(`Invalid model reference: ${input}`);
  const providerID = input.slice(0, providerEnd);
  const variantStart = input.indexOf("#", providerEnd + 1);
  const id = input.slice(providerEnd + 1, variantStart === -1 ? undefined : variantStart);
  const variant = variantStart === -1 ? undefined : input.slice(variantStart + 1);
  if (!id || providerID.includes("#") || (variant !== undefined && (!variant || variant.includes("#")))) {
    throw new ModelError(`Invalid model reference: ${input}`);
  }
  return variant === undefined ? { providerID, id } : { providerID, id, variant };
}

export function formatRef(ref: ModelRef): string {
  return `${ref.providerID}/${ref.id}${ref.variant ? `#${ref.variant}` : ""}`;
}

/** The `provider/model` part, without a variant. */
export function baseRef(ref: ModelRef): string {
  return `${ref.providerID}/${ref.id}`;
}

export function catalogFromModels(models: readonly CatalogSource[]): Catalog {
  return models.map((model) => ({
    providerID: model.providerID,
    id: model.id,
    variants: (model.variants ?? []).map((variant) => (typeof variant === "string" ? variant : variant.id)),
  }));
}

/**
 * Resolve one `agent()` call's model and effort.
 *
 * `resolved: null` means the call inherits the session model (a caller must still turn that into an
 * explicit ref for `session.create`, because an OpenCode child with no model fails to run).
 */
export function resolveModel(requested: string, options: ResolveModelOptions): Resolution {
  const rawEffort = options.effort ?? null;
  const notes: string[] = [];

  let requestedEffort: string | null = null;
  if (rawEffort !== null) {
    if (rawEffort === INHERIT) {
      requestedEffort = INHERIT;
      notes.push("effort inherit sends no variant");
    } else if (isEffort(rawEffort)) {
      requestedEffort = rawEffort;
    } else {
      throw new ModelError(
        `unknown effort ${JSON.stringify(rawEffort)} (use ${EFFORTS.join(", ")}, or ${INHERIT})`,
      );
    }
  }

  if (requested === INHERIT) {
    notes.push("model inherit uses the session model");
    return {
      requested: { model: requested, effort: rawEffort },
      resolved: null,
      requestedEffort,
      resolvedEffort: null,
      variant: null,
      notes,
    };
  }

  const ref = resolveRef(requested, options);
  const key = baseRef(ref);
  const catalogVariants = findVariants(options.catalog, ref);
  const baseVariant = ref.variant ?? null;

  if (requestedEffort === INHERIT) {
    const resolved = stripVariant(ref);
    return {
      requested: { model: requested, effort: rawEffort },
      resolved,
      requestedEffort,
      resolvedEffort: null,
      variant: null,
      notes,
    };
  }

  if (requestedEffort === null) {
    return {
      requested: { model: requested, effort: rawEffort },
      resolved: ref,
      requestedEffort,
      resolvedEffort: baseVariant,
      variant: baseVariant,
      notes,
    };
  }

  const override = lookupEffort(options.efforts, key, requestedEffort);
  const result = chooseVariant(requestedEffort, catalogVariants, override);

  if (result.overrideMissing) {
    notes.push(
      `configured ${requestedEffort} variant ${JSON.stringify(override)} is not available on ${key}; ` +
        `using ${result.variant ?? "no variant"}`,
    );
  }
  if (result.variant === null) {
    if (baseVariant !== null) {
      notes.push(`effort ${requestedEffort} dropped; keeping the ref's #${baseVariant}`);
    } else {
      notes.push(`effort ${requestedEffort} dropped; ${key} has no usable variants`);
    }
    return {
      requested: { model: requested, effort: rawEffort },
      resolved: ref,
      requestedEffort,
      resolvedEffort: baseVariant,
      variant: baseVariant,
      notes,
    };
  }

  if (result.note) notes.push(result.note);

  const resolved = withVariant(ref, result.variant);
  return {
    requested: { model: requested, effort: rawEffort },
    resolved,
    requestedEffort,
    resolvedEffort: result.variant,
    variant: result.variant,
    notes,
  };
}

interface VariantChoice {
  variant: string | null;
  note: string | null;
  /** A configured variant was unavailable, so the fallback below is not what the config asked for. */
  overrideMissing: boolean;
}

/**
 * "Nearest, prefer up": exact first, then the closest higher effort, then the closest lower.
 * Non-effort variant names only ever win through an explicit mapping.
 */
export function chooseVariant(
  effort: string,
  variants: readonly string[],
  override?: string | undefined,
): VariantChoice {
  const overrideMissing =
    override !== undefined && variants.length > 0 && !variants.includes(override);

  if (override && (variants.length === 0 || variants.includes(override))) {
    return {
      variant: override,
      note: override === effort ? null : `effort ${effort} resolved to ${override} by config`,
      overrideMissing: false,
    };
  }

  const target = override && isEffort(override) ? override : effort;
  if (variants.includes(target)) {
    return {
      variant: target,
      note: target === effort ? null : `effort ${effort} resolved to ${target} by config`,
      overrideMissing,
    };
  }

  const ranked = variants
    .map((variant) => ({ variant, rank: EFFORT_RANK.get(variant) }))
    .filter((entry): entry is { variant: string; rank: number } => entry.rank !== undefined)
    .sort((a, b) => a.rank - b.rank);
  const targetRank = EFFORT_RANK.get(target);
  if (targetRank === undefined || ranked.length === 0) {
    return { variant: null, note: null, overrideMissing };
  }

  const higher = ranked.find((entry) => entry.rank > targetRank);
  if (higher) {
    return {
      variant: higher.variant,
      note: `effort ${effort} is not a variant of this model; using ${higher.variant} (nearest higher)`,
      overrideMissing,
    };
  }
  const lower = [...ranked].reverse().find((entry) => entry.rank < targetRank);
  if (lower) {
    return {
      variant: lower.variant,
      note: `effort ${effort} is not a variant of this model; using ${lower.variant} (nearest lower)`,
      overrideMissing,
    };
  }
  return { variant: null, note: null, overrideMissing };
}

function resolveRef(requested: string, options: ResolveModelOptions): ModelRef {
  const aliasTarget = options.aliases[requested];
  if (aliasTarget !== undefined) {
    let parsed: ModelRef;
    try {
      parsed = parseRef(aliasTarget);
    } catch {
      throw new ModelError(`alias ${requested} points at an invalid model reference: ${aliasTarget}`);
    }
    if (options.catalog) assertInCatalog(parsed, options.catalog);
    return parsed;
  }

  if (requested.includes("/")) {
    const parsed = parseRef(requested);
    if (options.catalog) assertInCatalog(parsed, options.catalog);
    return parsed;
  }

  if (options.catalog) {
    const matches = options.catalog.filter((model) => model.id === requested);
    if (matches.length === 1) {
      const match = matches[0]!;
      return { providerID: match.providerID, id: match.id };
    }
    if (matches.length > 1) {
      throw new ModelError(
        `model ${JSON.stringify(requested)} is ambiguous: ${matches
          .map((model) => `${model.providerID}/${model.id}`)
          .join(", ")}`,
      );
    }
    throw new ModelError(
      `unknown model ${JSON.stringify(requested)} (aliases: ${Object.keys(options.aliases).join(", ") || "none"}); ` +
        `nearest available: ${nearestRefs(options.catalog, requested).join(", ") || "none"}`,
    );
  }

  throw new ModelError(
    `model ${JSON.stringify(requested)} is neither an alias (${Object.keys(options.aliases).join(", ") || "none"}) ` +
      `nor a provider/model reference`,
  );
}

function assertInCatalog(ref: ModelRef, catalog: Catalog): void {
  const found = catalog.find((model) => model.providerID === ref.providerID && model.id === ref.id);
  if (found) return;
  throw new ModelError(
    `model ${baseRef(ref)} is not available; nearest available: ${nearestRefs(catalog, ref.id).join(", ") || "none"}`,
  );
}

function findVariants(catalog: Catalog | undefined, ref: ModelRef): readonly string[] {
  if (!catalog) return [];
  const model = catalog.find((entry) => entry.providerID === ref.providerID && entry.id === ref.id);
  return model?.variants ?? [];
}

function lookupEffort(
  efforts: EffortMapping | undefined,
  key: string,
  effort: string,
): string | undefined {
  return efforts?.models?.[key]?.[effort] ?? efforts?.default?.[effort];
}

function withVariant(ref: ModelRef, variant: string): ModelRef {
  return { providerID: ref.providerID, id: ref.id, variant };
}

function stripVariant(ref: ModelRef): ModelRef {
  return { providerID: ref.providerID, id: ref.id };
}

function nearestRefs(catalog: Catalog, wanted: string): string[] {
  const withScore = catalog.map((model) => ({
    ref: `${model.providerID}/${model.id}`,
    score: commonPrefix(wanted, model.id),
  }));
  withScore.sort((a, b) => b.score - a.score || a.ref.localeCompare(b.ref));
  return withScore.slice(0, 5).map((entry) => entry.ref);
}

function commonPrefix(a: string, b: string): number {
  const length = Math.min(a.length, b.length);
  let index = 0;
  while (index < length && a[index] === b[index]) index += 1;
  return index;
}
