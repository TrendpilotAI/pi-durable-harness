import { createModels, createProvider, type Models, type Provider } from "@earendil-works/pi-ai";
import { openAICompletionsApi } from "@earendil-works/pi-ai/api/openai-completions.lazy";
import type { MlxConfig } from "./config.ts";

/** Per-session sampling overrides; absent fields keep the provider default (nothing is sent). */
export type Sampling = { temperature?: number; seed?: number };

/** The request-body fields for `sampling`, or undefined when there are none. */
export function samplingParams(sampling: Sampling | undefined): Record<string, number> | undefined {
	const out: Record<string, number> = {};
	if (sampling?.temperature !== undefined) out.temperature = sampling.temperature;
	if (sampling?.seed !== undefined) out.seed = sampling.seed;
	return Object.keys(out).length ? out : undefined;
}

/**
 * Build a pi-ai provider for mlx-serve's OpenAI-compatible chat completions endpoint. pi-durable exposes no
 * per-request sampling options, so `sampling` goes on the model as `samplingParams`, which pi-ai's
 * openai-completions adapter merges into every request body (root and sub-agent conversations alike).
 */
export function mlxProvider(cfg: MlxConfig, baseUrl = cfg.baseUrl, sampling?: Sampling): Provider {
	const params = samplingParams(sampling);
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
				...(params ? { samplingParams: params } : {}),
			},
		],
		api: openAICompletionsApi(),
	});
}

export function mlxModels(cfg: MlxConfig, baseUrl?: string, sampling?: Sampling): Models {
	const models = createModels();
	models.setProvider(mlxProvider(cfg, baseUrl, sampling));
	return models;
}
