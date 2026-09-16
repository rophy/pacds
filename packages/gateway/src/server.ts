import Fastify from "fastify";
import type { FastifyInstance } from "fastify";
import { DiagnosticRequestSchema } from "@pacds/shared";
import { validateAgentOutput } from "./validator.js";
import { SessionStore } from "./sessions.js";

export interface GatewayConfig {
  agentEndpoint: string;
  authTokens: string[];
}

export async function createGatewayServer(
  config: GatewayConfig,
): Promise<FastifyInstance> {
  const server = Fastify({ logger: true });
  const sessions = new SessionStore();

  server.get("/healthz", async () => ({ status: "ok" }));

  server.post("/api/v1/diagnose", async (request, reply) => {
    const auth = request.headers.authorization;
    if (!auth || !auth.startsWith("Bearer ")) {
      return reply.status(401).send({ error: "Missing authorization" });
    }
    const token = auth.slice(7);
    if (!config.authTokens.includes(token)) {
      return reply.status(401).send({ error: "Invalid token" });
    }

    const parsed = DiagnosticRequestSchema.safeParse(request.body);
    if (!parsed.success) {
      return reply.status(400).send({ error: "Invalid request", details: parsed.error.message });
    }

    const diagnosticRequest = parsed.data;
    const sessionId = diagnosticRequest.session_id ?? sessions.create(diagnosticRequest.service);
    if (diagnosticRequest.session_id) {
      sessions.touch(diagnosticRequest.session_id);
    }

    let agentResult: unknown;
    try {
      const res = await fetch(config.agentEndpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...diagnosticRequest, session_id: sessionId }),
      });
      agentResult = await res.json();
    } catch (err) {
      request.log.error(err, "Failed to reach diagnostic agent");
      return reply.status(502).send({ error: "Diagnostic agent unavailable" });
    }

    if (
      typeof agentResult !== "object" ||
      agentResult === null ||
      !("type" in agentResult)
    ) {
      request.log.warn("Malformed agent result: missing type");
      return reply.status(502).send({ error: "Malformed diagnostic agent response" });
    }

    if (agentResult.type === "clarification") {
      const clarification = agentResult as { type: "clarification"; question: string; session_id: string };
      return reply.status(200).send({
        type: "clarification",
        question: clarification.question,
        session_id: clarification.session_id,
      });
    }

    const agentResponse =
      agentResult.type === "finding" && "response" in agentResult
        ? (agentResult as { type: "finding"; response: unknown }).response
        : agentResult;

    const validation = validateAgentOutput(agentResponse);
    if (!validation.valid) {
      request.log.warn({ reason: validation.reason }, "Agent output rejected");
      return reply.status(422).send({ error: "Response rejected by output validation", reason: validation.reason });
    }

    request.log.info(
      { session_id: sessionId, service: diagnosticRequest.service },
      "Diagnostic response served",
    );
    return reply.send(validation.response);
  });

  return server;
}
