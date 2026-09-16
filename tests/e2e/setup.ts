import { createGatewayServer } from "@pacds/gateway";
import type { FastifyInstance } from "fastify";

export async function setupGateway(agentPort: number): Promise<FastifyInstance> {
  return createGatewayServer({
    agentEndpoint: `http://localhost:${agentPort}`,
    authTokens: ["e2e-test-token"],
  });
}
