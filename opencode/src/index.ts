import { join } from "node:path";
import { Plugin } from "@opencode/plugin";
import { materializeAgents } from "./agents.ts";
import { registerCommands } from "./commands.ts";
import { loadConfig } from "./config.ts";
import { createGuard } from "./guard.ts";
import { writeInstructions } from "./instructions.ts";
import { catalogFromModels } from "./models.ts";
import { materializeResources } from "./resources.ts";
import { registerWorkflowTool } from "./workflow-tool.ts";

/**
 * The package entry OpenCode loads. Setup registers the workflow bridge and the five commands, then
 * materializes the role agents, the two project resources, and the AGENTS.md managed block.
 */
export default Plugin.define({
  id: "claude-kiro",
  async setup(ctx) {
    const packageRoot = join(import.meta.dirname, "..");
    const projectDir = ctx.location.directory;
    const loaded = loadConfig({ directory: projectDir, options: ctx.options });

    let catalog;
    try {
      const models = await ctx.model.list({});
      catalog = catalogFromModels(
        models.data.map((model) => ({
          providerID: model.providerID,
          id: model.id,
          variants: model.variants,
        })),
      );
    } catch (error) {
      console.error(
        `[claude-kiro] could not read the model catalog; effort variants will only resolve when configured: ${message(error)}`,
      );
    }

    const workflows = await registerWorkflowTool({
      ctx,
      config: loaded.config,
      catalog,
      packageRoot,
      projectDir,
    });
    const guard = await createGuard(ctx, loaded.config, projectDir);
    const commands = await registerCommands(ctx, { packageRoot, projectDir });
    const agents = materializeAgents(projectDir, packageRoot);
    const resources = materializeResources(projectDir, packageRoot);
    const instructions = writeInstructions(projectDir, packageRoot);

    if (agents.conflicts.length > 0) {
      console.error(
        `[claude-kiro] kept your edited agent file(s): ${agents.conflicts.join(", ")} ` +
          "(delete one to let the plugin regenerate it)",
      );
    }
    console.error(
      `[claude-kiro] ready: ${commands.names.length} commands, ` +
        `${agents.written.length + agents.unchanged.length + agents.conflicts.length} agents, ` +
        `${resources.length} resources` +
        `${instructions.changed ? `, AGENTS.md ${instructions.appended ? "updated" : "refreshed"}` : ""}` +
        ` (sonnet=${loaded.config.models.sonnet}, project=${projectDir})`,
    );

    return async () => {
      await guard.dispose().catch(() => {});
      await commands.dispose().catch(() => {});
      await workflows();
    };
  },
});

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
