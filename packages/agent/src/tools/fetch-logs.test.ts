import { describe, it, expect, vi } from "vitest";
import { createFetchLogsTool } from "./fetch-logs.js";
import type { LogProvider } from "../log-provider.js";

describe("fetch-logs tool", () => {
  it("fetches logs from provider and returns them", async () => {
    const provider: LogProvider = {
      type: "test",
      fetchLogs: vi.fn().mockResolvedValue([
        "ERROR: payment timeout after 30s",
        "WARN: retrying payment for order 12345",
      ]),
    };

    const tool = createFetchLogsTool(provider);

    const result = await tool.execute("call-1", {
      start: "2026-09-16T00:00:00Z",
      end: "2026-09-16T01:00:00Z",
      labels: { service: "checkout-service" },
      limit: 100,
    });

    expect(result.content[0]).toEqual({
      type: "text",
      text: expect.stringContaining("payment timeout"),
    });
    expect(result.details).toEqual({ lineCount: 2 });
    expect(provider.fetchLogs).toHaveBeenCalledWith({
      query: undefined,
      labels: { service: "checkout-service" },
      start: "2026-09-16T00:00:00Z",
      end: "2026-09-16T01:00:00Z",
      limit: 100,
    });
  });

  it("returns message when no logs match", async () => {
    const provider: LogProvider = {
      type: "test",
      fetchLogs: vi.fn().mockResolvedValue([]),
    };

    const tool = createFetchLogsTool(provider);

    const result = await tool.execute("call-1", {
      start: "2026-09-16T00:00:00Z",
      end: "2026-09-16T01:00:00Z",
    });

    expect(result.content[0]).toEqual({
      type: "text",
      text: "No log lines matched the query.",
    });
    expect(result.details).toEqual({ lineCount: 0 });
  });

  it("handles provider errors gracefully", async () => {
    const provider: LogProvider = {
      type: "test",
      fetchLogs: vi.fn().mockRejectedValue(new Error("connection refused")),
    };

    const tool = createFetchLogsTool(provider);

    const result = await tool.execute("call-1", {
      start: "2026-09-16T00:00:00Z",
      end: "2026-09-16T01:00:00Z",
    });

    expect(result.content[0]).toEqual({
      type: "text",
      text: expect.stringContaining("Failed"),
    });
    expect(result.details).toEqual({ lineCount: 0 });
  });
});
