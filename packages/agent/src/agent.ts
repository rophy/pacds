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

export interface AgentConfig {
  gitUrl: string;
  gitToken: string;
  serviceRegistry: Record<string, string>;
  llmProvider: string;
  llmModel: string;
  repoBaseDir: string;
  llmBaseUrl?: string;
  llmApiKey?: string;
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
      const repoPath = config.serviceRegistry[request.service];
      if (!repoPath) {
        return {
          type: "finding",
          response: {
            findings: [{
              likelihood: "uncertain",
              explanation: `Service "${request.service}" not found in registry.`,
              relevant_area: "unknown",
            }],
            session_id: request.session_id!,
            confidence: "low",
          },
        };
      }

      const repoDir = `${config.repoBaseDir}/${request.service}`;

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

      const modelRuntime = await ModelRuntime.create();
      if (config.llmBaseUrl) {
        registerLlmProvider(modelRuntime, {
          providerId: config.llmProvider,
          baseUrl: config.llmBaseUrl,
          modelId: config.llmModel,
          modelName: config.llmModel,
          contextWindow: config.llmContextWindow,
          apiKey: config.llmApiKey,
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

      await session.prompt(prompt);
      session.dispose();

      if (clarifications.length > 0) {
        return {
          type: "clarification",
          question: clarifications[0].question,
          session_id: request.session_id!,
        };
      }

      const sessionId = request.session_id!;
      const confidence = findings.length > 0 ? "high" : "low";

      return {
        type: "finding",
        response: {
          findings,
          session_id: sessionId,
          confidence: confidence as "high" | "medium" | "low",
        },
      };
    },
  };
}
