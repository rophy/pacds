import { describe, it, expect, vi } from "vitest";
import { createFetchLogsTool } from "./fetch-logs.js";

describe("fetch-logs tool", () => {
  it("fetches logs from Loki and returns them", async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        data: {
          result: [
            {
              values: [
                ["1694880000000000000", "ERROR: payment timeout after 30s"],
                ["1694880001000000000", "WARN: retrying payment for order 12345"],
              ],
            },
          ],
        },
      }),
    });

    const tool = createFetchLogsTool("http://loki:3100", mockFetch);

    const result = await tool.execute("call-1", {
      query: '{service="checkout-service"}',
      start: "2026-09-16T00:00:00Z",
      end: "2026-09-16T01:00:00Z",
      limit: 100,
    });

    expect(result.content[0]).toEqual({
      type: "text",
      text: expect.stringContaining("payment timeout"),
    });
    expect(mockFetch).toHaveBeenCalledOnce();
  });

  it("handles Loki errors gracefully", async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      text: async () => "Internal Server Error",
    });

    const tool = createFetchLogsTool("http://loki:3100", mockFetch);

    const result = await tool.execute("call-1", {
      query: '{service="checkout-service"}',
      start: "2026-09-16T00:00:00Z",
      end: "2026-09-16T01:00:00Z",
    });

    expect(result.content[0]).toEqual({
      type: "text",
      text: expect.stringContaining("Failed"),
    });
  });
});
