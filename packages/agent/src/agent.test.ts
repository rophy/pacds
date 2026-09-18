import { describe, it, expect, vi, beforeEach } from "vitest";
import type { DiagnosticRequest } from "@pacds/shared";

vi.mock("./vllm-provider.js", () => ({
  registerLlmProvider: vi.fn(),
}));

vi.mock("@earendil-works/pi-coding-agent", () => ({
  ModelRuntime: {
    create: vi.fn().mockResolvedValue({
      getModel: vi.fn().mockReturnValue({ id: "test-model" }),
      registerProvider: vi.fn(),
    }),
  },
  createAgentSession: vi.fn().mockResolvedValue({
    session: {
      prompt: vi.fn().mockResolvedValue(undefined),
      dispose: vi.fn(),
    },
  }),
  DefaultResourceLoader: class {
    reload = vi.fn().mockResolvedValue(undefined);
    constructor() {}
  },
  getAgentDir: vi.fn().mockReturnValue("/tmp/agent-dir"),
  SessionManager: {
    inMemory: vi.fn().mockReturnValue({}),
  },
  defineTool: vi.fn().mockImplementation((def) => def),
}));

import { createDiagnosticAgent } from "./agent.js";
import { createAgentSession } from "@earendil-works/pi-coding-agent";

const baseConfig = {
  gitUrl: "",
  gitToken: "",
  serviceRegistry: { "checkout-service": "teams/commerce/checkout" },
  llmProvider: "test-provider",
  llmModel: "test-model",
  repoBaseDir: "/tmp/repos",
};

const baseRequest: DiagnosticRequest = {
  service: "checkout-service",
  log_query: { time_range: { start: "2026-01-01T00:00:00Z", end: "2026-01-01T01:00:00Z" } },
  log_provider: { type: "static" as const, lines: ["ERROR: test error"] },
  question: "Are these errors from our code?",
  session_id: "sess-test-123",
};

describe("createDiagnosticAgent", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("returns error for unregistered service", async () => {
    const agent = createDiagnosticAgent(baseConfig);
    const result = await agent.run({ ...baseRequest, service: "unknown-service" });
    expect(result.type).toBe("finding");
    if (result.type === "finding") {
      expect(result.response.status).toBe("error");
      expect(result.response.findings).toEqual([]);
      expect(result.response.errors[0]).toContain("not found in registry");
      expect(result.response.confidence).toBe("low");
      expect(result.response.session_id).toBe("sess-test-123");
    }
  });

  it("creates Pi session and runs prompt for valid service", async () => {
    const agent = createDiagnosticAgent(baseConfig);
    const result = await agent.run(baseRequest);

    const mockedCreate = vi.mocked(createAgentSession);
    expect(mockedCreate).toHaveBeenCalled();
    const callOpts = mockedCreate.mock.calls[0][0] as any;
    expect(callOpts.customTools).toHaveLength(7);

    expect(result.type).toBe("finding");
    if (result.type === "finding") {
      expect(result.response.status).toBe("error");
      expect(result.response.session_id).toBe("sess-test-123");
      expect(result.response.confidence).toBe("low");
      expect(result.response.findings).toEqual([]);
      expect(result.response.errors).toHaveLength(1);
      expect(result.response.errors[0]).toContain("no findings");
    }
  });

  it("returns findings when emit_finding tool is called during session", async () => {
    const mockedCreate = vi.mocked(createAgentSession);

    mockedCreate.mockImplementationOnce(async (opts: any) => {
      const emitTool = opts.customTools.find((t: any) => t.name === "emit_finding");
      if (emitTool) {
        await emitTool.execute("call-1", {
          likelihood: "high",
          explanation: "Found a null check issue",
          relevant_area: "payment module",
        });
      }
      return {
        session: {
          prompt: vi.fn().mockResolvedValue(undefined),
          dispose: vi.fn(),
        },
      };
    });

    const agent = createDiagnosticAgent(baseConfig);
    const result = await agent.run(baseRequest);

    expect(result.type).toBe("finding");
    if (result.type === "finding") {
      expect(result.response.status).toBe("ok");
      expect(result.response.findings).toHaveLength(1);
      expect(result.response.findings[0].likelihood).toBe("high");
      expect(result.response.confidence).toBe("high");
    }
  });

  it("returns auth error when LLM returns 401", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response("Unauthorized", { status: 401 }),
    );

    const agent = createDiagnosticAgent({
      ...baseConfig,
      llmBaseUrl: "http://llm:8000/v1",
      llmApiKey: "bad-key",
    });
    const result = await agent.run(baseRequest);

    expect(result.type).toBe("finding");
    if (result.type === "finding") {
      expect(result.response.status).toBe("error");
      expect(result.response.errors[0]).toContain("authentication failed");
      expect(result.response.errors[0]).toContain("401");
    }
    fetchSpy.mockRestore();
  });

  it("returns auth error when LLM returns 403", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response("Forbidden", { status: 403 }),
    );

    const agent = createDiagnosticAgent({
      ...baseConfig,
      llmBaseUrl: "http://llm:8000/v1",
      llmApiKey: "bad-key",
    });
    const result = await agent.run(baseRequest);

    expect(result.type).toBe("finding");
    if (result.type === "finding") {
      expect(result.response.status).toBe("error");
      expect(result.response.errors[0]).toContain("authentication failed");
      expect(result.response.errors[0]).toContain("403");
    }
    fetchSpy.mockRestore();
  });

  it("returns endpoint error when LLM returns 404", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response("Not Found", { status: 404 }),
    );

    const agent = createDiagnosticAgent({
      ...baseConfig,
      llmBaseUrl: "http://llm:8000/v1",
    });
    const result = await agent.run(baseRequest);

    expect(result.type).toBe("finding");
    if (result.type === "finding") {
      expect(result.response.status).toBe("error");
      expect(result.response.errors[0]).toContain("not found");
      expect(result.response.errors[0]).toContain("404");
    }
    fetchSpy.mockRestore();
  });

  it("returns server error when LLM returns 500", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response("Internal Server Error", { status: 500 }),
    );

    const agent = createDiagnosticAgent({
      ...baseConfig,
      llmBaseUrl: "http://llm:8000/v1",
    });
    const result = await agent.run(baseRequest);

    expect(result.type).toBe("finding");
    if (result.type === "finding") {
      expect(result.response.status).toBe("error");
      expect(result.response.errors[0]).toContain("server error");
      expect(result.response.errors[0]).toContain("500");
    }
    fetchSpy.mockRestore();
  });

  it("returns connection error when LLM is unreachable", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockRejectedValueOnce(
      new Error("fetch failed: ECONNREFUSED"),
    );

    const agent = createDiagnosticAgent({
      ...baseConfig,
      llmBaseUrl: "http://llm:8000/v1",
    });
    const result = await agent.run(baseRequest);

    expect(result.type).toBe("finding");
    if (result.type === "finding") {
      expect(result.response.status).toBe("error");
      expect(result.response.errors[0]).toContain("connection failed");
      expect(result.response.errors[0]).toContain("ECONNREFUSED");
    }
    fetchSpy.mockRestore();
  });

  it("returns error when LLM session throws", async () => {
    const mockedCreate = vi.mocked(createAgentSession);
    mockedCreate.mockImplementationOnce(async () => ({
      session: {
        prompt: vi.fn().mockRejectedValue(new Error("model overloaded")),
        dispose: vi.fn(),
      },
    }));

    const agent = createDiagnosticAgent(baseConfig);
    const result = await agent.run(baseRequest);

    expect(result.type).toBe("finding");
    if (result.type === "finding") {
      expect(result.response.status).toBe("error");
      expect(result.response.errors[0]).toContain("LLM session failed");
      expect(result.response.errors[0]).toContain("model overloaded");
    }
  });

  it("uses openai-responses endpoint for responses API type", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response("Unauthorized", { status: 401 }),
    );

    const agent = createDiagnosticAgent({
      ...baseConfig,
      llmBaseUrl: "http://llm:8000/v1",
      llmApiType: "openai-responses",
    });
    await agent.run(baseRequest);

    expect(fetchSpy).toHaveBeenCalledWith(
      "http://llm:8000/v1/responses",
      expect.anything(),
    );
    fetchSpy.mockRestore();
  });

  it("returns clarification when ask_clarification tool is called", async () => {
    const mockedCreate = vi.mocked(createAgentSession);

    mockedCreate.mockImplementationOnce(async (opts: any) => {
      const clarifyTool = opts.customTools.find((t: any) => t.name === "ask_clarification");
      if (clarifyTool) {
        await clarifyTool.execute("call-1", { question: "Which version?" });
      }
      return {
        session: {
          prompt: vi.fn().mockResolvedValue(undefined),
          dispose: vi.fn(),
        },
      };
    });

    const agent = createDiagnosticAgent(baseConfig);
    const result = await agent.run(baseRequest);

    expect(result.type).toBe("clarification");
    if (result.type === "clarification") {
      expect(result.question).toBe("Which version?");
      expect(result.session_id).toBe("sess-test-123");
    }
  });
});
