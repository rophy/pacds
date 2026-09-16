import { Type } from "typebox";
import { defineTool } from "@earendil-works/pi-coding-agent";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import type { AgentTool } from "./tool-types.js";

const ReadFileParams = Type.Object({
  path: Type.String({ description: "File path relative to the repo root" }),
  start_line: Type.Optional(Type.Integer({ description: "Start line (1-indexed)", minimum: 1 })),
  end_line: Type.Optional(Type.Integer({ description: "End line (1-indexed)", minimum: 1 })),
});

export function createReadFileTool(
  repoDir: string,
): AgentTool<typeof ReadFileParams, { lineCount: number }> {
  const tool = defineTool({
    name: "read_file",
    label: "Read File",
    description: "Read a file from the cloned source code repository.",
    parameters: ReadFileParams,
    async execute(_toolCallId, params) {
      const fullPath = join(repoDir, params.path);
      if (!fullPath.startsWith(repoDir)) {
        return {
          content: [{ type: "text" as const, text: "Error: path traversal not allowed." }],
          details: { lineCount: 0 },
        };
      }
      try {
        const content = await readFile(fullPath, "utf-8");
        let lines = content.split("\n");
        if (params.start_line || params.end_line) {
          const start = (params.start_line ?? 1) - 1;
          const end = params.end_line ?? lines.length;
          lines = lines.slice(start, end);
        }
        const numbered = lines.map((line, i) => {
          const lineNum = (params.start_line ?? 1) + i;
          return `${lineNum}\t${line}`;
        });
        return {
          content: [{ type: "text" as const, text: numbered.join("\n") }],
          details: { lineCount: lines.length },
        };
      } catch {
        return {
          content: [{ type: "text" as const, text: `File not found: ${params.path}` }],
          details: { lineCount: 0 },
        };
      }
    },
  });
  return tool as unknown as AgentTool<typeof ReadFileParams, { lineCount: number }>;
}
