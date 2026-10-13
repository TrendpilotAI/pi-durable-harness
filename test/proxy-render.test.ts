import { readFileSync, mkdtempSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { assembleSse, maxConcurrentFromEnv, startLoggingProxy } from "../src/proxy.ts";
import { groupThreads, renderEpisode } from "../src/mass/render.ts";
import { parseTemplate, renderTemplate, DESIGN_MARKER } from "../src/mass/workflow.ts";
import { parseJsonLoose } from "../src/mass/llm.ts";

const sse = [
	{ id: "x", model: "m", choices: [{ index: 0, delta: { role: "assistant", reasoning_content: "think " } }] },
	{ choices: [{ index: 0, delta: { content: "Hel" } }] },
	{ choices: [{ index: 0, delta: { content: "lo", tool_calls: [{ index: 0, id: "c1", type: "function", function: { name: "bash", arguments: '{"command":' } }] } }] },
	{ choices: [{ index: 0, delta: { tool_calls: [{ index: 0, function: { arguments: '"ls"}' } }] }, finish_reason: "tool_calls" }], usage: { prompt_tokens: 3, completion_tokens: 2 } },
].map((o) => `data: ${JSON.stringify(o)}\n\n`).join("") + "data: [DONE]\n\n";

describe("logging proxy (MASS api_proxy port)", () => {
	it("reassembles SSE like api_proxy.assemble_sse", () => {
		const r: any = assembleSse(sse);
		expect(r.object).toBe("chat.completion.reassembled");
		expect(r.choices[0].message).toEqual({ role: "assistant", content: "Hello", reasoning_content: "think ", tool_calls: [{ index: 0, id: "c1", type: "function", function: { name: "bash", arguments: '{"command":"ls"}' } }] });
		expect(r.choices[0].finish_reason).toBe("tool_calls");
		expect(r.usage).toEqual({ prompt_tokens: 3, completion_tokens: 2 });
	});
	it("relays streams unchanged and logs one record per request", async () => {
		const up = createServer((req, res) => {
			let b = "";
			req.on("data", (d) => (b += d));
			req.on("end", () => {
				if (JSON.parse(b).stream) {
					res.writeHead(200, { "content-type": "text/event-stream" });
					res.end(sse);
				} else {
					res.writeHead(200, { "content-type": "application/json" });
					res.end(JSON.stringify({ choices: [{ message: { role: "assistant", content: "ok" } }] }));
				}
			});
		});
		await new Promise<void>((r) => up.listen(0, "127.0.0.1", r));
		const dir = mkdtempSync(join(tmpdir(), "pdh-proxy-"));
		const log = join(dir, "api_requests.jsonl");
		const p = await startLoggingProxy(`http://127.0.0.1:${(up.address() as any).port}/v1`, log);
		const streamed = await (await fetch(`${p.url}/chat/completions`, { method: "POST", body: JSON.stringify({ stream: true, messages: [{ role: "user", content: "hi" }] }) })).text();
		expect(streamed).toBe(sse);
		const plain: any = await (await fetch(`${p.url}/chat/completions`, { method: "POST", body: JSON.stringify({ stream: false, messages: [] }) })).json();
		expect(plain.choices[0].message.content).toBe("ok");
		await p.close();
		up.close();
		const recs = readFileSync(log, "utf8").trim().split("\n").map((l) => JSON.parse(l));
		expect(recs.map((r) => r.seq)).toEqual([0, 1]);
		expect(recs[0].stream).toBe(true);
		expect(recs[0].request.messages[0].content).toBe("hi");
		expect(recs[0].response.choices[0].message.content).toBe("Hello");
		expect(recs[1].stream).toBe(false);
		for (const k of ["ts_start", "ts_start_iso", "method", "path", "seq", "request_bytes", "status", "response_bytes", "duration_s"]) expect(recs[0]).toHaveProperty(k);
	});
});

/** Fake upstream that answers after `delayMs` and records the peak number of requests in flight. */
async function slowUpstream(delayMs: number) {
	let inFlight = 0;
	let peak = 0;
	const server = createServer((req, res) => {
		inFlight++;
		peak = Math.max(peak, inFlight);
		req.resume();
		setTimeout(() => {
			inFlight--;
			res.writeHead(200, { "content-type": "application/json" });
			res.end(JSON.stringify({ choices: [{ message: { role: "assistant", content: "ok" } }] }));
		}, delayMs);
	});
	await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
	return { url: `http://127.0.0.1:${(server.address() as any).port}/v1`, peak: () => peak, close: () => server.close() };
}

describe("logging proxy concurrency limit", () => {
	it("bounds in-flight upstream requests to maxConcurrent", async () => {
		for (const [limit, expectPeak] of [
			[1, 1],
			[2, 2],
			[0, 4],
		]) {
			const up = await slowUpstream(80);
			const dir = mkdtempSync(join(tmpdir(), "pdh-sem-"));
			const p = await startLoggingProxy(up.url, join(dir, "api_requests.jsonl"), { maxConcurrent: limit });
			const rs = await Promise.all([0, 1, 2, 3].map(() => fetch(`${p.url}/chat/completions`, { method: "POST", body: "{}" }).then((r) => r.json())));
			expect(rs.every((r: any) => r.choices[0].message.content === "ok")).toBe(true);
			expect(up.peak()).toBe(expectPeak);
			await p.close();
			up.close();
			const recs = readFileSync(join(dir, "api_requests.jsonl"), "utf8").trim().split("\n").map((l) => JSON.parse(l));
			expect(recs.length).toBe(4);
			expect(recs.some((r) => "queue_wait_s" in r)).toBe(limit > 0);
		}
	});
	it("reads PDH_MAX_CONCURRENT (default unlimited) and rejects bad values", () => {
		expect(maxConcurrentFromEnv({})).toBe(0);
		expect(maxConcurrentFromEnv({ PDH_MAX_CONCURRENT: "2" })).toBe(2);
		expect(() => maxConcurrentFromEnv({ PDH_MAX_CONCURRENT: "-1" })).toThrow(/non-negative integer/);
		expect(() => maxConcurrentFromEnv({ PDH_MAX_CONCURRENT: "x" })).toThrow(/non-negative integer/);
	});
});

describe("logging proxy upstream failure mid-stream", () => {
	it("logs the partial response with upstream_error and fails the client connection", async () => {
		const first = sse.split("\n\n").slice(0, 2).join("\n\n") + "\n\n"; // role/reasoning + "Hel"
		const up = createServer((req, res) => {
			req.resume();
			res.writeHead(200, { "content-type": "text/event-stream" });
			res.write(first, () => setTimeout(() => res.socket?.destroy(), 30));
		});
		await new Promise<void>((r) => up.listen(0, "127.0.0.1", r));
		const dir = mkdtempSync(join(tmpdir(), "pdh-midstream-"));
		const log = join(dir, "api_requests.jsonl");
		const p = await startLoggingProxy(`http://127.0.0.1:${(up.address() as any).port}/v1`, log);
		const resp = await fetch(`${p.url}/chat/completions`, { method: "POST", body: JSON.stringify({ stream: true, messages: [] }) });
		expect(resp.status).toBe(200);
		await expect(resp.text()).rejects.toThrow();
		await p.close();
		up.close();
		const recs = readFileSync(log, "utf8").trim().split("\n").map((l) => JSON.parse(l));
		expect(recs.length).toBe(1);
		expect(recs[0].stream).toBe(true);
		expect(recs[0].upstream_error).toBeTruthy();
		expect(recs[0].response.choices[0].message.content).toBe("Hel");
		expect(recs[0].response.choices[0].finish_reason).toBeNull();
	});
});

describe("rendering (prepare_sft_data port)", () => {
	it("groups threads by prefix and swaps the teacher prompt for the bare task", () => {
		const dir = mkdtempSync(join(tmpdir(), "pdh-render-"));
		const sys = { role: "system", content: "S" };
		const teacher = "TASK + TEAM + DIRECTIVE";
		const orch1 = [sys, { role: "user", content: teacher }];
		const orch2 = [...orch1, { role: "assistant", content: "", tool_calls: [{ id: "1", type: "function", function: { name: "agent", arguments: "{}" } }] }, { role: "tool", tool_call_id: "1", content: "report" }];
		const worker = [sys, { role: "user", content: "worker task" }];
		const rec = (seq: number, messages: any[], content: string) => ({ seq, path: "/v1/chat/completions", status: 200, request: { messages, tools: [{ type: "function" }] }, response: { choices: [{ message: { role: "assistant", content } }] } });
		const truncated = { ...rec(3, [...orch2, { role: "assistant", content: "final" }, { role: "user", content: "x" }], "trunc"), upstream_error: "terminated" };
		writeFileSync(join(dir, "api_requests.jsonl"), [rec(0, orch1, "delegating"), rec(1, worker, "worker done"), rec(2, orch2, "final"), truncated].map((r) => JSON.stringify(r)).join("\n") + "\n");
		const chats = [rec(0, orch1, ""), rec(2, orch2, "")];
		expect(groupThreads(chats).length).toBe(1);
		const rows = renderEpisode(dir, teacher, "BARE TASK", { episode: "e" });
		const orch = rows.find((r) => r.role === "orchestrator");
		expect(orch.messages[1].content).toBe("BARE TASK");
		expect(orch.messages.at(-1)).toEqual({ role: "assistant", content: "final" });
		expect(orch.n_requests).toBe(2);
		expect(rows.find((r) => r.role === "worker").messages[1].content).toBe("worker task");
		// Without a system message (pi-durable with no sections) the first and later requests still group.
		const u0 = [{ role: "user", content: "T" }];
		const u1 = [...u0, { role: "assistant", content: "a" }, { role: "user", content: "more" }];
		expect(groupThreads([rec(0, u0, ""), rec(1, u1, "")]).length).toBe(1);
	});
});

describe("workflow template", () => {
	it("replaces only the design block", () => {
		const raw = `Task text.\n\n${DESIGN_MARKER}\n[{"agent_id":"a"}]\n\n## Execution requirements\n1. x`;
		const t = parseTemplate(raw);
		expect(t.beforeDesign).toBe("Task text.\n\n");
		const out = renderTemplate(t, '[{"agent_id":"b"}]');
		expect(out).toBe(`Task text.\n\n${DESIGN_MARKER}\n[{"agent_id":"b"}]\n\n## Execution requirements\n1. x`);
	});
	it("parses JSON with think blocks and fences", () => {
		expect(parseJsonLoose('<think>hmm</think>\n```json\n{"winner":"A"}\n```')).toEqual({ winner: "A" });
	});
});
