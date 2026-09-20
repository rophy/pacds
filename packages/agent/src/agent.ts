import {
  createAgentSession,
  DefaultResourceLoader,
  getAgentDir,
  ModelRuntime,
  SessionManager,
} from "@earendil-works/pi-coding-agent";
import { registerLlmProvider } from "./vllm-provider.js";
import { createLogProvider } from "./log-provider.js";
import type { DiagnosticRequest, DiagnosticResponse, Finding } from "@pacds/shared";
import { createLookupRepoTool } from "./tools/lookup-repo.js";
import { createFetchLogsTool } from "./tools/fetch-logs.js";
import { createSearchCodeTool } from "./tools/search-code.js";
import { createReadFileTool } from "./tools/read-file.js";
import { createListFilesTool } from "./tools/list-files.js";
import { createEmitFindingTool } from "./tools/emit-finding.js";
import {
  createAskClarificationTool,
  type ClarificationRequest,
} from "./tools/ask-clarification.js";
import { buildSystemPrompt } from "./system-prompt.js";
import { ensureRepo } from "./repo-cloner.js";

async function checkLlmConnection(config: AgentConfig): Promise<string | null> {
  const apiType = config.llmApiType ?? "openai-completions";
  const endpoint = apiType === "openai-responses"
    ? `${config.llmBaseUrl}/responses`
    : `${config.llmBaseUrl}/chat/completions`;

  try {
    const res = await fetch(endpoint, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(config.llmApiKey ? { Authorization: `Bearer ${config.llmApiKey}` } : {}),
      },
      body: JSON.stringify(
        apiType === "openai-responses"
          ? { model: config.llmModel, input: [{ role: "user", content: "ping" }], max_output_tokens: 1 }
          : { model: config.llmModel, messages: [{ role: "user", content: "ping" }], max_tokens: 1 },
      ),
      signal: AbortSignal.timeout(10_000),
    });

    if (res.status === 401 || res.status === 403) {
      return `LLM authentication failed (HTTP ${res.status}): invalid or expired API key for ${config.llmProvider}`;
    }
    if (res.status === 404) {
      return `LLM endpoint not found (HTTP 404): ${endpoint} — check LLM_BASE_URL and LLM_API_TYPE`;
    }
    if (res.status >= 500) {
      return `LLM provider error (HTTP ${res.status}): ${config.llmProvider} returned a server error`;
    }
    return null;
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return `LLM connection failed: ${message}`;
  }
}

export interface AgentConfig {
  gitUrl: string;
  gitToken: string;
  serviceRegistry: Record<string, string>;
  llmProvider: string;
  llmModel: string;
  repoBaseDir: string;
  llmBaseUrl?: string;
  llmApiKey?: string;
  llmApiType?: string;
  llmContextWindow?: number;
}

export type AgentResult =
  | { type: "finding"; response: DiagnosticResponse }
  | { type: "clarification"; question: string; session_id: string };

export interface DiagnosticAgentRunner {
  run(request: DiagnosticRequest): Promise<AgentResult>;
}

export function createDiagnosticAgent(config: AgentConfig): DiagnosticAgentRunner {
  return {
    async run(request: DiagnosticRequest): Promise<AgentResult> {
      const sessionId = request.session_id!;
      const repoPath = config.serviceRegistry[request.service];
      if (!repoPath) {
        return {
          type: "finding",
          response: {
            status: "error" as const,
            findings: [],
            errors: [`Service "${request.service}" not found in registry.`],
            session_id: sessionId,
            confidence: "low",
          },
        };
      }

      const repoDir = `${config.repoBaseDir}/${request.service}`;

      const cloneError = await ensureRepo({
        repoDir,
        gitUrl: config.gitUrl,
        gitToken: config.gitToken,
        repoPath,
        service: request.service,
      });
      if (cloneError) {
        return {
          type: "finding",
          response: {
            status: "error" as const,
            findings: [],
            errors: [cloneError],
            session_id: sessionId,
            confidence: "low",
          },
        };
      }

      const findings: Finding[] = [];
      const clarifications: ClarificationRequest[] = [];

      const logProvider = createLogProvider(request.log_provider);

      const tools = [
        createLookupRepoTool(config.serviceRegistry),
        createFetchLogsTool(logProvider),
        createSearchCodeTool(repoDir),
        createReadFileTool(repoDir),
        createListFilesTool(repoDir),
        createEmitFindingTool(findings),
        createAskClarificationTool(clarifications),
      ];

      const errors: string[] = [];

      if (config.llmBaseUrl) {
        const preflight = await checkLlmConnection(config);
        if (preflight) {
          errors.push(preflight);
          return {
            type: "finding",
            response: {
              status: "error" as const,
              findings: [],
              errors,
              session_id: sessionId,
              confidence: "low",
            },
          };
        }
      }

      const modelRuntime = await ModelRuntime.create();
      if (config.llmBaseUrl) {
        registerLlmProvider(modelRuntime, {
          providerId: config.llmProvider,
          baseUrl: config.llmBaseUrl,
          modelId: config.llmModel,
          modelName: config.llmModel,
          contextWindow: config.llmContextWindow,
          apiKey: config.llmApiKey,
          apiType: config.llmApiType,
        });
      }
      const model = modelRuntime.getModel(config.llmProvider, config.llmModel);

      const resourceLoader = new DefaultResourceLoader({
        cwd: repoDir,
        agentDir: getAgentDir(),
        systemPrompt: buildSystemPrompt(request.service, repoPath),
        noSkills: true,
        noPromptTemplates: true,
        noThemes: true,
        noContextFiles: true,
      });
      await resourceLoader.reload();

      const { session } = await createAgentSession({
        cwd: repoDir,
        sessionManager: SessionManager.inMemory(),
        modelRuntime,
        model,
        resourceLoader,
        customTools: tools,
        tools: tools.map((t) => t.name),
      });

      const prompt = [
        `Service: ${request.service}`,
        `Log query: ${JSON.stringify(request.log_query)}`,
        `Question: ${request.question}`,
      ].join("\n");

      try {
        const result = await session.prompt(prompt);
        if (findings.length === 0 && clarifications.length === 0) {
          const textContent = typeof result === "string" ? result : JSON.stringify(result);
          errors.push(`Agent produced no findings or clarifications. LLM response: ${textContent?.slice(0, 500) ?? "(empty)"}`);
        }
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        errors.push(`LLM session failed: ${message}`);
      } finally {
        session.dispose();
      }

      if (clarifications.length > 0) {
        return {
          type: "clarification",
          question: clarifications[0].question,
          session_id: sessionId,
        };
      }

      const hasFindings = findings.length > 0;
      const status = errors.length > 0 ? "error" as const : "ok" as const;
      const confidence = hasFindings ? "high" : errors.length > 0 ? "low" : "low";

      return {
        type: "finding",
        response: {
          status,
          findings,
          errors,
          session_id: sessionId,
          confidence: confidence as "high" | "medium" | "low",
        },
      };
    },
  };
}
