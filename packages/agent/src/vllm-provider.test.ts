import { describe, it, expect, vi } from "vitest";
import { registerLlmProvider } from "./vllm-provider.js";

describe("registerLlmProvider", () => {
  it("registers a provider on the model runtime", () => {
    const mockRuntime = {
      registerProvider: vi.fn(),
    };

    registerLlmProvider(mockRuntime as any, {
      providerId: "vllm-local",
      baseUrl: "http://vllm:8000/v1",
      modelId: "meta-llama-3.1-70b",
      modelName: "Llama 3.1 70B",
      contextWindow: 128000,
    });

    expect(mockRuntime.registerProvider).toHaveBeenCalledWith("vllm-local", {
      name: "vllm-local",
      baseUrl: "http://vllm:8000/v1",
      apiKey: "not-needed",
      api: "openai-completions",
      models: [
        expect.objectContaining({
          id: "meta-llama-3.1-70b",
          name: "Llama 3.1 70B",
          contextWindow: 128000,
        }),
      ],
    });
  });
});
