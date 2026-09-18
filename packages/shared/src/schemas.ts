import { z } from "zod";

const LogQuerySchema = z.object({
  trace_id: z.string().optional(),
  time_range: z.object({
    start: z.string(),
    end: z.string(),
  }),
  labels: z.record(z.string(), z.string()).optional(),
});

const LokiProviderSchema = z.object({
  type: z.literal("loki"),
  url: z.string().min(1),
});

const ElasticsearchProviderSchema = z.object({
  type: z.literal("elasticsearch"),
  url: z.string().min(1),
  index: z.string().min(1),
});

const VictorialogsProviderSchema = z.object({
  type: z.literal("victorialogs"),
  url: z.string().min(1),
});

const StaticProviderSchema = z.object({
  type: z.literal("static"),
  lines: z.array(z.string()),
});

export const LogProviderConfigSchema = z.discriminatedUnion("type", [
  LokiProviderSchema,
  ElasticsearchProviderSchema,
  VictorialogsProviderSchema,
  StaticProviderSchema,
]);

export const DiagnosticRequestSchema = z.object({
  service: z.string().min(1),
  log_query: LogQuerySchema,
  log_provider: LogProviderConfigSchema,
  question: z.string().min(1),
  session_id: z.string().optional(),
});

const FindingSchema = z.object({
  likelihood: z.enum(["high", "medium", "low", "uncertain"]),
  explanation: z.string().max(500),
  relevant_area: z.string().max(100),
});

export const DiagnosticResponseSchema = z.object({
  status: z.enum(["ok", "error"]),
  findings: z.array(FindingSchema),
  errors: z.array(z.string()).default([]),
  session_id: z.string(),
  confidence: z.enum(["high", "medium", "low"]),
});

export type DiagnosticRequest = z.infer<typeof DiagnosticRequestSchema>;
export type DiagnosticResponse = z.infer<typeof DiagnosticResponseSchema>;
export type Finding = z.infer<typeof FindingSchema>;
export type LogQuery = z.infer<typeof LogQuerySchema>;
export type LogProviderConfig = z.infer<typeof LogProviderConfigSchema>;

export function validateResponse(data: unknown): DiagnosticResponse {
  return DiagnosticResponseSchema.parse(data);
}
