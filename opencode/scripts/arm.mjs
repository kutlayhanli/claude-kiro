#!/usr/bin/env node
/**
 * Run one benchmark arm against a running OpenCode server.
 *
 *   node arm.mjs --url http://127.0.0.1:4096 --dir <clone> --model provider/model[#variant]
 *                --command spec/implement --text <spec> --state <state-dir> [--timeout 7200]
 *
 * `opencode run` does not dispatch slash commands, so this uses `session.command`. It waits for the
 * workflow's `record.json` (done/failed) rather than the parent's first idle: the workflow runs in the
 * background and can idle the parent before it finishes.
 *
 * `--check` only lists the registered commands, which is a cheap way to verify the plugin loaded.
 */

import { existsSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { OpenCode } from "@opencode/client";
import { discover } from "@opencode/client/service";

const DEFAULTS = {
  command: "spec/implement",
  timeout: 7200,
  interval: 1000,
};

function parseArgs(argv) {
  const options = { ...DEFAULTS, check: false };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--check") {
      options.check = true;
      continue;
    }
    if (!arg.startsWith("--")) throw new Error(`unexpected argument: ${arg}`);
    const key = arg.slice(2);
    const value = argv[index + 1];
    if (value === undefined) throw new Error(`missing value for ${arg}`);
    index += 1;
    switch (key) {
      case "url":
      case "dir":
      case "model":
      case "command":
      case "text":
      case "state":
      case "out":
      case "password":
        options[key] = value;
        break;
      case "timeout":
      case "interval":
        options[key] = Number(value);
        break;
      default:
        throw new Error(`unknown option: ${arg}`);
    }
  }
  if (options.check) {
    if (!options.dir) throw new Error("--dir is required with --check");
    return options;
  }
  for (const required of ["dir", "model", "text", "state"]) {
    if (!options[required]) throw new Error(`--${required} is required`);
  }
  if (!Number.isFinite(options.timeout) || options.timeout <= 0) throw new Error("--timeout must be positive");
  return options;
}

function basicAuth(username, password) {
  return `Basic ${Buffer.from(`${username}:${password}`).toString("base64")}`;
}

async function connect(options) {
  if (options.url) {
    return OpenCode.make({
      baseUrl: options.url,
      ...(options.password ? { headers: { authorization: basicAuth("opencode", options.password) } } : {}),
    });
  }
  const endpoint = await discover();
  if (!endpoint) {
    throw new Error("no OpenCode service found; start `opencode serve --service` or pass --url/--password");
  }
  const headers =
    endpoint.auth?.type === "basic" ? { authorization: basicAuth(endpoint.auth.username, endpoint.auth.password) } : undefined;
  return OpenCode.make({ baseUrl: endpoint.url, ...(headers ? { headers } : {}) });
}

/** Mirror of OpenCode's Model.Ref.parse. */
function parseRef(input) {
  const slash = input.indexOf("/");
  if (slash <= 0) throw new Error(`Invalid model reference: ${input}`);
  const providerID = input.slice(0, slash);
  const hash = input.indexOf("#", slash + 1);
  const id = input.slice(slash + 1, hash === -1 ? undefined : hash);
  const variant = hash === -1 ? undefined : input.slice(hash + 1);
  if (!id || providerID.includes("#") || (variant !== undefined && (!variant || variant.includes("#")))) {
    throw new Error(`Invalid model reference: ${input}`);
  }
  return variant === undefined ? { providerID, id } : { providerID, id, variant };
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function runDirs(stateDir) {
  const root = join(stateDir, "runs");
  return existsSync(root)
    ? readdirSync(root, { withFileTypes: true })
        .filter((entry) => entry.isDirectory())
        .map((entry) => entry.name)
    : [];
}

function readRecord(stateDir, runId) {
  const path = join(stateDir, "runs", runId, "record.json");
  if (!existsSync(path)) return null;
  try {
    return { path, data: JSON.parse(readFileSync(path, "utf8")) };
  } catch {
    return null;
  }
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const client = await connect(options);

  if (options.check) {
    // A location's plugins activate when its first session is created, not on command.list alone.
    const probe = await client.session.create({
      title: "ck bench check",
      location: { directory: options.dir },
    });
    const commands = await client.command.list({ location: { directory: options.dir } });
    const names = (commands.data ?? commands).map((command) => command.name);
    await client.session.remove({ sessionID: probe.id }).catch(() => {});
    process.stdout.write(`${JSON.stringify({ dir: options.dir, commands: names }, null, 2)}\n`);
    return;
  }

  const before = new Set(runDirs(options.state));
  const session = await client.session.create({
    title: `ck bench ${options.text}`,
    model: parseRef(options.model),
    location: { directory: options.dir },
  });

  await client.session.command({
    sessionID: session.id,
    name: options.command,
    text: options.text,
  });

  const deadline = Date.now() + options.timeout * 1000;
  let record = null;
  let runId = null;
  while (Date.now() < deadline) {
    const fresh = runDirs(options.state).filter((entry) => !before.has(entry));
    for (const candidate of fresh) {
      const found = readRecord(options.state, candidate);
      if (!found) continue;
      runId = candidate;
      record = found;
      if (found.data.status === "done" || found.data.status === "failed" || found.data.status === "partial") break;
    }
    if (record) break;
    await sleep(options.interval);
  }

  const info = await client.session.get({ sessionID: session.id }).catch(() => null);
  const result = {
    sessionID: session.id,
    runId,
    recordPath: record?.path ?? null,
    status: record?.data?.status ?? null,
    parentOutcome: info?.outcome ?? null,
  };
  const output = `${JSON.stringify(result, null, 2)}\n`;
  if (options.out) writeFileSync(options.out, output);
  process.stdout.write(output);
  if (!record) {
    process.stderr.write(`no run record appeared within ${options.timeout}s\n`);
    process.exitCode = 1;
  } else if (record.data.status !== "done") {
    process.exitCode = 1;
  }
}

main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = 1;
});
