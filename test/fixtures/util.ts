import type { MlxConfig } from "../../src/config.ts";
export function testConfig(): MlxConfig {
	return {
		provider: "mlx",
		baseUrl: "http://127.0.0.1:9/v1",
		apiKey: "test",
		compat: {},
		model: { id: "faux-model", name: "faux", contextWindow: 32000, maxTokens: 1000, reasoning: false, input: ["text"] },
	};
}
