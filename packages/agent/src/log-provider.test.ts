import { describe, it, expect, vi } from "vitest";
import { createLogProvider } from "./log-provider.js";

describe("LogProvider", () => {
  describe("static", () => {
    it("returns configured lines regardless of params", async () => {
      const provider = createLogProvider({
        type: "static",
        lines: ["ERROR: disk full", "WARN: retrying write"],
      });

      const lines = await provider.fetchLogs({
        start: "2026-09-17T00:00:00Z",
        end: "2026-09-17T01:00:00Z",
        limit: 100,
      });

      expect(lines).toEqual(["ERROR: disk full", "WARN: retrying write"]);
      expect(provider.type).toBe("static");
    });
  });

  describe("loki", () => {
    it("builds LogQL from labels and queries Loki", async () => {
      const mockFetch = vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          data: {
            result: [{
              values: [
                ["1694880000000000000", "ERROR: timeout"],
                ["1694880001000000000", "WARN: retry"],
              ],
            }],
          },
        }),
      });

      const provider = createLogProvider(
        { type: "loki", url: "http://loki:3100" },
        mockFetch,
      );

      const lines = await provider.fetchLogs({
        labels: { service: "checkout", env: "prod" },
        start: "2026-09-17T00:00:00Z",
        end: "2026-09-17T01:00:00Z",
        limit: 50,
      });

      expect(lines).toEqual(["ERROR: timeout", "WARN: retry"]);
      expect(mockFetch).toHaveBeenCalledOnce();

      const calledUrl = new URL(mockFetch.mock.calls[0][0]);
      expect(calledUrl.pathname).toBe("/loki/api/v1/query_range");
      const query = calledUrl.searchParams.get("query")!;
      expect(query).toContain('service="checkout"');
      expect(query).toContain('env="prod"');
    });

    it("passes raw query string when provided", async () => {
      const mockFetch = vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ data: { result: [] } }),
      });

      const provider = createLogProvider(
        { type: "loki", url: "http://loki:3100" },
        mockFetch,
      );

      await provider.fetchLogs({
        query: '{app="gateway"} |= "error"',
        start: "2026-09-17T00:00:00Z",
        end: "2026-09-17T01:00:00Z",
        limit: 100,
      });

      const calledUrl = new URL(mockFetch.mock.calls[0][0]);
      expect(calledUrl.searchParams.get("query")).toBe('{app="gateway"} |= "error"');
    });

    it("throws on non-OK response", async () => {
      const mockFetch = vi.fn().mockResolvedValue({
        ok: false,
        status: 500,
        text: async () => "Internal Server Error",
      });

      const provider = createLogProvider(
        { type: "loki", url: "http://loki:3100" },
        mockFetch,
      );

      await expect(provider.fetchLogs({
        start: "2026-09-17T00:00:00Z",
        end: "2026-09-17T01:00:00Z",
        limit: 100,
      })).rejects.toThrow("Loki query failed: HTTP 500");
    });
  });

  describe("elasticsearch", () => {
    it("builds bool query from labels and searches index", async () => {
      const mockFetch = vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          hits: {
            hits: [
              { _source: { "@timestamp": "2026-09-17T00:01:00Z", message: "ERROR: null ref" } },
              { _source: { "@timestamp": "2026-09-17T00:02:00Z", message: "WARN: fallback" } },
            ],
          },
        }),
      });

      const provider = createLogProvider(
        { type: "elasticsearch", url: "http://es:9200", index: "app-logs-*" },
        mockFetch,
      );

      const lines = await provider.fetchLogs({
        labels: { service: "checkout" },
        start: "2026-09-17T00:00:00Z",
        end: "2026-09-17T01:00:00Z",
        limit: 50,
      });

      expect(lines).toEqual([
        "2026-09-17T00:01:00Z ERROR: null ref",
        "2026-09-17T00:02:00Z WARN: fallback",
      ]);
      expect(mockFetch).toHaveBeenCalledWith(
        "http://es:9200/app-logs-*/_search",
        expect.objectContaining({ method: "POST" }),
      );
    });
  });

  describe("victorialogs", () => {
    it("queries VictoriaLogs and parses NDJSON", async () => {
      const mockFetch = vi.fn().mockResolvedValue({
        ok: true,
        text: async () => [
          '{"_time":"2026-09-17T00:01:00Z","_msg":"ERROR: timeout"}',
          '{"_time":"2026-09-17T00:02:00Z","_msg":"WARN: retry"}',
          "",
        ].join("\n"),
      });

      const provider = createLogProvider(
        { type: "victorialogs", url: "http://vl:9428" },
        mockFetch,
      );

      const lines = await provider.fetchLogs({
        labels: { service: "checkout" },
        start: "2026-09-17T00:00:00Z",
        end: "2026-09-17T01:00:00Z",
        limit: 100,
      });

      expect(lines).toEqual([
        "2026-09-17T00:01:00Z ERROR: timeout",
        "2026-09-17T00:02:00Z WARN: retry",
      ]);

      const calledUrl = new URL(mockFetch.mock.calls[0][0]);
      expect(calledUrl.pathname).toBe("/select/logsql/query");
      expect(calledUrl.searchParams.get("query")).toContain("service:checkout");
    });
  });
});
