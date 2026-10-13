/**
 * Local MASS pipeline (port of SakanaAI/TrendpilotAI mass/pipeline.py) on pi-durable.
 * Stages: search (RHI with fixed weights) -> collect -> rank -> select -> render. train/export are deferred.
 * Every stage is resumable: completed episodes and recorded judgments are skipped on rerun.
 */
import { appendFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { loadMlxConfig, type MlxConfig } from "../config.ts";
import { completed, readJson, runEpisode } from "./episode.ts";
import { pairwiseJudge, workspaceBundle } from "./judge.ts";
import { callJson } from "./llm.ts";
import { bradleyTerry, deriveChampionBaseline, tournamentPairs } from "./ranking.ts";
import { renderEpisode } from "./render.ts";
import { parseTemplate, readText, renderTemplate, taskFiles } from "./workflow.ts";

export type MassConfig = {
	generation: number;
	run_dir: string;
	tasks_dir: string;
	tasks: { train: number[]; test: number[] };
	search: { iterations: number; seed_base: number };
	collection: { candidates: number; train_ranks: number; validation_rank: number; seed_base: number };
	ranking: { opponents_per_episode: number; seed: number };
	episode: { timeout_ms: number; thinking_level: string };
};

export type Ctx = { c: MassConfig; cfg: MlxConfig; root: string; base: string; log: (s: string) => void; fetchImpl?: typeof fetch; retryFailed?: boolean };

export function validate(c: MassConfig): MassConfig {
	const train = new Set(c.tasks.train);
	if (c.tasks.test.some((t) => train.has(t))) throw new Error("Train and test tasks must be disjoint");
	if (c.search.iterations < 1) throw new Error("At least one RHI update is required");
	if (c.collection.validation_rank !== c.collection.train_ranks + 1) throw new Error("Validation must immediately follow the selected training ranks");
	if (c.collection.candidates < c.collection.validation_rank) throw new Error("Too few collection candidates");
	return c;
}

export function makeCtx(configPath: string, log = (s: string) => console.log(s)): Ctx {
	const c = validate(readJson(configPath));
	const root = dirname(resolve(configPath, ".."));
	const base = resolve(root, c.run_dir);
	mkdirSync(base, { recursive: true });
	const frozen = join(base, "config.json");
	if (existsSync(frozen) && JSON.stringify(readJson(frozen)) !== JSON.stringify(c)) throw new Error("Configuration differs from this run's saved config. Use a new run_dir.");
	if (!existsSync(frozen)) writeFileSync(frozen, JSON.stringify(c, null, 2) + "\n");
	return { c, cfg: loadMlxConfig(), root, base, log };
}

const qn = (q: number) => `query${q}_ourTeam`;
const tasksDir = (x: Ctx) => resolve(x.root, x.c.tasks_dir);
const records = (p: string): any[] => (existsSync(p) ? readFileSync(p, "utf8").split("\n").filter((l) => l.trim()).map((l) => JSON.parse(l)) : []);
const append = (p: string, r: unknown) => {
	mkdirSync(dirname(p), { recursive: true });
	appendFileSync(p, JSON.stringify(r) + "\n");
};
const writeJson = (p: string, v: unknown) => {
	mkdirSync(dirname(p), { recursive: true });
	writeFileSync(p, JSON.stringify(v, null, 2) + "\n");
};

export function workflowPrompt(x: Ctx, q: number, version: number): string {
	if (version === 0) return taskFiles(tasksDir(x), q).initial;
	return join(x.base, `search/${qn(q)}/updated_queries/${qn(q)}-v${version}.txt`);
}

async function episode(x: Ctx, prompt: string, workspace: string, logs: string, seed: number, condition: string, check?: string) {
	return runEpisode({ cfg: x.cfg, prompt, workspace, logs, seed, condition, check, timeoutMs: x.c.episode.timeout_ms, thinkingLevel: x.c.episode.thinking_level, log: x.log, retryFailed: x.retryFailed });
}

const OPT_SCHEMA = {
	type: "object",
	properties: {
		improved_multi_agent_design: { type: "string" },
		changes_from_previous: { type: "array", items: { type: "string" } },
		why_it_should_improve: { type: "array", items: { type: "string" } },
		evidence_used: { type: "array", items: { type: "string" } },
		expected_impact: { type: "array", items: { type: "string" } },
		verification_checks: { type: "array", items: { type: "string" } },
	},
	required: ["improved_multi_agent_design", "changes_from_previous", "why_it_should_improve", "evidence_used", "expected_impact", "verification_checks"],
	additionalProperties: false,
};

const OPT_SYSTEM =
	"You improve a multi-agent workflow design (a team of copies of the same coding model, run through an `agent` tool) for one task. Return valid JSON only. improved_multi_agent_design is the full replacement design block: it must start with the line 'Create an agent team with following agent candidates to solve this problem:' followed by a JSON array of {agent_id, role, instruction}, and must keep an orchestrator. Make evidence-targeted edits based on the executed workspace, the automated check, and the judge history; keep it compact; at most 8 items per list.";

/** RHI for one task: reference, then v0..vN, each judged vs the reference; champion arbitration; optimizer proposals. */
export async function searchTask(x: Ctx, q: number) {
	const tf = taskFiles(tasksDir(x), q);
	const taskText = readText(tf.bare);
	const out = join(x.base, `search/${qn(q)}`);
	const historyPath = join(out, "history.jsonl");
	const championPath = join(out, "champion_history.jsonl");
	const refWs = join(x.base, `reference/query${q}`);
	const refLogs = join(x.base, `logs/reference/query${q}`);
	await episode(x, tf.bare, refWs, refLogs, x.c.search.seed_base + q, `t${q}_bare`, tf.check);
	const last = x.c.search.iterations;
	for (let k = 0; k <= last; k++) {
		const ws = join(x.base, `workspaces/v${k}/${qn(q)}`);
		const logs = join(x.base, `logs/search/v${k}/${qn(q)}`);
		await episode(x, workflowPrompt(x, q, k), ws, logs, x.c.search.seed_base + q + k * 1000, `t${q}_v${k}`, tf.check);
		// In-loop evaluator: version vs reference (A = reference, B = version).
		const hist = records(historyPath);
		const key = `v${k}_vs_no_harness`;
		if (!hist.some((r) => r.key === key)) {
			const v = await pairwiseJudge(x.cfg, taskText, workspaceBundle(refWs, join(refLogs, "check.json")), workspaceBundle(ws, join(logs, "check.json")), x.c.ranking.seed + q * 100 + k, x.fetchImpl);
			const row = { key, kind: "vs_baseline", version: k, repo_a: refWs, repo_b: ws, ...v, judged_at_utc: new Date().toISOString() };
			append(historyPath, row);
			x.log(`[rhi] task ${q} ${key}: winner=${v.winner}`);
		}
		let champ = deriveChampionBaseline(records(historyPath), records(championPath), k);
		if (champ.needsArbitration) {
			const cw = join(x.base, `workspaces/v${champ.champion}/${qn(q)}`);
			const cl = join(x.base, `logs/search/v${champ.champion}/${qn(q)}`);
			const v = await pairwiseJudge(x.cfg, taskText, workspaceBundle(cw, join(cl, "check.json")), workspaceBundle(ws, join(logs, "check.json")), x.c.ranking.seed + q * 100 + 50 + k, x.fetchImpl);
			append(championPath, { key: `champion_v${champ.champion}_vs_v${k}`, kind: "champion_arbitration", version_a: champ.champion, version_b: k, ...v, judged_at_utc: new Date().toISOString() });
			x.log(`[rhi] task ${q} champion_v${champ.champion}_vs_v${k}: winner=${v.winner}`);
			champ = deriveChampionBaseline(records(historyPath), records(championPath), k);
		}
		if (k === last) {
			const result = { version: champ.hasWin ? champ.champion : null, beats_reference: champ.hasWin, model: x.cfg.model.id, last_evaluated_version: k };
			writeJson(join(out, "retained_workflow.json"), result);
			x.log(`[rhi] task ${q} retained ${JSON.stringify(result)}`);
			return result;
		}
		const next = workflowPrompt(x, q, k + 1);
		if (existsSync(next)) continue;
		// Optimizer: the same model proposes the next design.
		const tmpl = parseTemplate(readText(workflowPrompt(x, q, k)));
		const histText = records(historyPath).map((r) => `${r.key}: winner=${r.winner} — ${String(r.rationale).slice(0, 600)}`).join("\n");
		const best = champ.hasWin && champ.champion !== k ? `\n\nBEST DESIGN SO FAR (v${champ.champion}):\n${parseTemplate(readText(workflowPrompt(x, q, champ.champion))).designBlock}` : "";
		const user = `TASK:\n${taskText}\n\nCURRENT DESIGN (v${k}):\n${tmpl.designBlock}${best}\n\nEXECUTED WORKSPACE EVIDENCE (v${k}):\n${workspaceBundle(ws, join(logs, "check.json"), 12000)}\n\nJUDGE HISTORY (A = no-workflow reference, B = workflow version):\n${histText}\n\nPropose the improved design for v${k + 1}.`;
		const prop = await callJson(x.cfg, OPT_SYSTEM, user, { schema: OPT_SCHEMA, seed: x.c.search.seed_base + q + k, maxTokens: 6144, thinking: false, fetchImpl: x.fetchImpl });
		writeJson(join(out, `proposals/v${k + 1}.json`), { ...prop.payload, metrics: prop.metrics });
		mkdirSync(dirname(next), { recursive: true });
		writeFileSync(next, renderTemplate(tmpl, String(prop.payload.improved_multi_agent_design)));
		x.log(`[rhi] task ${q} proposed v${k + 1}: ${(prop.payload.changes_from_previous ?? []).slice(0, 3).join(" | ")}`);
	}
}

export async function collect(x: Ctx, tasks: number[]) {
	const manifest = join(x.base, "pool.json");
	const rows: Record<string, any> = existsSync(manifest) ? Object.fromEntries(readJson(manifest).map((r: any) => [r.episode, r])) : {};
	const directive = readText(join(tasksDir(x), "teacher_directive.txt"));
	for (const q of tasks) {
		if (!x.c.tasks.train.includes(q)) throw new Error(`Task ${q} is excluded from SFT collection`);
		const sel = readJson(join(x.base, `search/${qn(q)}/retained_workflow.json`));
		if (!sel.beats_reference) {
			x.log(`Task ${q}: no retained workflow beats the reference; excluded from collection.`);
			continue;
		}
		const teacher = join(x.base, `teacher_prompts/query${q}.txt`);
		const text = readText(workflowPrompt(x, q, sel.version)).trimEnd() + "\n\n" + directive;
		if (existsSync(teacher) && readText(teacher) !== text) throw new Error(`Teacher prompt changed: ${teacher}`);
		writeJson(teacher + ".json", { version: sel.version });
		writeFileSync(teacher, text);
		const tf = taskFiles(tasksDir(x), q);
		for (let i = 0; i < x.c.collection.candidates; i++) {
			const seed = x.c.collection.seed_base + 100 * q + i;
			const condition = `t${q}_L${x.c.generation}`;
			const name = `${condition}_s${seed}`;
			const ws = join(x.base, `pool/workspaces/${name}`);
			const logs = join(x.base, `pool/runlogs/${name}`);
			await episode(x, teacher, ws, logs, seed, condition, tf.check);
			rows[name] = { episode: name, task: q, condition, seed, workspace: ws, logs_dir: logs, teacher_prompt: teacher };
			writeJson(manifest, Object.values(rows).sort((a: any, b: any) => a.episode.localeCompare(b.episode)));
		}
	}
}

export async function rank(x: Ctx, tasks: number[]) {
	const pool = readJson(join(x.base, "pool.json"));
	for (const q of tasks) {
		const retained = readJson(join(x.base, `search/${qn(q)}/retained_workflow.json`));
		if (!retained.beats_reference) continue;
		const taskText = readText(taskFiles(tasksDir(x), q).bare);
		const rows: Record<string, any> = Object.fromEntries(pool.filter((r: any) => r.task === q && completed(r.logs_dir)).map((r: any) => [r.episode, r]));
		const names = Object.keys(rows).sort();
		if (names.length < x.c.collection.validation_rank) throw new Error(`Task ${q}: need at least ${x.c.collection.validation_rank} successful candidates, found ${names.length}`);
		const pairs = tournamentPairs(names, x.c.ranking.opponents_per_episode, x.c.ranking.seed + q);
		const path = join(x.base, `selection/task${q}_matches.jsonl`);
		const done = new Map(records(path).map((r) => [`${r.a}|${r.b}`, r]));
		for (const [i, [a, b]] of pairs.entries()) {
			if (done.has(`${a}|${b}`)) continue;
			const v = await pairwiseJudge(x.cfg, taskText, workspaceBundle(rows[a].workspace, join(rows[a].logs_dir, "check.json")), workspaceBundle(rows[b].workspace, join(rows[b].logs_dir, "check.json")), x.c.ranking.seed + q * 1000 + i, x.fetchImpl);
			const rec = { a, b, ...v };
			done.set(`${a}|${b}`, rec);
			append(path, rec);
		}
		const matches = new Map<string, Record<string, string>>();
		for (const [a, b] of pairs) {
			const w = done.get(`${a}|${b}`)!.winner;
			matches.set(JSON.stringify([a, b]), { self: w === "A" ? a : w === "B" ? b : "tie" });
		}
		const scores = bradleyTerry(names, matches);
		const ordered = [...names].sort((m, n) => scores[n] - scores[m] || m.localeCompare(n));
		writeJson(join(x.base, `selection/task${q}_ranked.json`), ordered.map((n, i) => ({ ...rows[n], score: scores[n], rank: i + 1 })));
	}
}

export function select(x: Ctx) {
	const train: any[] = [];
	const val: any[] = [];
	for (const q of x.c.tasks.train) {
		const p = join(x.base, `search/${qn(q)}/retained_workflow.json`);
		if (!existsSync(p) || !readJson(p).beats_reference) continue;
		const rows = readJson(join(x.base, `selection/task${q}_ranked.json`));
		if (rows.length < x.c.collection.validation_rank) throw new Error(`Missing validation rank for task ${q}`);
		train.push(...rows.slice(0, x.c.collection.train_ranks));
		val.push(rows[x.c.collection.validation_rank - 1]);
	}
	if (!train.length || !val.length) throw new Error("No eligible training/validation trajectories");
	writeJson(join(x.base, "selection/train.json"), train);
	writeJson(join(x.base, "selection/val.json"), val);
	x.log(`Selected ${train.length} training and ${val.length} validation trajectories.`);
}

export function render(x: Ctx) {
	for (const split of ["train", "val"]) {
		const rows = readJson(join(x.base, `selection/${split}.json`));
		const out = join(x.base, `data/${split}.jsonl`);
		mkdirSync(dirname(out), { recursive: true });
		writeFileSync(out, "");
		const stats: Record<string, number> = { episodes: rows.length, conversations: 0, orchestrator: 0, worker: 0 };
		for (const r of rows) {
			const convs = renderEpisode(r.logs_dir, readText(r.teacher_prompt), readText(taskFiles(tasksDir(x), r.task).bare), { episode: r.episode, task: r.task, condition: r.condition, rank: r.rank });
			if (!convs.some((c) => c.role === "orchestrator")) throw new Error(`${split}: ${r.episode} has no orchestrator conversation (strict rendering)`);
			for (const c of convs) {
				appendFileSync(out, JSON.stringify(c) + "\n");
				stats.conversations++;
				stats[c.role]++;
			}
		}
		writeJson(join(x.base, `data/${split}_stats.json`), stats);
		x.log(`[render] ${split}: ${JSON.stringify(stats)}`);
	}
}

export async function cycle(x: Ctx, tasks?: number[]) {
	const t = tasks ?? x.c.tasks.train;
	for (const q of t) await searchTask(x, q);
	await collect(x, t.filter((q) => x.c.tasks.train.includes(q)));
	await rank(x, t);
	select(x);
	render(x);
}
