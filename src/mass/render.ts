/**
 * Conversation rendering, ported from MASS training/prepare_sft_data.py up to (not including)
 * tokenization: group logged chat requests into threads, take each thread's final request plus its
 * response as one conversation, and replace the orchestrator's teacher prompt (task + workflow +
 * directive) with the bare task so the student learns to act without the workflow scaffold.
 * Output rows are chat-format JSONL ({"messages", "tools"}) usable by mlx_lm.lora / HF chat templates.
 */
import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

export function loadRequests(logsDir: string): any[] {
	const p = join(logsDir, "api_requests.jsonl");
	if (!existsSync(p)) return [];
	return readFileSync(p, "utf8")
		.split("\n")
		.filter((l) => l.trim())
		.flatMap((l) => {
			try {
				return [JSON.parse(l)];
			} catch {
				return [];
			}
		});
}

const stable = (x: unknown): string => JSON.stringify(x, (_k, v) => (v && typeof v === "object" && !Array.isArray(v) ? Object.fromEntries(Object.entries(v).sort()) : v));

/**
 * MASS keys a thread on (messages[0], messages[1]) because qwen-code always sends a system prompt first.
 * pi-durable sends no system message when no prompt sections render, so without a system message the key is the
 * first (user) message alone; with a system prompt this is identical to MASS.
 */
export function threadKey(msgs: any[]): string {
	const u = msgs[0]?.role === "system" && msgs.length > 1 ? msgs[1].content : "";
	return createHash("md5").update(stable(msgs[0]?.content) + "\u0000" + stable(u)).digest("hex").slice(0, 10);
}

export function messagesPrefix(a: any[], b: any[]): boolean {
	return a.length <= b.length && a.every((x, i) => stable(x) === stable(b[i]));
}

export function groupThreads(chats: any[]): { key: string; segment: any[] }[] {
	const byKey = new Map<string, any[]>();
	for (const r of chats) {
		const k = threadKey(r.request.messages);
		byKey.set(k, [...(byKey.get(k) ?? []), r]);
	}
	const convs: { key: string; segment: any[] }[] = [];
	for (const [key, rs] of byKey) {
		rs.sort((a, b) => a.seq - b.seq);
		let seg = [rs[0]];
		for (const r of rs.slice(1)) {
			if (messagesPrefix(seg[seg.length - 1].request.messages, r.request.messages)) seg.push(r);
			else {
				convs.push({ key, segment: seg });
				seg = [r];
			}
		}
		convs.push({ key, segment: seg });
	}
	return convs;
}

export function responseToReplayMessage(m: any): any {
	const out: any = { role: "assistant", content: m?.content ?? "" };
	if (m?.reasoning_content) out.reasoning_content = m.reasoning_content;
	if (m?.tool_calls?.length) out.tool_calls = m.tool_calls.map((t: any) => ({ id: t.id, type: t.type ?? "function", function: { name: t.function.name, arguments: t.function.arguments } }));
	return out;
}

const userText = (c: any): string => (typeof c === "string" ? c : Array.isArray(c) ? c.map((x: any) => x?.text ?? "").join("") : "");

/** Render one episode's logs into conversations. Orchestrator = the thread whose first user message is the teacher prompt. */
export function renderEpisode(logsDir: string, teacherPrompt: string, barePrompt: string, meta: Record<string, unknown> = {}) {
	const chats = loadRequests(logsDir).filter((r) => r.status === 200 && r.request?.messages && r.response?.choices?.[0]?.message && String(r.path).includes("chat/completions"));
	const rows: any[] = [];
	for (const { key, segment } of groupThreads(chats)) {
		const last = segment[segment.length - 1];
		const msgs = structuredClone(last.request.messages);
		const firstUser = msgs.findIndex((m: any) => m.role === "user");
		const isOrch = firstUser >= 0 && userText(msgs[firstUser].content).trim() === teacherPrompt.trim();
		if (isOrch) msgs[firstUser] = { ...msgs[firstUser], content: barePrompt };
		msgs.push(responseToReplayMessage(last.response.choices[0].message));
		rows.push({ ...meta, thread: key, role: isOrch ? "orchestrator" : "worker", n_requests: segment.length, messages: msgs, tools: last.request.tools ?? [] });
	}
	return rows;
}
