import { describe, it, expect, vi, beforeEach } from "vitest";
import type { DiagnosticRequest } from "@pacds/shared";

vi.mock("@earendil-works/pi-coding-agent", () => ({
  ModelRuntime: {
    create: vi.fn().mockResolvedValue({
      getModel: vi.fn().mockReturnValue({ id: "test-model" }),
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
  lokiUrl: "http://loki.test",
  serviceRegistry: { "checkout-service": "teams/commerce/checkout" },
  llmProvider: "test-provider",
  llmModel: "test-model",
  repoBaseDir: "/tmp/repos",
};

const baseRequest: DiagnosticRequest = {
  service: "checkout-service",
  log_query: { time_range: { start: "2026-01-01T00:00:00Z", end: "2026-01-01T01:00:00Z" } },
  question: "Are these errors from our code?",
  session_id: "sess-test-123",
};

describe("createDiagnosticAgent", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("returns unknown-service finding for unregistered service", async () => {
    const agent = createDiagnosticAgent(baseConfig);
    const result = await agent.run({ ...baseRequest, service: "unknown-service" });
    expect(result.type).toBe("finding");
    if (result.type === "finding") {
      expect(result.response.findings[0].likelihood).toBe("uncertain");
      expect(result.response.findings[0].explanation).toContain("not found in registry");
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
      expect(result.response.session_id).toBe("sess-test-123");
      expect(result.response.confidence).toBe("low");
      expect(result.response.findings).toEqual([]);
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
      expect(result.response.findings).toHaveLength(1);
      expect(result.response.findings[0].likelihood).toBe("high");
      expect(result.response.confidence).toBe("high");
    }
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
