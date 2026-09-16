import { createAgentServer } from "./server.js";

const port = parseInt(process.env.PORT ?? "3001", 10);
const server = await createAgentServer({
  gitUrl: process.env.GIT_URL ?? "",
  gitToken: process.env.GIT_TOKEN ?? "",
  lokiUrl: process.env.LOKI_URL ?? "http://loki:3100",
  serviceRegistry: JSON.parse(process.env.SERVICE_REGISTRY ?? "{}"),
  llmProvider: process.env.LLM_PROVIDER ?? "vllm-local",
  llmModel: process.env.LLM_MODEL ?? "meta-llama-3.1-70b",
  repoBaseDir: process.env.REPO_BASE_DIR ?? "/tmp/repos",
});

await server.listen({ port, host: "0.0.0.0" });
