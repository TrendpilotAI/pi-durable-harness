/**
 * TypeScript port of MASS runtime/api_proxy.py: a byte-faithful logging reverse proxy for an
 * OpenAI-compatible server. Requests/responses are relayed unchanged; one JSON line per request is
 * appended to the log with the parsed request body and the response, a streamed chat completion
 * reassembled into one message (content, reasoning_content, tool_calls, finish_reason, usage).
 * The record shape matches what MASS training/prepare_sft_data.py reads from api_requests.jsonl.
 * Listens on 127.0.0.1 on an ephemeral port; it never changes the upstream server.
 */
import { appendFileSync, mkdirSync } from "node:fs";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { dirname } from "node:path";

const HOP_BY_HOP = new Set(["connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length"]);

export function assembleSse(raw: string): Record<string, unknown> {
	const choices = new Map<number, any>();
	const meta: Record<string, unknown> = { id: null, model: null, created: null };
	let usage: unknown = null;
	let nChunks = 0;
	let nBad = 0;
	for (let line of raw.split("\n")) {
		line = line.replace(/\r$/, "");
		if (!line.startsWith("data:")) continue;
		const payload = line.slice(5).trim();
		if (!payload || payload === "[DONE]") continue;
		let obj: any;
		try {
			obj = JSON.parse(payload);
		} catch {
			nBad++;
			continue;
		}
		nChunks++;
		for (const k of Object.keys(meta)) if (meta[k] == null && obj[k] != null) meta[k] = obj[k];
		if (obj.usage) usage = obj.usage;
		for (const ch of obj.choices ?? []) {
			const idx = ch.index ?? 0;
			if (!choices.has(idx)) choices.set(idx, { index: idx, role: null, content: [], reasoning_content: [], tool_calls: new Map(), finish_reason: null, stop_reason: null });
			const st = choices.get(idx);
			const delta = ch.delta ?? {};
			if (delta.role) st.role = delta.role;
			if (delta.content) st.content.push(delta.content);
			const rc = delta.reasoning_content ?? delta.reasoning;
			if (rc) st.reasoning_content.push(rc);
			for (const tc of delta.tool_calls ?? []) {
				const ti = tc.index ?? 0;
				if (!st.tool_calls.has(ti)) st.tool_calls.set(ti, { index: ti, id: null, type: null, name: null, arguments: [] });
				const t = st.tool_calls.get(ti);
				if (tc.id) t.id = tc.id;
				if (tc.type) t.type = tc.type;
				const fn = tc.function ?? {};
				if (fn.name) t.name = (t.name ?? "") + fn.name;
				if (fn.arguments) t.arguments.push(fn.arguments);
			}
			if (ch.finish_reason) st.finish_reason = ch.finish_reason;
			if (ch.stop_reason != null) st.stop_reason = ch.stop_reason;
		}
	}
	const outChoices = [...choices.keys()].sort((a, b) => a - b).map((idx) => {
		const st = choices.get(idx);
		const toolCalls = [...st.tool_calls.keys()].sort((a: number, b: number) => a - b).map((ti: number) => {
			const t = st.tool_calls.get(ti);
			return { index: ti, id: t.id, type: t.type ?? "function", function: { name: t.name, arguments: t.arguments.join("") } };
		});
		return {
			index: idx,
			message: {
				role: st.role ?? "assistant",
				content: st.content.length ? st.content.join("") : null,
				reasoning_content: st.reasoning_content.length ? st.reasoning_content.join("") : null,
				tool_calls: toolCalls.length ? toolCalls : null,
			},
			finish_reason: st.finish_reason,
			stop_reason: st.stop_reason,
		};
	});
	return { ...meta, object: "chat.completion.reassembled", choices: outChoices, usage, n_chunks: nChunks, n_bad_chunks: nBad };
}

export type LoggingProxy = { url: string; port: number; close(): Promise<void>; requests(): number };

function readBody(req: IncomingMessage): Promise<Buffer> {
	return new Promise((res, rej) => {
		const chunks: Buffer[] = [];
		req.on("data", (c) => chunks.push(c));
		req.on("end", () => res(Buffer.concat(chunks)));
		req.on("error", rej);
	});
}

export async function startLoggingProxy(upstream: string, logPath: string, opts: { keepRawSse?: boolean; startSeq?: number } = {}): Promise<LoggingProxy> {
	mkdirSync(dirname(logPath), { recursive: true });
	const first = opts.startSeq ?? 0;
	const up = new URL(upstream);
	const upstreamOrigin = `${up.protocol}//${up.host}`;
	let seq = first;
	const write = (rec: unknown) => appendFileSync(logPath, JSON.stringify(rec) + "\n");
	const server: Server = createServer(async (req: IncomingMessage, res: ServerResponse) => {
		const body = await readBody(req);
		const t0 = Date.now() / 1000;
		const rec: Record<string, unknown> = {
			ts_start: t0,
			ts_start_iso: new Date(t0 * 1000).toISOString().replace(/\.\d{3}Z$/, "Z"),
			method: req.method,
			path: req.url,
			seq: seq++,
			request_bytes: body.length,
		};
		if (body.length) {
			try {
				rec.request = JSON.parse(body.toString("utf8"));
			} catch {
				rec.request = null;
				rec.request_raw = body.toString("utf8");
			}
		} else rec.request = null;
		const headers: Record<string, string> = {};
		for (const [k, v] of Object.entries(req.headers)) if (!HOP_BY_HOP.has(k.toLowerCase()) && typeof v === "string") headers[k] = v;
		try {
			const resp = await fetch(upstreamOrigin + req.url, {
				method: req.method,
				headers,
				body: body.length ? new Uint8Array(body) : undefined,
				redirect: "manual",
			});
			const respHeaders: Record<string, string> = {};
			resp.headers.forEach((v, k) => {
				if (!HOP_BY_HOP.has(k.toLowerCase()) && k.toLowerCase() !== "content-encoding") respHeaders[k] = v;
			});
			rec.status = resp.status;
			rec.response_headers = Object.fromEntries(Object.entries(respHeaders).filter(([k]) => ["content-type", "x-request-id"].includes(k.toLowerCase())));
			const ctype = resp.headers.get("content-type") ?? "";
			if (ctype.includes("text/event-stream") && resp.body) {
				res.writeHead(resp.status, respHeaders);
				const chunks: Buffer[] = [];
				let clientGone = false;
				res.on("close", () => {
					if (!res.writableEnded) clientGone = true;
				});
				for await (const chunk of resp.body as any) {
					const b = Buffer.from(chunk);
					chunks.push(b);
					if (!clientGone) {
						try {
							res.write(b);
						} catch (e) {
							clientGone = true;
							rec.client_disconnected = String(e);
						}
					}
				}
				const raw = Buffer.concat(chunks).toString("utf8");
				rec.stream = true;
				rec.response_bytes = Buffer.byteLength(raw);
				rec.response = assembleSse(raw);
				if (opts.keepRawSse) rec.sse_raw = raw;
				rec.duration_s = Math.round((Date.now() / 1000 - t0) * 1000) / 1000;
				write(rec);
				if (!clientGone) res.end();
				return;
			}
			const data = Buffer.from(await resp.arrayBuffer());
			rec.stream = false;
			rec.response_bytes = data.length;
			try {
				rec.response = data.length ? JSON.parse(data.toString("utf8")) : null;
			} catch {
				rec.response_raw = data.toString("utf8");
			}
			rec.duration_s = Math.round((Date.now() / 1000 - t0) * 1000) / 1000;
			write(rec);
			res.writeHead(resp.status, respHeaders);
			res.end(data);
		} catch (e) {
			rec.proxy_error = String(e);
			rec.duration_s = Math.round((Date.now() / 1000 - t0) * 1000) / 1000;
			write(rec);
			if (!res.headersSent) res.writeHead(502, { "content-type": "text/plain" });
			res.end(`proxy error: ${String(e)}`);
		}
	});
	await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
	const port = (server.address() as any).port as number;
	return {
		port,
		url: `http://127.0.0.1:${port}${up.pathname.replace(/\/$/, "")}`,
		requests: () => seq - first,
		close: () =>
			new Promise<void>((r) => {
				server.close(() => r());
				server.closeAllConnections();
			}),
	};
}
