import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { readJson, runEpisode } from "../src/mass/episode.ts";
import { validate } from "../src/mass/pipeline.ts";
import { SessionManager } from "../src/session.ts";
import { fakeOpenAI } from "./fixtures/fake-openai.ts";
import { testConfig } from "./fixtures/util.ts";

const lastText = (body: any) => {
	const m = body.messages.at(-1);
	return typeof m.content === "string" ? m.content : (m.content ?? []).map((c: any) => c.text ?? "").join("");
};

/** Root asks the `agent` tool once; the worker answers; the root finishes after the tool result. */
const teamReply = (body: any) => {
	if (body.messages.at(-1).role === "tool") return "final answer";
	if (body.messages.some((m: any) => JSON.stringify(m.content).includes("WORKER TASK"))) return "worker done";
	if (lastText(body).includes("follow up")) return "follow-up answer";
	return { tool: "agent", args: { agent_id: "agent_w", role_instruction: "You help.", task: "WORKER TASK" } };
};

describe("episode sampling (temperature / seed)", () => {
	it("sends temperature and seed on every request, sub-agents and resume included", async () => {
		const up = await fakeOpenAI(teamReply);
		try {
			const root = mkdtempSync(join(tmpdir(), "pdh-sampling-"));
			const sm = new SessionManager(root, { ...testConfig(), baseUrl: up.url });
			const meta = await sm.start({ id: "s1", prompt: "do it", cwd: join(root, "ws"), team: true, sampling: { temperature: 0.3, seed: 42 } });
			expect(meta.status).toBe("done");
			expect(meta.sampling).toEqual({ temperature: 0.3, seed: 42 });
			expect(up.bodies.length).toBe(3); // root, worker, root
			expect(up.bodies.some((b) => JSON.stringify(b.messages).includes("WORKER TASK"))).toBe(true);
			for (const b of up.bodies) expect([b.temperature, b.seed]).toEqual([0.3, 42]);
			// Resume with a follow-up: the stored sampling is used; a CLI override replaces only the given field.
			const again = await sm.resume("s1", { prompt: "follow up" });
			expect(again.prompts.at(-1)!.answerText).toBe("follow-up answer");
			expect([up.bodies.at(-1).temperature, up.bodies.at(-1).seed]).toEqual([0.3, 42]);
			await sm.resume("s1", { prompt: "follow up 2", sampling: { seed: 7 } });
			expect([up.bodies.at(-1).temperature, up.bodies.at(-1).seed]).toEqual([0.3, 7]);
			expect(sm.readMeta("s1").sampling).toEqual({ temperature: 0.3, seed: 7 });
		} finally {
			await up.close();
		}
	}, 60_000);

	it("sends neither field when unset (provider defaults unchanged)", async () => {
		const up = await fakeOpenAI(() => "hi");
		try {
			const root = mkdtempSync(join(tmpdir(), "pdh-sampling-"));
			const sm = new SessionManager(root, { ...testConfig(), baseUrl: up.url });
			await sm.start({ id: "s2", prompt: "do it", cwd: join(root, "ws") });
			expect(up.bodies.length).toBe(1);
			expect(up.bodies[0]).not.toHaveProperty("temperature");
			expect(up.bodies[0]).not.toHaveProperty("seed");
		} finally {
			await up.close();
		}
	}, 60_000);

	it("an episode forwards its seed (and configured temperature) and records them", async () => {
		const up = await fakeOpenAI(teamReply);
		try {
			const dir = mkdtempSync(join(tmpdir(), "pdh-episode-"));
			const prompt = join(dir, "prompt.txt");
			writeFileSync(prompt, "do it");
			const logs = join(dir, "logs");
			const st = await runEpisode({ cfg: { ...testConfig(), baseUrl: up.url }, prompt, workspace: join(dir, "ws"), logs, seed: 710101, temperature: 0.6, condition: "t1" });
			expect(st.exit_code).toBe(0);
			expect(st.seed).toBe(710101);
			expect(st.sampling).toEqual({ seed: 710101, seed_forwarded: true, temperature: 0.6 });
			const recs = readFileSync(join(logs, "api_requests.jsonl"), "utf8").trim().split("\n").map((l) => JSON.parse(l));
			expect(recs.length).toBe(3);
			for (const r of recs) expect([r.request.seed, r.request.temperature]).toEqual([710101, 0.6]);
			const noTemp = await runEpisode({ cfg: { ...testConfig(), baseUrl: up.url }, prompt, workspace: join(dir, "ws2"), logs: join(dir, "logs2"), seed: 5, condition: "t1b" });
			expect(noTemp.sampling.temperature).toBeNull();
			expect(up.bodies.at(-1).seed).toBe(5);
			expect(up.bodies.at(-1)).not.toHaveProperty("temperature");
		} finally {
			await up.close();
		}
	}, 60_000);

	it("MASS config: episode.temperature / episode.seed are optional and validated", () => {
		const base = readJson(join(__dirname, "../configs/local.json"));
		expect(validate(structuredClone(base)).episode.temperature).toBeUndefined();
		expect(validate({ ...base, episode: { ...base.episode, temperature: 0.6, seed: 3 } }).episode.seed).toBe(3);
		expect(() => validate({ ...base, episode: { ...base.episode, temperature: -1 } })).toThrow(/episode.temperature/);
		expect(() => validate({ ...base, episode: { ...base.episode, seed: 1.5 } })).toThrow(/episode.seed/);
	});
});
