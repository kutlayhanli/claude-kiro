/**
 * TUI status line for claude-kiro workflows.
 *
 * The server-side `workflow` tool writes one status file per run under
 * `<project>/.opencode/ck/status/<runId>.json`; this part polls that directory and renders the
 * newest running run (or a just-finished one) in OpenCode's prompt footer.
 *
 * OpenCode loads this as the package's `./tui` entrypoint. It needs no server RPC: the files are the
 * contract between the server and TUI processes.
 */

import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { Plugin } from "@opencode/plugin/tui";
import { createSignal } from "solid-js";
import {
  formatWorkflowStatus,
  isWorkflowStatus,
  pickActiveStatus,
  type WorkflowStatus,
} from "./status-format.ts";
import { statusDir } from "./status.ts";

const POLL_MS = 1000;

function loadStatuses(directory: string): WorkflowStatus[] {
  let entries: string[];
  try {
    entries = readdirSync(directory);
  } catch {
    return [];
  }
  const statuses: WorkflowStatus[] = [];
  for (const entry of entries) {
    if (!entry.endsWith(".json")) continue;
    try {
      const parsed: unknown = JSON.parse(readFileSync(join(directory, entry), "utf8"));
      if (isWorkflowStatus(parsed)) statuses.push(parsed);
    } catch {
      // A partially written status file is ignored until the next poll.
    }
  }
  return statuses;
}

export default Plugin.define({
  id: "claude-kiro.status",
  setup(context) {
    const projectDir = context.location?.directory ?? context.data.location.default().directory;
    const directory = statusDir(projectDir);
    const [line, setLine] = createSignal<string | null>(null);

    const refresh = (): void => {
      const status = pickActiveStatus(loadStatuses(directory));
      setLine(status ? formatWorkflowStatus(status) : null);
    };
    refresh();

    const timer = setInterval(refresh, POLL_MS);
    const dispose = context.ui.slot({
      append: "prompt.footer.status",
      render: () => {
        const value = line();
        return value ? <text fg={context.theme.text.base}>{value}</text> : null;
      },
    });

    return () => {
      clearInterval(timer);
      void dispose();
    };
  },
});
