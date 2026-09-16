import { describe, it, expect, vi } from "vitest";
import { registerVllmProvider } from "./vllm-provider.js";

describe("vllm-provider", () => {
  it("registers a provider with the correct config", () => {
    const mockPi = {
      registerProvider: vi.fn(),
    };

    registerVllmProvider(mockPi as any, {
      baseUrl: "http://vllm:8000/v1",
      modelId: "meta-llama-3.1-70b",
      modelName: "Llama 3.1 70B",
      contextWindow: 128000,
    });

    expect(mockPi.registerProvider).toHaveBeenCalledWith("vllm-local", {
      name: "vLLM Local",
      baseUrl: "http://vllm:8000/v1",
      apiKey: "$VLLM_API_KEY",
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
