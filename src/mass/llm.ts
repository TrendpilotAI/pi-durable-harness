import type { MlxConfig } from "../config.ts";

export type JsonCallResult = { payload: any; metrics: Record<string, unknown> };

export const MAX_ATTEMPTS = 3;
export const RETRY_BASE_MS = 1000;

class HttpError extends Error {
	readonly status: number;
	constructor(status: number, body: string) {
		super(`HTTP ${status}: ${body.slice(0, 300)}`);
		this.status = status;
	}
}

/** Client errors are final, except request timeout and rate limiting. */
const retryable = (e: unknown) => !(e instanceof HttpError && e.status >= 400 && e.status < 500 && e.status !== 408 && e.status !== 429);

/** Exponential backoff with jitter: base * 2^(attempt-1), scaled by a factor in [0.5, 1). */
export const backoffMs = (attempt: number, base = RETRY_BASE_MS, random = Math.random) => Math.round(base * 2 ** (attempt - 1) * (0.5 + random() / 2));

const realSleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/** Strip a leading <think>...</think> block and code fences, then parse the first JSON object. */
export function parseJsonLoose(text: string): any {
	let t = text.replace(/<think>[\s\S]*?<\/think>/g, "").trim();
	const fence = t.match(/```(?:json)?\s*([\s\S]*?)```/);
	if (fence) t = fence[1].trim();
	try {
		return JSON.parse(t);
	} catch {
		const s = t.indexOf("{");
		const e = t.lastIndexOf("}");
		if (s >= 0 && e > s) return JSON.parse(t.slice(s, e + 1));
		throw new Error(`model did not return JSON: ${t.slice(0, 200)}`);
	}
}

/**
 * One JSON-producing call to the same local model (MASS JsonLLMBackend analogue). Used by the in-loop
 * evaluator and the optimizer; weights are fixed, so executor = optimizer = evaluator.
 */
export async function callJson(
	cfg: MlxConfig,
	system: string,
	user: string,
	opts: {
		schema?: Record<string, unknown>;
		maxTokens?: number;
		temperature?: number;
		seed?: number;
		thinking?: boolean;
		fetchImpl?: typeof fetch;
		/** Injectable for tests. */
		sleep?: (ms: number) => Promise<void>;
		retryBaseMs?: number;
	} = {},
): Promise<JsonCallResult> {
	const f = opts.fetchImpl ?? fetch;
	const sleep = opts.sleep ?? realSleep;
	const t0 = Date.now();
	const body: Record<string, unknown> = {
		model: cfg.model.id,
		messages: [
			{ role: "system", content: system },
			{ role: "user", content: user },
		],
		max_tokens: opts.maxTokens ?? 4096,
		temperature: opts.temperature ?? 0.6,
		top_p: 0.95,
		stream: false,
		...(opts.seed !== undefined ? { seed: opts.seed } : {}),
		...(opts.thinking === false ? { reasoning_effort: "none" } : {}),
		...(opts.schema ? { response_format: { type: "json_schema", json_schema: { name: "result", schema: opts.schema, strict: true } } } : {}),
	};
	let lastErr: unknown;
	let attempt = 0;
	while (attempt < MAX_ATTEMPTS) {
		if (attempt > 0) await sleep(backoffMs(attempt, opts.retryBaseMs));
		attempt++;
		try {
			const r = await f(`${cfg.baseUrl.replace(/\/$/, "")}/chat/completions`, {
				method: "POST",
				headers: { "content-type": "application/json", authorization: `Bearer ${cfg.apiKey}` },
				body: JSON.stringify(body),
			});
			if (!r.ok) throw new HttpError(r.status, await r.text());
			const j: any = await r.json();
			const content: string = j.choices?.[0]?.message?.content ?? "";
			return { payload: parseJsonLoose(content), metrics: { usage: j.usage ?? null, duration_s: (Date.now() - t0) / 1000, attempt: attempt - 1, model: cfg.model.id } };
		} catch (e) {
			lastErr = e;
			if (!retryable(e)) break;
		}
	}
	throw new Error(`callJson failed after ${attempt} attempt(s): ${(lastErr as Error)?.message ?? String(lastErr)}`, { cause: lastErr });
}
