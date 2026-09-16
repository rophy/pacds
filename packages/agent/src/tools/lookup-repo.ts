import { Type } from "typebox";
import { defineTool } from "@earendil-works/pi-coding-agent";
import type { AgentTool } from "./tool-types.js";

const LookupRepoParams = Type.Object({
  service: Type.String({ description: "The service name to look up" }),
});

export function createLookupRepoTool(
  registry: Record<string, string>,
): AgentTool<typeof LookupRepoParams, { repoPath: string }> {
  const tool = defineTool({
    name: "lookup_repo",
    label: "Lookup Repo",
    description: "Resolve a service name to its GitLab project path using the service registry.",
    parameters: LookupRepoParams,
    async execute(_toolCallId, params) {
      const repoPath = registry[params.service];
      if (!repoPath) {
        return {
          content: [{ type: "text" as const, text: `Service "${params.service}" not found in registry.` }],
          details: { repoPath: "" },
        };
      }
      return {
        content: [{ type: "text" as const, text: `Repository: ${repoPath}` }],
        details: { repoPath },
      };
    },
  });
  return tool as unknown as AgentTool<typeof LookupRepoParams, { repoPath: string }>;
}
