import { Type } from "typebox";
import { defineTool } from "@earendil-works/pi-coding-agent";
import type { AgentTool } from "./tool-types.js";

export interface ClarificationRequest {
  question: string;
}

const AskClarificationParams = Type.Object({
  question: Type.String({ description: "The clarifying question to ask the SRE agent" }),
});

export function createAskClarificationTool(
  clarifications: ClarificationRequest[],
): AgentTool<typeof AskClarificationParams, Record<string, never>> {
  const tool = defineTool({
    name: "ask_clarification",
    label: "Ask Clarification",
    description: "Ask the SRE agent for more context about the incident. Use when you need additional information to answer their question.",
    parameters: AskClarificationParams,
    async execute(_toolCallId, params) {
      clarifications.push({ question: params.question });
      return {
        content: [{ type: "text" as const, text: "Clarification request sent. Waiting for response." }],
        details: {},
        terminate: true,
      };
    },
  });
  return tool as unknown as AgentTool<typeof AskClarificationParams, Record<string, never>>;
}
