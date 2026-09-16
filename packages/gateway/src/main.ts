import { createGatewayServer } from "./server.js";

const port = parseInt(process.env.PORT ?? "3000", 10);
const server = await createGatewayServer({
  agentEndpoint: process.env.AGENT_ENDPOINT ?? "http://localhost:3001",
  authTokens: (process.env.AUTH_TOKENS ?? "").split(",").filter(Boolean),
});

await server.listen({ port, host: "0.0.0.0" });
