import { execFileSync } from "node:child_process";
import { copyFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import type { MlxConfig } from "../config.ts";
import { startLoggingProxy } from "../proxy.ts";
import { renameSync } from "node:fs";
import { CONTINUE_PROMPT, SessionManager } from "../session.ts";

export const RUNTIME = "pi-durable-harness/0.1.0 (@earendil-works/pi-durable 1.2.0)";

export type EpisodeOptions = {
	cfg: MlxConfig;
	prompt: string; // path of the prompt file
	workspace: string;
	logs: string;
	seed: number;
	condition: string;
	check?: string; // task check script, run in the workspace after the episode
	timeoutMs?: number;
	thinkingLevel?: string;
	/** Resume an episode that finished unsuccessfully (e.g. server-side decode failures): archive its run_status and continue the same durable session. */
	retryFailed?: boolean;
	log?: (s: string) => void;
};

export function readJson(p: string): any {
	return JSON.parse(readFileSync(p, "utf8"));
}

/** MASS `completed()`: exit_code 0 and a success result. */
export function completed(logs: string): boolean {
	const p = join(logs, "run_status.json");
	if (!existsSync(p)) return false;
	const st = readJson(p);
	return st.exit_code === 0 && st.trace_has_success_result === true;
}

function countLines(p: string): number {
	if (!existsSync(p)) return 0;
	return readFileSync(p, "utf8").split("\n").filter((l) => l.trim()).length;
}

export function runCheck(check: string | undefined, workspace: string): { ran: boolean; exit_code: number | null; output: string } {
	if (!check) return { ran: false, exit_code: null, output: "" };
	try {
		const out = execFileSync("bash", [resolve(check)], { cwd: workspace, timeout: 120_000, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
		return { ran: true, exit_code: 0, output: out.slice(-4000) };
	} catch (e: any) {
		return { ran: true, exit_code: typeof e.status === "number" ? e.status : 1, output: `${e.stdout ?? ""}${e.stderr ?? ""}`.slice(-4000) };
	}
}

/**
 * Port of MASS runtime/run_episode.sh on pi-durable: one coding-agent episode in `workspace`, every model
 * request logged through the logging proxy to <logs>/api_requests.jsonl, and run_status.json written in
 * the MASS schema. The episode is a durable session under <logs>/session: if the process dies, calling
 * this again resumes the same conversation instead of starting over (MASS's runner refuses partial runs).
 */
export async function runEpisode(o: EpisodeOptions): Promise<any> {
	const log = o.log ?? (() => {});
	const statusFile = join(o.logs, "run_status.json");
	if (completed(o.logs)) {
		if (readFileSync(join(o.logs, "prompt.txt"), "utf8") !== readFileSync(o.prompt, "utf8")) throw new Error(`Completed episode has a different prompt: ${o.logs}`);
		log(`[episode] ${o.condition} already complete`);
		return readJson(statusFile);
	}
	let retry = false;
	if (existsSync(statusFile)) {
		if (!o.retryFailed) throw new Error(`Episode finished unsuccessfully; keep the evidence and use a new run_dir, or pass --retry-failed: ${o.logs}`);
		let n = 1;
		while (existsSync(join(o.logs, `run_status.attempt${n}.json`))) n++;
		renameSync(statusFile, join(o.logs, `run_status.attempt${n}.json`)); // evidence is kept
		retry = true;
		log(`[episode] ${o.condition}: retrying failed episode (attempt ${n + 1}) in the same durable session`);
	}
	mkdirSync(o.logs, { recursive: true });
	mkdirSync(o.workspace, { recursive: true });
	if (existsSync(join(o.logs, "prompt.txt"))) {
		if (readFileSync(join(o.logs, "prompt.txt"), "utf8") !== readFileSync(o.prompt, "utf8")) throw new Error(`Partial episode has a different prompt: ${o.logs}`);
	} else copyFileSync(o.prompt, join(o.logs, "prompt.txt"));
	const apiLog = join(o.logs, "api_requests.jsonl");
	const proxy = await startLoggingProxy(o.cfg.baseUrl, apiLog, { startSeq: countLines(apiLog) });
	const sessions = new SessionManager(join(o.logs, "session"), o.cfg);
	const startedAt = new Date().toISOString();
	let meta: any;
	let failure: unknown = null;
	try {
		const exists = existsSync(join(o.logs, "session", "episode", "meta.json"));
		if (!exists) sessions.create({ id: "episode", prompt: readFileSync(o.prompt, "utf8"), cwd: o.workspace, team: true, thinkingLevel: o.thinkingLevel ?? "off" });
		else log(`[episode] ${o.condition}: resuming durable session after interruption`);
		const runOpts = { baseUrl: proxy.url, timeoutMs: o.timeoutMs, onEvent: (s: string) => log(`[${o.condition}] ${s}`) };
		meta = retry ? await sessions.resume("episode", { ...runOpts, prompt: CONTINUE_PROMPT }) : await sessions.run("episode", runOpts);
	} catch (e) {
		failure = { error: String(e) };
	} finally {
		await proxy.close();
	}
	const check = runCheck(o.check, o.workspace);
	writeFileSync(join(o.logs, "check.json"), JSON.stringify(check, null, 2) + "\n");
	const answer = meta?.prompts?.[meta.prompts.length - 1]?.answerText ?? "";
	const success = meta?.status === "done" && answer.length > 0;
	const status = {
		started_at: startedAt,
		finished_at: new Date().toISOString(),
		model: o.cfg.model.id,
		endpoint: o.cfg.baseUrl,
		seed: o.seed,
		qwen_code_version: null,
		runtime: RUNTIME,
		exit_code: success ? 0 : 1,
		trace_has_success_result: success,
		failure: failure ?? (success ? null : { session_status: meta?.status, answer: answer.slice(0, 500) }),
		execution_mode: "headless_host_visible_yolo",
		operational_limits: { episode_timeout_ms: o.timeoutMs ?? null, context_window_tokens: o.cfg.model.contextWindow, max_tokens_per_model_response: o.cfg.model.maxTokens },
		hiw: { condition: o.condition, prompt_source: o.prompt, client_endpoint: proxy.url, api_log: apiLog, api_requests: countLines(apiLog), mass: true },
		check: { ran: check.ran, exit_code: check.exit_code },
		session: { id: "episode", store: join(o.logs, "session", "episode", "session.sqlite"), runs: meta?.runs ?? [] },
	};
	writeFileSync(statusFile, JSON.stringify(status, null, 2) + "\n");
	log(`[episode] ${o.condition} finished success=${success} api_requests=${status.hiw.api_requests} check=${check.exit_code}`);
	return status;
}
