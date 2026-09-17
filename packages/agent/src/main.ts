import { createAgentServer } from "./server.js";

const port = parseInt(process.env.PORT ?? "3001", 10);

const server = await createAgentServer({
  gitUrl: process.env.GIT_URL ?? "",
  gitToken: process.env.GIT_TOKEN ?? "",
  serviceRegistry: JSON.parse(process.env.SERVICE_REGISTRY ?? "{}"),
  llmProvider: process.env.LLM_PROVIDER ?? "vllm-local",
  llmModel: process.env.LLM_MODEL ?? "meta-llama-3.1-70b",
  repoBaseDir: process.env.REPO_BASE_DIR ?? "/tmp/repos",
  llmBaseUrl: process.env.LLM_BASE_URL,
  llmApiKey: process.env.LLM_API_KEY,
  llmContextWindow: process.env.LLM_CONTEXT_WINDOW ? parseInt(process.env.LLM_CONTEXT_WINDOW, 10) : undefined,
});

await server.listen({ port, host: "0.0.0.0" });
