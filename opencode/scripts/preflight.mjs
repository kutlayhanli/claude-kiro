#!/usr/bin/env node
/**
 * `ck doctor --harness opencode` bridge: run the same preflight the plugin runs at startup, without
 * an OpenCode server (so no model catalog; model refs are syntax-checked only).
 *
 *   node scripts/preflight.mjs [project-dir]
 */

import { join } from "node:path";
import { argv, exit, stdout } from "node:process";
import { loadConfig } from "../src/config.ts";
import { formatPreflight, runPreflight } from "../src/preflight.ts";

const projectDir = argv[2] || process.cwd();
const packageRoot = join(import.meta.dirname, "..");
const loaded = loadConfig({ directory: projectDir });
const result = await runPreflight({ projectDir, packageRoot, config: loaded.config });
stdout.write(`${formatPreflight(result)}\n`);
stdout.write(`${JSON.stringify(result)}\n`);
exit(result.ok ? 0 : 1);
