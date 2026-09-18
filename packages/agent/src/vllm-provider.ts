import type { ModelRuntime } from "@earendil-works/pi-coding-agent";

export interface LlmProviderConfig {
  providerId: string;
  baseUrl: string;
  modelId: string;
  modelName: string;
  contextWindow?: number;
  apiKey?: string;
  apiType?: string;
}

export function registerLlmProvider(modelRuntime: ModelRuntime, config: LlmProviderConfig): void {
  modelRuntime.registerProvider(config.providerId, {
    name: config.providerId,
    baseUrl: config.baseUrl,
    apiKey: config.apiKey ?? "not-needed",
    api: (config.apiType ?? "openai-completions") as "openai-completions",
    models: [
      {
        id: config.modelId,
        name: config.modelName,
        reasoning: false,
        input: ["text"],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        contextWindow: config.contextWindow ?? 32768,
        maxTokens: 4096,
      },
    ],
  });
}
