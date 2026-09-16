import type { Static, TSchema } from "typebox";

/**
 * Narrower view of a Pi `ToolDefinition` that only requires the arguments
 * tests and callers actually need (toolCallId, params). A function with
 * fewer parameters is structurally assignable wherever the full 5-arg
 * `ToolDefinition.execute` signature is expected, so tools typed this way
 * still satisfy `customTools: ToolDefinition[]` in agent.ts.
 */
export interface AgentTool<TParams extends TSchema, TDetails = unknown> {
  name: string;
  label: string;
  description: string;
  parameters: TParams;
  execute(toolCallId: string, params: Static<TParams>): Promise<{
    content: { type: "text"; text: string }[];
    details: TDetails;
    terminate?: boolean;
  }>;
}
