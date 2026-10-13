import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

export const DESIGN_MARKER = "Create an agent team with following agent candidates to solve this problem:";
export const DIRECTIVE_MARKER = "## Execution requirements";

/** MASS QueryTemplate: the task text, the replaceable multi-agent design block, and the suffix. */
export type QueryTemplate = { beforeDesign: string; designBlock: string; afterDesign: string };

export function parseTemplate(raw: string): QueryTemplate {
	const i = raw.indexOf(DESIGN_MARKER);
	if (i < 0) throw new Error("workflow prompt lacks the design marker");
	const rest = raw.slice(i);
	const j = rest.indexOf(DIRECTIVE_MARKER);
	return {
		beforeDesign: raw.slice(0, i),
		designBlock: (j < 0 ? rest : rest.slice(0, j)).trimEnd(),
		afterDesign: j < 0 ? "" : "\n\n" + rest.slice(j),
	};
}

export function renderTemplate(t: QueryTemplate, design: string): string {
	let d = design.trim();
	if (!d.startsWith(DESIGN_MARKER)) d = `${DESIGN_MARKER}\n${d}`;
	return t.beforeDesign + d + t.afterDesign;
}

/** Task files follow the MASS layout: tasks/bare/query<ID>.txt, tasks/initial/query<ID>_ourTeam.txt. */
export type TaskFiles = { id: number; bare: string; initial: string; check?: string };

export function taskFiles(tasksDir: string, id: number): TaskFiles {
	const bare = join(tasksDir, `bare/query${id}.txt`);
	const initial = join(tasksDir, `initial/query${id}_ourTeam.txt`);
	const check = join(tasksDir, `checks/query${id}.sh`);
	for (const p of [bare, initial]) if (!existsSync(p)) throw new Error(`missing task file ${p}`);
	return { id, bare, initial, check: existsSync(check) ? check : undefined };
}

export function readText(p: string): string {
	return readFileSync(p, "utf8");
}
