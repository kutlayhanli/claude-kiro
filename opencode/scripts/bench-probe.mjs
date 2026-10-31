#!/usr/bin/env node
/**
 * The bench harness probe, copied into a clone's .opencode/plugins/bench-probe.js by
 * `claude_kiro.bench_opencode.prepare_probe`.
 *
 * Its `bench_run` tool reads `.bench-args.json`, finds the claude-kiro `workflow` tool, starts
 * `spec:spec-implement` in the background, then polls `<state>/runs/<runId>/record.json` until the run
 * closes and returns `{recordPath, benchPath, status, runId}` as JSON.
 *
 * It does not pick a model or an agent: the runner passes the arm's model to `opencode run`.
 *
 * State directory, in order: $CK_BENCH_STATE, `.bench-state.json` next to the args file, then the
 * plugin's own default (`$XDG_STATE_HOME/claude-kiro/opencode`, or ~/.local/state/...).
 */

import { existsSync, readdirSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve } from "node:path";

const POLL_MS = 1000;
const RUN_CAP_MS = Number(process.env.CK_BENCH_RUN_CAP_MS) > 0
  ? Number(process.env.CK_BENCH_RUN_CAP_MS)
  : 2 * 60 * 60 * 1000;
const TERMINAL = new Set(["done", "failed", "partial"]);

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

function stateDirFor(projectDir) {
  const explicit = process.env.CK_BENCH_STATE;
  if (explicit) return resolve(explicit);
  const marker = join(projectDir, ".bench-state.json");
  if (existsSync(marker)) {
    try {
      const parsed = JSON.parse(readFileSync(marker, "utf8"));
      if (typeof parsed.stateDir === "string" && parsed.stateDir) {
        return resolve(projectDir, parsed.stateDir);
      }
    } catch {
      // fall through to the plugin's default
    }
  }
  const base = process.env.XDG_STATE_HOME || join(homedir(), ".local", "state");
  return join(base, "claude-kiro", "opencode");
}

function runIds(runsDir) {
  return existsSync(runsDir)
    ? readdirSync(runsDir, { withFileTypes: true })
        .filter((entry) => entry.isDirectory())
        .map((entry) => entry.name)
    : [];
}

function readRecord(stateDir, runId) {
  const recordPath = join(stateDir, "runs", runId, "record.json");
  if (!existsSync(recordPath)) return null;
  try {
    return { recordPath, data: JSON.parse(readFileSync(recordPath, "utf8")) };
  } catch {
    return null; // still being written, or not ours
  }
}

function startedRunId(started) {
  const content = typeof started === "string" ? started : started?.content;
  if (typeof content !== "string") return null;
  const match = /runId:\s*([A-Za-z0-9._-]+)/.exec(content);
  return match ? match[1] : null;
}

async function waitForRecord(stateDir, before, startedId) {
  const runsDir = join(stateDir, "runs");
  const deadline = Date.now() + RUN_CAP_MS;
  let latest = null;
  while (Date.now() < deadline) {
    const candidates = startedId
      ? [startedId]
      : runIds(runsDir).filter((runId) => !before.has(runId));
    for (const runId of candidates) {
      const found = readRecord(stateDir, runId);
      if (!found) continue;
      latest = { runId, ...found };
      if (TERMINAL.has(String(found.data.status))) return latest;
    }
    await sleep(POLL_MS);
  }
  return latest;
}

export default {
  id: "claude-kiro-bench-probe",
  async setup(ctx) {
    const projectDir = ctx.location?.directory ?? process.cwd();

    await ctx.tool.transform((editor) => {
      editor.add({
        name: "bench_run",
        description:
          "Run the spec-implement workflow prepared in .bench-args.json and return its run record. " +
          "Call it exactly once; it only returns after the run is done, failed or partial.",
        input: { type: "object", properties: {}, additionalProperties: false },
        execute: async (_input, toolContext) => {
          const argsPath = join(projectDir, ".bench-args.json");
          let args;
          try {
            args = JSON.parse(readFileSync(argsPath, "utf8"));
          } catch (error) {
            throw new Error(
              `bench_run: cannot read ${argsPath}: ${error instanceof Error ? error.message : String(error)}`,
            );
          }

          const stateDir = stateDirFor(projectDir);
          const before = new Set(runIds(join(stateDir, "runs")));

          const listed = await ctx.tool.list();
          const tools = Array.isArray(listed) ? listed : (listed?.data ?? []);
          const workflow = tools.find((tool) => (tool.id ?? tool.name) === "workflow");
          if (!workflow) {
            throw new Error(
              "bench_run: the claude-kiro workflow tool is not registered; is the plugin loaded?",
            );
          }

          const started = await workflow.execute(
            { name: "spec:spec-implement", args },
            { sessionID: toolContext.sessionID },
          );
          const found = await waitForRecord(stateDir, before, startedRunId(started));
          const runId = found?.runId ?? startedRunId(started) ?? null;
          const result = {
            recordPath: found?.recordPath ?? null,
            benchPath: runId ? join(stateDir, "runs", runId, "bench-output.json") : null,
            status: found?.data?.status ?? null,
            runId,
          };
          return { content: JSON.stringify(result) };
        },
      });
    });
  },
};
