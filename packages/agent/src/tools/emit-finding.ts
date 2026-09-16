import { Type } from "typebox";
import { defineTool } from "@earendil-works/pi-coding-agent";
import { StringEnum } from "@earendil-works/pi-ai";
import type { Finding } from "@pacds/shared";
import type { AgentTool } from "./tool-types.js";

const EmitFindingParams = Type.Object({
  likelihood: StringEnum(["high", "medium", "low", "uncertain"] as const),
  explanation: Type.String({ description: "Natural language explanation, max 500 chars", maxLength: 500 }),
  relevant_area: Type.String({ description: "Which part of the codebase is relevant, max 100 chars", maxLength: 100 }),
});

export function createEmitFindingTool(
  findings: Finding[],
): AgentTool<typeof EmitFindingParams, Record<string, never>> {
  const tool = defineTool({
    name: "emit_finding",
    label: "Emit Finding",
    description: "Record a diagnostic finding. Use this to report your conclusion about the code's relationship to the observed errors. This is the ONLY way to return results.",
    parameters: EmitFindingParams,
    async execute(_toolCallId, params) {
      findings.push({
        likelihood: params.likelihood,
        explanation: params.explanation,
        relevant_area: params.relevant_area,
      });
      return {
        content: [{ type: "text" as const, text: "Finding recorded. You may emit more findings or stop." }],
        details: {},
        terminate: true,
      };
    },
  });
  return tool as unknown as AgentTool<typeof EmitFindingParams, Record<string, never>>;
}
