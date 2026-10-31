#!/usr/bin/env node
/**
 * Print the live progress of claude-kiro workflows in an OpenCode project.
 *
 *   node scripts/progress.mjs [--project DIR] [--watch[=SECONDS]] [--json] [--all]
 *
 * Primary source: `<project>/.opencode/ck/status/<runId>.json`, written by the OpenCode plugin.
 * Fallback: the native `<state>/runs/<runId>/record.json` for runs started before the status
 * writer existed (or by a headless probe). Pure Node stdlib; no OpenCode imports.
 */

import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve } from "node:path";

const SCHEMA = "ck-opencode-status/1";

function usage() {
  console.log(`Usage: progress.mjs [options]

  --project DIR       Project to inspect (default: current directory)
  --watch[=SECONDS]   Refresh continuously (default 2 seconds)
  --interval SECONDS  Refresh interval for --watch
  --json              Print the selected status as JSON (NDJSON with --watch)
  --all               List every status file, newest first
  --compact           One line only
  --help              Show this help

Reads <project>/.opencode/ck/status/*.json, then falls back to the native
<state>/runs/*/record.json for this project.`);
}

function parseArgs(argv) {
  const options = { project: process.cwd(), watch: null, json: false, all: false, compact: false };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--help" || arg === "-h") {
      options.help = true;
    } else if (arg === "--json") {
      options.json = true;
    } else if (arg === "--all") {
      options.all = true;
    } else if (arg === "--compact") {
      options.compact = true;
    } else if (arg === "--project") {
      const value = argv[++index];
      if (!value) throw new Error("--project needs a directory");
      options.project = value;
    } else if (arg.startsWith("--project=")) {
      options.project = arg.slice("--project=".length);
    } else if (arg === "--watch") {
      options.watch = 2;
    } else if (arg.startsWith("--watch=")) {
      const value = Number(arg.slice("--watch=".length));
      if (!Number.isFinite(value) || value <= 0) throw new Error("--watch needs a positive number");
      options.watch = value;
    } else if (arg === "--interval") {
      const value = Number(argv[++index]);
      if (!Number.isFinite(value) || value <= 0) throw new Error("--interval needs a positive number");
      options.watch = value;
    } else if (arg.startsWith("--interval=")) {
      const value = Number(arg.slice("--interval=".length));
      if (!Number.isFinite(value) || value <= 0) throw new Error("--interval needs a positive number");
      options.watch = value;
    } else {
      throw new Error(`unknown argument: ${arg}`);
    }
  }
  return options;
}

function readJson(path) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return null;
  }
}

function loadStatuses(project) {
  const directory = join(project, ".opencode", "ck", "status");
  let entries;
  try {
    entries = readdirSync(directory);
  } catch {
    return [];
  }
  const statuses = [];
  for (const entry of entries) {
    if (!entry.endsWith(".json")) continue;
    const status = readJson(join(directory, entry));
    if (status && status.schema === SCHEMA && typeof status.runId === "string") statuses.push(status);
  }
  return statuses;
}

function stateDirs() {
  const dirs = [];
  if (process.env.CK_BENCH_STATE) dirs.push(process.env.CK_BENCH_STATE);
  const base = process.env.XDG_STATE_HOME || join(homedir(), ".local", "state");
  dirs.push(join(base, "claude-kiro", "opencode"));
  return [...new Set(dirs)];
}

function taskNumbers(rows, prefix) {
  const tasks = new Set();
  for (const row of rows) {
    const label = typeof row.label === "string" ? row.label : "";
    if (!new RegExp(`^${prefix}\\b`, "i").test(label)) continue;
    const match = /(?:^|\s)(?:task|land)\s+(\d+)/i.exec(label);
    if (match) tasks.add(Number(match[1]));
  }
  return tasks;
}

function fromRecord(record, logPath, mtime) {
  const rows = Array.isArray(record.agents) ? record.agents : [];
  const merged = taskNumbers(rows.filter((row) => row.status === "ok"), "land");
  const failed = taskNumbers(rows.filter((row) => row.status !== "ok"), "task");
  for (const task of merged) failed.delete(task);
  const started = typeof record.startedAt === "number" ? record.startedAt : mtime;
  const ended = typeof record.endedAt === "number" ? record.endedAt : null;
  const cost = rows.reduce((sum, row) => sum + (typeof row.cost === "number" ? row.cost : 0), 0);
  return {
    schema: SCHEMA,
    runId: record.runId,
    workflow: record.workflow,
    spec: record.spec ?? null,
    projectRoot: record.projectRoot,
    status: record.status === "done" || record.status === "failed" ? record.status : "running",
    phase: null,
    startedAt: started,
    updatedAt: ended ?? mtime,
    endedAt: ended,
    total: Array.isArray(record.args?.remaining) ? record.args.remaining.length : 0,
    merged: merged.size,
    failed: failed.size,
    running: [],
    agents: rows.length,
    cost: Number(cost.toFixed(6)),
    last: rows.length ? (rows[rows.length - 1].label ?? null) : null,
    log: logPath,
  };
}

function loadRecords(project) {
  const real = resolve(project);
  const found = [];
  for (const state of stateDirs()) {
    const runs = join(state, "runs");
    let entries;
    try {
      entries = readdirSync(runs);
    } catch {
      continue;
    }
    for (const entry of entries) {
      const directory = join(runs, entry);
      const recordPath = join(directory, "record.json");
      const record = readJson(recordPath);
      if (!record || record.schema !== "ck-opencode-run/1" || record.projectRoot !== real) continue;
      let mtime = Date.now();
      try {
        mtime = statSync(recordPath).mtimeMs;
      } catch {
        // keep now
      }
      found.push(fromRecord(record, join(directory, "run.log"), mtime));
    }
  }
  return found;
}

function pickActive(statuses) {
  const running = statuses.filter((s) => s.status === "running").sort((a, b) => b.updatedAt - a.updatedAt);
  if (running.length) return running[0];
  // Unlike the TUI footer, the CLI keeps the last finished run so it can be checked later.
  return statuses.sort((a, b) => b.updatedAt - a.updatedAt)[0] ?? null;
}

function shortLabel(label) {
  return String(label).replace(/\s*\[[^\]]*\]\s*$/, "").trim();
}

function elapsed(status, now = Date.now()) {
  const minutes = Math.max(1, Math.round(Math.max(0, (status.endedAt ?? now) - status.startedAt) / 60000));
  return minutes >= 60 ? `${Math.floor(minutes / 60)}h${String(minutes % 60).padStart(2, "0")}m` : `${minutes}m`;
}

function line(status, now = Date.now()) {
  const icon = status.status === "running" ? "▶" : status.failed > 0 ? "!" : "✓";
  const name = status.spec || status.workflow || "workflow";
  const tasks = status.total > 0 ? `${status.merged}/${status.total}` : `${status.merged}`;
  const parts = [`${icon} ck ${name}`, tasks];
  if (status.running?.length === 1) parts.push(shortLabel(status.running[0]));
  else if (status.running?.length > 1) parts.push(`${status.running.length} running`);
  if (status.failed > 0) parts.push(`${status.failed} failed`);
  parts.push(elapsed(status, now));
  if (status.status === "running" && status.agents > 0) parts.push(`${status.agents} agents`);
  return parts.filter(Boolean).join(" · ");
}

function details(status) {
  const rows = [`  phase: ${status.phase ?? "—"}`];
  if (status.running?.length) rows.push(`  running: ${status.running.map(shortLabel).join(", ")}`);
  if (status.last) rows.push(`  last: ${status.last}`);
  if (status.cost) rows.push(`  cost: $${Number(status.cost).toFixed(2)}`);
  if (status.log) {
    rows.push(`  log: ${status.log}`);
    rows.push(`  record: ${status.log.replace(/run\.log$/, "record.json")}`);
  }
  return rows.join("\n");
}

function render(options) {
  const project = resolve(options.project);
  const statuses = loadStatuses(project);
  const all = options.all ? statuses : null;
  const selected = all ? null : pickActive(statuses.length ? statuses : loadRecords(project));
  if (options.json) {
    if (all) {
      return JSON.stringify(all.sort((a, b) => b.updatedAt - a.updatedAt), null, 2);
    }
    return selected ? JSON.stringify(selected, null, 2) : "null";
  }
  if (all) {
    if (all.length === 0) return `no status files in ${join(project, ".opencode", "ck", "status")}`;
    return all
      .sort((a, b) => b.updatedAt - a.updatedAt)
      .map((status) => `${line(status)}\n  run: ${status.runId}`)
      .join("\n");
  }
  if (!selected) return `no active ck workflow in ${project}`;
  return options.compact ? line(selected) : `${line(selected)}\n${details(selected)}`;
}

function clearScreen() {
  if (process.stdout.isTTY) process.stdout.write("\x1b[2J\x1b[H");
}

function main() {
  let options;
  try {
    options = parseArgs(process.argv.slice(2));
  } catch (error) {
    console.error(`progress: ${error.message}`);
    usage();
    return 2;
  }
  if (options.help) {
    usage();
    return 0;
  }
  const print = () => {
    if (options.watch && !options.json) clearScreen();
    process.stdout.write(`${render(options)}\n`);
  };
  print();
  if (options.watch) {
    const timer = setInterval(print, options.watch * 1000);
    process.on("SIGINT", () => {
      clearInterval(timer);
      process.exit(0);
    });
  }
  return 0;
}

process.exitCode = main();
