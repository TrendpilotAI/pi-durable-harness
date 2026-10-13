import { describe, expect, it } from "vitest";
import { backoffMs, callJson } from "../src/mass/llm.ts";
import { testConfig } from "./fixtures/util.ts";

const ok = (content: string) => new Response(JSON.stringify({ choices: [{ message: { content } }], usage: { total_tokens: 1 } }), { status: 200, headers: { "content-type": "application/json" } });
const status = (code: number) => new Response("nope", { status: code });

/** A fake fetch that replays `responses` in order (a thrown Error simulates a network failure). */
function fakeFetch(responses: (Response | Error)[]) {
	const calls: any[] = [];
	const f = (async (url: string, init: any) => {
		calls.push({ url, body: JSON.parse(init.body) });
		const r = responses[calls.length - 1];
		if (r instanceof Error) throw r;
		return r;
	}) as unknown as typeof fetch;
	return { f, calls };
}

function recorder() {
	const sleeps: number[] = [];
	return { sleeps, sleep: async (ms: number) => void sleeps.push(ms) };
}

describe("callJson retries", () => {
	it("backs off exponentially with jitter and succeeds on the third attempt", async () => {
		const { f, calls } = fakeFetch([status(503), new Error("ECONNRESET"), ok('{"winner":"A"}')]);
		const { sleeps, sleep } = recorder();
		const r = await callJson(testConfig(), "s", "u", { fetchImpl: f, sleep });
		expect(r.payload).toEqual({ winner: "A" });
		expect(r.metrics.attempt).toBe(2);
		expect(calls.length).toBe(3);
		expect(sleeps.length).toBe(2);
		expect(sleeps[0]).toBeGreaterThanOrEqual(500);
		expect(sleeps[0]).toBeLessThan(1000);
		expect(sleeps[1]).toBeGreaterThanOrEqual(1000);
		expect(sleeps[1]).toBeLessThan(2000);
	});
	it("stops after 3 attempts and reports the attempt count", async () => {
		const { f, calls } = fakeFetch([status(500), status(502), ok("not json at all")]);
		const { sleeps, sleep } = recorder();
		await expect(callJson(testConfig(), "s", "u", { fetchImpl: f, sleep })).rejects.toThrow(/after 3 attempt\(s\): model did not return JSON/);
		expect(calls.length).toBe(3);
		expect(sleeps.length).toBe(2);
	});
	it("does not retry a 4xx other than 408/429", async () => {
		const { f, calls } = fakeFetch([status(400), ok("{}")]);
		const { sleeps, sleep } = recorder();
		await expect(callJson(testConfig(), "s", "u", { fetchImpl: f, sleep })).rejects.toThrow(/after 1 attempt\(s\): HTTP 400/);
		expect(calls.length).toBe(1);
		expect(sleeps).toEqual([]);
	});
	it("retries 408 and 429", async () => {
		const { f, calls } = fakeFetch([status(429), status(408), ok('{"x":1}')]);
		const r = await callJson(testConfig(), "s", "u", { fetchImpl: f, ...recorder() });
		expect(r.payload).toEqual({ x: 1 });
		expect(calls.length).toBe(3);
	});
	it("backoff bounds", () => {
		expect(backoffMs(1, 1000, () => 0)).toBe(500);
		expect(backoffMs(3, 1000, () => 0.999999)).toBe(4000);
	});
});
