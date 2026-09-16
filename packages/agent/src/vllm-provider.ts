import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export interface VllmConfig {
  baseUrl: string;
  modelId: string;
  modelName: string;
  contextWindow: number;
}

export function registerVllmProvider(pi: ExtensionAPI, config: VllmConfig): void {
  pi.registerProvider("vllm-local", {
    name: "vLLM Local",
    baseUrl: config.baseUrl,
    apiKey: "$VLLM_API_KEY",
    api: "openai-completions",
    models: [
      {
        id: config.modelId,
        name: config.modelName,
        reasoning: false,
        input: ["text"],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        contextWindow: config.contextWindow,
        maxTokens: 4096,
      },
    ],
  });
}
