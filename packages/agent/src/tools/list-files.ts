import { Type } from "typebox";
import { defineTool } from "@earendil-works/pi-coding-agent";
import { readdir } from "node:fs/promises";
import { join } from "node:path";
import type { AgentTool } from "./tool-types.js";
import { isInsideRepo } from "./path-utils.js";

const ListFilesParams = Type.Object({
  path: Type.Optional(Type.String({ description: "Directory path relative to repo root. Defaults to root." })),
});

export function createListFilesTool(
  repoDir: string,
): AgentTool<typeof ListFilesParams, { entryCount: number }> {
  const tool = defineTool({
    name: "list_files",
    label: "List Files",
    description: "List files and directories in the cloned source code repository.",
    parameters: ListFilesParams,
    async execute(_toolCallId, params) {
      const dir = join(repoDir, params.path ?? ".");
      if (!isInsideRepo(repoDir, dir)) {
        return {
          content: [{ type: "text" as const, text: "Error: path traversal not allowed." }],
          details: { entryCount: 0 },
        };
      }
      try {
        const entries = await readdir(dir, { withFileTypes: true });
        const listing = entries.map((e) => `${e.isDirectory() ? "d" : "f"} ${e.name}`);
        return {
          content: [{ type: "text" as const, text: listing.join("\n") || "Empty directory." }],
          details: { entryCount: entries.length },
        };
      } catch {
        return {
          content: [{ type: "text" as const, text: `Directory not found: ${params.path ?? "."}` }],
          details: { entryCount: 0 },
        };
      }
    },
  });
  return tool as unknown as AgentTool<typeof ListFilesParams, { entryCount: number }>;
}
