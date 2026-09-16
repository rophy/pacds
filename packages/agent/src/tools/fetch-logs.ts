import { Type } from "typebox";
import { defineTool } from "@earendil-works/pi-coding-agent";
import type { AgentTool } from "./tool-types.js";

type FetchFn = typeof globalThis.fetch;

const FetchLogsParams = Type.Object({
  query: Type.String({ description: "LogQL query, e.g. {service=\"checkout-service\"}" }),
  start: Type.String({ description: "Start time in RFC3339 format" }),
  end: Type.String({ description: "End time in RFC3339 format" }),
  limit: Type.Optional(Type.Integer({ description: "Max log lines to return", minimum: 1, maximum: 1000 })),
});

export function createFetchLogsTool(
  lokiUrl: string,
  fetchFn: FetchFn = globalThis.fetch,
): AgentTool<typeof FetchLogsParams, { lineCount: number }> {
  const tool = defineTool({
    name: "fetch_logs",
    label: "Fetch Logs",
    description: "Fetch production logs from Loki for a given LogQL query and time range.",
    parameters: FetchLogsParams,
    async execute(_toolCallId, params) {
      const url = new URL(`${lokiUrl}/loki/api/v1/query_range`);
      url.searchParams.set("query", params.query);
      url.searchParams.set("start", params.start);
      url.searchParams.set("end", params.end);
      url.searchParams.set("limit", String(params.limit ?? 100));

      try {
        const res = await fetchFn(url.toString());
        if (!res.ok) {
          const body = await res.text();
          return {
            content: [{ type: "text" as const, text: `Failed to fetch logs: HTTP ${res.status} — ${body}` }],
            details: { lineCount: 0 },
          };
        }

        const data = await res.json() as {
          data: { result: Array<{ values: Array<[string, string]> }> };
        };

        const lines = data.data.result.flatMap((stream) =>
          stream.values.map(([_ts, line]) => line)
        );

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
