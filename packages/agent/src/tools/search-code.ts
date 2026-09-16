import { Type } from "typebox";
import { defineTool } from "@earendil-works/pi-coding-agent";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import type { AgentTool } from "./tool-types.js";

const execFileAsync = promisify(execFile);

const SearchCodeParams = Type.Object({
  pattern: Type.String({ description: "Search pattern (regex)" }),
  file_glob: Type.Optional(Type.String({ description: "File glob to restrict search, e.g. '*.ts'" })),
});

export function createSearchCodeTool(
  repoDir: string,
): AgentTool<typeof SearchCodeParams, { matchCount: number }> {
  const tool = defineTool({
    name: "search_code",
    label: "Search Code",
    description: "Search for a pattern in the cloned source code repository using grep.",
    parameters: SearchCodeParams,
    async execute(_toolCallId, params) {
      try {
        const args = ["-rn", "--include", params.file_glob ?? "*", params.pattern, "."];
        const { stdout } = await execFileAsync("grep", args, {
          cwd: repoDir,
          maxBuffer: 1024 * 1024,
        });
        const lines = stdout.split("\n").filter(Boolean).slice(0, 50);
        return {
          content: [{ type: "text" as const, text: lines.join("\n") || "No matches found." }],
          details: { matchCount: lines.length },
        };
      } catch {
        return {
          content: [{ type: "text" as const, text: "No matches found." }],
          details: { matchCount: 0 },
        };
      }
    },
  });
  return tool as unknown as AgentTool<typeof SearchCodeParams, { matchCount: number }>;
}
