import { createModels, createProvider, type Models, type Provider } from "@earendil-works/pi-ai";
import { openAICompletionsApi } from "@earendil-works/pi-ai/api/openai-completions.lazy";
import type { MlxConfig } from "./config.ts";

/** Build a pi-ai provider for mlx-serve's OpenAI-compatible chat completions endpoint. */
export function mlxProvider(cfg: MlxConfig, baseUrl = cfg.baseUrl): Provider {
	const key = cfg.apiKey;
	return createProvider({
		id: cfg.provider,
		name: "mlx-serve",
		baseUrl,
		auth: {
			apiKey: {
				name: "mlx-serve key (local placeholder)",
				resolve: async () => ({ auth: { apiKey: key }, source: "pdh config" }),
			},
		},
		models: [
			{
				id: cfg.model.id,
				name: cfg.model.name,
				api: "openai-completions",
				provider: cfg.provider,
				baseUrl,
				input: cfg.model.input,
				reasoning: cfg.model.reasoning,
				thinkingLevelMap: cfg.model.thinkingLevelMap as any,
				contextWindow: cfg.model.contextWindow,
				maxTokens: cfg.model.maxTokens,
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
				compat: cfg.compat as any,
			},
		],
		api: openAICompletionsApi(),
	});
}

export function mlxModels(cfg: MlxConfig, baseUrl?: string): Models {
	const models = createModels();
	models.setProvider(mlxProvider(cfg, baseUrl));
	return models;
}
