// Minimal OpenAI-compatible chat completions server (SSE) for tests; records every request body.
import { createServer } from "node:http";

const chunk = (o: unknown) => `data: ${JSON.stringify(o)}\n\n`;

/** `reply(body)` returns the assistant text, or `{ tool: name, args }` for one tool call. */
export async function fakeOpenAI(reply: (body: any) => string | { tool: string; args: Record<string, unknown> }) {
	const bodies: any[] = [];
	const server = createServer((req, res) => {
		let b = "";
		req.on("data", (d) => (b += d));
		req.on("end", () => {
			const body = JSON.parse(b);
			bodies.push(body);
			const r = reply(body);
			res.writeHead(200, { "content-type": "text/event-stream" });
			const base = { id: `c${bodies.length}`, object: "chat.completion.chunk", created: 0, model: body.model };
			if (typeof r === "string") {
				res.write(chunk({ ...base, choices: [{ index: 0, delta: { role: "assistant", content: r } }] }));
				res.write(chunk({ ...base, choices: [{ index: 0, delta: {}, finish_reason: "stop" }] }));
			} else {
				const tc = { index: 0, id: `call_${bodies.length}`, type: "function", function: { name: r.tool, arguments: JSON.stringify(r.args) } };
				res.write(chunk({ ...base, choices: [{ index: 0, delta: { role: "assistant", tool_calls: [tc] } }] }));
				res.write(chunk({ ...base, choices: [{ index: 0, delta: {}, finish_reason: "tool_calls" }] }));
			}
			res.write(chunk({ ...base, choices: [], usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 } }));
			res.end("data: [DONE]\n\n");
		});
	});
	await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
	return { url: `http://127.0.0.1:${(server.address() as any).port}/v1`, bodies, close: () => new Promise<void>((r) => server.close(() => r())) };
}
