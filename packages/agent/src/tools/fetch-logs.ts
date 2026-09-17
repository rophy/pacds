import { Type } from "typebox";
import { defineTool } from "@earendil-works/pi-coding-agent";
import type { LogProvider } from "../log-provider.js";
import type { AgentTool } from "./tool-types.js";

const FetchLogsParams = Type.Object({
  query: Type.Optional(Type.String({ description: "Free-text query or filter expression for the log backend" })),
  labels: Type.Optional(Type.Record(Type.String(), Type.String(), { description: "Key-value labels to filter logs" })),
  start: Type.String({ description: "Start time in RFC3339 format" }),
  end: Type.String({ description: "End time in RFC3339 format" }),
  limit: Type.Optional(Type.Integer({ description: "Max log lines to return", minimum: 1, maximum: 1000 })),
});

export function createFetchLogsTool(
  logProvider: LogProvider,
): AgentTool<typeof FetchLogsParams, { lineCount: number }> {
  const tool = defineTool({
    name: "fetch_logs",
    label: "Fetch Logs",
    description: "Fetch production logs for a given time range. Optionally filter by labels or a query expression.",
    parameters: FetchLogsParams,
    async execute(_toolCallId, params) {
      try {
        const lines = await logProvider.fetchLogs({
          query: params.query,
          labels: params.labels,
          start: params.start,
          end: params.end,
          limit: params.limit ?? 100,
        });

        if (lines.length === 0) {
          return {
            content: [{ type: "text" as const, text: "No log lines matched the query." }],
            details: { lineCount: 0 },
          };
        }

        return {
          content: [{ type: "text" as const, text: lines.join("\n") }],
          details: { lineCount: lines.length },
        };
      } catch (err) {
        return {
          content: [{ type: "text" as const, text: `Failed to fetch logs: ${err}` }],
          details: { lineCount: 0 },
        };
      }
    },
  });
  return tool as unknown as AgentTool<typeof FetchLogsParams, { lineCount: number }>;
}
