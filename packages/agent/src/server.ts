import Fastify from "fastify";
import type { FastifyInstance } from "fastify";
import { DiagnosticRequestSchema } from "@pacds/shared";
import { createDiagnosticAgent, type AgentConfig } from "./agent.js";

export async function createAgentServer(config: AgentConfig): Promise<FastifyInstance> {
  const server = Fastify({ logger: true });
  const agent = createDiagnosticAgent(config);

  server.get("/healthz", async () => ({ status: "ok" }));

  server.post("/", async (request, reply) => {
    const parsed = DiagnosticRequestSchema.safeParse(request.body);
    if (!parsed.success) {
      return reply.status(400).send({ error: "Invalid request", details: parsed.error.message });
    }

    try {
      const result = await agent.run(parsed.data);
      return reply.send(result);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      request.log.error(err, "Agent execution failed");
      return reply.status(500).send({
        type: "finding",
        response: {
          status: "error",
          findings: [],
          errors: [`Agent execution failed: ${message}`],
          session_id: parsed.data.session_id ?? "",
          confidence: "low",
        },
      });
    }
  });

  return server;
}
