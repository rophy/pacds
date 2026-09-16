import { DiagnosticResponseSchema, checkCodeLikeness } from "@pacds/shared";
import type { DiagnosticResponse } from "@pacds/shared";

export interface ValidationResult {
  valid: boolean;
  response?: DiagnosticResponse;
  reason?: string;
}

export function validateAgentOutput(data: unknown): ValidationResult {
  const parsed = DiagnosticResponseSchema.safeParse(data);
  if (!parsed.success) {
    return { valid: false, reason: `schema: ${parsed.error.message}` };
  }

  for (const finding of parsed.data.findings) {
    const likeness = checkCodeLikeness(finding.explanation);
    if (!likeness.passed) {
      return {
        valid: false,
        reason: `code-likeness: explanation flagged for ${likeness.flags.join(", ")}`,
      };
    }
  }

  return { valid: true, response: parsed.data };
}
