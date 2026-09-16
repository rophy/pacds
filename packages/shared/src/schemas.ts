import { z } from "zod";

const LogQuerySchema = z.object({
  trace_id: z.string().optional(),
  time_range: z.object({
    start: z.string(),
    end: z.string(),
  }),
  labels: z.record(z.string(), z.string()).optional(),
});

export const DiagnosticRequestSchema = z.object({
  service: z.string().min(1),
  log_query: LogQuerySchema,
  question: z.string().min(1),
  session_id: z.string().optional(),
});

const FindingSchema = z.object({
  likelihood: z.enum(["high", "medium", "low", "uncertain"]),
  explanation: z.string().max(500),
  relevant_area: z.string().max(100),
});

export const DiagnosticResponseSchema = z.object({
  findings: z.array(FindingSchema),
  session_id: z.string(),
  confidence: z.enum(["high", "medium", "low"]),
});

export type DiagnosticRequest = z.infer<typeof DiagnosticRequestSchema>;
export type DiagnosticResponse = z.infer<typeof DiagnosticResponseSchema>;
export type Finding = z.infer<typeof FindingSchema>;
export type LogQuery = z.infer<typeof LogQuerySchema>;

export function validateResponse(data: unknown): DiagnosticResponse {
  return DiagnosticResponseSchema.parse(data);
}
