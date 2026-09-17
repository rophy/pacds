export interface LogFetchParams {
  query?: string;
  labels?: Record<string, string>;
  start: string;
  end: string;
  limit: number;
}

export interface LogProvider {
  readonly type: string;
  fetchLogs(params: LogFetchParams): Promise<string[]>;
}

export interface LokiProviderConfig {
  type: "loki";
  url: string;
}

export interface ElasticsearchProviderConfig {
  type: "elasticsearch";
  url: string;
  index: string;
}

export interface VictorialogsProviderConfig {
  type: "victorialogs";
  url: string;
}

export interface StaticProviderConfig {
  type: "static";
  lines: string[];
}

export type LogProviderConfig =
  | LokiProviderConfig
  | ElasticsearchProviderConfig
  | VictorialogsProviderConfig
  | StaticProviderConfig;

type FetchFn = typeof globalThis.fetch;

export function createLogProvider(
  config: LogProviderConfig,
  fetchFn: FetchFn = globalThis.fetch,
): LogProvider {
  switch (config.type) {
    case "loki":
      return createLokiProvider(config, fetchFn);
    case "elasticsearch":
      return createElasticsearchProvider(config, fetchFn);
    case "victorialogs":
      return createVictorialogsProvider(config, fetchFn);
    case "static":
      return createStaticProvider(config);
  }
}

function createLokiProvider(config: LokiProviderConfig, fetchFn: FetchFn): LogProvider {
  return {
    type: "loki",
    async fetchLogs(params) {
      const logqlQuery = params.query ?? buildLogQLFromLabels(params.labels);
      const url = new URL(`${config.url}/loki/api/v1/query_range`);
      url.searchParams.set("query", logqlQuery);
      url.searchParams.set("start", params.start);
      url.searchParams.set("end", params.end);
      url.searchParams.set("limit", String(params.limit));

      const res = await fetchFn(url.toString());
      if (!res.ok) {
        const body = await res.text();
        throw new Error(`Loki query failed: HTTP ${res.status} — ${body}`);
      }

      const data = await res.json() as {
        data: { result: Array<{ values: Array<[string, string]> }> };
      };

      return data.data.result.flatMap((stream) =>
        stream.values.map(([_ts, line]) => line),
      );
    },
  };
}

function buildLogQLFromLabels(labels?: Record<string, string>): string {
  if (!labels || Object.keys(labels).length === 0) return "{}";
  const selectors = Object.entries(labels).map(([k, v]) => `${k}="${v}"`);
  return `{${selectors.join(", ")}}`;
}

function createElasticsearchProvider(config: ElasticsearchProviderConfig, fetchFn: FetchFn): LogProvider {
  return {
    type: "elasticsearch",
    async fetchLogs(params) {
      const must: unknown[] = [
        { range: { "@timestamp": { gte: params.start, lte: params.end } } },
      ];
      if (params.labels) {
        for (const [k, v] of Object.entries(params.labels)) {
          must.push({ term: { [k]: v } });
        }
      }
      if (params.query) {
        must.push({ query_string: { query: params.query } });
      }

      const res = await fetchFn(`${config.url}/${config.index}/_search`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          size: params.limit,
          sort: [{ "@timestamp": "asc" }],
          query: { bool: { must } },
        }),
      });

      if (!res.ok) {
        const body = await res.text();
        throw new Error(`Elasticsearch query failed: HTTP ${res.status} — ${body}`);
      }

      const data = await res.json() as {
        hits: { hits: Array<{ _source: { message?: string; log?: string; "@timestamp"?: string } }> };
      };

      return data.hits.hits.map((hit) => {
        const msg = hit._source.message ?? hit._source.log ?? JSON.stringify(hit._source);
        const ts = hit._source["@timestamp"] ?? "";
        return ts ? `${ts} ${msg}` : msg;
      });
    },
  };
}

function createVictorialogsProvider(config: VictorialogsProviderConfig, fetchFn: FetchFn): LogProvider {
  return {
    type: "victorialogs",
    async fetchLogs(params) {
      const logsqlQuery = params.query ?? buildLogsQLFromLabels(params.labels);
      const url = new URL(`${config.url}/select/logsql/query`);
      url.searchParams.set("query", logsqlQuery);
      url.searchParams.set("start", params.start);
      url.searchParams.set("end", params.end);
      url.searchParams.set("limit", String(params.limit));

      const res = await fetchFn(url.toString());
      if (!res.ok) {
        const body = await res.text();
        throw new Error(`VictoriaLogs query failed: HTTP ${res.status} — ${body}`);
      }

      const text = await res.text();
      return text
        .split("\n")
        .filter((line) => line.trim() !== "")
        .map((line) => {
          try {
            const parsed = JSON.parse(line) as { _msg?: string; _time?: string };
            const msg = parsed._msg ?? line;
            return parsed._time ? `${parsed._time} ${msg}` : msg;
          } catch {
            return line;
          }
        });
    },
  };
}

function buildLogsQLFromLabels(labels?: Record<string, string>): string {
  if (!labels || Object.keys(labels).length === 0) return "*";
  return Object.entries(labels).map(([k, v]) => `${k}:${v}`).join(" AND ");
}

function createStaticProvider(config: StaticProviderConfig): LogProvider {
  return {
    type: "static",
    async fetchLogs() {
      return config.lines;
    },
  };
}
