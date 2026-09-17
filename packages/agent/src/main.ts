import { createAgentServer } from "./server.js";
import { createLogProvider, type LogProviderConfig } from "./log-provider.js";

const port = parseInt(process.env.PORT ?? "3001", 10);

const logProviderConfig: LogProviderConfig = process.env.LOG_PROVIDER
  ? JSON.parse(process.env.LOG_PROVIDER) as LogProviderConfig
  : { type: "loki", url: process.env.LOKI_URL ?? "http://loki:3100" };

const server = await createAgentServer({
  gitUrl: process.env.GIT_URL ?? "",
  gitToken: process.env.GIT_TOKEN ?? "",
  logProvider: createLogProvider(logProviderConfig),
  serviceRegistry: JSON.parse(process.env.SERVICE_REGISTRY ?? "{}"),
  llmProvider: process.env.LLM_PROVIDER ?? "vllm-local",
  llmModel: process.env.LLM_MODEL ?? "meta-llama-3.1-70b",
  repoBaseDir: process.env.REPO_BASE_DIR ?? "/tmp/repos",
  llmBaseUrl: process.env.LLM_BASE_URL,
  llmContextWindow: process.env.LLM_CONTEXT_WINDOW ? parseInt(process.env.LLM_CONTEXT_WINDOW, 10) : undefined,
});

await server.listen({ port, host: "0.0.0.0" });
