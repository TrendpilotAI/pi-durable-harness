import { readFileSync, existsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

/** Model settings for the local mlx-serve provider, read from pi's models.json when present. */
export type MlxModelConfig = {
	id: string;
	name: string;
	contextWindow: number;
	maxTokens: number;
	reasoning: boolean;
	input: ("text" | "image")[];
	thinkingLevelMap?: Record<string, string | null>;
};

export type MlxConfig = {
	provider: string;
	baseUrl: string;
	apiKey: string;
	compat: Record<string, unknown>;
	model: MlxModelConfig;
};

export const DEFAULT_MODEL_ID = "ddalcu/Qwen3.6-35B-A3B-MLX-Serve-4bit";
export const DEFAULT_BASE_URL = "http://127.0.0.1:11234/v1";

export function piModelsJsonPath(): string {
	return process.env.PDH_PI_MODELS_JSON ?? join(homedir(), ".mlx-serve", "pi", "models.json");
}

/** Home for durable session stores. Override with PDH_HOME. */
export function harnessHome(): string {
	return process.env.PDH_HOME ?? join(homedir(), ".pi-durable-harness");
}

/**
 * Resolve the mlx provider. Order: env overrides (PDH_BASE_URL, PDH_MODEL, PDH_MAX_TOKENS) over
 * ~/.mlx-serve/pi/models.json provider `mlx` over built-in defaults. The api key is the
 * non-secret placeholder mlx-serve accepts; it is never written to the repo by this code.
 */
export function loadMlxConfig(path = piModelsJsonPath()): MlxConfig {
	let provider: any = undefined;
	if (existsSync(path)) {
		try {
			provider = JSON.parse(readFileSync(path, "utf8"))?.providers?.mlx;
		} catch {
			provider = undefined;
		}
	}
	const wanted = process.env.PDH_MODEL ?? DEFAULT_MODEL_ID;
	const fromFile = (provider?.models ?? []).find((m: any) => m.id === wanted) ?? provider?.models?.[0];
	const model: MlxModelConfig = {
		id: process.env.PDH_MODEL ?? fromFile?.id ?? DEFAULT_MODEL_ID,
		name: fromFile?.name ?? wanted,
		contextWindow: fromFile?.contextWindow ?? 262144,
		maxTokens: Number(process.env.PDH_MAX_TOKENS ?? 0) || Math.min(fromFile?.maxTokens ?? 16384, 16384),
		reasoning: fromFile?.reasoning ?? true,
		input: ["text"],
		thinkingLevelMap: fromFile?.thinkingLevelMap ?? { off: "none" },
	};
	return {
		provider: "mlx",
		baseUrl: process.env.PDH_BASE_URL ?? provider?.baseUrl ?? DEFAULT_BASE_URL,
		apiKey: process.env.PDH_API_KEY ?? provider?.apiKey ?? "mlx-serve",
		compat: provider?.compat ?? { supportsDeveloperRole: false, supportsReasoningEffort: true, maxTokensField: "max_tokens" },
		model,
	};
}
