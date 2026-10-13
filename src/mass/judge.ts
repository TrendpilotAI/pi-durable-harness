import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import type { MlxConfig } from "../config.ts";
import { callJson } from "./llm.ts";

const SKIP = new Set([".git", "node_modules", "__pycache__", ".venv", ".pytest_cache", ".mypy_cache"]);

/** Workspace evidence bundle (MASS evaluation.bundle analogue, much smaller): file tree, file heads, check result. */
export function workspaceBundle(workspace: string, checkJson?: string, maxChars = 24000): string {
	const files: string[] = [];
	const walk = (d: string) => {
		if (!existsSync(d)) return;
		for (const n of readdirSync(d).sort()) {
			if (SKIP.has(n)) continue;
			const p = join(d, n);
			const st = statSync(p);
			if (st.isDirectory()) walk(p);
			else if (st.size < 200_000) files.push(p);
		}
	};
	walk(workspace);
	let out = `FILES (${files.length}):\n${files.map((f) => "- " + relative(workspace, f)).join("\n")}\n`;
	if (checkJson && existsSync(checkJson)) {
		const c = JSON.parse(readFileSync(checkJson, "utf8"));
		out += `\nAUTOMATED CHECK: ran=${c.ran} exit_code=${c.exit_code}\n${String(c.output ?? "").slice(-2000)}\n`;
	}
	for (const f of files) {
		if (out.length > maxChars) break;
		const text = readFileSync(f, "utf8");
		if (text.includes("\u0000")) continue;
		out += `\n===== ${relative(workspace, f)} =====\n${text.slice(0, 4000)}\n`;
	}
	return out.slice(0, maxChars);
}

export const JUDGE_SCHEMA = {
	type: "object",
	properties: { winner: { type: "string", enum: ["A", "B", "tie"] }, rationale: { type: "string" } },
	required: ["winner", "rationale"],
	additionalProperties: false,
};

export const JUDGE_SYSTEM =
	"You are a strict evaluator comparing two workspaces produced for the same software task. Judge only delivered artifacts: correctness, completeness against the stated deliverables, passing checks, code quality, and honest reporting. Ignore length for its own sake. Return JSON {\"winner\": \"A\"|\"B\"|\"tie\", \"rationale\": \"...\"} with a short, evidence-based rationale.";

/**
 * In-loop pairwise self-evaluation with the same model (MASS judging.self_judge). Presentation order is
 * swapped by seed and the verdict mapped back, to reduce position bias.
 */
export async function pairwiseJudge(cfg: MlxConfig, taskText: string, bundleA: string, bundleB: string, seed: number, fetchImpl?: typeof fetch) {
	const swapped = seed % 2 === 1;
	const [left, right] = swapped ? [bundleB, bundleA] : [bundleA, bundleB];
	const user = `TASK:\n${taskText}\n\n##### CANDIDATE A #####\n${left}\n\n##### CANDIDATE B #####\n${right}\n\nWhich candidate better completes the task?`;
	const res = await callJson(cfg, JUDGE_SYSTEM, user, { schema: JUDGE_SCHEMA, seed, maxTokens: 2048, thinking: false, fetchImpl });
	let winner: string = res.payload.winner;
	if (!["A", "B", "tie"].includes(winner)) winner = "tie";
	const presentedWinner = winner; // label as the judge saw it; the rationale uses these labels
	if (swapped) winner = ({ A: "B", B: "A" } as Record<string, string>)[winner] ?? winner;
	return { winner, presented_winner: presentedWinner, rationale_labels: swapped ? "presented order swapped: rationale 'A' = this record's B" : "presented order = record order", rationale: String(res.payload.rationale ?? ""), swapped, seed, model: cfg.model.id, metrics: res.metrics };
}
