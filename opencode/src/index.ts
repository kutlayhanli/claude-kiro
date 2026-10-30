import { join } from "node:path";
import { Plugin } from "@opencode/plugin";
import { loadConfig } from "./config.ts";
import { catalogFromModels } from "./models.ts";
import { registerWorkflowTool } from "./workflow-tool.ts";

/**
 * The package entry OpenCode loads. Phase 1 exposes the `workflow` bridge; commands and agent files
 * are added by the same setup in later phases.
 */
export default Plugin.define({
  id: "claude-kiro",
  async setup(ctx) {
    const loaded = loadConfig({ directory: ctx.location.directory, options: ctx.options });

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

    const cleanup = await registerWorkflowTool({
      ctx,
      config: loaded.config,
      catalog,
      packageRoot: join(import.meta.dirname, ".."),
      projectDir: ctx.location.directory,
    });

    console.error(
      `[claude-kiro] ready (sonnet=${loaded.config.models.sonnet}, project=${loaded.files.project})`,
    );
    return cleanup;
  },
});

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
