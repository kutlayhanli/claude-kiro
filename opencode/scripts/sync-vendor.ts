#!/usr/bin/env node
/**
 * Copy the shared claude-kiro plugin sources into the npm package, with a digest manifest.
 *
 * `plugin/` stays the single source for both harnesses. `vendor/` is generated, git-ignored, and
 * shipped in the npm tarball. `--check` fails if the copy has drifted from `plugin/`, which is the
 * guard against editing a vendored workflow while debugging.
 */

import { createHash } from "node:crypto";
import {
  copyFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { dirname, join, relative, sep } from "node:path";

const HERE = import.meta.dirname;
const SOURCE_ROOT = join(HERE, "..", "..", "plugin");
const VENDOR_ROOT = join(HERE, "..", "vendor");
const MANIFEST_PATH = join(VENDOR_ROOT, "manifest.json");
const GROUPS = ["workflows", "skills", "agents", "resources", "output-styles"] as const;

interface ManifestEntry {
  source: string;
  sha256: string;
  bytes: number;
}

interface Manifest {
  generatedBy: string;
  source: string;
  digestAlgorithm: "sha256";
  files: Record<string, ManifestEntry>;
}

interface Copy {
  /** Path relative to `vendor/`, always POSIX separators. */
  target: string;
  /** Path relative to the repo root, always POSIX separators. */
  source: string;
  bytes: Buffer;
}

function main(): void {
  const check = process.argv.includes("--check");
  const copies = collect();

  if (check) {
    const drift = verify(copies);
    if (drift.length > 0) {
      console.error(`sync-vendor: vendor/ is stale (${drift.length} difference${drift.length === 1 ? "" : "s"})`);
      for (const line of drift.slice(0, 20)) console.error(`  ${line}`);
      if (drift.length > 20) console.error(`  … and ${drift.length - 20} more`);
      console.error("sync-vendor: run `npm run sync-vendor` from opencode/");
      process.exitCode = 1;
      return;
    }
    console.log(`sync-vendor: vendor/ is current (${copies.length} files)`);
    return;
  }

  write(copies);
}

function collect(): Copy[] {
  if (!existsSync(SOURCE_ROOT)) {
    throw new Error(`sync-vendor: source directory not found: ${SOURCE_ROOT}`);
  }
  const copies: Copy[] = [];
  for (const group of GROUPS) {
    const directory = join(SOURCE_ROOT, group);
    if (!existsSync(directory)) {
      throw new Error(`sync-vendor: missing source group: ${directory}`);
    }
    for (const target of walk(directory)) {
      const source = join(directory, ...target.split("/"));
      copies.push({
        target: `${group}/${target}`,
        source: relative(join(SOURCE_ROOT, ".."), source).split(sep).join("/"),
        bytes: readFileSync(source),
      });
    }
  }
  return copies;
}

function write(copies: Copy[]): void {
  rmSync(VENDOR_ROOT, { recursive: true, force: true });
  let bytes = 0;
  const files: Record<string, ManifestEntry> = {};
  for (const copy of copies) {
    const destination = join(VENDOR_ROOT, ...copy.target.split("/"));
    mkdirSync(dirname(destination), { recursive: true });
    writeFileSync(destination, copy.bytes);
    bytes += copy.bytes.byteLength;
    files[copy.target] = {
      source: copy.source,
      sha256: digest(copy.bytes),
      bytes: copy.bytes.byteLength,
    };
  }
  const manifest: Manifest = {
    generatedBy: "opencode/scripts/sync-vendor.ts",
    source: "plugin/",
    digestAlgorithm: "sha256",
    files,
  };
  writeFileSync(MANIFEST_PATH, `${JSON.stringify(manifest, null, 2)}\n`);
  console.log(
    `sync-vendor: copied ${copies.length} files (${formatBytes(bytes)}) from plugin/ to opencode/vendor/`,
  );
}

function verify(copies: Copy[]): string[] {
  if (!existsSync(MANIFEST_PATH)) {
    return ["vendor/manifest.json is missing"];
  }
  if (!existsSync(VENDOR_ROOT)) {
    return ["vendor/ is missing"];
  }
  let manifest: Manifest;
  try {
    manifest = JSON.parse(readFileSync(MANIFEST_PATH, "utf8")) as Manifest;
  } catch (error) {
    return [`vendor/manifest.json is unreadable (${message(error)})`];
  }
  if (!manifest || typeof manifest !== "object" || !manifest.files || typeof manifest.files !== "object") {
    return ["vendor/manifest.json has no files map"];
  }

  const drift: string[] = [];
  const seen = new Set<string>();

  for (const copy of copies) {
    seen.add(copy.target);
    const entry = manifest.files[copy.target];
    if (!entry) {
      drift.push(`not in the manifest: ${copy.target}`);
      continue;
    }
    if (entry.source !== copy.source) {
      drift.push(`source path changed: ${copy.target} (manifest: ${entry.source}, now: ${copy.source})`);
    }
    const now = digest(copy.bytes);
    if (entry.sha256 !== now) {
      drift.push(`source changed since sync: ${copy.source}`);
    }
    const destination = join(VENDOR_ROOT, ...copy.target.split("/"));
    if (!existsSync(destination)) {
      drift.push(`missing from vendor/: ${copy.target}`);
      continue;
    }
    const vendored = readFileSync(destination);
    if (digest(vendored) !== now) {
      drift.push(`vendor/ copy differs from source: ${copy.target}`);
    }
  }

  for (const target of Object.keys(manifest.files)) {
    if (!seen.has(target)) drift.push(`removed from source but still in the manifest: ${target}`);
  }

  for (const target of walk(VENDOR_ROOT)) {
    if (target === "manifest.json") continue;
    if (!seen.has(target)) drift.push(`unexpected file in vendor/: ${target}`);
  }

  return drift;
}

function walk(root: string, prefix = ""): string[] {
  const found: string[] = [];
  for (const name of readdirSync(root, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
    const target = prefix ? `${prefix}/${name.name}` : name.name;
    const full = join(root, name.name);
    if (name.isDirectory()) {
      found.push(...walk(full, target));
    } else if (name.isFile()) {
      if (statSync(full).isFile()) found.push(target);
    }
  }
  return found;
}

function digest(bytes: Buffer): string {
  return createHash("sha256").update(bytes).digest("hex");
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

try {
  main();
} catch (error) {
  console.error(message(error));
  process.exitCode = 1;
}
